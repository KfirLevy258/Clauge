"""Taking the serial port off the installed service, and giving it back.

The fleet suite runs the real daemon against a real board on the user's own
machines, and only one process can hold a serial port -- so a run has to stop
the login service first and put it back in a finally block. That makes this
the code most able to leave one of those desks dark, which is why every test
here injects a runner and the fixture below turns a real subprocess call into
a failure. tests/conftest.py records what happened the last time login-service
code ran for real in a unit test.

The backends are exercised for all four platforms on whichever machine runs
this, for the reason given on cli._Backend: argv is most of what there is to
get wrong, and it can only be reviewed here.
"""
import os
import subprocess
import sys

import pytest

from pc import cli, service_ctl


class _RealCommandAttempted(BaseException):
    """Derived from BaseException on purpose.

    service_ctl catches Exception so that its callers never see one, and an
    AssertionError is an Exception -- a tripwire built from one would be
    swallowed by the very code it is watching, turning "this test shelled out
    to launchctl for real" into a silent pass.
    """


class _Runs:
    """Stands in for subprocess.run: records argv, replays exit codes."""

    def __init__(self, codes=None):
        self.calls = []
        self._codes = list(codes or [])

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = self._codes.pop(0) if self._codes else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="")

    @property
    def joined(self):
        return " | ".join(" ".join(c) for c in self.calls)

    def ran(self, *words):
        """True if one call contains all of `words`, in order."""
        for call in self.calls:
            line, at = " ".join(call), -1
            for w in words:
                at = line.find(w, at + 1)
                if at < 0:
                    break
            else:
                return True
        return False


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME is redirected by tests/conftest.py; this adds the stubs."""
    def _tripwire(argv, **kw):
        raise _RealCommandAttempted(" ".join(map(str, argv)))

    monkeypatch.setattr(cli.subprocess, "run", _tripwire)
    # Deterministic, and present on every platform this suite runs on.
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    return tmp_path


def _platform(monkeypatch, name):
    monkeypatch.setattr(cli.sys, "platform", name)


def _write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def _pretend_installed(monkeypatch):
    """Put this machine's own service artifact in the sandbox HOME."""
    if sys.platform == "darwin":
        _write(cli.plist_path())
    elif sys.platform.startswith("linux"):
        monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
        _write(cli.unit_path())
    # Windows keeps the state in the Scheduled Task, not in a file, so the
    # stubbed schtasks /query answering 0 is what "installed" means there.


# ---------------------------------------------------------------- launchd --

def test_launchd_stop_boots_the_agent_out(home, monkeypatch):
    # Not `launchctl stop`: the plist sets KeepAlive, so launchd would start
    # it straight back up and the port would still be held.
    _platform(monkeypatch, "darwin")
    _write(cli.plist_path())
    r = _Runs()
    assert cli._LaunchdBackend().stop(r) == "stopped"
    assert r.ran("launchctl", "bootout", "gui/501/com.blink.bridge")


