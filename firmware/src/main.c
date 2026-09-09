#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/mipi_dbi.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/reboot.h>
#include <lvgl.h>
#include <mbedtls/platform.h>
#include <string.h>
#include <errno.h>

#include "proto.h"
#include "usage_view.h"
#include "cfg_store.h"
#include "backlight.h"
#include "net_wifi.h"
#include "net_time.h"
#include "portal.h"
#include "ui_setup.h"
#include "dns_hijack.h"
#include "oauth.h"
#include "usage_client.h"
#include "ui_boot.h"
#include "ui_sleep.h"
#include "sleep_gate.h"
#include "usage_freshness.h"
#include "ui_settings.h"
#include "ui_anim.h"
#include "tz_fetch.h"
#include "ota.h"
#include "upd_tap.h"
#include "whatsnew.h"
#include "ui_touchfx.h"
#include "version.h"

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

/*
 * MADCTL correction for the production batch.
 *
 * These panels differ from the pilot units in two independent ways, and both
 * land in MADCTL (36h), so one write fixes both. Pilot boards must build with
 * CONFIG_BLINK_PANEL_PILOT, which compiles this away entirely.
 *
 * The pilot units and the production panels are driven with byte-identical
 * registers, yet production renders mirrored -- so the difference is in the
 * module, not in anything we send. The usual cure for that, the SS bit in
 * DISCTRL (B6h), was tried on hardware 2026-08-21: the built devicetree
 * carried it (disctrl = [0a a2 27 04]) and the image did not budge, which
 * rules the B6h route out on this controller. MADCTL is honoured, so the
 * compensation lives there instead.
 *
 * It cannot be a devicetree `rotation`. The ili9xxx driver's CMD_SET_1 table
 * reaches only MV (90 deg) and MV|MX|MY (270 deg) in landscape, and those two
 * differ by a pure 180 deg turn -- neither un-mirrors. What this panel needs is
 * MV|MY, which no rotation value can produce. So we re-issue MADCTL ourselves
 * once, straight onto the DBI bus, after the driver has finished its init.
 *
 * Second, the colour order. The driver hardcodes the BGR bit for every
 * orientation, but these panels are wired RGB, so red and blue arrived
 * swapped. Measured 2026-08-21 with a six-bar test pattern of known values:
 * the RED bar read blue and the BLUE bar read red, while GREEN stayed green
 * and white/black were exact -- a clean channel exchange, with no inversion
 * or gamma error. Clearing bit 3 restores it. (Green surviving a red/blue
 * swap untouched is why eyeballing a green UI element misdiagnosed this.)
 *
 * Safe to overwrite: the driver writes MADCTL only from set_orientation(),
 * which runs during init and is never called again (nothing in the LVGL glue
 * touches orientation), so this value is the one that sticks. Partial updates
 * stay correct because MY flips the row-address mapping wholesale -- LVGL's
 * window coordinates and its pixels mirror together, and the panel's own
 * reversed wiring cancels them back out.
 */
#ifdef CONFIG_BLINK_PANEL_PILOT

/* Pilot panels need none of it: the stock rotation=90 MADCTL (BGR|MV) that the
 * driver writes is already correct for them, mirror and colour order alike. */
static void panel_fix_madctl(void)
{
}

#else

#define PANEL_MADCTL_MV   BIT(5)
#define PANEL_MADCTL_MY   BIT(7)

static const struct device *const panel_dbi =
	DEVICE_DT_GET(DT_PARENT(DT_NODELABEL(ili9341)));
static const struct mipi_dbi_config panel_dbi_cfg =
	MIPI_DBI_CONFIG_DT(DT_NODELABEL(ili9341),
			   SPI_OP_MODE_MASTER | SPI_WORD_SET(8), 0);

static void panel_fix_madctl(void)
{
	uint8_t madctl = PANEL_MADCTL_MV | PANEL_MADCTL_MY;	/* BGR bit clear = RGB */
	int r;

	if (!device_is_ready(panel_dbi)) {
		printk("[usage] panel: DBI bus not ready, MADCTL uncorrected\n");
		return;
	}

	r = mipi_dbi_command_write(panel_dbi, &panel_dbi_cfg, 0x36, &madctl, 1);
	if (r < 0) {
		printk("[usage] panel: MADCTL write failed (%d)\n", r);
	} else {
		printk("[usage] panel: MADCTL=0x%02x\n", madctl);
	}
}

#endif /* CONFIG_BLINK_PANEL_PILOT */

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/*
 * Everything from here to the matching #endif belongs to the standalone WiFi
 * flow -- provisioning, the setup AP, the sign-in exchange, the blind-radio
 * retry. Every use of it is already inside the gate further down; only these
 * declarations sat outside, which made a USB-only build emit six
 * "defined but not used" warnings and hid anything genuine among them.
 */
/* Provisioning session state (one PKCE verifier per setup attempt). */
static char verifier[OAUTH_VERIFIER_LEN];
static char authorize_url[OAUTH_URL_LEN];

/*
 * The setup AP's WPA2 password: random once, persisted, carried by the QR.
 * Random rather than MAC-derived on purpose -- the MAC's vendor bytes are
 * guessable and its tail is broadcast in the SSID, so a derived password
 * would protect nothing. Factory reset clears it; the next boot rolls a
 * fresh one here.
 */
static void ap_psk_setup(void)
{
	/* No 0/O/1/l/I look-alikes: the password is only ever read by a
	 * camera, but a human may need to type it once if the QR scan is
	 * refused. */
	static const char alphabet[] = "abcdefghjkmnpqrstuvwxyz23456789";
	char psk[CFG_AP_PSK_MAX];

	if (!cfg_get_ap_psk(psk, sizeof(psk))) {
		uint8_t rnd[10];

		sys_rand_get(rnd, sizeof(rnd));
		for (int i = 0; i < 10; i++) {
			psk[i] = alphabet[rnd[i] % (sizeof(alphabet) - 1)];
		}
		psk[10] = '\0';
		cfg_set_ap_psk(psk);
	}
	net_wifi_set_ap_psk(psk);
}

/*
 * The radio sometimes comes out of a reset blind: every scan returns nothing
 * and joins time out, for that entire boot; another reboot re-rolls it
 * (observed on this hardware 2026-07-13, roughly every other soft reset).
 * These counters live in .noinit RAM so they survive the reboot the retry
 * depends on; the magic guards against power-on garbage.
 */
static __noinit uint32_t blind_boots;
static __noinit uint32_t blind_magic;
#define BLIND_MAGIC 0xb11dbea7u
#define BLIND_MAX 4

/* The boot scan could not see the stored SSID. The join still runs -- scans
 * miss hidden SSIDs and barely-beaconing phone hotspots (an iPhone hotspot
 * with a fresh token got bounced to setup this way, 2026-07-15) -- this
 * flag only picks the honest reason to show on the setup form if that
 * join fails too. */
static bool scan_said_absent;
#endif /* CONFIG_BLINK_WIFI_MODE */

/* ---- OTA boot-side: test-boot self-confirm, else MCUboot reverts ---- */

static bool ota_test_boot;
static bool ota_health;		/* mode proved WiFi + TLS + usage (or daemon) */
static int64_t ota_confirm_deadline;
static const struct device *wdt;
static int wdt_chan = -1;

static void ota_boot_begin(void)
{
	if (boot_is_img_confirmed()) {
		return;
	}
	ota_test_boot = true;
	ota_confirm_deadline = k_uptime_get() + 90 * 1000;
	printk("[ota] test boot of %s -- must confirm within 90 s\n",
	       BLINK_FW_VERSION);

	/* Hardware watchdog for hard hangs: a wedged main loop stops feeding,
	 * the chip resets, and MCUboot reverts the unconfirmed image. */
	wdt = DEVICE_DT_GET_OR_NULL(DT_ALIAS(watchdog0));
	if (wdt && device_is_ready(wdt)) {
		struct wdt_timeout_cfg cfg = {
			.window.max = 30000,
			.flags = WDT_FLAG_RESET_SOC,
		};

		wdt_chan = wdt_install_timeout(wdt, &cfg);
		if (wdt_chan >= 0) {
			wdt_setup(wdt, 0);
		}
	}
}

