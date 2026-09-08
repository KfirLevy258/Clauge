#!/bin/sh
# Build the `blink` program for THIS platform, as a directory: dist/blink/blink
# (blink.exe on Windows) plus dist/blink/_internal/.
#
# One directory, not one file. A one-file build unpacks 50 MB into a temp
# directory on every run, and macOS scans every file it writes: 5 to 11 s
# before the first line of Python, on `blink status` and everything else
# (2026-08-29). tools/package_binary.py turns the directory into the archive
# the feed serves.
#
#   tools/build_binary.sh [outdir]
#
# PyInstaller cannot cross-compile, so each platform's binary is built on that
# platform -- see .github/workflows/release-binaries.yml, which does exactly
# this on a macOS and a Linux runner and attaches the results to the release.
#
# The customer needs none of this. It exists so they need none of it: no
# Python, no virtualenv, no pip, no PyPI at install time.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
OUT="${1:-$ROOT/dist}"
BUILD="${TMPDIR:-/tmp}/blink-build"

# Find an interpreter that RUNS, not merely one that is on PATH.
#
# On a Windows box without the Store's Python, `python3` is a stub in
# %LOCALAPPDATA%\Microsoft\WindowsApps that exists, satisfies `command -v`,
# and then exits "Permission denied" -- it is there to open the Store, not to
# run code. The real interpreter on such a machine is `python`. Measured on
# the Windows release desk, 2026-09-06: `command -v python3` succeeded, the
# build then died on line 27, and `python` was 3.11.2 and healthy.
#
# So each candidate is executed, not just located, and the first that answers
# with a new enough version wins. `py -3` is the Windows launcher, worth trying
# last because it is absent everywhere else. BLINK_PYTHON is tried first, so a
# machine whose default interpreter is too old can name a better one without
# editing this file -- the same override burn.sh and check_factory.sh take.
#
# "New enough" means 3.10, and the floor is not cosmetic. pc/requirements.txt
# pins esptool==5.3.1, which declares Requires-Python >=3.10, so an older
# interpreter gets through this check and then dies at the pip step with
#
#     ERROR: No matching distribution found for esptool==5.3.1
#
# preceded by a wall of "Ignored the following versions that require a
# different python version" -- a message about esptool that is really about
# the interpreter, and which sends the reader to the pin rather than the
# python. macOS still ships 3.9 as /usr/bin/python3, so a stock Mac is BELOW
# the floor by default: the old test (version_info[0] == 3) accepted it.
# Found 2026-09-09 on a new machine; CI never met it because
# .github/workflows/release-binaries.yml pins setup-python to 3.11.
PY=""
PY_TRIED=""
# The versioned names are the fallback for exactly the macOS case above: the
# bare `python3` is Apple's 3.9 and fails the floor, while a Homebrew or
# python.org install is sitting right there under its own version. Newest
# first, so a machine with several does not build on its oldest.
for cand in ${BLINK_PYTHON:-} python3 python \
            python3.14 python3.13 python3.12 python3.11 python3.10 py; do
	command -v "$cand" >/dev/null 2>&1 || continue
	ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
	PY_TRIED="$PY_TRIED $cand ($ver)"
	"$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
		>/dev/null 2>&1 || continue
	PY="$cand"
	break
done
[ -n "$PY" ] || {
	echo "need python 3.10 or newer to build" >&2
	echo "  tried:${PY_TRIED:- nothing that ran}" >&2
	echo "  pc/requirements.txt pins esptool==5.3.1, which needs >= 3.10." >&2
	echo "  Set BLINK_PYTHON to a newer interpreter, e.g." >&2
	echo "    BLINK_PYTHON=python3.13 sh tools/build_binary.sh" >&2
	exit 1
}

# --clear, because $BUILD is a fixed path under TMPDIR that survives between
# runs, and `venv` over an existing directory does NOT replace what is already
# in bin/. Re-running after a build that used a different interpreter leaves
# the OLD bin/python in place while rewriting pyvenv.cfg to name the new one:
# the config says 3.13 and the venv runs 3.9. Observed 2026-09-09, where the
# symptom was a second identical "No matching distribution found for
# esptool==5.3.1" after the interpreter selection above had already been
# fixed -- the fix was correct and the stale venv was hiding it.
"$PY" -m venv --clear "$BUILD" >/dev/null
# A Windows venv puts its executables in Scripts/, not bin/. This script runs
# under Git Bash there, so the path style is the only difference that matters.
VBIN="$BUILD/bin"
[ -d "$VBIN" ] || VBIN="$BUILD/Scripts"
"$VBIN/python" -m pip install --quiet --upgrade pip
# The daemon's own pinned dependencies get frozen INTO the binary, so the
# customer's install can no longer drift with whatever PyPI serves that day.
"$VBIN/python" -m pip install --quiet pyinstaller -r "$ROOT/pc/requirements.txt"

