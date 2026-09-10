#ifndef VERSION_H
#define VERSION_H

/* One source of truth: the serial hello and the settings panel must never
 * disagree about what is flashed. Bump on any user-visible change.
 *
 * Bump it LAST, immediately before cutting the release. check_versions.sh
 * --release compares this file's last commit against every later change under
 * firmware/src, so a bump made early and then followed by more firmware work
 * fails the gate at the worst moment -- and the only honest way out is another
 * edit here. Bumping early feels tidy and buys nothing. */
#define BLINK_FW_VERSION "1.3.3"

/* The wire protocol spoken over USB, and the daemon's own PROTO_VERSION in
 * pc/version.py. It is a floor, not a format selector: the protocol only ever
 * grows, and msg_get_* already ignores keys it does not know, so this moves
 * only for a change that genuinely breaks an older peer. Kept here rather than
 * in proto.c so one header answers both "what am I" questions.
 * tests/ci/check_versions.sh pins it against the daemon's copy. */
#define BLINK_PROTO_VERSION 2

/* No accepted wall-clock time may precede this firmware's existence
 * (2026-07-17T00:00:00Z). SNTP is unauthenticated UDP; without a floor, an
 * on-path attacker can shift the clock into the past and make once-valid
 * leaked certificates verify again. Nudge forward on version bumps.
 * 1789084800 = 2026-09-11T00:00:00Z. */
#define BLINK_TIME_FLOOR 1789084800LL

#endif /* VERSION_H */
