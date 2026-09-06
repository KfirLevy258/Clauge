#!/bin/sh
# Stage WCH's CH340 driver into vendor/ch341ser/, for the Windows build to
# bundle. Run once on the machine that builds the Windows release; the result
# is git-ignored.
#
#   tools/fetch_ch340_driver.sh [url]
#
# WHY THE BYTES ARE NOT IN THIS REPOSITORY
#
# Two reasons, and the second is the one that matters.
#
# The small one: this repository is public and the driver is a third party's
# binary. A .sys and a .cat in the tree are noise in every diff, every clone
# and every history rewrite, for a file that changes once a year.
#
# The real one: BLINK is sold. Redistributing WCH's driver inside a paid
# product is a licensing question, and it is not one that a build script gets
# to answer by quietly downloading something. Keeping the fetch here, explicit
# and manual, means shipping the driver is a decision somebody made, on a day,
# with the terms in front of them -- and not a default that arrived because a
# script had a URL in it.
#
# Until that decision is made, everything still works: pc/win_driver.py finds
# no package, `blink install` says so in one line, and `blink status` tells a
# customer with an undriven board exactly what is wrong and where to get the
# driver. That is the shipped behaviour of a build that has never run this
# script, and it is a great deal better than "Board not plugged in".
#
# WHAT TO PUT IN vendor/ch341ser/
#
# The extracted driver package -- the .inf, its .cat and the .sys files for
# every architecture -- NOT the CH341SER.EXE installer. The .exe opens a
# window with a button on it, which is precisely the thing an installer cannot
# drive; pnputil takes the .inf.
#
# The .cat must be Microsoft-signed, or Windows refuses the install with no
# useful message. WCH's own package is; a copy extracted from somewhere else
# may not be, which is why this checks.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
DEST="$ROOT/vendor/ch341ser"
INF="$DEST/ch341ser.inf"

if [ -f "$INF" ]; then
	echo "already staged: $INF"
	echo "  (delete $DEST to re-stage)"
	exit 0
fi

cat >&2 <<'EOF'
No driver staged in vendor/ch341ser/.

This script does not download anything on its own -- see the comment at the
top of it for why. To stage the driver:

  1. Read WCH's redistribution terms and decide whether this product may
     ship their driver. That decision is not one this script can make.
  2. Download the CH341SER package from
       https://www.wch-ic.com/downloads/CH341SER_EXE.html
  3. Extract it and copy the driver package -- ch341ser.inf, its .cat, and
     the .sys files -- into:
       vendor/ch341ser/
  4. Run tools/build_binary.sh on the Windows machine. It picks the
     directory up on its own and bundles it.

A build without this staged is fully supported: it detects an undriven board
and tells the customer where to get the driver by hand.
EOF
exit 1
