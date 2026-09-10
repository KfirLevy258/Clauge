import hashlib
import io
import sys
import unittest

from pc import ota
from pc.bridge import Bridge
from pc.version import PROTO_VERSION, RELEASE_VERSION


_SAME = object()   # "the signed feed serves the same thing as the plain one"


def bridge(manifest=None, firmware=b"", fw_raises=None, flasher=True,
           self_update=None, pending=None, signed=_SAME):
    """A Bridge with the network and the flasher replaced.

    Returns (bridge, sent, flashed) -- `flashed` collects (blob, version)
    tuples so a test can assert what would have been written.
    """
    sent, flashed = [], []

    def fetch_fw():
        if fw_raises:
            raise fw_raises
        return firmware

    b = Bridge(write_msg=sent.append, fetch_usage=lambda: {},
               fetch_manifest=lambda: manifest, fetch_firmware=fetch_fw,
               flash_image=(lambda blob, v: flashed.append((blob, v)))
               if flasher else None,
               self_update=self_update, pending=pending,
               # The app-update decision reads a SIGNED manifest, never the
               # one the firmware path uses. Default it to the same object so
               # a test that does not care about signing behaves as before.
               fetch_signed_manifest=lambda: (manifest if signed is _SAME
                                              else signed))
    return b, sent, flashed


def types(sent):
    # .get: the stub fetch_usage returns {}, which poll_once forwards verbatim.
    return [m.get("t") for m in sent]


class TestVersion(unittest.TestCase):
    def test_strictly_newer(self):
        self.assertTrue(ota.is_newer("0.4.9", "0.4.8"))
        self.assertTrue(ota.is_newer("1.0.0", "0.9.9"))

    def test_same_or_older_is_not_newer(self):
        self.assertFalse(ota.is_newer("0.4.8", "0.4.8"))
        self.assertFalse(ota.is_newer("0.4.7", "0.4.8"))

    def test_malformed_never_triggers_an_install(self):
        for bad in ("", "x", "1.2", "1.2.3.4", None):
            self.assertFalse(ota.is_newer(bad, "0.4.8"))
            self.assertFalse(ota.is_newer("0.4.9", bad))


class TestOffer(unittest.TestCase):
    M = {"version": "0.4.9", "size": 600, "sha256": "ab" * 32}

    def test_offers_a_newer_release(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_avail"])
        self.assertEqual(sent[0]["version"], "0.4.9")

    def test_declines_when_board_is_current(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "ota_query", "cur": "0.4.9"})
        self.assertEqual(types(sent), ["ota_none"])

    def test_declines_when_board_is_ahead_of_the_release(self):
        """Normal during development: the board runs newer than any release."""
        b, sent, _ = bridge(manifest={"version": "0.4.7", "size": 1,
                                      "sha256": "a"})
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_none"])

    def test_unreachable_feed_declines_rather_than_errors(self):
        b, sent, _ = bridge(manifest=None)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_none"])


HELLO_SHA = hashlib.sha256(b"hello").hexdigest()


class TestCompatibility(unittest.TestCase):
    """The "v" both sides stamp on every message went unread for a long time.

    Harmless while the protocol only grows -- unknown fields are ignored -- but
    it left no way to refuse the one case that is not harmless: this daemon
    driving a firmware update on a board whose protocol it does not speak, into
    a slot with no auto-revert behind it.
    """

    M = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA}

    def test_a_board_that_outranks_us_is_not_offered_an_update(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "v": PROTO_VERSION + 1, "fw": "9.9.9"})
        sent.clear()
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_none"])

    def test_a_board_at_our_own_protocol_is_offered_one(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        sent.clear()
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_avail"])

    def test_a_hello_with_no_version_is_not_treated_as_ahead(self):
        """Every board ever shipped sends one, but absent must not mean newer:
        that would silently disable updates for the whole fleet."""
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "fw": "0.4.8"})
        sent.clear()
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_avail"])

    def test_a_release_declaring_a_protocol_floor_above_us_is_not_offered(self):
        m = dict(self.M, fw={"proto_min": PROTO_VERSION + 1})
        b, sent, _ = bridge(manifest=m)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_none"])

    def test_a_floor_we_meet_is_offered(self):
        m = dict(self.M, fw={"proto_min": PROTO_VERSION})
        b, sent, _ = bridge(manifest=m)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_avail"])

    def test_the_board_is_told_our_real_version(self):
        """It was told "0.3.0" -- a default that matched no release, and the
        only way the board can learn which half of the pair is behind."""
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        welcome = [m for m in sent if m.get("t") == "welcome"][0]
        self.assertEqual(welcome["app_ver"], RELEASE_VERSION)


