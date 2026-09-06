"""The daemon updating itself.

The board could always update itself; this half never could. A customer's
binary stayed at whatever they downloaded, forever, while the firmware feed it
drives moved on -- and since the daemon is what decides and performs firmware
updates, the stale half was the one in charge.

Three things guard this path, because it is the highest-privilege operation in
the product: a login agent on someone's machine, replacing its own executable
with bytes from the internet.

  1. The manifest is SIGNED, and this refuses to read the daemon block out of
     one that does not verify. The firmware has had this all along -- MCUboot
     will not boot an image signed by anything but the release key -- so a
     compromised release feed could never push firmware. It could have pushed
     a daemon. The key here is deliberately NOT MCUboot's: one key, one job.
  2. The download is checked against the size and sha256 the manifest names.
  3. The replacement is SELF-TESTED before it becomes the service's target --
     the new binary has to run and report the version it claims. A corrupt
     binary that reaches the login service is a device that never comes back
     and a customer who has to start over; 200 ms of proof is cheap next to
     that.

And one brake that is not technical: `daemon.auto` in the manifest. Ship with
it false, turn it on when the path has earned it, turn it off within minutes if
a release goes wrong. Auto-update without a remote off switch is a mechanism
with no brakes.
"""
import errno
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile

from pc import ota
from pc.version import RELEASE_VERSION

MANIFEST_URL = ota.RELEASE_BASE + "manifest.json"
SIG_URL = ota.RELEASE_BASE + "manifest.json.sig"

