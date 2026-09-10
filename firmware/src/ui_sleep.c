/*
 * Sleep mode (docs/sleep-mode-design.md).
 *
 * Three clips: closing once, the loop for as long as whatever put the board
 * to sleep still holds -- the host silent, or the reading no longer moving --
 * opening once. The clips are drawn straight to the panel by the BAN1 player,
 * over a plain LVGL screen in the clip's own ground colour; that screen is
 * also what takes the tap. Every frame gap services the protocol, so the
 * first word from a waking app is heard within a frame.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "bootclip.h"
#include "proto.h"
#include "sleep_gate.h"
#include "ui_boot.h"
#include "ui_settings.h"
#include "ui_sleep.h"
#include "usage_freshness.h"
#include "usage_view.h"

#define PEEK_MS 10000

static volatile bool tapped;

static void tap_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	tapped = true;
}

static bool (*wake_when)(void);

/*
 * Every reason to stop dozing, asked in one place.
 *
 * wake_when() is the caller's reason -- the app spoke again, or the reading
 * started moving. ui_settings_busy() is this file's own, and it is here
 * because of what dozing takes away rather than because of anything the
 * sleep design says.
 *
 * Settings are asked for by a left swipe on the gauge screen, or by a tap on
 * the right edge zone -- which is the route that still works over the
 * CONNECTING takeover, where the swipe is deliberately refused. Neither
 * gesture opens anything: both raise a flag that ui_settings_service() acts
 * on, and the only call to that lives in the mode loop this function has
 * taken over. So on the shipped build -- USB-only, one loop -- a request made
 * during a peek latched and nothing ever answered it. The panel then opened
 * by itself whenever the board next woke, which is the same bug wearing the
 * other face.
 *
 * That bites hardest in the state that causes it. A board plugged into a
 * computer with no daemon installed, or one that cannot open the port, dozes
 * after 60 s over the CONNECTING screen -- and settings is exactly where the
 * owner goes to fix that. A tap gave them ten seconds of dashboard; it did
 * not give them control.
 *
 * Waking rather than servicing the latch here is deliberate. ui_slide_run()
 * must be driven from a mode loop in thread context, never from under a clip
 * player or an LVGL callback (ui_slide.h), and a peek that opened the panel
 * would then have to decide what to do when its ten seconds ran out with the
 * user halfway through the list. Ending the doze hands the request back to
 * the loop that owns it, and the panel slides in on that loop's first pass.
 */
static bool woken(void)
{
	return wake_when() || ui_settings_busy();
}

static bool awake_now(void)
{
	return woken();
}

static bool awake_or_tap(void)
{
	return woken() || tapped;
}

static void service(void)
{
	proto_service();
	lv_timer_handler();
	k_sleep(K_MSEC(5));
}

/* A tap: the dashboard as it was, with a word about why nothing moves. Ten
 * seconds, or until there is something new to show, then back to dozing. */
static void peek(lv_obj_t *prev, lv_obj_t *sleep_scr, const char *note)
{
	int64_t until = k_uptime_get() + PEEK_MS;

	lv_scr_load(prev);
	ui_settings_notice(note);
	lv_refr_now(NULL);
	while (k_uptime_get() < until && !woken()) {
		service();
	}
	ui_settings_notice_dismiss();
	if (!woken()) {
		lv_scr_load(sleep_scr);
		lv_refr_now(NULL);
	}
}

