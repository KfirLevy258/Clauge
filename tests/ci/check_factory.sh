#!/bin/sh
# The factory scripts, run for real against a board that does not exist.
#
#   tests/ci/check_factory.sh
#
# tools/burn.sh and tools/flash_encrypted.sh are the two programs that decide
# what a unit IS -- its edition, whether it is a company unit, whether it can
# ever take an update -- and until this existed nothing ran them except a
# person with a board on the desk. This runs the real scripts, unmodified,
# with esptool/espefuse/espsecure replaced by stubs that record their calls
# (tests/ci/fakes/bin) and pyserial replaced by a scripted board
# (tests/ci/fakes/pyserial) that replays a boot transcript on every reset and
# answers the edition message. What is asserted is what matters on a
# production line: which images went to which addresses, that the logo
# partition is erased when no logo was asked for, and that every wrong answer
# from the board is a FAILED burn rather than a boxed unit.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

PY="${BLINK_PYTHON:-python3}"
"$PY" -c "import numpy, PIL" 2>/dev/null || {
	echo "needs numpy and pillow (pip install -r tests/requirements.txt)" >&2
	exit 1
}

WORK="${TMPDIR:-/tmp}/blink-factory-test"
rm -rf "$WORK"
mkdir -p "$WORK/bin" "$WORK/build/mcuboot/zephyr" "$WORK/build/firmware/zephyr" "$WORK/home"

cp "$ROOT/tests/ci/fakes/bin/"* "$WORK/bin/"
head -c 20000 /dev/zero | tr '\0' 'M' >"$WORK/build/mcuboot/zephyr/zephyr.bin"
head -c 90000 /dev/zero | tr '\0' 'A' >"$WORK/build/firmware/zephyr/zephyr.bin"
head -c 90000 /dev/zero | tr '\0' 'A' >"$WORK/build/firmware/zephyr/zephyr.signed.bin"
head -c 32 /dev/zero >"$WORK/home/flash_key.bin"

# tools/sign_confirmed.py reads the signing parameters out of the build's own
# build.ninja rather than hard-coding them, so the fake build needs one. The
# slot is small here only to keep the padded image quick to write.
mkdir -p "$WORK/build/firmware"
cat >"$WORK/build/firmware/build.ninja" <<NINJA
  POST_BUILD = cd $WORK && $PY $WORK/bin/imgtool.py sign --version 9.9.9 --header-size 0x20 --slot-size 131072 --align 32 --key $WORK/home/signing.pem $WORK/build/firmware/zephyr/zephyr.bin $WORK/build/firmware/zephyr/zephyr.signed.bin
NINJA

FW=$(sed -n 's/^#define BLINK_FW_VERSION "\(.*\)"$/\1/p' "$ROOT/firmware/src/version.h")
[ -n "$FW" ] || fail "cannot read BLINK_FW_VERSION"

# A picture for --logo: a flat rectangle with a mark in it.
"$PY" - "$WORK/acme.png" <<'EOF'
import sys
from PIL import Image
im = Image.new("RGB", (400, 200), (8, 16, 40))
for y in range(60, 140):
    for x in range(100, 300):
        im.putpixel((x, y), (240, 240, 240))
im.save(sys.argv[1])
EOF

export PATH="$WORK/bin:$PATH"
export PYTHONPATH="$ROOT/tests/ci/fakes/pyserial"
export BLINK_ETOOLS="$WORK/bin"
export BLINK_BUILD_DIR="$WORK/build"
export BLINK_FLASH_KEY="$WORK/home/flash_key.bin"
export BLINK_PYTHON="$PY"
export FAKE_TOOL_LOG="$WORK/tools.log"
export FAKE_BOARD_TRANSCRIPT="$WORK/board.txt"
export FAKE_EFUSE_BITS=0000000
unset FAKE_ESPTOOL_FAIL

# What a board prints from reset to the usage screen. `logo` is what the
# firmware reports for the logo partition; `edition` its stamped edition.
board() { # edition logo-line
	cat >"$FAKE_BOARD_TRANSCRIPT" <<EOF
ets Jun  8 2016 00:22:57
*** Booting MCUboot v2.1.0 ***
I (0) boot: Loading image 0
*** Booting Zephyr OS build v4.4.0 ***
{"t":"hello","v":2,"board":"cyd","board_id":"fakefakefake","fw":"$FW","reset":"0x8"}
{"t":"pref","v":2,"provider":"claude"}
[boot] edition: $1
[boot] logo: $2
[usage] mode: USB bridge (no host yet)
EOF
}

