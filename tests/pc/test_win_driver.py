"""The CH340 driver: what is detected, what is said, and what is run.

The ctypes half of pc/win_driver only runs on Windows, so what is pinned here
is everything either side of it -- how a device instance id is read, how
pnputil's output is understood, what a customer is told, and that none of it
does anything at all on a machine that is not Windows.

That last one is not a formality. This module is imported by pc/cli.py at the
top level and by the daemon's wait loop, on every platform, and the failure it
guards against is an ImportError or an AttributeError on macOS taking out
`blink status` for everybody.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pc import win_driver                                       # noqa: E402
from pc.cli import board_lines                                  # noqa: E402


def undriven(problem=win_driver.NO_DRIVER_BOUND,
             instance_id="USB\\VID_1A86&PID_7523\\5&1D2B3C4&0&2"):
    """One Undriven the way undriven_boards() would have built it.

    The default is NO_DRIVER_BOUND rather than problem 28, because that is
    the case a real customer is in: measured on the release desk, a CH340 on
    a machine with no driver carries no problem code at all.
    """
    return win_driver.Undriven(instance_id, 0x1A86, 0x7523, problem,
                               win_driver.problem_note(problem))


class TestReadingADeviceId:
    def test_the_vid_and_pid_come_out_of_an_instance_id(self):
        assert win_driver.ids_from(
            "USB\\VID_1A86&PID_7523\\5&1D2B3C4&0&2") == (0x1A86, 0x7523)

    def test_windows_writes_them_in_upper_case_but_neither_is_assumed(self):
        assert win_driver.ids_from("USB\\vid_1a86&pid_7523\\x") == (0x1A86, 0x7523)

    def test_a_device_with_no_vid_is_not_one_of_ours(self):
        # Root-enumerated devices, and a good half of what is on any machine.
        assert win_driver.ids_from("ROOT\\SYSTEM\\0000") is None
        assert win_driver.ids_from("") is None
        assert win_driver.ids_from(None) is None

    def test_the_ch340_wants_the_wch_driver(self):
        assert win_driver.wants_wch_driver("USB\\VID_1A86&PID_7523\\a")

    def test_chips_windows_already_drives_are_left_alone(self):
        # The CP210x and the FTDI part have had an in-box Windows driver for
        # years, and the ESP32-S3's native USB is a standard CDC device.
        # Offering a WCH driver for any of them would be wrong and would fail.
        assert not win_driver.wants_wch_driver("USB\\VID_10C4&PID_EA60\\a")
        assert not win_driver.wants_wch_driver("USB\\VID_0403&PID_6001\\a")
        assert not win_driver.wants_wch_driver("USB\\VID_303A&PID_1001\\a")


class TestProblemNotes:
    def test_the_missing_driver_case_reads_as_a_missing_driver(self):
        assert win_driver.problem_note(28) == "Windows has no driver for it"

    def test_no_problem_code_still_reads_as_a_missing_driver(self):
        # Code 0 reaches this only for a device with no driver bound, which
        # is what an undriven board on Windows 10 actually reports -- no
        # problem flag whatsoever. It must not read as "everything is fine"
        # to somebody staring at a board that does nothing.
        assert (win_driver.problem_note(win_driver.NO_DRIVER_BOUND)
                == "Windows has no driver for it")

    def test_a_code_with_no_wording_still_says_something_usable(self):
        # Support can act on a number. It cannot act on a KeyError.
        assert "97" in win_driver.problem_note(97)


class TestWhichStatesAreWorthOfferingADriverFor:
    def test_no_driver_bound_and_a_failed_install_both_are(self):
        assert win_driver.NO_DRIVER_BOUND in win_driver.DRIVER_FIXES
        assert win_driver.CM_PROB_FAILED_INSTALL in win_driver.DRIVER_FIXES

    def test_a_disabled_or_damaged_device_is_not(self):
        # Installing a driver does not re-enable a device somebody turned
        # off, repair a registry entry, or perform a pending restart.
        for code in (22, 19, 14, 10, 43):
            assert code not in win_driver.DRIVER_FIXES


class TestReadingTheDriverStore:
    def test_a_staged_package_is_recognised_by_its_directory(self):
        assert win_driver.store_dir_has_driver([
            "usbser.inf_amd64_1111111111111111",
            "ch341ser.inf_amd64_9a1f0b3c2d4e5f60",
        ])

    def test_an_unstaged_store_says_so(self):
        assert not win_driver.store_dir_has_driver([
            "usbser.inf_amd64_1111111111111111", "prnms003.inf_amd64_x"])

    def test_a_package_merely_mentioning_the_name_is_not_it(self):
        # Matched as a prefix, not a substring: some other vendor's package
        # named after ours must not read as ours being installed.
        assert not win_driver.store_dir_has_driver(["oem_ch341ser.inf_amd64_x"])

    def test_an_empty_store_is_not_a_crash(self):
        assert not win_driver.store_dir_has_driver([])

    def test_the_store_is_read_without_asking_for_administrator(self,
                                                                monkeypatch):
        # The behaviour this feature turns on. `pnputil /enum-drivers` wants
        # an administrator, so reaching for it here would raise the Windows
        # permission prompt on every install on every machine -- including
        # the ones with nothing to do.
        def never(*a, **k):
            raise AssertionError("pnputil was run to answer a free question")
        monkeypatch.setattr(win_driver, "_pnputil", never)
        monkeypatch.setattr(os, "listdir",
                            lambda p: ["ch341ser.inf_amd64_abc"])
        assert win_driver.driver_in_store() is True

    def test_an_unlistable_store_falls_back_to_pnputil(self, monkeypatch):
        def no_dir(p):
            raise PermissionError(p)
        monkeypatch.setattr(os, "listdir", no_dir)
        monkeypatch.setattr(win_driver, "_pnputil", lambda *a, **k:
                            subprocess.CompletedProcess(
                                [], 0, "Original Name: ch341ser.inf\n", ""))
        assert win_driver.driver_in_store() is True

    def test_the_inf_name_is_found_however_the_labels_are_translated(self):
        # The Windows release desk runs a non-English profile. A check keyed
        # to the English "Original Name:" would report "not installed" there
        # forever and re-elevate on every install.
        hebrew = ("שם שפורסם:            oem42.inf\n"
                  "שם מקורי:             ch341ser.inf\n")
        assert win_driver.store_has_driver(hebrew.lower())

    def test_a_store_without_it_says_so(self):
        assert not win_driver.store_has_driver(
            "Published Name: oem1.inf\nOriginal Name: usbser.inf\n")

    def test_no_output_at_all_is_not_a_driver(self):
        assert not win_driver.store_has_driver("")
        assert not win_driver.store_has_driver(None)


class TestWhatTheCustomerIsTold:
    def test_an_install_that_worked_says_so_and_nothing_more(self):
        assert win_driver.install_message("installed") == "installed"

    def test_a_reboot_requirement_is_reported_as_success_with_a_condition(self):
        line = win_driver.install_message("installed", "restart the machine to finish")
        assert line.startswith("installed")
        assert "restart" in line

    def test_a_build_with_no_driver_names_where_to_get_one(self):
        line = win_driver.install_message("absent")
        assert win_driver.DRIVER_PAGE in line

    def test_a_declined_prompt_does_not_read_as_a_failure(self):
        line = win_driver.install_message("declined")
        assert "fail" not in line.lower()
        assert "blink driver" in line

    def test_a_real_failure_carries_its_reason(self):
        assert "pnputil exited 5" in win_driver.install_message(
            "failed", "pnputil exited 5")


class TestSummaryAndAdvice:
    def test_nothing_wrong_produces_no_words(self):
        assert win_driver.summary([]) == ""
        assert win_driver.advice([]) == []

    def test_one_board_is_singular(self):
        assert win_driver.summary([undriven()]).startswith("a board is")

    def test_two_boards_are_counted(self):
        assert win_driver.summary([undriven(), undriven()]).startswith(
            "2 boards are")

    def test_a_missing_driver_is_pointed_at_the_command_that_fixes_it(self):
        assert win_driver.advice([undriven()], "blink") == [
            "run `blink driver` to install it"]

    def test_a_failed_install_is_pointed_there_too(self):
        assert win_driver.advice([undriven(problem=28)], "blink") == [
            "run `blink driver` to install it"]

    def test_the_command_is_the_path_the_customer_actually_has(self):
        # A customer whose PATH has no `blink` on it -- which is every
        # Windows customer, the binary lives in ~/.blink/bin -- must be given
        # the path they can paste.
        line = win_driver.advice([undriven()], r"C:\Users\x\.blink\bin\blink.exe")[0]
        assert r"C:\Users\x\.blink\bin\blink.exe driver" in line

    def test_a_disabled_device_is_not_pointed_at_the_driver_command(self):
        # `blink driver` cannot re-enable a device somebody disabled on
        # purpose, and sending them round that loop wastes a support pass.
        advice = win_driver.advice([undriven(problem=22)], "blink")
        assert advice == ["open Device Manager to see what it says about that device"]


class TestBoardLines:
    """The regression this whole module exists for."""

    def test_an_undriven_board_is_no_longer_reported_as_unplugged(self):
        lines = board_lines({}, [], [undriven()], "blink")
        assert "not plugged in" not in "\n".join(lines)
        assert lines[0] == ("Board       a board is plugged in, but Windows"
                            " has no driver for it")
        assert lines[1] == "            run `blink driver` to install it"

    def test_an_empty_desk_still_reads_as_an_empty_desk(self):
        assert board_lines({}, [], [], "blink") == ["Board       not plugged in"]

    def test_the_remembered_port_survives_the_undriven_branch(self):
        lines = board_lines({"port": "COM15"}, [], [undriven()], "blink")
        assert lines[-1] == "            last seen on COM15"

    def test_a_working_board_and_an_undriven_one_are_both_reported(self):
        # One board driven on COM3, a second plugged in that Windows cannot
        # use. The second is on no serial port, so nothing in `ports` knows
        # about it and the first board's line would otherwise hide it.
        lines = board_lines({"port": "COM3", "board_id": "abc"},
                            [("COM3", "CH340")], [undriven()], "blink")
        assert lines[0].startswith("Board       COM3 (CH340)")
        assert any("also:" in line for line in lines)
        assert any("blink driver" in line for line in lines)

    def test_the_old_signature_still_works(self):
        # Called with two arguments all over the existing tests, and the
        # default has to mean "no undriven boards", not "crash".
        assert board_lines({}, [("COM7", "CH340")]) == [
            "Board       none identified yet -- looking at COM7 (CH340)"]


class TestOffWindowsNothingHappens:
    """Imported on every platform; must do nothing at all on most of them."""

    def test_no_boards_are_ever_undriven(self):
        if sys.platform == "win32":
            return
        assert win_driver.undriven_boards() == []

    def test_nothing_is_ever_elevated(self):
        if sys.platform == "win32":
            return
        assert win_driver.is_elevated() is False

    def test_installing_is_a_no_op_that_reports_itself(self):
        if sys.platform == "win32":
            return
        assert win_driver.install_driver() == ("not-windows", "")
        status, line = win_driver.ensure_driver()
        assert status == "not-windows"
        assert line == "not needed on this system"

    def test_the_driver_command_says_so_and_succeeds(self, capsys):
        if sys.platform == "win32":
            return
        from pc.cli import cmd_driver
        assert cmd_driver(None) == 0
        assert "no driver" in capsys.readouterr().out


class TestDetectionNeverThrows:
    def test_a_missing_system_library_costs_a_line_not_the_command(self,
                                                                  monkeypatch):
        # `blink status` calls this. A status command that dies because
        # cfgmgr32 moved is worse than one that omits a line.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(win_driver, "_cfgmgr",
                            lambda: (_ for _ in ()).throw(OSError("gone")))
        assert win_driver.undriven_boards() == []

    def test_pnputil_being_absent_is_an_unknown_not_a_crash(self, monkeypatch):
        # Both sources gone: the store directory unlistable AND pnputil
        # missing. The directory has to be taken away explicitly -- on
        # Windows it is genuinely there and genuinely readable, so without
        # this the test passed for the wrong reason everywhere else and
        # failed on the one platform the code runs on (caught on the release
        # desk, 2026-09-06).
        def no_dir(p):
            raise PermissionError(p)

        def boom(*a, **k):
            raise FileNotFoundError("pnputil")
        monkeypatch.setattr(os, "listdir", no_dir)
        monkeypatch.setattr(subprocess, "run", boom)
        assert win_driver.driver_in_store() is None


class TestTheBundledPackage:
    def test_a_build_with_no_driver_staged_finds_none(self, monkeypatch,
                                                      tmp_path):
        # The default state of every checkout: vendor/ is git-ignored and
        # empty until somebody runs tools/fetch_ch340_driver.sh.
        monkeypatch.setattr(win_driver, "_bundle_roots", lambda: iter(()))
        monkeypatch.setattr(win_driver, "VENDOR_SUBDIR", ("nowhere",))
        assert win_driver.driver_package() is None

    def test_a_staged_package_is_found_in_the_bundle(self, monkeypatch,
                                                     tmp_path):
        staged = tmp_path / "drivers" / "ch341ser"
        staged.mkdir(parents=True)
        (staged / win_driver.INF_NAME).write_text("[Version]\n")
        monkeypatch.setattr(win_driver, "_bundle_roots",
                            lambda: iter([str(tmp_path)]))
        assert win_driver.driver_package() == str(staged / win_driver.INF_NAME)

    def test_the_capitals_wch_actually_ships_are_found(self, monkeypatch,
                                                       tmp_path):
        # The real package is CH341SER.INF, and it stays in capitals through
        # the Windows driver store -- which is where the release build's copy
        # is taken from.
        staged = tmp_path / "drivers" / "ch341ser"
        staged.mkdir(parents=True)
        (staged / "CH341SER.INF").write_text("[Version]\n")
        monkeypatch.setattr(win_driver, "_bundle_roots",
                            lambda: iter([str(tmp_path)]))
        assert win_driver.driver_package() == str(staged / "CH341SER.INF")

    def test_a_directory_with_no_inf_is_not_a_package(self, monkeypatch,
                                                      tmp_path):
        staged = tmp_path / "drivers" / "ch341ser"
        staged.mkdir(parents=True)
        (staged / "CH341S64.SYS").write_text("")
        monkeypatch.setattr(win_driver, "_bundle_roots",
                            lambda: iter([str(tmp_path)]))
        monkeypatch.setattr(win_driver, "VENDOR_SUBDIR", ("nowhere",))
        assert win_driver.driver_package() is None


class TestInstallSteps:
    def test_windows_has_one_step_more_than_the_others(self):
        from pc import cli
        assert cli._install_steps() == (6 if sys.platform == "win32" else 5)

    def test_the_stepper_counts_to_the_total_it_was_given(self, capsys):
        from pc import cli
        step = cli._stepper(6)
        step("Program")
        step("Status line")
        out = capsys.readouterr().out
        assert "[1/6] Program ... " in out
        assert "[2/6] Status line ... " in out
