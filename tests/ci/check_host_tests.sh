#!/bin/sh
# Compile and run every standalone firmware host test.
#
#   tests/ci/check_host_tests.sh
#
# These test the firmware's pure logic on a laptop, with no board and no
# Zephyr -- native_sim is Linux-only, so this is how the C in this project
# gets exercised on a Mac. Eight of them existed and NOTHING ran them
# automatically: each carried a `cc ...` line in a comment that a human was
# expected to copy and paste. A test nobody runs is documentation.
#
# The table below is explicit rather than parsed out of those comments: a
# runner that greps for its own build commands fails open the moment a comment
# is reworded, which is the failure mode of every clever version of this.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

WORK="${TMPDIR:-/tmp}/blink-host-tests"
rm -rf "$WORK"
mkdir -p "$WORK"

CC="${CC:-cc}"
SRC="$ROOT/firmware/src"
failures=0

# name | extra sources (space separated, relative to firmware/src) | extra cflags
run_one() {
	name=$1
	extra=$2
	cflags=$3
	src="$ROOT/tests/$name/host_test.c"

	if [ ! -f "$src" ]; then
		echo "MISSING: tests/$name/host_test.c" >&2
		failures=$((failures + 1))
		return
	fi

	objs=""
	for e in $extra; do
		objs="$objs $SRC/$e"
	done

	# -Wall -Werror: these compile in seconds and a warning in firmware
	# logic is worth stopping for. The Zephyr build already runs with
	# -Wall, so anything caught here would have been caught there anyway --
	# but here it is caught before a nine-minute build.
	# shellcheck disable=SC2086
	# $cflags AFTER the sources. It carries -lm, and GNU ld on Ubuntu runs
	# with --as-needed: a library named before the objects that need it is
	# dropped, and usage_contrast failed to link with an undefined pow().
	# Apple's linker does not care about the order, which is why it passed
	# on the machine it was written on.
	if ! $CC -Wall -Werror -I "$SRC" "$src" $objs $cflags \
		-o "$WORK/$name" 2>"$WORK/$name.cc"; then
		printf '  %-14s BUILD FAILED\n' "$name"
		sed 's/^/      /' "$WORK/$name.cc"
		failures=$((failures + 1))
		return
	fi

	if "$WORK/$name" >"$WORK/$name.out" 2>&1; then
		n=$(grep -c '^PASS' "$WORK/$name.out" || true)
		# A suite that ran nothing is not a suite that is fine, and
		# until now this runner could not tell the two apart. Two of
		# them printed only on failure, so they reported "ok (0
		# checks)" and their whole main() could be deleted without
		# changing a byte of this output -- which is how "the sleep
		# logic is covered" stayed true on paper while nothing
		# enforced it. Exiting zero is the easiest thing in the world
		# for broken code to do; asserting something is not. Every
		# suite here has checks, so zero means they stopped running.
		if [ "$n" -eq 0 ]; then
			printf '  %-14s NO CHECKS RAN\n' "$name"
			failures=$((failures + 1))
			return
		fi
		printf '  %-14s ok (%s checks)\n' "$name" "$n"
	else
		printf '  %-14s FAILED\n' "$name"
		grep '^FAIL' "$WORK/$name.out" | sed 's/^/      /' || true
		failures=$((failures + 1))
	fi
}

echo "== firmware host tests"
run_one backlight    ""                ""
run_one bootanim     "bootanim_dec.c"  ""
run_one fmt          "fmt.c"           ""
run_one logo         "logo_parse.c bootanim_dec.c" ""
run_one msg_parse    "msg_parse.c"     ""
run_one oauth        "oauth.c"         "-DOAUTH_HOST_TEST"
run_one ota_parse    "ota_parse.c"     ""
run_one sleep_gate   "sleep_gate.c usage_freshness.c" ""
run_one ui_slide_geom ""               ""
run_one ui_swipe_geom ""               ""
run_one upd_prompt  "upd_prompt.c"    ""
run_one upd_row     "upd_row.c"       ""
run_one upd_tap     "upd_tap.c"       ""
run_one whatsnew    "whatsnew.c ota_parse.c" ""
run_one usage_contrast ""              "-lm"
run_one usage_freshness "usage_freshness.c" ""
run_one usage_layout ""                ""
run_one usage_parse  "usage_parse.c"   ""
run_one usage_state  "usage_state.c"   ""

if [ "$failures" -ne 0 ]; then
	printf '\n%d host test(s) failed\n' "$failures" >&2
	exit 1
fi
echo "PASS [host tests]"
