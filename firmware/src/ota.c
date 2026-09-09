/*
 * OTA engine: resolve the latest GitHub release, stream the signed image
 * into slot1, verify its SHA-256, and hand it to MCUboot as a test image.
 *
 * GitHub answers /releases/latest/download/<asset> with a 302 to a
 * pre-signed CDN URL, so every fetch is two TLS legs with different trust
 * anchors. Fixed asset names keep the device's request path constant; the
 * manifest's sha256 pins the exact bytes, so a release changing between
 * check and install fails safe at the hash step -- and even a hash-passing
 * forgery still dies at MCUboot's ECDSA-P256 check. Runs entirely on the
 * net-worker thread; the UI only ever touches the snapshot + request flags.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/net/http/client.h>
#include <zephyr/dfu/flash_img.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/printk.h>
#include <mbedtls/sha256.h>
#include <string.h>
#include <strings.h>	/* strncasecmp; string.h does not declare it */
#include <stdio.h>

#include "ota.h"
#include "certs.h"
#include "version.h"

#define CA_TAG_GITHUB 3
#define CA_TAG_GH_CDN 4
#define OTA_HOST "github.com"
#define OTA_BASE "/KfirLevy258/Blink/releases/latest/download/"
/* Fixed asset names; the manifest's sha256 pins the exact bytes, so a release
 * changing between check and install fails safe at the hash step. */
#define OTA_MANIFEST_PATH OTA_BASE "manifest.json"
#define OTA_IMAGE_PATH    OTA_BASE "blink-fw.bin"

/* Must hold a whole 302 response head fragment INCLUDING the Location header:
 * capture_headers() scans one fragment at a time and cannot stitch a header
 * across a boundary, so a Location that straddles two fragments is captured
 * short. GitHub's release-asset Location is ~905 chars today (SAS params + a
 * JWT), and their 302 heads run ~5 KB, so 1536 left too little room -- 3072
 * keeps the header comfortably inside one fragment. */
static uint8_t recv_buf[3072];

/* --- redirect capture: latest/download answers with TWO 302s on GitHub today
 * (github.com -> the tag URL on github.com -> the CDN), so we follow a chain,
 * not a single hop. --- */
/* GitHub's release-asset redirect is ~905 chars and grew there over time (SAS
 * query params plus a JWT). At the old 768 it was silently truncated mid-JWT:
 * the CDN then answered the mangled URL with a non-HTTP status (618 observed),
 * which is what actually broke the update check -- verified on hardware
 * 2026-07-25 by tracing each hop. Sized with headroom for further growth; a
 * truncation here is not a graceful degradation, it is an outage. */
static char redirect_url[1536];
static char small_body[512];	/* manifest.json is ~120 bytes */
static size_t small_len;
static int http_status;

/* --- install-leg streaming state (single worker thread; no locking) --- */
static struct flash_img_context fictx;
static mbedtls_sha256_context sha;
static uint32_t dl_total, dl_got;
static int stream_err;
/* Heartbeat for the download. A 1.28 MB image over WiFi runs for MINUTES
 * (measured: 416 s on a weak link, ~3 KB/s average), and without this the
 * serial log goes silent for the whole transfer -- so a stalled download, a
 * slow one, and a healthy one all look identical from outside, and a user
 * reporting "it sat at 100% for ages" cannot be checked against anything.
 * The rate is per-interval, not cumulative, so a collapsing link shows up
 * immediately instead of being hidden by a good average. */
#define DL_LOG_INTERVAL_MS 5000
static int64_t dl_started_ms;
static int64_t dl_logged_ms;
static uint32_t dl_logged_bytes;

/* --- UI snapshot + request flags --- */
static struct ota_ui ui;
static struct k_spinlock ui_lock;
static atomic_t req_check;
static atomic_t req_install;
static atomic_t badge;
static struct ota_manifest last_m;	/* last successful check; worker only */

