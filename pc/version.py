"""The version of this release, and the version of the wire protocol.

Two numbers, deliberately separate, because they answer different questions.

RELEASE_VERSION is the product's. Firmware and daemon ship as one release from
one tag -- tools/release.sh builds blink-fw.bin and .github/workflows/
release-binaries.yml builds the four binaries from that same commit -- so they
carry the same number and a mismatch means somebody's install is half-updated.
That is the number a customer sees and the one support asks for.

PROTO_VERSION is the link's, and it moves far more slowly. Protocol changes are
additive: new fields, ignored by any peer that does not know them (the
firmware's msg_get_* already skips unknown keys, and json.loads never minds).
So this only increments for a change that genuinely breaks an older peer, which
should be close to never. It is a floor, not a format selector.

The consequence worth stating: PROTO_VERSION is what refuses, RELEASE_VERSION
is what advises. A board whose firmware needs a protocol this daemon does not
speak gets no update offered at all. A board merely newer than the daemon keeps
working and says so on the settings screen.

tests/ci/check_versions.sh pins both against firmware/src/version.h.
"""

RELEASE_VERSION = "1.3.2"
PROTO_VERSION = 2