class TestFlash(unittest.TestCase):
    M = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA}

    def test_approval_flashes_the_downloaded_image(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])

    def test_the_board_is_told_when_the_write_starts(self):
        """Not at consent, which is where the breadcrumb used to be written.

        In a pair update the daemon replaces itself between the two, and the
        new process opening the port resets the board -- which then reported a
        failure for an install that had not started, spending the breadcrumb
        so the real success went unreported.
        """
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_begin"])
        self.assertEqual(sent[-1]["version"], "0.4.9")
        self.assertEqual(flashed, [(b"hello", "0.4.9")])

    def test_nothing_is_begun_when_the_image_is_rejected(self):
        m = dict(self.M, sha256="cd" * 32)
        b, sent, flashed = bridge(manifest=m, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertNotIn("ota_begin", types(sent))

    def test_flash_without_an_offer_is_refused(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_error"])
        self.assertEqual(flashed, [])

    def test_size_mismatch_is_never_written(self):
        """slot0 has no auto-revert, so a bad image must not reach it."""
        b, sent, flashed = bridge(manifest=self.M, firmware=b"much longer")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])
        self.assertEqual(flashed, [])

    def test_sha_mismatch_is_never_written(self):
        """The hash the manifest publishes was, for a while, checked by nobody.

        Over WiFi the board hashed the stream. Over USB it never sees the
        bytes, and this side only compared lengths -- so any 5-byte answer
        from the CDN would have been written to a slot with no auto-revert.
        """
        m = dict(self.M, sha256="cd" * 32)
        b, sent, flashed = bridge(manifest=m, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])
        self.assertEqual(sent[-1]["why"], "sha256 mismatch")
        self.assertEqual(flashed, [])

    def test_hash_comparison_ignores_case_and_whitespace(self):
        """sha256sum and shasum disagree about case; neither is wrong."""
        m = dict(self.M, sha256="  " + HELLO_SHA.upper() + "\n")
        b, sent, flashed = bridge(manifest=m, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])

    def test_download_failure_reports_instead_of_raising(self):
        b, sent, flashed = bridge(manifest=self.M, fw_raises=OSError("no route"))
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])
        self.assertEqual(flashed, [])

    def test_missing_flasher_reports_instead_of_crashing(self):
        b, sent, _ = bridge(manifest=self.M, firmware=b"hello", flasher=False)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])

    def test_offer_is_consumed_so_a_repeat_cannot_reflash(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(len(flashed), 1)


class TestFlashGuards(unittest.TestCase):
    """ota.flash() must not write a chip it cannot vouch for."""

    def setUp(self):
        # These tests inject `run`, so the only thing still reaching outside is
        # tool DISCOVERY: _esptool() looks for esptool in whatever interpreter
        # runs the tests. That made the outcome depend on the machine --
        # green in a venv that happens to carry esptool, three failures on a
        # clean CI runner -- and in neither case was it testing the guards it
        # names. Pin the discovery; the guards are the subject.
        self._tools = (ota._esptool, ota._efuse_probe)
        ota._esptool = lambda: ["python", "-m", "esptool"]
        ota._efuse_probe = lambda: ["python", "-m", "pc.efuse_probe"]

    def tearDown(self):
        ota._esptool, ota._efuse_probe = self._tools

    def _run(self, efuse_out=None, rc=0, fail_probe=False):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            class R:
                returncode = rc
                stdout = efuse_out if "efuse_probe" in " ".join(cmd) else "ok"
                stderr = ""
            if fail_probe and "efuse_probe" in " ".join(cmd):
                raise OSError("probe failed")
            return R()
        return run, calls

    def test_refuses_an_encrypted_chip(self):
        run, calls = self._run(efuse_out="flash_encryption enabled\n")
        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertFalse(ok)
        self.assertIn("Encrypted chip", why)
        # It ends up on the board, which keeps 47 characters of it.
        self.assertLessEqual(len(why), 47)
        self.assertFalse(any("write_flash" in " ".join(c) for c in calls))

    def test_refuses_when_the_efuses_cannot_be_read(self):
        run, calls = self._run(fail_probe=True)
        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertFalse(ok)
        self.assertIn("Could not check the chip", why)
        self.assertLessEqual(len(why), 47)
        self.assertFalse(any("write_flash" in " ".join(c) for c in calls))

    def test_writes_a_plaintext_chip(self):
        run, calls = self._run(efuse_out="flash_encryption disabled\n")
        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertTrue(ok, why)
        self.assertTrue(any("write_flash" in " ".join(c) for c in calls))

    def test_it_does_not_pay_for_a_read_back_esptool_already_did(self):
        """write_flash ends by comparing the written region's MD5 off the chip
        -- "Hash of data verified." is in the output of every flash this
        project has run. A second verify_flash pass repeated that check at the
        cost of about two minutes of dark screen on a 1.3 MB image."""
        run, calls = self._run(efuse_out="flash_encryption disabled\n")
        ota.flash("/dev/null", b"x", run=run)
        self.assertFalse(any("verify_flash" in " ".join(c) for c in calls))

    def test_it_does_not_try_to_go_faster_than_115200(self):
        """Measured on the CYD's CH340 on 2026-08-22: three attempts at 460800
        all died with "Invalid head of packet" right after the baud change,
        while 115200 moved 1 MB cleanly. A failed read is free; a failed write
        leaves a slot0 that will not boot, and there is no revert behind it.
        """
        run, calls = self._run(efuse_out="flash_encryption disabled\n")
        ota.flash("/dev/null", b"x", run=run)
        bauds = {c[c.index("--baud") + 1] for c in calls if "--baud" in c
                 and "efuse_probe" not in " ".join(c)}
        self.assertEqual(bauds, {"115200"})

    def test_a_failed_write_is_retried_once(self):
        state = {"writes": 0}

        def run(cmd, **kw):
            joined = " ".join(cmd)
            rc = 0
            if "write_flash" in joined:
                state["writes"] += 1
                rc = 1 if state["writes"] == 1 else 0   # the second one lands

            class R:
                returncode = rc
                stdout = ("flash_encryption disabled\n"
                          if "efuse_probe" in joined else "ok")
                stderr = "content mismatch"
            return R()

        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertTrue(ok, why)
        self.assertIn("second try", why)
        self.assertEqual(state["writes"], 2)

    def test_a_write_that_never_lands_reports_failure(self):
        def run(cmd, **kw):
            joined = " ".join(cmd)

            class R:
                returncode = 1 if "write_flash" in joined else 0
                stdout = ("flash_encryption disabled\n"
                          if "efuse_probe" in joined else "ok")
                stderr = "content mismatch at 0x20000"
            return R()

        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertFalse(ok)
        self.assertIn("mismatch", why)


if __name__ == "__main__":
    unittest.main()


class FakePending:
    """update.PendingFirmware without a filesystem."""

    def __init__(self, version=None, tries=0):
        self.version = version
        self.tries = tries
        self.writes = []

    def set(self, version):
        self.version = version
        self.tries = 0
        self.writes.append(version)

    def read(self):
        if not self.version:
            return None, 0
        return self.version, self.tries

    def bump(self):
        if self.version:
            self.tries += 1

    def clear(self):
        self.version = None

    def take(self):
        v, self.version = self.version, None
        return v


class TestPairUpdate(unittest.TestCase):
    """One tap on the board, two installs, and the daemon replaces itself in
    the middle of them."""

    # The daemon version is deliberately far ahead of whatever this checkout
    # is: update.available() compares against the RUNNING version, so pinning
    # a literal here would quietly stop testing anything after the next bump.
    M = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA,
         "daemon": {"version": "99.0.0", "artifacts": {
             "macos-arm64": {"size": 9, "sha256": "ab" * 32}}}}

    def setUp(self):
        from pc import update
        self._key = update.platform_key
        update.platform_key = lambda: "macos-arm64"
        self.addCleanup(lambda: setattr(update, "platform_key", self._key))

    def test_the_offer_names_the_app_version_too(self):
        b, sent, _ = bridge(manifest=self.M, self_update=lambda v, a: True)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(sent[0]["app"], "99.0.0")

    def test_the_app_offer_comes_from_the_SIGNED_manifest(self):
        """The manifest the firmware path uses is not signature-checked, and
        does not need to be: MCUboot refuses an image that was not signed with
        the release key whatever a manifest claims. A daemon binary has no such
        backstop, and this is the path a customer actually taps -- so it must
        not be decided by a manifest nobody verified."""
        unsigned = dict(self.M, daemon={"version": "99.9.9", "artifacts": {
            "macos-arm64": {"size": 1, "sha256": "ff" * 32}}})
        b, sent, _ = bridge(manifest=unsigned, signed=self.M,
                            self_update=lambda v, a: True)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(sent[0]["app"], "99.0.0",
                         "took the version from the unsigned manifest")

    def test_an_unverifiable_manifest_offers_no_app_update(self):
        b, sent, _ = bridge(manifest=self.M, signed=None,
                            self_update=lambda v, a: True)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertNotIn("app", sent[0])

    def test_a_current_app_is_not_mentioned(self):
        """The field is an annotation on a firmware offer, not a fixture: a
        release with no newer daemon in it says nothing about the app."""
        v1 = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA}
        b, sent, _ = bridge(manifest=v1, self_update=lambda v, a: True)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertNotIn("app", sent[0])

    def test_the_app_is_updated_before_the_firmware(self):
        """Order is the whole point: the new daemon is the half that knows how
        to drive the new firmware."""
        order = []
        pending = FakePending()

        def self_update(version, artifact):
            order.append(("app", version))
            return True          # in reality this never returns

        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  self_update=self_update, pending=pending)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(order, [("app", "99.0.0")])
        self.assertEqual(flashed, [])        # the restart carries it on

    def test_consent_is_recorded_before_we_replace_ourselves(self):
        pending = FakePending()
        b, _, _ = bridge(manifest=self.M, firmware=b"hello", pending=pending,
                         self_update=lambda v, a: True)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(pending.writes, ["0.4.9"])

    def test_a_failed_app_update_still_installs_the_firmware(self):
        """Falling back is safe: the protocol floor was already checked, so
        the current app can drive this image. Better than nothing happening."""
        pending = FakePending()
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  pending=pending,
                                  self_update=lambda v, a: False)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])
        self.assertIsNone(pending.version)   # and the note is cleared

    def test_the_new_daemon_finishes_the_install_on_reconnect(self):
        pending = FakePending(version="0.4.9")
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])
        self.assertIn("ota_resume", types(sent))

    def test_consent_is_consumed_so_a_crash_loop_cannot_retry_forever(self):
        pending = FakePending(version="0.4.9")
        b, _, _ = bridge(manifest=self.M, firmware=b"hello", pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertIsNone(pending.version)

    def test_a_release_that_moved_on_is_not_installed(self):
        """The note records consent to a specific version, not consent to
        whatever the feed is serving by the time we get back."""
        pending = FakePending(version="0.4.7")
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [])

    def test_nothing_pending_is_the_ordinary_case(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  pending=FakePending())
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [])
        self.assertNotIn("ota_resume", types(sent))


