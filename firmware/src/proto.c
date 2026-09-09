#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/ring_buffer.h>
#include <stdio.h>
#include <string.h>

#include "proto.h"
#include "msg_parse.h"
#include "ota.h"
#include "version.h"
#include "cfg_store.h"
#include "whatsnew.h"
#include "usage_view.h"
#include "usage_state.h"
#include "usage_freshness.h"
#include "net_time.h"
#include "version.h"
#include "cfg_store.h"

#define PROTO_VERSION BLINK_PROTO_VERSION
#define PING_INTERVAL_MS 10000
/*
 * The daemon answers every ping with a pong, so silence means it is genuinely
 * gone -- not merely between its 60 s usage polls. Three missed pings (10 s
 * apart) is enough to be sure without being twitchy about one dropped line.
 */
/* Three missed pongs (the board pings every 10 s). Past this a host that
 * has not said goodbye is taken to be asleep -- see proto_host_lost(). */
#define HOST_TIMEOUT_MS 30000
#define LINE_MAX 512
#define RX_RING_SIZE 1024

static const struct device *const console_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

static char line[LINE_MAX];
static size_t line_len;

/*
 * A number off the wire, or the default it arrived with, and never a value
 * outside the range the field can mean.
 *
 * strtod happily returns nan, inf and 1e300, and every one of those was cast
 * straight to an int -- undefined behaviour in C, and on this soft-float
 * target a saturated or arbitrary integer on the panel. The `time` handler
 * has always bounded its two fields; the usage fields did not. Anything that
 * is not a real number in range leaves the default in place, which every
 * field here defines as "unknown" or "none".
 */
#define SECS_MAX 2147483647.0
/*
 * Percentages can legitimately exceed 100: extra usage carries a weekly window
 * past its limit. The ceiling used to be 100, and num() does not clamp -- it
 * refuses -- so 102 left the caller's `double wp = 0` untouched and the panel
 * drew an empty ring at the moment the user went over. Accept the real number
 * and let usage_view saturate the ring.
 */
#define PCT_MAX 1000.0

static void num(const char *json, const char *key, double *out,
		double lo, double hi)
{
	double v;

	if (!msg_get_double(json, key, &v)) {
		return;
	}
	if (v != v || v < lo || v > hi) { /* nan, or out of range */
		return;
	}
	*out = v;
}
static int64_t last_ping_ms;
static int64_t last_host_ms;
static bool host_seen;
static bool host_bye;		/* the app said it is going, on purpose */
static bool host_lost;		/* silence past the timeout, without a bye */
static char host_ver[16];	/* the daemon's release version, from welcome */
static int host_proto;		/* ...and the protocol it speaks */

/*
 * RX is interrupt-driven, not polled. On a real UART at 115200 baud the 128-byte
 * hardware FIFO holds only ~11 ms of traffic, so any main-loop stall longer than
 * that (an LVGL frame, a TLS handshake) would silently truncate an inbound line.
 * The ISR drains the FIFO into this ring buffer; proto_service() consumes it at
 * whatever pace the main loop happens to run.
 */
RING_BUF_DECLARE(rx_ring, RX_RING_SIZE);
static uint32_t rx_dropped;

static void uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	if (!uart_irq_update(dev)) {
		return;
	}

	while (uart_irq_rx_ready(dev)) {
		uint8_t buf[64];
		int n = uart_fifo_read(dev, buf, sizeof(buf));

		if (n <= 0) {
			break;
		}
		if (ring_buf_put(&rx_ring, buf, n) < (uint32_t)n) {
			rx_dropped++;
		}
	}
}

static void emit(const char *json)
{
	/* JSON lines go to the same console UART; '\n'-terminated. */
	printk("%s\n", json);
}

void proto_send_pref(void)
{
	char buf[64];

	snprintf(buf, sizeof(buf),
		 "{\"t\":\"pref\",\"v\":%d,\"provider\":\"%s\"}",
		 PROTO_VERSION,
		 cfg_get_main_src() == CFG_MAIN_SRC_CODEX ? "codex" : "claude");
	emit(buf);
}

