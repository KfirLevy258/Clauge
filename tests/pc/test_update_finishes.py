"""What `blink update` leaves behind when it is done.

An update is two halves: the program on disk, and a daemon running it. Until
2026-09-09 this command only checked the first. It printed whatever the
backend said about the restart and returned 0 either way, so a service that
never came back was one line in the middle of an update that read as a
success -- and the visible symptom was on the other side of the cable, where
the board sat on "Link the PC daemon" with nothing on the computer explaining
it. A customer upgrading 1.2.5 -> 1.3.2 spent that afternoon there.

The other half is the shims. Both ship inside the binary and are copied out
at install time, so a swap that replaces only the program leaves the previous
release's shims on disk. The daemon's DriftWatchdog repairs them within five
minutes of starting -- which is no help at all on a machine where the daemon
is the thing that did not start.
"""
import os

import pytest

from pc import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A ~/.blink with both shims already installed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    blink = tmp_path / ".blink"
    blink.mkdir()
    for name in ("blink-statusline.sh", "blink-hook.sh"):
        (blink / name).write_text("#!/bin/sh\n# an older release wrote this\n")
    return tmp_path


# --------------------------------------------------------------- the verdict

def test_the_exact_word_is_the_only_success():
    assert cli.restart_left_it_down("restarted") is False


def test_a_restart_that_left_nothing_running_is_not_a_success():
    """The trap this predicate exists for.

    Every backend's failure string BEGINS with the success string, so a
    startswith() test -- the obvious way to write this -- calls the failure a
    success. That is not hypothetical: `4ba9c7d fix: Linux and Windows
    claimed a running service from an exit code` is the same mistake one
    layer down, and the reason restart() was taught to check at all.
    """
    for detail in (
        "restarted, but launchd reports it is not running -- see /tmp/x.log",
        "restarted, but systemd reports it is not running -- see: systemctl",
        "restarted, but it is not running -- see /tmp/x.log",
    ):
        assert cli.restart_left_it_down(detail) is True, detail


def test_a_restart_that_could_not_run_is_not_a_success():
    assert cli.restart_left_it_down("could not restart it") is True


def test_nothing_attempted_is_not_a_fault():
    """A skip and an unsupervised platform did not fail; they did not try.

    Reporting these as a broken service would tell every developer running
    from a checkout, and every test run in this repository, to go and repair
    an install that is working exactly as intended.
    """
    assert cli.restart_left_it_down("skipped (BLINK_SKIP_SERVICE=1)") is False
    assert cli.restart_left_it_down(
        "not running under a supervisor; restart it yourself") is False


# ---------------------------------------------------------------- the shims

def test_update_rewrites_the_shims_it_finds(home, monkeypatch):
    written = []
    monkeypatch.setattr(cli, "_write_shim",
                        lambda path, name: written.append((path, name)))
    cli._refresh_shims()
    assert sorted(n for _, n in written) == ["blink-hook.sh",
                                             "blink-statusline.sh"]


def test_update_does_not_create_a_shim_that_was_not_there(home, monkeypatch):
    """An update must not turn a machine with no install into half of one.

    `blink uninstall` removes these files and leaves ~/.blink standing. If
    update wrote them back, the next status would report an activity feature
    that the user had deliberately removed.
    """
    os.remove(os.path.join(str(home), ".blink", "blink-hook.sh"))
    written = []
    monkeypatch.setattr(cli, "_write_shim",
                        lambda path, name: written.append((path, name)))
    cli._refresh_shims()
    assert [n for _, n in written] == ["blink-statusline.sh"]


def test_a_shim_that_cannot_be_written_does_not_fail_the_update(home,
                                                                monkeypatch,
                                                                capsys):
    """The program swap has already succeeded by the time this runs.

    Raising here would report an update that worked as one that failed, and
    send the user to download it again -- which would not help, because the
    new program is already on disk.
    """
    def boom(path, name):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(cli, "_write_shim", boom)
    cli._refresh_shims()                     # must not raise
    out = capsys.readouterr().out
    assert "could not refresh blink-hook.sh" in out