void ui_sleep_run(bool (*awake)(void), const char *peek_note)
{
	const struct bootclip *close = sleepclip_active(SLEEP_CLOSE);
	const struct bootclip *loop = sleepclip_active(SLEEP_LOOP);
	const struct bootclip *open = sleepclip_active(SLEEP_OPEN);
	lv_obj_t *prev = lv_scr_act();
	lv_obj_t *scr = lv_obj_create(NULL);

	wake_when = awake;
	/*
	 * Tell the dashboard it is asleep, before anything else.
	 *
	 * service() below keeps the protocol running for the whole sleep, so
	 * every usage_view setter goes on being called with nobody looking --
	 * and the status-change popup is the one that would ACT on that, by
	 * putting a card on a screen that is not loaded and leaving it there:
	 * the 1 s tick that expires it is driven from the mode loop this
	 * function has taken over. A tap's peek would then show a sentence
	 * from hours ago as though it were news.
	 */
	usage_view_set_sleeping(true);
	/*
	 * And take What's new down, because it would eat the tap below.
	 *
	 * That screen is a full 320x240 on lv_layer_top, which sits above
	 * every screen -- so lv_scr_load() does not hide it and the closing
	 * clip, drawn straight to the panel, only paints over it. The tap is
	 * the part that matters: lv_obj_create hands out LV_OBJ_FLAG_CLICKABLE
	 * by default (lv_obj.c:495), so the hit test finds this overlay before
	 * it finds `scr`, tap_cb never runs, and a board dozing with the screen
	 * open cannot be woken by touching it at all. Every OTHER modal here is
	 * narrower than the panel and leaves a live strip down each side, which
	 * is why this is the first one to reach the tap.
	 *
	 * The notice underneath is left alone: the peek replaces it with its
	 * own note a moment later, and that path has worked since it was
	 * written.
	 */
	ui_settings_whatsnew_dismiss();
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_add_flag(scr, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_bg_color(scr, lv_color_hex(close->bg_rgb), 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_obj_add_event_cb(scr, tap_cb, LV_EVENT_CLICKED, NULL);
	lv_scr_load(scr);
	lv_refr_now(NULL);
	printk("[sleep] dozing (%s)\n", close->name);

	ui_boot_play_clip(close->blob, close->blob_len, awake_now);
	while (!woken()) {
		tapped = false;
		if (!ui_boot_play_clip(loop->blob, loop->blob_len,
				       awake_or_tap)) {
			/* A loop that will not decode must not spin. */
			int64_t until = k_uptime_get() + 1000;

			while (k_uptime_get() < until) {
				service();
			}
		}
		if (tapped && !woken()) {
			peek(prev, scr, peek_note);
		}
	}
	printk("[sleep] waking\n");
	ui_boot_play_clip(open->blob, open->blob_len, NULL);

	/*
	 * Back to the dashboard as it was -- flagged old only if it still IS.
	 *
	 * This used to stamp STALE unconditionally, which was right for the
	 * one caller that existed and wrong for both of the others. A board
	 * dozing because its reading stopped moving wakes on a FRESH reading,
	 * which has already set the dot green; stamping amber over it labels
	 * the very frame that woke us as old. And a board dozing before it
	 * ever met a daemon has no reading at all to call old.
	 *
	 * The test is the display's own bound, not the dozing one. Whether to
	 * doze is a question about the person and is answered in hours;
	 * whether the dot is amber is a question about the number and is
	 * answered in half an hour, the same 1800 s the daemon uses to set the
	 * `stale` flag it normally tells us. Asking the four-hour question
	 * here left a board waking from an hour's doze green over an hour-old
	 * reading until the next usage message landed, up to a minute later.
	 *
	 * It is also the reading's own AGE, not the desk's. main.c dozes on
	 * usage_freshness_active_age_s, because that decision is about the
	 * person; this dot describes the figure beside it, so it keeps asking
	 * how old the figure is even when the two answers differ by hours --
	 * which they do exactly when the daemon is re-offering a remembered
	 * five-hour reading. A board woken by a live status line whose dial is
	 * still this morning's SHOULD say the reading is old, because it is.
	 */
	lv_scr_load(prev);
	lv_obj_del(scr);
	/*
	 * Eyes open: the popup may speak again, from the NEXT change. The
	 * counts that arrived during the sleep were recorded as they came, so
	 * this does not release a backlog -- waking shows what is true now.
	 */
	usage_view_set_sleeping(false);
	if (usage_view_have_data() &&
	    sleep_reading_is_stale(usage_freshness_age_s(k_uptime_get()))) {
		usage_view_set_status(USAGE_STATUS_STALE);
	}
	lv_refr_now(NULL);
}
