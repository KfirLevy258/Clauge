#!/bin/bash
# Flash a Blink CYD **whose flash-encryption eFuses are burned** (the original
# unit, fused 2026-07-17). Such a chip boots only images encrypted with its
# device key: a plain `west flash` writes plaintext the ROM cannot read and the
# board sits dead until re-flashed with THIS script. (The board still works;
# only the flashing path changed.)
#
# NOT every CYD is fused. Pointed at a plaintext board this script writes
# ciphertext the ROM reads as garbage -- "invalid header" and a boot loop until
# it is re-flashed in plaintext (2026-08-10, board a4f00f5e7b14). Nothing is
# bricked and no eFuse is burned by the mistake, but a flashing cycle is lost,
# so the script now reads FLASH_CRYPT_CNT and refuses a chip that is not fused.
#
# Key: ~/.blink/flash_key.bin (override with BLINK_FLASH_KEY). The eFuse
# copy is sealed inside the chip and unreadable -- the file is the only usable
# copy in the world. KEEP A BACKUP OFF THIS DISK. No key file = no future
# updates, ever.
#
# Build first (sysbuild + MCUboot, also mandatory since the same date):
#   west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
#     -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0
#
# Usage: tools/flash_encrypted.sh [--logo FILE.bin | --keep-logo] [port]
#   (port defaults to the first /dev/cu.usbserial*; opening it resets the
#   board -- stop any logger first)
#
# The company logo partition (firmware/src/logo_parse.h): --logo writes a
# .bin built by tools/encode_logo.py, encrypted like the other two images;
# with neither flag the partition is ERASED so the unit is an individual one,
# same as tools/burn.sh. --keep-logo leaves it alone -- for a dev board that
# is re-flashed many times a day and should keep the logo it has.
set -euo pipefail

LOGO=""
KEEP_LOGO=0
while [ $# -gt 0 ]; do
	case "$1" in
	--logo) LOGO="$2"; shift 2 ;;
	--keep-logo) KEEP_LOGO=1; shift ;;
	*) break ;;
	esac
done
# Must match logo_partition in firmware/boards/esp32_devkitc_esp32_procpu.overlay.
LOGO_OFF=0x330000
LOGO_SIZE=0x80000

PORT="${1:-$(ls /dev/cu.usbserial* 2>/dev/null | head -1 || true)}"
KEY="${BLINK_FLASH_KEY:-$HOME/.blink/flash_key.bin}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Overridable so a diagnostic image (build-trace) can be flashed without
# overwriting the release artifacts in build-sb -- which are what a restore
# flashes back. Unset behaves exactly as before.
BUILD="${BLINK_BUILD_DIR:-$(cd "$HERE/.." && pwd)/firmware/build-sb}"
ETOOLS="${BLINK_ETOOLS:-/Library/Frameworks/Python.framework/Versions/3.10/bin}"

[ -f "$KEY" ] || { echo "FATAL: flash key missing at $KEY -- no key, no flashing (see firmware/README security section)"; exit 1; }
[ -n "${PORT}" ] || { echo "FATAL: no /dev/cu.usbserial* port found"; exit 1; }
[ -f "$BUILD/mcuboot/zephyr/zephyr.bin" ] || { echo "FATAL: no sysbuild output in $BUILD -- build first (see header)"; exit 1; }
[ -z "$LOGO" ] || [ -f "$LOGO" ] || { echo "FATAL: --logo $LOGO: no such file"; exit 1; }