static void send_hello(void)
{
	uint8_t id[16];
	char idhex[33] = "unknown";
	ssize_t n = hwinfo_get_device_id(id, sizeof(id));

	if (n > 0) {
		for (ssize_t i = 0; i < n && i < 16; i++) {
			snprintf(idhex + i * 2, 3, "%02x", id[i]);
		}
	}

	uint32_t cause = 0;
	(void)hwinfo_get_reset_cause(&cause);

	char buf[160];
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"hello\",\"v\":%d,\"board\":\"cyd\","
		 "\"board_id\":\"%s\",\"fw\":\"" BLINK_FW_VERSION "\","
		 "\"reset\":\"0x%x\"}",
		 PROTO_VERSION, idhex, cause);
	emit(buf);
	/* Straight after hello: a daemon that starts later, or restarts, has
	 * no other way to learn a preference the user set while it was gone. */
	proto_send_pref();
}

static void send_ping(void)
{
	char buf[64];
	snprintf(buf, sizeof(buf), "{\"t\":\"ping\",\"v\":%d,\"up_ms\":%lld}",
		 PROTO_VERSION, (long long)k_uptime_get());
	emit(buf);
}


/*
 * --- OTA while tethered ---
 *
 * USB-bridge mode has no network of its own: run_usb() never starts
 * net_worker, so ota.c's HTTPS updater is unreachable and the update row did
 * nothing here. The daemon has both an internet connection and the board's
 * USB serial link, so the split is: the board asks and approves, the daemon
 * fetches and writes.
 *
 * The board does NOT receive the image. An earlier revision pushed it down
 * this protocol in base64 chunks and it was hopeless -- 33% encoding overhead
 * on top of a stop-and-wait round trip per chunk, measured at 213 B/s, and
 * then MCUboot still had to swap slot1 into slot0 afterwards (121-357 s by the
 * notes in ui_settings.c). The daemon instead runs esptool against slot0
 * directly, which this project has been doing by hand all along at ~17 KB/s:
 * about 75 s for a 1.3 MB image, and no swap at all because the bytes land
 * where the bootloader already looks.
 *
 * So all that crosses this link is consent. The board sends ota_flash and puts
 * up a "keep it connected" screen; the next thing it experiences is esptool
 * resetting it into the ROM loader.
 *
 * The trade is real and worth stating: writing slot0 in place gives up
 * MCUboot's test-boot and auto-revert, so a bad image needs a reflash rather
 * than recovering by itself. That is acceptable *here specifically* because
 * this path only exists while a machine with esptool is physically cabled to
 * the board -- the recovery is the same cable that caused it.
 */
static struct ota_manifest ota_m;
static bool ota_staged;		/* ota_avail seen: ota_m is valid */
/* The daemon version this release also carries, when it is newer than the one
 * on the cable. Empty otherwise. The confirmation screen says so, because one
 * tap is about to install both halves. */
static char ota_app[16];

const char *proto_ota_app_version(void)
{
	return ota_app;
}

bool proto_ota_staged(void)
{
	return ota_staged;
}

void proto_ota_check(void)
{
	char buf[96];

	ota_staged = false;
	ota_app[0] = '\0';
	ota_ui_set(OTA_UI_CHECKING, NULL, 0);
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"ota_query\",\"v\":%d,\"cur\":\"%s\"}",
		 PROTO_VERSION, BLINK_FW_VERSION);
	emit(buf);
}

