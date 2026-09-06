#!/bin/bash
# Build, sign, and publish a Blink firmware release the board can install
# over WiFi. The version comes from firmware/src/version.h -- bump it FIRST.
# Requires: zephyr env NOT needed here (script sources it), `gh auth status` ok.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/.."
REPO="${OTA_REPO:-KfirLevy258/Blink}"
REPO_URL="${OTA_REPO_URL:-https://github.com/$REPO.git}"
TAG="v$(sed -n 's/#define BLINK_FW_VERSION "\(.*\)"/\1/p' "$ROOT/firmware/src/version.h")"

VER="${TAG#v}"
[ -n "$VER" ] || { echo "FATAL: no version in version.h"; exit 1; }
# The tag has to exist on the remote BEFORE the draft is created.
#
# A draft release does not create its git tag -- GitHub only writes the tag
# when the release is published. So a draft for a tag nobody pushed leaves
# release-binaries.yml checking out a ref that does not exist, four jobs deep
# and forty minutes in, with an error that says nothing about tagging.
git -C "$ROOT" ls-remote --exit-code --tags "$REPO_URL" "refs/tags/$TAG" \
	>/dev/null 2>&1 || {
	echo "FATAL: $TAG is not on the remote." >&2
	echo "       The draft release would name a tag that does not exist, and" >&2
	echo "       the binary build checks that tag out. Push it first:" >&2
	echo "         git tag $TAG && git push origin $TAG" >&2
	exit 1; }
# The firmware and the daemon ship from this one tag, so they must already
# agree about what it is. Cheaper to fail here than to publish a release whose
# two halves introduce themselves differently.
sh "$HERE/../tests/ci/check_versions.sh"
PROTO=$(sed -n 's/^#define BLINK_PROTO_VERSION \([0-9][0-9]*\).*$/\1/p' \
	"$ROOT/firmware/src/version.h")
[ -z "$(git -C "$ROOT" status --porcelain)" ] || {
	echo "FATAL: working tree dirty -- releases come from committed code only"; exit 1; }
# The firmware feed lives on the same release as the source tag ($TAG). Refuse
# to overwrite an existing firmware asset -- bump version.h for a new build.
gh release view "$TAG" --repo "$REPO" --json assets \
	-q '.assets[].name' 2>/dev/null | grep -qx blink-fw.bin && {
	echo "FATAL: $TAG already carries blink-fw.bin -- bump version.h"; exit 1; }

source ~/zephyr-v4.4.0/.venv/bin/activate
source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh
KEY="${OTA_SIGNING_KEY:-$HOME/.blink/ota_signing_key_p256.pem}"
[ -f "$KEY" ] || { echo "FATAL: signing key missing at $KEY (set OTA_SIGNING_KEY)"; exit 1; }
# Stamp the real version into the MCUboot image header. Zephyr's default for
# CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION is "0.0.0+0" (it only auto-fills from an
# app VERSION file, which this app does not use -- version.h is the one source
# of truth), so every image ever released before 2026-07-26 carried 0.0.0.
#
# It changes nothing today: swap-using-move is told which image to boot by the
# flag ota.c sets via boot_request_upgrade(), and MCUboot never reads the
# version field in that mode. It matters for DIRECT-XIP, which has no copy and
# therefore no flag, and picks a slot by COMPARING these versions -- with both
# slots claiming 0.0.0 the tie-break is arbitrary and an update can silently
# no-op forever. Stamping it now means the field is already correct whenever
# direct-XIP lands, instead of being a trap waiting at the end of that work.
#
# "firmware" is the sysbuild image name (see build-sb/domains.yaml), not a path.
# Note this is a RELEASE-path fix: a plain `west build` still produces 0.0.0+0,
# which is fine and even desirable -- a dev image should never out-rank a
# released one under direct-XIP.
( cd "$ROOT/firmware" && west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
	-- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
	-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$KEY\"" \
	-Dfirmware_CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="\"$VER\"" )   # inner quotes: Kconfig strings

BIN="$ROOT/firmware/build-sb/firmware/zephyr/zephyr.signed.bin"

# A release must not carry the on-device network path. CONFIG_BLINK_WIFI_MODE
# defaults to n, but a default is not a guarantee: a stray EXTRA_CONF_FILE or a
# sticky build directory flips it silently, and nobody would find out until a
# customer's board signed itself in to Anthropic as Claude Code.
#
# Checks the ARTIFACT, not only the config, because the artifact is what ships.
# If the OAuth path were ever linked in, these strings would be in the image.
CFG="$ROOT/firmware/build-sb/firmware/zephyr/.config"
if grep -q "^CONFIG_BLINK_WIFI_MODE=y" "$CFG"; then
	echo "FATAL: CONFIG_BLINK_WIFI_MODE=y in a release build." >&2
	echo "       That ships the on-device sign-in and the token store." >&2
	exit 1
