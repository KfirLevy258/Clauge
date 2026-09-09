#ifndef UPD_TAP_H
#define UPD_TAP_H

#include <stdbool.h>

/*
 * Which of the two pending OTA requests the tethered loop acts on this turn.
 *
 * Pure, and in its own file, for the reason upd_row is: this was two
 * independent `if` statements in main.c, in the wrong order, and nothing
 * could reach them from a host test.
 *
 * What the order cost. `ota_request_check()` and `ota_request_install()` set
 * separate consume-on-read flags, and main.c took the check first:
 *
 *	if (ota_take_check_request())   proto_ota_check();
 *	...
 *	if (ota_take_install_request()) proto_ota_install();
 *
 * proto_ota_check() clears `ota_staged`, and proto_ota_install() returns
 * false without sending anything when nothing is staged -- a return value
 * main.c discarded. So when both flags were up on the same turn, the check
 * disarmed the install and the install failed silently.
 *
 * Both flags being up together is not a rare interleaving. The daemon sends
 * `welcome` on every connection and proto.c answers it with
 * ota_request_check(), so ANY daemon restart queues a check -- and `blink
 * update` restarts the daemon by design. A customer looking at the update
 * prompt while their app updates underneath them taps "Update now" straight
 * into that window.
 *
 * Reported by a customer on 2026-09-09, upgrading 1.2.5 -> 1.3.2: the tap did
 * nothing at all. No message left the board, the badge had already been
 * cleared, the prompt had latched itself shut for the boot, and thirty
 * seconds later the only thing on screen was "Couldn't check for updates."
 * -- an error about a check they never asked for.
 *
 * The rule this file encodes: consent outranks housekeeping. A tap is a
 * person standing at the desk; the check is a background errand that will
 * come round again on the next connection. And a check that arrives with an
 * install is DROPPED rather than deferred, because running it afterwards
 * would set CHECKING over the DOWNLOADING the install just raised -- the
 * same clobber, one turn later.
 */
enum upd_act {
	UPD_ACT_NONE,		/* nothing was asked for */
	UPD_ACT_CHECK,		/* ask the daemon what it has */
	UPD_ACT_INSTALL,	/* the user consented and there is an image */
	UPD_ACT_REFUSED,	/* the user consented and there is not */
};

/*
 * `install_req` and `check_req` are the values of the two request flags,
 * which MUST both already have been taken this turn -- they are
 * consume-on-read, so a caller that reads one only when the other is unset
 * leaves a stale request armed for the next turn.
 *
 * `staged` is proto_ota_staged(): whether an `ota_avail` has arrived and the
 * manifest the install would use is still valid.
 */
enum upd_act upd_tap_act(bool install_req, bool check_req, bool staged);

#endif /* UPD_TAP_H */