bool proto_ota_install(void)
{
	char buf[64];

	if (!ota_staged) {
		return false;
	}
	/*
	 * Say which link this is on BEFORE raising the state: ota_ui_source()
	 * otherwise still reads WIFI (its default) and the progress screen
	 * announces a download that is not happening -- reported 2026-08-21,
	 * where a USB flash was labelled "Over WiFi".
	 */
	ota_ui_set_source(OTA_SRC_USB);

	/*
	 * The breadcrumb is NOT written here, at consent. It is written when
	 * the daemon says the write is starting (ota_begin below).
	 *
	 * It used to be written here, and that is wrong for a pair update: the
	 * daemon replaces itself first, and the new process opening the serial
	 * port resets this board. It would boot, find a breadcrumb naming a
	 * version it is not running, and announce "Update failed, previous
	 * version restored." before the firmware install had begun -- then
	 * spend the breadcrumb doing it, so the real success went unreported.
	 */

	/* Percent stays at 0 throughout: the board cannot see esptool's
	 * progress, and a bar that does not move is worse than no bar. The UI
	 * shows the "keep it connected" wording for this source instead. */
	ota_ui_set(OTA_UI_DOWNLOADING, &ota_m, 0);
	snprintf(buf, sizeof(buf), "{\"t\":\"ota_flash\",\"v\":%d}",
		 PROTO_VERSION);
	emit(buf);
	return true;
}