static void capture_headers(struct http_response *rsp)
{
	http_status = rsp->http_status_code;

	if ((http_status == 301 || http_status == 302) && !redirect_url[0]) {
		/* Scan the raw buffer: Zephyr's client exposes no per-header
		 * hook. This sees ONE fragment, so the Location header must land
		 * whole inside it -- see recv_buf's sizing note. */
		const char *p = rsp->recv_buf;
		size_t n = rsp->data_len;

		for (size_t i = 0; i + 10 < n; i++) {
			if (strncasecmp(&p[i], "location:", 9) != 0) {
				continue;
			}
			i += 9;
			while (i < n && p[i] == ' ') {
				i++;
			}
			size_t o = 0;

			while (i < n && p[i] != '\r' && p[i] != '\n' &&
			       o < sizeof(redirect_url) - 1) {
				redirect_url[o++] = p[i++];
			}
			redirect_url[o] = '\0';
			break;
		}
	}
}

/*
 * Returns int, not void, and the type matters.
 *
 * Zephyr's http_response_cb_t returns int, and http_client.c propagates it: a
 * negative value from the HTTP_DATA_MORE path is taken as "aborted by the
 * application" and fails the transfer with -ECONNABORTED. Declared void, the
 * return register holds whatever was last in it, so a multi-fragment response
 * -- which is every reply here big enough to span two reads -- could abort on
 * garbage. Always 0: nothing below wants to stop the transfer early.
 */
static int capture_cb(struct http_response *rsp, enum http_final_call final,
		      void *u)
{
	ARG_UNUSED(final); ARG_UNUSED(u);
	capture_headers(rsp);
	if (rsp->body_frag_start && rsp->body_frag_len) {
		size_t n = rsp->body_frag_len;

		if (small_len + n > sizeof(small_body) - 1) {
			n = sizeof(small_body) - 1 - small_len;
		}
		if (n > 0) {
			memcpy(small_body + small_len, rsp->body_frag_start, n);
			small_len += n;
		}
	}
	return 0;
}

static void stream_consume(const uint8_t *d, size_t n);
static enum ota_result stream_seal(const struct ota_manifest *m);

static int stream_cb(struct http_response *rsp, enum http_final_call final,
		     void *u)
{
	ARG_UNUSED(final); ARG_UNUSED(u);
	capture_headers(rsp);
	if (http_status != 200 || stream_err ||
	    !rsp->body_frag_start || !rsp->body_frag_len) {
		return 0;
	}

	stream_consume(rsp->body_frag_start, rsp->body_frag_len);
	return 0;
}

/*
 * Hash and write one fragment, and drive the progress readout.
 *
 * Split out of the body callback so the write path and the HTTP plumbing stay
 * separable; flash_img_buffered_write() does not care where the bytes came
 * from.
 */
static void stream_consume(const uint8_t *d, size_t n)
{
	mbedtls_sha256_update(&sha, d, n);
	stream_err = flash_img_buffered_write(&fictx, (uint8_t *)d, n, false);
	if (stream_err) {
		printk("[ota] slot1 write failed: %d\n", stream_err);
		/* 0, not an error: returning negative here WOULD abort the
		 * transfer (http_client.c treats it as "aborted by the
		 * application"), and stopping a download whose flash writes are
		 * already failing is arguably right -- but the caller decides
		 * the outcome from stream_err either way, and changing when the
		 * socket closes is not something to do untested. The guard
		 * above means the remaining fragments cost only a memcpy. */
		return;
	}
	dl_got += n;
	if (dl_total) {
		uint8_t pct = (uint8_t)((uint64_t)dl_got * 100 / dl_total);
		static uint8_t last_pct = 255;

		if (pct != last_pct) {
			last_pct = pct;
			ota_ui_set(OTA_UI_DOWNLOADING, NULL, pct);
		}

		int64_t now = k_uptime_get();

		if (now - dl_logged_ms >= DL_LOG_INTERVAL_MS) {
			uint32_t span = (uint32_t)(now - dl_logged_ms);
			uint32_t rate = (dl_got - dl_logged_bytes) * 1000U / span;

			printk("[ota] %3u%%  %u/%u bytes  %u B/s  t+%us\n",
			       pct, dl_got, dl_total, rate,
			       (unsigned)((now - dl_started_ms) / 1000));
			dl_logged_ms = now;
			dl_logged_bytes = dl_got;
		}
	}
}

/* One TLS GET. body_cb==NULL captures into small_body. Returns http status
 * or negative errno. Caller passes the right CA tag per host. */