# run <expected-exit> <script> args... ; output in $OUT, tool calls in $FAKE_TOOL_LOG
run() {
	want=$1; shift
	: >"$FAKE_TOOL_LOG"
	OUT="$WORK/out.txt"
	set +e
	"$@" >"$OUT" 2>&1
	got=$?
	set -e
	if [ "$got" != "$want" ]; then
		sed 's/^/      /' "$OUT"
		fail "$* exited $got, expected $want"
	fi
}
saw()     { grep -q -- "$1" "$OUT" || { sed 's/^/      /' "$OUT"; fail "expected output: $1"; }; }
called()  { grep -q -- "$1" "$FAKE_TOOL_LOG" || { sed 's/^/      /' "$FAKE_TOOL_LOG"; fail "expected tool call: $1"; }; }
never()   { ! grep -q -- "$1" "$FAKE_TOOL_LOG" || { sed 's/^/      /' "$FAKE_TOOL_LOG"; fail "unexpected tool call: $1"; }; }
BURN="$ROOT/tools/burn.sh"
ENC="$ROOT/tools/flash_encrypted.sh"
PORT=/dev/cu.fake

echo "== burn.sh: arguments"
run 2 "$BURN"
saw "must be claude or codex"
run 2 "$BURN" --edition gemini --port $PORT
saw "must be claude or codex"
run 2 "$BURN" --bogus
saw "usage:"
run 2 "$BURN" --edition claude --port $PORT --logo "$WORK/missing.png"
saw "no such file"
never esptool
ok "refuses a missing edition, an unknown edition and a missing logo before touching anything"

echo "== burn.sh: a fused chip"
# (export, not a prefix assignment: `VAR=x fn` persists in a POSIX shell when
# fn is a function, and did -- every later scenario saw a fused chip.)
board claude none
export FAKE_EFUSE_BITS=0000001
run 1 "$BURN" --edition claude --port $PORT --no-build
export FAKE_EFUSE_BITS=0000000
saw "FUSED"
never "esptool.*write_flash"
ok "refuses to write plaintext to a fused chip"

echo "== burn.sh: an individual unit"
board claude none
export FAKE_BOARD_EDITION_REPLY='[cfg] edition stamped as claude'
run 0 "$BURN" --edition claude --port $PORT --no-build
saw "PASS -- this unit is a claude board (individual)"
saw "logo partition erased"
called "esptool .*erase_region 0x330000 0x80000"
called "esptool .*write_flash 0x1000 $WORK/build/mcuboot/zephyr/zephyr.bin 0x20000 $WORK/build/firmware/zephyr/zephyr.confirmed.bin\$"
# The CONFIRMED image, never the OTA artifact. zephyr.signed.bin boots once
# and reverts at 90 s on a bench board, which undoes the burn minutes after
# this script says PASS.
never "write_flash.*zephyr.signed.bin"
called "imgtool sign .*--pad --confirm "
never "write_flash.*0x330000"
called 'host -> {"t": "edition", "v": 2, "edition": "claude"}'
# The erase comes BEFORE the firmware is written: a failure there must stop
# the burn while the board still runs what it ran before.
[ "$(grep -n 'erase_region' "$FAKE_TOOL_LOG" | cut -d: -f1)" -lt \
  "$(grep -n 'write_flash' "$FAKE_TOOL_LOG" | cut -d: -f1)" ] || fail "erase must precede the flash"
ok "erases the logo partition, writes MCUboot + app, stamps, confirms"

echo "== burn.sh: a company unit"
board codex "company, 3040 bytes, 1 frames, hold 3000 ms"
export FAKE_BOARD_EDITION_REPLY='[cfg] edition stamped as codex'
run 0 "$BURN" --edition codex --port $PORT --no-build --logo "$WORK/acme.png"
saw "PASS -- this unit is a codex board with a company logo"
saw "1 frame(s) @ 1 fps, hold 3000 ms"
[ -s "$WORK/build/logo.bin" ] || fail "logo.bin was not built"
called "write_flash 0x1000 .* 0x20000 .* 0x330000 $WORK/build/logo.bin\$"
never erase_region
"$PY" "$ROOT/tools/encode_logo.py" --info "$WORK/build/logo.bin" >/dev/null || fail "logo.bin does not parse"
ok "encodes the picture, writes it with the firmware in one call, sees it on the board"

