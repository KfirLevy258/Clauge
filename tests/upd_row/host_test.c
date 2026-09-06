/* What the "Software update" row says, pinned.
 *
 *   cc -Wall -Werror -I firmware/src tests/upd_row/host_test.c \
 *      firmware/src/upd_row.c -o /tmp/upd_row
 *
 * The branch this covers said "Up to date" in green over an app a release
 * behind, on a real desk, with nothing on that screen able to fix it. It was
 * inlined in ui_settings.c where no test could reach it. See upd_row.h.
 */
#include <stdio.h>
#include "upd_row.h"

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
	/* --- idle --- */

	/* nothing to say */
	CHECK(upd_row_idle(false, false) == UPD_ROW_BLANK);
	/* the app is behind: say so, even with no firmware update pending */
	CHECK(upd_row_idle(false, true) == UPD_ROW_APP_OLD);
	/* a downloaded update outranks the advisory -- it is the one thing
	 * here a tap can act on */
	CHECK(upd_row_idle(true, false) == UPD_ROW_READY);
	CHECK(upd_row_idle(true, true) == UPD_ROW_READY);

	/* --- after a check that found no newer firmware --- */

	/* both halves current: the only state that may claim it */
	CHECK(upd_row_checked(false) == UPD_ROW_UP_TO_DATE);

	/*
	 * THE REGRESSION. The check asks "is there newer firmware" and the
	 * answer was no -- but the app on the computer is a release behind,
	 * so the product is not up to date and must not say it is.
	 *
	 * Reachable without anything unusual: the daemon offers firmware from
	 * the latest release whatever its own version, and never updates
	 * itself (daemon.auto is false in every manifest), so a board
	 * overtakes the app as a matter of course.
	 */
	CHECK(upd_row_checked(true) == UPD_ROW_APP_OLD);
	CHECK(upd_row_checked(true) != UPD_ROW_UP_TO_DATE);

	if (fails) {
		printf("%d FAILED\n", fails);
		return 1;
	}
	return 0;
}