static void dispatch(const char *json)
{
	char type[16];
	if (!msg_get_str(json, "t", type, sizeof(type))) {
		return; /* not a protocol line */
	}

	last_host_ms = k_uptime_get();
	host_seen = true;
	host_lost = false;
	if (strcmp(type, "bye") == 0) {
		/*
		 * A clean exit -- uninstall. Without this a computer with no
		 * app any more looks exactly like a sleeping one, and the
		 * face would doze instead of saying "connecting".
		 */
		printk("[proto] host says bye\n");
		host_bye = true;
		return;
	}
	host_bye = false;
	if (strcmp(type, "usage") == 0) {
		double sp = 0, wp = 0;
		/* Remaining seconds, not the absolute resets_at timestamps: the
		 * board has no wall clock when tethered over USB, so the daemon
		 * does the subtraction. -1 means unknown.
		 */
		double ss = -1, ws = -1;

		num(json, "session_pct", &sp, -1, PCT_MAX);
		num(json, "weekly_pct", &wp, -1, PCT_MAX);
		num(json, "session_resets_in_s", &ss, -1, SECS_MAX);
		num(json, "weekly_resets_in_s", &ws, -1, SECS_MAX);
		usage_view_update(sp, (int32_t)ss, wp, (int32_t)ws);

		/* Flat model key (protocol.py flattens its models list for
		 * us); an absent key leaves the -1 "unknown" default. */
		double mf = -1;

		num(json, "fable_pct", &mf, -1, PCT_MAX);
		usage_view_set_models(mf);

		/* Every usage message says whether its own numbers can be
		 * trusted, so the dot is decided here rather than by a
		 * separate status message arriving afterwards.
		 *
		 * This must run AFTER usage_view_update() and
		 * usage_view_set_models(), both of which set OK internally --
		 * ordering it earlier would have the amber immediately
		 * overwritten by green.
		 *
		 * Before this existed the daemon had to fake it, sending
		 * status "rate_limited" purely because that string already
		 * mapped to amber -- which made a stale reading and a real
		 * rate limit indistinguishable on the panel. An absent key
		 * leaves `stale` false, so a daemon older than this firmware
		 * still reads as OK rather than as a warning.
		 */
		bool stale = false;

		msg_get_bool(json, "stale", &stale);
		usage_view_set_provider1_stale(stale);
		/*
		 * The status is armed if EITHER page is old; usage_view then
		 * shows the warning only on a page it is actually true of.
		 * Set below, after p2 has been parsed, because until then only
		 * half the answer is known -- and arming on the first
		 * provider alone was the mirror of the bug this fixes: a fresh
		 * codex reading beside a stale claude one left the claude page
		 * claiming to be current.
		 */

		/*
		 * The multi-provider fields. All OPTIONAL, and all defaulting
		 * to "say nothing": the daemon omits a key it has no answer
		 * for rather than sending a sentinel, so an absent key and an
		 * old daemon are the same case here and neither may turn an
		 * indicator on. See pc/protocol.usage().
		 */
		char state[16];
		enum usage_activity act = USAGE_ACTIVITY_NONE;

		if (msg_get_str(json, "state", state, sizeof(state))) {
			act = usage_activity_from_state(state);
		}
		usage_view_set_activity(act);

		/* Session and agent counts. Absent means zero here rather than
		 * unknown -- a daemon that sends no count is one with nothing
		 * to report, and the readout hides itself at one session with
		 * no agents anyway. */
		double ns = 0, na = 0;

		num(json, "n_sess", &ns, 0, 9999);
		num(json, "n_agents", &na, 0, 9999);
		usage_view_set_sessions((int)ns, (int)na);

		double nr = 0, nw = 0, nst = 0;

		/* Sent since the counts existed, dropped on the floor until
		 * the pip row had a use for them. Absent means zero on both
		 * sides, which is why the daemon omits them. */
		num(json, "n_run", &nr, 0, 9999);
		num(json, "n_wait", &nw, 0, 9999);
		num(json, "n_stuck", &nst, 0, 9999);
		usage_view_set_counts((int)ns, (int)nr, (int)nw, (int)nst);

		char p1[16];

		if (msg_get_str(json, "provider", p1, sizeof(p1))) {
			usage_view_set_provider1(p1);
		}

		/*
		 * How fast the session window is filling, when nothing can say
		 * WHEN it rolls. Absent is the normal case and means zero, so a
		 * daemon older than this firmware simply never fills the
		 * countdown slot -- which is what it does today.
		 *
		 * Sent only when there is no session reset time; the two are
		 * mutually exclusive on the daemon's side, so this is set
		 * unconditionally and the view prefers the countdown anyway.
		 */
		double burn = 0;

		num(json, "burn_pph", &burn, 0, 9999);
		usage_view_set_burn(burn);

		char p2[16];
		double p2s = -1, p2w = -1, p2si = -1, p2wi = -1;

		if (msg_get_str(json, "p2", p2, sizeof(p2))) {
			bool p2stale = false;

			num(json, "p2_session_pct", &p2s, -1, PCT_MAX);
			num(json, "p2_weekly_pct", &p2w, -1, PCT_MAX);
			num(json, "p2_s_in_s", &p2si, -1, SECS_MAX);
			num(json, "p2_w_in_s", &p2wi, -1, SECS_MAX);
			/* Absent leaves this false, so a daemon older than
			 * this firmware reports its second provider as fresh
			 * rather than as old -- the reading it sent IS the
			 * latest one it has, and the alternative is a page
			 * permanently labelled stale by a missing key. */
			msg_get_bool(json, "p2_stale", &p2stale);
			usage_view_set_provider2(p2, p2s, p2w, (int32_t)p2si,
						 (int32_t)p2wi, p2stale);
			stale = stale || p2stale;
		} else {
			usage_view_set_provider2("", -1, -1, -1, -1, false);
		}

		/*
		 * How old each READING is, as opposed to how long ago the
		 * daemon last spoke. Both defaults stay -1 when the keys are
		 * absent, which is how a daemon older than this firmware
		 * leaves the panel counting from the message exactly as it
		 * always did.
		 *
		 * After the p2 block: set_provider2() with an empty tag drops
		 * that page, and an age written before it would be an age for
		 * a page that no longer exists.
		 */
		double age = -1, p2age = -1, active_age = -1;

		num(json, "age_s", &age, -1, SECS_MAX);
		num(json, "p2_age_s", &p2age, -1, SECS_MAX);
		usage_view_set_ages((int32_t)age, (int32_t)p2age);

		/*
		 * And the other freshness question, which is not drawn
		 * anywhere: how long since ANY tool on that machine wrote
		 * anything at all. One figure for the whole desk rather than
		 * one per page, because it decides whether the board dozes
		 * and the board dozes as one screen.
		 *
		 * Absent leaves it -1, and usage_freshness_note then reads
		 * the reading's own age instead -- which is exactly right for
		 * a daemon that predates the field. See usage_freshness.h.
		 */
		num(json, "active_age_s", &active_age, -1, SECS_MAX);

		/* The same facts, kept where main.c can ask and a laptop can
		 * test the reasoning about them. See usage_freshness.h. */
		usage_freshness_note((int32_t)age, (int32_t)active_age, act,
				     k_uptime_get());

		/*
		 * AFTER both providers, and after the calls above that set OK
		 * internally -- usage_view_update() and set_models() both do,
		 * so arming this earlier would have the amber immediately
		 * overwritten by green. See the note where `stale` is read.
		 */
		if (stale) {
			usage_view_set_status(USAGE_STATUS_STALE);
		}

		printk("[usage] session %.0f%% (%ds)  weekly %.0f%% (%ds)%s%s\n",
		       sp, (int)ss, wp, (int)ws, stale ? "  STALE" : "",
		       act == USAGE_ACTIVITY_NONE ? "" : " +state");
	} else if (strcmp(type, "time") == 0) {
		double epoch = 0, off = 0;

		if (msg_get_double(json, "epoch", &epoch) &&
		    msg_get_double(json, "utc_offset_min", &off) &&
		    epoch > 0 && epoch < 4102444800.0 &&	/* < year 2100 */
		    off >= -840 && off <= 840) {
			net_time_set_manual((int64_t)epoch);
			net_time_set_offset((int32_t)off);
			printk("[proto] host time synced\n");
		}
	} else if (strcmp(type, "pong") == 0) {
		/* Liveness only: last_host_ms was already stamped above. */
	} else if (strcmp(type, "edition") == 0) {
		/*
		 * A factory fact arriving over the cable, ONCE, after the
		 * board is programmed -- see cfg_edition in cfg_store.h. The
		 * enclosure decides which clip is right, and a user who could
		 * flip it would only be putting the wrong animation in the
		 * wrong box.
		 *
		 * "Not reachable from the settings screen" is not the whole
		 * story and used to be treated as if it were. This message
		 * arrives from whatever is on the other end of the cable, and
		 * the tool that sends it is the same binary the user installs
		 * -- so the enforcement has to be in the record, not in the
		 * UI. cfg_set_edition latches on the first successful write
		 * and refuses every one after it.
		 *
		 * Takes effect on the next boot, because what it selects is a
		 * boot animation. Saying so in the log is the difference
		 * between "it did nothing" and "it will do it in a moment".
		 */
		char ed[12];

		if (msg_get_str(json, "edition", ed, sizeof(ed))) {
			uint8_t v;

			if (strcmp(ed, "claude") == 0) {
				v = CFG_EDITION_CLAUDE;
			} else if (strcmp(ed, "codex") == 0) {
				v = CFG_EDITION_CODEX;
			} else {
				printk("[cfg] unknown edition '%s'; ignored\n", ed);
				return;
			}
			/*
			 * The LATCH decides, not the value.
			 *
			 * An earlier version skipped the write whenever the
			 * stored edition already matched, which looks like a
			 * harmless optimisation and is a hole: 0 means both
			 * "Claude" and "never stamped", so provisioning a
			 * blank board as claude reported success, wrote
			 * nothing, and left it stampable as codex afterwards
			 * by anyone with the cable.
			 */
			int rc;

			if (cfg_edition_locked()) {
				printk("[cfg] edition already stamped as %s;"
				       " refusing to change it\n",
				       cfg_get_edition() == CFG_EDITION_CODEX
					       ? "codex" : "claude");
			} else if ((rc = cfg_set_edition(v)) == 0) {
				printk("[cfg] edition set to %s"
				       " (applies on next boot)\n", ed);
			} else {
				printk("[cfg] could not store the edition"
				       " (%d)\n", rc);
			}
		}
	} else if (strcmp(type, "welcome") == 0) {
		double hv = 0;

		/*
		 * Both of these used to be dropped on the floor. The board had
		 * no way to tell a customer that the half of the product on
		 * their computer was the half that needed updating -- and that
		 * is the only half that cannot update itself from here.
		 */
		if (!msg_get_str(json, "app_ver", host_ver, sizeof(host_ver))) {
			host_ver[0] = '\0';
		}
		host_proto = msg_get_double(json, "v", &hv) ? (int)hv : 0;
		printk("[proto] host connected: app %s, protocol %d\n",
		       host_ver[0] ? host_ver : "?", host_proto);
		/*
		 * Every host connection is a firmware check, not just the
		 * first after boot. The daemon replaces itself for an app
		 * update and comes back with a newer release on its feed;
		 * a board that only asked once per boot sat on 1.0.0 with
		 * 1.1.1 published (2026-08-29). The daemon now offers on
		 * its own as well, for boards running this code's
		 * predecessors; this side covers the case where it does not
		 * know what we run.
		 */
		ota_request_check();
		/*
		 * Answer. The daemon sends `welcome` as a question -- "is a
		 * Blink board on this port?" -- and decides whether to pull
		 * the reset line on whether anything comes back within 1.5 s
		 * (claude_usage_bridge.probe_is_our_board). This handler used
		 * to reply with nothing: the ota_query it was credited with
		 * is sent once per boot by main.c, so from the second host
		 * connection of a boot onwards the probe heard silence unless
		 * a 10 s ping happened to land, and the board was reset -- and
		 * replayed its boot clip -- on most daemon restarts anyway.
		 * The preference is the right answer: it is what a host that
		 * has just connected needs to know, and it already rides with
		 * hello for the same reason.
		 */
		proto_send_pref();
	} else if (strcmp(type, "ota_avail") == 0) {
		double sz = 0;

		if (msg_get_str(json, "version", ota_m.version,
				sizeof(ota_m.version)) &&
		    msg_get_str(json, "sha256", ota_m.sha256,
				sizeof(ota_m.sha256)) &&
		    msg_get_double(json, "size", &sz) && sz > 0) {
			ota_m.size = (uint32_t)sz;
			/* Optional: absent on any release that predates pair
			 * updates, and on any release where this computer's
			 * app is already current. */
			if (!msg_get_str(json, "app", ota_app,
					 sizeof(ota_app))) {
				ota_app[0] = '\0';
			}
			ota_staged = true;
			ota_ui_set(OTA_UI_AVAILABLE, &ota_m, 0);
			printk("[proto] daemon offers %s (%u bytes)\n",
			       ota_m.version, ota_m.size);
		} else {
			ota_ui_set(OTA_UI_FAILED, NULL, 0);
		}
	} else if (strcmp(type, "ota_begin") == 0) {
		/* The write is starting; nothing can now come between this and
		 * esptool resetting us, so the next boot's report is honest
		 * whichever way it goes. */
		char v[16];

		if (msg_get_str(json, "version", v, sizeof(v))) {
			char trail[CFG_OTA_VER_MAX];

			printk("[proto] ota: writing %s\n", v);
			/* Where we came FROM as well as where we are going.
			 * The next boot's popup needs it to say what changed
			 * across a jump of more than one release, and this is
			 * the last moment anything knows: after the write
			 * this image is gone. See whatsnew.h for why it is
			 * packed into the existing field. */
			whatsnew_trail(BLINK_FW_VERSION, v, trail,
				       sizeof(trail));
			cfg_set_ota_state(1, trail);
		}
	} else if (strcmp(type, "ota_resume") == 0) {
		/*
		 * The daemon replaced itself mid-update and is picking up an
		 * install this board already approved. Without this the panel
		 * would come back to an "Install?" prompt for something
		 * already under way, and the user would answer it twice.
		 */
		if (msg_get_str(json, "version", ota_m.version,
				sizeof(ota_m.version))) {
			printk("[proto] daemon resuming install of %s\n",
			       ota_m.version);
			ota_ui_set_source(OTA_SRC_USB);
			ota_ui_set(OTA_UI_DOWNLOADING, &ota_m, 0);
		}
	} else if (strcmp(type, "ota_none") == 0) {
		ota_staged = false;
		ota_ui_set(OTA_UI_UP_TO_DATE, NULL, 0);
	} else if (strcmp(type, "ota_error") == 0) {
		char why[48] = "";

		ota_staged = false;
		ota_ui_set(OTA_UI_FAILED, NULL, 0);
		/* After the state, not before: ota_ui_set() clears the reason
		 * on any state that is not FAILED, so the order matters. */
		if (msg_get_str(json, "why", why, sizeof(why))) {
			printk("[proto] ota failed: %s\n", why);
			ota_ui_set_error(why);
		}
	} else if (strcmp(type, "status") == 0) {
		char st[24] = "";

		msg_get_str(json, "state", st, sizeof(st));
		printk("[proto] host status: %s\n", st);
		usage_view_set_status(strcmp(st, "rate_limited") == 0 ?
				      USAGE_STATUS_STALE : USAGE_STATUS_ERROR);
	} else if (strcmp(type, "session") == 0) {
		/*
		 * Which project is doing the thing the dot is already
		 * colouring. Its own message rather than a field on `usage`
		 * because that line is at 506 of the 512 this parser accepts
		 * and an over-long one is dropped whole -- see
		 * pc/protocol.py:331.
		 *
		 * An absent label is the normal several-sessions case, not a
		 * parse failure: the daemon refuses to name one of several,
		 * and `n` carries the meaning instead.
		 */
		char lbl[28] = "";
		double n = 0;

		msg_get_str(json, "label", lbl, sizeof(lbl));
		num(json, "n", &n, 0, 9999);
		usage_view_set_session(lbl, (int)n);
	}
	/* unknown types ignored */
}

