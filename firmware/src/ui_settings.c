/*
 * Settings panel: the recovery actions that used to require reflashing.
 *
 * Each action clears one persistence tier and reboots -- the boot path then
 * lands in the right flow by itself (no creds -> provisioning, no token ->
 * sign-in, nothing -> mode selection). Deliberately no in-place teardown of
 * the running mode: a cold reboot is simpler and reclaims all memory.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/printk.h>
/*
 * The gesture thresholds are per-indev fields with no public setter and no
 * Kconfig -- LVGL 9.3 hard-codes the defaults in lv_indev.c. See
 * tune_gestures() for why they cannot be left alone on this panel.
 */
#include <indev/lv_indev_private.h>
#include <lvgl.h>
#include <stdio.h>

#include "ui_settings.h"
#include "cfg_store.h"
#include "ui_boot.h"
#include "ui_anim.h"
#include "ui_swipe.h"
#include "ui_slide.h"
#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
#include "net_wifi.h"
#endif
#include "fmt.h"
#include "version.h"
#include "backlight.h"
#include "ota.h"
#include "proto.h"
#include "upd_row.h"
#include "usage_view.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_RED		lv_color_hex(0xE74C3C)
#define COL_GREEN	lv_color_hex(0x2ECC71)
#define COL_AMBER	lv_color_hex(0xF1C40F)
#define COL_PANEL	lv_color_hex(0x161A20)	/* card fill, sits above COL_BG */
#define COL_LINE	lv_color_hex(0x20252D)	/* the full-width section rules */
#define COL_DANGER_BG	lv_color_hex(0x1E1412)	/* factory tile: red-tinted, not solid red */
#define COL_DANGER_BD	lv_color_hex(0x7A2B23)

/*
 * Destructive actions -- and the confirm-then-reboot machinery behind them --
 * are standalone-WiFi-only.
 *
 * Every one of them forgets something the device is holding: a network, a
 * token, or all of it. A USB unit holds none of that. The config record a
 * reset would wipe contains a brightness level and an OTA breadcrumb the next
 * boot clears anyway, so the whole ceremony -- a confirm dialog, a cold
 * reboot -- bought a return to 100% brightness, which the Brightness row does
 * on the spot. See the layout note in the USB panel builder.
 */
#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
enum action {
	ACT_WIFI,	/* forget network, keep token */
	ACT_SIGNIN,	/* forget token, keep network */
	ACT_FACTORY,	/* forget everything */
};
#endif

static lv_obj_t *panel;		/* NULL when closed */
/* Shared with the software-update install dialog, which is in both builds. */
static lv_obj_t *confirm;	/* NULL when no dialog is up */
#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
static enum action pending;
#endif

/* Software-update widgets (all NULL when their owner is closed/absent). */
static lv_obj_t *notice;	/* outcome popup on the top layer */
static lv_obj_t *upd_btn;
static lv_obj_t *upd_lbl;
static lv_timer_t *upd_timer;	/* whole session, not just the panel */
/* Download progress. Full-screen and touch-swallowing on purpose, so it must
 * be torn down the moment the download stops -- a successful install ends in a
 * reboot, but a FAILED one returns to a live UI, and leaving this up locked the
 * screen against every tap (user-reported 2026-07-25: "after I confirmed the
 * popup the screen looks stuck"). It was never deleted anywhere. */
static lv_obj_t *dl_overlay;
static lv_obj_t *dl_bar;
static lv_obj_t *dl_lbl;
static lv_obj_t *dl_sub;	/* time remaining, under the bar */
static lv_obj_t *dl_src;	/* which link the image is coming over */
/*
 * Download-rate estimator. A fixed baseline (first percent seen, extrapolated
 * to the end) was the first attempt and was unusable: for the first ~20 s it
 * divides a 1-2 percent delta by a couple of seconds, so a single slow TCP
 * window swings the answer by minutes, and because the baseline never moves
 * the early noise never washes out (user-reported 2026-07-25).
 *
 * Instead: measure ms-per-percent over each percent step, smooth it with an
 * exponential moving average, and hold back any number at all until a few
 * steps have landed. Between steps the shown value counts down off the clock
 * rather than sitting still, so it reads as a timer instead of a stutter.
 */
static int64_t dl_first_ms;	/* first non-zero percent: warm-up reference */
static int64_t dl_last_ms;	/* when dl_last_pct was observed */
static uint8_t dl_last_pct;
static int dl_mspp;		/* EMA of milliseconds per percent */
static int dl_samples;
static int64_t dl_eta_ms;	/* remaining, as computed at dl_eta_at_ms */
static int64_t dl_eta_at_ms;
static int dl_shown_left = -1;	/* last printed value, to stop 1 s jitter */
static enum ota_ui_state upd_seen = OTA_UI_IDLE;
static int64_t upd_revert_at;	/* "Up to date" shows briefly, then idles */

/*
 * Deadline for a USB install to take the board away from us.
 *
 * Over USB the daemon answers ota_flash by closing the port and running
 * esptool, which resets this chip into the ROM loader within seconds -- so in
 * the healthy case nothing here runs again at all. The unhealthy case is a
 * daemon that dies between our consent and esptool's reset: the panel then
 * held "Keep the cable connected" forever, because nothing on the board was
 * watching for a hand-off that never came.
 *
 * Generous on purpose. A false expiry is only cosmetic -- if esptool starts
 * late the flash still succeeds and the next boot reports the new version --
 * whereas expiring early on a slow machine would call a working update failed.
 */
/* Long enough for the slowest thing that can legitimately happen before the
 * board is taken away: in a PAIR update the app downloads a ~12 MB binary,
 * self-tests it, replaces itself and reconnects, all while this screen is up
 * and no message arrives. 90 s covered a firmware-only install and would have
 * expired in the middle of that. */
#define USB_DL_DEADLINE_MS 300000
static int64_t usb_dl_deadline;

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/* "Factory reset" is the honest name here and only here: this build's config
 * record really does hold the network credentials, the refresh token and the
 * AP password, so clearing it returns the device to the state it shipped in. */
static const char *const act_label[] = {
	[ACT_WIFI] = "Reset WiFi",
	[ACT_SIGNIN] = "Re-sign-in",
	[ACT_FACTORY] = "Factory reset",
};

/* Red is for the action that costs a full re-setup, and only that one;
 * painting every confirm red made them all look equally scary. */
static inline bool act_is_danger(enum action a)
{
	return a == ACT_FACTORY;
}

static void do_pending(void)
{
	printk("[settings] %s -> reboot\n", act_label[pending]);
	switch (pending) {
	case ACT_WIFI:
		cfg_clear_wifi();
		break;
	case ACT_SIGNIN:
		cfg_clear_token();
		break;
	case ACT_FACTORY:
		cfg_reset();
		break;
	}
	k_msleep(200);	/* let the NVS writes land */
	ui_boot_mark_intentional_reboot();
	sys_reboot(SYS_REBOOT_COLD);
}

static void confirm_yes_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	do_pending();	/* never returns */
}
#endif /* CONFIG_BLINK_WIFI_MODE -- destructive actions */

static void confirm_no_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_obj_del(confirm);
	confirm = NULL;
}

static lv_obj_t *mk_btn(lv_obj_t *parent, const char *txt, lv_color_t bg,
			lv_event_cb_t cb, void *user)
{
	lv_obj_t *b = lv_btn_create(parent);

	lv_obj_set_size(b, 200, 40);
	lv_obj_set_style_bg_color(b, bg, 0);
	/* In LVGL 9 every child is born SCROLLABLE *and* GESTURE_BUBBLE. Clear
	 * both: a tap that drifts a few px on the jittery panel was bubbling up
	 * as a right-swipe and closing settings. A control's gesture now stops
	 * at the control; swipe-to-close still works from the bare panel. */
	lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);

	lv_obj_t *l = lv_label_create(b);

	lv_label_set_text(l, txt);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_center(l);
	return b;
}