fi
# `strings` runs inside a pipeline below, where a missing binary would leave
# grep reading an empty stream and every pattern "absent" -- a guard against
# shipping the OAuth path that silently passes because the tool that looks for
# it is not installed.
command -v strings >/dev/null 2>&1 || {
	echo "FATAL: 'strings' not found; cannot check the artifact." >&2
	echo "       Refusing to publish an image nothing has inspected." >&2
	exit 1; }
for pat in "/api/oauth/usage" "refresh_token" "claude-code/"; do
	if strings "$BIN" | grep -qF -- "$pat"; then
		echo "FATAL: release image contains '$pat' -- the network path is" >&2
		echo "       linked in even though the Kconfig symbol looks off." >&2
		exit 1
	fi
done

SIZE=$(stat -f%z "$BIN")
SHA=$(shasum -a 256 "$BIN" | cut -d' ' -f1)
SLOT=$((0x150000))
[ "$SIZE" -le "$SLOT" ] || { echo "FATAL: image $SIZE > slot $SLOT"; exit 1; }

# The OTA artifact must stay UNCONFIRMED -- the opposite of what the factory
# scripts flash (tools/sign_confirmed.py). It lands in slot 1, boots once, and
# firmware/src/main.c confirms it only after WiFi+TLS or the daemon proves the
# image works. A pre-confirmed artifact would take that safety net away: a bad
# release would then be permanent on every board that accepted it, with no
# revert. The size check above already catches --pad, which pads to the full
# slot -- this names the reason, so nobody "fixes" it by padding here too.
[ "$SIZE" -lt "$SLOT" ] || {
	echo "FATAL: $BIN is exactly the slot size -- it looks padded, and a"
	echo "       padded image carries a trailer. The OTA artifact must be"
	echo "       neither padded nor confirmed. See tools/sign_confirmed.py."
	exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$BIN" "$TMP/blink-fw.bin"

# --- publish -----------------------------------------------------------
#
# As a DRAFT first, and only undrafted once everything is attached.
#
# The manifest names the sha256 of four binaries that do not exist yet when
# this script starts: PyInstaller cannot cross-compile, so each platform's
# build happens on that platform in .github/workflows/release-binaries.yml.
# Publishing the firmware first and patching the manifest afterwards would
# leave /latest/download/ serving a half-release -- and drafts are invisible
# there, so this costs nothing but a wait.
#
# The signing also has to happen HERE rather than in the workflow: the release
# key stays on this machine. Putting it in GitHub Secrets would sign the
# artifacts with a key held by the same account that could publish forged ones,
# which is most of the reason for signing gone.
RELKEY="${BLINK_RELEASE_KEY:-$HOME/.blink/release_signing_key_p256.pem}"
[ -f "$RELKEY" ] || { echo "FATAL: release signing key missing at $RELKEY"; exit 1; }

ARTIFACTS="macos-arm64 macos-x86_64 linux-x86_64 windows-x86_64"
# The file each key is served as (pc/update.archive_name): tar.gz, zip on
# Windows. Since 1.1.0 the program is a directory, so an archive.
artifact_file() { case "$1" in windows*) echo "blink-$1.zip" ;; *) echo "blink-$1.tar.gz" ;; esac; }

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
	# Draft it again before adding anything. This branch runs when the tag
	# was already released -- a source-tag release with no firmware on it,
	# or a retry -- and uploading straight onto a PUBLISHED release is the
	# half-release the draft-first flow exists to prevent: the firmware
	# would be live at /latest/download/ for the ~40 minutes before the
	# manifest describing it exists.
	gh release edit "$TAG" --repo "$REPO" --draft=true
	gh release upload "$TAG" --repo "$REPO" --clobber "$TMP/blink-fw.bin"
else
	# Notes written for the people who buy the device, when there are any
	# (docs/releases/<version>.md); the firmware digest otherwise.
	NOTES="$ROOT/docs/releases/$VER.md"
	if [ -f "$NOTES" ]; then
		gh release create "$TAG" --repo "$REPO" --draft --title "BLINK $VER" \
			--notes-file "$NOTES" "$TMP/blink-fw.bin"
	else
		gh release create "$TAG" --repo "$REPO" --draft --title "BLINK $VER" \
			--notes "Firmware $VER — size $SIZE bytes, sha256 $SHA" \
			"$TMP/blink-fw.bin"
	fi