static int https_get(const char *host, const char *path, sec_tag_t tag,
		     http_response_cb_t body_cb, int32_t timeout_ms)
{
	static bool creds_added;

	if (!creds_added) {
		tls_credential_add(CA_TAG_GITHUB, TLS_CREDENTIAL_CA_CERTIFICATE,
				   ca_cert_github, sizeof(ca_cert_github));
		tls_credential_add(CA_TAG_GH_CDN, TLS_CREDENTIAL_CA_CERTIFICATE,
				   ca_cert_gh_cdn, sizeof(ca_cert_gh_cdn));
		creds_added = true;
	}

	small_len = 0;
	http_status = 0;
	redirect_url[0] = '\0';

	struct zsock_addrinfo hints = {
		.ai_family = AF_INET, .ai_socktype = SOCK_STREAM,
	};
	struct zsock_addrinfo *res = NULL;

	if (zsock_getaddrinfo(host, "443", &hints, &res) != 0 || !res) {
		return -EIO;
	}

	int sock = zsock_socket(res->ai_family, res->ai_socktype,
				IPPROTO_TLS_1_2);

	if (sock < 0) {
		zsock_freeaddrinfo(res);
		return -EIO;
	}

	sec_tag_t tags[] = { tag };

	zsock_setsockopt(sock, SOL_TLS, TLS_SEC_TAG_LIST, tags, sizeof(tags));
	zsock_setsockopt(sock, SOL_TLS, TLS_HOSTNAME, host, strlen(host) + 1);

	/* STALL timeout, not a transfer deadline. http_client_req's timeout is a
	 * budget for the WHOLE request, which is the wrong shape for a 1.28 MB
	 * download over WiFi: it has to be generous enough for a slow link, and
	 * that same generosity would leave a genuinely dead connection hanging
	 * for the entire budget. Bounding each recv instead means a stalled
	 * transfer fails in 30 s while a slow-but-progressing one runs as long
	 * as it needs. Requires CONFIG_NET_CONTEXT_RCVTIMEO (set in prj.conf) --
	 * without it these are silent no-ops. */
	struct timeval tv = { .tv_sec = 30 };

	zsock_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	zsock_setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

	int rc = zsock_connect(sock, res->ai_addr, res->ai_addrlen);

	zsock_freeaddrinfo(res);
	if (rc < 0) {
		zsock_close(sock);
		return -EIO;
	}

	const char *headers[] = {
		"User-Agent: blink/" BLINK_FW_VERSION "\r\n",
		NULL,
	};

	struct http_request req = {
		.method = HTTP_GET,
		.url = path,
		.host = host,
		.protocol = "HTTP/1.1",
		.header_fields = headers,
		.response = body_cb ? body_cb : capture_cb,
		.recv_buf = recv_buf,
		.recv_buf_len = sizeof(recv_buf),
	};

	rc = http_client_req(sock, &req, timeout_ms, NULL);
	zsock_close(sock);
	if (rc < 0) {
		return rc;
	}
	return http_status;
}

/* github.com's cert (USERTrust root) vs the CDN's (ISRG Root X1) are different
 * trust anchors, so each hop needs the right CA. Anything under
 * *.githubusercontent.com is the release-asset CDN. */
static bool host_is_cdn(const char *h)
{
	size_t n = strlen(h);
	static const char suf[] = "githubusercontent.com";
	size_t sn = sizeof(suf) - 1;

	return n >= sn && strncasecmp(h + n - sn, suf, sn) == 0;
}

/* GET `path` on github.com and follow the redirect chain (latest/download is
 * two hops today: the tag URL, then the CDN) until a 200, feeding the final
 * body to body_cb. The callbacks no-op on non-200, so intermediate 302s only
 * yield their Location. Returns OTA_OK on a 200, else an error. */