class TestResumeSurvivesATransient(unittest.TestCase):
    """A resume that never got as far as installing must not spend consent.

    From a customer desk, 2026-09-10: the app half of a pair update landed and
    the firmware half did not. The board stayed on 1.2.5 with the app on
    1.3.2, the daemon answered `ota_none`, and the panel said nothing at all.

    The resume had taken the approval, failed to produce a manifest, and
    returned -- so the consent was gone. The board does not re-offer inside one
    boot either (ui_settings.c latches upd_prompt_done on "Update now"), so the
    two faults compound: the update is never offered again and never explained.
    """

    M = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA}

    def test_a_manifest_that_cannot_be_read_keeps_the_approval(self):
        pending = FakePending(version="0.4.9")
        b, sent, flashed = bridge(manifest=None, firmware=b"hello",
                                  pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [])
        self.assertEqual(pending.version, "0.4.9")   # still approved
        self.assertEqual(pending.tries, 1)

    def test_it_installs_when_the_feed_comes_back(self):
        """The whole point of keeping it: the next connect finishes the job."""
        pending = FakePending(version="0.4.9", tries=1)
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])
        self.assertIsNone(pending.version)

    def test_the_approval_is_dropped_once_the_tries_are_gone(self):
        """Bounded, so a permanently broken feed ends in an answer rather
        than a silent retry on every reconnect for the rest of the install."""
        from pc import update
        pending = FakePending(version="0.4.9", tries=update.RESUME_TRIES - 1)
        b, sent, flashed = bridge(manifest=None, firmware=b"hello",
                                  pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [])
        self.assertIsNone(pending.version)
        self.assertIn("ota_error", types(sent))

    def test_a_release_that_moved_on_is_not_retried(self):
        """Not a transient. The feed answered; what it serves is not what the
        user agreed to, and no number of retries will change that."""
        pending = FakePending(version="0.4.7")
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello",
                                  pending=pending)
        b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
        self.assertEqual(flashed, [])
        self.assertIsNone(pending.version)
        self.assertNotIn("ota_error", types(sent))


