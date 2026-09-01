"""One binary: the setup, the status check, and the bridge itself.

A customer downloads a single file and runs it. There is no Python to install,
no virtualenv to build, no repository to clone, and nothing left behind that
they have to keep in place -- `blink install` copies the binary into
~/.blink/bin and points the login service at that copy, so the download is
disposable the moment it finishes.

This replaces install.sh. The shell version needed Python 3.9+ on the machine,
built a virtualenv, and pulled two packages from PyPI at install time -- three
things that could fail on a customer's machine for reasons they could do
nothing about, and one of them (an unpinned PyPI resolve) meant two people
installing a week apart could get different software.

The status line shim stays a shell script on purpose. It runs on EVERY status
line render, many times a minute, and a frozen binary takes 100-400 ms to start
because it unpacks itself first -- that delay would land in the customer's
prompt. A few lines of sh start in single-digit milliseconds.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

from pc import (install_hooks, install_statusline, protocol, statusline_source,
                update)
from pc.version import RELEASE_VERSION

# Resolved per call, not at import. These used to be module constants, which
# meant every path was fixed by whatever HOME happened to be when the module
# loaded -- untestable without a subprocess, and quietly wrong for any caller
# that changes HOME.
LABEL = "com.blink.bridge"


def _home():
    return os.path.expanduser("~")


def blink_home():
    return os.path.join(_home(), ".blink")


def bin_dir():
    return os.path.join(blink_home(), "bin")


def installed_bin():
    # .exe on Windows: without the extension the copy is not executable, and
    # the Scheduled Task would register a path Windows refuses to launch.
    name = "blink.exe" if sys.platform == "win32" else "blink"
    return os.path.join(bin_dir(), name)


def shim_path():
    return os.path.join(blink_home(), "blink-statusline.sh")


def hook_shim_path():
    return os.path.join(blink_home(), "blink-hook.sh")


def log_path():
    return os.path.join(blink_home(), "bridge.log")


def launcher_path():
    """The script the Scheduled Task runs, so the bridge starts with no window.

    A console program started by a Scheduled Task gets a console window, and
    on the first Windows machine it was installed on that window sat on the
    desktop for as long as the service ran (2026-08-29: "it should be some
    service in the background, not a UI app"). wscript.exe can start a
    program with its window hidden; this is the one-line script that asks
    it to. Written in ASCII with the profile taken from the environment,
    because a non-ASCII path inside a .vbs depends on the machine's code
    page, and the machine in question had a Hebrew user name.
    """
    return os.path.join(blink_home(), "blink-bridge.vbs")


LAUNCHER_VBS = "\n".join((
    "' Starts the BLINK bridge with no window. Written by `blink install`;",
    "' the Scheduled Task \"Blink bridge\" runs it at every logon.",
    'Set sh = CreateObject("WScript.Shell")',
    'home = sh.ExpandEnvironmentStrings("%USERPROFILE%")',
    # VBScript doubles a quote to embed one: this runs
    #   "<home>\.blink\bin\blink.exe" run --log "<home>\.blink\bridge.log"
    # with window style 0 (hidden) and without waiting for it to finish.
    'sh.Run """" & home & "\\.blink\\bin\\blink.exe"" run --log """'
    ' & home & "\\.blink\\bridge.log""", 0, False',
    "",
))


def pid_path():
    """Where the running daemon records its pid: ~/.blink/bridge.pid.

    Derived from the program's own location (its directory's parent), not
    from ~. A login service runs in the user's environment rather than the
    one that registered it, so the two can disagree about what ~ is -- and
    when they do, the pid lands where nothing will look for it.

    Beside the program, not inside its directory: an update rotates
    ~/.blink/bin to bin.old with the old daemon still running from it, and a
    pid file that moved with the directory is one restart() can no longer
    find -- leaving the old daemon holding the serial port against the new.
    """
    return os.path.join(blink_home(), "bridge.pid")


def settings_path():
    return os.path.join(_home(), ".claude", "settings.json")


def plist_path():
    return os.path.join(_home(), "Library", "LaunchAgents", LABEL + ".plist")


def unit_path():
    return os.path.join(_home(), ".config", "systemd", "user",
                        "blink-bridge.service")


# Windows has no launchd and no systemd. A Scheduled Task with an at-logon
# trigger is the equivalent that needs no admin rights and no service wrapper.
TASK_NAME = "Blink bridge"

# The oldest Claude Code that carries usage figures in its status line payload.
# 2.1.0 does not carry rate_limits at all; 2.1.100 does. Below this every step
# of the install succeeds and the panel stays blank forever.
MIN_CLAUDE = (2, 1, 100)

# Set by the tests to skip registering a real login service. The launchd label
# and the systemd unit name are constants while everything else is scoped to
# $HOME, so without this a test under a temporary HOME still boots out the
# real agent of whoever is logged in.
def _skip_service():
    return os.environ.get("BLINK_SKIP_SERVICE") == "1"


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def _self_path() -> str:
    """The binary to copy into place. sys.executable is the frozen binary."""
    return sys.executable if _frozen() else os.path.abspath(sys.argv[0])


def _shim_source(name: str = "blink-statusline.sh") -> str:
    """A shim's text, from the bundle when frozen, the tree when not.

    One source of truth either way -- tools/ is what the build embeds, so the
    shipped shim and the one in the repository cannot drift apart. Any new
    shim added here must also be added to the --add-data list in
    tools/build_binary.sh, or it will work from a checkout and be missing
    from every shipped binary.
    """
    if _frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(_self_path()))
        return open(os.path.join(base, name), encoding="utf-8").read()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(here, "tools", name), encoding="utf-8").read()


def _write_shim(path: str, name: str) -> None:
    # newline="\n", or Windows writes the shim with CRLF and Git Bash then
    # fails on `case ... in\r` on every status line render and every tool
    # call. The CI CRLF check only ever looked at the files in the repository,
    # never at the copy this function installs.
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_shim_source(name))
    os.chmod(path, 0o755)


# ---------------------------------------------------------------- Claude Code


def claude_version():
    """(text, tuple) for the Claude Code on PATH, or (None, None)."""
    exe = shutil.which("claude")
    if not exe:
        return None, None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except Exception:
        return None, None
    head = out.split()[0] if out else ""
    pieces = head.split(".")[:3]
    # Every component has to be a number, or we did not parse a version.
    # Mapping the odd ones to 0 meant "claude version unknown" came back as
    # (0, 0, 0) -- below every floor, so a perfectly good install was reported
    # as TOO OLD, and callers testing `ver is None` never saw it coming.
    if not pieces or not all(x.isdigit() for x in pieces):
        return (out or None), None
    parts = [int(x) for x in pieces]
    while len(parts) < 3:
        parts.append(0)
    return (out or None), tuple(parts)


def desktop_app_present() -> bool:
    """Has Claude Desktop ever written its usage cache on this machine?

    The file, not the application bundle. An installed app that has never run
    has nothing for us to read, and a machine where the app was removed but
    the cache remains still has a (stale, and reported as stale) reading. The
    file is the thing this product actually depends on, so the file is what
    gets asked about.
    """
    from pc.providers import claude_desktop
    try:
        return os.path.exists(claude_desktop.cache_path())
    except Exception:
        return False


def desktop_staleness_note(age_s):
    """Why a Claude Desktop reading is old -- or nothing, when it is not.

    Claude Desktop does not write its usage cache on a clock. It writes after
    a usage fetch, and it only fetches while somebody is actually at the
    machine: step away, lock the screen or let the machine sleep and the file
    simply stops growing. The app is still running and still right, it is
    just not asking. A reading hours old is therefore the ordinary shape of
    "nobody was here", not a fault, and a panel showing one is not stuck.

    Measured from Claude.app 1.40609.0 on 2026-08-31, by reading its own
    bundle and then watching it run: the poll is 15 minutes by default and
    5 only for the half hour after the menubar panel has been opened, it
    skips the fetch entirely once the system has been idle ten minutes or
    the screen is locked, and it fetches at launch and again every time the
    menubar icon is opened. Opening that icon is the one action that
    refreshes on demand: focusing the app window fetches nothing, because
    the chat runs in a web view that never touches this file.

    None of those intervals is quoted below on purpose. They belong to an
    application we do not control, they have already changed twice (a flat
    5 minutes when pc/providers/claude_desktop.py was written, then the
    tray-gated 5/15 split around 2026-08-21 -- arriving without an app
    update, so the cadence is server-controlled and can move again), and a
    number that goes quietly wrong is worse here than no
    number: the reader needs the shape, which does not change, and the one
    action that works.
    """
    from pc.providers import claude_desktop
    if age_s is None or age_s < claude_desktop.STALE_AFTER_S:
        return []
    return [
        "Claude Desktop refreshes this only while you are at the machine,",
        "and stops once it has been idle a while -- so a reading this old",
        "usually means nobody was here, not that anything is broken.",
        "Click the Claude icon in your menu bar to refresh it now.",
    ]