echo "== burn.sh: a pre-built logo .bin is written as-is"
board claude "company, 3040 bytes, 1 frames, hold 3000 ms"
export FAKE_BOARD_EDITION_REPLY='[cfg] edition stamped as claude'
cp "$WORK/build/logo.bin" "$WORK/acme.bin"
run 0 "$BURN" --edition claude --port $PORT --no-build --logo "$WORK/acme.bin"
called "0x330000 $WORK/acme.bin\$"
ok "a .bin skips the encoder"

echo "== burn.sh: the board disagrees about the logo"
board claude none
run 1 "$BURN" --edition claude --port $PORT --no-build --logo "$WORK/acme.png"
saw "the logo was flashed but the board does not see it"
saw "FAILED"
board claude "company, 3040 bytes, 1 frames, hold 3000 ms"
run 1 "$BURN" --edition claude --port $PORT --no-build
saw "no logo was flashed but the board still shows one"
ok "a logo the board cannot see, or one it should not have, fails the burn"

echo "== burn.sh: the edition latch"
board codex none
export FAKE_BOARD_EDITION_REPLY='[cfg] edition already stamped as codex; refusing to change it'
run 1 "$BURN" --edition claude --port $PORT --no-build
saw "WRONG EDITION"
run 0 "$BURN" --edition codex --port $PORT --no-build
saw "already this edition -- re-burn, nothing to change"
saw "PASS"
ok "a re-burn of the same edition passes; the other edition fails"

echo "== burn.sh: the firmware on the board is not the one built"
board claude none
export FAKE_BOARD_EDITION_REPLY='[cfg] edition stamped as claude'
sed -i.bak "s/\"fw\":\"$FW\"/\"fw\":\"0.0.1\"/" "$FAKE_BOARD_TRANSCRIPT"
run 1 "$BURN" --edition claude --port $PORT --no-build
saw "the flash did not take, or the build directory is stale"
ok "a stale build directory cannot stamp a unit"

echo "== burn.sh: an image that comes back unconfirmed"
# The one byte this whole path turns on. If --confirm ever stops taking, the
# only symptom in the field is a board that reverts 90 s after a burn that
# said PASS -- so the burn has to refuse the image rather than write it.
board claude none
export FAKE_BOARD_EDITION_REPLY='[cfg] edition stamped as claude'
export FAKE_IMGTOOL_NO_CONFIRM=1
run 1 "$BURN" --edition claude --port $PORT --no-build
unset FAKE_IMGTOOL_NO_CONFIRM
saw "image_ok unset"
never "esptool.*write_flash"
ok "refuses to flash an image whose trailer is not confirmed"

echo "== burn.sh: a flash that dies mid-way"
board claude none
export FAKE_ESPTOOL_FAIL=write_flash
run 1 "$BURN" --edition claude --port $PORT --no-build
unset FAKE_ESPTOOL_FAIL
saw "flash failed"
never "host -> "
ok "no stamp is attempted over a torn image"

echo "== flash_encrypted.sh"
run 1 "$ENC" $PORT
saw "flash encryption is NOT enabled"
never "esptool.*write_flash"
export FAKE_EFUSE_BITS=0000001
run 0 "$ENC" --logo "$WORK/acme.bin" $PORT
saw "with the company logo"
called "espsecure .*--address 0x1000 "
called "espsecure .*--address 0x20000 .*zephyr.confirmed.bin"
never "espsecure .*zephyr.signed.bin"
called "espsecure .*--address 0x330000 "
called "write_flash 0x1000 .*mcuboot.enc 0x20000 .*app.enc 0x330000 .*logo.enc"
run 0 "$ENC" $PORT
saw "logo partition erased"
called "erase_region 0x330000 0x80000"
never "0x330000 .*logo"
run 0 "$ENC" --keep-logo $PORT
saw "left as it was"
never erase_region
never "0x330000"
ok "refuses an unfused chip; encrypts each image at its own address; erases, writes or keeps the logo as asked"

echo "== encode_logo.py: the partition limit"
"$PY" - "$WORK/noise" <<'EOF'
import os, sys
import numpy as np
from PIL import Image
os.makedirs(sys.argv[1], exist_ok=True)
rng = np.random.default_rng(1)
for i in range(4):   # four frames of noise: ~150 KB each, nothing to delta
    Image.fromarray(rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)).save(
        os.path.join(sys.argv[1], f"{i}.png"))
EOF
run 1 "$PY" "$ROOT/tools/encode_logo.py" --frames "$WORK/noise" --out "$WORK/noise.bin"
saw "too big"
[ ! -e "$WORK/noise.bin" ] || fail "an oversize logo must not be written"
ok "a logo that cannot fit is refused, not written"

rm -rf "$WORK"
echo "PASS [factory scripts]"