fi

# workflow_dispatch, not the `release: published` trigger -- a draft never
# publishes, which is the point. Needs release-binaries.yml to exist on the
# default branch; that is a GitHub rule about dispatch, not about this tag.
echo "Building the four binaries..."
gh workflow run release-binaries.yml --repo "$REPO" -f tag="$TAG"

# Poll the release rather than chasing a run id: the assets appearing IS the
# condition we care about, and it cannot be confused by a concurrent run.
deadline=$(( $(date +%s) + 2400 ))
while :; do
	have=$(gh release view "$TAG" --repo "$REPO" --json assets \
		-q '.assets[].name' 2>/dev/null || true)
	missing=""
	for k in $ARTIFACTS; do
		echo "$have" | grep -qx "$(artifact_file "$k")" || missing="$missing $k"
	done
	[ -n "$missing" ] || break
	[ "$(date +%s)" -lt "$deadline" ] || {
		echo "FATAL: timed out waiting for:$missing" >&2
		echo "       The release is still a draft; nothing is public." >&2
		exit 1; }
	sleep 20
done

# Hash what was actually published, not what CI said it built. This is the
# whole value of signing locally: the signature covers bytes this machine has
# seen, from the same URL a customer will fetch.
for k in $ARTIFACTS; do
	gh release download "$TAG" --repo "$REPO" -p "$(artifact_file "$k")" -D "$TMP"
done

# The document itself comes from pc/manifest.py, which is the only place its
# shape is written down and the thing tests/pc/test_manifest_contract.py pins.
# It used to be a second definition inline here, free to drift from the one the
# daemon reads.
ROOT="$ROOT" VER="$VER" PROTO="$PROTO" SIZE="$SIZE" SHA="$SHA" TMP="$TMP" \
	ARTIFACTS="$ARTIFACTS" python3 - <<'PYEOF' > "$TMP/manifest.json"
import hashlib, json, os, sys

sys.path.insert(0, os.environ["ROOT"])
from pc import manifest, update

tmp = os.environ["TMP"]
artifacts = {}
for key in os.environ["ARTIFACTS"].split():
    blob = open(os.path.join(tmp, update.archive_name(key)), "rb").read()
    artifacts[key] = {"size": len(blob),
                      "sha256": hashlib.sha256(blob).hexdigest()}

print(json.dumps(manifest.build(
    version=os.environ["VER"],
    fw_size=os.environ["SIZE"],
    fw_sha256=os.environ["SHA"],
    proto=os.environ["PROTO"],
    artifacts=artifacts,
    # The remote brake ships off. Turning it on is a decision made per
    # release, after a real update has been watched end to end.
    auto=False,
), indent=1))
PYEOF

openssl dgst -sha256 -sign "$RELKEY" -out "$TMP/manifest.json.sig" \
	"$TMP/manifest.json"
# Prove the signature verifies with the public half that is compiled into the
# binaries we just built, before anyone can download it.
python3 - "$ROOT" "$TMP/manifest.json" "$TMP/manifest.json.sig" <<'PYEOF'
import sys
# Explicit root: the firmware build left us in a different directory, and
# picking pc/ up from the cwd would work here and nowhere else.
sys.path.insert(0, sys.argv[1])
from pc import update
raw = open(sys.argv[2], "rb").read()
sig = open(sys.argv[3], "rb").read()
if not update.verify_signature(raw, sig):
    sys.exit("FATAL: the manifest does not verify against the shipped public key")
print("manifest signature verifies")
PYEOF

gh release upload "$TAG" --repo "$REPO" --clobber \
	"$TMP/manifest.json" "$TMP/manifest.json.sig"
# BLINK_RELEASE_DRAFT=1 stops here, with everything attached and signed but
# nothing public. That is how a release gets watched end to end on a real
# board first: `gh release download $TAG -D some-dir` and run the daemon with
# BLINK_OTA_DIR=some-dir. When it has been seen to work:
#   gh release edit $TAG --draft=false
if [ "${BLINK_RELEASE_DRAFT:-0}" = "1" ]; then
	echo "Draft $TAG is complete and signed; left unpublished (BLINK_RELEASE_DRAFT=1)."
	echo "Publish with: gh release edit $TAG --repo $REPO --draft=false"
	exit 0
fi
gh release edit "$TAG" --repo "$REPO" --draft=false
echo "Released $TAG ($SIZE bytes) with binaries for:$( for k in $ARTIFACTS; do printf ' %s' "$k"; done)"
echo "Boards pick it up on their next check."