static void ota_boot_pump(void)
{
	if (!ota_test_boot) {
		return;
	}
	if (wdt_chan >= 0) {
		wdt_feed(wdt, wdt_chan);	/* alive != healthy; deadline judges health */
	}
	if (ota_health) {
		boot_write_img_confirmed();
		ota_test_boot = false;
		if (wdt_chan >= 0) {
			wdt_disable(wdt);
			wdt_chan = -1;
		}
		printk("[ota] image confirmed\n");
		return;
	}
	if (k_uptime_get() > ota_confirm_deadline) {
		printk("[ota] not healthy within 90 s -- rebooting to revert\n");
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
}

/*
 * "Do not close this board's eyes right now", in one place.
 *
 * There were two hand-written copies of this condition and they had already
 * drifted: the doze in run_usb() carried ota_test_boot, and
 * reading_moved_again() -- whose own comment calls it the mirror of that
 * doze -- did not. The drift is unreachable today, because ota_test_boot is
 * raised once in ota_boot_begin() before any mode loop exists and from then
 * on only ever falls. But it is exactly the term whose absence cost a board
 * on the bench (2026-09-03): dozing blocks the loop that feeds the 30 s
 * watchdog an unconfirmed image arms, the SoC resets, MCUboot re-runs the
 * same unconfirmed image, and it dozes again at 60 s -- forever. A condition
 * that has to be right in two files to keep a board out of a reset loop
 * should exist once.
 *
 * "An update is in flight" honestly covers "the new image has not proved
 * itself yet", which is why the test boot rides this term instead of
 * becoming a fourth condition at each call site; staying awake is also what
 * lets the deliberate revert at the 90 s deadline happen, logged, rather
 * than as a bare watchdog reset nobody can read afterwards.
 *
 * Read fresh on every call, never cached: an update that starts while the
 * board is already dozing has to put the progress screen back up.
 */
static bool ota_blocks_sleep(void)
{
	struct ota_ui snap;

	ota_ui_get(&snap);
	return snap.st == OTA_UI_DOWNLOADING || snap.st == OTA_UI_REBOOTING ||
	       ota_test_boot;
}

/* Did the previous boot's install land? Runs once the gauge screen exists so
 * the notice popup has somewhere to live. */
static void ota_report_outcome(void)
{
	char trail[CFG_OTA_VER_MAX];
	char from[CFG_OTA_VER_MAX], to[CFG_OTA_VER_MAX];
	/* Static rather than automatic: this is a quarter of a kilobyte and
	 * it runs on the main thread during boot, beside the LVGL work. It
	 * is called once, so the BSS is the cheaper of the two. */
	static char msg[WHATSNEW_MAX + 48];

	if (cfg_get_ota_state(trail, sizeof(trail)) != 1) {
		return;
	}
	cfg_set_ota_state(0, "");
	/* Through whatsnew_split, always: the breadcrumb may be a packed
	 * pair, and comparing "1.2.5>1.3.2" against BLINK_FW_VERSION would
	 * announce a successful update as a failed one. */
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	if (strcmp(to, BLINK_FW_VERSION) == 0) {
		int n = snprintf(msg, sizeof(msg), "Updated to version %s.",
				 BLINK_FW_VERSION);

		/*
		 * The notes go after the header's NUL, which is then
		 * overwritten with the newline that joins them -- so a
		 * release with nothing to say about itself leaves exactly
		 * the sentence this popup showed before, rather than a
		 * heading with a blank space under it.
		 */
		if (n > 0 && (size_t)n + 1 < sizeof(msg) &&
		    whatsnew_render(from, BLINK_FW_VERSION, msg + n + 1,
				    sizeof(msg) - n - 1) > 0) {
			msg[n] = '\n';
		}
		ui_settings_notice(msg);
	} else {
		ui_settings_notice("Update failed, previous version restored.");
	}
}

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/* Everything from here to usb_anim_pump() is the board's own network path:
 * the captive portal, the sign-in, the scan. Whole functions, so this is a
 * region rather than a sprinkle -- see firmware/Kconfig. */
static void pump_ui(void)
{
	ui_setup_service();
	ota_boot_pump();
	lv_timer_handler();
}

/* Called from portal_run's idle wait: keep the setup screen alive. */
void portal_idle_hook(void)
{
	pump_ui();
}

/* Same job during net_wifi's blocking waits (scan/join/DHCP/AP-up): keep
 * whatever screen is up animating. */
static void wifi_idle(void)
{
	/* Feed the boot watchdog here too. This hook is what net_wifi pumps
	 * through scan, association and DHCP -- up to ~66 s on a failed join,
	 * against a 30 s window -- and it fed nothing, so the watchdog reset a
	 * perfectly healthy board and MCUboot reverted a good image: the exact
	 * opposite of its job. Fed from the loops themselves rather than a
	 * k_timer on purpose: a timer would keep feeding from ISR context even
	 * with the main thread wedged, which is the hang this is here to catch.
	 */
	ota_boot_pump();
	lv_timer_handler();
}

/*
 * Radio settle before the first join of a boot. The pre-redesign flow never
 * joined before ~12 s of uptime (8 s sniff + selection screen) and connected
 * every time; the detection flow's ~3 s join failed with a mid-handshake
 * disconnect on every boot tried (2026-07-14). The driver evidently needs
 * runway after init. Keeps the UI pumping so the spinner stays alive.
 */
static void wifi_settle(void)
{
	for (int i = 0; i < 500; i++) {	/* 5 s */
		ota_boot_pump();	/* 5 s of the 30 s window; see wifi_idle */
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

/*
 * The one background thread. Used serially, never concurrently: the sign-in
 * exchange during provisioning, or standalone mode's network loop -- a boot
 * does one or the other. Lower priority than main (higher number): 1-2 s of
 * ECDHE math must not starve the render loop on this single-core build.
 */
#define NET_WORKER_PRIO 5

/* Re-join backoff, doubling up to five minutes because a router that is
 * genuinely down stays down and retrying the radio in a tight loop is what
 * starved the WiFi heap before.
 *
 * MIN is the value the backoff RESETS to after a success, not the first wait:
 * the failure path doubles before it waits, so the observed sequence is
 * 30 -> 60 -> 120 -> 240 -> 300 s and "retry in 15 s" is never printed. Worth
 * knowing when reading a capture -- 30 s first is correct, not a malfunction.
 *
 * Also note a failed net_wifi_connect(.., 30) can itself block this thread for
 * ~36-66 s (up to 6 s in its request-retry loop, then the 30 s timeout applied
 * to association AND again to DHCP). An outage shorter than that can be ridden
 * out entirely inside one attempt, so a brief router reboot may never reach
 * this backoff at all. */
#define REJOIN_WAIT_MIN_MS (15 * 1000)
#define REJOIN_WAIT_MAX_MS (5 * 60 * 1000)

/* Same shape for a refresh that failed on transport rather than credentials.
 * Used by the not-yet-signed-in state at the top of the worker loop and by the
 * proactive pre-expiry refresh below it.
 *
 * 10 s first, not 15, so a brief outage recovers a little sooner. Do not read
 * the ladder as a schedule: a failing attempt can itself burn 45-60 s
 * (net_time_sync walks three servers at 10 s each, then the token POST carries
 * a 15 s timeout plus DNS and a handshake), so the real cadence is whichever of
 * the two is longer. On an OTA test boot that means roughly two attempts inside
 * the 90 s confirm window, not four. The cap is unreachable there by design --
 * an image that cannot reach Anthropic in 90 s has not proven itself and SHOULD
 * revert. */
#define REFRESH_RETRY_MIN_MS (10 * 1000)
#define REFRESH_RETRY_MAX_MS (5 * 60 * 1000)
/* A 401 that survives a SUCCESSFUL refresh is not a stale access token -- a
 * scope mismatch, a 401 from an intermediary, an entitlement pulled. Backing
 * the refresh off would be wrong (it worked), so the POLL backs off instead;
 * otherwise that state re-POSTs a token every 5 s indefinitely. */
#define UNAUTH_WAIT_MIN_MS (5 * 1000)
#define UNAUTH_WAIT_MAX_MS (10 * 60 * 1000)

static K_THREAD_STACK_DEFINE(net_stack, 8192);	/* TLS-sized, like main's */
static struct k_thread net_thread;

/* ---- provisioning callbacks (portal owns HTTP, we own WiFi + OAuth) ---- */

/* Runs ON THE WORKER THREAD -- no LVGL calls. ui_setup_set_state() is safe
 * from here by design (volatile pending + apply on the LVGL thread), the
 * same contract the net-mgmt callbacks use. */
static int cb_sign_in(const char *code)
{
	struct oauth_tokens tok;

	ui_setup_set_state(UI_SETUP_SIGNIN, NULL);

	/* TLS certificate checks need a real wall clock; sync it now, the first
	 * time we actually talk to Anthropic. */
	if (!net_time_valid()) {
		net_time_sync(10);
	}

	int rc = oauth_exchange_code(code, verifier, &tok);

	if (rc == 0) {
		cfg_set_token(tok.refresh);	/* write-before-use */
		ui_setup_set_state(UI_SETUP_DONE, NULL);
	} else {
		ui_setup_set_state(UI_SETUP_ERROR, NULL);
	}
	return rc;
}

/*
 * Async shell around cb_sign_in. The portal's server is single-threaded;
 * with the 15-25 s exchange run inline it went deaf mid-sign-in, /status
 * polls piled into the backlog, and the phone never learned the outcome
 * (user-reported 2026-07-16, twice). The exchange runs on the worker
 * thread instead, and the portal keeps answering polls throughout.
 */
static volatile int signin_result = 1;	/* <0 running, 0 ok, >0 fail */
static char signin_code[256];

static void signin_worker(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
	signin_result = (cb_sign_in(signin_code) == 0) ? 0 : 1;
}

static void cb_sign_in_start(const char *code)
{
	strncpy(signin_code, code, sizeof(signin_code) - 1);
	signin_code[sizeof(signin_code) - 1] = '\0';
	signin_result = -1;
	k_thread_create(&net_thread, net_stack, K_THREAD_STACK_SIZEOF(net_stack),
			signin_worker, NULL, NULL, NULL,
			NET_WORKER_PRIO, 0, K_NO_WAIT);
}

static int cb_sign_in_poll(void)
{
	return signin_result;
}

/*
 * Phase 1: run the AP until credentials arrive, then tear it down and try to
 * join. On failure, restart the AP with the reason on the form and let the
 * user rejoin the setup network. Returns 0 with creds stored, or negative on
 * portal timeout / AP failure (caller reboots either way).
 */
static int phase1_get_wifi(char *ssid, size_t slen, char *psk, size_t plen,
			   const char *err0)
{
	const char *err = err0;
	bool first_round = true;

	for (;;) {
		/* Scan before the AP comes up (scanning under SoftAP misses
		 * most APs). Re-scan each round: the failure may have been
		 * "network not found". */
		static char nets[12][33];
		int nn = 0;

		for (int attempt = 0; attempt < 3 && nn <= 0; attempt++) {
			if (attempt > 0) {
				for (int t = 0; t < 100; t++) {	/* 1 s, screen alive */
					pump_ui();
					k_sleep(K_MSEC(10));
				}
			}
			nn = net_wifi_scan(nets, 12, 8);
		}

		if (blind_magic != BLIND_MAGIC) {
			blind_magic = BLIND_MAGIC;
			blind_boots = 0;
		}
		if (nn > 0) {
			blind_boots = 0;
		} else if (first_round) {
			/* Blind boot. Reboot to re-roll the radio rather than
			 * offer an empty network list. Bounded: after BLIND_MAX
			 * consecutive blind boots, give up and serve the portal
			 * anyway (its own 15-min timeout starts a new streak),
			 * so a location with no networks can't reboot-loop. */
			if (blind_boots < BLIND_MAX) {
				blind_boots++;
				printk("[wifi] radio blind (0 scans); reboot %u/%u to re-roll\n",
				       blind_boots, BLIND_MAX);
				k_msleep(300);
				ui_boot_mark_intentional_reboot();
				sys_reboot(SYS_REBOOT_COLD);
			}
			blind_boots = 0;
		}
		first_round = false;

		portal_set_networks(nets, nn > 0 ? nn : 0);

		int rc = net_wifi_start_ap();

		if (rc != 0) {
			return rc;
		}
		dns_hijack_start();
		ui_setup_set_state(UI_SETUP_WAIT, err);	/* err on-device too */
		pump_ui();

		rc = portal_run_wifi(ssid, slen, psk, plen, err, 900);

		dns_hijack_stop();
		net_wifi_stop_ap();
		if (rc != 0) {
			return rc;	/* nobody set us up in time */
		}

		/*
		 * Do NOT join here. On this driver a station brought up right
		 * after AP_DISABLE accepts the connect request but never
		 * completes it -- every post-AP join timed out on hardware
		 * (3/3), while the same credentials joined instantly from a
		 * fresh boot (resume path). So persist and reboot; the boot
		 * path does the join. A wrong password self-corrects: the
		 * resume join fails and lands back here with the reason on
		 * the form.
		 */
		/* The phone's timezone rides along with the credentials: the
		 * board's own lookup is best-effort at most (captive-ish
		 * networks eat it), and you re-provision exactly when you
		 * change timezones. Boot wires cfg -> net_time. */
		int32_t tzm;

		if (portal_last_tzmin(&tzm)) {
			cfg_set_tz(tzm);
		}
		cfg_set_wifi(ssid, psk);
		/* No "restarting" interstitial: CONNECTING goes up now, the
		 * skip splash wears the same dark background, and the resume
		 * boot re-applies CONNECTING as its very first frame -- one
		 * continuous screen across the reset (user request
		 * 2026-07-16; the old 2.5 s notice earned its keep back when
		 * the reboot was visible). */
		ui_setup_set_state(UI_SETUP_CONNECTING, ssid);
		for (int i = 0; i < 50; i++) {	/* 0.5 s, state rendered */
			pump_ui();
			k_msleep(10);
		}
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
}

/* skip_join_reason: non-NULL when the caller already knows a join is doomed
 * (boot scan saw other networks but not ours) -- skip the 30 s attempt and
 * put that reason on the portal form instead. */
static void run_provisioning(const char *skip_join_reason)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	bool resume = skip_join_reason == NULL &&
		      cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));

	ui_setup_show();
	if (resume) {
		/* The first rendered frame must already be CONNECTING:
		 * letting the default scan-QR state flash for one frame
		 * betrayed the reboot (user request 2026-07-16 -- the
		 * restart should be invisible). */
		ui_setup_set_state(UI_SETUP_CONNECTING, ssid);
	}
	pump_ui();
	ui_boot_teardown();	/* boot screen freed once setup screen is live */

	oauth_gen_verifier(verifier, sizeof(verifier));
	oauth_authorize_url(verifier, authorize_url, sizeof(authorize_url));

	/* With a stored token the ack page must not promise a sign-in step --
	 * after the join, boot goes straight to the gauges. */
	char tok[CFG_TOKEN_MAX];

	portal_set_resume(cfg_get_token(tok, sizeof(tok)));

	bool joined = false;
	bool signed_in = false;
	const char *join_err = skip_join_reason;

	/* The ONLY join path: from a clean boot, before any AP mode has run
	 * this boot (a post-AP join never completes on this driver). Phase 1
	 * stores credentials and reboots into this. The CONNECTING state has
	 * been showing since the first frame above. */
	if (resume) {
		wifi_settle();
		joined = (net_wifi_connect(ssid, psk, 30) == 0);
		if (!joined) {
			join_err = net_wifi_last_error();
			/* Drop the failed pair: leaving it in NVS makes every
			 * later boot re-run this doomed join before the portal
			 * (user request 2026-07-16). The next portal round
			 * stores fresh credentials anyway. Established devices
			 * (token present) never take this branch, so an AP
			 * outage can't wipe a working setup. */
			cfg_clear_wifi();
		}
	}

	if (!joined) {
		/* Collects credentials over the AP, then reboots (or returns
		 * on timeout/AP failure -- reboot then too, via out). */
		phase1_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk), join_err);
		goto out;
	}

	/* Phase 2: sign-in over the home LAN. */
	char ip[16], url[48];

	if (!net_wifi_sta_ip(ip, sizeof(ip))) {
		goto out;
	}
	/* The query is a cache-buster: the device keeps its LAN IP across
	 * provisionings, and a phone that cached a previous incarnation's
	 * page would re-render that instead of loading this one (seen
	 * 2026-07-17). A fresh URL per boot can never be in its cache. */
	snprintf(url, sizeof(url), "http://%s/?b=%u", ip, (unsigned)k_uptime_get());
	printk("[usage] sign-in page at %s\n", url);
	ui_setup_set_state(UI_SETUP_WIFI_OK, url);
	pump_ui();

	signed_in = (portal_run_signin(authorize_url, cb_sign_in_start,
				       cb_sign_in_poll, 900) == 0);
