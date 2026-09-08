"""Release fetch and serial push, for OTA while the board is tethered.

USB-bridge mode has no network on the board -- run_usb() never starts
net_worker, so the firmware's own HTTPS updater is unreachable and the update
row did nothing there. We have both an internet connection and the board's
serial link, so the split is: we fetch from the same GitHub release feed the
WiFi path uses, and push the image down; the board writes and verifies it with
exactly the same code either way.

Nothing here trusts the transport, but note WHERE that check now lives. Over
WiFi the board hashed what it received. Over USB the board is not part of the
transfer at all -- esptool writes slot0 directly -- so it has no bytes to hash,
and the verification happens on this side instead: pc/bridge.py checks the
download against the manifest's sha256 before calling flash(), and esptool's
own write_flash reads the written region's MD5 back off the chip before it
returns. Both matter more here than they would over HTTPS, because slot0 is
written in place: there is no test boot and no automatic revert to catch a bad
image.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request

# Every process the daemon starts on Windows must be told not to open a
# console window. The daemon itself runs hidden (started by wscript, see
# cli.launcher_path), so a console-subsystem child -- esptool, the eFuse probe, the
# self-test of a downloaded app -- would otherwise be given a brand-new
# console of its own, and a black window appeared on the desktop for the
# 75 s of every firmware flash (user-reported 2026-08-30).
NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

RELEASE_BASE = "https://github.com/KfirLevy258/Blink/releases/latest/download/"
MANIFEST_URL = RELEASE_BASE + "manifest.json"
FIRMWARE_URL = RELEASE_BASE + "blink-fw.bin"



# Serve a locally built release instead of GitHub's. Set BLINK_OTA_DIR to a
# directory holding manifest.json + blink-fw.bin.
#
# This exists because the published feed cannot exercise the update path during
# development: the board usually runs something newer than the latest release,
# so a check can only ever answer "up to date". Pointing this at a local build
# is the only way to test a real transfer without publishing one.
OTA_DIR_ENV = "BLINK_OTA_DIR"


def _local_dir():
    d = os.environ.get(OTA_DIR_ENV)
    return pathlib.Path(d) if d else None


def _ssl_context():
    """The certificates a fetch will trust: the system's, plus certifi's.

    The frozen macOS build carries python.org's OpenSSL, which looks for its
    CA bundle at /Library/Frameworks/Python.framework/Versions/3.X/etc/openssl
    -- a path that exists only on a Mac with that exact Python installed and
    its "Install Certificates" step run. On every other Mac each HTTPS fetch
    failed with CERTIFICATE_VERIFY_FAILED: `blink update` said the feed could
    not be read and the daemon's daily check logged "unreachable", so no
    installed device could ever have updated (2026-08-29, found on the desk
    Mac an hour after 1.0.1 was published; 1.0.0 shipped the same way).

    certifi carries the Mozilla bundle inside the binary, so the fetch no
    longer depends on what the machine happens to have. The system store is
    still loaded first, for a workplace proxy that re-signs traffic with its
    own root. A source checkout without certifi gets the system store alone,
    which is what it always had.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def _get(url, timeout=30):
    local = _local_dir()
    if local is not None:
        return (local / url.rsplit("/", 1)[-1]).read_bytes()
    with urllib.request.urlopen(url, timeout=timeout,
                                context=_ssl_context()) as r:
        return r.read()


def fetch_manifest(get=_get):
    """{"version","size","sha256"} for the latest release, or None."""
    try:
        m = json.loads(get(MANIFEST_URL).decode("utf-8"))
    except Exception as e:
        # Say why. "Unreachable" hid a certificate failure on every macOS
        # install for a day; the reason is the only thing support can act on.
        print(f"[ota] could not read the release feed: {e}", file=sys.stderr)
        return None
    if not all(k in m for k in ("version", "size", "sha256")):
        return None
    return m


def fetch_firmware(get=_get):
    """The release binary. ~1.3 MB, so this blocks for a few seconds."""
    return get(FIRMWARE_URL, timeout=300)


def _parts(v):
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except (AttributeError, ValueError):
        return None


def is_newer(cand, cur):
    """True if `cand` is a strictly later version than `cur`.

    Mirrors ota_version_newer() in the firmware: a malformed version on either
    side means "no", so a bad manifest can never trigger an install.
    """
    a, b = _parts(cand), _parts(cur)
    if a is None or b is None or len(a) != 3 or len(b) != 3:
        return False
    return a > b



# --- writing it to the board -------------------------------------------
#
# esptool against slot0, which is what this project has always done by hand.
# The image is a signed app and 0x20000 is where the bootloader looks, so a
# plain write lands somewhere already bootable -- no MCUboot swap afterwards,
# which is the slower half of an over-the-air update on this hardware.

APP_OFFSET = "0x20000"


def _tool(module, script):
    """How to invoke an esptool-family tool on THIS machine.

    `python -m <module>` first: that works wherever the daemon's own
    interpreter has esptool installed, which is the portable answer and the
    one `pip install esptool` produces. Only if the module is absent do we go
    looking for a standalone script on PATH -- which is how it happened to be
    installed on the machine this was first written on, and relying on that
    would have made the daemon work there and nowhere else.
    """
    if importlib.util.find_spec(module) is not None:
        return [sys.executable, "-m", module]
    for name in (script, script[:-3] if script.endswith(".py") else script):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def _esptool():
    return _tool("esptool", "esptool.py")


