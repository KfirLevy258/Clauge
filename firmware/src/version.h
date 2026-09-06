#ifndef VERSION_H
#define VERSION_H

/* One source of truth: the serial hello and the settings panel must never
 * disagree about what is flashed. Bump on any user-visible change. */
#define BLINK_FW_VERSION "1.3.2"

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
 * 1787961600 = 2026-08-29T00:00:00Z. */
#define BLINK_TIME_FLOOR 1787961600LL

#endif /* VERSION_H */