# Packaging and test machinery that PyInstaller collects on its own and this
# program never imports. It only ever showed up on Linux -- 323 modules the
# macOS build did not have, led by setuptools at 1.1 MB -- which is why the
# Linux download was twice the size of the others for the same program.
#
# esptool imports bitstring, which picks its backend by importlib at runtime
# -- invisible to PyInstaller's analysis, so bitstring and bitarray are
# collected whole. Its other backend, tibs, is only chosen behind an
# environment variable nobody sets, so it stays out (0.9 MB).
#
# espefuse and espsecure ship in the same wheel and are deliberately NOT
# here. The daemon used espefuse to read one eFuse bit before a flash, and
# espefuse imports espsecure, which imports `cryptography`: 12 MB of native
# code, a third of the whole download, for that one bit (2026-08-30).
# pc/efuse_probe.py reads it through esptool's own chip class instead.
# Excluded by name so a future esptool that happens to import one of them
# fails here, at build time, not by quietly doubling the download.
#
# Nothing under pc/ imports any of these, and neither does esptool, pyserial
# or ecdsa (checked against the pinned versions). asyncio and multiprocessing
# are also collected and also unused, but they are left in: together they are
# 0.4 MB, and unlike the list below they are plausible lazy imports for some
# future dependency, where a wrong exclusion surfaces as a crash on a
# customer's machine rather than at build time.
set -- \
	--exclude-module setuptools \
	--exclude-module pkg_resources \
	--exclude-module distutils \
	--exclude-module packaging \
	--exclude-module unittest \
	--exclude-module doctest \
	--exclude-module pydoc \
	--exclude-module tkinter \
	--exclude-module espefuse \
	--exclude-module espsecure \
	--exclude-module cryptography \
	--exclude-module tibs

# The Linux libpython ships with its debug symbols: 23 MB unstripped, against
# 7 MB for the macOS framework, which Apple strips before shipping. Stripping
# it costs nothing a customer can observe -- there is no debugger on the other
# end of this download -- and is most of the remaining difference.
#
# Linux only. PyInstaller warns that --strip can produce unusable binaries on
# macOS, where it would also invalidate a code signature, and there is nothing
# for it to do on Windows.
if [ "$(uname -s)" = "Linux" ]; then
	set -- "$@" --strip
fi

# The CH340 driver, on Windows only. macOS and Linux drive the chip out of
# the kernel and would be carrying a Windows .sys around for nothing.
#
# Staged by tools/fetch_ch340_driver.sh, which is a deliberate manual step --
# read the comment at the top of it before wondering why this is not
# downloaded here. A build without it is supported and is what every machine
# produces by default: pc/win_driver.py finds no package and says so.
# A native C:\... path and a ";" separator, unlike every other --add-data
# here. Both were arrived at the hard way on the release desk (2026-09-06):
#
#   - Handing Git Bash the /c/Users/... form produced
#     \c\Users\...\vendor\ch341ser -- slashes flipped, drive letter never
#     mapped. It rewrites arguments that look like paths on their way to a
#     native program, and gets this one wrong. A path already beginning
#     "C:\" is left alone, which is what cygpath is for.
#   - Making it relative instead did not help: PyInstaller resolves a
#     relative source against the SPEC directory, which --specpath puts in
#     $BUILD, not against the working directory.
#
# The lines above survive on ":" only because their destination is "." --
# enough for Git Bash to recognise a path list and map the drive properly.
case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
	if [ -f "$ROOT/vendor/ch341ser/CH341SER.INF" ]; then
		DRIVER_WIN=$(cygpath -w "$ROOT/vendor/ch341ser")
		set -- "$@" --add-data "${DRIVER_WIN};drivers/ch341ser"
		echo "bundling the CH340 driver from $DRIVER_WIN"
	elif [ -n "${BLINK_ALLOW_NO_DRIVER:-}" ]; then
		echo "no CH340 driver, and BLINK_ALLOW_NO_DRIVER is set: this build"
		echo "  will tell customers to install it by hand"
	else
		# Fail, rather than quietly producing a Windows build that cannot
		# set up a board. The driver is committed under vendor/ch341ser, so
		# its absence means something is wrong with this checkout -- and the
		# only symptom downstream is one line of `blink install` output,
		# which is far too easy to miss on a release.
		echo "FATAL: no CH340 driver at $ROOT/vendor/ch341ser" >&2
		echo "  A Windows build without it cannot set up a customer's board." >&2
		echo "  See vendor/ch341ser/README.md. To build anyway:" >&2
		echo "    BLINK_ALLOW_NO_DRIVER=1 $0" >&2
		exit 1
	fi
	;;
esac

cd "$ROOT"
"$VBIN/pyinstaller" \
	--onedir \
	--contents-directory _internal \
	--name blink \
	--distpath "$OUT" \
	--workpath "$BUILD/work" \
	--specpath "$BUILD" \
	--add-data "$ROOT/tools/blink-statusline.sh:." \
	--add-data "$ROOT/tools/blink-hook.sh:." \
	--collect-all esptool \
	--collect-all bitstring \
	--collect-all bitarray \
	--hidden-import claude_usage_bridge \
	--hidden-import pc.efuse_probe \
	--hidden-import ecdsa \
	--collect-all certifi \
	--hidden-import serial.tools.list_ports \
	"$@" \
	--noconfirm --clean \
	blink_main.py >"$BUILD/pyinstaller.log" 2>&1 || {
		tail -30 "$BUILD/pyinstaller.log" >&2
		echo "FATAL: build failed; full log at $BUILD/pyinstaller.log" >&2
		exit 1
	}

BUILT="$OUT/blink/blink"
[ -f "$BUILT" ] || BUILT="$OUT/blink/blink.exe"
[ -f "$BUILT" ] || { echo "FATAL: no executable under $OUT/blink" >&2; exit 1; }

# --collect-all bitarray brings its own test suite and a C header along with
# the backend. 0.4 MB that no customer will ever run or compile.
rm -f "$OUT"/blink/_internal/bitarray/test_*.py \
	"$OUT"/blink/_internal/bitarray/*.h

# Nothing the daemon runs may pull these back in: a customer's download
# would double, and the first sign would be the release's size, if anyone
# looked. See the exclusions above.
for gone in cryptography espefuse espsecure tibs; do
	if [ -e "$OUT/blink/_internal/$gone" ]; then
		echo "FATAL: $gone ended up in the bundle; see the exclusions in $0" >&2
		exit 1
	fi
done
echo "built $BUILT ($(du -sh "$OUT/blink" | cut -f1))"
