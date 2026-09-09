/* Which OTA request the tethered loop acts on, pinned.
 *
 *   cc -Wall -Werror -I firmware/src tests/upd_tap/host_test.c \
 *      firmware/src/upd_tap.c -o /tmp/upd_tap
 *
 * The branch this covers swallowed a customer's tap on "Update now" whole:
 * no message sent, no error shown, the prompt latched shut behind it. It was
 * two `if` statements in main.c where no test could reach them. See
 * upd_tap.h.
 */
#include <stdio.h>
#include "upd_tap.h"

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

int main(void)
{
	/* --- nothing pending --- */
	CHECK(upd_tap_act(false, false, false) == UPD_ACT_NONE);
	CHECK(upd_tap_act(false, false, true) == UPD_ACT_NONE);

	/* --- a check on its own is still a check --- */
	CHECK(upd_tap_act(false, true, true) == UPD_ACT_CHECK);
	/* Staging is the install's precondition, not the check's: a check is
	 * how something GETS staged, so refusing it here would mean a board
	 * that has nothing can never learn that it does. */
	CHECK(upd_tap_act(false, true, false) == UPD_ACT_CHECK);

	/* --- an install on its own --- */
	CHECK(upd_tap_act(true, false, true) == UPD_ACT_INSTALL);
	/* Consent with nothing staged is REFUSED, never NONE. proto.c returns
	 * false here and main.c used to throw that away, which is exactly how
	 * the tap became silence: the caller has to have something to report. */
	CHECK(upd_tap_act(true, false, false) == UPD_ACT_REFUSED);

	/* --- both at once: the reported bug --- */
	/* The daemon reconnects (queuing a check) in the same window the user
	 * taps "Update now". The check must not win: running it first clears
	 * ota_staged, and the install that follows finds nothing to send. */
	CHECK(upd_tap_act(true, true, true) == UPD_ACT_INSTALL);
	/* And when the state really is gone, the answer is still the user's
	 * question, answered -- not the errand, run in its place. */
	CHECK(upd_tap_act(true, true, false) == UPD_ACT_REFUSED);

	/* An install request is never dropped for any combination of the
	 * other two inputs. Stated as its own loop because the whole fault was
	 * one specific combination getting past a reviewer's eye. */
	for (int chk = 0; chk < 2; chk++) {
		for (int st = 0; st < 2; st++) {
			enum upd_act a = upd_tap_act(true, chk != 0, st != 0);

			CHECK(a == UPD_ACT_INSTALL || a == UPD_ACT_REFUSED);
			CHECK(a != UPD_ACT_CHECK);
			CHECK(a != UPD_ACT_NONE);
		}
	}

	printf(fails ? "FAILED\n" : "ok\n");
	return fails ? 1 : 0;
}
