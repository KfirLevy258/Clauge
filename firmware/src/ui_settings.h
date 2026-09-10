#ifndef UI_SETTINGS_H
#define UI_SETTINGS_H

#include <lvgl.h>

/* Make `scr` (the gauge screen) open the settings panel on a left swipe.
 * The panel is an overlay on the same screen, NOT a separate LVGL screen:
 * the 16 KiB LVGL pool cannot hold two full screens (see usage_view_deinit).
 * All three actions (Reset WiFi / Re-sign-in / Factory reset) confirm first,
 * then persist the change and reboot. */
void ui_settings_attach(lv_obj_t *scr);

/* One-line notice popup on the gauge screen (update outcome etc.). Safe to
 * call once the screen exists; replaces any previous notice. */
void ui_settings_notice(const char *txt);
/* The same box with a heading above the body: the heading in montserrat_16
 * at full brightness, the body dim beneath the flex gap. For a notice that
 * is a headline plus detail rather than one sentence -- see the comment on
 * the implementation. A NULL or empty title is exactly ui_settings_notice. */
void ui_settings_notice_titled(const char *title, const char *body);
/* The post-update notice: a tick, the version, "5 changes since 1.2.5", and
 * a "What's new" button that opens the paged list. `from`/`to` are the halves
 * of the OTA breadcrumb, and they decide what that screen shows. */
void ui_settings_notice_update(const char *version, const char *summary,
			       const char *from, const char *to);
/* Close the What's new screen, if it is open. */
void ui_settings_whatsnew_dismiss(void);
/* Take a notice down without the tap; the sleep peek does this on timeout. */
void ui_settings_notice_dismiss(void);

/* Run any pending open/close transition. MUST be called from a mode loop in
 * thread context, not from an LVGL callback: the transition drives
 * lv_refr_now() itself (see ui_slide.h). `pump` (may be NULL) runs each step
 * so background duties and the boot watchdog stay alive. */
/*
 * Discard any open/close the handlers latched but nobody serviced.
 *
 * ui_settings_attach() arms the gestures long before either mode loop exists:
 * main.c runs boot_ssid_scan() and the WiFi settle in between, which pump
 * lv_timer_handler() with touch fully live and can take a minute on a failed
 * join. A tap latched in that window would otherwise be acted on by the mode
 * loop's first iteration -- the panel sliding in by itself long after the user
 * gave up on it. Call this once before entering a mode loop; a deliberate tap
 * repeats, and from there on the latch is serviced within one iteration.
 */
void ui_settings_drop_pending(void);

void ui_settings_service(void (*pump)(void));

/*
 * Is the panel open, or is a gesture still waiting to be serviced?
 *
 * Both answers mean the same thing to a caller that is about to stop running
 * the mode loop: a person is in the middle of using this panel, and the only
 * code that can act on them is the loop that is about to go away. The doze in
 * ui_sleep.c asks it as a reason to wake, and main.c as a reason not to fall
 * asleep in the first place -- because settings is where an owner goes when
 * the board is behaving badly, which is precisely when it is dozing.
 */
bool ui_settings_busy(void);

#endif /* UI_SETTINGS_H */