out:
	/* Reboot either way: on success come up standalone; on timeout, retry
	 * setup from a clean slate. A cold restart also reclaims all the AP/TLS
	 * memory before the standalone stack allocates it. The toast makes the
	 * restart announced instead of crash-shaped (user request 2026-07-15). */
	ui_setup_set_state(UI_SETUP_RESTART, signed_in ? "Setup complete" : NULL);
	for (int i = 0; i < 300; i++) {	/* 3 s, screen kept alive */
		pump_ui();
		k_sleep(K_MSEC(10));
	}
	ui_boot_mark_intentional_reboot();
	sys_reboot(SYS_REBOOT_COLD);
}

/*
 * Is the stored network on the air? First live run of this gate was right
 * and got misread: it reported the SSID absent minutes after the device
 * moved homes (2026-07-14), and the "scan must be broken" fix that replaced
 * it made a move cost three failed-join boot cycles before the portal.
 * Scan first: an answer in ~8 s. Same blind-radio discipline as the
 * provisioning scan -- an empty result is bimodal radio luck, not evidence
 * of absence, so re-roll by rebooting (bounded); only a scan that SEES
 * networks, just not ours, gets to say "absent". Known limit: a hidden SSID
 * would always read absent and live at the portal.
 */
enum ssid_scan { SSID_VISIBLE, SSID_ABSENT, SSID_RADIO_BLIND };

static enum ssid_scan boot_ssid_scan(const char *ssid)
{
	static char nets[12][33];
	int nn = 0;

	for (int attempt = 0; attempt < 3 && nn <= 0; attempt++) {
		if (attempt > 0) {
			for (int t = 0; t < 100; t++) {	/* 1 s, splash alive */
				wifi_idle();
				k_sleep(K_MSEC(10));
			}
		}
		nn = net_wifi_scan(nets, 12, 8);
	}

	if (blind_magic != BLIND_MAGIC) {
		blind_magic = BLIND_MAGIC;
		blind_boots = 0;
	}
	if (nn <= 0) {
		if (blind_boots < BLIND_MAX) {
			blind_boots++;
			printk("[wifi] radio blind (0 scans); reboot %u/%u to re-roll\n",
			       blind_boots, BLIND_MAX);
			k_msleep(300);
			ui_boot_mark_intentional_reboot();
			sys_reboot(SYS_REBOOT_COLD);
		}
		/* Persistently blind: stop guessing. The portal (typed-SSID
		 * field) is the honest next step, and its timeout-reboot
		 * starts a fresh streak. */
		blind_boots = 0;
		return SSID_RADIO_BLIND;
	}
	blind_boots = 0;

