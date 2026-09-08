#!/usr/bin/env bash
# Dev loop for the CYD board.
#
#   tools/dev.sh flash   -- stop daemon, build, flash, restart daemon
#   tools/dev.sh up      -- start the daemon in the background
#   tools/dev.sh down    -- stop the daemon (frees the port)
#   tools/dev.sh log     -- tail the daemon log
#
# Only ONE process may own the serial port, so the daemon must be down while
# esptool talks to the board. `flash` sequences that for you.
set -euo pipefail

PORT="${PORT:-$(ls /dev/cu.usbserial* 2>/dev/null | head -1 || true)}"
BOARD="esp32_devkitc/esp32/procpu"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="/tmp/claude-usage-bridge.log"
PIDFILE="/tmp/claude-usage-bridge.pid"

activate() {
	# shellcheck disable=SC1090
	source "$ROOT/tools/lib_zephyr.sh"
	blink_zephyr_activate || exit 1
}

down() {
	if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
		kill "$(cat "$PIDFILE")" 2>/dev/null || true
		sleep 1
	fi
	pkill -f claude_usage_bridge.py 2>/dev/null || true
	rm -f "$PIDFILE"
	sleep 0.5
	if lsof "$PORT" >/dev/null 2>&1; then
		echo "warning: $PORT still held by:"
		lsof "$PORT"
	fi
}

up() {
	activate
	cd "$ROOT"
	nohup python3 -u claude_usage_bridge.py --port "$PORT" >"$LOG" 2>&1 &
	echo $! >"$PIDFILE"
	sleep 2
	echo "daemon up (pid $(cat "$PIDFILE")), logging to $LOG"
	tail -n 5 "$LOG" || true
}

case "${1:-flash}" in
flash)
	down
	# dev.sh writes PLAINTEXT. A fused chip's ROM cannot read that, and the
	# board goes dark until tools/flash_encrypted.sh restores it -- the exact
	# mirror of the mistake that script refuses. Ask the chip, before the build.
	if [ "${BLINK_SKIP_EFUSE_CHECK:-0}" != "1" ]; then
		# shellcheck source=lib_efuse.sh
		. "$ROOT/tools/lib_efuse.sh"
		efuse_probe "$PORT"
		case "$EFUSE_STATE" in
		plaintext)
			;;	# what this script is for
		encrypted)
			{
				echo "FATAL: this chip is FUSED (FLASH_CRYPT_CNT = 0b$EFUSE_BITS${EFUSE_MAC:+, MAC $EFUSE_MAC})."
				echo "       dev.sh writes plaintext, which a fused ROM cannot read -- the board"
				echo "       would go dark until re-flashed. Use the encrypted path instead:"
				echo "         cd $ROOT/firmware && west build --sysbuild -d build-sb -b $BOARD . \\"
				echo "           -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \\"
				# Single-quoted so the key path reaches the user's shell unexpanded,
				# and the nested quotes survive: sysbuild needs the value quoted, and
				# a build that omits this silently signs with MCUboot's bundled dev
				# key -- the board boots, but every later OTA is rejected and reverts.
				echo '             -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.blink/ota_signing_key_p256.pem\"" \'
				echo "           && $ROOT/tools/flash_encrypted.sh $PORT"
				echo "       If this is one of the two PILOT boards, add to that build:"
				echo "           -DEXTRA_CONF_FILE=pilot.conf"
				echo "       (their display module differs; a stock build renders them"
				echo "        mirrored, red/blue swapped and dim. Judge per board -- being"
				echo "        fused is NOT the same thing as being a pilot.)"
				echo "       BLINK_SKIP_EFUSE_CHECK=1 overrides."
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
	activate   # sources zephyr-env.sh too
	cd "$ROOT/firmware"
	west build -b "$BOARD" . -- -DUSE_CCACHE=0 | tail -3
	# 921600 is beyond what this board's CH340 tolerates; it fails mid-write.
	west flash --esp-device "$PORT" --esp-baud-rate 115200 | tail -1
	up
	;;
up) down; up ;;
down) down; echo "daemon down, $PORT free" ;;
log) tail -f "$LOG" ;;
*) echo "usage: $0 {flash|up|down|log}"; exit 1 ;;
esac
