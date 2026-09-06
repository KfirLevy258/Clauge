"""The fleet suite's foreman on one machine: own the board, prove it works.

Run on each of the three desks before a release:

    python -m tests.fleet.agent --board claude --port auto \\
        --scenarios tests/fleet/scenarios --out results.json

It takes the serial port off the installed service, runs the real daemon
against the real board once per scenario, reads the transcript BLINK_TAP left
behind, and hands the verdict to tap_asserts.check(). Then it gives the port
back -- from a finally, on every path including the ones that raise, because
this brackets somebody's working day and the desk has to be exactly as it was
found.

Given --bundle and --prev-bundle it also runs the two scenarios that need no
board at all: installing the release the way a customer does, and updating the
previous one off a local copy of the signed feed. Those two run BEFORE the
service is stopped -- neither goes near the serial port -- so an unplugged
desk still answers for the half of the product a customer meets first.

It also owns the daemon's LIFETIME, which is not a detail: a scenario with
`host_silence_s` is run in two passes with the daemon stopped in between,
because stopping the daemon is the only thing that makes this board sleep.
The firmware stamps last_host_ms on any host protocol line (proto.c:262) and
the daemon answers every ten-second ping with a pong, so no poll interval
leaves the board 30 s silent while a daemon is alive. Quiet data is not a
quiet host.

Four decisions worth stating, because each of them is a bug that has already
happened once somewhere in this repository:

  - BLINK_SKIP_SERVICE goes in the CHILD's environment and nowhere else. The
    daemon we spawn must not stop the login service out from under us; but if
    this process ever inherited or set that variable, stop_service() would
    become a no-op, the real daemon would keep the port, ours would be
    refused it, and the run would report a hardware fault against a healthy
    board -- with start_service() no-opping in turn, so nothing broken is
    left behind to diagnose from. tests/ci/check_install.sh documents
    exporting that variable, so a shell that has it set is realistic. A
    skipped stop therefore aborts the run before anything is spawned.

  - HOME and USERPROFILE are both redirected for a scenario pass, for the
    reason tests/conftest.py gives: expanduser reads HOME on POSIX and
    USERPROFILE on Windows, and setting one without the other sent twelve
    tests writing into a real user profile while asserting against a
    temporary directory.

  - launchctl bootout returns before the port is free. So the run does not
    take the first refusal as an answer: it retries a short handshake until
    the board speaks, and if it never does, the result says which of the two
    it could not tell apart rather than picking one. The same asynchrony sits
    between two scenarios, where there is no handshake to hide behind: a
    settle precedes each one, and a daemon whose first traffic came late is
    reported as inconclusive rather than as a board that failed.

  - A child that will not die is named. Nothing else here can mean anything
    while a daemon this run started still holds the port, and the shape of
    that failure is a desk that looks perfectly healthy with a dark board on
    it, so _stop_daemon() reports rather than shrugs.

Port selection is left to the daemon, which finds the board by USB VID:PID
(claude_usage_bridge.py:41-48) -- that is how it picks COM15 out of the
Windows desk's twelve mostly-Bluetooth ports. --port is forwarded only when
the operator names one.
"""
import argparse
import json
import locale
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

from pc.service_ctl import start_service, stop_service
from pc.update import archive_name, platform_key
from tests.fleet import tap_asserts

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIOS = REPO_ROOT / "tests" / "fleet" / "scenarios"

# A scenario is a timeline and the daemon emits one usage frame per poll, so
# at the shipped 60 s a thirty-second scenario would produce a single frame
# and the sequence under test would never reach the board.
POLL_INTERVAL_S = 3.0

# Time past the scenario's own duration before the daemon is stopped. See
# grace_s(): a fixed allowance is the wrong shape, because what has to happen
# after the last step differs per scenario.
GRACE_BASE_S = 8.0
GRACE_POLLS = 2
GRACE_WAKE_S = 6.0

# A stopped daemon does not hand the port back the instant it exits, and the
# next scenario's daemon is spawned immediately after. Cheap insurance
# against a shifted timeline; see CONNECT_TOLERANCE_S for the detection that
# catches the times it is not enough.
PORT_SETTLE_S = 2.0

# ScriptedProvider stamps its t0 when the daemon is CONSTRUCTED, not when it
# reaches the board (pc/providers/scripted.py:49-58). A daemon kept waiting
# for a busy port therefore starts its timeline early: steps come due while
# there is nothing to send them over, and the replay -- one step per poll --
# hands the whole timeline over that much later than the file describes,
# pushing its last steps towards the end of duration_s + grace and past it if
# the wait was long enough. Scenario steps are pinned at least 4 s apart
# (tests/pc/test_fleet_scenarios.py), so a first record later than that is
# the point where the run stops being able to prove what it set out to.
CONNECT_TOLERANCE_S = 4.0

# The handshake that absorbs an asynchronous bootout.
SETTLE_TIMEOUT_S = 15.0
SETTLE_ATTEMPTS = 3

# The real-account pass waits for whatever this machine's tools happen to
# report, which nothing here controls the timing of.
REAL_ACCOUNT_TIMEOUT_S = 120.0
REAL_ACCOUNT = "real_account"

# How long the real-account pass keeps its daemon alive AFTER the usage frame
# has left the host, waiting for the board's own two records. Sized off the
# board's ping, which is every 10 s (PING_INTERVAL_MS, firmware/src/proto.c):
# an `rx` record can only appear when a board message meets a running read
# loop, and on a quiet link the ping is the only message that comes. A window
# shorter than one ping interval could therefore expire between two pings on a
# perfectly healthy desk -- the same race in a smaller frame. One interval
# plus half of it again leaves room for the apply-and-print and for a settle
# that begins just after a ping went by, and costs nothing against the
# orchestrator's per-desk budget (tools/fleet/run.py AGENT_TIMEOUT_S).
REAL_ACCOUNT_SETTLE_S = 15.0

# What the real-account pass demands: the same shape as a scenario's block,
# reduced to the only two things true of every desk -- one frame out, one
# frame applied. Percentages come from a live account, so no count of stale
# lines and no sleep window can be asserted.
REAL_ACCOUNT_EXPECT = {"min_tx": 1, "min_board_usage": 1,
                       "min_stale_lines": 0, "min_sleep_wakes": 0}

# The two scenarios that need no board: what a customer does on day one, and
# what they do on the day after the next release.
FRESH_INSTALL = "fresh_install"
UPDATE_PATH = "update_path"

# Unpacking 50 MB, an installer that self-tests the copy it made, and an
# update that downloads, unpacks and self-tests again. The self-test alone is
# allowed 300 s (pc/update.py:244), measured at 97 s on a machine under load
# average 89 -- a laptop mid-build, which is exactly when somebody runs this.
# So this has to sit outside 300 s comfortably, or the outer cap fires first
# and replaces the update's own message with "could not be run at all".
#
# It is a cap per COMMAND, and the customer path makes BUNDLE_CALLS of them,
# so the number that matters is the product: at the 900 s this used to be,
# the two bundle scenarios alone could spend the orchestrator's whole per-desk
# budget (tools/fleet/run.py AGENT_TIMEOUT_S) and the four board scenarios
# would never run -- reported as a timed-out desk rather than as a result.
# The two are pinned against each other by a test.
BUNDLE_TIMEOUT_S = 420.0

# fresh_install: unpack, --version, install, --version. update_path: unpack,
# --version, install, --version, update, --version.
BUNDLE_CALLS = 10