	for (int i = 0; i < nn; i++) {
		if (strcmp(nets[i], ssid) == 0) {
			return SSID_VISIBLE;
		}
	}
	return SSID_ABSENT;
}

/* ---- standalone WiFi mode: fetch usage over TLS, feed the gauges ---- */

/*
 * The net worker owns every blocking network call in standalone mode; the
 * main thread owns LVGL. They meet only at this queue: the worker posts UI
 * work, main applies it. NEV_WAIT strings must be static -- the pointer
 * crosses threads.
 */
struct net_evt {
	enum { NEV_STAGE, NEV_USAGE, NEV_STATUS, NEV_MODELS } kind;
	int stage;				/* NEV_STAGE */
	double s_pct, w_pct;			/* NEV_USAGE; NEV_MODELS reuses
						 * s_pct as fable's weekly */
	int32_t s_reset, w_reset;
	enum usage_status status;		/* NEV_STATUS */
};

K_MSGQ_DEFINE(net_evtq, sizeof(struct net_evt), 8, 4);

/* The boot bar's step lists -- same screen in both modes, different words
 * (user request 2026-07-16). */
static const char *const wifi_boot_steps[] = {
	"Join the WiFi",
	"Sign in to Anthropic",
	"Fetch first usage",
};

static void apply_net_evt(const struct net_evt *e)
{
	switch (e->kind) {
	case NEV_STAGE:
		usage_view_boot_stage(e->stage);
		break;
	case NEV_USAGE:
		/* First applied usage = WiFi + TLS + parse all proven: the
		 * bar a test boot must clear before it self-confirms. */
		ota_health = true;
		usage_view_update(e->s_pct, e->s_reset, e->w_pct, e->w_reset);
		break;
	case NEV_STATUS:
		usage_view_set_status(e->status);
		break;
	case NEV_MODELS:
		usage_view_set_models(e->s_pct);
		break;
	}
}

/* Between boot-clip frames (ui_anim_run) the mode's background duties keep
 * running through these: standalone drains the worker's queue, USB keeps the
 * serial protocol alive, and both feed the OTA test-boot state.
 *
 * That last one is not optional. ota_boot_pump is the only watchdog feeder and
 * the only caller of boot_write_img_confirmed, and the clip player's loop is
 * unbounded -- so on a test boot, watching the eyes for 30 s used to trip the
 * watchdog and revert a healthy image. Standalone made it worse: draining the
 * queue below sets ota_health, so the board proved itself and reverted anyway.
 */
static void standalone_anim_pump(void)
{
	struct net_evt e;

	while (k_msgq_get(&net_evtq, &e, K_NO_WAIT) == 0) {
		apply_net_evt(&e);
	}
	ota_boot_pump();
}

#endif /* CONFIG_BLINK_WIFI_MODE */

/* Outside the gate: this is the tethered path's own step list, and it was
 * only sitting next to wifi_boot_steps out of habit. */
static const char *const usb_boot_steps[] = {
	"Link the PC daemon",
	"Fetch first usage",
};

static void usb_anim_pump(void)
{
	proto_service();
	if (usage_view_have_data()) {
		ota_health = true;	/* daemon delivered usage */
	}
	ota_boot_pump();
}

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/* Lower priority than main (higher number): 1-2 s of ECDHE math must not
 * starve the render loop on this single-core build. */
static char worker_refresh[CFG_TOKEN_MAX];
/* Credentials the worker re-joins with. The initial join happens on the UI
 * thread in run_standalone(); these copies let the worker recover the link on
 * its own without reaching back into cfg_store from another thread. */
static char worker_ssid[CFG_SSID_MAX];
static char worker_psk[CFG_PSK_MAX];

static void post_stage(int stage)
{
	struct net_evt e = { .kind = NEV_STAGE, .stage = stage };

	k_msgq_put(&net_evtq, &e, K_NO_WAIT);
}

static void post_status(enum usage_status st)
{
	struct net_evt e = { .kind = NEV_STATUS, .status = st };

	k_msgq_put(&net_evtq, &e, K_NO_WAIT);
}

/*
 * One owner for the refresh backoff ladder. It was open-coded at three sites
 * that each got a different subset right -- one reset it on success and one
 * did not, one stamped the next attempt and one did not, and the mid-run site
 * sat outside it entirely.
 */
static int refresh_backoff_arm(int64_t *next, int *wait_ms)
{
	int used = *wait_ms;

	*next = k_uptime_get() + used;
	*wait_ms = MIN(used * 2, REFRESH_RETRY_MAX_MS);
	return used;	/* the wait actually scheduled, for the log line */
}

static void refresh_backoff_reset(int64_t *next, int *wait_ms)
{
	*next = 0;
	*wait_ms = REFRESH_RETRY_MIN_MS;
}

/*
 * A rejected credential is only fatal once it REPEATS. oauth.c maps both 400
 * and 401 to -EACCES, so one bad response -- an intermediary, a brief
 * server-side fault, a clock skew that resolves itself -- used to wipe the
 * token and reboot into provisioning, from any of the three call sites. This
 * is the consecutive-failure count the mid-run comment used to ask for; it
 * lives here because every refresh funnels through the function below.
 */
#define REFRESH_REJECT_LIMIT 3
/*
 * ...and only after the rejections have persisted this long.
 *
 * The count alone is not a measure of anything: the retry ladder starts at
 * REFRESH_RETRY_MIN_MS and doubles, so three rejections land at t=0, t=10 s and
 * t=30 s. A token-endpoint fault lasting under a minute -- or a hotel/office
 * portal answering 4xx to everything, which oauth.c cannot distinguish from a
 * genuine invalid_grant because it maps both 400 and 401 to -EACCES -- would
 * wipe a perfectly good credential and force a full re-login. Requiring the
 * streak to SPAN half an hour makes the test "this has been broken for a
 * while", which is what the decision actually rests on.
 */
#define REFRESH_REJECT_MIN_SPAN_MS (30 * 60 * 1000)

static int refresh_rejects;
static int64_t refresh_reject_since;	/* uptime of the streak's first rejection */

/*
 * When the last token POST went out, successful or not.
 *
 * The mid-run 401 path needs to know "did we just refresh?", and it used to
 * ask next_refresh -- which is a FAILURE stamp, cleared to 0 by
 * refresh_backoff_reset(). After a SUCCESSFUL refresh that test reads
 * `now < 0`, i.e. false, so the guard it was written to provide could not fire
 * exactly when it was needed. This is the question it was actually asking.
 */
static int64_t last_refresh_ms;
#define REFRESH_MIN_GAP_MS (60 * 1000)

/*
 * How many times a mid-run 401 may be answered with a refresh that SUCCEEDS.
 *
 * If the credential refreshes cleanly and the very next fetch still 401s, the
 * credential was never the problem -- a scope mismatch, an intermediary, or one
 * of the misclassified cases below. Refreshing again cannot help, but the code
 * did it on every retry forever: a full DNS + TLS + ECDHE POST and an NVS token
 * rewrite, ~144 a day at the 10-minute ceiling, each rotating the refresh
 * token. After this many, back off the poll and leave the credential alone.
 */
#define UNAUTH_REFRESH_TRIES 2
static int unauth_refresh_done;

/*
 * The worker's only way to refresh. Returns 0 with *tok holding a usable
 * credential and *deadline reset; returns a negative errno on failure, leaving
 * *tok untouched so the caller still has something valid to retry with. It
 * does not return at all once a REJECTED credential has been rejected
 * REFRESH_REJECT_LIMIT times running AND has stayed that way for
 * REFRESH_REJECT_MIN_SPAN_MS: at that point a retry cannot fix it, so it drops
 * the token and reboots into provisioning. Both conditions matter -- see the
 * span constant for why the count on its own measures nothing.
 *
 * The reply lands in a separate `fresh` rather than in *tok, so a caller
 * passing tok->refresh as `sent` is not reading a buffer this function is
 * writing. (The !authed site passes the stored bootstrap copy instead.)
 */