static enum ota_result fetch_follow(const char *path, http_response_cb_t body_cb,
				    int32_t timeout_ms)
{
	char host[80];
	/* static, not stack: it must match redirect_url's capacity (1536) and the
	 * net worker's 8 KB stack already carries the TLS handshake. Safe because
	 * ota.c is single-worker by contract and fetch_follow never recurses. */
	static char hpath[1536];

	strncpy(host, OTA_HOST, sizeof(host) - 1);
	host[sizeof(host) - 1] = '\0';
	strncpy(hpath, path, sizeof(hpath) - 1);
	hpath[sizeof(hpath) - 1] = '\0';

	for (int hop = 0; hop < 4; hop++) {
		sec_tag_t tag = host_is_cdn(host) ? CA_TAG_GH_CDN : CA_TAG_GITHUB;

		int st = https_get(host, hpath, tag, body_cb, timeout_ms);

		if (st < 0) {
			return OTA_ERR_NET;
		}
		if (st == 200) {
			return OTA_OK;
		}
		if (st == 301 || st == 302 || st == 303 ||
		    st == 307 || st == 308) {
			/* ota_split_url reads redirect_url into host/hpath
			 * before the next https_get clears it. */
			if (!redirect_url[0] ||
			    ota_split_url(redirect_url, host, sizeof(host),
					  hpath, sizeof(hpath)) != 0) {
				printk("[ota] bad redirect target\n");
				return OTA_ERR_PARSE;
			}
			continue;
		}
		printk("[ota] %s: unexpected status %d\n", path, st);
		return OTA_ERR_HTTP;
	}
	printk("[ota] too many redirects for %s\n", path);
	return OTA_ERR_HTTP;
}

enum ota_result ota_check(struct ota_manifest *out, bool *newer)
{
	*newer = false;

	enum ota_result r = fetch_follow(OTA_MANIFEST_PATH, NULL, 15000);

	if (r != OTA_OK) {
		return r;
	}
	if (ota_parse_manifest(small_body, small_len, out) != 0) {
		return OTA_ERR_PARSE;
	}
	if (out->size > FIXED_PARTITION_SIZE(slot1_partition)) {
		printk("[ota] image %u exceeds slot1\n", out->size);
		return OTA_ERR_SIZE;
	}
	last_m = *out;
	*newer = ota_version_newer(out->version, BLINK_FW_VERSION);
	printk("[ota] latest %s (running %s) -> %s\n", out->version,
	       BLINK_FW_VERSION, *newer ? "update available" : "up to date");
	return OTA_OK;
}

void ota_last_manifest(struct ota_manifest *out)
{
	*out = last_m;
}

static enum ota_source ui_src = OTA_SRC_WIFI;

enum ota_source ota_ui_source(void)
{
	return ui_src;
}

void ota_ui_set_source(enum ota_source src)
{
	ui_src = src;
}

enum ota_result ota_install(const struct ota_manifest *m)
{
	ui_src = OTA_SRC_WIFI;
	if (flash_img_init(&fictx) != 0) {
		return OTA_ERR_FLASH;
	}
	mbedtls_sha256_init(&sha);
	mbedtls_sha256_starts(&sha, 0);
	dl_total = m->size;
	dl_got = 0;
	stream_err = 0;

	dl_started_ms = k_uptime_get();
	dl_logged_ms = dl_started_ms;
	dl_logged_bytes = 0;

	printk("[ota] downloading %s (%u bytes)\n", m->version, m->size);

	/* 20 minutes, not 5. The old 300 s was a TOTAL-request deadline, so any
	 * download slower than that died -- and because it expired near the end
	 * of a transfer that was otherwise fine, it looked like a server-side
	 * failure "just before the finish" (user-reported twice, 2026-07-25, at
	 * ~96%). Measured: this image needs ~3-4 min on a good link but over
	 * 8 min on a weak one, so 5 minutes was simply too tight.
	 *
	 * Safe to be this generous only because https_get now sets a 30 s recv
	 * stall timeout: a dead connection still fails quickly, and this bound
	 * exists purely to stop a pathological transfer running forever. The
	 * signed CDN URL's own 300 s token life is NOT a constraint here --
	 * measured 2026-07-25: a deliberately rate-limited 412 s download
	 * completed with HTTP 200, so the token is checked when the request is
	 * made, not while it streams. */
	enum ota_result r = fetch_follow(OTA_IMAGE_PATH, stream_cb, 1200000);

	/* Separate from the last heartbeat on purpose: the bar reaches 100% on
	 * the final data fragment, but the request is not done until the server
	 * closes the connection. Any gap between the last heartbeat and this
	 * line is time the UI has been showing a full bar with nothing left to
	 * report -- exactly the "it sat there for ages" window, and previously
	 * invisible. */
	printk("[ota] transfer ended: %u/%u bytes in %us\n", dl_got, dl_total,
	       (unsigned)((k_uptime_get() - dl_started_ms) / 1000));

