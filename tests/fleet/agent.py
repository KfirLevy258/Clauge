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

Three decisions worth stating, because each of them is a bug that has already
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
    it could not tell apart rather than picking one.

Port selection is left to the daemon, which finds the board by USB VID:PID
(claude_usage_bridge.py:41-48) -- that is how it picks COM15 out of the
Windows desk's twelve mostly-Bluetooth ports. --port is forwarded only when
the operator names one.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

from pc.service_ctl import start_service, stop_service
from tests.fleet import tap_asserts

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIOS = REPO_ROOT / "tests" / "fleet" / "scenarios"

# A scenario is a timeline and the daemon emits one usage frame per poll, so
# at the shipped 60 s a thirty-second scenario would produce a single frame
# and the sequence under test would never reach the board.
POLL_INTERVAL_S = 3.0

# Time past the scenario's own duration before the daemon is stopped: the
# last step still has to be polled, sent, applied and printed.
GRACE_S = 8.0

# The handshake that absorbs an asynchronous bootout.
SETTLE_TIMEOUT_S = 15.0
SETTLE_ATTEMPTS = 3

# The real-account pass waits for whatever this machine's tools happen to
# report, which nothing here controls the timing of.
REAL_ACCOUNT_TIMEOUT_S = 120.0
REAL_ACCOUNT = "real_account"

# What the real-account pass demands: the same shape as a scenario's block,
# reduced to the only two things true of every desk -- one frame out, one
# frame applied. Percentages come from a live account, so no count of stale
# lines and no sleep window can be asserted.
REAL_ACCOUNT_EXPECT = {"min_tx": 1, "min_board_usage": 1,
                       "min_stale_lines": 0, "quiet_window_s": 0}


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


def env_for_run(sandbox_dir, scenario, tap, sandbox=True,
                poll_interval=POLL_INTERVAL_S):
    """The environment for one daemon run.

    sandbox=False is the real-account pass and is the only time the daemon
    sees the operator's own home directory: the point of that pass is the
    account this machine actually has, which a redirected HOME hides.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["BLINK_SKIP_SERVICE"] = "1"
    env["BLINK_TAP"] = str(tap)
    env["BLINK_POLL_INTERVAL_S"] = str(poll_interval)
    env.pop("BLINK_SCENARIO", None)
    if sandbox:
        env["HOME"] = env["USERPROFILE"] = str(sandbox_dir)
    if scenario is not None:
        env["BLINK_SCENARIO"] = str(scenario)
    return env


def select_scenarios(directory, only=None):
    """The scenario files to run, in name order.

    A name that matches nothing is refused rather than skipped. A typo in
    --only would otherwise produce a green results file that proves less than
    the operator believes it does.
    """
    directory = Path(directory)
    found = {p.stem: p for p in sorted(directory.glob("*.json"))}
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


def _stop_daemon(proc):
    """End the child, and never raise doing it.

    Called on the way out of every pass, including the failing ones. An
    exception here would replace the problem the run was there to find with
    one about the cleanup.
    """
    for step in (proc.terminate, proc.kill):
        try:
            step()
            proc.wait(timeout=10)
            return
        except Exception:
            continue


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


def preflight(workroot, port, deps):
    """Wait for the port to actually be free, then say whether it ever was.

    stop_service() has returned by the time this runs, but launchctl bootout
    is asynchronous: the agent is gone and the file descriptor may not be.
    The daemon is started under an empty sandbox home with no scenario, so it
    has nothing to report and does nothing but greet the board -- all this
    needs is one inbound message.

    A failure deliberately does not name a culprit. "Still held" and "no
    board attached" look identical from here, and a message that guessed
    would send someone at 2am to the wrong end of the desk.
    """
    for attempt in range(1, SETTLE_ATTEMPTS + 1):
        home = Path(workroot) / f"preflight-{attempt}"
        home.mkdir(parents=True, exist_ok=True)
        tap = home / "tap.jsonl"
        env = env_for_run(home, scenario=None, tap=tap, sandbox=True)
        try:
            proc = deps.spawn(daemon_cmd(port), env, REPO_ROOT)
        except Exception as e:
            return False, (f"The daemon could not be started at all: {e}."
                           f" Check that {REPO_ROOT / 'blink_main.py'} runs"
                           f" from this machine's Python.")
        try:
            heard, _ = _wait_for(tap, _heard_from_board, SETTLE_TIMEOUT_S, deps)
        finally:
            _stop_daemon(proc)
        if heard:
            return True, f"The board answered on attempt {attempt}."
    return False, (f"No board message arrived within"
                   f" {SETTLE_TIMEOUT_S:.0f}s on any of"
                   f" {SETTLE_ATTEMPTS} attempts. Either the serial port is"
                   f" still held by the service that was just stopped"
                   f" (launchctl bootout returns before the port is free) or"
                   f" no board is attached to this machine.")


def run_scenario(path, board, workroot, port, deps):
    """One scenario end to end: spawn, wait it out, stop, read, judge."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    name = doc.get("name", Path(path).stem)
    home = Path(workroot) / name
    home.mkdir(parents=True, exist_ok=True)
    tap = home / "tap.jsonl"
    scenario = prepare_scenario(path, board, home / "scenario")
    env = env_for_run(home, scenario=scenario, tap=tap, sandbox=True)

    problems = []
    proc = deps.spawn(daemon_cmd(port), env, REPO_ROOT)
    try:
        duration = tap_asserts.as_number(doc.get("duration_s"))
        if duration is None:
            duration = max([tap_asserts.as_number(s.get("at")) or 0
                            for s in doc.get("steps", [])] or [0]) + 10
        deps.sleep(duration + GRACE_S)
        if proc.poll() is not None:
            problems.append(f"Scenario {name}: the daemon exited on its own"
                            f" before the scenario was over (status"
                            f" {proc.poll()}); its output is beside the tap in"
                            f" {tap}.log.")
    finally:
        _stop_daemon(proc)
    problems += tap_asserts.check(read_tap(tap), doc.get("expect", {}), name)
    return {"ok": not problems, "problems": problems}


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


