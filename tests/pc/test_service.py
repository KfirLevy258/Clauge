"""The login-service backends: what each platform would actually run.

None of this had a test before. The five sys.platform ladders these replaced
were reachable only by installing for real on the platform in question, so
every defect in them -- and there were three on Windows alone -- was found by
a CI runner rather than here. A backend object can be built on any machine and
asked what argv it would hand to launchctl, schtasks or systemctl, which is
most of what there is to get wrong.

This does not replace tests/ci/check_install.sh. That one proves the commands
work; this one proves they are the commands we meant.
"""
import os
import subprocess

import pytest

from pc import cli


class _Runs:
    """Stands in for subprocess.run, recording argv and replaying exit codes."""

    def __init__(self, codes=None, stderr="", running=True, dump=None):
        self.calls = []
        self._codes = list(codes or [])
        self._stderr = stderr
        # Whether a process actually exists, as each platform's own health
        # question would answer it. Every backend now believes that answer
        # rather than the exit code of the command that started the service,
        # so this is the switch between a service that started and one that
        # was only accepted.
        #
        # NOTE THE EXIT CODES BELOW: every health question answers ZERO in
        # both directions, which is true of all three of them on the real
        # platforms. That is deliberate here. A backend that went back to
        # reading a return code would see success while this stub is saying
        # "nothing is running", and the tests that pin it would go red --
        # which is the only reason they are worth having.
        self._running = running
        self._dump = dump

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = self._codes.pop(0) if self._codes else 0
        out = ""
        if len(argv) > 1 and argv[1] == "print":
            # launchctl print: a human-readable dump, exit 0 once the job is
            # registered whatever state it is in.
            out = self._dump if self._dump is not None else (
                "state = running\n" if self._running
                else "active count = 0\n\tstate = not running\n")
        elif "is-active" in argv:
            # systemctl is-active prints the state word; --quiet would not.
            out = "active\n" if self._running else "inactive\n"
        elif argv[0] == "tasklist":
            # tasklist exits 0 whether or not the filters matched. A match is
            # a CSV row naming the pid; a miss is a sentence with no digits
            # in it -- and on a localised Windows, not an English one.
            pid = argv[argv.index("/fi") + 1].split()[-1]
            out = (f'"blink.exe","{pid}","Console","1","41,904 K"\n'
                   if self._running else
                   "INFO: No tasks are running which match the specified criteria.\n")
        return subprocess.CompletedProcess(argv, code, stdout=out,
                                           stderr=self._stderr)

    def ran(self, *words):
        """True if some call contains all of `words` in order."""
        for call in self.calls:
            joined = " ".join(call)
            at = -1
            for w in words:
                at = joined.find(w, at + 1)
                if at < 0:
                    break
            else:
                return True
        return False


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME is redirected by tests/conftest.py; this adds the stubs."""
    # Never let a stray real call out of a unit test.
    monkeypatch.setattr(cli.subprocess, "run", _Runs())
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    # Deterministic, and present on every platform this suite runs on.
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    return tmp_path


def _runs(monkeypatch, codes=None, stderr="", running=True, dump=None):
    r = _Runs(codes, stderr, running, dump)
    monkeypatch.setattr(cli.subprocess, "run", r)
    return r


def _recorded_pid(pid=4242):
    """The pid the daemon writes for itself at startup, ~/.blink/bridge.pid.

    Windows health is read from this file (see _SchtasksBackend._is_running),
    so a test that wants a live daemon has to leave one the way a live daemon
    would -- and a test that wants a dead one simply does not.
    """
    os.makedirs(cli.blink_home(), exist_ok=True)
    with open(cli.pid_path(), "w", encoding="utf-8") as f:
        f.write(str(pid))
    return pid


def _platform(monkeypatch, name):
    monkeypatch.setattr(cli.sys, "platform", name)


# ------------------------------------------------------------------ choice --

@pytest.mark.parametrize("platform,cls", [
    ("darwin", cli._LaunchdBackend),
    ("win32", cli._SchtasksBackend),
    ("linux", cli._SystemdBackend),
    ("linux2", cli._SystemdBackend),
])
def test_backend_per_platform(monkeypatch, platform, cls):
    _platform(monkeypatch, platform)
    assert isinstance(cli.backend(), cls)


def test_unknown_platform_gets_the_base_backend(monkeypatch):
    """Not an error. It is a machine we cannot supervise, and it is told so."""
    _platform(monkeypatch, "freebsd14")
    b = cli.backend()
    assert type(b) is cli._Backend
    assert b.creates() is None
    assert "freebsd14" in b.install()
    assert "run yourself" in b.install() or "run it yourself" in b.install()
    assert b.remove() == "nothing to remove"
    assert "freebsd14" in b.status()


# ----------------------------------------------------------------- launchd --

def test_launchd_install_writes_the_plist_and_bootstraps(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch)

    assert cli.backend().install() == "running (launchd)"

    plist = open(cli.plist_path()).read()
    assert "<key>Label</key><string>com.blink.bridge</string>" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    assert cli.log_path() in plist
    # bootout must come first, or bootstrap fails on "already loaded".
    assert r.calls[0][:2] == ["launchctl", "bootout"]
    assert r.calls[1][:2] == ["launchctl", "bootstrap"]
    assert r.calls[1][2] == "gui/501"
    # And STARTED, not merely registered. bootstrap accepts the job; it does
    # not run it, and on a bootout/bootstrap cycle it routinely does not.
    assert r.ran("launchctl", "kickstart", "gui/501/com.blink.bridge")


def test_launchd_install_retries_bootstrap(home, monkeypatch):
    """bootout is asynchronous, so the first bootstrap after it can lose.

    Without the retry a reinstall left no service running at all, silently.
    """
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch, codes=[0, 1, 1, 0])

    assert cli.backend().install() == "running (launchd)"
    bootstraps = [c for c in r.calls if c[1] == "bootstrap"]
    assert len(bootstraps) == 3


def test_launchd_install_gives_up_with_the_command_to_run(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, codes=[0, 1, 1, 1, 1])

    msg = cli.backend().install()
    assert msg.startswith("installed, but could not be started:")
    # The message has to be a command someone can paste.
    assert "launchctl bootstrap gui/501" in msg
    assert cli.plist_path() in msg


def test_launchd_install_will_not_claim_running_when_it_is_not(home, monkeypatch):
    """The defect this exists for, measured on the owner's machine 2026-09-04.

    Every launchctl command returned zero, the installer printed
    "running (launchd)" as its last word, and the service was not running:

        active count = 0
        state = not running
        last exit code = (never exited)

    The board went quiet and dozed while the install said it was fine. So the
    claim must come from launchd's own answer, not from a bootstrap that
    merely succeeded -- registration is not health, which the comment above
    _service_command() already had to learn once.
    """
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, running=False)

    msg = cli.backend().install()
    assert "running (launchd)" != msg
    assert "not running" in msg
    # And it has to be a command someone can paste, like its sibling above.
    assert "launchctl kickstart -k gui/501/com.blink.bridge" in msg


def test_launchd_remove_boots_out_and_deletes_the_plist(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    os.makedirs(os.path.dirname(cli.plist_path()), exist_ok=True)
    open(cli.plist_path(), "w").write("x")
    r = _runs(monkeypatch)

    assert cli.backend().remove() == "removed"
    assert r.ran("launchctl", "bootout", "gui/501/com.blink.bridge")
    assert not os.path.exists(cli.plist_path())


def test_launchd_status(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, codes=[0])
    assert cli.backend().status() == "registered with launchd"
    _runs(monkeypatch, codes=[1])
    assert cli.backend().status() == "not installed"


def test_launchd_status_names_a_running_pid(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, dump="\tpid = 8123\n\tstate = running\n")
    assert cli.backend().status() == "running (launchd, pid 8123)"


def test_launchd_status_calls_a_crash_loop_a_crash_loop(home, monkeypatch):
    """The row exists because a job that had crash-looped 59 times read as
    healthy on the author's own machine, while every other line of `blink
    status` looked fine. launchctl knew all along -- it reports the run count
    and the last exit code -- so the only thing that was missing was asking.

    No pid, a nonzero last exit code: registered, and failing. Anything that
    reads the exit code the other way round, or ignores it, says "registered
    with launchd" here and the dead daemon goes unnoticed for another month.
    """
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, dump=("\tstate = not running\n"
                             "\tlast exit code = 1\n"
                             "\truns = 59\n"))

    msg = cli.backend().status()
    assert msg != "registered with launchd"
    assert "FAILING" in msg
    assert "code 1" in msg and "59 attempts" in msg
    assert cli.log_path() in msg          # where the reason is written


def test_launchd_status_does_not_cry_fault_over_a_clean_exit(home, monkeypatch):
    """A job that exited zero and is between runs is not a crash loop.

    The dump is parsed defensively for exactly this reason: launchctl print
    is a human-readable dump and not a contract, so an unrecognised or
    innocent one has to fall back rather than invent a fault.
    """
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, dump="\tlast exit code = 0\n\truns = 3\n")
    assert cli.backend().status() == "registered with launchd"


def test_launchd_restart_asks_launchd_rather_than_trusting_kickstart(home, monkeypatch):
    """A kickstart that exits zero is a request accepted, not a live process.

    The same defect install() was measured making on 2026-09-04, in the same
    file: every launchctl command returned zero and nothing was running. So
    "restarted" has to come from launchd's own answer.
    """
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch)

    assert cli.backend().restart() == "restarted"
    assert r.calls[0][:2] == ["launchctl", "kickstart"]
    assert r.ran("launchctl", "print", "gui/501/com.blink.bridge")


def test_launchd_restart_will_not_claim_restarted_when_it_is_not(home, monkeypatch):
    """kickstart exits 0, launchd says not running: say so, and where to look.

    This is the `blink update` path -- the binary was just replaced. If the
    new one cannot start, launchd respawns it and it dies again, and the log
    is the only thing that explains why.
    """
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, running=False)

    msg = cli.backend().restart()
    assert msg != "restarted"
    assert "not running" in msg
    assert cli.log_path() in msg


def test_launchd_restart_reports_a_kickstart_that_failed(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch, codes=[1])

    assert cli.backend().restart() == "could not restart it"
    # And it did not go on to ask launchd: there is nothing to confirm when
    # the request itself was refused.
    assert not r.ran("launchctl", "print")


# ---------------------------------------------------------------- schtasks --

def test_schtasks_install_registers_at_logon_then_starts_it(home, monkeypatch):
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    pid = _recorded_pid()

    assert cli.backend().install() == "running (Scheduled Task)"
    create = r.calls[0]
    assert create[:3] == ["schtasks", "/create", "/f"]
    # /sc onlogon needs no admin rights; a Windows service would.
    assert "onlogon" in create
    assert cli.TASK_NAME in create
    # The task runs a hidden-window launcher, not the program: a console
    # program started by a task gets a console window on the desktop.
    # The path is quoted inside /tr: Windows splits an unquoted one on the
    # space in "Program Files" and on any space in the user's name.
    assert f'wscript.exe //B //Nologo "{cli.launcher_path()}"' in create
    vbs = open(cli.launcher_path(), encoding="ascii").read()
    assert vbs.isascii()                       # the code page never matters
    assert "%USERPROFILE%" in vbs              # the profile, not a literal path
    assert "run --log" in vbs                  # hidden means it logs itself
    assert ", 0, False" in vbs                 # hidden window, do not wait
    # onlogon does not start it now, so it is started explicitly -- after
    # the previous install's daemon is ended, or it keeps the port.
    assert r.ran("schtasks", "/end")
    assert r.ran("schtasks", "/run")
    # ...and then it ASKED Windows, by the pid the daemon records for itself,
    # with the image name as a filter so a recycled pid cannot answer for it.
    ask = [c for c in r.calls if c[0] == "tasklist"]
    assert ask and f"PID eq {pid}" in ask[0]
    assert "IMAGENAME eq blink.exe" in ask[0]
    # NOT the task's own state. Its action is wscript, which starts the
    # bridge without waiting and exits, so a healthy install reads "Ready"
    # within a second of /run -- and its status word is translated on a
    # localised Windows, which the owner's PC is.
    assert not r.ran("schtasks", "/query")


def test_schtasks_install_will_not_claim_running_when_it_is_not(home, monkeypatch):
    """The 2026-09-04 defect, on Windows, where it is still unfixed.

    Every schtasks command exits zero -- /create, /end and /run all did here
    -- and nothing is running. `/run` triggers a task; a triggered task is
    not a live daemon, exactly as a successful `launchctl bootstrap` was not.

    Nothing writes ~/.blink/bridge.pid in this test, which is what a daemon
    that never started looks like from the outside.
    """
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch, running=False)

    msg = cli.backend().install()
    assert msg != "running (Scheduled Task)"
    assert "not running" in msg
    # And a command someone can paste, like its launchd sibling.
    assert f'schtasks /run /tn "{cli.TASK_NAME}"' in msg
    assert r.ran("schtasks", "/run")


def test_schtasks_install_does_not_believe_a_dead_recorded_pid(home, monkeypatch):
    """A pid file outlives the daemon that wrote it.

    Install kills the previous daemon and never deletes its pid file, so the
    file is there at exactly the moment install is deciding what to claim. If
    the file alone counted as health, every failed install would report the
    corpse of the one before it as a success.
    """
    _platform(monkeypatch, "win32")
    _recorded_pid(4242)
    _runs(monkeypatch, running=False)      # tasklist finds no such process

    assert "not running" in cli.backend().install()


def _autostart(monkeypatch, set_err="", present=False):
    """Stand in for the user's Run key, which only exists on Windows.

    Returns the list every _autostart_set call appends to, so a test can see
    whether the fallback was actually taken rather than only what was said
    about it.
    """
    written = []
    state = {"present": present}

    def _set():
        if set_err:
            return set_err
        written.append(cli._autostart_command())
        state["present"] = True
        return ""

    monkeypatch.setattr(cli, "_autostart_set", _set)
    monkeypatch.setattr(cli, "_autostart_clear",
                        lambda: state.update(present=False))
    monkeypatch.setattr(cli, "_autostart_present", lambda: state["present"])
    monkeypatch.setattr(cli, "_start_launcher", lambda: written.append("start"))
    return written


def test_schtasks_install_falls_back_when_windows_refuses_the_task(
        home, monkeypatch):
    """A logon trigger needs administrator; the user's Run key does not.

    Measured on the release desk, 2026-09-06: from a non-elevated process
    `schtasks /create /sc onlogon` answers "Access is denied" while
    `/sc once` succeeds, on stock ACLs with no policy set. This used to give
    up there, and the result was a customer with a working board, a correctly
    installed driver, no daemon, and a panel stuck on its own setup screen.
    """
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[1], stderr="ERROR: Access is denied.\n")
    written = _autostart(monkeypatch)
    _recorded_pid()

    msg = cli.backend().install()
    assert msg == "running (starts when you log in)"
    # It really registered, and really started something -- not just said so.
    assert cli._autostart_command() in written
    assert "start" in written


def test_schtasks_install_says_so_when_neither_mechanism_works(home,
                                                               monkeypatch):
    """Both refused. That is worth reporting in full, with both reasons."""
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[1], stderr="ERROR: Access is denied.\n")
    _autostart(monkeypatch, set_err="registry is read-only")

    msg = cli.backend().install()
    assert "could not register a Scheduled Task" in msg
    assert "Access is denied." in msg
    assert "registry is read-only" in msg


def test_schtasks_install_will_not_claim_the_fallback_is_running(home,
                                                                 monkeypatch):
    """Same honesty the task path owes: registered is not running."""
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[1], stderr="ERROR: Access is denied.\n",
          running=False)
    _autostart(monkeypatch)

    msg = cli.backend().install()
    assert "not running" in msg


def test_schtasks_status_reports_the_fallback_rather_than_not_installed(
        home, monkeypatch):
    """The line that made a working install look like a missing one."""
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[1])          # no such task
    _autostart(monkeypatch, present=True)

    assert cli.backend().status() == "registered to start when you log in"


def test_schtasks_status_still_says_not_installed_when_neither_is_there(
        home, monkeypatch):
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[1])
    _autostart(monkeypatch, present=False)

    assert cli.backend().status() == "not installed"


def test_schtasks_remove_clears_the_run_key_too(home, monkeypatch):
    """Uninstall does not know which mechanism was used, so it clears both.

    A leftover Run entry points at a program uninstall is about to delete,
    and would try to start it at every logon for the life of the machine.
    """
    _platform(monkeypatch, "win32")
    _runs(monkeypatch)
    _autostart(monkeypatch, present=True)
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: None)
    monkeypatch.setattr(cli, "_kill_by_path", lambda: None)

    cli.backend().remove()
    assert cli._autostart_present() is False


def test_schtasks_restart_starts_the_launcher_when_there_is_no_task(
        home, monkeypatch):
    """`blink update` on a Run-key install must not leave nothing running."""
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[0, 1])       # /end ok, /run fails: no such task
    written = _autostart(monkeypatch, present=True)
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: None)
    _recorded_pid()

    assert cli.backend().restart() == "restarted"
    assert "start" in written


def test_schtasks_restart_still_gives_up_when_there_is_no_fallback_either(
        home, monkeypatch):
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[0, 1])
    _autostart(monkeypatch, present=False)
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: None)

    assert cli.backend().restart() == "could not restart it"


def test_the_autostart_runs_the_same_launcher_as_the_task(home):
    """Both mechanisms must start the bridge identically -- hidden, logging
    to its own file. A fallback that started it differently would be a second
    code path to keep working."""
    assert cli._autostart_command() == (
        f'wscript.exe //B //Nologo "{cli.launcher_path()}"')


def test_the_registry_helpers_are_safe_where_there_is_no_registry(home,
                                                                  monkeypatch):
    """They are called from status() on every platform the tests exercise."""
    monkeypatch.setattr(cli, "_winreg", lambda: None)
    assert cli._autostart_present() is False
    assert cli._autostart_set() == "no registry on this system"
    cli._autostart_clear()                 # must not raise


def test_schtasks_remove_ends_deletes_and_kills(home, monkeypatch):
    """/delete alone leaves the daemon running -- and holding the serial port."""
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: killed.append("pid"))
    monkeypatch.setattr(cli, "_kill_by_path", lambda: killed.append("path"))

    assert cli.backend().remove() == "removed"
    assert r.ran("schtasks", "/end")
    assert r.ran("schtasks", "/delete", "/f")
    assert killed == ["pid", "path"]


def test_schtasks_restart_kills_the_detached_successor(home, monkeypatch):
    """A daemon that replaced itself is not the task's child any more.

    /run would otherwise add a second daemon competing for the same port.
    """
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: killed.append("pid"))

    _recorded_pid()

    assert cli.backend().restart() == "restarted"
    assert killed == ["pid"]
    assert r.ran("schtasks", "/run")
    assert r.ran("tasklist")               # and then asked, rather than assumed


def test_schtasks_restart_will_not_claim_restarted_when_it_is_not(home, monkeypatch):
    """The `blink update` path: the .exe under the task was just replaced.

    `schtasks /run` exits zero over a binary that cannot start -- a bad
    download, a missing DLL, a scanner holding the file -- and unlike an
    onlogon task on a Mac's KeepAlive, nothing will try again. Saying
    "restarted" would send the user away from the one file that explains it.
    """
    _platform(monkeypatch, "win32")
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: None)
    _runs(monkeypatch, running=False)

    msg = cli.backend().restart()
    assert msg != "restarted"
    assert "not running" in msg
    assert cli.log_path() in msg


def test_schtasks_restart_reports_a_run_that_failed(home, monkeypatch):
    _platform(monkeypatch, "win32")
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: None)
    r = _runs(monkeypatch, codes=[0, 1])   # /end fine, /run refused

    assert cli.backend().restart() == "could not restart it"
    # Nothing to confirm when the request itself was refused.
    assert not r.ran("tasklist")


def test_schtasks_status_tells_registered_from_not_installed(home, monkeypatch):
    """`schtasks /query /tn` exits 0 when the task exists and nonzero when it
    does not, and the two answers had never been told apart by a test -- so
    swapping them changed nothing that anybody checked. `blink status` prints
    this line straight to the user.

    It reports REGISTRATION, deliberately and not health: the task is Ready,
    not Running, whenever the bridge is alive (see _is_running), so there is
    no health to read here. Health belongs on the Bridge line.
    """
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch, codes=[0])
    assert cli.backend().status() == "registered as a Scheduled Task"
    assert r.calls[0][:2] == ["schtasks", "/query"]
    assert cli.TASK_NAME in r.calls[0]

    _runs(monkeypatch, codes=[1])
    assert cli.backend().status() == "not installed"


# ----------------------------------------------------------------- systemd --

def test_systemd_install_writes_the_unit_and_enables_it(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _runs(monkeypatch)

    assert cli.backend().install() == "running (systemd)"
    unit = open(cli.unit_path()).read()
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    assert r.ran("systemctl", "--user", "daemon-reload")
    assert r.ran("systemctl", "--user", "enable", "--now")
    # ...and then asked systemd, which is the only thing that knows. Without
    # --quiet, so there is a word to read rather than a code to trust.
    assert r.ran("systemctl", "--user", "is-active", "blink-bridge.service")
    assert not r.ran("is-active", "--quiet")


def test_systemd_install_will_not_claim_running_when_it_is_not(home, monkeypatch):
    """The 2026-09-04 defect, on Linux, where it is still unfixed.

    `enable --now` exits zero here and systemd says "inactive". That pairing
    is not contrived: it is what a unit whose ExecStart cannot be resolved
    does, and this file has seen it -- _systemd_exec() records an unquoted
    ExecStart failing with "Failed to locate executable" while `blink
    install` still printed "running (systemd)". Nothing caught it then
    because the only evidence anybody read was an exit code.
    """
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _runs(monkeypatch, running=False)

    msg = cli.backend().install()
    assert msg != "running (systemd)"
    assert "not running" in msg
    # Somewhere to look, and it is a command that can be pasted.
    assert "systemctl --user status blink-bridge.service" in msg
    assert r.ran("systemctl", "--user", "is-active")


def test_systemd_install_reports_an_enable_that_failed(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _runs(monkeypatch, codes=[0, 1])   # daemon-reload fine, enable refused

    assert cli.backend().install().startswith("installed, but could not be started:")
    # Nothing to confirm when the request itself was refused.
    assert not r.ran("systemctl", "--user", "is-active")


def test_systemd_restart_asks_systemd_rather_than_trusting_the_exit_code(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _runs(monkeypatch)

    assert cli.backend().restart() == "restarted"
    assert r.calls[0][:4] == ["systemctl", "--user", "restart",
                              "blink-bridge.service"]
    assert r.ran("systemctl", "--user", "is-active")


def test_systemd_restart_will_not_claim_restarted_when_it_is_not(home, monkeypatch):
    """`blink update` swapped the binary underneath the unit. `restart` exits
    zero over a binary that cannot start; Restart=always then respawns it and
    it dies again, forever, and the journal is the only thing that says why.
    """
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    _runs(monkeypatch, running=False)

    msg = cli.backend().restart()
    assert msg != "restarted"
    assert "not running" in msg
    assert "systemctl --user status blink-bridge.service" in msg


def test_systemd_restart_reports_a_restart_that_failed(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _runs(monkeypatch, codes=[1])

    assert cli.backend().restart() == "could not restart it"
    assert not r.ran("systemctl", "--user", "is-active")


def test_systemd_activating_is_not_yet_running(home, monkeypatch):
    """A unit that has been asked to start reads "activating" until its exec
    is up. That is neither health nor failure, so it is retried -- and only
    the word "active" is allowed to end the wait.
    """
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")

    class _Activating(_Runs):
        def __call__(self, argv, **kw):
            r = super().__call__(argv, **kw)
            if "is-active" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="activating\n")
            return r

    r = _Activating()
    monkeypatch.setattr(cli.subprocess, "run", r)

    msg = cli.backend().install()
    assert "not running" in msg
    # Asked more than once before giving up: a slow start is not a failure.
    assert len([c for c in r.calls if "is-active" in c]) > 1


def test_systemd_remove_disables_and_reloads(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    os.makedirs(os.path.dirname(cli.unit_path()), exist_ok=True)
    open(cli.unit_path(), "w").write("x")
    r = _runs(monkeypatch)

    assert cli.backend().remove() == "removed"
    assert r.ran("systemctl", "--user", "disable", "--now")
    assert not os.path.exists(cli.unit_path())
    # daemon-reload after the unit file is gone, not before.
    assert r.calls[-1][:3] == ["systemctl", "--user", "daemon-reload"]


def test_systemd_status(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    _runs(monkeypatch, codes=[0])
    assert cli.backend().status() == "running"
    _runs(monkeypatch, codes=[1])
    assert cli.backend().status() == "not running"


class TestLinuxWithoutSystemd:
    """A machine with no systemd is not a broken machine.

    Every method has to have a real answer for it, and they have to agree
    with each other. They did not: remove() knew exactly what was going on
    and status() said "unknown on linux".
    """

    @pytest.fixture(autouse=True)
    def _no_systemctl(self, monkeypatch):
        _platform(monkeypatch, "linux")
        monkeypatch.setattr(cli.shutil, "which", lambda _n: None)

    def test_install_still_writes_the_unit(self, home, monkeypatch):
        """So a machine that gains systemd later already has the right file."""
        r = _runs(monkeypatch)
        msg = cli.backend().install()
        assert "no systemd here" in msg
        assert cli.installed_bin() in msg          # what to run by hand
        assert os.path.exists(cli.unit_path())
        assert r.calls == []                       # nothing was invoked

    def test_remove_does_not_claim_to_have_stopped_anything(self, home, monkeypatch):
        os.makedirs(os.path.dirname(cli.unit_path()), exist_ok=True)
        open(cli.unit_path(), "w").write("x")
        _runs(monkeypatch)
        msg = cli.backend().remove()
        assert msg == "no systemd here; stop it yourself if you started it"
        assert not os.path.exists(cli.unit_path())

    def test_status_says_what_remove_says(self, home, monkeypatch):
        _runs(monkeypatch)
        assert "no systemd here" in cli.backend().status()

    def test_restart_falls_back(self, home, monkeypatch):
        _runs(monkeypatch)
        assert cli.backend().restart() == (
            "not running under a supervisor; restart it yourself")


# ---------------------------------------------------------------- disclosure --

@pytest.mark.parametrize("platform,needle", [
    ("darwin", "LaunchAgents"),
    ("linux", "systemd"),
    ("win32", "Scheduled Task"),
])
def test_creates_names_the_thing_install_will_make(home, monkeypatch,
                                                   platform, needle):
    """`blink install` prints this before it does anything."""
    _platform(monkeypatch, platform)
    assert needle in cli.backend().creates()


# ------------------------------------------------------------------- skips --

@pytest.mark.parametrize("fn", ["_install_service", "restart_service",
                                "_remove_service"])
def test_skip_service_short_circuits_every_entry_point(home, monkeypatch, fn):
    """The tests' own guard. If one of these forgot it, a unit test under a
    temporary HOME would boot out the agent of whoever is logged in."""
    _platform(monkeypatch, "darwin")
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")
    r = _runs(monkeypatch)

    assert getattr(cli, fn)() == "skipped (BLINK_SKIP_SERVICE=1)"
    assert r.calls == []


def test_kill_recorded_daemon_reads_the_legacy_pid_beside_the_old_program(home, monkeypatch):
    """Before 1.1.0 the daemon kept its pid beside its executable, and the
    1.1.0 install rotates that directory to bin.old. The first upgrade on a
    real PC left the 1.0.4 daemon alive on the serial port because only the
    new location, ~/.blink/bridge.pid, was read (2026-08-29)."""
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    os.makedirs(cli.bin_dir() + ".old")
    with open(os.path.join(cli.bin_dir() + ".old", "bridge.pid"), "w") as f:
        f.write("4242")
    cli._kill_recorded_daemon()
    assert r.ran("taskkill", "/f", "/t", "/pid", "4242")


# ------------------------------------------------------------------- halt --

def test_schtasks_halt_stops_the_daemon_but_keeps_the_registration(
        home, monkeypatch):
    """Windows will not rename <bin> while the daemon holds blink.exe open.

    halt() exists to let go of it just before the update swap. What it must
    NOT do is unregister: a failed update that also deleted the task would
    leave a machine that never starts Blink again.
    """
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon",
                        lambda **kw: killed.append("pid"))

    cli.backend().halt()
    assert r.ran("schtasks", "/end")
    assert killed == ["pid"]
    assert not r.ran("schtasks", "/delete")
    assert not r.ran("reg", "delete")


@pytest.mark.parametrize("platform", ["darwin", "linux", "freebsd14"])
def test_halt_does_nothing_off_windows(home, monkeypatch, platform):
    """A rename elsewhere does not care what is open, and launchd or systemd
    would put the daemon straight back -- so stopping it buys a gap on the
    board and nothing else."""
    _platform(monkeypatch, platform)
    r = _runs(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: killed.append("pid"))

    assert cli.backend().halt() is None
    assert r.calls == []
    assert killed == []


def test_halt_service_never_raises(home, monkeypatch):
    """It runs in the middle of an update. Nothing it can fail at is worth
    turning a working update into a traceback."""
    _platform(monkeypatch, "win32")

    monkeypatch.delenv("BLINK_SKIP_SERVICE", raising=False)
    reached = []

    class Boom:
        def halt(self):
            reached.append(True)
            raise RuntimeError("schtasks is not on this machine")

    monkeypatch.setattr(cli, "backend", lambda: Boom())
    assert cli.halt_service() is None
    assert reached, "the swallow must be around a halt that actually ran"
