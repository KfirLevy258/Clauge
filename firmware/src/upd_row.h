#ifndef UPD_ROW_H
#define UPD_ROW_H

#include <stdbool.h>

/*
 * What the "Software update" row on the settings screen says.
 *
 * Pure, and in its own file, for the reason sleep_gate is: this decision was
 * three branches inlined in ui_settings.c, nothing could reach them from a
 * host test, and one of them was wrong for a year.
 *
 * The wrong one: after a check finds the firmware current, the row said
 * "Up to date" in green -- unconditionally, painting over the amber "App is
 * old" that the idle state had shown a moment earlier. So a customer whose
 * app was a release behind tapped the row, was told everything was fine, and
 * nothing happened. Reported from a desk on 2026-09-06, where the panel had
 * firmware 1.3.0 against an app on 1.2.5.
 *
 * That state is not exotic, which is what makes the green a real fault
 * rather than a cosmetic one. The daemon fetches firmware from the LATEST
 * release whatever its own version (pc/ota.py RELEASE_BASE ends
 * /releases/latest/), and ships with daemon.auto false so it never updates
 * itself. A board therefore overtakes the app routinely -- any customer who
 * accepts a firmware update without separately running `blink update` lands
 * here -- and the half that is behind is the half this screen cannot reach.
 *
 * "Up to date" has to mean the PRODUCT is up to date, not one half of it.
 */
enum upd_row {
	UPD_ROW_BLANK,		/* nothing to report */
	UPD_ROW_READY,		/* an update is downloaded and waiting */
	UPD_ROW_APP_OLD,	/* this board is current; the computer is not */
	UPD_ROW_UP_TO_DATE,	/* both halves current */
};

/* The row while the board is not mid-check. `badge` is ota_badge(),
 * `host_outdated` is proto_host_outdated(). */
enum upd_row upd_row_idle(bool badge, bool host_outdated);

/* The row after a check came back with no newer firmware. */
enum upd_row upd_row_checked(bool host_outdated);

#endif /* UPD_ROW_H */