class TestManifestCompatibility(unittest.TestCase):
    """A manifest missing the newer blocks must still install firmware.

    Not about installs in the field -- there are none yet. It is about the
    manifests that are written by hand: BLINK_OTA_DIR feeds during
    development are typed out by whoever is testing, and a partially written
    one should not take the update path down with it. It becomes a
    compatibility guarantee the day the first customer's app reads a manifest.
    """

    def test_a_v1_manifest_still_drives_a_firmware_update(self):
        v1 = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA}
        b, sent, flashed = bridge(manifest=v1, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])

    def test_unknown_keys_are_ignored(self):
        v3 = {"version": "0.4.9", "size": 5, "sha256": HELLO_SHA,
              "schema": 7, "something": {"we": "have not invented"}}
        b, sent, flashed = bridge(manifest=v3, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])


class TestTheFetchTrustsCertifi(unittest.TestCase):
    """The frozen macOS binary's OpenSSL looks for its CA bundle at a path
    that exists only on a Mac with python.org's Python installed; without a
    bundle of our own every fetch failed and no installed device could update
    (2026-08-29). The context must carry certifi's roots even when the
    system offers none."""

    def test_the_context_carries_certifi_roots_without_a_system_bundle(self):
        import os
        import certifi
        want = open(certifi.where(), "rb").read().count(b"BEGIN CERTIFICATE")
        self.assertGreater(want, 100)
        saved = {k: os.environ.get(k) for k in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        try:
            # OpenSSL honours these over its compiled-in default, so pointing
            # them nowhere is a machine with no certificate store at all.
            os.environ["SSL_CERT_FILE"] = "/nonexistent/cert.pem"
            os.environ["SSL_CERT_DIR"] = "/nonexistent/certs"
            have = ota._ssl_context().cert_store_stats()["x509_ca"]
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertGreaterEqual(have, want)


class TestOfferOnConnect(unittest.TestCase):
    """A board asked for itself once per boot and when tapped, and that was
    all: a board that stayed plugged in through three app updates never
    heard about their firmware, and a tap that landed while the app was
    restarting went unanswered for good (2026-08-29). Every connect is now
    a check from the daemon's side."""
    M = {"version": "0.4.9", "size": 600, "sha256": "ab" * 32}

    def test_a_hello_is_answered_with_an_offer_when_newer_exists(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "v": 2, "board_id": "ab", "fw": "0.4.8"})
        self.assertIn("ota_avail", types(sent))
        self.assertEqual([m for m in sent if m.get("t") == "ota_avail"][0]["version"], "0.4.9")

    def test_a_hello_from_a_current_board_hears_up_to_date(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "v": 2, "board_id": "ab", "fw": "0.4.9"})
        self.assertIn("ota_none", types(sent))

    def test_a_hello_without_a_version_offers_nothing(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "hello", "v": 2, "board_id": "ab"})
        self.assertNotIn("ota_avail", types(sent))
        self.assertNotIn("ota_none", types(sent))

    def test_a_reconnect_uses_the_remembered_version(self):
        b, sent, _ = bridge(manifest=self.M)
        b.offer_if_newer("0.4.8")
        self.assertEqual(types(sent), ["ota_avail"])

    def test_the_boards_own_query_is_not_answered_twice(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.offer_if_newer("0.4.8")
        self.assertEqual(types(sent).count("ota_avail"), 1)


class TestNoWindow(unittest.TestCase):
    """The hidden Windows daemon must not hand its children a console: a
    black window sat on the desktop for the length of every firmware flash
    (2026-08-30). Off Windows the flag set is empty and nothing changes."""

    def setUp(self):
        self._tools = (ota._esptool, ota._efuse_probe)

    def tearDown(self):
        ota._esptool, ota._efuse_probe = self._tools

    def test_esptool_and_the_efuse_probe_are_started_without_a_window(self):
        seen = []
        def run(cmd, **kw):
            seen.append(kw)
            class R:
                returncode = 0
                stdout = "flash_encryption disabled\n"
                stderr = ""
            return R()
        ota._esptool = lambda: ["python", "-m", "esptool"]
        ota._efuse_probe = lambda: ["python", "-m", "pc.efuse_probe"]
        ota.flash("/dev/null", b"x" * 16, run=run)
        self.assertGreaterEqual(len(seen), 2)
        for kw in seen:
            for k, v in ota.NO_WINDOW.items():
                self.assertEqual(kw.get(k), v)

    def test_the_flag_is_windows_only(self):
        import sys
        if sys.platform == "win32":
            self.assertEqual(ota.NO_WINDOW, {"creationflags": 0x08000000})
        else:
            self.assertEqual(ota.NO_WINDOW, {})


class TestTheEfuseProbeAnswer(unittest.TestCase):
    """flash_encrypted_chip reads exactly what pc/efuse_probe.py prints."""

    def _run_saying(self, text):
        def run(cmd, **kw):
            class R:
                returncode = 0
                stdout = text
                stderr = ""
            return R()
        return run

    def test_enabled_disabled_and_nonsense(self):
        self.assertIs(ota.flash_encrypted_chip(
            "p", run=self._run_saying("flash_encryption enabled\n")), True)
        self.assertIs(ota.flash_encrypted_chip(
            "p", run=self._run_saying("Connecting...\nflash_encryption disabled\n")),
            False)
        self.assertIsNone(ota.flash_encrypted_chip(
            "p", run=self._run_saying("Traceback (most recent call last)\n")))

    def test_a_probe_that_never_answers_says_why(self):
        """It used to say nothing at all.

        The probe's stderr was discarded, so a daemon that could not read the
        eFuses logged no reason -- and the only visible symptom was 47
        characters on the board's screen about a chip.
        """
        def denied(cmd, **kw):
            class R:
                returncode = 1
                stdout = ""
                stderr = "could not open port 'COM25': Access is denied."
            return R()

        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            self.assertIsNone(ota.flash_encrypted_chip(
                "COM25", run=denied))
        finally:
            sys.stderr = real
        said = err.getvalue()
        self.assertIn("could not read the chip's eFuses", said)
        self.assertIn("COM25", said)
        self.assertIn("Access is denied", said)

    def test_an_exception_from_the_runner_is_reported(self):
        tries = []

        def boom(cmd, **kw):
            tries.append(cmd)
            raise OSError("the port vanished")

        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            self.assertIsNone(ota.flash_encrypted_chip(
                "COM25", run=boom))
        finally:
            sys.stderr = real
        self.assertEqual(len(tries), 1)
        self.assertIn("the port vanished", err.getvalue())

    def test_the_probe_is_the_bundled_module_when_frozen(self):
        import sys
        had = getattr(sys, "frozen", None)
        sys.frozen = True
        try:
            self.assertEqual(ota._efuse_probe()[1:], ["-m", "pc.efuse_probe"])
        finally:
            if had is None:
                del sys.frozen
            else:
                sys.frozen = had
        self.assertTrue(ota._efuse_probe()[-1].endswith("efuse_probe.py"))