	if (r != OTA_OK) {
		mbedtls_sha256_free(&sha);
		return r;
	}
	return stream_seal(m);
}

/*
 * Close a completed transfer: flush the writer, check the length, verify the
 * hash, and only then hand slot1 to MCUboot.
 *
 * Nothing is handed to MCUboot until the length and the hash both agree, so a
 * truncated or corrupted download leaves slot1 as garbage that is simply never
 * marked pending.
 */
static enum ota_result stream_seal(const struct ota_manifest *m)
{
	if (stream_err) {
		mbedtls_sha256_free(&sha);
		return OTA_ERR_FLASH;
	}
	if (flash_img_buffered_write(&fictx, NULL, 0, true) != 0) {
		mbedtls_sha256_free(&sha);
		return OTA_ERR_FLASH;
	}
	if (flash_img_bytes_written(&fictx) != m->size) {
		mbedtls_sha256_free(&sha);
		printk("[ota] truncated: %zu of %u bytes\n",
		       flash_img_bytes_written(&fictx), m->size);
		return OTA_ERR_SIZE;
	}

	uint8_t digest[32];
	char hex[65];

	mbedtls_sha256_finish(&sha, digest);
	mbedtls_sha256_free(&sha);
	for (int i = 0; i < 32; i++) {
		snprintf(&hex[i * 2], 3, "%02x", digest[i]);
	}
	if (strcmp(hex, m->sha256) != 0) {
		printk("[ota] sha256 mismatch\n");
		return OTA_ERR_HASH;
	}

	/* Slot1 now holds verified bytes; hand it to MCUboot as a test
	 * image. Anything failing before this line leaves slot1 garbage,
	 * which is harmless -- it is never marked pending. */
	if (boot_request_upgrade(BOOT_UPGRADE_TEST) != 0) {
		return OTA_ERR_FLASH;
	}
	printk("[ota] %s verified and marked pending\n", m->version);
	return OTA_OK;
}


/* --- UI <-> worker handshake --- */

void ota_ui_get(struct ota_ui *out)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	*out = ui;
	k_spin_unlock(&ui_lock, key);
}

void ota_ui_set(enum ota_ui_state st, const struct ota_manifest *m,
		uint8_t pct)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	ui.st = st;
	if (m) {
		strncpy(ui.version, m->version, sizeof(ui.version) - 1);
		ui.version[sizeof(ui.version) - 1] = '\0';
		ui.size = m->size;
	}
	ui.pct = pct;
	if (st != OTA_UI_FAILED) {
		ui.err[0] = '\0';	/* a reason outlives only its failure */
	}
	k_spin_unlock(&ui_lock, key);

	if (st == OTA_UI_AVAILABLE) {
		atomic_set(&badge, 1);
	} else if (st == OTA_UI_UP_TO_DATE || st == OTA_UI_REBOOTING) {
		atomic_set(&badge, 0);
	}
}

void ota_ui_set_error(const char *why)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	if (why && why[0]) {
		strncpy(ui.err, why, sizeof(ui.err) - 1);
		ui.err[sizeof(ui.err) - 1] = '\0';
	} else {
		ui.err[0] = '\0';
	}
	k_spin_unlock(&ui_lock, key);
}

void ota_request_check(void)
{
	atomic_set(&req_check, 1);
}

void ota_request_install(void)
{
	/*
	 * The badge is NOT cleared here any more.
	 *
	 * Clearing it at consent meant the board forgot there was an update
	 * the instant the user asked for one -- before anything had been
	 * sent, let alone accepted. On every path where the install then did
	 * not happen (a daemon that had gone away, an ota_staged cleared out
	 * from under the tap -- see upd_tap.h) the row went blank and the one
	 * affordance for trying again went with it.
	 *
	 * ota_ui_set() already clears it at REBOOTING, which is the point of
	 * no return, and at UP_TO_DATE, which is a check saying there is
	 * nothing to badge. Those are the two places it is actually true.
	 */
	atomic_set(&req_install, 1);
}

bool ota_take_check_request(void)
{
	return atomic_set(&req_check, 0) != 0;
}

bool ota_take_install_request(void)
{
	return atomic_set(&req_install, 0) != 0;
}

bool ota_badge(void)
{
	return atomic_get(&badge) != 0;
}
