#!/bin/sh
# The firmware and the daemon ship from one tag, so they must agree about what
# that tag is -- and about the protocol they speak over the cable.
#
# Nothing else can catch a mismatch. A daemon claiming 0.6.0 while the board it
# flashes reports 0.5.1 is not a build failure, not a test failure, and not
# visible on screen; it is a support call six weeks later from someone whose
# install is half-updated. This is two greps, run in CI and again as the first
# thing tools/release.sh does.
set -eu

# --release also insists the firmware version has MOVED since the last change
# to the firmware. In CI that is a warning, because it is the ordinary state of
# a branch mid-flight; at release time it is fatal, because it is the mistake
# that shipped 31 firmware commits nobody could reach.
RELEASE=0
[ "${1:-}" != "--release" ] || RELEASE=1

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

VH="$ROOT/firmware/src/version.h"
PV="$ROOT/pc/version.py"

fw=$(sed -n 's/^#define BLINK_FW_VERSION "\(.*\)"$/\1/p' "$VH")
fw_proto=$(sed -n 's/^#define BLINK_PROTO_VERSION \([0-9][0-9]*\).*$/\1/p' "$VH")
pc=$(sed -n 's/^RELEASE_VERSION = "\(.*\)"$/\1/p' "$PV")
pc_proto=$(sed -n 's/^PROTO_VERSION = \([0-9][0-9]*\)$/\1/p' "$PV")

fail=0
for pair in "fw:$fw" "fw_proto:$fw_proto" "pc:$pc" "pc_proto:$pc_proto"; do
	name=${pair%%:*}
	value=${pair#*:}
	if [ -z "$value" ]; then
		echo "FATAL: could not read $name -- has the declaration moved?" >&2
		fail=1
	fi
done
[ "$fail" -eq 0 ] || exit 1

if [ "$fw" != "$pc" ]; then
	echo "FATAL: release version disagrees." >&2
	echo "       firmware/src/version.h says $fw" >&2
	echo "       pc/version.py says          $pc" >&2
	echo "       They ship from one tag; bump both." >&2
	fail=1
fi
if [ "$fw_proto" != "$pc_proto" ]; then
	echo "FATAL: protocol version disagrees ($fw_proto vs $pc_proto)." >&2
	echo "       Both sides must move together, and only for a change that" >&2
	echo "       genuinely breaks an older peer -- additive fields do not." >&2
	fail=1
fi
[ "$fail" -eq 0 ] || exit 1

# --- has the firmware version moved since the firmware did? ----------------
#
# The two greps above catch a version.h that disagrees with pc/version.py.
# Nothing caught a version.h that agrees with it and is simply STALE, and that
# is the failure that actually happened: version.h last changed 2026-08-31 and
# 31 firmware commits landed after it, among them a watchdog reboot loop and
# the doze fix. Every board compared "1.2.5" against "1.2.5", every daemon said
# there was nothing to do, and the fixes were unreachable by OTA for a week
# with nothing anywhere saying so.
#
# Comparing build hashes would be the thorough version. This catches the
# mistake that was actually made -- forgetting to bump -- for two git commands.
stale_firmware() {
	# Not a checkout at all (an unpacked tarball, a shallow export): say
	# nothing rather than guess. A check that cannot run is not a failure.
	git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || return 1
	# A version.h edited but not yet committed IS the bump, in progress.
	git -C "$ROOT" diff --quiet HEAD -- firmware/src/version.h 2>/dev/null \
		|| return 1
	bump=$(git -C "$ROOT" log -1 --format=%H -- firmware/src/version.h \
		2>/dev/null) || return 1
	[ -n "$bump" ] || return 1
	# That commit against the WORKING TREE, not against HEAD: release.sh
	# builds the image from the tree, so an uncommitted firmware change
	# ships just as surely as a committed one. version.h itself cannot
	# appear here -- it matches at both ends, or we returned above -- so a
	# non-empty answer is firmware whose version did not move.
	changed=$(git -C "$ROOT" diff --name-only "$bump" -- firmware/src \
		2>/dev/null) || return 1
	[ -n "$changed" ] || return 1
	echo "$changed"
}

if changed=$(stale_firmware); then
	n=$(printf '%s\n' "$changed" | wc -l | tr -d ' ')
	echo "firmware/src has changed in $n file(s) since BLINK_FW_VERSION last moved:" >&2
	printf '%s\n' "$changed" | sed 's/^/       /' >&2
	if [ "$RELEASE" -eq 1 ]; then
		echo "FATAL: releasing $fw would publish firmware that every board" >&2
		echo "       already believes it is running. Bump BLINK_FW_VERSION" >&2
		echo "       in firmware/src/version.h (and RELEASE_VERSION with it)." >&2
		exit 1
	fi
	echo "       (warning only -- fatal at release time)" >&2
fi

echo "versions agree: release $fw, protocol $fw_proto"