def run_real_account(workroot, port, deps, timeout=REAL_ACCOUNT_TIMEOUT_S):
    """The pass no scenario can stand in for: this machine's own account.

    Everything else in the suite replays an invented timeline, which proves
    the wire and the firmware and says nothing at all about whether this desk
    can read the tools installed on it. So one pass runs with the operator's
    real home and no scenario, and asks for the two things that must be true
    of a working install: a percentage went out and the board took it.

    `blink status --wire` is checked afterwards rather than alongside,
    because it is the command a support conversation starts with and it must
    not be competing with the daemon for the port while it answers.
    """
    home = Path(workroot) / REAL_ACCOUNT
    home.mkdir(parents=True, exist_ok=True)
    tap = home / "tap.jsonl"
    env = env_for_run(home, scenario=None, tap=tap, sandbox=False)

    problems = []
    proc = deps.spawn(daemon_cmd(port), env, REPO_ROOT)
    try:
        seen, _ = _wait_for(tap, _real_usage_seen, timeout, deps)
        if not seen:
            problems.append(
                f"Scenario {REAL_ACCOUNT}: no usage frame carrying a real"
                f" percentage went out within {timeout:.0f}s. The daemon is"
                f" running but this machine's tools are reporting nothing"
                f" usable.")
    finally:
        _stop_daemon(proc)
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
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
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
        deps.start()
        return _finish(result)

    try:
        ready, detail = preflight(workroot, args.port, deps)
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
                                       deps)
                result["scenarios"][path.stem] = outcome
                print(f"[fleet] {path.stem}:"
                      f" {'ok' if outcome['ok'] else 'FAILED'}")
                for problem in outcome["problems"]:
                    print(f"        {problem}")
            if args.real_account:
                outcome = run_real_account(workroot, args.port, deps,
                                           args.real_timeout)
                result["scenarios"][REAL_ACCOUNT] = outcome
    except Exception as e:
        result["problems"].append(
            f"The run stopped early on an unexpected error: {e!r}. The"
            f" transcripts it did write are under {workroot}.")
    finally:
        started = deps.start()
        print(f"[fleet] start service: {started}")
        if not started.ok:
            result["problems"].append(
                f"The installed service was not started again: {started}."
                f" This machine's board will stay dark until it is.")
    return _finish(result)


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
    args = ap.parse_args(argv)
    args.only = [n.strip() for n in args.only.split(",")] if args.only else None
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
