#include "upd_prompt.h"

struct upd_prompt_turn upd_prompt_step(enum ota_ui_state st, bool prompt_open,
				       bool latched, bool busy)
{
	struct upd_prompt_turn t = { UPD_PROMPT_NOTHING, latched };

	/*
	 * The offer asserts something about the CURRENT state, so it goes away
	 * when that state does -- and because it was never answered, the latch
	 * goes with it.
	 */
	if (st != OTA_UI_AVAILABLE && prompt_open) {
		t.act = UPD_PROMPT_WITHDRAW;
		t.latched = false;
	}

	/*
	 * A failure lifts the latch whether or not a box is open, which is the
	 * whole difference between this and the rule above. The tap that
	 * started the install already closed the prompt, so there is nothing
	 * left to withdraw: without this line the latch set by that tap
	 * survives to the end of the boot and the board can never ask again.
	 */
	if (st == OTA_UI_FAILED) {
		t.latched = false;
	}

	/* Never stack on top of something the user is already answering. */
	if (st == OTA_UI_AVAILABLE && !prompt_open && !latched && !busy) {
		t.act = UPD_PROMPT_SHOW;
		t.latched = true;
	}

	return t;
}