def codex_present() -> bool:
    """Has Codex CLI written a rollout log on this machine?

    The logs, not the binary, for the reason desktop_app_present() gives: the
    file the daemon reads is the thing that matters.
    """
    from pc.providers import codex_cli
    try:
        return bool(codex_cli.recent_rollouts(limit=1))
    except Exception:
        return False


def _note_if_no_claude_code():
    """Say what this machine will and will not show, when Claude Code is absent.

    Steps 2 and 3 of the install write a status line and a set of hooks into
    ~/.claude/settings.json. With no Claude Code on the machine nothing ever
    reads that file, so both steps report success and produce nothing -- and
    the customer is left with a device that half works and no way to find out
    why. That is the worst shape a first run can have, and it cost nothing to
    keep quiet about, which is why it stayed quiet.

    A note, not a refusal, and not a failed step. The edits are correct and
    stay correct: install Claude Code tomorrow and it all starts working with
    nothing to redo. Same reasoning as _warn_if_claude_too_old below.
    """
    text, _ = claude_version()
    if text is not None:
        return

    print()
    if codex_present() and not desktop_app_present():
        # A Codex machine. The daemon has known about Codex for a while; this
        # message did not, and told a Codex-edition customer that nothing on
        # the machine reports usage and to install Claude Code.
        print("  !  Claude Code is not installed, so the panel runs on Codex")
        print("     alone: both Codex limits and their countdowns, from the")
        print("     session log Codex already writes. The Claude page stays")
        print("     empty, and there is no activity light -- that needs")
        print("     Claude Code's hooks. The status line and hooks just")
        print("     installed start working by themselves if you add it later.")
        return
    if not desktop_app_present():
        # No source exists. This is not a reduced panel, it is an empty
        # one, and saying anything softer would be misleading.
        print("  !! Nothing on this machine reports usage yet.")
        print("     Blink reads figures that Claude Code, Claude Desktop or")
        print("     Codex have already worked out. With none of them installed")
        print("     the panel will connect and then sit blank.")
        print()
        print("       npm install -g @anthropic-ai/claude-code@latest")
        print()
        print("     Nothing here needs redoing afterwards -- it starts on its own.")
        return

    print("  !  Claude Code is not installed, so the panel runs on")
    print("     Claude Desktop alone. That works, with less on it:")
    print()
    print("       Shown    both usage percentages, and how fast the")
    print("                five-hour window is filling")
    print("       Missing  the reset countdowns, and the activity light")
    print()
    # Why, not just what. The countdowns are the part people ask about, and
    # "it is not implemented yet" would be the wrong answer -- there is
    # nothing to implement. Verified across every file, LevelDB store and
    # cache the desktop app writes, 2026-08-28.
    print("     Claude Desktop does not record when either window resets --")
    print("     anywhere, in any file -- so there is no countdown to show and")
    print("     the panel shows a rate instead. The activity light needs")
    print("     Claude Code's hooks.")
    print()
    print("     The status line and hooks just installed are correct and will")
    print("     start working by themselves if you add Claude Code later:")
    print()
    print("       npm install -g @anthropic-ai/claude-code@latest")


def _warn_if_claude_too_old():
    text, ver = claude_version()
    if ver is None or ver >= MIN_CLAUDE:
        return
    # A warning, not a refusal. Everything installed is correct and stays
    # correct, so the moment they update it starts working with nothing to
    # redo; refusing would make them run this again for no reason.
    m = ".".join(str(n) for n in MIN_CLAUDE)
    print()
    print(f"  !! Your Claude Code is {text}, and Blink needs {m} or newer.")
    print("     Older versions do not put the usage figures in the status line")
    print("     at all, so the panel will sit blank until you update:")
    print()
    print("       npm install -g @anthropic-ai/claude-code@latest")
    print()
    print("     Nothing else needs redoing -- it starts working on its own.")


# -------------------------------------------------------------------- service


_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD plist_path() 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <!-- Frozen Python buffers stdout when it is a file, so without this the
       log lags minutes behind the daemon and reads as a hang. -->
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- The bridge waits for a board rather than exiting, so KeepAlive is a
       backstop for a crash, not the normal path. ThrottleInterval keeps a
       repeatable crash from becoming a spin. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

def _systemd_exec() -> str:
    """ExecStart, with every argument quoted.

    systemd splits ExecStart on whitespace, so a bare " ".join() broke on any
    space in the interpreter path, the checkout path or $HOME: with a spaced
    home the unit resolved the executable as the first fragment and failed
    with "Failed to locate executable", while `blink install` still printed
    "running (systemd)". The launchd and schtasks backends already quote
    per-argument; systemd was the only one that did not, and the CI scenario
    that uses a spaced home asserts only that the unit FILE exists.

    systemd's own quoting: double quotes, with \\ and \" escaped inside.
    """
    out = []
    for a in _service_command():
        out.append('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"'))
    return " ".join(out)


