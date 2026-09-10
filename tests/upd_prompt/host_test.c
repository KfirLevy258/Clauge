/* Whether the update offer may be shown, withdrawn, or asked again, pinned.
 *
 *   cc -Wall -Werror -I firmware/src tests/upd_prompt/host_test.c \
 *      firmware/src/upd_prompt.c -o /tmp/upd_prompt
 *
 * The rule this covers left a customer's board half-updated for good
 * (2026-09-10): the app half of a pair update landed, the firmware half was
 * dropped in silence, and the prompt -- latched shut by the very tap that
 * started it -- never came back. Every branch below lived inside an LVGL
 * timer callback where nothing but a finger could reach it. See upd_prompt.h.
 */
#include <stdio.h>
#include "upd_prompt.h"

static int fails;

/* Prints on both arms: check_host_tests.sh counts PASS lines, and a suite
 * that says nothing when checks hold is indistinguishable from an empty one. */
#define CHECK(c) do { \
	if (!(c)) { \
		fails++; \
		printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); \
	} else { \
		printf("PASS %s:%d %s\n", __FILE__, __LINE__, #c); \
	} \
} while (0)

/* The seven states, so a new one cannot be added without this test seeing it. */
static const enum ota_ui_state ALL[] = {
	OTA_UI_IDLE, OTA_UI_CHECKING, OTA_UI_UP_TO_DATE, OTA_UI_AVAILABLE,
	OTA_UI_DOWNLOADING, OTA_UI_REBOOTING, OTA_UI_FAILED,
};
#define NSTATES ((int)(sizeof(ALL) / sizeof(ALL[0])))

int main(void)
{
	struct upd_prompt_turn t;
	int i;

	/* --- the ordinary offer ------------------------------------------- */
	t = upd_prompt_step(OTA_UI_AVAILABLE, false, false, false);
	CHECK(t.act == UPD_PROMPT_SHOW);
	CHECK(t.latched == true);

	/* Shown once. The second turn finds the latch and says nothing --
	 * this is what makes "Later" mean later rather than five seconds. */
	t = upd_prompt_step(OTA_UI_AVAILABLE, false, true, false);
	CHECK(t.act == UPD_PROMPT_NOTHING);
	CHECK(t.latched == true);

	/* Already on the glass: not shown twice, and not withdrawn either. */
	t = upd_prompt_step(OTA_UI_AVAILABLE, true, true, false);
	CHECK(t.act == UPD_PROMPT_NOTHING);

	/* --- never on top of something else -------------------------------- */
	t = upd_prompt_step(OTA_UI_AVAILABLE, false, false, true);
	CHECK(t.act == UPD_PROMPT_NOTHING);
	/* And being refused for busy must NOT latch: the offer has not been
	 * made yet, so the next quiet turn still owes the user the question. */
	CHECK(t.latched == false);

	/* --- withdrawal: the board took it back ---------------------------- */
	/* Any state that is not AVAILABLE, with the box open. CHECKING is the
	 * one that actually happens: proto.c answers every `welcome` with a
	 * check, so a daemon restart lands here under an open green button. */
	for (i = 0; i < NSTATES; i++) {
		if (ALL[i] == OTA_UI_AVAILABLE) {
			continue;
		}
		t = upd_prompt_step(ALL[i], true, true, false);
		CHECK(t.act == UPD_PROMPT_WITHDRAW);
		CHECK(t.latched == false);   /* never answered: ask again */
	}

	/* --- THE ONE THAT WAS UNREACHABLE ---------------------------------- */
	/*
	 * FAILED with NO box open. The tap that started the install closed the
	 * prompt, so there is nothing to withdraw -- and every other rule here
	 * keys off an open box. Without the FAILED term the latch set by that
	 * tap survives the whole boot and the board never offers again, which
	 * is exactly the state a customer's desk was left in on 2026-09-10.
	 */
	t = upd_prompt_step(OTA_UI_FAILED, false, true, false);
	CHECK(t.act == UPD_PROMPT_NOTHING);   /* nothing to take down */
	CHECK(t.latched == false);            /* but ask again */

	/* And once it has lifted, the next genuine offer really is made. */
	t = upd_prompt_step(OTA_UI_AVAILABLE, false, false, false);
	CHECK(t.act == UPD_PROMPT_SHOW);

	/* A failure while the box is somehow still up: both rules agree. */
	t = upd_prompt_step(OTA_UI_FAILED, true, true, false);
	CHECK(t.act == UPD_PROMPT_WITHDRAW);
	CHECK(t.latched == false);

	/* Busy does not stop a failure from re-arming: `busy` gates SHOWING,
	 * not the latch. A notice on screen is usually the failure's own. */
	t = upd_prompt_step(OTA_UI_FAILED, false, true, true);
	CHECK(t.latched == false);

	/* --- the latch is never set by anything but showing ---------------- */
	for (i = 0; i < NSTATES; i++) {
		t = upd_prompt_step(ALL[i], false, false, false);
		if (ALL[i] == OTA_UI_AVAILABLE) {
			CHECK(t.latched == true);
		} else {
			CHECK(t.latched == false);
		}
	}

	/* --- a quiet board is quiet ---------------------------------------- */
	for (i = 0; i < NSTATES; i++) {
		if (ALL[i] == OTA_UI_AVAILABLE || ALL[i] == OTA_UI_FAILED) {
			continue;
		}
		t = upd_prompt_step(ALL[i], false, true, false);
		CHECK(t.act == UPD_PROMPT_NOTHING);
		CHECK(t.latched == true);   /* nothing here lifts it */
	}

	printf("%s\n", fails ? "FAILURES" : "all checks passed");
	return fails ? 1 : 0;
}