static void drain_rx(void)
{
	uint8_t c;
	/*
	 * Set on overflow and cleared at the next newline. Without it an
	 * over-long line was not dropped whole, as the comment on LINE_MAX
	 * (and pc/protocol.py, which mirrors it) claimed: line_len went back
	 * to 0 and the REST of the same line went on accumulating, to be
	 * dispatched as a message of its own. With a scanner that finds keys
	 * anywhere in the text, a tail that happened to contain "t":"..."
	 * parsed as a frame the host never sent.
	 */
	static bool discarding;

	while (ring_buf_get(&rx_ring, &c, 1) == 1) {
		if (c == '\n' || c == '\r') {
			if (discarding) {
				discarding = false;
			} else if (line_len > 0) {
				line[line_len] = '\0';
				dispatch(line);
			}
			line_len = 0;
		} else if (discarding) {
			/* the tail of a line already given up on */
		} else if (line_len < LINE_MAX - 1) {
			line[line_len++] = (char)c;
		} else {
			line_len = 0;
			discarding = true; /* overflow: drop the WHOLE line */
		}
	}

	if (rx_dropped) {
		printk("[proto] rx ring overflow, dropped %u bytes\n", rx_dropped);
		rx_dropped = 0;
	}
}

void proto_init(void)
{
	line_len = 0;

	if (console_dev && device_is_ready(console_dev)) {
		uart_irq_callback_user_data_set(console_dev, uart_isr, NULL);
		uart_irq_rx_enable(console_dev);
	}

	send_hello();
	last_ping_ms = k_uptime_get();
}

