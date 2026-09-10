"""The daemon replacing its own binary.

This is the highest-privilege thing the product does -- a login agent on
someone's machine, overwriting its own executable with bytes from the internet
-- so the tests here are mostly about what must NOT happen.
"""
import hashlib
import json
import os
import unittest

import ecdsa

from pc import update


def keypair():
    sk = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
    return sk, sk.get_verifying_key().to_pem().decode()


def sign(sk, raw):
    return sk.sign(raw, hashfunc=hashlib.sha256,
                   sigencode=ecdsa.util.sigencode_der)


class TestPlatformKey(unittest.TestCase):
    def _key(self, system, machine):
        import platform
        real = (platform.system, platform.machine)
        platform.system, platform.machine = lambda: system, lambda: machine
        try:
            return update.platform_key()
        finally:
            platform.system, platform.machine = real

    def test_the_four_published_builds(self):
        self.assertEqual(self._key("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(self._key("Darwin", "x86_64"), "macos-x86_64")
        self.assertEqual(self._key("Linux", "x86_64"), "linux-x86_64")
        self.assertEqual(self._key("Windows", "AMD64"), "windows-x86_64")

    def test_the_feed_serves_an_archive_per_key(self):
        """A directory since 1.1.0 -- a one-file build unpacked 50 MB on every
        run, 5-11 s on an Intel Mac before `blink status` printed a line."""
        self.assertEqual(update.archive_name("macos-arm64"), "blink-macos-arm64.tar.gz")
        self.assertEqual(update.archive_name("linux-x86_64"), "blink-linux-x86_64.tar.gz")
        self.assertEqual(update.archive_name("windows-x86_64"), "blink-windows-x86_64.zip")

    def test_an_intel_process_under_rosetta_stays_intel(self):
        """Keyed off the running process, not the silicon. Replacing an x86_64
        build with an arm64 one is an architecture change, not an update."""
        self.assertEqual(self._key("Darwin", "x86_64"), "macos-x86_64")

    def test_a_platform_we_do_not_publish_for_returns_none(self):
        self.assertIsNone(self._key("Linux", "aarch64"))
        self.assertIsNone(self._key("FreeBSD", "x86_64"))


class TestSignature(unittest.TestCase):
    def test_a_good_signature_verifies(self):
        sk, pub = keypair()
        raw = b'{"version":"0.6.1"}'
        self.assertTrue(update.verify_signature(raw, sign(sk, raw), pub))

    def test_tampered_content_does_not(self):
        sk, pub = keypair()
        raw = b'{"version":"0.6.1"}'
        sig = sign(sk, raw)
        self.assertFalse(update.verify_signature(raw + b" ", sig, pub))

    def test_another_key_does_not(self):
        sk, _ = keypair()
        _, other_pub = keypair()
        raw = b'{"version":"0.6.1"}'
        self.assertFalse(update.verify_signature(raw, sign(sk, raw), other_pub))

    def test_garbage_is_refused_rather_than_raising(self):
        _, pub = keypair()
        self.assertFalse(update.verify_signature(b"x", b"not a signature", pub))

    def test_an_unsigned_feed_yields_no_manifest(self):
        """The whole point: strip the signature and the feed goes dead, rather
        than falling back to trusting whatever arrived."""
        body = json.dumps({"daemon": {"version": "9.9.9"}}).encode()

        def get(url, timeout=30):
            return body if url.endswith("manifest.json") else b"nope"

        self.assertIsNone(update.fetch_signed_manifest(get=get))


class TestAvailable(unittest.TestCase):
    M = {"daemon": {"version": "0.7.0", "artifacts": {
        "macos-arm64": {"size": 10, "sha256": "ab" * 32}}}}

    def test_a_newer_build_for_this_platform(self):
        got = update.available(self.M, current="0.6.0", key="macos-arm64")
        self.assertEqual(got[0], "0.7.0")

    def test_same_or_older_is_not_an_update(self):
        self.assertIsNone(update.available(self.M, "0.7.0", "macos-arm64"))
        self.assertIsNone(update.available(self.M, "0.8.0", "macos-arm64"))

    def test_a_platform_the_release_did_not_publish(self):
        self.assertIsNone(update.available(self.M, "0.6.0", "linux-x86_64"))

    def test_a_manifest_with_no_daemon_block_is_not_an_error(self):
        """Every manifest published before this existed looks like that, and a
        daemon must keep working against the feed it already knows."""
        self.assertIsNone(update.available({"version": "9.9.9"}, "0.6.0",
                                           "macos-arm64"))
        self.assertIsNone(update.available(None, "0.6.0", "macos-arm64"))


class TestDownload(unittest.TestCase):
    def test_size_mismatch_raises(self):
        art = {"size": 99, "sha256": hashlib.sha256(b"abc").hexdigest()}
        with self.assertRaises(ValueError):
            update.download("linux-x86_64", art, get=lambda u, timeout=30: b"abc")

    def test_hash_mismatch_raises(self):
        art = {"size": 3, "sha256": "cd" * 32}
        with self.assertRaises(ValueError):
            update.download("linux-x86_64", art, get=lambda u, timeout=30: b"abc")

    def test_a_matching_download_is_returned(self):
        art = {"size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
        self.assertEqual(
            update.download("linux-x86_64", art,
                            get=lambda u, timeout=30: b"abc"), b"abc")


def fake_run(ok=True, version="0.7.0"):
    def run(cmd, **kw):
        class R:
            returncode = 0 if ok else 1
            stdout = f"blink {version}\n" if ok else ""
            stderr = ""
        return R()
    return run


def archive(files, kind="tar"):
    """A release archive in memory: {relative path: bytes} under blink/."""
    import io
    import tarfile
    import zipfile
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in files.items():
                info = zipfile.ZipInfo("blink/" + name)
                info.external_attr = (0o755 | 0o100000) << 16
                z.writestr(info, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in files.items():
                info = tarfile.TarInfo("blink/" + name)
                info.size = len(data)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


NEW = {"blink": b"#!/bin/sh\necho new\n", "_internal/lib.so": b"support"}


class TestApply(unittest.TestCase):
    """The program is a directory: <bin>/blink plus <bin>/_internal. An
    update unpacks the archive to <bin>.new, self-tests it, and rotates
    <bin> -> <bin>.old, <bin>.new -> <bin>."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="blink-apply-")
        self.bin = os.path.join(self.d, "bin")
        self.target = os.path.join(self.bin, "blink")
        os.makedirs(os.path.join(self.bin, "_internal"))
        with open(self.target, "wb") as f:
            f.write(b"old binary")
        with open(os.path.join(self.bin, "_internal", "lib.so"), "wb") as f:
            f.write(b"old support")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _read(self, *parts):
        return open(os.path.join(self.d, *parts), "rb").read()

    def test_it_replaces_the_program_and_keeps_a_rollback(self):
        ok, msg = update.apply(archive(NEW), self.target, "0.7.0",
                               run=fake_run())
        self.assertTrue(ok, msg)
        self.assertEqual(self._read("bin", "blink"), NEW["blink"])
        self.assertEqual(self._read("bin", "_internal", "lib.so"), b"support")
        self.assertEqual(self._read("bin.old", "blink"), b"old binary")
        self.assertEqual(self._read("bin.old", "_internal", "lib.so"), b"old support")
        self.assertFalse(os.path.exists(self.bin + ".new"))

    def test_a_zip_unpacks_the_same_way(self):
        ok, msg = update.apply(archive(NEW, "zip"), self.target, "0.7.0",
                               run=fake_run())
        self.assertTrue(ok, msg)
        self.assertEqual(self._read("bin", "_internal", "lib.so"), b"support")

    def test_the_replacement_is_executable(self):
        update.apply(archive(NEW), self.target, "0.7.0", run=fake_run())
        self.assertTrue(os.access(self.target, os.X_OK))

    def test_a_program_that_will_not_run_never_becomes_the_target(self):
        """The failure this exists for: a login service pointed at a broken
        program is a device that never comes back, with nothing on screen to
        explain it. The old program has to survive."""
        ok, msg = update.apply(archive(NEW), self.target, "0.7.0",
                               run=fake_run(ok=False))
        self.assertFalse(ok)
        self.assertIn("did not run", msg)
        self.assertEqual(self._read("bin", "blink"), b"old binary")
        self.assertFalse(os.path.exists(self.bin + ".new"))

    def test_a_program_reporting_the_wrong_version_is_refused(self):
        ok, _ = update.apply(archive(NEW), self.target, "0.7.0",
                             run=fake_run(version="0.5.0"))
        self.assertFalse(ok)
        self.assertEqual(self._read("bin", "blink"), b"old binary")

    def test_a_download_that_is_not_an_archive_is_refused(self):
        ok, msg = update.apply(b"not an archive", self.target, "0.7.0",
                               run=fake_run())
        self.assertFalse(ok)
        self.assertIn("stage", msg)
        self.assertEqual(self._read("bin", "blink"), b"old binary")
        self.assertFalse(os.path.exists(self.bin + ".new"))

    def test_an_archive_without_the_program_is_refused(self):
        ok, msg = update.apply(archive({"README": b"hi"}), self.target,
                               "0.7.0", run=fake_run())
        self.assertFalse(ok)
        self.assertIn("does not contain", msg)
        self.assertEqual(self._read("bin", "blink"), b"old binary")

    def test_a_member_that_escapes_the_directory_is_refused(self):
        ok, _ = update.apply(archive({"../escape": b"x"}), self.target,
                             "0.7.0", run=fake_run())
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.d, "escape")))

    def test_a_first_install_needs_no_previous_program(self):
        import shutil
        shutil.rmtree(self.bin)
        ok, _ = update.apply(archive(NEW), self.target, "0.7.0",
                             run=fake_run())
        self.assertTrue(ok)
        self.assertEqual(self._read("bin", "blink"), NEW["blink"])
        self.assertFalse(os.path.exists(self.bin + ".old"))


class TestRecover(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="blink-recover-")
        self.bin = os.path.join(self.d, "bin")
        self.target = os.path.join(self.bin, "blink")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _old(self, data=b"previous"):
        os.makedirs(self.bin + ".old", exist_ok=True)
        with open(os.path.join(self.bin + ".old", "blink"), "wb") as f:
            f.write(data)

    def test_a_missing_program_is_restored_from_the_rollback(self):
        self._old()
        self.assertTrue(update.recover(self.target))
        self.assertEqual(open(self.target, "rb").read(), b"previous")
        self.assertFalse(os.path.exists(self.bin + ".old"))

    def test_an_empty_program_is_treated_as_missing(self):
        os.makedirs(self.bin)
        open(self.target, "wb").close()
        self._old()
        self.assertTrue(update.recover(self.target))
        self.assertEqual(open(self.target, "rb").read(), b"previous")

    def test_a_healthy_program_is_left_alone(self):
        os.makedirs(self.bin)
        with open(self.target, "wb") as f:
            f.write(b"current")
        self._old()
        self.assertFalse(update.recover(self.target))
        self.assertEqual(open(self.target, "rb").read(), b"current")
        self.assertTrue(os.path.exists(self.bin + ".old"))

    def test_nothing_to_recover_from_is_not_an_error(self):
        self.assertFalse(update.recover(self.target))


class TestOptOut(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="blink-optout-")
        self._env = os.environ.pop(update.NO_AUTO_ENV, None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)
        if self._env is not None:
            os.environ[update.NO_AUTO_ENV] = self._env
        else:
            os.environ.pop(update.NO_AUTO_ENV, None)

    def test_allowed_by_default(self):
        self.assertTrue(update.auto_update_allowed(self.d))

    def test_a_marker_file_turns_it_off(self):
        """A file, not only an environment variable: the daemon is started by
        launchd, where nobody's shell has exported anything."""
        open(os.path.join(self.d, "no-auto-update"), "w").close()
        self.assertFalse(update.auto_update_allowed(self.d))

    def test_the_environment_variable_turns_it_off(self):
        os.environ[update.NO_AUTO_ENV] = "1"
        self.assertFalse(update.auto_update_allowed(self.d))


class TestReplaceRetries(unittest.TestCase):
    """Windows refuses to rename <bin> while the daemon holds blink.exe open.

    The observed symptom was not a failed update -- it was a SUCCESSFUL one
    that told the customer it had failed:

        could not replace C:\\Users\\...\\.blink\\bin: [WinError 5] Access is denied

    printed, with the app on the new version anyway. A single os.replace either
    won the race on its first try or reported defeat; it never waited.
    """

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="blink-replace-")
        self.src = os.path.join(self.d, "src")
        self.dst = os.path.join(self.d, "dst")
        os.makedirs(self.src)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _flaky(self, refusals, exc):
        """An os.replace that refuses `refusals` times and then works."""
        real = os.replace
        state = {"n": 0}

        def fake(a, b):
            state["n"] += 1
            if state["n"] <= refusals:
                raise exc
            return real(a, b)

        return fake, state

    def _run(self, fake, **kw):
        real = os.replace
        os.replace = fake
        slept = []
        try:
            update._replace(self.src, self.dst, sleep=slept.append, **kw)
        finally:
            os.replace = real
        return slept

    def _denied(self, winerror=5):
        e = OSError(13, "Access is denied")
        e.winerror = winerror
        return e

    def test_a_handle_that_is_let_go_makes_the_rename_succeed(self):
        fake, state = self._flaky(3, self._denied())
        slept = self._run(fake)
        self.assertTrue(os.path.isdir(self.dst))
        self.assertEqual(state["n"], 4)
        # It waited between tries rather than spinning.
        self.assertEqual(slept, [update.REPLACE_DELAY] * 3)

    def test_a_sharing_violation_is_waited_out_too(self):
        # WinError 32 is what a file merely open under <bin> produces; 5 is
        # what an executing image produces. Both are the same wait.
        fake, _ = self._flaky(1, self._denied(32))
        self._run(fake)
        self.assertTrue(os.path.isdir(self.dst))

    def test_a_handle_nobody_lets_go_of_still_fails(self):
        fake, state = self._flaky(99, self._denied())
        with self.assertRaises(OSError) as caught:
            self._run(fake, tries=4)
        # The LAST error, so the message swap_in prints still names what
        # actually went wrong instead of something generic.
        self.assertEqual(getattr(caught.exception, "winerror", None), 5)
        self.assertEqual(state["n"], 4)

    def test_an_error_that_waiting_cannot_fix_is_raised_at_once(self):
        import errno as _errno
        fake, state = self._flaky(99, OSError(_errno.ENOENT, "No such file"))
        with self.assertRaises(OSError):
            self._run(fake)
        self.assertEqual(state["n"], 1, "waiting cannot conjure a missing file")

    def test_a_rename_that_works_first_time_never_sleeps(self):
        slept = self._run(os.replace)
        self.assertEqual(slept, [])
        self.assertTrue(os.path.isdir(self.dst))


class TestSwapWaitsForTheHandle(unittest.TestCase):
    """The retry has to be reached from swap_in, not only exist beside it."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="blink-swapwait-")
        self.bin = os.path.join(self.d, "bin")
        self.target = os.path.join(self.bin, "blink")
        os.makedirs(os.path.join(self.bin, "_internal"))
        with open(self.target, "wb") as f:
            f.write(b"old binary")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_an_update_that_wins_on_the_second_try_reports_success(self):
        real, state = os.replace, {"n": 0}

        def flaky(a, b):
            state["n"] += 1
            # Refuse the move of <bin> out of the way exactly once, the way
            # a daemon that has not quite exited does.
            if state["n"] == 2:
                e = OSError(13, "Access is denied")
                e.winerror = 5
                raise e
            return real(a, b)

        os.replace = flaky
        try:
            ok, msg = update.apply(archive(NEW), self.target, "0.7.0",
                                   run=fake_run())
        finally:
            os.replace = real
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "updated to 0.7.0")
        self.assertEqual(open(os.path.join(self.bin, "blink"), "rb").read(),
                         NEW["blink"])
        self.assertNotIn("could not replace", msg)


class TestPendingFirmware(unittest.TestCase):
    """The note that carries one tap across the daemon replacing itself."""

    def note(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d,
                                                            ignore_errors=True))
        return update.PendingFirmware(os.path.join(d, "pending_fw.json"))

    def test_absent_reads_as_nothing(self):
        self.assertEqual(self.note().read(), (None, 0))

    def test_set_then_read_does_not_spend_it(self):
        p = self.note()
        p.set("1.3.2")
        self.assertEqual(p.read(), ("1.3.2", 0))
        self.assertEqual(p.read(), ("1.3.2", 0))   # still there

    def test_bump_counts_attempts_without_losing_the_version(self):
        p = self.note()
        p.set("1.3.2")
        p.bump()
        p.bump()
        self.assertEqual(p.read(), ("1.3.2", 2))

    def test_clear_removes_it(self):
        p = self.note()
        p.set("1.3.2")
        p.clear()
        self.assertEqual(p.read(), (None, 0))
        p.clear()                       # and clearing twice is not an error

    def test_a_note_from_an_older_daemon_has_no_counter(self):
        """THE pair-update case, and the reason `tries` may not be there.

        The OLD app writes this file and the NEW one reads it -- that is what
        a pair update is -- so the first read after an update to a daemon that
        counts attempts is always of a note written by one that did not. A
        missing counter is zero attempts, never a reason to discard consent
        the user gave.
        """
        p = self.note()
        with open(p.path, "w", encoding="utf-8") as f:
            json.dump({"version": "1.3.2"}, f)      # pre-counter format
        self.assertEqual(p.read(), ("1.3.2", 0))

    def test_a_corrupt_note_is_not_consent(self):
        p = self.note()
        with open(p.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(p.read(), (None, 0))

    def test_take_still_reads_and_clears(self):
        """_on_ota_flash uses it when an app update failed and the old app is
        installing the firmware itself -- there the consent IS being spent."""
        p = self.note()
        p.set("1.3.2")
        self.assertEqual(p.take(), "1.3.2")
        self.assertEqual(p.read(), (None, 0))


if __name__ == "__main__":
    unittest.main()