def test_launchd_start_bootstraps_the_installed_plist(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _write(cli.plist_path())
    r = _Runs()
    assert cli._LaunchdBackend().start(r) == "started"
    assert r.ran("launchctl", "bootstrap", "gui/501", cli.plist_path())


def test_launchd_stop_reports_a_failure_with_the_command_to_run(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _write(cli.plist_path())
    r = _Runs(codes=[1])
    out = cli._LaunchdBackend().stop(r)
    assert "could not stop it" in out and "launchctl bootout" in out


def test_launchd_without_a_plist_is_not_an_error(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    r = _Runs()
    assert cli._LaunchdBackend().stop(r) == "not installed"
    assert cli._LaunchdBackend().start(r) == "not installed"
    assert r.calls == []


# --------------------------------------------------------------- schtasks --

@pytest.fixture
def killed(monkeypatch):
    """Records _kill_recorded_daemon, and the runner it was handed."""
    seen = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon",
                        lambda runner=None: seen.append(runner))
    return seen


def test_schtasks_stop_ends_the_task_and_kills_the_detached_daemon(
        home, monkeypatch, killed):
    # /end only reaches the instance the task launched. A daemon that replaced
    # itself started its successor detached, and that one keeps COM15 open --
    # a stop that leaves it running reads as a hardware fault.
    _platform(monkeypatch, "win32")
    r = _Runs()
    assert cli._SchtasksBackend().stop(r) == "stopped"
    assert r.ran("schtasks", "/end", "/tn", cli.TASK_NAME)
    assert killed == [r]


def test_schtasks_stop_kills_the_daemon_even_with_no_task_registered(
        home, monkeypatch, killed):
    _platform(monkeypatch, "win32")
    r = _Runs(codes=[1])                       # /query: no such task
    assert cli._SchtasksBackend().stop(r) == "not installed"
    assert not r.ran("/end")
    assert killed == [r]                       # ...but the port is still freed


def test_schtasks_start_runs_the_task(home, monkeypatch, killed):
    _platform(monkeypatch, "win32")
    r = _Runs()
    assert cli._SchtasksBackend().start(r) == "started"
    assert r.ran("schtasks", "/run", "/tn", cli.TASK_NAME)
    assert killed == []                        # start() never kills anything


def test_schtasks_start_without_the_task_is_not_an_error(home, monkeypatch, killed):
    _platform(monkeypatch, "win32")
    r = _Runs(codes=[1])
    assert cli._SchtasksBackend().start(r) == "not installed"
    assert not r.ran("/run")


# ---------------------------------------------------------------- systemd --

def test_systemd_stop_and_start_the_user_unit(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    _write(cli.unit_path())
    r = _Runs()
    assert cli._SystemdBackend().stop(r) == "stopped"
    assert cli._SystemdBackend().start(r) == "started"
    assert r.ran("systemctl", "--user", "stop", "blink-bridge.service")
    assert r.ran("systemctl", "--user", "start", "blink-bridge.service")


def test_systemd_stop_falls_back_when_there_is_no_systemctl(home, monkeypatch):
    # A Linux box without systemd is not a broken machine: it gets the base
    # class's honest answer rather than a crash.
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    _write(cli.unit_path())
    r = _Runs()
    assert "yourself" in cli._SystemdBackend().stop(r)
    assert "yourself" in cli._SystemdBackend().start(r)
    assert r.calls == []


def test_systemd_without_a_unit_file_is_not_an_error(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _Runs()
    assert cli._SystemdBackend().stop(r) == "not installed"
    assert cli._SystemdBackend().start(r) == "not installed"
    assert r.calls == []


# ------------------------------------------------------------------- base --

def test_an_unknown_platform_says_what_it_cannot_do(home, monkeypatch):
    _platform(monkeypatch, "freebsd14")
    r = _Runs()
    assert "yourself" in cli.backend().stop(r)
    assert "yourself" in cli.backend().start(r)
    assert r.calls == []


# ----------------------------------------------------------------- safety --

def test_stopping_and_starting_never_installs_or_removes(home, monkeypatch, killed):
    """The one thing this pair must never do.

    stop/start bracket a test run on a machine someone works on: a stray
    /create, bootstrap of a plist we wrote, or /delete would rewrite the
    user's own installation on the way past.
    """
    forbidden = ("/create", "/delete", "enable", "disable", "unload", "load",
                 "daemon-reload", "bootout gui/501 ")
    for platform, artifact in (("darwin", cli.plist_path),
                               ("linux", cli.unit_path),
                               ("win32", None)):
        _platform(monkeypatch, platform)
        monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
        if artifact:
            _write(artifact())
        r = _Runs()
        cli.backend().stop(r)
        cli.backend().start(r)
        assert not any(w in r.joined for w in forbidden), r.joined
        if artifact:
            assert os.path.exists(artifact()), f"{platform} deleted its own unit"


# ------------------------------------------------------- the thin wrappers --

def test_no_unit_test_can_stop_the_real_service(home):
    """BLINK_SKIP_SERVICE, set for every test by tests/conftest.py."""
    r = _Runs()
    assert "skipped" in service_ctl.stop_service(runner=r)
    assert "skipped" in service_ctl.start_service(runner=r)
    assert r.calls == []


def test_stop_and_start_drive_this_machine_platform(home, monkeypatch):
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _pretend_installed(monkeypatch)
    r = _Runs()
    service_ctl.stop_service(runner=r)
    service_ctl.start_service(runner=r)
    if sys.platform == "darwin":
        assert "launchctl" in r.joined
    elif sys.platform.startswith("linux"):
        assert "systemctl" in r.joined
    elif sys.platform == "win32":
        assert "schtasks" in r.joined
    else:
        assert r.calls == []                   # no supervisor to drive


def test_a_failing_command_is_reported_not_raised(home, monkeypatch):
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _pretend_installed(monkeypatch)
    out = service_ctl.stop_service(runner=_Runs(codes=[1, 1, 1]))
    assert isinstance(out, str) and "not" in out.lower()


def test_a_missing_tool_is_reported_not_raised(home, monkeypatch):
    """The fleet agent calls start_service from a finally block: an exception
    raised there would replace the real test failure with this one."""
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _pretend_installed(monkeypatch)

    def _absent(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert isinstance(service_ctl.stop_service(runner=_absent), str)
    assert isinstance(service_ctl.start_service(runner=_absent), str)