def _efuse_probe():
    """How to run pc/efuse_probe.py on THIS machine.

    Frozen, the interpreter is this program and the module is bundled, so it
    is `blink -m pc.efuse_probe`. From source the daemon's cwd is wherever
    launchd or schtasks put it, where `-m pc.efuse_probe` would not resolve,
    so the file is run by path instead.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "-m", "pc.efuse_probe"]
    return [sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "efuse_probe.py")]


def flash_encrypted_chip(port, run=subprocess.run):
    """True if the chip has flash encryption burned, None if we cannot tell.

    Writing a plaintext image to a fused chip produces a board that cannot
    boot until it is flashed the other way -- recoverable, but not something
    to do to someone's device by accident. Two of these boards exist here and
    only one is fused, so this asks the chip rather than assuming.

    None stops the update with "Could not check the chip; not flashing" on the
    board: forty-seven characters about a chip the customer never asked about.
    Everything the probe said about WHY used to be thrown away -- only stdout
    was read, and only for the one line it expected -- so a daemon that could
    not reach the chip logged nothing whatsoever, and a customer's failed
    update was diagnosable only from a photograph of the screen. It is printed
    here instead.
    """
    why = ""
    try:
        proc = run(_efuse_probe() + ["--port", port],
                   capture_output=True, text=True, timeout=60, **NO_WINDOW)
    except Exception as e:
        why = str(e)
    else:
        for line in (proc.stdout or "").splitlines():
            words = line.split()
            if len(words) == 2 and words[0] == "flash_encryption":
                return words[1] == "enabled"
        why = ((proc.stderr or proc.stdout or "").strip()
               or f"exit {proc.returncode} and no answer")

    print(f"[ota] could not read the chip's eFuses on {port}; not flashing."
          f" The probe said: {why[-300:]}", file=sys.stderr)
    return None


# 115200, and no faster.
#
# Measured on the CYD's CH340 on 2026-08-22, and anything faster does not work: three consecutive attempts at 460800 died
# with "Invalid head of packet ... possible serial noise or corruption", each
# time immediately after the baud change. The same 1 MB transfer at 115200
# completed cleanly in 96 s. tools/dev.sh already carried this warning for
# 921600; it turns out to apply well below that.
#
# A failed READ is free. A failed WRITE is not: slot0 is written in place, so a
# transfer that dies halfway leaves an image that will not boot, and the retry
# would have to succeed before the board is usable again. There is nothing to
# win here worth that.
FLASH_BAUD = 115200


def _esptool_run(exe, port, baud, args, run, timeout=900):
    """(ok, last 200 chars of output) for one esptool invocation."""
    try:
        r = run(exe + ["--port", port, "--baud", str(baud)] + args,
                capture_output=True, text=True, timeout=timeout, **NO_WINDOW)
    except Exception as e:
        return False, str(e)[:200]
    if r.returncode == 0:
        return True, ""
    return False, ((r.stderr or r.stdout or "") or "").strip()[-200:]


def flash(port, blob, run=subprocess.run):
    """Write `blob` to the app slot. Returns (ok, message).

    esptool verifies the write itself, by MD5, before it returns.
    """
    exe = _esptool()
    if not exe:
        return False, ("esptool not found -- pip install esptool"
                       " into the interpreter running this daemon")

    # These messages end up on the board's screen, where proto.c keeps 47
    # characters of them: say the thing that fits.
    enc = flash_encrypted_chip(port, run=run)
    if enc is None:
        return False, "Could not check the chip; not flashing"
    if enc:
        # tools/flash_encrypted.sh exists for this and needs the key file;
        # doing it from here would mean handling that key, so decline loudly.
        return False, "Encrypted chip; needs the factory tool"

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(blob)
        img = f.name
    try:
        # write_flash only, because it already verifies.
        #
        # This ran a second full esptool pass (verify_flash) after the write,
        # reasoning that slot0 has no auto-revert behind it so a bad image
        # would only be caught at boot. The reasoning was right and the remedy
        # was redundant: esptool's write_flash finishes by reading the written
        # region's MD5 back off the chip and comparing it -- raising "MD5 of
        # file does not match data in flash!" on a mismatch, and printing
        # "Hash of data verified." otherwise. That line has been in the output
        # of every flash this project has ever run, including the ones used to
        # justify adding the second pass.
        #
        # The read-back is not compressed, so it cost about two minutes on a
        # 1.3 MB image. It doubled how long a customer stares at a dark screen
        # in order to repeat a check that had already passed.
        #
        # Underscore spelling rather than the hyphenated one esptool 5 prefers:
        # both 4.x and 5.x accept it, and _tool() can resolve to whatever
        # esptool a source checkout happens to have on PATH.
        write = ["write_flash", APP_OFFSET, img]

        ok, why = _esptool_run(exe, port, FLASH_BAUD, write, run)
        if ok:
            return True, "written and verified"
        # One retry. A single failure is usually a bad block or a blip on the
        # line, and a rewrite is far cheaper than a customer holding a board
        # that will not boot.
        ok, why = _esptool_run(exe, port, FLASH_BAUD, write, run)
        if ok:
            return True, "written and verified on the second try"
        return False, why
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.unlink(img)
        except OSError:
            pass