# Directories the run makes for itself under the work root. A scenario of the
# same name would have its working directory emptied from under it -- or, for
# "home", would take the run's shared sandbox home with it. See
# select_scenarios(), which refuses one before anything is stopped.
RESERVED_WORK_NAMES = frozenset({"home", "preflight", REAL_ACCOUNT,
                                 FRESH_INSTALL, UPDATE_PATH})


def _popen(cmd, env, cwd):
    """Start the daemon detached from any console.

    The Windows desk runs this from a shell that must not sprout a second
    window, and the daemon's own output is prose for a person -- kept beside
    the tap it belongs to, so a failed run has both halves of the story.

    The log handle is closed as soon as Popen returns: the child has its own
    duplicate by then, and five scenarios plus a preflight would otherwise
    leave this process holding descriptors it never uses again.
    """
    kwargs = {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kwargs["creationflags"] = flags
    with open(str(env["BLINK_TAP"]) + ".log", "ab") as log:
        return subprocess.Popen(cmd, env=env, cwd=str(cwd), stdout=log,
                                stderr=subprocess.STDOUT, **kwargs)


class Deps(NamedTuple):
    """Everything that touches the world outside this process.

    Gathered into one argument so the whole agent can be exercised with
    fakes. No unit test in this repository may spawn the daemon, open a
    serial port or move a login service, and the only way to keep that true
    of a module whose entire job is those three things is to make them
    substitutable.
    """
    spawn: Callable = _popen
    sleep: Callable = time.sleep
    now: Callable = time.monotonic
    # Wall clock, separate from `now`: tap records are stamped in epoch
    # seconds by the daemon, so comparing one against a monotonic reading
    # would subtract two different origins and produce nonsense.
    wall: Callable = time.time
    runner: Callable = subprocess.run
    stop: Callable = stop_service
    start: Callable = start_service


def daemon_cmd(port=None):
    """The command that runs the bridge in the foreground.

    `auto` is not passed through: the daemon's own autodetection is the thing
    that finds this hardware, and the name heuristic it replaced never once
    matched a real board.
    """
    cmd = [sys.executable, str(REPO_ROOT / "blink_main.py"), "run"]
    if port and port != "auto":
        cmd += ["--port", port]
    return cmd


def prepare_scenario(path, board, workdir):
    """The scenario file to hand the daemon for this board.

    A Codex desk replays the same timeline attributed to Codex, in a copy --
    the shipped file is an input to three machines and none of them may edit
    it. The daemon keeps preferred_provider="claude", but that only decides
    which of TWO providers takes the headline; with codex the only one
    reporting, normalizer.select_pair() returns it as the primary anyway
    (pc/normalizer.py:213-218), so the frame under test is still the one on
    the big number.
    """
    path = Path(path)
    if board != "codex":
        return path
    doc = json.loads(path.read_text(encoding="utf-8"))
    for step in doc.get("steps", []):
        if isinstance(step, dict):
            step["provider"] = "codex"
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / path.name
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return out


def _clean_env():
    """This process's environment with every inherited BLINK_* variable gone.

    Swept by prefix rather than removed by name, because a list of names is a
    snapshot and this has to stay true of variables nobody has written yet. A
    hand-kept list was already one short when review found it: an inherited
    BLINK_RELEASE_PUBKEY_FILE would have let update_path verify a throwaway
    locally signed manifest and report that it had proved the real update
    path, and tests/ci/check_update.sh exports exactly that variable.

    The daemon passes need this as much as the install ones and arguably
    more, because theirs reaches hardware: BLINK_OTA_DIR redirects the
    FIRMWARE feed (pc/ota.py:52), and a scenario is a running daemon offering
    firmware to a real board. A stray variable in the operator's shell could
    put an unrelated local build in front of three of them.

    Sweeping cannot cost us BLINK_SKIP_SERVICE, which is the one variable
    that must reach every child: it is SET by each builder after this runs,
    never inherited. Both builders are pinned by a test that names their
    whole BLINK_* surface, so a reordering that broke that would fail at
    once -- and another test reads the shipped sources for BLINK_* and
    requires every name it finds to be swept here or set back deliberately,
    so the pair cannot fall behind pc/ the way a list would.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("BLINK_")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _redirect_home(env, sandbox_dir):
    """Point every notion of "the user's home" at the sandbox. Both, always.

    One function rather than the same two assignments in three places,
    because the bug this prevents is precisely the one where somebody writes
    only the line their own platform needs: expanduser reads HOME on POSIX
    and USERPROFILE on Windows, and setting one without the other once sent
    twelve tests writing into a real user profile while asserting against a
    temporary directory (tests/conftest.py). The install and update scenarios
    below run a real installer, so for them the cost of that mistake is not a
    confused assertion -- it is somebody's actual ~/.blink.
    """
    env["HOME"] = env["USERPROFILE"] = str(sandbox_dir)
    return env


def env_for_run(sandbox_dir, scenario, tap, sandbox=True,
                poll_interval=POLL_INTERVAL_S):
    """The environment for one daemon run.

    sandbox=False is the real-account pass and is the only time the daemon
    sees the operator's own home directory: the point of that pass is the
    account this machine actually has, which a redirected HOME hides.
    """
    env = _clean_env()
    env["BLINK_SKIP_SERVICE"] = "1"
    env["BLINK_TAP"] = str(tap)
    env["BLINK_POLL_INTERVAL_S"] = str(poll_interval)
    if sandbox:
        _redirect_home(env, sandbox_dir)
    if scenario is not None:
        env["BLINK_SCENARIO"] = str(scenario)
    return env


def grace_s(doc, poll_interval=POLL_INTERVAL_S):
    """How long to keep the daemon alive past the scenario's own duration.

    Three things have to happen after the last step's `at`, and only the
    first of them is a constant:

      - the daemon has to start, connect, apply the frame and print it. That
        is the base allowance;
      - a poll has to come round to notice the step at all. One poll can also
        be missed while the daemon is still opening the port, so two are
        allowed for -- which means a run told to poll slowly waits longer,
        rather than failing for having been told;
      - a board coming out of sleep plays its opening clip before it is back
        on the dashboard, and only then can a frame be applied. Only a
        scenario that stops the daemon has that to do.

    A fixed grace made the longest scenario the first to lose its final frame
    on a slow desk. A flaky scenario inside a release gate is worse than a
    slow one: it teaches people to re-run until green, and that ends the gate.
    """
    waking = GRACE_WAKE_S if doc.get("host_silence_s") else 0.0
    return GRACE_BASE_S + GRACE_POLLS * poll_interval + waking


def sandbox_home(workroot):
    """The one home directory every sandboxed pass in this run shares.

    Per run, not per scenario. A fresh home has no cached release manifest,
    so the daemon offers an update on every hello -- and a home per scenario
    means an Install prompt on the panel at the start of every pass. It
    cannot flash unattended, but it is noise on the one screen the run exists
    to watch, and an invitation to a stray tap on a desk with somebody
    sitting at it.

    Sharing costs nothing that matters: the isolation this provides is from
    the operator's real home, and one directory per run is as isolated as
    five. It is also closer to how the daemon actually runs, which is the
    thing the suite is trying to observe.
    """
    home = Path(workroot) / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def select_scenarios(directory, only=None):
    """The scenario files to run, in name order.

    A name that matches nothing is refused rather than skipped. A typo in
    --only would otherwise produce a green results file that proves less than
    the operator believes it does.

    So is a name this run already uses for a directory of its own, or one that
    is not a plain directory name at all. Every pass empties its working
    directory before it starts (_fresh_work), so a scenario named after the
    run's shared home would delete that home in the middle of the run, and one
    named after another pass would delete its transcript. Refused here, before
    the service is stopped, where it costs nothing -- and by work_name() again
    at the delete itself.
    """
    directory = Path(directory)
    found = {p.stem: p for p in sorted(directory.glob("*.json"))}
    for candidate in found.values():
        work_name(candidate)
    if not only:
        return list(found.values())
    missing = [n for n in only if n not in found]
    if missing:
        raise ValueError(f"There is no scenario named {missing[0]!r} in"
                         f" {directory}. Available: {', '.join(found)}")
    return [found[n] for n in only]


def read_tap(path):
    """The tap as parsed records, tolerating a transcript still being written.

    The daemon appends while it runs and may be killed mid-line, so the last
    line is routinely half a record. A reader that raised on it would turn
    every timing coincidence into a crash report.
    """
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def work_name(path):
    """The name of the directory a scenario's pass owns, from its FILE name.

    Never from the JSON `name`. The pass empties this directory before its
    daemon starts, so whatever builds it decides what shutil.rmtree is handed
    -- and the JSON body is a hand-edited field in a file that --scenarios can
    point at from anywhere. A file of any name carrying "name": "home" would
    empty the run's shared sandbox home in the middle of the run, and
    "name": "../.." would walk out of the work root and delete something that
    has nothing to do with the fleet at all.

    The file's own stem is the value select_scenarios has already vetted, so
    keying the directory off it leaves one source of truth instead of two
    guards that can drift apart -- which is exactly how the JSON name got past
    the first one. It is checked again here rather than assumed, because the
    delete is the thing being protected and run_scenario has callers that
    never went through select_scenarios, and because a file called `...json`
    has the stem `..`: a traversal wearing a filename.

    The JSON `name` keeps its own job. It names the scenario in every sentence
    this run prints, which is what it was always for.
    """
    stem = Path(path).stem
    # ALL DOTS, not just "." and "..". Three reasons it is the right test:
    # `..` is the traversal this guard exists for; `...` and beyond are names
    # Windows refuses to create at all, so a scenario called `....json` died
    # with a PermissionError from inside _fresh_work instead of being refused
    # here with a sentence (Windows desk, 2026-09-06); and pathlib's idea of a
    # stem is not stable -- Python 3.14 stopped treating leading dots as
    # suffix separators, so which of these a given filename produces moves
    # under us. Refusing the whole family is stable under all three.
    if (not stem.strip(".") or any(c in stem for c in "/\\")
            or stem in RESERVED_WORK_NAMES):
        raise ValueError(
            f"{Path(path).name} cannot be a scenario: {stem!r} is not a name"
            f" this run can give a working directory of its own. It has to be"
            f" a plain file name, and not one of the directories the run makes"
            f" for itself ({', '.join(sorted(RESERVED_WORK_NAMES))}) -- each"
            f" of those is emptied before its own pass starts.")
    return stem


def _fresh_work(work):
    """A pass's working directory, with nothing from an earlier run left in it.

    The work root is a fixed path beside --out, `git archive | tar -x` never
    touches it because it is not in the snapshot, and Tap opens the transcript
    with "a". So without this a second run in the same workdir appends to the
    first one's records and then judges the union of both, which is not a
    small error: a daemon that writes nothing at all -- a board unplugged, a
    port still held, a daemon wedged -- reads as a PASS from the second run
    onward, and preflight, whose entire job is catching "no board attached",
    answers from a transcript written last week.

    Per scenario RUN, not per daemon pass: sleep_wake's two passes share one
    transcript on purpose, because the wake has to be readable in the same
    file as the frames either side of it.

    The daemon's own log beside the tap goes with it. A transcript from this
    run next to output from the last one is worse than either alone -- those
    two files side by side are the whole story of a red desk.
    """
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    return work


def _stop_daemon(proc, what="the daemon"):
    """End the child and say so; never raise, never lie about having done it.

    Returns the problems, which is the whole point of returning anything.
    Silently failing to kill this child is the worst outcome the agent has:
    the service is restarted on top of a process that still holds the port,
    so the desk looks healthy, the board stays dark, and the results file
    says nothing at all. Every later verdict is then measured through a port
    somebody else owns.

    It still never raises. This is called from finally blocks, and an
    exception here would replace the problem the run was there to find with
    one about the cleanup.
    """
    for step in (proc.terminate, proc.kill):
        try:
            step()
            proc.wait(timeout=10)
            return []
        except Exception:
            continue
    return [f"Neither terminate nor kill would end {what}, so a daemon this"
            f" run started may still be holding the serial port. Find it and"
            f" end it before trusting anything below, and before the installed"
            f" service is expected to work: the desk will look fine and the"
            f" board will stay dark."]


def _wait_for(tap, ready, timeout, deps):
    """Poll the transcript until it says what we are waiting for.

    Returns the records either way: the ones from a run that timed out are
    what the problem message is written from.
    """
    started = deps.now()
    while True:
        records = read_tap(tap)
        if ready(records):
            return True, records
        if deps.now() - started >= timeout:
            return False, records
        deps.sleep(0.5)


def _heard_from_board(records):
    return any(r.get("dir") == "rx" for r in records)


def preflight(workroot, port, deps, poll_interval=POLL_INTERVAL_S):
    """Wait for the port to actually be free, then say whether it ever was.

    stop_service() has returned by the time this runs, but launchctl bootout
    is asynchronous: the agent is gone and the file descriptor may not be.
    The daemon is started under the run's sandbox home with no scenario, so
    it has nothing to report and does nothing but greet the board -- all this
    needs is one inbound message. It is also the pass that warms that home's
    release manifest, so the scenarios after it are not each met with a
    firmware offer.

    A failure deliberately does not name a culprit. "Still held" and "no
    board attached" look identical from here, and a message that guessed
    would send someone at 2am to the wrong end of the desk.
    """
    home = sandbox_home(workroot)
    taps = _fresh_work(Path(workroot) / "preflight")
    for attempt in range(1, SETTLE_ATTEMPTS + 1):
        tap = taps / f"tap-{attempt}.jsonl"
        env = env_for_run(home, scenario=None, tap=tap, sandbox=True,
                          poll_interval=poll_interval)
        try:
            proc = deps.spawn(daemon_cmd(port), env, REPO_ROOT)
        except Exception as e:
            return False, (f"The daemon could not be started at all: {e}."
                           f" Check that {REPO_ROOT / 'blink_main.py'} runs"
                           f" from this machine's Python.")
        try:
            heard, _ = _wait_for(tap, _heard_from_board, SETTLE_TIMEOUT_S, deps)
        finally:
            orphans = _stop_daemon(proc, "the preflight daemon")
        if orphans:
            # Nothing below can mean anything while another daemon holds the
            # port, so this ends the run rather than joining a list of
            # problems on the way past.
            return False, orphans[0]
        if heard:
            return True, f"The board answered on attempt {attempt}."
    return False, (f"No board message arrived within"
                   f" {SETTLE_TIMEOUT_S:.0f}s on any of"
                   f" {SETTLE_ATTEMPTS} attempts. Either the serial port is"
                   f" still held by the service that was just stopped"
                   f" (launchctl bootout returns before the port is free) or"
                   f" no board is attached to this machine.")


def _scenario_duration(doc):
    duration = tap_asserts.as_number(doc.get("duration_s"))
    if duration is None:
        duration = max([tap_asserts.as_number(s.get("at")) or 0
                        for s in doc.get("steps", [])] or [0]) + 10
    return duration


def _one_pass(name, env, tap, seconds, port, deps, label):
    """Run the daemon for one stretch. Returns (problems, connect_delay).

    connect_delay is how long after the spawn the first record of THIS pass
    was written, which is the only view the agent gets of a daemon that had
    to wait for the port. It is not a verdict; run_scenario decides what to
    do with it.
    """
    problems = []
    before = len(read_tap(tap))
    started = deps.wall()
    proc = deps.spawn(daemon_cmd(port), env, REPO_ROOT)
    try:
        deps.sleep(seconds)
        if proc.poll() is not None:
            problems.append(f"Scenario {name}: the daemon exited on its own"
                            f" during the {label} pass (status {proc.poll()});"
                            f" its output is beside the tap in {tap}.log.")
    finally:
        problems += _stop_daemon(proc, f"{name}'s {label} daemon")

    # Wire records only. The daemon walks a candidate list looking for the
    # board (claude_usage_bridge.py:858-863) and prints as it goes, so on the
    # twelve-port Windows desk the first record of a pass is routinely a
    # foreign device being probed seconds before the real board answers.
    # Measured from that line this would report the time to the first port
    # chatter, and a genuinely late connect would slip past
    # CONNECT_TOLERANCE_S to be blamed on the board -- the one outcome this
    # measurement exists to prevent.
    fresh = read_tap(tap)[before:]
    stamps = [t for t in (tap_asserts.as_number(r.get("t")) for r in fresh
                          if r.get("dir") in ("rx", "tx"))
              if t is not None]
    return problems, (min(stamps) - started if stamps else None)


def run_scenario(path, board, workroot, port, deps,
                 poll_interval=POLL_INTERVAL_S):
    """One scenario end to end: spawn, wait it out, stop, read, judge.

    A scenario with `host_silence_s` runs in TWO passes with the daemon gone
    in between, because that is the only way this board ever sleeps: the
    firmware stamps last_host_ms on any host protocol line (proto.c:262) and
    the daemon answers every ping with a pong, so no poll interval leaves the
    board 30 s silent while a daemon is alive. Both passes append to one
    transcript -- the wake has to be readable in the same file as the frames
    that came before and after it -- and the second daemon replays the
    scenario from its own start, since ScriptedProvider stamps t0 at
    construction.

    The transcript and the rewritten scenario live in a directory of this
    scenario's own; the daemon's home is the run's shared one. Keeping the
    tap out of that home matters twice over: five passes appending to one
    file could not be told apart, and a scenario's own artefacts have no
    business in a directory the daemon treats as somebody's account.

    That directory is emptied first -- see _fresh_work(). The verdict is a
    count over the transcript, and a transcript that survived the last run
    would let this one pass on records its own daemon never wrote. It is named
    after the scenario FILE, never after the `name` inside it: the emptying is
    an rmtree, and the file name is the one of the two that has been vetted
    (work_name()).
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    # The JSON name is what this scenario is CALLED, in every sentence below.
    # The directory it owns is named after the file, which is the value
    # select_scenarios vetted -- see work_name(), and the rmtree it guards.
    name = doc.get("name", Path(path).stem)
    work = _fresh_work(Path(workroot) / work_name(path))
    tap = work / "tap.jsonl"
    # Zero after the clear above, and read anyway: the verdict below is taken
    # from this run's records alone, so a caller that ever hands this function
    # a work directory it did not clear cannot resurrect a transcript.
    before = len(read_tap(tap))
    scenario = prepare_scenario(path, board, work / "scenario")
    env = env_for_run(sandbox_home(workroot), scenario=scenario, tap=tap,
                      sandbox=True, poll_interval=poll_interval)
    grace = grace_s(doc, poll_interval)
    silence = tap_asserts.as_number(doc.get("host_silence_s")) or 0.0

    # The port the previous scenario's daemon had does not come free the
    # instant it exits, and this one is spawned immediately afterwards.
    deps.sleep(PORT_SETTLE_S)
    problems, delay = _one_pass(name, env, tap,
                                _scenario_duration(doc) + grace, port, deps,
                                "first" if silence else "only")

    if silence:
        if problems:
            # The first pass could not be ended cleanly. Waiting out a
            # silence that something is still talking through would produce a
            # board that never slept and a scenario that blamed it.
            problems.append(f"Scenario {name}: the silence was skipped,"
                            f" because a daemon from the first pass may still"
                            f" be running. Nothing here can prove a sleep.")
        else:
            deps.sleep(silence)
            wake = tap_asserts.as_number(doc.get("wake_duration_s")) or \
                _scenario_duration(doc)
            more, wake_delay = _one_pass(name, env, tap, wake + grace, port,
                                         deps, "wake")
            problems += more
            # The wake pass needs the same protection as the first one, and
            # needs it more: min_tx counts on the second daemon replaying
            # every step, so a slow connect there pushes the last of them past
            # the end of a pass that is shorter than the first one, and comes
            # up short -- which is the board being blamed for the port.
            delay = max([d for d in (delay, wake_delay) if d is not None],
                        default=None)

    problems += tap_asserts.check(read_tap(tap)[before:], doc.get("expect", {}),
                                  name)
    outcome = {"ok": not problems, "problems": problems}

    # Reported last and separately: a run whose timeline was shifted has not
    # earned a verdict either way, and calling it a failure would send
    # somebody looking at a board that did nothing wrong.
    if delay is not None and delay > CONNECT_TOLERANCE_S:
        outcome["inconclusive"] = True
        outcome["ok"] = False
        outcome["problems"] = [
            f"Scenario {name}: the daemon's first traffic came {delay:.1f}s"
            f" after it was started, past the {CONNECT_TOLERANCE_S:.0f}s the"
            f" scenario's step spacing allows, so its timeline is shifted by"
            f" that much and its last steps may not have been reached before"
            f" the daemon was stopped. The port was probably still held. This"
            f" run proves nothing either way -- run it again rather than"
            f" reading anything into it."] + outcome["problems"]
    return outcome


def _real_usage_seen(records):
    """A usage frame that left the host carrying a percentage from the account."""
    for r in records:
        msg = r.get("msg")
        if r.get("dir") != "tx" or r.get("sent") is not True:
            continue
        if not isinstance(msg, dict) or msg.get("t") != "usage":
            continue
        session = tap_asserts.as_number(msg.get("session_pct"))
        weekly = tap_asserts.as_number(msg.get("weekly_pct"))
        if (session is not None and session >= 0) or (
                weekly is not None and weekly >= 0):
            return True
    return False


def _real_board_evidence(records):
    """Both halves of the board's answer: it applied a frame, and it is heard.

    Neither half stands in for the other. The '[usage] session ' line is the
    board saying it applied what the host sent; an `rx` record is the daemon's
    read loop parsing a message the board sent back. A board message reaches
    the transcript TWICE on a real desk -- once as raw console bytes while the
    daemon is still probing ports, and again as `rx` once the read loop is
    running -- so a transcript with console chatter and no `rx` proves only
    that bytes are moving on the wire, which is what the probe already proved.
    """
    applied = any(r.get("dir") == "console"
                  and isinstance(r.get("line"), str)
                  and tap_asserts.APPLIED_MARKER in r["line"]
                  for r in records)
    return applied and _heard_from_board(records)


def run_real_account(workroot, port, deps, timeout=REAL_ACCOUNT_TIMEOUT_S,
                     settle=REAL_ACCOUNT_SETTLE_S,
                     poll_interval=POLL_INTERVAL_S):
    """The pass no scenario can stand in for: this machine's own account.

    Everything else in the suite replays an invented timeline, which proves
    the wire and the firmware and says nothing at all about whether this desk
    can read the tools installed on it. So one pass runs with the operator's
    real home and no scenario, and asks for the two things that must be true
    of a working install: a percentage went out and the board took it.

    `blink status --wire` is checked afterwards rather than alongside,
    because it is the command a support conversation starts with and it must
    not be competing with the daemon for the port while it answers.

    The frame going out is the start of the evidence, not the end of it, so
    the daemon is kept alive until the board has answered -- see the settle
    wait below.
    """
    work = _fresh_work(Path(workroot) / REAL_ACCOUNT)
    tap = work / "tap.jsonl"
    env = env_for_run(work, scenario=None, tap=tap, sandbox=False,
                      poll_interval=poll_interval)

    problems = []
    deps.sleep(PORT_SETTLE_S)
    proc = deps.spawn(daemon_cmd(port), env, REPO_ROOT)
    try:
        seen, _ = _wait_for(tap, _real_usage_seen, timeout, deps)
        if not seen:
            problems.append(
                f"Scenario {REAL_ACCOUNT}: no usage frame carrying a real"
                f" percentage went out within {timeout:.0f}s. The daemon is"
                f" running but this machine's tools are reporting nothing"
                f" usable.")
        else:
            # The frame has left the host; the two records this pass is judged
            # on have not arrived. The board still has to apply the frame and
            # print its '[usage] session ' line, and its next ping -- every
            # 10 s -- still has to be read. Stopping the daemon at the tx,
            # which this did, ended the pass before either could happen:
            # measured on the Mac 2026-08-31, the daemon's log ended with a
            # genuine usage frame while the transcript held tx: 3, rx: 0,
            # console: 3, and the run reported a healthy desk as a board that
            # never sent a message and printed no usage lines.
            #
            # An expiry adds no problem here on purpose. check() below reads
            # the same transcript and names the half that is missing, in the
            # same words it uses for every other scenario; a second sentence
            # about the timeout would describe one missing record twice.
            _wait_for(tap, _real_board_evidence, settle, deps)
    finally:
        problems += _stop_daemon(proc, "the real-account daemon")
    problems += tap_asserts.check(read_tap(tap), REAL_ACCOUNT_EXPECT,
                                  REAL_ACCOUNT)
    problems += _check_status_wire(deps)
    return {"ok": not problems, "problems": problems}


def _check_status_wire(deps):
    """`blink status --wire` has to print one parseable JSON line.

    Decoded as UTF-8 rather than the platform's default: the Windows desk has
    a non-ASCII profile name, the wire message carries a transcript path, and
    that path through cp1255 is the UnicodeDecodeError that once left a whole
    machine with no figure on its board.

    Its environment is built the same way as every other child's, through
    _clean_env(). `blink status` reads only BLINK_SKIP_SERVICE today, so an
    inherited variable would change nothing -- but _clean_env()'s own reason
    for existing is that there is no child anywhere in this file that is the
    exception, because the exception is what the next person copies. The home
    is deliberately NOT redirected: this pass is about the account this
    machine actually has, and status must answer for the same one.
    """
    env = _clean_env()
    env["BLINK_SKIP_SERVICE"] = "1"
    cmd = [sys.executable, str(REPO_ROOT / "blink_main.py"), "status", "--wire"]
    try:
        done = deps.runner(cmd, cwd=str(REPO_ROOT), env=env,
                           capture_output=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except Exception as e:
        return [f"Scenario {REAL_ACCOUNT}: `blink status --wire` could not be"
                f" run at all: {e}."]
    for line in (done.stdout or "").splitlines():
        try:
            if isinstance(json.loads(line.strip()), dict):
                return []
        except ValueError:
            continue
    return [f"Scenario {REAL_ACCOUNT}: `blink status --wire` printed no line"
            f" that parses as a JSON object, so nothing can read the message"
            f" this machine would send. It printed:"
            f" {(done.stdout or '').strip()[:200]!r}"]


# --- the customer's own path: install it, then update it ----------------
#
# Every scenario above proves the daemon can drive the board. Neither of the
# two below touches the board at all: they prove that the thing we publish can
# be installed and can replace itself, which is the part of the product a
# customer meets before the board ever lights up, and the part no amount of
# running from a source checkout exercises.

def unpack_cmd(archive, dest):
    """The argv that unpacks a release archive into `dest`.

    The machine's own tool rather than Python's tarfile/zipfile, because this
    scenario is the customer's path and the customer unpacks a download with
    whatever their machine ships. Windows 10 and macOS both ship bsdtar, which
    reads a zip as happily as a tarball. GNU tar does not, so a zip on Linux
    goes to unzip -- a case that only arises when an operator hands a Linux
    desk the Windows archive by mistake, since archive_name() produces a zip
    only for a windows key (pc/update.py:111).
    """
    archive, dest = str(archive), str(dest)
    lowered = archive.lower()
    if lowered.endswith((".tar.gz", ".tgz")):
        return ["tar", "-xzf", archive, "-C", dest]
    if lowered.endswith(".zip"):
        if sys.platform.startswith("linux"):
            return ["unzip", "-q", "-o", archive, "-d", dest]
        return ["tar", "-xf", archive, "-C", dest]
    raise ValueError(f"Cannot unpack {archive}: a release archive is a .tar.gz"
                     f" or a .zip and this is neither. Pass the file the"
                     f" release published, not the one beside it.")


def bundle_bin(bundle_dir):
    """The program inside an unpacked bundle.

    The extension matters: without it Windows will not launch the file, which
    is why the installed copy carries one too (pc/cli.py:51).
    """
    return Path(bundle_dir) / ("blink.exe" if sys.platform == "win32"
                               else "blink")


def installed_bin_under(home):
    """Where `blink install` leaves the program, for a given home.

    Mirrors pc/cli.py's blink_home()/bin_dir()/installed_bin(), which resolve
    it from expanduser("~") on every call -- so a child with HOME and
    USERPROFILE redirected installs here and nowhere near the operator's own
    account. This is also where an update lands: cmd_update passes
    installed_bin() to update.apply() (pc/cli.py:1542), which rotates the
    directory rather than the file (<bin> -> <bin>.old, <bin>.new -> <bin>).
    """
    return bundle_bin(Path(home) / ".blink" / "bin")


def bundle_env(sandbox_dir, ota_dir=None):
    """The environment for running a published release the way a customer does.

    Nothing BLINK_* is inherited -- see _clean_env(), which both builders go
    through -- and this one sets back only two things. BLINK_RELEASE_PUBKEY_FILE
    is why that matters most here: it makes the update verify against a key of
    the caller's choosing (pc/update.py:73-85), and tests/ci/check_update.sh
    exports it by design, so it is a variable somebody working in this
    repository plausibly has set. Inherited, update_path would verify a
    throwaway locally signed manifest and report that it had proved the real
    signed update path -- the exact claim the scenario exists to make.

    BLINK_SKIP_SERVICE goes in the CHILD and only the child. It is what keeps
    `blink install` from registering a login agent on this desk and `blink
    update` from restarting one; the agent's own process must never have it
    (see the module docstring).
    """
    env = _clean_env()
    env["BLINK_SKIP_SERVICE"] = "1"
    _redirect_home(env, sandbox_dir)
    if ota_dir is not None:
        env["BLINK_OTA_DIR"] = str(ota_dir)
    return env


def reported_version(text):
    """The version out of `blink --version` output, or None.

    Parsed to a token and compared whole, never matched as a substring: the
    output is "blink 1.2.4", and asking whether "1.2" appears in it would pass
    a run of 1.2.4 that was meant to prove 1.2 -- and, worse, pass a failed
    update whose old version happens to be a prefix of the new one.
    """
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "blink":
            return parts[1].strip()
        if len(parts) == 1 and parts[0][:1].isdigit():
            return parts[0].strip()
    return None


class _Ran(NamedTuple):
    """What a command did, with its output decoded to text exactly once."""
    returncode: int
    stdout: str
    stderr: str


def _decode_console(raw):
    """Text out of a program's output, whatever it wrote it in.

    UTF-8 first, then the machine's own preferred encoding, then UTF-8 with
    replacements so that something always comes back. Nothing here may raise:
    this is the diagnostic in a failure message, and a decoder that threw
    would replace the problem the run found with one about reading it.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    for codec in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run_program(cmd, env, runner, name, what, ours=True):
    """Run one command. Returns (_Ran or None, problems).

    `ours=False` is for a program that is not one of ours -- which here means
    tar and unzip. Our own children are told PYTHONIOENCODING=utf-8, so
    naming that encoding to subprocess reads them back correctly. tar.exe is
    not a Python program and has never heard of that variable: on Windows it
    writes in the machine's own code page, and the message it writes contains
    the path it failed on -- which on the desk that matters contains a
    non-ASCII profile name. Forced through UTF-8, the one diagnostic a failed
    unpack has comes back as a row of replacement characters. So its bytes
    are captured and decoded here instead, where there is somewhere to fall
    back to.
    """
    kwargs = {"env": env, "capture_output": True, "timeout": BUNDLE_TIMEOUT_S}
    if ours:
        kwargs.update(encoding="utf-8", errors="replace")
    try:
        done = runner([str(c) for c in cmd], **kwargs)
    except Exception as e:
        return None, [f"Scenario {name}: {what} could not be run at all: {e}."
                      f" The command was {' '.join(str(c) for c in cmd)}."]
    ran = _Ran(done.returncode, _decode_console(done.stdout),
               _decode_console(done.stderr))
    if ran.returncode != 0:
        return ran, [f"Scenario {name}: {what} exited {ran.returncode}."
                     f" It printed: {_tail(ran.stdout)}"
                     f"{_tail(ran.stderr, ' Errors: ')}"]
    return ran, []


def _tail(text, prefix=""):
    text = (text or "").strip()
    return f"{prefix}{text[-400:]!r}" if text else ""


def _version_of(binary, env, runner, name, what):
    """What a program says it is. Returns (version or None, problems)."""
    done, problems = _run_program([binary, "--version"], env, runner, name,
                                  f"{what} at {binary}")
    if problems:
        return None, problems
    version = reported_version(done.stdout)
    if version is None:
        return None, [f"Scenario {name}: {what} at {binary} ran but printed no"
                      f" version. It printed: {_tail(done.stdout)}"]
    return version, []


def unpack_bundle(archive, dest, runner=subprocess.run, name=FRESH_INSTALL,
                  home=None):
    """Unpack a release archive and hand back the directory holding the program.

    The archive carries one top-level `blink/` directory so that a person who
    unpacks it by hand gets a folder rather than a spill of files
    (pc/update.py:256), so the program is normally a level down -- but the
    directory itself is checked too, rather than assuming a layout, and an
    archive that yielded no program at all is reported as that instead of as a
    version check against a file that is not there.

    The unpacker gets a redirected home like every other child. tar writes
    where -C tells it to and would not read one, so this buys no safety by
    itself -- it keeps an invariant whole. "Both variables, always" is worth
    having only if there is no child anywhere in this file that is the
    exception, because the exception is what the next person copies.
    """
    archive, dest = Path(archive), Path(dest)
    if not archive.exists():
        return None, [f"Scenario {name}: there is no archive at {archive}."
                      f" Download the release's own file and pass that path."]
    try:
        cmd = unpack_cmd(archive, dest)
    except ValueError as e:
        return None, [f"Scenario {name}: {e}"]
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, [f"Scenario {name}: cannot create {dest}: {e}."]
    _, problems = _run_program(cmd, bundle_env(home or dest), runner, name,
                               f"unpacking {archive.name} with {cmd[0]}",
                               ours=False)
    if problems:
        return None, problems
    for candidate in (dest / "blink", dest):
        if bundle_bin(candidate).exists():
            return candidate, []
    return None, [f"Scenario {name}: {archive} unpacked into {dest} but there"
                  f" is no {bundle_bin(dest).name} program in it. Either the"
                  f" archive is not a BLINK release or it was built for"
                  f" another platform."]


def check_feed_dir(ota_dir, name=UPDATE_PATH):
    """Say whether the local feed can serve an update, before one is attempted.

    BLINK_OTA_DIR makes ota._get read <dir>/<basename of the url>
    (pc/ota.py:87-93), and fetch_signed_manifest goes through it
    (pc/update.py:183), so three files have to be present or the update stops
    at a message the scenario would then have to reverse-engineer.

    It cannot check that the signature verifies -- that is the update's job,
    and deliberately so: the manifest is verified against the public key
    frozen into the shipped binaries (pc/update.py:196), which is exactly why
    a hand-written manifest cannot drive this scenario. The directory has to
    come from a genuinely signed release, draft or published. What this
    catches is the far more common mistake of pointing it at a directory that
    has the archive but not the .sig, where `blink update` refuses the feed
    and the run reads as a broken update path.
    """
    ota_dir = Path(ota_dir)
    if not ota_dir.is_dir():
        return [f"Scenario {name}: there is no feed directory at {ota_dir}."
                f" It has to hold the candidate release's manifest.json,"
                f" manifest.json.sig and platform archive."]
    key = platform_key()
    if key is None:
        return [f"Scenario {name}: there is no published BLINK build for this"
                f" machine ({platform.system()} {platform.machine()}), so no"
                f" feed could serve it one."]
    missing = [leaf for leaf in ("manifest.json", "manifest.json.sig",
                                 archive_name(key))
               if not (ota_dir / leaf).exists()]
    if not missing:
        return []
    return [f"Scenario {name}: the feed at {ota_dir} is missing"
            f" {', '.join(missing)}. The update path verifies the manifest's"
            f" signature against the key frozen into the binary, so all three"
            f" files have to be the ones a real release published -- download"
            f" them from the release (a draft is fine), do not write them."]


def run_install_check(bundle_dir, expect_version, runner=subprocess.run,
                      sandbox=None, name=FRESH_INSTALL):
    """Install an unpacked release into a sandbox home and prove what landed.

    Three questions in the order a customer meets them: is this the build we
    think it is, does `blink install` finish, and does the copy it left under
    ~/.blink/bin run and report the same version.

    The third is the reason this scenario exists. The first two are answered
    by the archive we already have in our hands, and an installer that prints
    its way to a cheerful ending while leaving a program that will not start
    is not a hypothetical here: `blink install` stages a copy and hands it to
    update.swap_in(), which self-tests it (pc/cli.py:1112) -- so this is the
    fleet's independent check on the machinery that decides whether a
    customer's login service has anything to run at all.
    """
    bundle_dir = Path(bundle_dir)
    sandbox = Path(sandbox or bundle_dir.parent / "home")
    sandbox.mkdir(parents=True, exist_ok=True)
    env = bundle_env(sandbox)
    exe = bundle_bin(bundle_dir)

    got, problems = _version_of(exe, env, runner, name, "the unpacked program")
    if got is not None and got != expect_version:
        problems.append(
            f"Scenario {name}: the unpacked program reports {got}, not the"
            f" {expect_version} this run was told to expect. Either --bundle"
            f" points at the wrong archive or --expect-version is wrong;"
            f" nothing was installed.")
    if problems:
        return {"ok": False, "problems": problems}

    _, problems = _run_program([exe, "install"], env, runner, name,
                               "`blink install`")
    if problems:
        return {"ok": False, "problems": problems}

    installed = installed_bin_under(sandbox)
    got, problems = _version_of(installed, env, runner, name,
                                "the installed program")
    if not problems and got != expect_version:
        problems.append(
            f"Scenario {name}: `blink install` finished, but the program it"
            f" left at {installed} reports {got} rather than"
            f" {expect_version}. The install did not put this bundle in"
            f" place.")
    return {"ok": not problems, "problems": problems}


def run_update_check(prev_dir, ota_dir, expect_version, runner=subprocess.run,
                     sandbox=None, name=UPDATE_PATH):
    """Install the previous release, update it off the feed, prove the result.

    The previous release is installed first, and it is the INSTALLED copy
    that runs the update -- not the unpacked bundle. Both halves of that
    matter, and for the same reason: what can go wrong here is the rotation,
    <bin> to <bin>.old and <bin>.new to <bin> (pc/update.py:322-360), of a
    directory the running program is inside of. Updating from the unpacked
    bundle would rename a directory nothing is running from, and updating
    into an empty ~/.blink/bin would prove the download and skip the rename.
    Either shortcut leaves the Windows desk -- the one where a locked file is
    a real possibility -- untested by the scenario named after it.

    Two ways this could pass while proving nothing, both closed here:

      - `blink update` exits 0 for "Already up to date." So a previous bundle
        that already reports the candidate version is refused before anything
        is run, and the verdict is taken from what the installed program
        reports afterwards, not from an exit status.
      - a feed that does not verify is REFUSED, by design: the manifest is
        checked against the key frozen into the binary (pc/update.py:196), so
        a fabricated one cannot drive this. That path exits 1 and is reported
        with what the command printed, so a red run says "not properly
        signed" rather than leaving somebody to guess.
    """
    prev_dir = Path(prev_dir)
    sandbox = Path(sandbox or prev_dir.parent / "home")
    sandbox.mkdir(parents=True, exist_ok=True)
    env = bundle_env(sandbox)
    exe = bundle_bin(prev_dir)

    previous, problems = _version_of(exe, env, runner, name,
                                     "the previous release")
    if problems:
        return {"ok": False, "problems": problems}
    if previous == expect_version:
        return {"ok": False, "problems": [
            f"Scenario {name}: the previous release reports {previous}, the"
            f" same version this run expects to end on, so `blink update`"
            f" would answer \"Already up to date.\" and this scenario would"
            f" pass without updating anything. Point --prev-bundle at the"
            f" release before {expect_version}."]}

    _, problems = _run_program([exe, "install"], env, runner, name,
                               "`blink install` of the previous release")
    if problems:
        return {"ok": False, "problems": problems}
    installed = installed_bin_under(sandbox)
    got, problems = _version_of(installed, env, runner, name,
                                "the previous release, once installed")
    if not problems and got != previous:
        problems.append(
            f"Scenario {name}: the previous release was installed but the"
            f" program at {installed} reports {got} rather than {previous}."
            f" There is nothing here to update from.")
    if problems:
        return {"ok": False, "problems": problems}

    # The INSTALLED copy runs the update, not the unpacked bundle. That is
    # what a customer does, and it is the only arrangement that exercises the
    # risk: update.apply renames the directory the running executable is
    # inside of (pc/update.py:322-360). Run from the bundle, the rename is of
    # a directory nothing is running from -- which cannot fail the way
    # Windows fails, and so proves nothing about the desk most likely to.
    _, problems = _run_program([installed, "update"],
                               bundle_env(sandbox, ota_dir),
                               runner, name, "`blink update`")
    if problems:
        return {"ok": False, "problems": problems}

    got, problems = _version_of(installed, env, runner, name,
                                "the updated program")
    if not problems and got != expect_version:
        problems.append(
            f"Scenario {name}: `blink update` finished without complaining,"
            f" but the program at {installed} still reports {got} rather than"
            f" {expect_version}. The feed was read and nothing newer was"
            f" taken from it: check that its manifest names a daemon version"
            f" above {previous} and an artifact for"
            f" {platform_key() or 'this platform'}.")
    return {"ok": not problems, "problems": problems}


def _clear(work):
    """Empty a scenario's working directory before it is used again.

    The work root is a fixed path beside --out and nothing else ever removes
    it, so without this the sandbox home outlives the run that made it and
    the next run's version check reads a program THIS run's installer never
    wrote. An installer that exits 0 having copied nothing is not a
    hypothetical -- that is what `blink install` does when handed an unfrozen
    build (pc/cli.py:1123) -- and the leftovers would prove it correct,
    hiding exactly the packaging fault these scenarios exist to catch.

    It also restores the name: after the first run, every fresh_install would
    otherwise be an install over an existing one.
    """
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    (work / "home").mkdir(parents=True, exist_ok=True)
    return work


def run_fresh_install(archive, workroot, deps, expect_version):
    """The fresh_install scenario, from the archive a release published."""
    work = _clear(Path(workroot) / FRESH_INSTALL)
    bundle, problems = unpack_bundle(archive, work / "unpacked", deps.runner,
                                     FRESH_INSTALL, home=work / "home")
    if problems:
        return {"ok": False, "problems": problems}
    return run_install_check(bundle, expect_version, runner=deps.runner,
                             sandbox=work / "home")


def run_update_path(archive, ota_dir, workroot, deps, expect_version):
    """The update_path scenario: the previous release, brought up to date."""
    work = _clear(Path(workroot) / UPDATE_PATH)
    problems = check_feed_dir(ota_dir)
    if problems:
        return {"ok": False, "problems": problems}
    bundle, problems = unpack_bundle(archive, work / "unpacked", deps.runner,
                                     UPDATE_PATH, home=work / "home")
    if problems:
        return {"ok": False, "problems": problems}
    return run_update_check(bundle, ota_dir, expect_version,
                            runner=deps.runner, sandbox=work / "home")


def customer_path(args, workroot, deps):
    """The scenarios that need no board, as {name: outcome}.

    Each gets a home of its own, and neither of them gets the one the daemon
    scenarios share. `blink install` writes a program into ~/.blink/bin and
    hooks into ~/.claude/settings.json, and sandbox_home() is deliberately one
    directory for the whole run -- an installed program appearing in it
    halfway through would change what the passes after it are running against,
    for no gain, since the isolation that matters is from the operator's own
    account and is already had.
    """
    out = {}
    if args.bundle:
        out[FRESH_INSTALL] = run_fresh_install(args.bundle, workroot, deps,
                                               args.expect_version)
    if args.prev_bundle:
        out[UPDATE_PATH] = run_update_path(args.prev_bundle, args.ota_dir,
                                           workroot, deps, args.expect_version)
    return out


def run(args, deps=None):
    """The whole pass on this machine, as the results document.

    Beyond the four keys the fleet aggregator reads (host, board, scenarios,
    ok) there is a top-level `problems`, which holds everything that went
    wrong around the scenarios rather than inside one: a service that would
    not stop, a port that never came free. Those are the failures that make
    every scenario fail identically, and a results file that only had
    per-scenario problems would repeat the same sentence four times without
    ever saying what actually happened.
    """
    deps = deps or Deps()
    result = {"host": platform.node() or "unknown", "board": args.board,
              "scenarios": {}, "problems": [], "ok": False}
    workroot = Path(args.out).resolve().parent / "fleet-work"
    workroot.mkdir(parents=True, exist_ok=True)

    # Before the service is touched, not after: a mistyped --only must not
    # cost the operator their running daemon on the way to an error message.
    try:
        paths = select_scenarios(args.scenarios, args.only)
    except ValueError as e:
        result["problems"].append(f"{e} Nothing was run and the installed"
                                  f" service was left alone.")
        return _finish(result)

    # Before the service is touched, and before a board is asked for. Neither
    # of these opens the serial port -- both run into a sandbox home with
    # BLINK_SKIP_SERVICE set -- so neither is a reason to take somebody's
    # daemon away, and a desk whose board is unplugged still returns a verdict
    # on the half of the product a customer meets first.
    for scenario, outcome in customer_path(args, workroot, deps).items():
        result["scenarios"][scenario] = outcome
        print(f"[fleet] {scenario}: {'ok' if outcome['ok'] else 'FAILED'}")
        for problem in outcome["problems"]:
            print(f"        {problem}")

    stopped = deps.stop()
    print(f"[fleet] stop service: {stopped}")
    if stopped.skipped:
        # Not a warning to run past: nothing was stopped, the real daemon
        # still holds the port, and every scenario below would fail as though
        # the board were broken.
        result["problems"].append(
            "BLINK_SKIP_SERVICE is set in this shell, so the installed"
            " service was never stopped and it still holds the serial port."
            " Nothing was run. Unset it and start again.")
        return _finish(result)
    if not stopped.ok:
        result["problems"].append(
            f"The installed service would not stop, so the serial port is"
            f" probably still held: {stopped}. Nothing was run.")
        _restart_service(result, deps)
        return _finish(result)

    try:
        ready, detail = preflight(workroot, args.port, deps,
                                  args.poll_interval)
        print(f"[fleet] preflight: {detail}")
        # Kept even when it succeeded: "the board answered on attempt 3" is
        # how a reader learns the port took ten seconds to come free on this
        # machine, which is the difference between a flaky desk and a slow one.
        result["preflight"] = detail
        if not ready:
            # Named against every scenario as well as at the top level: the
            # aggregator reads per-scenario verdicts, and a machine whose
            # board never spoke has to show as four failures rather than as
            # four missing entries that could be read as four passes.
            result["problems"].append(detail)
            for path in paths:
                result["scenarios"][path.stem] = {"ok": False,
                                                  "problems": [detail]}
        else:
            for path in paths:
                outcome = run_scenario(path, args.board, workroot, args.port,
                                       deps, args.poll_interval)
                result["scenarios"][path.stem] = outcome
                print(f"[fleet] {path.stem}:"
                      f" {'ok' if outcome['ok'] else 'FAILED'}")
                for problem in outcome["problems"]:
                    print(f"        {problem}")
            if args.real_account:
                # By name: this call grew a settle timeout between the two it
                # already passed, and positionally that handed the poll
                # interval to the wrong parameter without changing a result.
                outcome = run_real_account(workroot, args.port, deps,
                                           timeout=args.real_timeout,
                                           poll_interval=args.poll_interval)
                result["scenarios"][REAL_ACCOUNT] = outcome
                # Printed like every other pass. It is the only one that says
                # whether this desk can read its own tools, and it was the
                # only one whose verdict never reached the operator watching
                # the run go by.
                print(f"[fleet] {REAL_ACCOUNT}:"
                      f" {'ok' if outcome['ok'] else 'FAILED'}")
                for problem in outcome["problems"]:
                    print(f"        {problem}")
    except Exception as e:
        result["problems"].append(
            f"The run stopped early on an unexpected error: {e!r}. The"
            f" transcripts it did write are under {workroot}.")
    finally:
        _restart_service(result, deps)
    return _finish(result)


def _restart_service(result, deps):
    """Give the port back, and say so in the result when that did not work.

    One function because there are two paths that restart, and the one that
    used to throw its Outcome away -- the early return after a stop that
    failed -- is the path most likely to be looking at a machine whose
    service is in a state nobody asked for. A desk left with no daemon and no
    sentence saying so is the failure this whole file is arranged to avoid.
    """
    started = deps.start()
    print(f"[fleet] start service: {started}")
    if not started.ok:
        result["problems"].append(
            f"The installed service was not started again: {started}."
            f" This machine's board will stay dark until it is.")


def _finish(result):
    result["ok"] = (not result["problems"]
                    and bool(result["scenarios"])
                    and all(s["ok"] for s in result["scenarios"].values()))
    return result


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="tests.fleet.agent",
        description="Run the fleet scenarios against this machine's board.")
    ap.add_argument("--board", choices=("claude", "codex"), default="claude",
                    help="Which provider this desk's board is showing")
    ap.add_argument("--port", default="auto",
                    help="Serial port, or auto to let the daemon find it")
    ap.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS),
                    help="Directory of scenario files")
    ap.add_argument("--out", default="results.json",
                    help="Where to write this machine's results")
    ap.add_argument("--only", default=None,
                    help="Comma-separated scenario names, instead of all")
    ap.add_argument("--real-account", action="store_true",
                    help="Also run one pass against this machine's own account")
    ap.add_argument("--real-timeout", type=float,
                    default=REAL_ACCOUNT_TIMEOUT_S,
                    help="Seconds to wait for a real reading")
    ap.add_argument("--bundle", default=None,
                    help="Release archive for this platform, installed fresh"
                         " into a sandbox home as the fresh_install scenario")
    ap.add_argument("--prev-bundle", default=None,
                    help="The PREVIOUS release's archive, installed and then"
                         " brought up to date as the update_path scenario")
    ap.add_argument("--ota-dir", default=None,
                    help="Directory holding the candidate release's"
                         " manifest.json, manifest.json.sig and archive,"
                         " served to the update as BLINK_OTA_DIR")
    ap.add_argument("--expect-version", default=None,
                    help="The version both customer-path scenarios must end"
                         " up reporting")
    ap.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_S,
                    help="Seconds between the daemon's usage polls. A slower"
                         " poll lengthens the wait after each scenario to"
                         " match, so raising it cannot fail a run by itself")
    args = ap.parse_args(argv)
    args.only = [n.strip() for n in args.only.split(",")] if args.only else None
    # Refused here rather than reported as a failed scenario. A customer-path
    # scenario with nothing to compare against would run a real installer and
    # then have no verdict to give, and the version is the entire claim.
    if (args.bundle or args.prev_bundle) and not args.expect_version:
        ap.error("Both --bundle and --prev-bundle need --expect-version:"
                 " the scenario's whole claim is that the program ends up"
                 " reporting a particular version.")
    if args.prev_bundle and not args.ota_dir:
        ap.error("The --prev-bundle scenario needs --ota-dir too, a"
                 " directory holding the"
                 " candidate release's manifest.json, manifest.json.sig and"
                 " platform archive. The update verifies that signature"
                 " against the key frozen into the binary, so the files have"
                 " to come from a real release -- a draft is fine.")
    return args


def main(argv=None, deps=None):
    args = parse_args(argv)
    result = run(args, deps)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for problem in result["problems"]:
        print(f"[fleet] {problem}")
    print(f"[fleet] {'PASS' if result['ok'] else 'FAIL'}: {out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
