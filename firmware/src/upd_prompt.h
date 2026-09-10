#ifndef UPD_PROMPT_H
#define UPD_PROMPT_H

#include <stdbool.h>
#include "ota.h"

/*
 * Whether the "Update now / Later" box goes up, comes down, or stays as it is
 * -- and what the once-per-boot latch must be afterwards.
 *
 * Pure, and in its own file, for the reason upd_tap and upd_row are: this was
 * three statements scattered through a 200-line LVGL timer callback, and the
 * only way to reach any of them was a finger on a resistive panel.
 *
 * WHY THE LATCH EXISTS. The offer is shown at most once per boot so that
 * "Later" means later. Without it the box would come back on the next OTA
 * state tick, seconds after being dismissed, which is not an offer -- it is
 * nagging.
 *
 * WHY THE LATCH HAS TO LIFT AGAIN, in two different cases that are easy to
 * mistake for one:
 *
 *   - WITHDRAWN. The board took the offer down itself because the state that
 *     justified it went away -- proto.c answers every `welcome` with a check,
 *     so any daemon restart moves the board to CHECKING underneath an open
 *     green button. That offer was never answered, so the next genuine
 *     AVAILABLE must be allowed to ask again. Reported 2026-09-09.
 *
 *   - FAILED. The customer said yes and did not get an update. That is not a
 *     "Later" either, and it is the case the withdrawal rule cannot cover:
 *     answering the prompt CLOSES it, so by the time the failure arrives
 *     there is no open box to withdraw and nothing lifts the latch. A board
 *     in that state will not offer again until it is power-cycled.
 *
 * The second one is what made a customer's half-finished pair update
 * permanent (2026-09-10): the app half landed, the firmware half was dropped
 * silently, and the board -- latched shut by the tap that started it -- never
 * offered the firmware again. The daemon now says `ota_error` rather than
 * `ota_none` when it gives up, which is what puts the board in FAILED at all;
 * this is the half that makes FAILED mean the offer can return.
 *
 * `busy` is "something else is already asking this person something": the
 * settings panel, a confirm, a notice, or the download overlay. An offer must
 * never stack on top of one of those.
 */
enum upd_prompt_act {
	UPD_PROMPT_NOTHING,	/* leave whatever is on the glass alone */
	UPD_PROMPT_SHOW,	/* put the offer up */
	UPD_PROMPT_WITHDRAW,	/* take it down; it was never answered */
};

struct upd_prompt_turn {
	enum upd_prompt_act act;
	bool latched;		/* what the latch must be AFTER this turn */
};

/*
 * `prompt_open` is whether the box is on screen right now, `latched` the
 * current once-per-boot flag, `busy` whether anything else owns the glass.
 *
 * The caller applies the answer verbatim -- act, then `latched` -- rather
 * than deciding anything itself. That is the point: every rule above is in
 * one function that a host test can drive through all seven OTA states.
 */
struct upd_prompt_turn upd_prompt_step(enum ota_ui_state st, bool prompt_open,
				       bool latched, bool busy);

#endif /* UPD_PROMPT_H */