# The public half of ~/.blink/release_signing_key_p256.pem. Signing happens in
# tools/release.sh. Losing the private half costs nothing today and everything
# after launch: from the first customer onwards, no installed app would ever
# accept another update, and there is no way to reach one that will not. Back
# it up the way MCUboot's key is.
RELEASE_PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEs++ur2jHlamykVsPeCvtT/VE5Awy
JK1K9T7tpqx6hxXWLKxorhWH6Pkxs8Bd/jzv4Zsk2yTOhaUE+dZmSt24Yw==
-----END PUBLIC KEY-----
"""

# Opt out regardless of what the manifest says. A file rather than only an
# environment variable because the daemon is started by launchd/systemd, where
# nobody's shell exports anything.
NO_AUTO_ENV = "BLINK_NO_AUTO_UPDATE"

# Verify against a different public key. This exists so the update path can be
# exercised end to end against a local feed -- tests/ci/check_update.sh signs a
# throwaway manifest and points this at the matching public half -- because the
# real key lives on one machine and CI must never have it.
#
# It is not a weakening: setting an environment variable for this process means
# already being able to run code as this user, at which point the binary itself
# is writable. What it must never become is a way to skip verification, so an
# unreadable file falls back to the embedded key rather than to trusting
# whatever arrived.
PUBKEY_ENV = "BLINK_RELEASE_PUBKEY_FILE"


def _pubkey():
    path = os.environ.get(PUBKEY_ENV)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            print(f"[update] cannot read {path}: {e}; using the built-in key",
                  file=sys.stderr)
    return RELEASE_PUBKEY_PEM


def auto_update_allowed(blink_home) -> bool:
    if os.environ.get(NO_AUTO_ENV) == "1":
        return False
    return not os.path.exists(os.path.join(blink_home, "no-auto-update"))


def platform_key():
    """Which release artifact this process should be replaced with, or None.

    Keyed off the RUNNING process, not the silicon: an x86_64 build under
    Rosetta reports x86_64, and replacing it with an arm64 binary would be an
    architecture change rather than an update.
    """
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    if system == "Linux":
        return "linux-x86_64" if machine in ("x86_64", "amd64") else None
    if system == "Windows":
        return "windows-x86_64"
    return None


def archive_name(key):
    """The file on the release feed for a platform key.

    An archive, not a bare executable, since 1.1.0: the program ships as a
    directory (the executable and its _internal/ support files) so that it
    starts in a fraction of a second instead of unpacking 50 MB into a temp
    directory on every run -- 5 to 11 s on an Intel Mac before the first
    line of Python, on `blink status` and every other command. Zip on
    Windows, where `tar` reads it but nothing native writes one; tar.gz
    elsewhere, where it keeps the executable bit and one `curl | tar xz`
    installs it.
    """
    return "blink-" + key + (".zip" if key.startswith("windows") else ".tar.gz")


# The directory that holds the executable, its rollback and its staging copy:
#   <bin>          what runs            ~/.blink/bin/blink[.exe] + _internal/
#   <bin>.old      the previous one, kept until the new one has proven itself
#   <bin>.new      the download, unpacked and self-tested before it moves in
def _dirs(target):
    d = os.path.dirname(os.path.abspath(target))
    return d, d + ".old", d + ".new"


# --- the signed manifest ------------------------------------------------

def verify_signature(raw: bytes, sig: bytes, pubkey_pem=RELEASE_PUBKEY_PEM) -> bool:
    """True if `sig` is a valid P-256/SHA-256 signature over `raw`.

    Uses openssl when the library is missing so a source checkout without the
    dependency still verifies rather than silently skipping the check. The
    shipped binary always has the library -- pc/requirements.txt pins it and
    PyInstaller freezes it in -- so the fallback is a developer convenience,
    never the customer's path.
    """
    try:
        import ecdsa
    except ImportError:
        return _verify_with_openssl(raw, sig, pubkey_pem)
    try:
        vk = ecdsa.VerifyingKey.from_pem(pubkey_pem)
        return vk.verify(sig, raw, hashfunc=hashlib.sha256,
                         sigdecode=ecdsa.util.sigdecode_der)
    except Exception:
        return False


def _verify_with_openssl(raw, sig, pubkey_pem):
    import shutil
    import tempfile
    if not shutil.which("openssl"):
        return False
    d = tempfile.mkdtemp(prefix="blink-verify-")
    try:
        paths = {}
        for name, data in (("pub.pem", pubkey_pem.encode()),
                           ("sig", sig), ("data", raw)):
            paths[name] = os.path.join(d, name)
            with open(paths[name], "wb") as f:
                f.write(data)
        r = subprocess.run(["openssl", "dgst", "-sha256",
                            "-verify", paths["pub.pem"],
                            "-signature", paths["sig"], paths["data"]],
                           capture_output=True, **ota.NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        import shutil as sh
        sh.rmtree(d, ignore_errors=True)


def fetch_signed_manifest(get=ota._get):
    """The manifest, or None if it is absent, malformed or not properly signed.

    None on a bad signature rather than an exception: the caller's job is to
    keep the bridge running, and a feed we cannot trust is a reason to do
    nothing, not a reason to fall over.
    """
    try:
        raw = get(MANIFEST_URL)
        sig = get(SIG_URL)
    except Exception as e:
        print(f"[update] could not read the release feed: {e}", file=sys.stderr)
        return None
    if not verify_signature(raw, sig, _pubkey()):
        print("[update] manifest signature did not verify -- ignoring the feed",
              file=sys.stderr)
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def available(manifest, current=RELEASE_VERSION, key=None):
    """(version, artifact) for a newer build of this platform, else None."""
    if not manifest:
        return None
    daemon = manifest.get("daemon") or {}
    version = daemon.get("version")
    if not ota.is_newer(version, current):
        return None
    key = key or platform_key()
    artifact = (daemon.get("artifacts") or {}).get(key)
    if not artifact or "sha256" not in artifact or "size" not in artifact:
        return None
    return version, artifact


def download(key, artifact, get=ota._get):
    """The new release's archive, checked against the manifest. Raises on
    either mismatch -- there is nothing sensible to do with a download we
    cannot identify, and running it is certainly not it."""
    blob = get(ota.RELEASE_BASE + archive_name(key), timeout=300)
    if len(blob) != artifact["size"]:
        raise ValueError(f"size {len(blob)} != manifest {artifact['size']}")
    digest = hashlib.sha256(blob).hexdigest()
    want = str(artifact["sha256"]).strip().lower()
    if digest != want:
        raise ValueError(f"sha256 {digest} != manifest {want}")
    return blob


# --- putting it in place ------------------------------------------------

# Generous, because the cost of being wrong is asymmetric.
#
# The binary needs about two seconds of CPU to answer --version (it unpacks
# itself first), but wall-clock is what a timeout measures. Measured at 97 s on
# a machine under load average 89 -- a developer's laptop mid-build, which is
# exactly when someone might run this. Timing out means refusing a good update,
# so the limit is set where only a genuinely hung binary reaches it.
SELF_TEST_TIMEOUT_S = 300


def _self_test(path, expect_version, run=subprocess.run) -> bool:
    try:
        r = run([path, "--version"], capture_output=True, text=True,
                timeout=SELF_TEST_TIMEOUT_S, **ota.NO_WINDOW)
    except Exception:
        return False
    return r.returncode == 0 and expect_version in (r.stdout or "")


def unpack(blob, into):
    """Unpack a release archive so that `into` holds the executable directly.

    The archive carries one top-level directory, `blink/`, so that a person
    who runs `tar xz` gets a folder rather than a spill of files. Here that
    level is stripped. Every member is checked to land inside `into`: the
    bytes were hash-checked against a signed manifest, so this is belt and
    braces, but a path check costs nothing and a traversal would cost a lot.
    """
    def _dest(name):
        parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise ValueError(f"refusing archive member {name!r}")
        if parts[0] == "blink":
            parts = parts[1:]
        if not parts:
            return None
        return os.path.join(into, *parts)

    os.makedirs(into, exist_ok=True)
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for m in z.infolist():
                dest = _dest(m.filename)
                if dest is None:
                    continue
                if m.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                mode = (m.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(dest, mode)
        return
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        for m in t:
            dest = _dest(m.name)
            if dest is None:
                continue
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            if m.issym():
                # Relative, and resolving to somewhere inside `into`. The
                # macOS bundle needs these (Python.framework is built on
                # them); nothing needs one pointing anywhere else.
                link = m.linkname.replace("\\", "/")
                where = os.path.normpath(os.path.join(os.path.dirname(dest), link))
                if os.path.isabs(link) or not (where + os.sep).startswith(
                        os.path.abspath(into) + os.sep):
                    raise ValueError(f"refusing archive symlink {m.name!r} -> {link!r}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.lexists(dest):
                    os.remove(dest)
                os.symlink(link, dest)
                continue
            if not m.isfile():
                raise ValueError(f"refusing archive member {m.name!r} (not a file)")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with t.extractfile(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(dest, m.mode & 0o777 or 0o644)


def swap_in(new_dir, target, version, run=subprocess.run):
    """Make the program at `new_dir` the one at `target`'s directory.

    Self-tests it first, then rotates: <bin> -> <bin>.old, <bin>.new -> <bin>.
    A running executable can be renamed but not overwritten, which is what
    makes an in-place update possible without a second program to do the
    swapping.

    That is where the ease ends on Windows, which refuses to rename a
    directory while any process holds a file open inside it -- and the daemon
    holds bin\\blink.exe. The renames go through _replace(), which waits for
    the handle instead of reporting failure on the first refusal. Callers that
    can stop the daemon first should (see cli.halt_service); the retry is the
    net under them, not a substitute.
    """
    cur, old, new = _dirs(target)
    exe = os.path.join(new_dir, os.path.basename(target))
    try:
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP
                 | stat.S_IXOTH)
    except OSError:
        pass
    if not _self_test(exe, version, run=run):
        _rmtree(new_dir)
        return False, ("the downloaded program did not run -- keeping the"
                       " current one")
    if os.path.abspath(new_dir) != os.path.abspath(new):
        _rmtree(new)
        _replace(new_dir, new)
    try:
        _rmtree(old)
        if os.path.exists(cur):
            _replace(cur, old)
        _replace(new, cur)
    except OSError as e:
        # Put back whatever we moved, so a half-applied update is not left
        # pointing the login service at nothing. Retried too: the thing that
        # blocked the move out is just as able to block the move back, and
        # this is the branch where giving up leaves no program at all.
        if not os.path.exists(cur) and os.path.exists(old):
            try:
                _replace(old, cur)
            except OSError:
                pass
        _rmtree(new)
        return False, f"could not replace {cur}: {e}"
    return True, f"updated to {version}"


def apply(blob, target, version, run=subprocess.run):
    """Replace the program at `target` with the archive `blob`. (ok, message).

    Does NOT restart anything: which supervisor owns this process, and whether
    it is this process, is the caller's business (see cli.restart_service).
    """
    cur, old, new = _dirs(target)
    try:
        os.makedirs(os.path.dirname(cur), exist_ok=True)
    except OSError as e:
        return False, f"cannot write to {os.path.dirname(cur)}: {e}"
    _rmtree(new)
    try:
        unpack(blob, new)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as e:
        _rmtree(new)
        return False, f"could not stage the download: {e}"
    if not os.path.exists(os.path.join(new, os.path.basename(target))):
        _rmtree(new)
        return False, "the download does not contain the program"
    return swap_in(new, target, version, run=run)


def recover(target) -> bool:
    """Restore the previous program if the current one is missing or empty.

    Called on every start. The window it covers is small -- between the two
    renames in swap_in() -- but what it prevents is not: a login service whose
    program does not exist never runs again, and nothing on the board or the
    computer would explain why.
    """
    cur, old, _ = _dirs(target)
    try:
        healthy = os.path.exists(target) and os.path.getsize(target) > 0
    except OSError:
        healthy = False
    if healthy or not os.path.exists(os.path.join(old, os.path.basename(target))):
        return False
    try:
        _rmtree(cur)
        os.replace(old, cur)
        print(f"[update] restored the previous program at {cur}",
              file=sys.stderr)
        return True
    except OSError:
        return False


def restart_from_daemon(target):
    """Hand over to the freshly written program, from inside the daemon itself.

    On macOS and Linux this just exits: launchd's KeepAlive and systemd's
    Restart=always exist for crashes, and an update is the one time we want
    them. Windows has neither -- `schtasks /sc onlogon` fires at logon and
    never again -- so the replacement is started explicitly before this one
    goes away. The task still points at the same path, so nothing is orphaned
    beyond the current session. It is started the way the task starts it,
    logging to bridge.log, or the log would go dark from the first update on.
    """
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        DETACHED = 0x00000008 | 0x00000200 | 0x08000000
        home = os.path.dirname(_dirs(target)[0])
        log = os.path.join(home, "bridge.log")
        try:
            subprocess.Popen([target, "run", "--log", log],
                             creationflags=DETACHED, close_fds=True)
        except Exception as e:
            print(f"[update] could not start the new program: {e}",
                  file=sys.stderr)
            return                            # stay up rather than vanish
    print("[update] restarting into the new version", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def cleanup(target):
    """Drop the rollback copy once we are running and healthy.

    Best effort: on Windows the daemon this one replaced may still be closing
    down inside <bin>.old, in which case the next start gets it.
    """
    _rmtree(_dirs(target)[1])


class PendingFirmware:
    """The firmware version the user already approved, across our own restart.

    A pair update is one tap on the board, but it is two operations and the
    daemon replaces itself in the middle of them -- so the consent has to
    outlive the process that received it. One line in a file does that.

    take() clears before the caller acts on it, deliberately. If flashing goes
    wrong the right outcome is a board still running its old firmware and a
    user who can tap again, not a daemon that retries the same failing install
    on every reconnect for the rest of its life.
    """

    def __init__(self, path):
        self.path = path

    def set(self, version):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"version": version}, f)
        except OSError as e:
            print(f"[update] could not record the pending update: {e}",
                  file=sys.stderr)

    def take(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                version = json.load(f).get("version")
        except (OSError, ValueError):
            return None
        _rm(self.path)
        return version or None


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


# A handle is let go within a moment or two of the process holding it dying;
# these add up to about three seconds, which is long enough for that and short
# enough that a genuinely stuck update does not look like a hang.
REPLACE_TRIES = 10
REPLACE_DELAY = 0.3

# Windows says access denied (5), sharing violation (32) or directory not
# empty (145); Python surfaces the first two as EACCES/EPERM and the third as
# ENOTEMPTY. EBUSY is the macOS/Linux equivalent (a mount point).
_TRANSIENT_ERRNO = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY,
                              errno.ENOTEMPTY})
_TRANSIENT_WINERROR = frozenset({5, 32, 145})


def _replace(src, dst, sleep=time.sleep, tries=REPLACE_TRIES):
    """os.replace, retried while something still holds the old directory.

    POSIX renames a directory whatever is open inside it. WINDOWS DOES NOT:
    while any process holds a file open under <bin>, renaming <bin> fails with
    WinError 5. The daemon runs from bin\\blink.exe, so `blink update` races
    its own background service letting go of it -- and the race was not even
    run, because a single os.replace either won on the first try or reported
    failure to the customer.

    Which is what a Windows desk showed: "could not replace ...\\.blink\\bin:
    [WinError 5] Access is denied" printed, and the app was on the new version
    anyway. The update worked; the person running it was told it had not.

    Raises the LAST error if every attempt fails, so the message the caller
    prints still names what actually went wrong.
    """
    last = None
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if (e.errno not in _TRANSIENT_ERRNO
                    and getattr(e, "winerror", None) not in _TRANSIENT_WINERROR):
                raise
            last = e
            if attempt + 1 < tries:
                sleep(REPLACE_DELAY)
    raise last