const char *proto_host_version(void)
{
	return host_ver;
}

bool proto_host_outdated(void)
{
	/* Only ever an advisory. The pair ships from one tag, so a daemon
	 * older than this firmware means somebody's install is half-updated --
	 * usually because the app on the computer has no way to update itself
	 * that the customer has noticed. Usage keeps flowing either way. */
	return host_seen && host_ver[0] &&
	       ota_version_newer(BLINK_FW_VERSION, host_ver);
}

bool proto_host_seen(void)
{
	return host_seen;
}

void proto_service(void)
{
	drain_rx();
	int64_t now = k_uptime_get();

	if (now - last_ping_ms >= PING_INTERVAL_MS) {
		send_ping();
		last_ping_ms = now;
	}

	/* If the host goes away, say so. Holding a green dot over numbers that
	 * stopped updating is worse than admitting we are disconnected. The
	 * daemon polls every 60 s, so the window must be comfortably longer.
	 */
	if (host_seen && (now - last_host_ms) > HOST_TIMEOUT_MS) {
		printk("[proto] host went away%s\n",
		       host_bye ? " (said bye)" : "");
		usage_view_set_status(USAGE_STATUS_DISCONNECTED);
		host_seen = false;
		host_lost = !host_bye;
		host_bye = false;
	}
}

void proto_resync(void)
{
	send_hello();
}

bool proto_host_lost(void)
{
	return host_lost;
}