static int worker_refresh_token(const char *sent, struct oauth_tokens *tok,
				int64_t *deadline)
{
	struct oauth_tokens fresh;
	int rc = oauth_refresh(sent, &fresh);

	last_refresh_ms = k_uptime_get();	/* attempted, whatever the outcome */

	if (oauth_creds_rejected(rc)) {
		if (refresh_rejects == 0) {
			refresh_reject_since = k_uptime_get();
		}
		if (++refresh_rejects < REFRESH_REJECT_LIMIT ||
		    k_uptime_get() - refresh_reject_since < REFRESH_REJECT_MIN_SPAN_MS) {
			/* Hand it back as an ordinary failure: the caller backs
			 * off and keeps the credential, and a one-off 400/401
			 * costs a retry instead of the whole provisioning. */
			printk("[oauth] refresh rejected (%d/%d, %lld s into the streak) -- token kept, backing off\n",
			       refresh_rejects, REFRESH_REJECT_LIMIT,
			       (k_uptime_get() - refresh_reject_since) / 1000);
			return rc;
		}
		/* The "log in once" chain really is broken. Drop the token and
		 * reboot; with none stored the board re-provisions, keeping the
		 * WiFi credentials. */
		printk("[oauth] refresh rejected %d times -- dropping the token, re-provisioning\n",
		       refresh_rejects);
		cfg_clear_token();
		post_status(USAGE_STATUS_ERROR);
		k_sleep(K_SECONDS(3));
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
	if (rc != 0) {
		/* Transport, not rejection -- so the streak is broken. Both
		 * comments here call this a CONSECUTIVE count; without this it
		 * was a since-the-last-success count, and three isolated 400s
		 * spread across a day reached the limit just as surely as a
		 * genuinely dead credential. */
		refresh_rejects = 0;
		return rc;
	}
	if (fresh.refresh[0] == '\0') {
		/* oauth_refresh guarantees this cannot happen. Belt and braces:
		 * persisting it would look exactly like having no token at all,
		 * and the next boot would re-provision. */
		printk("[oauth] refresh returned an empty token -- keeping the stored one\n");
		return -EINVAL;
	}
	refresh_rejects = 0;
	*tok = fresh;
	cfg_set_token(tok->refresh);	/* persist a rotated token before use */
	*deadline = k_uptime_get() + (int64_t)tok->expires_in * 1000;
	return 0;
}

static void net_worker(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

	struct oauth_tokens tok;
	int64_t token_deadline = 0;
	int64_t next_poll = 0;
	int64_t next_tz = 0;	/* fetch as soon as we are online */
	/*
	 * Fallback only. The first check normally rides the end of the boot
	 * sequence (see the ota_boot_checked block below) -- this timer just
	 * catches a board that never reaches that point.
	 */
	int64_t next_ota = k_uptime_get() + 5 * 60 * 1000;
	bool ota_boot_checked = false;
	int32_t tz_min = 0;
	int64_t next_rejoin = 0;
	int rejoin_wait_ms = REJOIN_WAIT_MIN_MS;
	int64_t next_refresh = 0;
	int refresh_wait_ms = REFRESH_RETRY_MIN_MS;
	int unauth_wait_ms = UNAUTH_WAIT_MIN_MS;
	bool authed = false;
	/* Whether the gauges have ever shown real numbers. usage_view restores
	 * the CONNECTING overlay only for DISCONNECTED, so posting ERROR before
	 * any data has landed hides the boot screen behind bare "--%" gauges
	 * captioned "error - showing last known" when there is no last known. */
	bool had_usage = false;

	/* Advance the boot bar before each blocking step: visible progress on
	 * one screen instead of a title per stage (user request 2026-07-15).
	 * The clock sync rides inside the sign-in stage -- TLS needs the clock,
	 * and it is too quick to deserve a segment. */
	post_stage(1);

	while (1) {
		int64_t now = k_uptime_get();

		/*
		 * Keep the link alive. The station drops on its own after long
		 * uptimes -- AP idle-timeout, a roam, a radio glitch -- and
		 * NOTHING here ever re-joined: net_wifi's disconnect handler
		 * only clears have_ip, and the initial join happens once, on
		 * the UI thread, before this loop starts. The board therefore
		 * stayed offline until it was power-cycled, and a drop during a
		 * download killed an OTA install outright (user-reported
		 * 2026-07-26).
		 */
		if (!net_wifi_has_ip()) {
			post_status(USAGE_STATUS_DISCONNECTED);

			if (now >= next_rejoin) {
				printk("[wifi] link down -- rejoining\n");

				/* net_wifi_connect() pumps the idle hook while
				 * it waits, and that hook drives LVGL. We are
				 * NOT the LVGL thread, so calling it from here
				 * would touch LVGL from two threads -- the same
				 * class of corruption as the SoftAP crash. Drop
				 * the hook for the duration; main keeps
				 * rendering on its own regardless. */
				net_wifi_set_idle_hook(NULL);
				int rc = net_wifi_connect(worker_ssid,
							  worker_psk, 30);
				net_wifi_set_idle_hook(wifi_idle);

				if (rc == 0) {
					printk("[wifi] rejoined\n");
					rejoin_wait_ms = REJOIN_WAIT_MIN_MS;
					next_poll = 0;	/* refresh at once */
					next_tz = 0;
					/* Re-opens the refresh gate and clears
					 * the ladder so the first attempt after
					 * a rejoin is not stuck behind a stale
					 * backoff. It does NOT re-run sign-in:
					 * `authed` is never cleared. Do not
					 * "fix" that by clearing it here -- the
					 * !authed path re-sends worker_refresh,
					 * the bootstrap token, which the
					 * endpoint has long since rotated. That
					 * is a 400 invalid_grant on every WiFi
					 * flap, and now a wipe once it repeats. */
					refresh_backoff_reset(&next_refresh,
							      &refresh_wait_ms);
				} else {
					/* Back off: a router that is down stays
					 * down for minutes, and hammering the
					 * radio every few seconds is how the
					 * heap got starved before. */
					rejoin_wait_ms = MIN(rejoin_wait_ms * 2,
							     REJOIN_WAIT_MAX_MS);
					next_rejoin = k_uptime_get() +
						      rejoin_wait_ms;
					printk("[wifi] rejoin failed, retry in %d s\n",
					       rejoin_wait_ms / 1000);
				}
			}
			k_sleep(K_MSEC(500));
			continue;	/* nothing else works while offline */
		}

		/*
		 * Not signed in yet. This used to run ABOVE the loop, which put
		 * it above the only rejoin path there is: a link drop during
		 * the first refresh -- the likeliest moment, right after a join
		 * -- wedged this thread in a backoff sleep forever, with no
		 * rejoin, no clock sync, and the boot bar frozen on "Sign in to
		 * Anthropic" until someone power-cycled the board. As a loop
		 * state it gets the rejoin above it for free.
		 */
		if (!authed) {
			if (now < next_refresh) {
				k_sleep(K_MSEC(250));
				continue;
			}

			/* TLS certificate checks need a real wall clock. Inside
			 * the loop so it re-runs after a rejoin, not once before
			 * the network was ever up. */
			if (!net_time_valid()) {
				net_time_sync(10);
			}

			int rc = worker_refresh_token(worker_refresh, &tok,
						      &token_deadline);

			if (rc == 0) {
				authed = true;
				refresh_wait_ms = REFRESH_RETRY_MIN_MS;
				next_refresh = 0;
				next_poll = 0;
				post_stage(2);
				continue;
			}

			/* DISCONNECTED, not ERROR: with no data yet, usage_view's
			 * overlay IS the boot screen, and it restores that screen
			 * only for DISCONNECTED. Posting ERROR here hid the
			 * CONNECTING bar behind bare "--%" gauges captioned
			 * "error - showing last known" -- when there was no last
			 * known -- and nothing ever brought it back. */
			int wait = refresh_backoff_arm(&next_refresh,
						       &refresh_wait_ms);

			post_status(USAGE_STATUS_DISCONNECTED);
			printk("[oauth] refresh failed (%d) -- %s, token kept, retry in %d s\n",
			       rc,
			       oauth_creds_rejected(rc) ? "rejected" : "transport",
			       wait / 1000);
			continue;
		}

		/* Refresh proactively, 5 min before expiry. Backed off on
		 * failure: without a next-attempt stamp this condition stays
		 * true once the window opens, and a failing refresh re-fired a
		 * full DNS + TLS + ECDHE round on every 250 ms tick of this
		 * loop -- on the single core LVGL shares, against the heap whose
		 * starvation once crashed the board in z_swap. */
		if (now > token_deadline - 5 * 60 * 1000 && now >= next_refresh) {
			if (worker_refresh_token(tok.refresh, &tok,
						 &token_deadline) == 0) {
				refresh_backoff_reset(&next_refresh,
						      &refresh_wait_ms);
			} else {
				int wait = refresh_backoff_arm(&next_refresh,
							       &refresh_wait_ms);

				printk("[oauth] proactive refresh failed -- retry in %d s\n",
				       wait / 1000);
			}
		}

		if (now >= next_poll) {
			struct usage_data d;
			int status;
			enum usage_result r = usage_client_fetch(tok.access, &d, &status);

			if (r == USAGE_OK) {
				struct net_evt e = {
					.kind = NEV_USAGE,
					.s_pct = d.five_hour.utilization,
					.s_reset = net_time_secs_until(d.five_hour.resets_at),
					.w_pct = d.seven_day.utilization,
					.w_reset = net_time_secs_until(d.seven_day.resets_at),
				};

				k_msgq_put(&net_evtq, &e, K_NO_WAIT);

				struct net_evt m = {
					.kind = NEV_MODELS,
					.s_pct = d.seven_day_fable.present
						 ? d.seven_day_fable.utilization : -1,
				};

				k_msgq_put(&net_evtq, &m, K_NO_WAIT);
				had_usage = true;
				unauth_wait_ms = UNAUTH_WAIT_MIN_MS;
				unauth_refresh_done = 0;	/* 401s are over */
				/*
				 * Stamped from a FRESH reading, not the `now`
				 * at the top of the loop. Everything between
				 * the two blocks: a rejoin, a sign-in, the
				 * proactive refresh, then the fetch itself --
				 * 45-60 s of it on a bad link. Measuring the
				 * interval from before all that leaves the
				 * deadline already past, so the next poll
				 * fires at once and the "one a minute" the
				 * endpoint is sized for becomes a much faster
				 * knock. Same reason the PC bridge holds off
				 * on a 429.
				 */
				next_poll = k_uptime_get() + 60 * 1000;
			} else if (r == USAGE_RATE_LIMITED) {
				post_status(USAGE_STATUS_STALE);
				next_poll = k_uptime_get() + 600 * 1000;
			} else if (r == USAGE_UNAUTHORIZED) {
				/* Token died mid-run. A refresh rejected with
				 * 400/401 never comes back from here --
				 * worker_refresh_token reboots into provisioning.
				 *
				 * Two other kinds of dead credential do come back,
				 * misclassified: oauth_creds_rejected() keys on
				 * -EACCES alone, so a 403 (entitlement pulled, org
				 * removed) arrives as -EIO and a 200 carrying
				 * invalid_grant arrives as -EINVAL. Both back off
				 * as if the network were at fault and retry for
				 * good. So does the case where the refresh
				 * SUCCEEDS while the fetch keeps 401ing -- a scope
				 * mismatch, or a 401 from an intermediary.
				 *
				 * The consecutive-failure count now lives in
				 * worker_refresh_token, so a repeated rejection
				 * does re-provision; the misclassified 403 and
				 * invalid_grant cases still retry, but under the
				 * ladder below rather than forever at 5 s. */
				if (now < next_refresh ||
				    now - last_refresh_ms < REFRESH_MIN_GAP_MS ||
				    unauth_refresh_done >= UNAUTH_REFRESH_TRIES) {
					/*
					 * Don't POST. Three ways that is the
					 * right answer: the ladder has already
					 * scheduled an attempt; we refreshed
					 * moments ago (the proactive refresh
					 * runs earlier in this same iteration,
					 * and testing next_refresh could not
					 * catch that -- see last_refresh_ms);
					 * or refreshing has already been tried
					 * and demonstrably does not fix this
					 * 401, so repeating it is just token
					 * churn.
					 */
					post_status(had_usage ? USAGE_STATUS_ERROR
							      : USAGE_STATUS_DISCONNECTED);
					next_poll = k_uptime_get() + unauth_wait_ms;
					unauth_wait_ms = MIN(unauth_wait_ms * 2,
							     UNAUTH_WAIT_MAX_MS);
				} else if (worker_refresh_token(tok.refresh, &tok,
								&token_deadline) == 0) {
					/* The refresh worked, so this 401 is not
					 * a stale access token. Leave the refresh
					 * ladder clear and back off the poll. */
					unauth_refresh_done++;
					refresh_backoff_reset(&next_refresh,
							      &refresh_wait_ms);
					next_poll = k_uptime_get() + unauth_wait_ms;
					unauth_wait_ms = MIN(unauth_wait_ms * 2,
							     UNAUTH_WAIT_MAX_MS);
				} else {
					int wait = refresh_backoff_arm(&next_refresh,
								       &refresh_wait_ms);

					post_status(had_usage ? USAGE_STATUS_ERROR
							      : USAGE_STATUS_DISCONNECTED);
					next_poll = k_uptime_get() + wait;
				}
			} else {
				post_status(had_usage ? USAGE_STATUS_ERROR
						      : USAGE_STATUS_DISCONNECTED);
				next_poll = k_uptime_get() + 60 * 1000;
			}
		}

		if (now >= next_tz) {
			int32_t om;

			if (tz_fetch_offset(&om) == 0) {
				net_time_set_offset(om);
				if (!cfg_get_tz(&tz_min) || tz_min != om) {
					cfg_set_tz(om);	/* new last-known */
				}
				next_tz = now + 86400LL * 1000;	/* daily: tracks DST */
			} else {
				next_tz = now + 3600LL * 1000;	/* retry hourly */
			}
		}

		/*
		 * First check as soon as the board is genuinely up, rather than
		 * five minutes later (user request 2026-08-20: tell me at boot,
		 * with the choice to update or carry on).
		 *
		 * had_usage is the right gate, not "WiFi associated": it means
		 * the join, the clock, the token and a real fetch have ALL
		 * worked -- which is exactly the three boot stages completing.
		 * Checking earlier would race the clock, and TLS without a
		 * valid clock fails.
		 */
		if (had_usage && !ota_boot_checked) {
			ota_boot_checked = true;
			next_ota = now;
		}

		/* --- OTA: daily background check + UI-requested actions --- */
		bool manual = ota_take_check_request();

		if (manual || now >= next_ota) {
			struct ota_manifest m;
			bool newer = false;

			if (manual) {
				ota_ui_set(OTA_UI_CHECKING, NULL, 0);
			}
			enum ota_result r = ota_check(&m, &newer);

			if (r == OTA_OK && newer) {
				ota_ui_set(OTA_UI_AVAILABLE, &m, 0);
			} else if (manual) {
				ota_ui_set(r == OTA_OK ? OTA_UI_UP_TO_DATE
						       : OTA_UI_FAILED, NULL, 0);
			}
			next_ota = now + 86400LL * 1000;
		}

		if (ota_take_install_request()) {
			struct ota_manifest m;

			/* The UI snapshot doesn't carry the sha256; install
			 * exactly what the last successful check verified. */
			ota_last_manifest(&m);
			ota_ui_set(OTA_UI_DOWNLOADING, &m, 0);
			if (ota_install(&m) == OTA_OK) {
				char trail[CFG_OTA_VER_MAX];

				whatsnew_trail(BLINK_FW_VERSION, m.version,
					       trail, sizeof(trail));
				cfg_set_ota_state(1, trail);
				ota_ui_set(OTA_UI_REBOOTING, &m, 100);
				k_sleep(K_SECONDS(1));	/* let the UI paint it */
				ui_boot_mark_intentional_reboot();
				sys_reboot(SYS_REBOOT_COLD);
			}
			ota_ui_set(OTA_UI_FAILED, &m, 0);
		}

		k_sleep(K_MSEC(250));	/* scheduling tick, not a UI pump */
	}
}

static void run_standalone(void)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], refresh[CFG_TOKEN_MAX];

	cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	cfg_get_token(refresh, sizeof(refresh));

	usage_view_boot_begin(wifi_boot_steps, 3);
	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	lv_timer_handler();

	/* Settle even after the scan: every hotspot join that succeeded
	 * tonight ran behind this runway, and the one that skipped it
	 * dropped mid-association (2026-07-15). */
	wifi_settle();
	/* The FIRST join after a cold boot (and especially right after a flash)
	 * is RF-warmup-flaky: it would fail once, drop to setup, and only a
	 * manual reset made the identical credentials connect (user-reported
	 * 2026-07-20). One silent retry behind the same CONNECTING screen -- a
	 * settle, then try again -- turns that into an invisible hiccup. Still
	 * no reboot-retry dance; two clean failures go to setup as before. */
	bool joined = false;

	for (int attempt = 0; attempt < 2 && !joined; attempt++) {
		if (attempt > 0) {
			printk("[wifi] first join failed; settling and retrying\n");
			wifi_settle();
		}
		joined = (net_wifi_connect(ssid, psk, 30) == 0);
	}
	if (!joined) {
		/* A failed join goes to the setup screen with the reason on the
		 * form (user decision 2026-07-15 -- silent self-restarts read as
		 * crashes). The token survives, so this costs re-picking a
		 * network, never re-signing-in. */
		const char *err = scan_said_absent ? "network not found"
						   : net_wifi_last_error();

		usage_view_deinit();	/* gauges out before setup in */
		run_provisioning(err);	/* reboots when done */
	}

	/* Clock: last-known offset immediately (survives API outages), the
	 * live tz answer replaces it from the worker below. */
	int32_t tz_min = 0;

	if (cfg_get_tz(&tz_min)) {
		net_time_set_offset(tz_min);
	}

	/* Everything network-bound from here (SNTP, TLS exchanges, the 60 s
	 * poll loop) runs on the worker thread. These blocking library calls
	 * used to run right here on the UI thread: the spinner froze during
	 * every fetch and touch events overflowed their queues (user-reported
	 * 2026-07-14/15). Main now only renders. */
	strncpy(worker_refresh, refresh, sizeof(worker_refresh) - 1);
	worker_refresh[sizeof(worker_refresh) - 1] = '\0';
	strncpy(worker_ssid, ssid, sizeof(worker_ssid) - 1);
	worker_ssid[sizeof(worker_ssid) - 1] = '\0';
	strncpy(worker_psk, psk, sizeof(worker_psk) - 1);
	worker_psk[sizeof(worker_psk) - 1] = '\0';
	k_thread_create(&net_thread, net_stack, K_THREAD_STACK_SIZEOF(net_stack),
			net_worker, NULL, NULL, NULL,
			NET_WORKER_PRIO, 0, K_NO_WAIT);

	int64_t last_tick = k_uptime_get();

	/* Anything the user tapped during the scan and the settle above was
	 * latched with nothing to service it -- up to a minute ago. Acting on
	 * it now would slide the panel in unprompted. See ui_settings.h. */
	ui_settings_drop_pending();

	while (1) {
		struct net_evt e;

		while (k_msgq_get(&net_evtq, &e, K_NO_WAIT) == 0) {
			apply_net_evt(&e);
		}
		ota_boot_pump();

		if (ui_anim_pending()) {
			ui_anim_run(standalone_anim_pump);
		}
		ui_settings_service(standalone_anim_pump);

		int64_t now = k_uptime_get();

		if (now - last_tick >= 1000) {
			usage_view_tick_1s();
			last_tick = now;

			int hh = -1, mm = 0;

			net_time_local(&hh, &mm);
			usage_view_set_clock(hh, mm);
		}
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

#endif /* CONFIG_BLINK_WIFI_MODE */

/* ---- USB bridge mode: PC daemon pushes usage over serial ---- */

/* The original reason to wake: the app said something. proto.c clears
 * host_seen on the timeout that armed the sleep in the first place, so this
 * is false throughout the doze and true on the first line back. */
static bool host_is_back(void)
{
	return proto_host_seen();
}

/*
 * The other reason to wake: something new to show.
 *
 * The mirror of sleep_stale_should_start's non-had_usage terms, and it has
 * to be exactly that -- tests/sleep_gate pins the two as complements over a
 * grid, because a wake condition a second away from the sleep condition
 * would have this board closing and opening its eyes forever on a real desk.
 *
 * It says mirror and now it is one: the OTA term comes from
 * ota_blocks_sleep(), the same call the doze arms on. It used to be spelled
 * out here a second time, and the second spelling was missing ota_test_boot.
 */
static bool reading_moved_again(void)
{
	/* The ACTIVE age, matching the branch that started this doze. Waking
	 * on the reading age instead would break the complementarity the grid
	 * in tests/sleep_gate pins, on exactly the desk this field exists
	 * for: a remembered reading never gets younger, so the board would
	 * doze on one number and refuse to wake on the other. */
	int32_t active_age = usage_freshness_active_age_s(k_uptime_get());

	return sleep_stale_should_wake(active_age, ota_blocks_sleep(),
				       usage_freshness_activity());
}

static void run_usb(void)
{
	int64_t last_tick = k_uptime_get();
	int stage_shown = 1;

	/* When the app was last heard from, for the waiting-for-host doze
	 * below. Starts at the top of the loop rather than at boot: this is
	 * about how long THIS screen has been unanswered. */
	int64_t host_quiet_since = last_tick;
	int64_t checking_since = 0;	/* OTA_UI_CHECKING entered at, ms */

	/* Same as run_standalone: drop anything latched before this loop
	 * existed to service it. See ui_settings.h. */
	ui_settings_drop_pending();

	while (1) {
		proto_service();

		if (usage_view_have_data()) {
			ota_health = true;	/* daemon delivered usage */
		}
		ota_boot_pump();

		/* Two reasons to doze (docs/sleep-mode-design.md). The first
		 * is silence past the host timeout. The second is an app that
		 * never stopped talking and has had nothing new to say for
		 * hours -- which is what a sleeping computer with a running
		 * daemon actually looks like from here, and why the first
		 * rule alone left a panel awake all night (field report
		 * 2026-09-02). Both block until there is a reason to wake.
		 *
		 * And neither of them while the owner is in the settings
		 * panel.
		 *
		 * Both rules below are about an absent person, and an open
		 * panel is a present one -- the same argument sleep_gate.c
		 * makes for refusing to doze over anything on screen that is
		 * asking for somebody. It also closes the loop ui_sleep.c
		 * opens at the other end: a swipe during a peek ends the
		 * doze, this loop opens the panel on its next pass, and
		 * without this the board would fall asleep again over the
		 * open panel sixty seconds later -- with the owner reading
		 * it. The cost is that a panel left open holds the board
		 * awake indefinitely; that is a deliberate act by a person
		 * standing at the desk, and it ends when they close it.
		 */
		if (!ui_settings_busy()) {
			/* Whether an update forbids dozing is one question
			 * with one answer, and it lives at
			 * ota_blocks_sleep() -- including the test-boot term,
			 * whose absence from the copy that used to sit here
			 * cost a board on the bench (2026-09-03). Taken once
			 * so both rules below judge the same instant. */
			bool ota_busy = ota_blocks_sleep();

			if (sleep_should_start(proto_host_lost(),
					       usage_view_have_data(),
					       ota_busy)) {
				ui_sleep_run(host_is_back,
					     "Your computer may be asleep.");
				continue;
			}
			/*
			 * On the ACTIVE age, never the reading's own.
			 *
			 * "Has this number stopped moving?" and "has this
			 * desk gone quiet?" are two questions, and the
			 * daemon answers both because they can differ: it
			 * re-offers the last five-hour reading it saw at its
			 * original time, so the dial can be twelve hours old
			 * five seconds after Claude Code rewrote the file it
			 * came from. Dozing on the first question closed the
			 * board's eyes on an owner who was sitting in front
			 * of it (field review 2026-09-02). ui_sleep.c's
			 * closing stamp keeps asking the first question,
			 * because that one really is about the number.
			 */
			if (sleep_stale_should_start(
				    usage_freshness_active_age_s(
					    k_uptime_get()),
				    usage_view_have_data(), ota_busy,
				    usage_freshness_activity())) {
				ui_sleep_run(reading_moved_again,
					     "No new readings for a while.");
				continue;
			}
		}

		/*
		 * OTA, via the daemon. This mode has no network of its own --
		 * nothing here starts net_worker -- so ota_check()/ota_install()
		 * cannot run and the update row used to do nothing at all when
		 * tethered (user-reported 2026-08-20). The daemon has both an
		 * internet connection and this link, so it does the fetching and
		 * pushes the image down; see the OTA block in proto.c.
		 *
		 * Gated on the daemon TALKING, not on it having delivered
		 * usage. Those are unrelated: the usage endpoint is
		 * aggressively rate-limited (the bridge backs off 120-600 s on
		 * a 429), and gating here on ota_health meant a throttled
		 * session never checked for updates at all -- observed
		 * 2026-08-21. Unlike standalone, this mode needs no clock and
		 * no token of its own: the daemon does the fetching.
		 */
		/* The boot-time check moved into proto.c's welcome handler,
		 * which fires on every host connection rather than the first;
		 * see there. */
		/*
		 * Both flags are taken unconditionally, before either is acted
		 * on. They are consume-on-read, so reading one only when the
		 * other is clear leaves a stale request armed for the next
		 * turn -- and reading them inside the call would leave the
		 * order to the compiler. upd_tap.h has the rule and the bug
		 * that produced it: a customer's tap on "Update now" during a
		 * daemon restart went nowhere at all.
		 */
		{
			bool inst_req = ota_take_install_request();
			bool chk_req = ota_take_check_request();

			switch (upd_tap_act(inst_req, chk_req,
					    proto_ota_staged())) {
			case UPD_ACT_CHECK:
				proto_ota_check();
				break;
			case UPD_ACT_INSTALL:
				proto_ota_install();
				break;
			case UPD_ACT_REFUSED:
				/* Consent with nothing to spend it on. Say so
				 * here rather than let the 30 s watchdog below
				 * describe it as a failed check: the customer
				 * asked to install, and an answer about
				 * checking sends them somewhere else. */
				ota_ui_set(OTA_UI_FAILED, NULL, 0);
				ota_ui_set_error("The app stopped answering");
				break;
			case UPD_ACT_NONE:
				break;
			}
		}
		/*
		 * A check nobody answers must not hang the row. The query goes
		 * out over the cable; if the daemon was restarting at that
		 * moment (an app update does exactly that) it was never read,
		 * and "Checking..." with the row disabled stayed on screen
		 * for good (user-reported 2026-08-29). Thirty seconds is far
		 * past a feed fetch; after it the row says why and a tap
		 * asks again.
		 */
		{
			struct ota_ui snap;

			ota_ui_get(&snap);
			if (snap.st == OTA_UI_CHECKING) {
				if (checking_since == 0) {
					checking_since = k_uptime_get();
				} else if (k_uptime_get() - checking_since
					   > 30 * 1000) {
					ota_ui_set(OTA_UI_FAILED, NULL, 0);
					ota_ui_set_error("No answer from the app");
					checking_since = 0;
				}
			} else {
				checking_since = 0;
			}
		}
		/* No completion handling: the daemon writes slot0 with esptool
		 * and resets the board itself, so the next thing that happens
		 * here is a boot. See the OTA block in proto.c.
		 */

		if (ui_anim_pending()) {
			ui_anim_run(usb_anim_pump);
		}
		ui_settings_service(usb_anim_pump);

		if (!usage_view_have_data()) {
			/* Keep the bar honest: hello was answered at boot,
			 * but proto declares the host gone after 35 s of
			 * silence -- drop back to the link step then. */
			int want = proto_host_seen() ? 1 : 0;

			if (want != stage_shown) {
				usage_view_boot_stage(want);
				stage_shown = want;
			}

			/*
			 * Waiting-for-host timeout.
			 *
			 * This used to cold-reboot into standalone
			 * (user request 2026-07-16), on the theory that a
			 * daemon which has not spoken is not coming back and
			 * a board with WiFi and a token can serve itself. On
			 * a desk it read as an unexplained reset, and it made
			 * the two never-heard-from-a-daemon cases behave
			 * differently for no reason a user could see: a board
			 * that once talked to the app dozes, a board that
			 * never did rebooted. Now both doze (owner's call,
			 * 2026-09-02). The cost is real and accepted: a board
			 * that could have fetched its own usage over WiFi
			 * sleeps instead. Standalone is still chosen at boot
			 * in main() when no daemon answers the splash.
			 *
			 * Still gated on the host being GONE rather than
			 * slow, which is what stops a live-but-busy daemon
			 * from dozing us mid-conversation -- and note that
			 * proto_host_seen() goes false again after
			 * HOST_TIMEOUT_MS, so the timer restarts on every
			 * silence rather than only the first.
			 */
			if (proto_host_seen()) {
				host_quiet_since = k_uptime_get();
			} else if (!ota_blocks_sleep() &&
				   !ui_settings_busy() &&
				   k_uptime_get() - host_quiet_since
				   > 60 * 1000) {
				/* Both refusals are the ones argued at the
				 * doze block above, and this is the branch
				 * they matter most in. It is the one that
				 * was found looping on the watchdog -- this
				 * loop feeds the unconfirmed image's, and a
				 * board with no daemon is exactly the board
				 * whose image cannot prove itself, so the
				 * two conditions coincide precisely here.
				 * And it is the one an owner meets when the
				 * app is not installed: this is the doze
				 * they swipe out of to reach settings, so
				 * dozing again while they are in there would
				 * shut the door behind them. The timer keeps
				 * running, so the doze happens the moment
				 * either reason clears. */
				printk("[usage] no app after 60 s; dozing until one speaks\n");
				ui_sleep_run(host_is_back,
					     "Waiting for the app on your computer.");
				host_quiet_since = k_uptime_get();
				continue;
			}
		}

		int64_t now = k_uptime_get();

		if (now - last_tick >= 1000) {
			usage_view_tick_1s();
			last_tick = now;

			int hh = -1, mm = 0;

			net_time_local(&hh, &mm);
			usage_view_set_clock(hh, mm);
		}
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

int main(void)
{
	printk("[usage] firmware boot OK\n");

	/* Route mbedTLS at the kernel heap. prj.conf sets MBEDTLS_ENABLE_HEAP=n
	 * so no static _mbedtls_heap is reserved: TLS and WiFi now share
	 * HEAP_MEM_POOL_SIZE, which is sized for the larger of the two peaks
	 * instead of their sum (they never overlap -- the SoftAP is down
	 * whenever OTA runs, and TLS is idle during provisioning). That is what
	 * frees enough DRAM for the 16 KB inbound record buffer the release
	 * CDN's full-size records require.
	 *
	 * MUST happen before any TLS use: without it mbedTLS falls back to libc
	 * calloc, and picolibc's heap here is ~5 KB, so every handshake would
	 * fail. k_calloc/k_free match mbedTLS's expected signatures exactly. */
	if (mbedtls_platform_set_calloc_free(k_calloc, k_free) != 0) {
		printk("[usage] FATAL: mbedTLS allocator hookup failed\n");
		return -1;
	}

	if (!device_is_ready(display_dev)) {
		printk("[usage] display not ready\n");
		return -1;
	}
	panel_fix_madctl();
	display_blanking_off(display_dev);
	ui_touchfx_init();	/* light touch-echo feedback on every press */

	cfg_init();
	ota_boot_begin();	/* unconfirmed image? start the confirm clock */
	/* ...and from here the boot screen's own wait loops feed it too; they
	 * block for seconds against a 30 s window. */
	ui_boot_set_pump(ota_boot_pump);
	backlight_init();	/* drive the PWM to the persisted level */
#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
	net_wifi_init();
	net_wifi_set_idle_hook(wifi_idle);
	ap_psk_setup();		/* before any QR or AP use */
#else
	/* Nothing in this build can use a refresh token -- the OAuth code is
	 * not compiled in. But a unit flashed with a WiFi build still has one
	 * in NVS, and unfused flash is readable over USB with esptool, so
	 * compiling the writer out does not make the customer's token go
	 * away. Erase it on the first boot that cannot use it. */
	{
		char tok[CFG_TOKEN_MAX];

		if (cfg_get_token(tok, sizeof(tok))) {
			cfg_clear_token();
			printk("[usage] cleared a stored refresh token: no WiFi in this build\n");
		}
	}
#endif

#ifdef TEST_SCREEN
	ui_setup_show();
	for (;;) {
		const struct { enum ui_setup_state s; const char *d; } seq[] = {
			{ UI_SETUP_WAIT, NULL }, { UI_SETUP_PHONE, NULL },
			{ UI_SETUP_WIFI_OK, NULL }, { UI_SETUP_SIGNIN, NULL },
			{ UI_SETUP_DONE, NULL },
		};
		for (int i = 0; i < 5; i++) {
			ui_setup_set_state(seq[i].s, seq[i].d);
			for (int t = 0; t < 250; t++) { pump_ui(); k_sleep(K_MSEC(10)); }
		}
	}
#endif

	/* proto first: its hello goes out now, so a daemon's reply is already
	 * in flight during the splash and the selection can short-circuit. */
	proto_init();

	/* A mid-provisioning reboot (WiFi stored, token not yet) has exactly
	 * one destination: the setup flow. Route it straight there -- even
	 * the brief splash-colored interstitial between setup screens read
	 * as dead time on hardware (user feedback 2026-07-16). A PC daemon
	 * never loses the board to this shortcut: the daemon opening the
	 * port is a hard reset, which clears the intentional mark. */
#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
	if (ui_boot_intentional_pending()) {
		char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], tok[CFG_TOKEN_MAX];

		if (cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk)) &&
		    !cfg_get_token(tok, sizeof(tok))) {
			printk("[usage] mode: provisioning (resume, no splash)\n");
			run_provisioning(NULL);	/* reboots when done */
		}
	}
#endif

	ui_boot_splash();

	/* Detection, not a menu. First match wins: a talking daemon, then a
	 * reachable home network, then setup. Plugging into a PC later still
	 * works -- the daemon opening the port resets this board, so it always
	 * announces itself into a fresh splash. */
	if (proto_host_seen()) {
		printk("[usage] mode: USB bridge\n");
		usage_view_init();
		/* Same CONNECTING bar as standalone (user request
		 * 2026-07-16); the link step is already done -- the daemon
		 * spoke during the splash. */
		usage_view_boot_begin(usb_boot_steps, 2);
		usage_view_boot_stage(1);
		lv_timer_handler();
		ui_boot_teardown();	/* only after the new screen is loaded */
		ui_settings_attach(lv_scr_act());
		ota_report_outcome();
		usage_view_set_status(USAGE_STATUS_DISCONNECTED);
		proto_resync();		/* daemon re-pushes time+usage right away */
		run_usb();
	}

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
	char tok[CFG_TOKEN_MAX], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	bool have_wifi = cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	bool have_tok = cfg_get_token(tok, sizeof(tok));

	if (have_wifi && have_tok) {
		/* Take over the screen BEFORE the scan: a setup-flow reboot
		 * should land on the progress UI immediately instead of
		 * holding the splash color for the scan's 1-2 s (user
		 * feedback 2026-07-16). The scan runs under the boot bar. */
		usage_view_init();
		/* Step list before the first frame: one rendered frame of a
		 * wrong takeover flashes visibly on this panel
		 * (user-reported 2026-07-15). */
		usage_view_boot_begin(wifi_boot_steps, 3);
		lv_timer_handler();
		ui_boot_teardown();
		ui_settings_attach(lv_scr_act());
		ota_report_outcome();

		switch (boot_ssid_scan(ssid)) {	/* may reboot (blind radio) */
		case SSID_VISIBLE:
			printk("[usage] mode: standalone WiFi\n");
			run_standalone();
			break;			/* unreachable */
		case SSID_ABSENT:
			/* Not in the scan -- but hidden SSIDs and phone
			 * hotspots rarely are. The join is the authority;
			 * the flag makes its failure decisive. */
			printk("[usage] mode: standalone WiFi (\"%s\" not in scan; probing)\n",
			       ssid);
			scan_said_absent = true;
			run_standalone();
			break;			/* unreachable */
		case SSID_RADIO_BLIND:
			printk("[usage] mode: provisioning (radio blind)\n");
			run_provisioning("no networks found; check antenna or power");
			break;			/* unreachable */
		}
	}

	printk("[usage] mode: provisioning\n");
	run_provisioning(NULL);	/* reboots when done */
#else
	/*
	 * No radio in this build, so there is nothing to fall through TO. The
	 * detection above had three destinations; this one has one, and
	 * run_usb() already owns the case it lands in: it shows the
	 * waiting-for-host step and times out on its own if no daemon ever
	 * speaks. Deliberately no proto_resync() here -- that exists to make a
	 * daemon that already said hello re-push, and by definition none has.
	 */
	printk("[usage] mode: USB bridge (no host yet)\n");
	usage_view_init();
	usage_view_boot_begin(usb_boot_steps, 2);
	lv_timer_handler();
	ui_boot_teardown();
	ui_settings_attach(lv_scr_act());
	ota_report_outcome();
	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	run_usb();
#endif
	return 0;
}