_UNIT_TEMPLATE = """[Unit]
Description=Blink USB bridge

[Service]
ExecStart={command}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _service_command():
    """What the login service should run.

    Frozen, that is the copy in ~/.blink/bin. From a checkout it is this
    interpreter and the repo's entry SCRIPT -- not `-m pc.cli`.

    `-m` was wrong and silently so. A login service starts with no working
    directory of ours (launchd uses /), so `python -m pc.cli` could not import
    `pc` at all: the agent crash-looped on ModuleNotFoundError every
    ThrottleInterval, forever, writing the same line to bridge.log -- while
    `blink status` reported "registered with launchd", because registration
    is not health. Found on the author's own machine 2026-08-28, where it had
    been failing unnoticed.

    Running the script BY PATH fixes it without a WorkingDirectory or a
    PYTHONPATH: Python puts a script's own directory on sys.path, and
    blink_main.py sits at the repo root next to `pc/`.
    """
    if _frozen():
        return [installed_bin(), "run"]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, os.path.join(repo, "blink_main.py"), "run"]


class _Backend:
    """One platform's answer to "start this at login, and keep it running".

    There used to be five sys.platform ladders: one each for install,
    restart, remove, the line the installer prints about what it creates,
    and status. Nothing held them together, so a case could be handled in
    four of them and forgotten in the fifth -- which is not hypothetical. A
    Linux box with no systemd got a considered answer from remove ("no
    systemd here; stop it yourself if you started it") and a shrug from
    status ("unknown on linux"), for the same machine in the same state.

    One object per platform now, chosen once by backend(). That also makes
    this code reachable from a unit test for the first time: a backend can
    be built on any machine and asked what it would run, where before the
    argv handed to launchctl, schtasks and systemctl could only be observed
    by installing for real on that platform. Three Windows defects in this
    file were found by CI rather than by a test for exactly that reason.

    The base class is not abstract -- it IS the behaviour for a platform
    with no supervisor we know how to drive, and every subclass overrides
    only the parts it can do better.
    """

    def creates(self):
        """What installing will put on disk, for the disclosure block.

        None when there is nothing to name.
        """
        return None

    def install(self) -> str:
        return (f"not supported on {sys.platform}; run it yourself: "
                f"{installed_bin()} run")

    def restart(self) -> str:
        return "not running under a supervisor; restart it yourself"

    def stop(self, runner=None) -> str:
        """Stop the installed service, without uninstalling it.

        Restart() is what `blink update` needs; this pair is what a test run
        needs. The fleet suite drives the real daemon against a real board, so
        it has to take the serial port off the service that owns it and hand
        it back afterwards -- on a machine the user works on, where anything
        that touched the plist, the unit file or the Scheduled Task itself
        would rewrite their own installation on the way past. So: stop and
        start only, and a service that is not installed is an answer rather
        than an error, because start() is called from a finally block where
        an exception would replace the failure the run was there to find.

        runner is subprocess.run unless a caller substitutes one. Resolved at
        call time rather than bound as a default argument, so a test that
        stubs subprocess.run is still in charge of what this can execute.
        """
        return "not running under a supervisor; stop it yourself"

    def start(self, runner=None) -> str:
        """Start the installed service again. See stop() for the pair.

        Not a restart: this assumes stop() ran, and does not go looking for
        anything else to kill. A caller that wants both is asking for
        restart().
        """
        return "not running under a supervisor; start it yourself"

    def remove(self) -> str:
        return "nothing to remove"

    def status(self) -> str:
        return f"unknown on {sys.platform}"


# launchctl exits with an errno. ESRCH is what booting out a job launchd is
# not running gives back ("Boot-out failed: 3: No such process"), which is an
# answer rather than a failure -- see _LaunchdBackend.stop.
_ESRCH = 3


class _LaunchdBackend(_Backend):
    def creates(self):
        return plist_path()

    def install(self) -> str:
        os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
        with open(plist_path(), "w", encoding="utf-8") as f:
            args = "".join(f"    <string>{_xml_escape(a)}</string>\n"
                           for a in _service_command())
            f.write(_PLIST_TEMPLATE.format(label=LABEL, args=args,
                                           log=_xml_escape(log_path())))
        uid = os.getuid()
        # bootout first so a rerun replaces the running agent rather than
        # failing with "service already loaded".
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True, **update.ota.NO_WINDOW)
        # ...and then retry, because bootout is asynchronous. The bootstrap
        # immediately after it can fail while launchd is still tearing the old
        # job down, which left a reinstall with no service running at all --
        # observed on the second install of the day, silently.
        #
        # That retry is still worth having, but it was only ever half of this
        # story: it guards a bootstrap that FAILS. A bootstrap that succeeds
        # also starts nothing. Measured on a live machine, with this very
        # plist (RunAtLoad is set in _PLIST_TEMPLATE):
        #
        #   launchctl bootout   gui/501/com.blink.bridge   -> rc 0
        #   launchctl bootstrap gui/501 <plist>            -> rc 0
        #   launchctl print     gui/501/com.blink.bridge   -> "not running",
        #                                                     runs = 0
        #                          (still not running six seconds later)
        #   launchctl kickstart gui/501/com.blink.bridge   -> running, runs = 1
        #
        # So bootstrap registers the job and kickstart is what runs it. Until
        # this line existed the customer was told "running (launchd)" about a
        # job that was registered and idle, and that would not have actually
        # started before their next login.
        for attempt in range(4):
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist_path()],
                               capture_output=True, **update.ota.NO_WINDOW)
            if r.returncode == 0:
                break
            time.sleep(0.5 * (attempt + 1))
        if r.returncode != 0:
            return f"installed, but could not be started: launchctl bootstrap gui/{uid} {plist_path()}"
        k = subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{LABEL}"],
                           capture_output=True, **update.ota.NO_WINDOW)
        if k.returncode == 0:
            return "running (launchd)"
        # Registered but not running. Not the same failure as a bootstrap that
        # never took -- the plist is on disk, launchd has the job, and the next
        # login does start it -- so it gets the same honest sentence rather
        # than a harder one: everything except the word "running" is true.
        return f"installed, but could not be started: launchctl kickstart gui/{uid}/{LABEL}"

    def restart(self) -> str:
        r = subprocess.run(["launchctl", "kickstart", "-k",
                            f"gui/{os.getuid()}/{LABEL}"], capture_output=True, **update.ota.NO_WINDOW)
        return "restarted" if r.returncode == 0 else "could not restart it"

    def stop(self, runner=None) -> str:
        """bootout, not `launchctl stop`.

        The plist sets KeepAlive, so a stopped job is one launchd starts
        straight back up -- the process would be gone for about a second and
        the serial port would be held again before anything else could take
        it. bootout unloads the job; the plist stays exactly where it is, and
        start() below bootstraps that same file back in.

        A job that is not loaded is stopped, not a failure. launchctl exits
        with an errno, and booting out something launchd has never heard of
        gives ESRCH -- "Boot-out failed: 3: No such process". That is an
        ordinary state: a plist on disk whose agent was already booted out,
        by a previous run or by hand. Reported as a failure it aborts the
        fleet agent with "the serial port is probably still held", about a
        port that is in fact free.
        """
        if not os.path.exists(plist_path()):
            return "not installed"
        uid = os.getuid()
        r = (runner or subprocess.run)(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
            capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "stopped"
        if r.returncode == _ESRCH:
            return "stopped (it was not loaded)"
        return f"could not stop it: launchctl bootout gui/{uid}/{LABEL}"

    def start(self, runner=None) -> str:
        """bootstrap, and then kickstart, because bootstrap starts nothing.

        `launchctl bootstrap` registers the job with launchd and exits 0
        without running it, RunAtLoad in the plist notwithstanding. Measured
        on a live machine -- see the block in install(), which had the same
        gap -- a bootstrapped job sits at "not running / runs = 0" for as
        long as you care to watch it, and one kickstart turns it into
        "running / runs = 1" with a pid.

        This is the fleet suite's restore path, called from a finally block
        on a machine someone works at. Answering "started" for a registered
        but idle job is the worst answer available here: the run reports
        success, the daemon never retakes the serial port, and the desk goes
        dark with nothing pointing at the cause. So a kickstart that fails
        is a failed start, even though the job is registered -- what the
        caller asked for is a process holding the port again.
        """
        if not os.path.exists(plist_path()):
            return "not installed"
        uid = os.getuid()
        run = runner or subprocess.run
        r = run(["launchctl", "bootstrap", f"gui/{uid}", plist_path()],
                capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            # No job to kick, and a second failing command would only bury the
            # one line the operator needs to paste.
            return f"could not start it: launchctl bootstrap gui/{uid} {plist_path()}"
        k = run(["launchctl", "kickstart", f"gui/{uid}/{LABEL}"],
                capture_output=True, **update.ota.NO_WINDOW)
        if k.returncode == 0:
            return "started"
        return f"could not start it: launchctl kickstart gui/{uid}/{LABEL}"

    def remove(self) -> str:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True, **update.ota.NO_WINDOW)
        _rm(plist_path())
        return "removed"

    def status(self) -> str:
        """Registered is not the same as working, and the difference is the
        whole point of this line.

        This used to answer "registered with launchd" for a job that had
        crash-looped 59 times, which is how a dead daemon went unnoticed on
        the author's own machine while every other line of `blink status`
        looked healthy. launchctl already knows -- it reports the run count
        and the last exit code -- so ask it.

        Parsed defensively: launchctl print is a human-readable dump and not a
        contract, so anything unrecognised falls back to the old answer rather
        than claiming a fault that may not exist.
        """
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                           capture_output=True, text=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            return "not installed"

        fields = {}
        for line in (r.stdout or "").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k in ("pid", "last exit code", "runs") and k not in fields:
                fields[k] = v

        if fields.get("pid", "").isdigit():
            return f"running (launchd, pid {fields['pid']})"

        exit_code = fields.get("last exit code")
        if exit_code and exit_code.isdigit() and int(exit_code) != 0:
            runs = fields.get("runs", "?")
            return (f"registered but FAILING -- exits with code {exit_code},"
                    f" {runs} attempts so far. See {log_path()}")
        return "registered with launchd"


class _SchtasksBackend(_Backend):
    def creates(self):
        return (f'a Scheduled Task named "{TASK_NAME}", and {launcher_path()}\n'
                "             the one-line script it runs, so the bridge starts\n"
                "             with no window")

    def install(self) -> str:
        # The task runs the launcher, not the program: see launcher_path().
        # The bridge's output goes to bridge.log by its own hand (--log),
        # since a hidden window is nowhere for it to go.
        try:
            os.makedirs(blink_home(), exist_ok=True)
            with open(launcher_path(), "w", encoding="ascii", newline="\r\n") as f:
                f.write(LAUNCHER_VBS)
        except OSError as e:
            return f"could not write {launcher_path()}: {e}"
        # /f overwrites a task from an earlier install rather than failing.
        # /sc onlogon needs no admin rights; a Windows service would.
        r = subprocess.run(
            ["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "onlogon",
             "/tr", f'wscript.exe //B //Nologo "{launcher_path()}"'],
            capture_output=True, text=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            return f"could not register a Scheduled Task: {r.stderr.strip()[:120]}"
        # A daemon from an earlier install is still running, holding the
        # serial port and the single-instance lock, and /create does nothing
        # to it: on the first reinstall over a live 1.0.2 the new task's
        # daemon started, found the lock held, and left -- the old one, with
        # its window, carried on (2026-08-29). Stop it the way restart() does.
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, **update.ota.NO_WINDOW)
        _kill_recorded_daemon()
        # /sc onlogon does not start it now, only at the next logon.
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, **update.ota.NO_WINDOW)
        return "running (Scheduled Task)"

    def restart(self) -> str:
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME],
                       capture_output=True, **update.ota.NO_WINDOW)
        # /end only reaches the instance the TASK launched. A daemon that
        # replaced itself started its successor detached (see
        # update.restart_from_daemon), and that one is not the task's -- so
        # without this, /run below would add a second daemon competing for the
        # same serial port.
        _kill_recorded_daemon()
        r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                           capture_output=True, **update.ota.NO_WINDOW)
        return "restarted" if r.returncode == 0 else "could not restart it"

    def _registered(self, runner=None) -> bool:
        r = (runner or subprocess.run)(["schtasks", "/query", "/tn", TASK_NAME],
                                       capture_output=True, **update.ota.NO_WINDOW)
        return r.returncode == 0

    def stop(self, runner=None) -> str:
        """/end, and then the daemon /end cannot reach.

        Same trap as restart(): /end stops the instance the TASK launched,
        while a daemon that replaced itself started its successor detached
        (see update.restart_from_daemon). That successor keeps the serial
        port, and a second process asking for the same COM port is refused
        with "Access is denied" -- which looks exactly like a board fault. So
        the kill happens even when there is no task registered at all: the
        port is the thing being freed here, not the task, and an unregistered
        task with a live detached daemon says so rather than reporting the
        bare "not installed" that would deny the port was ever held.
        """
        run = runner or subprocess.run
        registered = self._registered(run)
        r = None
        if registered:
            r = run(["schtasks", "/end", "/tn", TASK_NAME],
                    capture_output=True, **update.ota.NO_WINDOW)
        killed = _kill_recorded_daemon(runner=run)
        if not registered:
            return ("not installed; killed a detached daemon" if killed
                    else "not installed")
        if r.returncode == 0:
            return "stopped"
        return f'could not stop it: schtasks /end /tn "{TASK_NAME}"'

    def start(self, runner=None) -> str:
        run = runner or subprocess.run
        if not self._registered(run):
            return "not installed"
        r = run(["schtasks", "/run", "/tn", TASK_NAME],
                capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "started"
        return f'could not start it: schtasks /run /tn "{TASK_NAME}"'

    def remove(self) -> str:
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, **update.ota.NO_WINDOW)
        subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK_NAME],
                       capture_output=True, **update.ota.NO_WINDOW)
        _kill_recorded_daemon()
        _kill_by_path()
        return "removed"

    def status(self) -> str:
        r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                           capture_output=True, **update.ota.NO_WINDOW)
        return ("registered as a Scheduled Task" if r.returncode == 0
                else "not installed")


class _SystemdBackend(_Backend):
    """Linux, where the supervisor may simply not be there.

    systemd is overwhelmingly the default but it is not guaranteed, and a
    machine without it is not a broken machine -- so every method below has
    a real answer for that case rather than an error.
    """

    @staticmethod
    def _has_systemctl() -> bool:
        return bool(shutil.which("systemctl"))

    def creates(self):
        return unit_path()

    def install(self) -> str:
        # The unit file is written either way. It costs nothing, and it means
        # a machine that gains systemd later already has the right file.
        os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
        with open(unit_path(), "w", encoding="utf-8") as f:
            f.write(_UNIT_TEMPLATE.format(command=_systemd_exec()))
        if not self._has_systemctl():
            return f"no systemd here; run it yourself: {installed_bin()} run"
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, **update.ota.NO_WINDOW)
        r = subprocess.run(["systemctl", "--user", "enable", "--now",
                            "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "running (systemd)"
        return "installed, but could not be started: systemctl --user enable --now blink-bridge"

    def restart(self) -> str:
        if not self._has_systemctl():
            return super().restart()
        r = subprocess.run(["systemctl", "--user", "restart",
                            "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
        return "restarted" if r.returncode == 0 else "could not restart it"

    def stop(self, runner=None) -> str:
        if not self._has_systemctl():
            return super().stop(runner)
        if not os.path.exists(unit_path()):
            return "not installed"
        r = (runner or subprocess.run)(
            ["systemctl", "--user", "stop", "blink-bridge.service"],
            capture_output=True, **update.ota.NO_WINDOW)
        # An explicit stop is not undone by Restart=always, so unlike launchd
        # this needs nothing stronger than the obvious command.
        if r.returncode == 0:
            return "stopped"
        return "could not stop it: systemctl --user stop blink-bridge.service"

    def start(self, runner=None) -> str:
        if not self._has_systemctl():
            return super().start(runner)
        if not os.path.exists(unit_path()):
            return "not installed"
        r = (runner or subprocess.run)(
            ["systemctl", "--user", "start", "blink-bridge.service"],
            capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "started"
        return "could not start it: systemctl --user start blink-bridge.service"

    def remove(self) -> str:
        if not self._has_systemctl():
            # Install said "no systemd here; run it yourself", so whatever is
            # running was started by hand and nothing here can stop it. Saying
            # "removed" would be followed a line later by "Nothing of Blink's
            # is left running", which would not be true.
            _rm(unit_path())
            return "no systemd here; stop it yourself if you started it"
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
        _rm(unit_path())
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, **update.ota.NO_WINDOW)
        return "removed"

    def status(self) -> str:
        if not self._has_systemctl():
            # Was "unknown on linux", which is the drift described on
            # _Backend: remove() knew exactly what this machine's situation
            # was and status() claimed not to.
            return "no systemd here; not something this can check"
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                            "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
        return "running" if r.returncode == 0 else "not running"


def backend() -> _Backend:
    """The login-service backend for this machine.

    Built fresh each call rather than cached at import: every path it uses is
    resolved from HOME at call time (see the note above blink_home), and the
    tests move HOME between calls.
    """
    if sys.platform == "darwin":
        return _LaunchdBackend()
    if sys.platform == "win32":
        return _SchtasksBackend()
    if sys.platform.startswith("linux"):
        return _SystemdBackend()
    return _Backend()


def _install_service() -> str:
    if _skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
    return backend().install()


def restart_service() -> str:
    """Bounce the login service so it comes up on a freshly replaced binary.

    Not the same as exiting and letting the supervisor notice: this is called
    from `blink update`, which is a separate process from the daemon. The
    daemon's own path is simpler -- on macOS and Linux it exits and KeepAlive /
    Restart=always bring it back. Windows has neither: a Scheduled Task with an
    onlogon trigger does not restart anything, so it is told explicitly.
    """
    if _skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
    return backend().restart()


def _say_bye():
    """Tell the board the app is leaving for good, so it shows \"connecting\"
    rather than dozing off (firmware 1.2.0 and later). Best effort: the port
    the daemon last used, opened plainly (no DTR/RTS preset -- that would
    reset the board), one line, gone. Any failure is silent; the board falls
    back to what it always did."""
    try:
        import json
        import serial
        from pc import protocol
        with open(os.path.join(blink_home(), "board.json"), encoding="utf-8") as f:
            port = json.load(f).get("port")
        if not port:
            return
        with serial.Serial(port, 115200, timeout=0.2, write_timeout=1) as s:
            s.write((json.dumps(protocol.bye(), separators=(",", ":")) + "\n").encode())
            s.flush()
    except Exception:
        pass


def _remove_service() -> str:
    if _skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
    return backend().remove()


# One definition, in pc/update.py. This module already imports it.
_rm = update._rm


def _kill_recorded_daemon(runner=None) -> int:
    """Stop the bridge by the pid it wrote for itself, not by its name.

    Returns how many pids it signalled, which stop() reports: on a machine
    with no Scheduled Task registered, a daemon killed here is the difference
    between a freed serial port and one still held.

    Ending the Scheduled Task ends the process the task launched. PyInstaller's
    onefile bootloader re-executes the same .exe as a child, and that child
    keeps running the bridge loop and keeps blink.exe open, which is enough for
    Windows to refuse every attempt to delete it -- including the detached
    rmdir scheduled for after we exit.

    By pid, with the image name only as a FILTER: `taskkill /im blink.exe`
    matches the uninstaller too, and killing ourselves mid-uninstall is exactly
    what the previous attempt did. /t takes the bootloader's child with it.
    """
    if sys.platform != "win32":
        return 0
    killed = 0
    # Every place a daemon may have left its pid: ~/.blink since 1.1.0, and
    # beside the program before that -- which, after 1.1.0's install has
    # rotated the directory, means bin.old. The first 1.0.4 -> 1.1.0
    # install on a real PC left the old daemon running with the serial port
    # because only the new location was read (2026-08-29).
    for path in (pid_path(),
                 os.path.join(bin_dir(), "bridge.pid"),
                 os.path.join(bin_dir() + ".old", "bridge.pid")):
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if pid == os.getpid():
            continue              # somehow ours; nothing to stop
        # runner, so _Backend.stop() can be handed one and have every command
        # it causes go the same way. None is subprocess.run, resolved here
        # rather than as a default argument so the existing callers -- and the
        # tests that stub subprocess.run for them -- are unaffected.
        (runner or subprocess.run)(
            ["taskkill", "/f", "/t", "/pid", str(pid),
             "/fi", "IMAGENAME eq " + os.path.basename(installed_bin())],
            capture_output=True, **update.ota.NO_WINDOW)
        killed += 1
    return killed


def _kill_by_path():
    """Last resort: anything running the exact binary we are deleting.

    The pid file is the good path and this is what happens when it is missing
    -- an install from before it existed, a daemon killed before it could write
    one, a profile that moved. Matched on the full executable PATH and not on
    the image name, so the uninstaller (same name, different file, or the very
    same file) is never in scope, and our own pid is excluded outright.
    """
    if sys.platform != "win32":
        return
    target = installed_bin().replace("'", "''")     # PowerShell string escape
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ExecutablePath -eq '{target}' "
        f"-and $_.ProcessId -ne {os.getpid()} }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", script], capture_output=True, **update.ota.NO_WINDOW)


def _remove_bin_dir(attempts=6):
    """Delete ~/.blink/bin. Returns (done, message).

    Straightforward everywhere but Windows, which will not delete a running
    executable -- and here the executable is usually this one. The undo hint we
    print says `~/.blink/bin/blink.exe uninstall`, so a customer following it
    is asking a program to delete the file it is running from. The daemon can
    be holding the same file too: `schtasks /end` returns before the process
    has actually exited.

    So: try, wait, try again, and if Windows still says no, hand the job to
    something that will outlive us.

    This used to end with `taskkill /f /im blink.exe`, which is worse than the
    problem it was for -- the uninstaller has that image name, so it killed
    itself, mid-uninstall, having already removed the Scheduled Task and the
    status line. Every Windows scenario in CI exited non-zero with no output at
    all, which is exactly what being terminated looks like.
    """
    for attempt in range(attempts):
        for d in (bin_dir(), bin_dir() + ".old", bin_dir() + ".new"):
            shutil.rmtree(d, ignore_errors=True)
        if not any(os.path.exists(d) for d in
                   (bin_dir(), bin_dir() + ".old", bin_dir() + ".new")):
            return True, "removed"
        time.sleep(0.4 * (attempt + 1))
    if sys.platform == "win32":
        return _schedule_windows_cleanup()
    return False, "could not be removed -- something is still running from it"


def _schedule_windows_cleanup():
    """Let Windows delete the directory once nothing is running inside it.

    A detached cmd that waits a few seconds and then removes the tree. By the
    time it fires, this process has exited and released its own hold, and the
    service stopped a moment ago has finished going away.

    `ping -n 4 127.0.0.1` rather than `timeout /t`, which wants a console and
    fails outright in a detached process that has no window.
    """
    script = "ping -n 4 127.0.0.1 >nul" + "".join(
        f' & rmdir /s /q "{d}"' for d in (bin_dir(), bin_dir() + ".old",
                                          bin_dir() + ".new"))
    DETACHED_NO_WINDOW = 0x00000008 | 0x08000000
    try:
        subprocess.Popen(["cmd", "/c", script],
                         creationflags=DETACHED_NO_WINDOW, close_fds=True)
    except Exception as e:
        return False, f"could not be removed ({e})"
    return True, "will be gone a moment after this window closes"


# -------------------------------------------------------------------- install


def _announce():
    """Say what is about to change, before changing it.

    Install is deliberately unattended -- it asks nothing, because plugging the
    board in is meant to be the whole setup. That makes disclosure the only
    thing standing between us and silently editing a file the customer owns,
    so it is not optional and it runs before the first write.
    """
    print("Blink setup. Here is everything it is about to do, before it does any of it.")
    print()
    print(f"  Creates    {installed_bin()}")
    print("             a copy of this program and its support files, so the")
    print("             folder you downloaded can be deleted when this finishes.")
    print(f"  Creates    {shim_path()}")
    print("             the small script Claude Code runs to hand over the")
    print("             usage figures. It keeps the last status line payload")
    print("             Claude Code sent, which also names the session, the")
    print("             working directory and the transcript file. Only the")
    print("             usage figures are read out of it, and nothing in it")
    print("             leaves this machine. It is readable by you alone.")
    print(f"  Creates    {hook_shim_path()}")
    print("             a second small script, run when Claude Code starts and")
    print("             finishes work, so the panel can show whether it is busy.")
    print("             It records the event name, the time, and the session and")
    print("             agent ids Claude Code generates -- used to tell concurrent")
    print("             sessions apart, and for nothing else. No prompt, no tool")
    print("             arguments, no file paths, no message text.")
    print(f"  Creates    {os.path.join(blink_home(), 'state')}")
    print("             one small file per open session, deleted when it ends.")
    print(f"  Changes    {settings_path()}")
    # This list has to stay exactly true. Install asks nothing, so the
    # disclosure is the only thing standing between us and silently editing a
    # file the customer owns -- and it said "statusLine.command, and nothing
    # else in the file" for one release after the hooks key started being
    # written too. A disclosure that is merely mostly right is worse than none,
    # because it is the thing people rely on instead of reading the diff.
    # Counted, not spelled. This said "six" while the code wrote ten: the
    # number drifted when StopFailure, SubagentStart and SubagentStop were
    # added, and nothing failed. The comment above says this list has to stay
    # exactly true because it is the thing people rely on instead of reading
    # the diff -- so it now reads itself off the list it describes, and a test
    # pins the two together.
    print("             two keys: statusLine.command, and an entry under hooks")
    print(f"             for each of {len(install_hooks.HOOK_EVENTS)} Claude Code"
          " events. Nothing else in")
    print("             the file is touched, and your own hooks are left in place.")
    prev_cmd = install_statusline._current_command(
        install_statusline._load(settings_path()))
    if prev_cmd:
        print(f"             Your current status line is kept and still runs:")
        print(f"               {prev_cmd}")
    created = backend().creates()
    if created:
        print(f"  Creates    {created}")
        print("             so the bridge starts when you log in.")
    print()
    print("  It reads no credential and no token, and sends nothing anywhere.")
    print("  The usage figures come from Claude Code, Claude Desktop's usage")
    print("  cache, or the Codex session log -- each of which has already")
    print("  worked them out. From the Codex log it keeps only the rate-limit")
    print("  line, never the conversation.")
    print()
    print(f"  To undo all of it:  {installed_bin()} uninstall")
    print()


def cmd_install(_args) -> int:
    # Before anything is written, and before the disclosure: if the file we are
    # about to edit does not parse, the honest move is to change nothing at all
    # and say why. The alternative -- treating it as empty -- writes a fresh
    # settings.json over whatever the customer was halfway through editing.
    try:
        install_statusline._load(settings_path())
    except install_statusline.SettingsUnreadable as e:
        print(f"Blink setup stopped. {e}")
        print()
        print("Nothing was changed. Fix the file, or move it aside, and run")
        print("this again.")
        return 1

    _announce()

    print("[1/4] Program ... ", end="", flush=True)
    os.makedirs(bin_dir(), exist_ok=True)
    if _frozen():
        # The program is a directory -- the executable and its _internal/
        # support files -- so it is staged whole and rotated in with the
        # same swap the updater uses: <bin>.new is copied, self-tested, and
        # then renamed over <bin> while the previous one goes to <bin>.old.
        # That is what makes re-running the installer over a live install
        # work on every platform: a running program can be renamed but not
        # overwritten, and its directory likewise. Not when it IS the
        # installed copy: a re-run from ~/.blink/bin has nothing to copy.
        src = os.path.dirname(_self_path())
        if os.path.abspath(src) != os.path.abspath(bin_dir()):
            staged = bin_dir() + ".new"
            shutil.rmtree(staged, ignore_errors=True)
            shutil.copytree(src, staged, symlinks=True)
            ok, message = update.swap_in(staged, installed_bin(),
                                         RELEASE_VERSION)
            if not ok:
                print(message)
                print()
                print("Nothing else was changed.")
                return 1
        print(installed_bin())
    else:
        # From a checkout there is nothing to copy; the service points at this
        # interpreter instead. Customers never take this path.
        print("running from a checkout, nothing to copy")

    print("[2/4] Status line ... ", end="", flush=True)
    os.makedirs(blink_home(), exist_ok=True)
    # Private to the user. The shims write the status line payload -- which
    # names the working directory and the session -- and the per-session
    # state files in here. Both shims create with umask 077, but a directory
    # from an earlier install was made at the default umask, so this
    # tightens it once rather than leaving it to the next mkdir.
    for d in (blink_home(), os.path.join(blink_home(), "state")):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass          # absent (state/ appears on the first hook), or Windows
    _write_shim(shim_path(), "blink-statusline.sh")
    install_statusline._announce(settings_path(), shim_path(),
                                 undo_hint=f"{installed_bin()} uninstall")
    print("      " + install_statusline.install(settings_path(), shim_path()))

    print("[3/4] Activity hooks ... ", end="", flush=True)
    _write_shim(hook_shim_path(), "blink-hook.sh")
    try:
        print(install_hooks.install(settings_path(), hook_shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # The status line is the product; the activity light is a nicety. A
        # hooks section we cannot safely edit costs the user a pulsing dot,
        # and is not worth failing an install that has otherwise worked.
        print(f"skipped ({e})")

    print("[4/4] Background service ... ", end="", flush=True)
    print(_install_service())

    print()
    print("Done. Plug the board in over USB -- it picks it up on its own.")
    print(f"  Log:     {log_path()}")
    print(f"  Check:   {installed_bin()} status")
    print(f"  Undo:    {installed_bin()} uninstall")
    print()
    print("  You can delete the folder you downloaded.")
    # Absent first: it is a bigger fact than out-of-date, and the two are
    # mutually exclusive (claude_version() gives no version for an absent
    # install, which is exactly what _warn_if_claude_too_old returns on).
    _note_if_no_claude_code()
    _warn_if_claude_too_old()
    return 0


def cmd_uninstall(_args) -> int:
    print("Blink uninstall.")
    print()
    print("[1/4] Background service ... ", end="", flush=True)
    print(_remove_service())
    _say_bye()

    print("[2/4] Claude Code setting:")
    try:
        print("      " + install_statusline.uninstall(settings_path(), shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # Keep going. The login service is already gone by this point, so
        # stopping here would leave the machine half-undone -- and the one
        # thing we must not do is "repair" the file by writing a fresh one
        # over whatever the customer was in the middle of editing.
        print(f"      Left alone: {e}")
        print("      Remove the statusLine.command line by hand once it parses.")

    print("[3/4] Activity hooks:")
    try:
        print("      " + install_hooks.uninstall(settings_path(),
                                                 hook_shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # Same reasoning as the step above: keep going, and never "repair" a
        # file we cannot parse by writing a fresh one over it.
        print(f"      Left alone: {e}")

    print("[4/4] Files ... ", end="", flush=True)
    # Only what install created. NOT ~/.blink itself: it also holds the two
    # signing keys, which cannot be regenerated -- every board flashed with the
    # first one's public half, and every app carrying the second one's, would
    # stop accepting updates.
    for p in (shim_path(), hook_shim_path(), launcher_path(),
              os.path.join(blink_home(), "statusline.json"),
              os.path.join(blink_home(), "statusline.json.tmp"),
              # Pid-scoped temp names, from a render interrupted mid-write.
              *glob.glob(os.path.join(blink_home(), "statusline.json.*.tmp")),
              os.path.join(blink_home(), "state.json"),
              os.path.join(blink_home(), "state.json.tmp"),
              os.path.join(blink_home(), "pending_fw.json")):
        _rm(p)
    # The per-session state directory, and everything under it. state.json
    # above is the single-slot file this replaced; it is still on the list so
    # an install that predates the directory leaves nothing behind.
    _rm_state_dir()
    done, message = _remove_bin_dir()
    print(message)
    print()
    if done:
        print("Done. Nothing of Blink's is left running.")
        return 0
    print("Everything else is undone, but that file is still there. Log out and")
    print("back in, then delete it by hand:")
    print(f"  {bin_dir()}")
    return 1


def _live_sessions() -> int:
    """How many sessions the hooks are currently tracking.

    The most useful support answer after "are the hooks installed": whether
    they are actually firing. A count of zero on a machine where Claude Code
    is open says the hooks are configured and not running, which is a
    different problem from not being configured at all.
    """
    from pc.providers import claude_state
    try:
        counts, _ = claude_state.ClaudeStateProvider(
            path=os.path.join(blink_home(), "state"), sweep=False
        ).scan(time.time())
    except Exception:
        return 0
    return sum(counts.values())


def _rm_tree(root):
    """Remove a flat directory of per-session files."""
    try:
        for name in os.listdir(root):
            _rm(os.path.join(root, name))
        os.rmdir(root)
    except OSError:
        pass


def _rm_state_dir():
    """Remove ~/.blink/state and its per-session subdirectories.

    Two levels deep and no deeper, by construction: the shim only ever creates
    <session>.state files and <session>/<agent> files. Walking rather than
    shutil.rmtree because this runs against a path under the customer's home
    and a bounded loop cannot be talked into deleting more than it was told.
    """
    root = os.path.join(blink_home(), "state")
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        p = os.path.join(root, name)
        if os.path.isdir(p):
            for inner in (os.listdir(p) if os.path.isdir(p) else []):
                _rm(os.path.join(p, inner))
            try:
                os.rmdir(p)
            except OSError:
                pass
        else:
            _rm(p)
    try:
        os.rmdir(root)
    except OSError:
        pass


def cmd_status(args) -> int:
    if _skip_service():
        # The launchd label and systemd unit name are global while everything
        # else is scoped to $HOME, so querying them under a test HOME reports
        # the real user's agent -- which read as "installed" for an install
        # that never happened.
        print("Bridge      not checked (BLINK_SKIP_SERVICE=1)")
    else:
        print("Bridge      " + backend().status())

    print(f"App         {RELEASE_VERSION}")
    for line in _board_lines():
        print(line)

    text, ver = claude_version()
    if text is None:
        # The consequence, not just the fact. "Not found" alone left someone
        # with missing countdowns and no way to connect the two.
        if desktop_app_present():
            print("Claude Code not found -- running on Claude Desktop alone"
                  " (no countdowns, no activity light)")
        elif codex_present():
            print("Claude Code not found -- running on Codex alone"
                  " (no Claude page, no activity light)")
        else:
            print("Claude Code not found -- and no Claude Desktop cache or"
                  " Codex log either, so nothing is reporting usage")
    elif ver is None:
        # It ran and said something we could not read as a version. Saying
        # "not found" would send someone to reinstall a working install.
        print(f"Claude Code {text} -- could not read a version from that")
    elif ver < MIN_CLAUDE:
        m = ".".join(str(n) for n in MIN_CLAUDE)
        print(f"Claude Code {text} -- TOO OLD, needs {m}+ (panel will stay blank)")
    else:
        print(f"Claude Code {text}")

    print("Status line " + (f"installed at {shim_path()}" if os.path.exists(shim_path())
                            else "not installed"))

    # Install writes two things into settings.json, so status has to report
    # two. Reporting only the status line is the same omission the setup
    # disclosure had: someone whose activity pip never lights needs a way to
    # see whether the hooks are actually there, and "Status line installed"
    # answers a different question.
    #
    # Counted from settings.json rather than from the shim's presence on disk:
    # the file existing proves an install ran once, not that Claude Code is
    # still configured to call it, and drift is exactly what goes wrong here.
    try:
        hooks = install_statusline._load(settings_path()).get("hooks")
        # isinstance, not `or {}`: only None and {} take the `or` branch, so a
        # string or list under "hooks" reached .get() and crashed status --
        # the same trap _current_command documents for statusLine.
        if not isinstance(hooks, dict):
            hooks = {}
        ours = sum(
            1 for event, _ in install_hooks.HOOK_EVENTS
            for group in (hooks.get(event) or [])
            if isinstance(group, dict)
            for h in (group.get("hooks") or [])
            if isinstance(h, dict)
            and h.get("command") == install_hooks.hook_command(
                hook_shim_path(), event))
        total = len(install_hooks.HOOK_EVENTS)
        if ours == total:
            live = _live_sessions()
            note = f", {live} live session{'s' if live != 1 else ''}" if live else ""
            print(f"Activity    hooks installed ({ours}/{total} events{note})")
        elif ours:
            print(f"Activity    PARTIAL -- {ours}/{total} hooks present;"
                  f" run `{installed_bin()} install` to restore them")
        else:
            print("Activity    hooks not installed -- the busy/idle pip will"
                  " stay dark")
    except install_statusline.SettingsUnreadable:
        print("Activity    unknown -- settings.json does not parse")

    # The most useful support answer: is fresh data actually arriving?
    payload = os.path.join(blink_home(), "statusline.json")
    if os.path.exists(payload):
        age = int(time.time() - os.path.getmtime(payload))
        # The same bound the panel uses (statusline_source.STALE_AFTER_S):
        # this said "old" at two minutes for a reading the board was still
        # showing as live.
        if age < statusline_source.STALE_AFTER_S:
            print(f"Usage data  fresh ({age}s old)")
        else:
            print(f"Usage data  {age}s old -- open Claude Code to refresh it")
    elif codex_present():
        print("Usage data  from the Codex session log (no Claude Code status"
              " line yet)")
    else:
        print("Usage data  none yet -- open Claude Code once so it renders "
              "its status line")
    _source_lines()
    if getattr(args, "wire", False):
        _wire_line()
    return 0


def _wire_line():
    """The usage message the bridge would put on the cable right now.

    The last thing that can be checked without a board: every source read,
    merged and encoded exactly as the daemon does it (pc/ingest -> normalizer
    -> protocol). CI feeds the installed binary the files a customer's machine
    would have and asserts on this line; a person can paste it into a report.
    """
    import json as _json
    from pc import ingest
    msg = ingest.IngestionBus().poll()
    print()
    if msg is None:
        print("Wire        nothing to send -- no source has a reading")
        return
    print("Wire        the next usage message, exactly as the board gets it:")
    print(_json.dumps(msg, separators=(",", ":")))


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def board_lines(known, ports):
    """The Board lines of `blink status`, from what the app remembers and
    what is plugged in right now.

    `known` is board.json ({port, board_id, fw}); `ports` is [(device,
    chip)] for every port that could be a board. A developer with other
    CH340 hardware on the desk needs to see that the app knows the
    difference: the board it talks to, named; everything else listed as
    looked at once and left alone.
    """
    port = known.get("port")
    here = [d for d, _ in ports]
    chip = dict(ports)
    out = []
    if port and port in here:
        ident = []
        if known.get("board_id"):
            ident.append(f"id {known['board_id']}")
        if known.get("fw"):
            ident.append(f"firmware {known['fw']}")
        tail = f" -- {', '.join(ident)}" if ident else ""
        out.append(f"Board       {port} ({chip[port]}){tail}")
    elif here:
        seen = ", ".join(f"{d} ({c})" for d, c in ports)
        out.append(f"Board       none identified yet -- looking at {seen}")
        if port:
            out.append(f"            last seen on {port}")
    else:
        out.append("Board       not plugged in"
                   + (f" (last seen on {port})" if port else ""))
    others = [(d, c) for d, c in ports if d != port]
    if port and port in here and others:
        names = ", ".join(f"{d} ({c})" for d, c in others)
        out.append(f"            other serial devices: {names}"
                   " -- asked once, not BLINK, left alone")
    return out


def _board_lines():
    try:
        from claude_usage_bridge import describe_ports, remembered_board
        return board_lines(remembered_board(blink_home()), describe_ports())
    except Exception as e:
        return [f"Board       could not list serial ports: {e}"]


def _source_lines():
    """One line each for the two sources CI cannot exercise.

    Claude Desktop is a closed GUI app and Codex will not write a rollout
    without an account, so neither format change can be caught on a runner.
    It can only be caught here, on the machine where the file is -- and the
    daemon already knows (it logs once and goes quiet). This is where a
    person finds out, and what they paste into a bug report.
    """
    from pc.providers import claude_desktop, codex_cli
    now = time.time()

    desktop = claude_desktop.ClaudeDesktopProvider()
    if os.path.exists(desktop.path()):
        frames = desktop.poll(now)
        if frames:
            age = now - frames[0].observed_at
            print(f"Desktop     usage cache parsed, reading"
                  f" {_age(age)} old")
            for line in desktop_staleness_note(age):
                print(f"            {line}")
        else:
            print("Desktop     usage cache present but did not parse --"
                  " the app's file format may have changed; please report it")
    else:
        # Name the path. Whether Claude Desktop keeps this file where we
        # think it does has been seen on macOS and is a convention on the
        # other two; the first person to run this beside a signed-in Desktop
        # on Windows or Linux is the check, and this line is what they need.
        print("Desktop     no usage cache (Claude Desktop not installed, or"
              " never signed in)")
        print(f"            looked at {desktop.path()}")

    logs = codex_cli.recent_rollouts()
    if logs:
        frames = codex_cli.CodexCliProvider().poll(now)
        if frames:
            print(f"Codex       session log parsed, reading"
                  f" {_age(now - frames[0].observed_at)} old")
        else:
            print(f"Codex       {len(logs)} recent session log(s), none with a"
                  " rate-limit line -- finish one turn in Codex; if that does"
                  " not help, its log format may have changed; please report it")
    else:
        print("Codex       no session logs (Codex not installed, or never run)")
        print(f"            looked under {codex_cli.sessions_root()}")


def cmd_update(_args) -> int:
    """Fetch and install a newer daemon, then restart the service.

    Manual by design for now. The machinery for doing it unattended is all
    here, but it is gated on `daemon.auto` in the signed manifest so that the
    first release to switch it on is a decision someone makes, not a side
    effect of shipping this command.
    """
    print(f"Blink update. This app is {RELEASE_VERSION}.")
    print()
    manifest = update.fetch_signed_manifest()
    if manifest is None:
        print("Could not read the release feed, or it is not properly signed.")
        print("Nothing was changed.")
        return 1
    found = update.available(manifest)
    if not found:
        key = update.platform_key()
        if key is None:
            print(f"There is no published build for {sys.platform}"
                  f" {os.uname().machine if hasattr(os, 'uname') else ''}.")
            return 1
        print("Already up to date.")
        return 0
    version, artifact = found
    print(f"Downloading {version} ...")
    try:
        blob = update.download(update.platform_key(), artifact)
    except Exception as e:
        print(f"Download failed: {e}")
        print("Nothing was changed.")
        return 1
    ok, message = update.apply(blob, installed_bin(), version)
    print(message)
    if not ok:
        return 1
    print("Background service ... " + restart_service())
    return 0


def cmd_provision(args) -> int:
    """Stamp this unit's edition. A factory step, run ONCE after programming.

    Deliberately its own command rather than a flag on `install`: it is not
    part of setting a customer's machine up, it is part of building a board,
    and the two happen in different places by different people.

    Once is enforced by the BOARD, not by this. The user installs this same
    binary, so a check here would be a check on the honour system -- the
    firmware latches the record on the first successful stamp and refuses
    every later one. All this can do is report that clearly.

    The daemon owns the serial port whenever it is running, so this stops the
    login service, talks to the board, and starts it again. That is heavier
    than it looks like it needs to be and it is the honest sequence: two
    processes cannot hold one tty, and a provisioning step that silently did
    nothing because the port was busy is exactly the failure this product
    cannot afford at the point a board is being boxed.
    """
    import serial                      # only this path needs it installed

    from claude_usage_bridge import autodetect_port

    port = args.port or autodetect_port()
    if not port:
        print("No board found. Plug one in, or pass --port.", file=sys.stderr)
        return 1

    stopped = False
    if not _skip_service():
        # Stop by pid first: the login service may not be registered on a
        # bench machine, and the pid file is the thing that is true either way.
        _kill_recorded_daemon()
        try:
            backend().remove()
            # Put a service back afterwards only where one belongs: on a
            # machine Blink is INSTALLED on. remove() reports "removed"
            # whether or not anything was registered, and treating that as
            # proof of a service left a bench laptop -- running the
            # downloaded binary, never installed -- with a login item
            # pointing at ~/.blink/bin/blink, which does not exist there,
            # respawning on every throttle interval forever.
            stopped = os.path.exists(installed_bin())
        except Exception:
            pass
        time.sleep(1.0)

    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = args.baud
        ser.timeout = 0.2
        # Same open dance as the daemon: leave DTR/RTS alone, then pulse RTS,
        # so the board comes up in run mode rather than the ROM loader. See
        # the long note in claude_usage_bridge.main.
        ser.dtr = False
        ser.rts = False
        ser.open()
        ser.dtr = False
        ser.rts = True
        time.sleep(0.15)
        ser.rts = False
        time.sleep(0.4)
        ser.reset_input_buffer()
    except Exception as e:
        print(f"Could not open {port}: {e}", file=sys.stderr)
        if stopped:
            print("Background service ... " + _install_service())
        return 1

    try:
        # Wait for the board to say hello before sending anything.
        #
        # Opening the port resets it (the RTS pulse above is deliberate --
        # see the daemon), so for the first second or two the thing on the
        # other end is a bootloader, not the firmware. The first attempt at
        # this wrote immediately after a 0.4 s sleep and the message went
        # into the void: no error, no confirmation, indistinguishable from a
        # board too old to understand it. `hello` is the board saying it is
        # listening, and it is the only honest thing to wait for.
        heard = []
        ready = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            chunk = ser.readline()
            if not chunk:
                continue
            text = chunk.decode("utf-8", "replace").strip()
            if text:
                heard.append(text)
            if '"t":"hello"' in text.replace(" ", ""):
                ready = True
                break
        if not ready:
            print(f"No hello from the board on {port} within 10 s.",
                  file=sys.stderr)
            ser.close()
            if stopped:
                print("Background service ... " + _install_service())
            return 1

        ser.write(protocol.encode(protocol.edition(args.edition)))
        ser.flush()
        # Read back what the board says about it. The firmware prints one
        # line either way, and that line is the whole confirmation: a board
        # too old to know the message answers nothing, which is a different
        # outcome from a board that stored it.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            chunk = ser.readline()
            if not chunk:
                continue
            text = chunk.decode("utf-8", "replace").strip()
            if text:
                heard.append(text)
            if "[cfg] edition" in text:
                break
    finally:
        ser.close()
        # The service goes back in the SAME finally that closes the port.
        #
        # It used to be restored only on the happy path and the two early
        # returns, so a nudged cable, a CH340 re-enumerating, or Ctrl-C during
        # the ten-second hello wait propagated straight past it: the launchd
        # plist / systemd unit had already been deleted, nothing restarted the
        # daemon at next login, and nothing said so. On an assembly line that
        # is a unit boxed with a dead bridge.
        #
        # Idempotent by construction -- `stopped` is cleared here, so the tail
        # of the function cannot install it twice.
        if stopped:
            print("Background service ... " + _install_service())
            stopped = False

    said = [h for h in heard if "[cfg] edition" in h]
    if said:
        line = said[-1].split("[cfg] ", 1)[-1]
        print(line)
        # A refusal is not a success. The edition latches on the first stamp
        # and the board declines every one after it (cfg_set_edition), so a
        # unit that comes back down the line for a second pass has to fail
        # here loudly rather than be boxed as whatever the label said.
        if "refusing" in line:
            print("This unit was stamped already. Clearing it means erasing"
                  " the config partition\nover USB with the board in"
                  " bootloader mode -- a factory operation, on purpose.",
                  file=sys.stderr)
            rc = 1
        else:
            rc = 0
    else:
        print(f"Sent edition={args.edition}, but the board did not confirm it."
              "\nA board running firmware older than this feature ignores the"
              " message.", file=sys.stderr)
        rc = 1

    # Normally already done by the finally above; kept for the paths that
    # never reached it.
    if stopped:
        print("Background service ... " + _install_service())
    return rc


def cmd_run(args) -> int:
    """The daemon. This is what the login service starts.

    Its arguments are handed over explicitly. Calling main() with none left it
    parsing sys.argv itself, which in the packaged binary begins with the
    subcommand name "run" -- rejected on the spot, restarted by the service ten
    seconds later, forever.
    """
    import claude_usage_bridge
    log = getattr(args, "log", None)
    if log:
        # The Windows service runs with no window: this is where its output
        # goes instead. launchd and systemd redirect for us; wscript does not.
        f = open(log, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = f
    forwarded = []
    if getattr(args, "port", None):
        forwarded += ["--port", args.port]
    if getattr(args, "baud", None):
        forwarded += ["--baud", str(args.baud)]
    claude_usage_bridge.main(forwarded)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="blink", description="Blink desk gauge: setup and bridge.")
    # Also the self-test in pc/update.py: a replacement binary has to run and
    # say what it is before it is allowed to become the login service's target.
    parser.add_argument("--version", action="version",
                        version=f"blink {RELEASE_VERSION}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("install", help="Set everything up (this is the default)")
    sub.add_parser("uninstall", help="Put it all back")
    status_p = sub.add_parser("status", help="Is the panel getting data?")
    status_p.add_argument(
        "--wire", action="store_true",
        help="Also print the usage message the bridge would send the board"
             " right now, as one JSON line")
    sub.add_parser("update", help="Fetch a newer version of this app")
    prov_p = sub.add_parser(
        "provision",
        help="Stamp this unit's edition (factory step, run once)")
    prov_p.add_argument("--edition", required=True,
                        choices=list(protocol.EDITIONS),
                        help="Which boot clip this board plays")
    prov_p.add_argument("--port", default=None,
                        help="Serial port (default: find the board)")
    prov_p.add_argument("--baud", type=int, default=115200)
    run_p = sub.add_parser("run", help="Run the bridge in the foreground")
    run_p.add_argument("--port", default=None,
                       help="Serial port (default: find the board)")
    run_p.add_argument("--baud", type=int, default=115200)
    run_p.add_argument("--log", default=None,
                       help="Append output to this file (the Windows service)")
    args = parser.parse_args(argv)

    # Bare `./blink` installs. Someone who just downloaded a file and
    # double-clicked it meant "set this up", and making them discover a
    # subcommand first is the opposite of the point.
    return {
        None: cmd_install,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "update": cmd_update,
        "provision": cmd_provision,
        "run": cmd_run,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
