# Shared flash-encryption detection, sourced by the scripts that write flash.
#
# Both directions destroy a working boot: encrypted images on an unfused chip,
# or plaintext on a fused one. Either leaves the board looping on "invalid
# header" until it is re-flashed the other way (nothing is bricked -- flashing
# burns no eFuses). So whatever opens the serial port asks the chip first.
#
#   efuse_probe <port> [etools_dir]
#     always returns 0; sets EFUSE_STATE to encrypted|plaintext|unknown,
#     plus EFUSE_BITS, EFUSE_MAC and (when unknown) EFUSE_REASON. Reboots the
#     chip back into its application before returning -- see efuse_release.
#
# Written defensively for `set -euo pipefail` callers: a grep that matches
# nothing must not abort the script before it can explain itself.

# BLINK_ETOOLS overrides the directory outright, and must keep doing so:
# tests/ci/check_factory.sh points it at stubs so the scripts that source this
# can run against no chip at all. Nothing below may outrank it.
#
# Failing that, this used to name /Library/Frameworks/Python.framework/-
# Versions/3.10/bin and nothing else. That was one machine's Python, and on any
# other one efuse_probe reported "espefuse.py not found" -- which burn.sh
# correctly turns into "cannot tell whether this chip is fused. Refusing to
# flash blind." So the factory script did not misflash anything; it simply
# could not burn a unit at all on a new machine, and said so in a sentence
# about the chip rather than about a missing tool. Found 2026-09-09 on a
# replacement Mac, on the first real burn attempted there.
#
# So look for a directory that actually holds espefuse.py. The old framework
# path stays last: it is still right on the machine it was written for.
efuse_default_etools() {
	local d
	if [ -n "${BLINK_ETOOLS:-}" ]; then
		printf '%s\n' "$BLINK_ETOOLS"
		return 0
	fi
	# Beside the interpreter the caller chose, if it named one. pip installs
	# espefuse.py next to the python it belongs to, so this is where a repo
	# venv's copy lives.
	if [ -n "${BLINK_PYTHON:-}" ]; then
		d=$(dirname -- "$BLINK_PYTHON")
		[ -f "$d/espefuse.py" ] && { printf '%s\n' "$d"; return 0; }
	fi
	# Then whatever is on PATH -- an esptool installed deliberately wins over
	# any guess this file could make.
	d=$(command -v espefuse.py 2>/dev/null) && [ -n "$d" ] && {
		printf '%s\n' "$(dirname -- "$d")"; return 0; }
	printf '%s\n' "/Library/Frameworks/Python.framework/Versions/3.10/bin"
}
EFUSE_DEFAULT_ETOOLS="$(efuse_default_etools)"

# espefuse leaves the chip sitting in the ROM download bootloader: it has
# --before but no --after (checked against 5.1.0), and its teardown is a bare
# port close. A probe that is not immediately followed by a flash would then
# leave the board dark with no firmware running -- dev.sh builds for minutes
# after probing, and aborts entirely if the build fails. esptool boots what is
# already in flash; a caller that does go on to flash re-enters download mode
# by itself, so this costs a couple of seconds and nothing else.
efuse_release() {
	local port="$1"
	local etools="${2:-$EFUSE_DEFAULT_ETOOLS}"

	[ -f "$etools/esptool.py" ] || return 0
	"$etools/esptool.py" --port "$port" --after hard-reset run >/dev/null 2>&1 || true
	return 0
}

efuse_probe() {
	local port="$1"
	local etools="${2:-$EFUSE_DEFAULT_ETOOLS}"
	local summary bits ones

	EFUSE_STATE="unknown"; EFUSE_BITS=""; EFUSE_MAC=""; EFUSE_REASON=""

	if [ ! -f "$etools/espefuse.py" ]; then
		EFUSE_REASON="espefuse.py not found at $etools"
		return 0
	fi

	# Assign separately from `local` so the exit status is the command's, not
	# local's (which is always 0 and would swallow the failure).
	summary="$("$etools/espefuse.py" --port "$port" summary 2>&1)" || {
		EFUSE_REASON="espefuse.py could not read $port -- is the port free? A logger or the usage daemon may hold it."
		return 0
	}

	# From here the chip is in download mode no matter which way we exit below.
	efuse_release "$port" "$etools"

	# `|| true` on both: a miss is a legitimate outcome to report, not an exit.
	bits="$(printf '%s\n' "$summary" | grep -m1 '^FLASH_CRYPT_CNT' | sed -nE 's/.*\(0b([01]+)\).*/\1/p' || true)"
	EFUSE_MAC="$(printf '%s\n' "$summary" | grep -oiE '([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -1 || true)"

	if [ -z "$bits" ]; then
		EFUSE_REASON="could not parse FLASH_CRYPT_CNT from espefuse output -- refusing to guess"
		return 0
	fi

	EFUSE_BITS="$bits"
	ones="$(printf '%s' "$bits" | tr -cd '1' | wc -c | tr -d ' ')"
	# Espressif's rule: encryption is on when an ODD number of the counter's
	# bits are set. 0 is a never-fused chip; an even non-zero count means
	# encryption was enabled and then disabled again.
	if [ $((ones % 2)) -eq 1 ]; then
		EFUSE_STATE="encrypted"
	else
		EFUSE_STATE="plaintext"
	fi
	return 0
}