# Refuse a chip that cannot boot what we are about to write. Detection is
# shared with tools/dev.sh, which needs the mirror check.
if [ "${BLINK_SKIP_EFUSE_CHECK:-0}" != "1" ]; then
	# shellcheck source=lib_efuse.sh
	. "$HERE/lib_efuse.sh"
	efuse_probe "$PORT" "$ETOOLS"
	case "$EFUSE_STATE" in
	encrypted)
		;;	# what this script is for
	plaintext)
		{
			echo "FATAL: flash encryption is NOT enabled on this chip (FLASH_CRYPT_CNT = 0b$EFUSE_BITS)."
			echo "       ${EFUSE_MAC:+MAC $EFUSE_MAC -- }this is a plaintext-booting board, not the encrypted unit."
			echo "       Writing encrypted images here yields 'invalid header' and a boot loop."
			echo "       Flash it in plaintext instead (two commands -- west flash writes only"
			echo "       the app at 0x20000 and leaves MCUboot at 0x1000 untouched):"
			echo "         west flash -d $BUILD --esp-device $PORT --esp-baud-rate 115200"
			echo "         $ETOOLS/esptool.py --port $PORT --baud 115200 write_flash \\"
			echo "             0x1000 $BUILD/mcuboot/zephyr/zephyr.bin"
			echo "       Override with BLINK_SKIP_EFUSE_CHECK=1 if you are certain."
		} >&2
		exit 1
		;;
	*)
		echo "FATAL: cannot tell whether this chip is fused -- $EFUSE_REASON" >&2
		echo "       Refusing to flash blind. BLINK_SKIP_EFUSE_CHECK=1 overrides." >&2
		exit 1
		;;
	esac
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

enc() { # address, plaintext-in, encrypted-out
	"$ETOOLS/espsecure.py" encrypt_flash_data --keyfile "$KEY" \
		--flash_crypt_conf 0xf --address "$1" -o "$3" "$2"
}

# A CONFIRMED image, not zephyr.signed.bin. The signed artifact's trailer says
# "boot this once" -- correct for OTA, where main.c confirms the image after it
# proves itself, and wrong for a direct flash, where the board has no daemon and
# no network at the bench, never becomes healthy, and reverts at 90 s. See
# tools/sign_confirmed.py.
APP_CONFIRMED="$BUILD/firmware/zephyr/zephyr.confirmed.bin"
rm -f "$APP_CONFIRMED"
"${BLINK_PYTHON:-python3}" "$HERE/sign_confirmed.py" \
	--build-dir "$BUILD" --out "$APP_CONFIRMED" || {
	echo "FATAL: could not sign a confirmed image. Flashing the unconfirmed" >&2
	echo "       one produces a unit that reverts 90 s after it boots." >&2
	exit 1; }

# Offsets match the sysbuild runners: MCUboot at 0x1000, signed app in slot0
# at 0x20000. The address is part of the cipher tweak -- never shuffle these.
enc 0x1000  "$BUILD/mcuboot/zephyr/zephyr.bin" "$TMP/mcuboot.enc"
enc 0x20000 "$APP_CONFIRMED"                   "$TMP/app.enc"

# 115200: 921600 fails on this CH340.
if [ -n "$LOGO" ]; then
	enc "$LOGO_OFF" "$LOGO" "$TMP/logo.enc"
	"$ETOOLS/esptool.py" --port "$PORT" --baud 115200 write_flash \
		0x1000 "$TMP/mcuboot.enc" 0x20000 "$TMP/app.enc" \
		"$LOGO_OFF" "$TMP/logo.enc"
	echo "Flashed encrypted, with the company logo."
elif [ "$KEEP_LOGO" = 1 ]; then
	"$ETOOLS/esptool.py" --port "$PORT" --baud 115200 write_flash \
		0x1000 "$TMP/mcuboot.enc" 0x20000 "$TMP/app.enc"
	echo "Flashed encrypted; logo partition left as it was."
else
	"$ETOOLS/esptool.py" --port "$PORT" --baud 115200 erase_region \
		"$LOGO_OFF" "$LOGO_SIZE"
	"$ETOOLS/esptool.py" --port "$PORT" --baud 115200 write_flash \
		0x1000 "$TMP/mcuboot.enc" 0x20000 "$TMP/app.enc"
	echo "Flashed encrypted; logo partition erased (individual unit)."
fi
echo "NVS at 0x3b0000 untouched (settings survive)."