/* A full-width hairline rule at row y -- the grouped-list separator. */
static void mk_line(lv_obj_t *parent, int y)
{
	lv_obj_t *l = lv_obj_create(parent);

	lv_obj_set_size(l, LV_HOR_RES, 1);
	lv_obj_align(l, LV_ALIGN_TOP_LEFT, 0, y);
	lv_obj_set_style_bg_color(l, COL_LINE, 0);
	lv_obj_set_style_bg_opa(l, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(l, 0, 0);
	lv_obj_set_style_radius(l, 0, 0);
	lv_obj_clear_flag(l, LV_OBJ_FLAG_SCROLLABLE);
}

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/* A raised panel-coloured card (decoration only -- not clickable, so the
 * controls sitting on top of it still get every touch). */
static lv_obj_t *mk_card(lv_obj_t *parent, int x, int y, int w, int h)
{
	lv_obj_t *c = lv_obj_create(parent);

	lv_obj_set_size(c, w, h);
	lv_obj_align(c, LV_ALIGN_TOP_LEFT, x, y);
	lv_obj_set_style_bg_color(c, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(c, COL_TRACK, 0);
	lv_obj_set_style_border_width(c, 1, 0);
	lv_obj_set_style_radius(c, 11, 0);
	lv_obj_set_style_pad_all(c, 0, 0);
	lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
	/* The card is clickable-by-default and sits under the brightness gap;
	 * without this a drift-press between the steppers would bubble up and
	 * close the panel. Swallow the gesture here. */
	lv_obj_clear_flag(c, LV_OBJ_FLAG_GESTURE_BUBBLE);
	return c;
}
#endif /* CONFIG_BLINK_WIFI_MODE */

/*
 * Brightness is five discrete levels -- 20/40/60/80/100, see backlight.h -- so
 * it is drawn as five stops rather than a pair of steppers. Five positions and
 * five targets means one press to reach any of them, where +/- needed four to
 * cross the range.
 */
#define BRIGHT_STOPS 5

static lv_obj_t *pct_lbl;	/* the brightness row's subtitle: "60%" */

/*
 * No "Main source" row.
 *
 * It toggled which provider owned the outer ring and the big number -- back
 * when both providers shared one gauge and one of them had to be chosen. They
 * do not share it any more: each has a page of its own, reached with a swipe,
 * and "which one is in front" is now answered by which page you are looking
 * at. A stored preference that decides the same thing a second time is a
 * second answer to a question that already has one.
 *
 * cfg_get/set_main_src stay: the value is still sent to the host on every
 * hello (proto.c), where the daemon uses it to break ties when it merges
 * sources. That is a HOST-side meaning, and it is not something to settle from
 * across the room with a fingertip.
 */
static lv_obj_t *bright_big;	/* the big readout on the brightness screen */
static lv_obj_t *bright_ov;	/* the brightness screen itself, or NULL */
static lv_obj_t *seg[BRIGHT_STOPS];

static void bright_refresh(void)
{
	uint8_t p = backlight_get();
	char b[8];

	snprintf(b, sizeof(b), "%d%%", p);
	/* Every one of these is optional: the row exists only while the panel
	 * is open, and the readout and stops only while the brightness screen
	 * is. Writing through a pointer whose object has been deleted is the
	 * NULL-deref this file has already been bitten by once. */
	if (pct_lbl) {
		lv_label_set_text(pct_lbl, b);
	}
	if (bright_big) {
		lv_label_set_text(bright_big, b);
	}
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		uint8_t lvl = 20 + i * 20;

		if (seg[i] == NULL) {
			continue;
		}
		/* Below the level reads as filled, the level itself as bright,
		 * above it as empty -- so the setting is legible as a shape,
		 * without reading the number. That matters here specifically:
		 * the screen is at its dimmest exactly when someone reaches
		 * for this control. */
		lv_obj_set_style_bg_color(seg[i],
			lvl < p ? COL_DIM : (lvl == p ? COL_TEXT : COL_TRACK), 0);
	}
}

/* user_data carries the step direction (+1 / -1). */
#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/* The +/- stepper, kept for the WiFi layout only. The shipped panel picks a
 * level directly -- see show_bright() -- because stepping needed four presses
 * to cross a range with five positions. */
static void bright_step_cb(lv_event_t *e)
{
	backlight_step((int)(intptr_t)lv_event_get_user_data(e));
	bright_refresh();
}
#endif /* CONFIG_BLINK_WIFI_MODE */

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
static void show_confirm(void)
{
	confirm = lv_obj_create(panel);
	lv_obj_set_size(confirm, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(confirm, COL_BG, 0);
	lv_obj_set_style_bg_opa(confirm, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(confirm, 0, 0);
	lv_obj_set_style_pad_all(confirm, 0, 0);
	lv_obj_clear_flag(confirm, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(confirm);

	lv_obj_t *q = lv_label_create(confirm);

	lv_label_set_text_fmt(q, "%s?\nThe device will reboot.",
			      act_label[pending]);
	lv_obj_set_style_text_color(q, COL_TEXT, 0);
	lv_obj_set_style_text_align(q, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(q, LV_ALIGN_TOP_MID, 0, 40);

	/* Red is reserved for the one action that costs a full re-setup;
	 * painting every confirm red made them all look equally scary. */
	lv_obj_t *yes = mk_btn(confirm, "Yes, do it",
			       act_is_danger(pending) ? COL_RED : COL_GREEN,
			       confirm_yes_cb, NULL);

	lv_obj_align(yes, LV_ALIGN_BOTTOM_MID, 0, -70);

	lv_obj_t *no = mk_btn(confirm, "Cancel", COL_TRACK,
			      confirm_no_cb, NULL);

	lv_obj_align(no, LV_ALIGN_BOTTOM_MID, 0, -18);
}

static void act_cb(lv_event_t *e)
{
	pending = (enum action)(intptr_t)lv_event_get_user_data(e);
	show_confirm();
}
#endif /* CONFIG_BLINK_WIFI_MODE */

/* --- Software update: tile state machine, confirm, progress overlay --- */

static void notice_ok_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_obj_del(notice);
	notice = NULL;
}

void ui_settings_notice_dismiss(void)
{
	if (notice) {
		lv_obj_del(notice);
		notice = NULL;
	}
}

void ui_settings_notice(const char *txt)
{
	if (notice) {
		lv_obj_del(notice);
	}
	/*
	 * Sized by its text, not fixed. A 130 px box held three lines; the
	 * fourth ran under the OK button (an OTA error message did exactly
	 * that on the desk, 2026-08-29). A flex column puts the button below
	 * whatever the label needs, and the height follows -- clamped to the
	 * screen, with the label scrolling inside in the absurd case.
	 */
	notice = lv_obj_create(lv_layer_top());
	lv_obj_set_width(notice, 300);
	lv_obj_set_height(notice, LV_SIZE_CONTENT);
	lv_obj_set_style_min_height(notice, 110, 0);
	lv_obj_set_style_max_height(notice, 230, 0);
	lv_obj_set_style_pad_all(notice, 12, 0);
	lv_obj_set_style_pad_row(notice, 12, 0);
	lv_obj_set_style_bg_color(notice, COL_BG, 0);
	lv_obj_set_style_bg_opa(notice, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(notice, COL_TRACK, 0);
	lv_obj_set_style_border_width(notice, 1, 0);
	lv_obj_clear_flag(notice, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_flex_flow(notice, LV_FLEX_FLOW_COLUMN);
	lv_obj_set_flex_align(notice, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER,
			      LV_FLEX_ALIGN_CENTER);

	lv_obj_t *l = lv_label_create(notice);

	lv_label_set_text(l, txt);
	lv_obj_set_width(l, 270);
	lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);

	lv_obj_t *ok = mk_btn(notice, "OK", COL_TRACK, notice_ok_cb, NULL);

	lv_obj_set_size(ok, 120, 36);
	/* Centre once the content has a size; before layout it is 0 x 0. */
	lv_obj_update_layout(notice);
	lv_obj_center(notice);
}

/*
 * Boot-time "an update is waiting" prompt.
 *
 * Lives on lv_layer_top() like the notice, NOT inside the settings panel:
 * it has to appear on the gauge screen with nothing else open, which is
 * exactly where show_install_confirm() cannot go (that one is a child of
 * `panel`). Shown at most once per boot -- an update the user answered
 * "Later" to must not re-ask on every OTA state tick.
 */
static lv_obj_t *upd_prompt;
static bool upd_prompt_done;

static void upd_prompt_close(void)
{
	if (upd_prompt) {
		lv_obj_del(upd_prompt);
		upd_prompt = NULL;
	}
}

static void upd_prompt_later_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	upd_prompt_close();
}

static void upd_prompt_now_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	upd_prompt_close();
	/* The download overlay is screen-level and upd_timer now runs whether
	 * or not the panel is open, so the bar appears without going through
	 * settings. */
	ota_request_install();
}

static void upd_prompt_show(const struct ota_ui *snap)
{
	/* Never stack on top of something the user is already answering. */
	if (upd_prompt_done || upd_prompt || panel || confirm || notice ||
	    dl_overlay) {
		return;
	}
	upd_prompt_done = true;

	upd_prompt = lv_obj_create(lv_layer_top());
	lv_obj_set_size(upd_prompt, 300, 130);
	lv_obj_set_style_bg_color(upd_prompt, COL_BG, 0);
	lv_obj_set_style_bg_opa(upd_prompt, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(upd_prompt, COL_GREEN, 0);
	lv_obj_set_style_border_width(upd_prompt, 1, 0);
	/* Zero the container padding before laying anything out. lv_obj_create
	 * inherits the theme's default pad, which shrinks the usable width --
	 * two 130 px buttons at +/-12 px from the edges then did not fit inside
	 * it and overlapped (user-reported 2026-08-20). */
	lv_obj_set_style_pad_all(upd_prompt, 0, 0);
	lv_obj_clear_flag(upd_prompt, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(upd_prompt);

	lv_obj_t *l = lv_label_create(upd_prompt);

	if (proto_ota_app_version()[0]) {
		/* Both halves in one tap. Saying so is not a detail: the app on
		 * the computer restarts as part of this, and someone watching
		 * the gauge should not have to guess why. */
		lv_label_set_text_fmt(l, "Version %s is available.\n"
				      "This also updates the app on your "
				      "computer.", snap->version);
	} else {
		lv_label_set_text_fmt(l, "Version %s is available.",
				      snap->version);
	}
	lv_obj_set_width(l, 270);
	lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(l, LV_ALIGN_TOP_MID, 0, 10);

	/*
	 * Placed from the CENTRE, not from the edges: +/-70 either side of the
	 * midline puts two 132 px buttons at x 14..146 and 154..286 inside the
	 * 300 px popup, an 8 px gap between them and 14 px to each edge -- and
	 * it stays symmetric whatever the container padding turns out to be,
	 * which edge-relative offsets did not.
	 */
	lv_obj_t *yes = mk_btn(upd_prompt, "Update now", COL_GREEN,
			       upd_prompt_now_cb, NULL);

	lv_obj_set_size(yes, 132, 36);
	lv_obj_align(yes, LV_ALIGN_BOTTOM_MID, -70, -10);

	lv_obj_t *no = mk_btn(upd_prompt, "Later", COL_TRACK,
			      upd_prompt_later_cb, NULL);

	lv_obj_set_size(no, 132, 36);
	lv_obj_align(no, LV_ALIGN_BOTTOM_MID, 70, -10);
}

static void install_yes_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	ota_request_install();
	lv_obj_del(confirm);
	confirm = NULL;
}

static void show_install_confirm(const struct ota_ui *snap)
{
	confirm = lv_obj_create(panel);
	lv_obj_set_size(confirm, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(confirm, COL_BG, 0);
	lv_obj_set_style_bg_opa(confirm, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(confirm, 0, 0);
	lv_obj_set_style_pad_all(confirm, 0, 0);
	lv_obj_clear_flag(confirm, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(confirm);

	lv_obj_t *q = lv_label_create(confirm);

	lv_label_set_text_fmt(q, "Install version %s?\nThe screen restarts when done.",
			      snap->version);
	lv_obj_set_style_text_color(q, COL_TEXT, 0);
	lv_obj_set_style_text_align(q, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(q, LV_ALIGN_TOP_MID, 0, 40);

	lv_obj_t *yes = mk_btn(confirm, "Yes, do it", COL_GREEN,
			       install_yes_cb, NULL);

	lv_obj_align(yes, LV_ALIGN_BOTTOM_MID, 0, -70);

	lv_obj_t *no = mk_btn(confirm, "Cancel", COL_TRACK,
			      confirm_no_cb, NULL);

	lv_obj_align(no, LV_ALIGN_BOTTOM_MID, 0, -18);
}

static void upd_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	struct ota_ui snap;

	ota_ui_get(&snap);
	if (snap.st == OTA_UI_IDLE || snap.st == OTA_UI_UP_TO_DATE) {
		ota_request_check();
	} else if (snap.st == OTA_UI_AVAILABLE && confirm == NULL) {
		show_install_confirm(&snap);
	}
}

/* Children go with the parent; null the handles so a later show() rebuilds
 * them instead of writing through dangling pointers. */
static void dl_overlay_hide(void)
{
	if (!dl_overlay) {
		return;
	}
	lv_obj_del(dl_overlay);
	dl_overlay = NULL;
	dl_lbl = NULL;
	dl_bar = NULL;
	dl_sub = NULL;
	dl_src = NULL;
}

static void dl_overlay_show(const struct ota_ui *snap, bool rebooting)
{
	if (!dl_overlay) {
		/* Full-screen and clickable: it swallows every touch, so
		 * nothing underneath (panel close included) can run. Only
		 * the reboot removes it. */
		dl_overlay = lv_obj_create(lv_layer_top());
		lv_obj_set_size(dl_overlay, LV_PCT(100), LV_PCT(100));
		lv_obj_set_style_bg_color(dl_overlay, COL_BG, 0);
		lv_obj_set_style_bg_opa(dl_overlay, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(dl_overlay, 0, 0);
		lv_obj_set_style_radius(dl_overlay, 0, 0);
		lv_obj_clear_flag(dl_overlay, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_center(dl_overlay);

		dl_lbl = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_lbl, COL_TEXT, 0);
		lv_obj_align(dl_lbl, LV_ALIGN_CENTER, 0, -30);

		/* Which link this is arriving on. Serial tops out near 11 KB/s
		 * against WiFi's hundreds, so a USB update legitimately takes
		 * minutes -- saying so up front is the difference between
		 * "slow" and "stuck" (user request 2026-08-20). */
		dl_src = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_src, COL_DIM, 0);
		lv_obj_align(dl_src, LV_ALIGN_CENTER, 0, -8);

		dl_bar = lv_bar_create(dl_overlay);
		lv_obj_set_size(dl_bar, 260, 12);
		lv_bar_set_range(dl_bar, 0, 100);
		lv_obj_set_style_bg_color(dl_bar, COL_TRACK, 0);
		lv_obj_set_style_bg_color(dl_bar, COL_GREEN, LV_PART_INDICATOR);
		lv_obj_align(dl_bar, LV_ALIGN_CENTER, 0, 10);

		dl_sub = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_sub, COL_DIM, 0);
		/* Bounded and wrapping, not free-running. A single centred line
		 * runs off both edges of a 320 px panel at about 45 characters,
		 * which is how "This takes a few minutes. Keep the device
		 * powered." ended up hanging off the screen (user-reported
		 * 2026-07-26 -- the second overflow of the evening, after the
		 * update row). Constraining the label means future copy wraps
		 * instead of silently escaping. */
		lv_obj_set_width(dl_sub, 280);
		lv_label_set_long_mode(dl_sub, LV_LABEL_LONG_WRAP);
		lv_obj_set_style_text_align(dl_sub, LV_TEXT_ALIGN_CENTER, 0);
		lv_obj_align(dl_sub, LV_ALIGN_CENTER, 0, 34);
		lv_label_set_text(dl_sub, "");

		dl_first_ms = 0;
		dl_last_ms = 0;
		dl_last_pct = 0;
		dl_mspp = 0;
		dl_samples = 0;
		dl_eta_ms = 0;
		dl_eta_at_ms = 0;
		dl_shown_left = -1;
	}
	if (rebooting) {
		/* NOT "Restarting...": this frame is the LAST thing the panel
		 * draws, and it stays frozen on screen for as long as MCUboot
		 * spends swapping slot1 into slot0 -- nothing repaints until the
		 * new image boots. Claiming a restart that visibly is not
		 * happening reads as a hang (user-reported 2026-07-25).
		 *
		 * "A few minutes", not a number. Measured host-clock swap times
		 * for a 1.28 MB image: 357 s on the pre-2026-07-26 bootloader,
		 * 179 s after the sector-sized copy buffer landed (0.4.6), and
		 * 121 s once the encrypted 0xFF fill was deferred (0.4.7). The
		 * spread is the whole point of staying vague. The app cannot
		 * tell which bootloader it is running on -- MCUboot is not
		 * delivered over the air, so an OTA'd image may sit on either --
		 * and a device that promises "3 minutes" then takes six has
		 * recreated the exact "is it stuck?" problem this text exists to
		 * prevent. Vague and true beats precise and wrong.
		 *
		 * Do NOT re-derive this from MCUboot's own log timestamps: they
		 * read 17.3 s and 0.9 s for those same two swaps. */
		lv_label_set_text(dl_lbl, "Installing update...");
		lv_label_set_text(dl_src, "");
		lv_label_set_text(dl_sub,
				  "This takes a few minutes.\nKeep the device powered.");
		lv_bar_set_value(dl_bar, 100, LV_ANIM_OFF);
		return;
	}

	if (ota_ui_source() == OTA_SRC_USB) {
		/*
		 * Over USB the daemon drives esptool and the board is not part
		 * of the transfer at all -- it cannot see a byte count, so
		 * there is no honest percentage to show. A bar frozen at 0 for
		 * a minute reads as a hang, which is the exact failure the
		 * "Installing update..." wording elsewhere in this file exists
		 * to avoid, so hide it and say what is actually happening.
		 */
		lv_label_set_text_fmt(dl_lbl, "Updating to %s...", snap->version);
		lv_label_set_text(dl_src, "Over the USB cable");
		lv_obj_add_flag(dl_bar, LV_OBJ_FLAG_HIDDEN);
		/* Warn about the blackout explicitly. esptool resets the board
		 * into the ROM loader to write it, so the panel stops being
		 * driven at all and simply goes dark for the duration -- with
		 * no warning that reads as a crash, not an update. */
		/* Back to two. It was raised to four for a second esptool pass
		 * that read the image back off the chip -- a check write_flash
		 * had already performed by MD5, so the two minutes bought
		 * nothing and are gone again. Vague and true beats precise and
		 * wrong, and both halves of that apply to being too pessimistic. */
		lv_label_set_text(dl_sub,
				  "The screen goes dark for about 2 minutes.\n"
				  "Keep the cable connected.");
		return;
	}

	lv_obj_clear_flag(dl_bar, LV_OBJ_FLAG_HIDDEN);
	lv_label_set_text_fmt(dl_lbl, "Downloading version %s...", snap->version);
	lv_label_set_text(dl_src, "Over WiFi");
	lv_bar_set_value(dl_bar, snap->pct, LV_ANIM_OFF);

	int64_t now = k_uptime_get();

	if (snap->pct == 0) {
		return;			/* no body bytes yet */
	}
	if (dl_last_ms == 0) {		/* first percent: start the clock */
		dl_first_ms = now;
		dl_last_ms = now;
		dl_last_pct = snap->pct;
		return;
	}

	if (snap->pct > dl_last_pct) {
		int dp = snap->pct - dl_last_pct;
		int inst = (int)((now - dl_last_ms) / dp);

		/* EMA, alpha 1/4. Slow enough to ride out a stalled window,
		 * quick enough to track a genuine rate change. */
		dl_mspp = dl_mspp ? (dl_mspp * 3 + inst) / 4 : inst;
		dl_last_pct = snap->pct;
		dl_last_ms = now;
		dl_samples++;

		dl_eta_ms = (int64_t)dl_mspp * (100 - snap->pct);
		dl_eta_at_ms = now;
	}

	/* Warm-up: say nothing rather than something wrong. The bar is already
	 * showing that work is happening. */
	if (dl_samples < 4 || now - dl_first_ms < 5000) {
		return;
	}

	int64_t remain = dl_eta_ms - (now - dl_eta_at_ms);
	int left = remain > 0 ? (int)(remain / 1000) : 0;

	/* Coarser buckets further out: nobody needs 1 s resolution on a
	 * two-minute estimate, and the quantisation is what kills the twitch. */
	if (left > 90) {
		left = (left + 5) / 10 * 10;
	} else if (left > 30) {
		left = (left + 2) / 5 * 5;
	}

	if (left <= 5) {
		if (dl_shown_left != 0) {
			dl_shown_left = 0;
			lv_label_set_text(dl_sub, "Almost done...");
		}
		return;
	}

	/* Let it fall freely but resist rising: an estimate that keeps growing
	 * feels broken even when it is honest, so only admit a slowdown once it
	 * is unmistakable (>20% worse than what is on screen). */
	if (dl_shown_left > 0 && left > dl_shown_left) {
		int slack = dl_shown_left / 5 + 3;

		if (left - dl_shown_left < slack) {
			return;
		}
	}
	if (left == dl_shown_left) {
		return;
	}
	dl_shown_left = left;
	if (left >= 60) {
		lv_label_set_text_fmt(dl_sub, "About %d min %d s left",
				      left / 60, left % 60);
	} else {
		lv_label_set_text_fmt(dl_sub, "About %d s left", left);
	}
}

/* Paint one of upd_row.h's answers. The words and the colour live together
 * so a state cannot pick up the wrong pairing -- which is how "Up to date"
 * came to be drawn in green over an app that was a release behind. The label
 * budget is ~13 characters (see OTA_UI_AVAILABLE below). */
static void set_upd_row(lv_obj_t *lbl, enum upd_row row)
{
	switch (row) {
	case UPD_ROW_READY:
		lv_label_set_text(lbl, "Update ready");
		lv_obj_set_style_text_color(lbl, COL_GREEN, 0);
		break;
	case UPD_ROW_APP_OLD:
		/* Amber, not red: nothing is broken, the figures keep
		 * flowing. It is the one thing this screen cannot fix. */
		lv_label_set_text(lbl, "App is old");
		lv_obj_set_style_text_color(lbl, COL_AMBER, 0);
		break;
	case UPD_ROW_UP_TO_DATE:
		lv_label_set_text(lbl, "Up to date");
		lv_obj_set_style_text_color(lbl, COL_GREEN, 0);
		break;
	case UPD_ROW_BLANK:
	default:
		lv_label_set_text(lbl, "");
		lv_obj_set_style_text_color(lbl, COL_DIM, 0);
		break;
	}
}

static void upd_timer_cb(lv_timer_t *t)
{
	ARG_UNUSED(t);
	struct ota_ui snap;

	ota_ui_get(&snap);

	/*
	 * Screen-level state first, and unconditionally.
	 *
	 * This timer runs for the whole session now, not only while the panel
	 * is open, so an install started from the boot prompt still gets its
	 * progress bar and its outcome popup -- both of those live on
	 * lv_layer_top() and never needed the panel. Everything touching
	 * upd_lbl/upd_btn is fenced below instead: those die with the panel,
	 * and writing through them once it is shut is a NULL deref.
	 */
	if (snap.st != OTA_UI_DOWNLOADING && snap.st != OTA_UI_REBOOTING) {
		dl_overlay_hide();
	}

	switch (snap.st) {
	case OTA_UI_DOWNLOADING:
		dl_overlay_show(&snap, false);
		if (ota_ui_source() == OTA_SRC_USB) {
			if (upd_seen != OTA_UI_DOWNLOADING) {
				usb_dl_deadline = k_uptime_get() +
						  USB_DL_DEADLINE_MS;
			} else if (k_uptime_get() > usb_dl_deadline) {
				ota_ui_set(OTA_UI_FAILED, NULL, 0);
				ota_ui_set_error("the computer stopped responding");
			}
		}
		break;
	case OTA_UI_REBOOTING:
		dl_overlay_show(&snap, true);
		break;
	case OTA_UI_AVAILABLE:
		upd_prompt_show(&snap);
		break;
	case OTA_UI_UP_TO_DATE:
		/* Bounce back to IDLE even with the panel shut, so a stale
		 * "Up to date" cannot greet the next open. */
		if (upd_seen != OTA_UI_UP_TO_DATE) {
			upd_revert_at = k_uptime_get() + 3000;
		} else if (k_uptime_get() > upd_revert_at) {
			ota_ui_set(OTA_UI_IDLE, NULL, 0);
		}
		break;
	case OTA_UI_FAILED:
		if (upd_seen != OTA_UI_FAILED) {
			const char *base = upd_seen == OTA_UI_DOWNLOADING ?
				"Update failed. The current version keeps running." :
				"Couldn't check for updates.";

			/* Say which failure it was when we know. "Update
			 * failed" alone is true of a hash that did not match,
			 * a release that never downloaded and a chip we
			 * refuse to write, and the three want different
			 * answers from whoever is standing there. */
			if (snap.err[0]) {
				static char msg[160];
				/* The reasons arrive terse and lowercase --
				 * "sha256 mismatch" -- because they are also
				 * log lines. On screen they are a sentence. */
				int n = snprintf(msg, sizeof(msg), "%s\n", base);

				if (n > 0 && n < (int)sizeof(msg) - 1) {
					snprintf(msg + n, sizeof(msg) - n, "%s",
						 snap.err);
					if (msg[n] >= 'a' && msg[n] <= 'z') {
						msg[n] -= 'a' - 'A';
					}
				}
				ui_settings_notice(msg);
			} else {
				ui_settings_notice(base);
			}
			ota_ui_set(OTA_UI_IDLE, NULL, 0);
		}
		break;
	default:
		break;
	}

	if (upd_lbl == NULL) {		/* panel shut: no row to update */
		upd_seen = snap.st;
		return;
	}

	switch (snap.st) {
	case OTA_UI_IDLE:
		/* Left label + chevron already say "Software update"; the right
		 * side is a STATUS, blank until there's something to report, so
		 * it can't crowd the title. A pending badge is the exception.
		 *
		 * So is an out-of-date app: the board can update itself from
		 * here, but the half running on the customer's computer cannot
		 * be reached from this screen at all, so this is the only place
		 * it can be said. Amber, not green -- nothing is broken.
		 * Budget is ~13 characters (see OTA_UI_AVAILABLE below). */
		set_upd_row(upd_lbl, upd_row_idle(ota_badge(),
						  proto_host_outdated()));
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_CHECKING:
		lv_label_set_text(upd_lbl, "Checking...");
		lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
		lv_obj_add_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_UP_TO_DATE:
		/* "No newer firmware" is not "this product is current". See
		 * upd_row.h: saying so in green over an app a release behind
		 * is what sent a customer round in a circle -- the row said
		 * the app was old, the tap said everything was fine. */
		set_upd_row(upd_lbl, upd_row_checked(proto_host_outdated()));
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_AVAILABLE:
		/* Version only: this row shares 296 px with "Software update" on
		 * the left, which leaves ~13 characters here before the two
		 * collide (the size string made it 23 and they overlapped --
		 * user-reported 2026-07-25). Keep any future state text within
		 * the same budget as "Update ready". */
		lv_label_set_text_fmt(upd_lbl, "Install %s", snap.version);
		lv_obj_set_style_text_color(upd_lbl, COL_GREEN, 0);
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_FAILED:
		lv_label_set_text(upd_lbl, "");
		lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	default:
		break;
	}
	upd_seen = snap.st;
}

/*
 * The panel slides in from the right and back out again (user request
 * 2026-07-17) -- the motion says where settings lives and which way leads
 * home. The WHOLE screen moves, and does so without LVGL redrawing it.
 *
 * Two earlier attempts are worth recording, because both look like tuning
 * problems and are not.
 *
 * Sliding it with LVGL costs a full redraw per frame -- ~124 ms here, so a
 * 400 ms transition rendered about three frames and read as a stutter. Note it
 * is the object's SIZE that governs that, not how it is painted:
 * lv_obj_invalidate() works off lv_obj_get_coords() and never looks at bg_opa.
 *
 * Travelling as a header-height bar and expanding on arrival did fix the
 * framerate -- measured ~14 ms a frame against ~124 -- but a bar sliding while
 * the rest of the screen appears from nowhere is incoherent motion, and being
 * smooth does not redeem it (user-reported 2026-08-17, twice). Anything
 * between the two only trades one complaint for the other: cost scales with
 * the area that moves, so "more of the screen" and "more frames" are one dial
 * pulled in opposite directions.
 *
 * The way out is not to redraw at all. See ui_slide.c -- the panel's own
 * scroll register moves the image already sitting in its GRAM, so a transition
 * costs ONE full render spread across forty steps instead of one per frame.
 *
 * The consequence here is that open and close cannot run where they are asked
 * for. ui_slide_run() drives lv_refr_now() itself, and re-entering the refresh
 * from inside lv_timer_handler() is not safe, so the gesture handlers only
 * raise a flag and the mode loop runs the transition from thread context --
 * exactly as ui_anim does for the clip.
 */
static bool closing;
static volatile bool want_open;
/*
 * A pending provider-page change, -1 or +1, 0 for none. Same reason want_open
 * exists: the page change is a wipe transition now, and ui_slide_run() must
 * not be entered from inside an LVGL event callback.
 *
 * Only the LAST direction is kept rather than a queue. Two swipes delivered
 * during a transition (input is not dispatched for its length, so they arrive
 * as a burst afterwards) should land the user one page further, not replay a
 * second 650 ms transition for a page they have already left.
 */
static volatile int want_page;
static volatile bool want_close;

#if !IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
/* The shipped panel's furniture: a back control, a list row, and the
 * brightness screen. The WiFi layout above predates all three and keeps
 * its own card-and-tiles arrangement, which is the only thing five
 * actions fit into. */
/*
 * The back control.
 *
 * It was 40 x 26 drawn with a 12 px extended touch area -- 54 x 40 reachable
 * once the part hanging off the screen edges is discounted, which is
 * 9.6 x 7.1 mm on this panel. The width was never the problem; 7.1 mm of
 * height is barely over the point where a resistive panel starts guessing, and
 * the DRAWN button was 40 x 26, so it looked like something you were not meant
 * to press even where it answered.
 *
 * Now 60 x 36 drawn inside a 40 px bar, with 10 px of extended area: 72 x 48
 * reachable, 12.8 x 8.5 mm. It stops exactly where the first row begins, so
 * the two never contend for a press.
 */
static lv_obj_t *mk_back(lv_obj_t *parent, lv_event_cb_t cb)
{
	lv_obj_t *b = lv_btn_create(parent);

	lv_obj_set_size(b, 60, 36);
	lv_obj_align(b, LV_ALIGN_TOP_LEFT, 6, 2);
	/*
	 * Unbordered, and it keeps the 60 x 36 hit target anyway.
	 *
	 * The border was there to make the control announce itself at 40 x 26,
	 * where an unmarked chevron read as a label. The SIZE is what fixed
	 * that -- 72 x 48 reachable, 12.8 x 8.5 mm -- and once it was big
	 * enough the box around it was a second answer to a solved problem,
	 * sitting in the one corner of the panel that should be quietest
	 * (user request 2026-08-27). The filled ground stays: it is what
	 * separates the chevron from the title rule behind it.
	 */
	lv_obj_set_style_bg_color(b, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(b, 0, 0);
	lv_obj_set_style_radius(b, 9, 0);
	lv_obj_set_style_shadow_width(b, 0, 0);
	lv_obj_set_style_pad_all(b, 0, 0);
	lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
	/* Gestures MUST bubble from here: the extended area covers the seam and
	 * a strip of bare panel, and whatever the hit test lands on becomes the
	 * gesture's origin -- with bubbling off, a swipe starting in that region
	 * died on this button instead of closing the panel. */
	lv_obj_add_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_set_ext_click_area(b, 10);
	lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *l = lv_label_create(b);

	lv_label_set_text(l, LV_SYMBOL_LEFT);
	lv_obj_set_style_text_color(l, COL_DIM, 0);
	lv_obj_center(l);
	return b;
}

/*
 * A list row: 296 x 56, which is 52.7 x 10.0 mm on a 2.8" 320x240 panel
 * (5.62 px/mm). A fingertip covers about 9 mm, so this is the smallest a row
 * can be and still be pressed on purpose rather than on average.
 *
 * Two lines, because the height is there either way and each subtitle earns
 * it: the level without opening brightness, both version numbers without
 * opening the updater, and what a factory reset does before you commit to
 * finding out.
 */
/* `h` is a parameter because the number of rows decides it: the panel's usable
 * height is shared out among however many there are, so a row is as tall as it
 * can afford to be rather than a fixed 56. See the layout note at the call
 * site. Labels align to the row's middle, so they follow whatever height it
 * is given. */
static lv_obj_t *mk_row(lv_obj_t *parent, int y, int h, const char *title,
			const char *sub, bool danger, lv_event_cb_t cb,
			void *user, lv_obj_t **sub_out)
{
	lv_color_t fg = danger ? COL_RED : COL_TEXT;
	lv_obj_t *row = lv_btn_create(parent);

	lv_obj_set_size(row, 296, h);
	lv_obj_align(row, LV_ALIGN_TOP_LEFT, 12, y);
	lv_obj_set_style_bg_color(row, danger ? COL_DANGER_BG : COL_PANEL, 0);
	lv_obj_set_style_bg_opa(row, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(row, danger ? COL_DANGER_BD : COL_TRACK, 0);
	lv_obj_set_style_border_width(row, 1, 0);
	lv_obj_set_style_radius(row, 10, 0);
	lv_obj_set_style_shadow_width(row, 0, 0);
	lv_obj_set_style_pad_all(row, 0, 0);
	/* Both flags off, as everywhere else on this panel: a horizontal drag
	 * on a SCROLLABLE button is eaten as a scroll and the tap never fires,
	 * and a press that drifts must not bubble into a panel close. */
	lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(row, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(row, cb, LV_EVENT_CLICKED, user);

	lv_obj_t *t = lv_label_create(row);

	lv_label_set_text(t, title);
	lv_obj_set_style_text_color(t, fg, 0);
	/* A NULL subtitle is a one-line row, and the title then sits on the
	 * row's own middle rather than 11 px above a line that is not there.
	 * The update row is the only one: its version line moved out to the
	 * footer, where it belongs to the panel and not to a button. */
	lv_obj_align(t, LV_ALIGN_LEFT_MID, 14, sub ? -11 : 0);

	lv_obj_t *sl = NULL;

	if (sub != NULL) {
		sl = lv_label_create(row);
		lv_label_set_text(sl, sub);
		lv_obj_set_style_text_color(sl, COL_DIM, 0);
		/* Width-bounded and dotted. A centred line escapes this 320 px
		 * panel around 45 characters, and two overflow bugs have
		 * shipped that way. */
		lv_obj_set_width(sl, 236);
		lv_label_set_long_mode(sl, LV_LABEL_LONG_DOT);
		lv_obj_align(sl, LV_ALIGN_LEFT_MID, 14, 11);
	}

	lv_obj_t *chev = lv_label_create(row);

	lv_label_set_text(chev, LV_SYMBOL_RIGHT);
	lv_obj_set_style_text_color(chev, danger ? COL_RED : COL_DIM, 0);
	lv_obj_align(chev, LV_ALIGN_RIGHT_MID, -12, 0);

	if (sub_out) {
		*sub_out = sl;
	}
	return row;
}

/* --- The brightness screen ------------------------------------------- */

static void bright_close(void)
{
	if (bright_ov == NULL) {
		return;
	}
	/* Forget the children BEFORE deleting the parent: bright_refresh() can
	 * be called from a step at any time and writes through both. */
	bright_big = NULL;
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		seg[i] = NULL;
	}
	lv_obj_del(bright_ov);
	bright_ov = NULL;
	bright_refresh();	/* the row's subtitle survives and needs the number */
}

static void bright_close_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	bright_close();
}

static void bright_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* The same swipe that closes the panel closes this first. Without it
	 * the gesture would bubble past an overlay that is covering the panel
	 * and shut the whole thing, which is not what the motion looks like it
	 * should do from here. */
	if (lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		bright_close();
	}
}

static void bright_stop_cb(lv_event_t *e)
{
	backlight_set((uint8_t)(intptr_t)lv_event_get_user_data(e));
	bright_refresh();
}

static void show_bright(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (bright_ov || confirm) {
		return;
	}
	bright_ov = lv_obj_create(panel);
	lv_obj_set_size(bright_ov, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(bright_ov, COL_BG, 0);
	lv_obj_set_style_bg_opa(bright_ov, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(bright_ov, 0, 0);
	lv_obj_set_style_radius(bright_ov, 0, 0);
	lv_obj_set_style_pad_all(bright_ov, 0, 0);
	lv_obj_clear_flag(bright_ov, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(bright_ov, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(bright_ov, bright_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_center(bright_ov);

	mk_back(bright_ov, bright_close_cb);

	lv_obj_t *title = lv_label_create(bright_ov);

	lv_label_set_text(title, "Brightness");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

	mk_line(bright_ov, 40);

	bright_big = lv_label_create(bright_ov);
	lv_obj_set_style_text_font(bright_big, &lv_font_montserrat_20, 0);
	lv_obj_set_style_text_color(bright_big, COL_TEXT, 0);
	lv_obj_align(bright_big, LV_ALIGN_TOP_MID, 0, 50);

	/* Five stops, 56 x 116 -- 10.0 x 20.6 mm. The whole screen is spent on
	 * them because this is the one control that gets used while the panel
	 * is too dim to read. */
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		uint8_t lvl = 20 + i * 20;

		seg[i] = lv_btn_create(bright_ov);
		lv_obj_set_size(seg[i], 56, 116);
		lv_obj_align(seg[i], LV_ALIGN_TOP_LEFT, 12 + i * 60, 86);
		lv_obj_set_style_bg_opa(seg[i], LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(seg[i], 0, 0);
		lv_obj_set_style_radius(seg[i], 8, 0);
		lv_obj_set_style_shadow_width(seg[i], 0, 0);
		lv_obj_set_style_pad_all(seg[i], 0, 0);
		lv_obj_clear_flag(seg[i], LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_clear_flag(seg[i], LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_add_event_cb(seg[i], bright_stop_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)lvl);
	}

	lv_obj_t *hint = lv_label_create(bright_ov);

	lv_label_set_text(hint, "Tap a level");
	lv_obj_set_style_text_color(hint, COL_DIM, 0);
	lv_obj_align(hint, LV_ALIGN_TOP_MID, 0, 210);

	bright_refresh();	/* paints the stops and the readout */
}
#endif /* !CONFIG_BLINK_WIFI_MODE */

static void build_panel(lv_obj_t *parent_scr);

/* Thread context. The tree surgery happens frozen: creating the panel would
 * otherwise invalidate the screen, and refreshing that invalidation would
 * repaint over the very gauge pixels the scroll is about to move away. */
static void do_open(void (*pump)(void))
{
	ui_slide_begin();
	/* The live screen, not one cached when the gesture landed. The cached
	 * pointer outlived its object on the failed-join path, where
	 * usage_view_deinit() deletes the gauge screen before provisioning. */
	build_panel(lv_screen_active());
	lv_obj_update_layout(panel);
	ui_slide_freeze(false);

	/*
	 * Mute across the transition, the way ui_anim does for the clip.
	 * ui_slide_run() blocks for the whole slide with no input dispatched
	 * (pump() does not run lv_timer_handler()), so every touch point of the
	 * opening swipe -- and any impatient second one -- is delivered in a
	 * burst afterwards, against a screen that has changed underneath it.
	 */
	ui_anim_gesture_mute(UI_SLIDE_MS * 6);

	/* Gauges exit left, settings enters from the right. */
	ui_slide_run(UI_SLIDE_LEFT, pump);
	ui_anim_gesture_mute(250);	/* short tail past the input burst */
}

/*
 * Thread context, and the shortest of the three transitions to set up: the
 * page change edits the widgets that are already on screen rather than
 * building or deleting a tree, so the "incoming screen" is the same objects
 * carrying the other provider's numbers.
 *
 * Frozen for the edit, exactly like do_open: render_gauges() invalidates
 * everything it retexts, and refreshing that would repaint the destination
 * over the whole screen at once -- leaving the wipe nothing to reveal.
 */
/*
 * The page change is not a transition any more.
 *
 * Three were tried on this axis. A cut did not read as a swipe. A wipe read as
 * a repaint, because the two pages are the same layout and the boundary
 * between them has nothing to be made of. A wipe with a bright leading edge
 * gave that boundary something to see and still did not feel natural -- which
 * it was not: a bar sweeping the panel is an object that exists nowhere else
 * on this device and means nothing when it arrives.
 *
 * All three were transitions between two PICTURES. This is an instrument, and
 * the motion that belongs to one is the needle moving. usage_view_page_step()
 * now animates the rings from the reading they were showing to the other
 * provider's, and nothing is covered or revealed at all. See the note above it.
 *
 * So this does not block, does not touch the display, does not need the strip
 * machinery, and does not need `pump` -- an LVGL animation runs from
 * lv_timer_handler like everything else on the screen. It stays a mode-loop
 * request rather than moving back into the swipe callback because the
 * can_page() re-check there still matters: a usage message can remove the
 * second provider in the gap between the swipe and this.
 */
static void do_page(int delta, void (*pump)(void))
{
	ARG_UNUSED(pump);

	usage_view_page_step(delta);

	/*
	 * NO MUTE. This used to swallow the next swipe for 250 ms and that is
	 * what "working sometimes but not sometimes" was: of five vertical
	 * strokes logged on 2026-08-28, five fired and only FOUR changed the
	 * page. The fifth landed inside the mute and was dropped in silence.
	 *
	 * The mute exists to absorb the tail of a swipe REPLAYED after a
	 * blocking transition -- input is not dispatched while ui_slide_run
	 * holds the display, so it arrives in a burst afterwards. Neither half
	 * of that is true here any more. A page change is an animation and
	 * blocks nothing, so no burst accumulates; and ui_swipe reads the
	 * panel directly rather than LVGL's queue, so there is no replay to
	 * absorb in the first place. It also fires at most once per stroke and
	 * needs a genuinely new one -- reports stopping for a whole re-arm
	 * window -- before it can fire again, which is the actual protection
	 * against a stroke counting twice.
	 *
	 * So the mute was guarding against something that cannot happen, at
	 * the cost of the thing people do most: page back and forth.
	 */
}


static void do_close(void (*pump)(void))
{
	/*
	 * Re-test the download guard here, not just in close_panel().
	 *
	 * close_panel() refuses while an overlay is up, but it now only LATCHES
	 * the request -- and the overlay can appear in the gap before this
	 * runs: the worker raises OTA_UI_DOWNLOADING on its own 250 ms tick and
	 * upd_timer_cb shows the overlay from the next lv_timer_handler, both
	 * of which can land after a swipe has already passed the guard.
	 *
	 * upd_timer_cb is the ONLY caller of dl_overlay_hide(). It used to die
	 * with the panel, which left a full-screen, opaque, touch-swallowing
	 * object orphaned on lv_layer_top() with nothing alive to remove it --
	 * the screen locked against every tap, user-reported 2026-07-25. The
	 * timer now outlives the panel (see upd_timer_cb), so that particular
	 * trap is gone; this guard stays because closing mid-download still
	 * tears down the panel under a live install for no gain.
	 */
	if (dl_overlay) {
		closing = false;
		return;
	}

	ui_slide_begin();
	/* upd_timer deliberately survives the panel -- see upd_timer_cb. It is
	 * created once in ui_settings_attach(). */
	upd_btn = NULL;		/* dies with the panel */
	upd_lbl = NULL;
	pct_lbl = NULL;
	bright_big = NULL;
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		seg[i] = NULL;
	}
	lv_obj_del(panel);	/* deletes confirm and any overlay with it */
	panel = NULL;
	confirm = NULL;
	bright_ov = NULL;
	lv_obj_update_layout(lv_screen_active());
	ui_slide_freeze(false);

	/*
	 * Same mute as do_open, and it matters more here: this uncovers the
	 * gauge screen, where a replayed RIGHT swipe is exactly what ASKS for
	 * the eye clip. Without it, closing settings could start the clip by
	 * itself.
	 */
	ui_anim_gesture_mute(UI_SLIDE_MS * 6);

	/* Settings exits right, the gauges come back in from the left. */
	ui_slide_run(UI_SLIDE_RIGHT, pump);
	ui_anim_gesture_mute(250);
	closing = false;
}

void ui_settings_drop_pending(void)
{
	want_open = false;
	want_close = false;
	want_page = 0;
	closing = false;
}

/*
 * Somebody is working this panel -- it is open, or a gesture has asked for
 * something and no mode loop has run it yet.
 *
 * The latch half is the part that is easy to get wrong. A gesture never opens
 * anything itself; it raises one of these flags and ui_settings_service(),
 * which only a mode loop calls, runs the transition from thread context. So
 * a latch is not a record of a past intention, it is a request still waiting
 * to be answered, and any code that takes the mode loop away from it -- the
 * doze in ui_sleep.c does exactly that -- has to know the request is there.
 * Otherwise the swipe does nothing, and then does something worse: the flag
 * sits until the board next wakes on its own and the panel slides open by
 * itself, hours later, in front of nobody.
 */
bool ui_settings_busy(void)
{
	return panel != NULL || want_open || want_close || want_page != 0;
}

void ui_settings_service(void (*pump)(void))
{
	if (want_open && panel == NULL) {
		want_open = false;
		do_open(pump);
	} else if (want_close && panel != NULL) {
		want_close = false;
		do_close(pump);
	} else if (want_page != 0 && panel == NULL) {
		/*
		 * Last, and only with the panel closed. A page change is the
		 * least important of the three, and re-checking can_page here
		 * matters: the flag was set when the gesture landed, and a
		 * usage message can have removed the second provider in the
		 * gap -- which would run a transition to a page that no longer
		 * exists.
		 */
		int step = want_page;

		want_page = 0;
		if (usage_view_can_page(step)) {
			do_page(step, pump);
		}
	}
	want_open = false;
	want_close = false;
	want_page = 0;
}

static void close_panel(void)
{
	if (closing || dl_overlay) {	/* no closing under a download */
		return;
	}
	closing = true;
	want_close = true;	/* serviced from the mode loop; see do_close */
}

static void back_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* bright_ov covers the panel and carries its own back control, so this
	 * one is unreachable while it is up -- but the extended touch area
	 * reaches past the overlay's edge, and a press landing there must not
	 * close the panel out from under it. */
	if (confirm == NULL && bright_ov == NULL) {
		close_panel();
	}
}

static void panel_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (confirm == NULL && bright_ov == NULL &&
	    lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		close_panel();
	}
}

static void build_panel(lv_obj_t *parent_scr)
{
	panel = lv_obj_create(parent_scr);
	lv_obj_set_size(panel, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(panel, COL_BG, 0);
	lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(panel, 0, 0);
	lv_obj_set_style_radius(panel, 0, 0);
	/* Kill the theme's default padding: it silently shifted every child
	 * down, and the footer line landed on the last button (seen on
	 * hardware 2026-07-17). */
	lv_obj_set_style_pad_all(panel, 0, 0);
	lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
	/* Do NOT bubble: with the default GESTURE_BUBBLE the panel's own swipes
	 * travelled up to the screen (whose handler ignores gestures while the
	 * panel is open), so swipe-to-close only ever worked by accident. Stop
	 * gestures here and let panel_gesture_cb below act on them. */
	lv_obj_clear_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(panel, panel_gesture_cb, LV_EVENT_GESTURE, NULL);
	/* Parked. Nothing animates the panel itself any more -- the whole
	 * screen moves under it, in ui_slide_run(). */
	lv_obj_set_pos(panel, 0, 0);

#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)
	/* --- Header: green edge seam + back chevron + title + rule. The
	 * chevron used to be a full-height LEFT_MID button that the reset tiles
	 * kept covering; it now lives in the top bar (still a left-edge cue via
	 * the seam). Tap or swipe-right both go home. */
	lv_obj_t *seam = lv_obj_create(panel);

	lv_obj_set_size(seam, 3, 30);
	lv_obj_align(seam, LV_ALIGN_TOP_LEFT, 0, 0);
	lv_obj_set_style_bg_color(seam, COL_GREEN, 0);
	lv_obj_set_style_bg_opa(seam, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(seam, 0, 0);
	lv_obj_set_style_radius(seam, 0, 0);
	lv_obj_clear_flag(seam, LV_OBJ_FLAG_SCROLLABLE);

	lv_obj_t *back = lv_btn_create(panel);

	/* Drawn narrow and short (x 2..42, y 2..28), no gesture-bubble: it used
	 * to be 48 wide and sat right on top of the brightness "-" stepper, so a
	 * high tap on "-" hit the chevron and bounced you home (user-reported
	 * 2026-07-20). The steppers below start at y=39 -- an 11px gap. */
	lv_obj_set_size(back, 40, 26);
	lv_obj_set_style_bg_opa(back, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(back, 0, 0);
	lv_obj_align(back, LV_ALIGN_TOP_LEFT, 2, 2);
	/* Gestures MUST bubble from here. The extended touch area below covers
	 * the green seam and a strip of bare panel, and whatever the hit test
	 * lands on becomes the gesture's origin -- so with bubbling off, a
	 * swipe-right starting anywhere in that region died on this button
	 * instead of reaching panel_gesture_cb, silently un-closing ~814 px of
	 * a panel whose own seam comment promises the swipe works.
	 * close_panel() is idempotent (its `closing` guard), so a release that
	 * also fires back_cb costs nothing. */
	lv_obj_add_flag(back, LV_OBJ_FLAG_GESTURE_BUBBLE);
	/*
	 * 40x26 is too small to hit reliably on a resistive panel that already
	 * needs 16 averaged reads per report -- reported as "not really
	 * clickable" 2026-07-27. Grow the TOUCH area without growing the drawn
	 * button: the chevron stays visually where it belongs in the top bar,
	 * but answers to a region 12 px larger on every side. That is 64x50 nominally;
	 * 10 px of it falls off the top and left edges of the screen, so the reachable
	 * area is 54x40.
	 *
	 * Safe against the 2026-07-20 regression even though the extended area
	 * now reaches into the stepper row: lv_indev hit-tests children last to
	 * first and takes the first hit, and "minus" is created after this, so
	 * it wins wherever the two overlap. Keep it that way -- moving this
	 * button's creation below the steppers would silently restore the bug.
	 */
	lv_obj_set_ext_click_area(back, 12);
	lv_obj_add_event_cb(back, back_cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *bl = lv_label_create(back);

	lv_label_set_text(bl, LV_SYMBOL_LEFT);
	lv_obj_set_style_text_color(bl, COL_DIM, 0);
	lv_obj_center(bl);

	lv_obj_t *title = lv_label_create(panel);

	lv_label_set_text(title, "Settings");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 6);

	mk_line(panel, 30);

	/* --- Brightness: card, big opposite-corner steppers, and a stacked
	 * "Brightness / 70%" readout. The pips are gone -- the number already
	 * says the level (user-reported the panel was too crowded). --- */
	mk_card(panel, 12, 34, 296, 50);

	/* Wide steppers filling the row to a 4px edge inset (minus x16..104, plus
	 * x216..304), leaving ~112px in the centre for the value/label. Centred
	 * in the 50px card (y39..79): the card runs y34..84, a 5px margin top and
	 * bottom. The back chevron ends at y28, so the 11px gap keeps a high tap
	 * on "-" off it even at the left edge. */
	lv_obj_t *minus = mk_btn(panel, LV_SYMBOL_MINUS, COL_TRACK,
				 bright_step_cb, (void *)(intptr_t)-1);
	lv_obj_set_size(minus, 88, 40);
	lv_obj_set_style_radius(minus, 8, 0);
	lv_obj_align(minus, LV_ALIGN_TOP_LEFT, 16, 39);

	lv_obj_t *plus = mk_btn(panel, LV_SYMBOL_PLUS, COL_TRACK,
				bright_step_cb, (void *)(intptr_t)1);
	lv_obj_set_size(plus, 88, 40);
	lv_obj_set_style_radius(plus, 8, 0);
	lv_obj_align(plus, LV_ALIGN_TOP_RIGHT, -16, 39);

	lv_obj_t *blab = lv_label_create(panel);

	lv_label_set_text(blab, "Brightness");
	lv_obj_set_style_text_color(blab, COL_DIM, 0);
	lv_obj_align(blab, LV_ALIGN_TOP_MID, 0, 40);

	pct_lbl = lv_label_create(panel);
	lv_obj_set_style_text_font(pct_lbl, &lv_font_montserrat_20, 0);
	lv_obj_set_style_text_color(pct_lbl, COL_TEXT, 0);
	lv_obj_align(pct_lbl, LV_ALIGN_TOP_MID, 0, 56);
	bright_refresh();

	/* --- Software update: its own row directly under brightness, fenced
	 * by rules so it never reads as a fourth reset action --- */
	mk_line(panel, 89);

	upd_btn = lv_btn_create(panel);
	lv_obj_set_size(upd_btn, 296, 30);
	lv_obj_align(upd_btn, LV_ALIGN_TOP_LEFT, 12, 94);
	lv_obj_set_style_bg_color(upd_btn, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(upd_btn, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(upd_btn, COL_TRACK, 0);
	lv_obj_set_style_border_width(upd_btn, 1, 0);
	lv_obj_set_style_radius(upd_btn, 9, 0);
	lv_obj_set_style_shadow_width(upd_btn, 0, 0);
	lv_obj_set_style_pad_all(upd_btn, 0, 0);
	/* Same as the other controls: don't scroll its (wide) contents and don't
	 * bubble drift-gestures into a close. A horizontal drag on a SCROLLABLE
	 * button gets eaten as a scroll and the tap never fires -- that is why
	 * this row felt dead (user-reported 2026-07-20). */
	lv_obj_clear_flag(upd_btn, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(upd_btn, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(upd_btn, upd_cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *swl = lv_label_create(upd_btn);

	lv_label_set_text(swl, "Software update");
	lv_obj_set_style_text_color(swl, COL_TEXT, 0);
	lv_obj_align(swl, LV_ALIGN_LEFT_MID, 12, 0);

	lv_obj_t *swchev = lv_label_create(upd_btn);

	lv_label_set_text(swchev, LV_SYMBOL_RIGHT);
	lv_obj_set_style_text_color(swchev, COL_DIM, 0);
	lv_obj_align(swchev, LV_ALIGN_RIGHT_MID, -10, 0);

	/* The right-hand label IS the OTA state readout (driven by
	 * upd_timer_cb); "Software update" on the left never changes. */
	upd_lbl = lv_label_create(upd_btn);
	lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
	lv_obj_align(upd_lbl, LV_ALIGN_RIGHT_MID, -26, 0);

	upd_timer_cb(NULL);	/* correct the row before the first tick */

	/* --- Reset actions --- */
	mk_line(panel, 129);

	static const char *const acticon[] = {
		[ACT_WIFI] = LV_SYMBOL_WIFI,
		[ACT_SIGNIN] = LV_SYMBOL_REFRESH,
		[ACT_FACTORY] = LV_SYMBOL_TRASH,
	};

	/* (already inside the WiFi-only branch that opens above) */
	/* Three actions: a centred heading over a row of three tiles.
	 *
	 * The labels are one word each because three of them share 296 px; the
	 * single-action layout below has the room to say "Factory reset" in
	 * full, and does. */
	static const char *const acttext[] = {
		[ACT_WIFI] = "Wi-Fi",
		[ACT_SIGNIN] = "Sign-in",
		[ACT_FACTORY] = "Factory",
	};

	lv_obj_t *rlab = lv_label_create(panel);

	lv_label_set_text(rlab, "RESET");
	lv_obj_set_style_text_color(rlab, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(rlab, 2, 0);
	lv_obj_align(rlab, LV_ALIGN_TOP_MID, 0, 134);

	static const enum action acts[] = { ACT_WIFI, ACT_SIGNIN, ACT_FACTORY };
	static const int tilex[] = { 12, 114, 216 };

	/* ARRAY_SIZE, not a literal 3. It WAS a literal, and when this build
	 * stopped shipping the radio the arrays shrank to one entry while the
	 * loop kept running three times -- reading acts[1..2] and tilex[1..2]
	 * off the end, using the garbage as an index into acticon/acttext, and
	 * handing whatever pointer came back to lv_label_set_text. Two extra
	 * tiles in junk positions with junk labels, which is what "the settings
	 * page looks weird" turned out to be. */
	for (unsigned int i = 0; i < ARRAY_SIZE(acts); i++) {
		enum action a = acts[i];
		bool danger = a == ACT_FACTORY;
		lv_obj_t *tile = lv_btn_create(panel);
		lv_color_t fg = danger ? COL_RED : COL_TEXT;

		lv_obj_set_size(tile, 92, 58);
		lv_obj_set_style_bg_color(tile,
			danger ? COL_DANGER_BG : COL_PANEL, 0);
		lv_obj_set_style_border_color(tile,
			danger ? COL_DANGER_BD : COL_TRACK, 0);
		lv_obj_set_style_border_width(tile, 1, 0);
		lv_obj_set_style_radius(tile, 12, 0);
		lv_obj_set_style_shadow_width(tile, 0, 0);
		/* Zero the theme's button padding: it shrank the 58px content box
		 * until the icon (top) and label (bottom) overprinted each other
		 * (user-reported 2026-07-20). And no gesture-bubble -> a drift on
		 * a tile can't close the panel. */
		lv_obj_set_style_pad_all(tile, 0, 0);
		lv_obj_clear_flag(tile, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_align(tile, LV_ALIGN_TOP_LEFT, tilex[i], 152);
		lv_obj_add_event_cb(tile, act_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)a);

		lv_obj_t *ic = lv_label_create(tile);

		lv_label_set_text(ic, acticon[a]);
		lv_obj_set_style_text_color(ic, fg, 0);
		lv_obj_align(ic, LV_ALIGN_TOP_MID, 0, 8);

		lv_obj_t *tx = lv_label_create(tile);

		lv_label_set_text(tx, acttext[a]);
		lv_obj_set_style_text_color(tx, fg, 0);
		lv_obj_align(tx, LV_ALIGN_BOTTOM_MID, 0, -8);
	}

	/* Debug-me line: build + network, answered without a serial cable
	 * (which would reset the board). IP dropped at the user's request
	 * (2026-07-20); the SSID gets more room, capped so it can't overrun. */
	char line[56];
	/* (already inside the WiFi-only branch that opens above) */
	char ip[16], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];

	/* (already inside the WiFi-only branch that opens above) */
	if (net_wifi_sta_ip(ip, sizeof(ip)) &&
	    cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk))) {
		char ssid_a[CFG_SSID_MAX];

		fmt_ascii(ssid, ssid_a, sizeof(ssid_a));
		snprintf(line, sizeof(line), "Blink %s  |  %.20s",
			 BLINK_FW_VERSION, ssid_a);
	} else
	{
		/* Both halves when the daemon has introduced itself: they ship
		 * from one tag, so the useful support question is which of the
		 * two is behind -- and the version of the half running on the
		 * customer's computer is otherwise invisible from here.
		 *
		 * Nested rather than chained onto the #if'd branch above: that
		 * `else` only exists in a WiFi build, and an `else if` here
		 * left the USB build with an else and no if. */
		if (proto_host_version()[0]) {
			snprintf(line, sizeof(line), "Blink %s  |  App %s",
				 BLINK_FW_VERSION, proto_host_version());
		} else {
			snprintf(line, sizeof(line), "Blink %s",
				 BLINK_FW_VERSION);
		}
	}

	lv_obj_t *info = lv_label_create(panel);

	lv_label_set_text(info, line);
	lv_obj_set_style_text_color(info, COL_DIM, 0);
	/* One line, dotted if it would run wider than the screen. Narrower than
	 * the screen so the dots land inside the right edge, not on it. */
	lv_obj_set_size(info, 296, 17);
	lv_label_set_long_mode(info, LV_LABEL_LONG_DOT);
	lv_obj_set_style_text_align(info, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(info, LV_ALIGN_BOTTOM_MID, 0, -2);
#else
	/*
	 * Two rows and nothing else.
	 *
	 * The panel is 240 px tall and a fingertip covers about 9 mm, which is
	 * 51 px at this panel's 5.62 px/mm. Take 40 for the title bar and 200
	 * is left. There were three rows at 56 px (10.0 mm) while a "Reset to
	 * defaults" row existed; it does not any more, because in this build
	 * the only thing it reset was brightness -- and the row directly above
	 * it sets brightness without a reboot.
	 *
	 * The freed height goes into the two that remain rather than being
	 * left as a hole under them: 72 px each, 12.8 mm, with 24 px of margin
	 * above and below the pair so it reads as centred instead of
	 * top-aligned with something missing.
	 *
	 * What this replaces was measured and did not pass: 30 px rows are
	 * 5.3 mm, sitting 5 px apart, so one press covered both and which one
	 * fired came down to where the pressure centroid landed.
	 */
/*
 * 56 px is 10.0 mm against a ~9 mm fingertip -- the smallest a row can be and
 * still be pressed on purpose rather than on average.
 *
 * TWO rows now, brightness and software update: the main-source row went when
 * the providers got a page each. The height it freed is not redistributed. It
 * goes to the footer, which is the version pair and the one instruction this
 * device needs to give -- both of them things you read rather than press, and
 * neither of which belongs inside a button.
 */
#define ROW_H 56
#define ROW_TOP 56		/* first row, clear of the title rule at y=40 */
#define ROW_GAP 8

/*
 * The footer: two centred lines under the last row, on the panel itself.
 *
 * The version pair used to be the update row's subtitle, which put a fact
 * inside a control -- so reading the version meant looking at a button, and
 * the button's own state ("Update ready") had to be squeezed in beside it,
 * right-aligned and clear of a chevron. Separating them gives the button one
 * job and the footer room for the second line, which is the thing an update
 * actually needs someone to know.
 */
#define FOOT_Y1 192
#define FOOT_Y2 212

/*
 * Two rows and a two-line footer, inside 240. Checked here rather than
 * eyeballed, because the last time these moved a row went off the bottom of a
 * 240 px screen and nobody noticed until it was flashed. FONT_LINE_H is 16 for
 * the default montserrat_14, so the second line ends at 228.
 */
BUILD_ASSERT(ROW_TOP + 2 * ROW_H + ROW_GAP <= FOOT_Y1,
	     "the settings rows now overlap the footer");
BUILD_ASSERT(FOOT_Y2 + 16 <= 240,
	     "the settings footer no longer fits on the panel");
	/* No green edge seam here. It existed as a left-edge cue back when the
	 * back control was a bare chevron that was easy to miss; the control is
	 * now a bordered 60 x 36 button that announces itself, so the seam was
	 * a second hint for something that no longer needs one -- and green is
	 * the gauge's "live" colour, which it was quietly spending. */
	mk_back(panel, back_cb);

	lv_obj_t *title = lv_label_create(panel);

	lv_label_set_text(title, "Settings");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 11);

	mk_line(panel, 40);

	char sub[56];

	snprintf(sub, sizeof(sub), "%d%%", backlight_get());
	mk_row(panel, ROW_TOP, ROW_H, "Brightness", sub, false, show_bright, NULL,
	       &pct_lbl);

	/*
	 * Both versions live here, on the row that is already about versions.
	 *
	 * There is no footer left to put them in -- the two rows and their
	 * margins use the height -- and a footer was the wrong home anyway: someone
	 * looking for a version number is already on their way to this row. It
	 * is also the only place on the device that can say which HALF of the
	 * pair is behind, since the app's version is otherwise invisible from
	 * the panel.
	 */
	upd_btn = mk_row(panel, ROW_TOP + (ROW_H + ROW_GAP), ROW_H,
			 "Software update", NULL,
			 false, upd_cb, NULL, NULL);

	/* The row's state, beside the row's name. Right-aligned clear of the
	 * chevron so it can grow to "Install 0.6.1" without colliding, and on
	 * the middle now that there is no second line to sit above. */
	upd_lbl = lv_label_create(upd_btn);
	lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
	lv_obj_align(upd_lbl, LV_ALIGN_RIGHT_MID, -34, 0);

	/*
	 * Both versions, on the panel rather than in the button.
	 *
	 * This is the only place on the device that can say which HALF of the
	 * pair is behind -- the app's version is otherwise invisible from the
	 * panel -- so it says both, or says the board's alone when no host has
	 * introduced itself.
	 */
	lv_obj_t *ver = lv_label_create(panel);

	if (proto_host_version()[0]) {
		snprintf(sub, sizeof(sub), "Blink %s  |  App %s",
			 BLINK_FW_VERSION, proto_host_version());
	} else {
		snprintf(sub, sizeof(sub), "Blink %s", BLINK_FW_VERSION);
	}
	lv_label_set_text(ver, sub);
	lv_obj_set_style_text_color(ver, COL_DIM, 0);
	lv_obj_align(ver, LV_ALIGN_TOP_MID, 0, FOOT_Y1);

	/*
	 * The one instruction this device gives, and it is here because this
	 * is the screen where it matters: an update writes a new image over
	 * USB and a board that loses power halfway is a board that has to be
	 * recovered with a cable anyway. Stated as a standing condition rather
	 * than fired as a warning mid-download -- by the time a progress bar
	 * could say it, unplugging has already happened.
	 */
	lv_obj_t *keep = lv_label_create(panel);

	lv_label_set_text(keep, "Keep the cable connected");
	lv_obj_set_style_text_color(keep, COL_DIM, 0);
	lv_obj_align(keep, LV_ALIGN_TOP_MID, 0, FOOT_Y2);

	upd_timer_cb(NULL);	/* correct the row before the first tick */

#endif

}

/*
 * A stroke in progress, for the rail to draw.
 *
 * Guarded by the same three conditions the completed stroke is, and for the
 * same reasons: with the panel open the gauge screen is not visible, and
 * during the mute the page has just changed and the rail is already saying so.
 * Showing a preview in either case would be the indicator contradicting the
 * screen.
 *
 * Horizontal strokes report nothing. They mean settings and the boot clip,
 * which are not page changes, and growing a rail mark for one would promise
 * something that is not about to happen.
 */
static void swipe_progress_cb(enum ui_swipe_dir dir, int pct)
{
	int delta = 0;

	if (panel == NULL && !ui_anim_gesture_muted()) {
		if (dir == UI_SWIPE_UP) {
			delta = 1;
		} else if (dir == UI_SWIPE_DOWN) {
			delta = -1;
		}
	}
	usage_view_page_preview(delta, delta ? pct : 0);
}

/*
 * A completed stroke, from ui_swipe rather than from LVGL.
 *
 * LVGL's own gesture detector cannot survive this panel -- one physical swipe
 * arrives as five or six short presses and it resets its accumulator on every
 * one of them. The measurements and the reasoning are in ui_swipe.h. What
 * reaches here is one event per stroke, already stitched across the dropouts
 * and already refused if it was too diagonal to call.
 *
 * Runs on the LVGL thread from a timer, so the rules that governed the gesture
 * callback still govern this: flag the request, never run a transition here.
 */
static void swipe_cb(enum ui_swipe_dir dir)
{
	if (panel != NULL) {
		return;
	}
	if (ui_anim_gesture_muted()) {
		return;	/* the swipe that just closed the clip, replayed */
	}
	if (dir == UI_SWIPE_LEFT) {
		/* Not off the CONNECTING screen. A swipe is the ACCIDENTAL
		 * route -- see the edge zones below, which stay open on
		 * purpose. */
		if (usage_view_takeover_active()) {
			return;
		}
		want_open = true;	/* run from the mode loop; see do_open */
	} else if (dir == UI_SWIPE_RIGHT) {
		/* The left chevron's promise: the boot clip on loop. Only
		 * flagged here -- the mode loop runs the player from thread
		 * context, never from inside an LVGL event. */
		ui_anim_request();
	} else if (dir == UI_SWIPE_UP || dir == UI_SWIPE_DOWN) {
		/*
		 * The provider stack. Content follows the finger, the way a
		 * list does: swiping UP pulls the next page in from below.
		 *
		 * Flagged for the mode loop like the two above, and for the
		 * same reason: this became a wipe transition when the cut was
		 * judged not to read as a swipe, and ui_slide_run() owns
		 * lv_refr_now() and cannot be re-entered from inside
		 * lv_timer_handler(). It used to run right here, back when a
		 * page change was one repaint.
		 *
		 * Asked BEFORE flagging, not after: arming a transition that
		 * cannot move is 650 ms of frozen panel for nothing, and with
		 * one provider reporting -- or during the CONNECTING takeover,
		 * where there is no data and so only one page -- that is every
		 * vertical swipe there is.
		 */
		int step = (dir == UI_SWIPE_UP) ? 1 : -1;

		/*
		 * At the end of the stack, a vertical swipe goes the only way
		 * it can.
		 *
		 * Up is "next" because content follows the finger, and that is
		 * a defensible model right up until someone uses it. Across
		 * three builds on 2026-08-27 every vertical stroke the user
		 * made went DOWN -- including after the cue was corrected to
		 * point up -- and on page 0 down asks for a page that does not
		 * exist, so nothing happened, six times.
		 *
		 * The device has TWO pages. "Which direction is forwards" is a
		 * question a two-item stack does not really have, and the ask
		 * was "a swipe down/up will switch the mode", not "a swipe up
		 * advances an ordered list". So when the requested direction
		 * has nowhere to go and the other one does, take the other one.
		 *
		 * This is not a wrap. In the middle of a longer stack both
		 * directions are available and each still does its own thing;
		 * only an end, where one of them is dead, hands over.
		 */
		if (!usage_view_can_page(step) && usage_view_can_page(-step)) {
			step = -step;
		}
		if (usage_view_can_page(step)) {
			want_page = step;
		}
	}
}

/*
 * Invisible tap strips along both screen edges: the chevrons drawn there
 * read as buttons, so tapping them must work too (tried on hardware
 * 2026-07-17). GESTURE_BUBBLE keeps the swipes alive across the strips.
 *
 * Both zones consult the gesture mute, which is not obvious: they are CLICKED
 * handlers, and a swipe is not a click. But LVGL sends CLICKED on release
 * whenever the object was pressed and nothing scrolled -- it does not suppress
 * it because it already sent GESTURE (lv_indev.c) -- so a swipe whose release
 * happens to land inside a 44x150 strip fires that strip's button as well. The
 * mute is what stops the replayed tail of a transition swipe from re-opening
 * the thing the transition just closed.
 *
 * Both routes into settings check the takeover, because there are two of them
 * and they are covered differently. The swipe arrives because usage_view marks
 * the takeover GESTURE_BUBBLE on purpose; the tap arrives because these strips
 * are created by ui_settings_attach(), which main.c runs AFTER
 * usage_view_init(), so they sit ABOVE the overlay and it never occludes them.
 *
 * The two routes are treated DIFFERENTLY while the CONNECTING bar is up (user
 * request 2026-08-18, refined the same day).
 *
 * The swipe is blocked. A board that has not connected yet has nothing in
 * settings the user can act on, and sliding away from the one screen that says
 * what it is waiting for reads as the board losing its place. That reverses
 * the intent recorded on the overlay's GESTURE_BUBBLE flag, which exists so
 * "the settings gesture is dead the whole time we're waiting for a host"
 * would not happen -- that is now the behaviour we want for swipes.
 *
 * This tap is NOT blocked, and that is deliberate. Blocking both would strand
 * the board: a WiFi join that succeeds while the fetch never does leaves the
 * bar up indefinitely with have_data false, and with no way into settings
 * there is no way to sign out or change network short of a power cycle. A tap
 * on the edge chevron is a deliberate act rather than an accidental one, so it
 * stays as the escape hatch.
 */
/*
 * A tap on an edge zone -- but only if it was a tap.
 *
 * LVGL sends CLICKED on release whenever an object was pressed and nothing
 * scrolled. It does not suppress it because the touch turned out to be a
 * swipe, so a swipe that begins or ends inside one of these 44x150 strips
 * fires the strip's button as well as the swipe. On the gauge screen that
 * means a vertical swipe near an edge opens the settings panel, which is half
 * of what "some of them is detected as swipe to settings" was: not a misread
 * direction, but a stray button press left behind by one.
 *
 * The mute does not cover this. It is set when a transition STARTS, and this
 * arrives on release -- before ui_swipe has even decided whether the stroke
 * was a swipe. ui_swipe_dragging() answers from the stroke still in progress,
 * which is the only thing that knows in time.
 */
static bool zone_was_a_tap(void)
{
	return panel == NULL && !ui_anim_gesture_muted() &&
	       !ui_swipe_dragging();
}

static void zone_settings_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (zone_was_a_tap()) {
		want_open = true;
	}
}

static void zone_anim_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (zone_was_a_tap()) {
		ui_anim_request();
	}
}

/*
 * Tapping the rail changes page, which is what makes up/down as reliable as
 * left/right finally are.
 *
 * The two horizontal gestures have had a tap path since the beginning: a
 * chevron drawn at each edge with an invisible 44x150 zone behind it. That is
 * the ONLY reason they feel dependable -- measured on 2026-08-28 they miss at
 * about the same rate as the vertical one (a 32 px horizontal stroke was
 * refused in the same session that refused 17, 19 and 22 px vertical ones).
 * The difference is that a missed horizontal swipe leaves a chevron to press,
 * and a missed vertical one left nothing at all, so every miss was a dead end.
 *
 * A resistive panel is good at presses and bad at slides, and that is not
 * something thresholds can fix: every reliability problem in this file's
 * history traced to contact pressure breaking under a MOVING finger. The swipe
 * stays, because it works for a committed stroke and it is the faster way once
 * you know it. It is just no longer the only way.
 *
 * No new furniture. The zone sits over the band the rail and the provider name
 * already occupy, so what you press is what was already telling you there was
 * somewhere to go -- exactly the arrangement the edge chevrons have, where the
 * drawn thing is the affordance and the hit area is invisible and much larger
 * than it.
 *
 * 200 x 44 is 35.6 x 7.8 mm. Wider than it looks like it needs to be because
 * the thing being aimed at is 6 px tall, and the cost of overshooting is
 * nothing: below the countdowns there is only this.
 */
#define PAGE_ZONE_W	200
#define PAGE_ZONE_H	44

static void zone_page_cb(lv_event_t *e)
{
	ARG_UNUSED(e);

	if (!zone_was_a_tap()) {
		return;
	}
	/*
	 * Same end-of-stack rule as the swipe, so the two controls cannot
	 * disagree about where a tap goes: try forwards, and take backwards if
	 * forwards has nowhere to go. With two pages that is simply "the other
	 * one".
	 */
	if (usage_view_can_page(1)) {
		want_page = 1;
	} else if (usage_view_can_page(-1)) {
		want_page = -1;
	}
}

static void mk_page_zone(lv_obj_t *scr)
{
	lv_obj_t *z = lv_btn_create(scr);

	lv_obj_set_size(z, PAGE_ZONE_W, PAGE_ZONE_H);
	lv_obj_set_style_bg_opa(z, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(z, 0, 0);
	lv_obj_align(z, LV_ALIGN_BOTTOM_MID, 0, 0);
	/* A swipe that starts here must still reach the screen, or putting a
	 * target under the rail would kill the gesture it is meant to back up. */
	lv_obj_add_flag(z, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(z, zone_page_cb, LV_EVENT_CLICKED, NULL);
	/* Behind everything: it is a hit area, not a surface, and the rail and
	 * the name have to keep drawing over it. */
	lv_obj_move_background(z);
}

static void mk_edge_zone(lv_obj_t *scr, lv_align_t align, lv_event_cb_t cb)
{
	lv_obj_t *z = lv_btn_create(scr);

	lv_obj_set_size(z, 44, 150);
	lv_obj_set_style_bg_opa(z, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(z, 0, 0);
	lv_obj_align(z, align, 0, 0);
	lv_obj_add_flag(z, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(z, cb, LV_EVENT_CLICKED, NULL);
}

/*
 * Loosen LVGL's own gesture thresholds, for the two places still using them.
 *
 * The gauge screen does not any more -- see swipe_cb and ui_swipe.h. But the
 * settings panel and the brightness overlay both close on a swipe, and those
 * still go through LVGL, so its two hard-coded defaults still apply there
 * (lv_indev.c, no Kconfig and no setter, which is why this reaches into the
 * private header).
 *
 *   gesture_min_velocity  3   a sample that moved less than this in BOTH axes
 *                             ZEROES the accumulator. Not "ignore this
 *                             sample" -- it throws away everything counted so
 *                             far. LVGL samples faster than this panel
 *                             reports, so a large share of ticks see no new
 *                             point and each one discards the stroke.
 *   gesture_limit        50   and then it wants 50 px on top, which is 21% of
 *                             a 240 px screen -- 8.9 mm of travel.
 *
 * 1 and 24 is as far as this can be pushed: the floor cannot go below 1 (at 0
 * the comparison is never true, which would leave the accumulator running
 * across the whole press), and at 1 a repeated identical point STILL trips it.
 * That ceiling is precisely why the gauge screen stopped using this path
 * rather than tuning it further. These two remaining callers are closing
 * something that also has a back button, so a swipe that misses is an
 * inconvenience rather than a dead end.
 */
#define GESTURE_MIN_VELOCITY	1
#define GESTURE_LIMIT_PX	24

static void tune_gestures(void)
{
	lv_indev_t *in = NULL;

	while ((in = lv_indev_get_next(in)) != NULL) {
		if (lv_indev_get_type(in) != LV_INDEV_TYPE_POINTER) {
			continue;
		}
		in->gesture_min_velocity = GESTURE_MIN_VELOCITY;
		in->gesture_limit = GESTURE_LIMIT_PX;
	}
}

void ui_settings_attach(lv_obj_t *scr)
{
	tune_gestures();
	/*
	 * The gauge screen's swipes come from ui_swipe, not from LVGL. There
	 * is deliberately no LV_EVENT_GESTURE handler here any more: leaving
	 * one would double-fire, and it would double-fire WRONGLY, since the
	 * fragments LVGL classifies are the ones ui_swipe exists to stitch
	 * back together.
	 */
	ui_swipe_init(swipe_cb, swipe_progress_cb);
	mk_edge_zone(scr, LV_ALIGN_RIGHT_MID, zone_settings_cb);
	mk_edge_zone(scr, LV_ALIGN_LEFT_MID, zone_anim_cb);
	mk_page_zone(scr);

	/* The OTA watcher runs from here on, not from the panel build: the boot
	 * prompt, the download bar and the outcome popup are all screen-level
	 * and have to work with settings shut. See upd_timer_cb. */
	if (upd_timer == NULL) {
		upd_timer = lv_timer_create(upd_timer_cb, 250, NULL);
	}
}
