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
    """Stands in for subprocess.run: records argv and kwargs, replays codes."""

    def __init__(self, codes=None):
        self.calls = []
        self.kwargs = []
        self._codes = list(codes or [])

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kwargs.append(kw)
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


class _Killer:
    """Stands in for cli._kill_recorded_daemon.

    Records the runner it was handed, and reports how many daemons it found
    -- which is what tells a Windows stop with no Scheduled Task whether it
    freed the port or found nothing to free.
    """

    def __init__(self):
        self.runners = []
        self.count = 0

    def __call__(self, runner=None):
        self.runners.append(runner)
        return self.count


class _RecordingBackend:
    """A backend that does nothing but remember which runner it was handed."""

    def __init__(self):
        self.handed = None

    def stop(self, runner=None):
        self.handed = runner
        return "stopped"

    def start(self, runner=None):
        self.handed = runner
        return "started"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME is redirected by tests/conftest.py; this adds the stubs."""
    def _tripwire(argv, **kw):
        raise _RealCommandAttempted(" ".join(map(str, argv)))

    monkeypatch.setattr(cli.subprocess, "run", _tripwire)
    # Deterministic, and present on every platform this suite runs on.
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    return tmp_path


@pytest.fixture
def killer(monkeypatch):
    k = _Killer()
    monkeypatch.setattr(cli, "_kill_recorded_daemon", k)
    return k


def _platform(monkeypatch, name):
    monkeypatch.setattr(cli.sys, "platform", name)


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


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

def test_schtasks_stop_ends_the_task_and_kills_the_detached_daemon(
        home, monkeypatch, killer):
    # /end only reaches the instance the task launched. A daemon that replaced
    # itself started its successor detached, and that one keeps COM15 open --
    # a stop that leaves it running reads as a hardware fault.
    _platform(monkeypatch, "win32")
    r = _Runs()
    assert cli._SchtasksBackend().stop(r) == "stopped"
    assert r.ran("schtasks", "/end", "/tn", cli.TASK_NAME)
    assert killer.runners == [r]


def test_schtasks_stop_kills_the_daemon_even_with_no_task_registered(
        home, monkeypatch, killer):
    _platform(monkeypatch, "win32")
    killer.count = 1                           # a detached daemon was running
    r = _Runs(codes=[1])                       # /query: no such task
    # The port really was freed, so the answer does not deny it was held.
    assert cli._SchtasksBackend().stop(r) == "not installed; killed a detached daemon"
    assert not r.ran("/end")
    assert killer.runners == [r]


def test_schtasks_stop_with_nothing_at_all_running(home, monkeypatch, killer):
    _platform(monkeypatch, "win32")
    r = _Runs(codes=[1])
    assert cli._SchtasksBackend().stop(r) == "not installed"


def test_schtasks_start_runs_the_task(home, monkeypatch, killer):
    _platform(monkeypatch, "win32")
    r = _Runs()
    assert cli._SchtasksBackend().start(r) == "started"
    assert r.ran("schtasks", "/run", "/tn", cli.TASK_NAME)
    assert killer.runners == []                # start() never kills anything


def test_schtasks_start_without_the_task_is_not_an_error(home, monkeypatch, killer):
    _platform(monkeypatch, "win32")
    r = _Runs(codes=[1])
    assert cli._SchtasksBackend().start(r) == "not installed"
    assert not r.ran("/run")


def test_the_recorded_daemon_is_killed_by_pid_through_the_runner(home, monkeypatch):
    """The kill itself: by pid, with the runner it was handed, counted."""
    _platform(monkeypatch, "win32")
    monkeypatch.setattr(cli.update.ota, "NO_WINDOW", {"creationflags": 0x08000000})
    _write(cli.pid_path(), "424242")
    r = _Runs()
    assert cli._kill_recorded_daemon(runner=r) == 1
    assert r.ran("taskkill", "/f", "/t", "/pid", "424242")
    assert r.kwargs[0].get("creationflags") == 0x08000000


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

def test_stopping_and_starting_never_installs_or_removes(home, monkeypatch, killer):
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


def test_every_command_hides_the_console_window(home, monkeypatch, killer):
    """The v1.2.1 fix: nothing Blink starts may flash a console window.

    update.ota.NO_WINDOW is an empty dict off Windows, so asserting on its
    real value would be vacuous on the machine most likely to run this suite
    -- and a call site that dropped the spread would stay green. A sentinel
    value is what actually pins it.
    """
    monkeypatch.setattr(cli.update.ota, "NO_WINDOW", {"creationflags": 0x08000000})
    for platform, artifact in (("win32", None),
                               ("darwin", cli.plist_path),
                               ("linux", cli.unit_path)):
        _platform(monkeypatch, platform)
        monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
        if artifact:
            _write(artifact())
        r = _Runs()
        cli.backend().stop(r)
        cli.backend().start(r)
        assert r.kwargs, f"{platform} ran nothing to check"
        for kw in r.kwargs:
            assert kw.get("creationflags") == 0x08000000, (platform, kw)


# ------------------------------------------------------- the thin wrappers --

def test_no_unit_test_can_stop_the_real_service(home):
    """BLINK_SKIP_SERVICE, set for every test by tests/conftest.py."""
    r = _Runs()
    for out in (service_ctl.stop_service(runner=r),
                service_ctl.start_service(runner=r)):
        assert out.skipped is True and out.ok is False
        assert "BLINK_SKIP_SERVICE" in str(out)
    assert r.calls == []


def test_a_skipped_stop_cannot_be_read_as_a_done_one(home, monkeypatch):
    """Both answers are a line of prose, so the difference has to be a field.

    tests/ci/check_install.sh documents exporting BLINK_SKIP_SERVICE=1, so a
    fleet agent started from such a shell is a real path: it would stop
    nothing, be refused the port, and blame the board -- and since the start
    no-ops too, it leaves a perfectly healthy desk and nothing to diagnose.
    """
    skipped = service_ctl.stop_service(runner=_Runs())
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _pretend_installed(monkeypatch)
    done = service_ctl.stop_service(runner=_Runs())
    assert skipped.skipped and not done.skipped
    assert str(skipped) != str(done)


def test_the_outcome_still_reads_as_the_line_it_replaced(home, monkeypatch):
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _pretend_installed(monkeypatch)
    out = service_ctl.stop_service(runner=_Runs())
    assert str(out) == out.detail and isinstance(out.detail, str)


def test_ok_tracks_what_the_backend_actually_did(home, monkeypatch, killer):
    """Pins service_ctl._WORKED to the phrases the backends really return."""
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _platform(monkeypatch, "darwin")
    assert service_ctl.stop_service(runner=_Runs()).ok          # not installed
    _write(cli.plist_path())
    assert service_ctl.stop_service(runner=_Runs()).ok          # stopped
    assert service_ctl.start_service(runner=_Runs()).ok         # started
    assert not service_ctl.stop_service(runner=_Runs(codes=[1])).ok
    _platform(monkeypatch, "freebsd14")
    assert not service_ctl.stop_service(runner=_Runs()).ok      # no supervisor
    _platform(monkeypatch, "win32")
    killer.count = 1
    out = service_ctl.stop_service(runner=_Runs(codes=[1]))     # no task, but
    assert out.ok and "killed a detached daemon" in str(out)    # the port is free


def test_a_runner_less_call_uses_the_runner_the_suite_stubbed(home, monkeypatch):
    """WHEN the default runner is resolved, which is a safety property.

    It has to be looked up on the subprocess module at call time. Captured
    once -- as a `runner=subprocess.run` default argument, or any module
    constant standing in for one -- it is the real function, and neither this
    file's tripwire nor the stub the rest of the suite installs can take it
    back. That leaves one delenv("BLINK_SKIP_SERVICE") between a test and the
    logged-in user's agent, which is the incident tests/conftest.py describes.

    Note what this does NOT depend on: spelling it cli.subprocess.run rather
    than importing subprocess here is only a statement of intent, since both
    read the same attribute off the one module object at call time. The
    binding time is the part that bites, so that is the part pinned here.
    """
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    stub = _Runs()
    monkeypatch.setattr(cli.subprocess, "run", stub)
    b = _RecordingBackend()
    monkeypatch.setattr(cli, "backend", lambda: b)
    service_ctl.stop_service()
    assert b.handed is stub
    service_ctl.start_service()
    assert b.handed is stub


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
    assert out.ok is False and "not" in str(out).lower()


def test_a_missing_tool_is_reported_not_raised(home, monkeypatch):
    """The fleet agent calls start_service from a finally block: an exception
    raised there would replace the real test failure with this one."""
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    _pretend_installed(monkeypatch)

    def _absent(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    for out in (service_ctl.stop_service(runner=_absent),
                service_ctl.start_service(runner=_absent)):
        assert out.ok is False and isinstance(out.detail, str)
