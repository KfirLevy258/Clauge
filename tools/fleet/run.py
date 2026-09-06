"""Run the fleet agent on all three desks from one of them, and add it up.

    python tools/fleet/run.py                       # the whole fleet
    python tools/fleet/run.py --only galit-win10    # one desk
    python tools/fleet/run.py --scenarios overage   # one scenario, everywhere

Each desk gets a snapshot of THIS checkout at HEAD (`git archive` piped into
`tar -x` over ssh), runs tests/fleet/agent.py against its own board, and hands
back a result.json. The three run at the same time -- one board per desk, no
shared state -- and the verdicts land in .fleet/last_run.json, which the
release gate reads.

The inventory is fleet.toml next to this file. It names an interpreter per
desk rather than assuming `python3`, because the agent spawns the daemon and
the daemon imports serial at module scope: on this Mac only .venv-test has
pyserial, and a system python would fail as a board fault.

Everything here is written around one question -- can this print PASS when it
did not actually prove anything? -- and the four answers it has to keep shut:

  - A desk that never ran must never read as a desk that passed. An
    unreachable machine, a missing interpreter, a hung ssh and a truncated
    result all return {"ok": False, "problems": [...]}; run_host raises
    nothing at all. A host that produced no result is still a key in the
    output, holding a sentence, because a missing key is invisible and an
    honest failure is not.

  - The workdir survives between runs, so result.json from LAST week is
    sitting on the remote before this run starts. If the agent dies before
    writing, pulling that file back would report last week's green against
    today's commit. It is deleted on the remote, in the same command that
    unpacks the snapshot, before the agent is started.

  - .fleet/last_run.json is removed before the inventory is even read, and
    written atomically at the end from a finally. A run that dies halfway --
    or that gives up on a typo in --only -- leaves no file rather than the
    previous green one, and the gate cannot read a half-written one. The
    report is printed from a try of its own, because a fault in the table
    must not be the last word over a verdict already on disk.

  - An ssh that hangs -- a desk asleep, a key that needs a passphrase, a
    machine mid-reboot -- would otherwise hold the gate open forever. Every
    call has a timeout, and BatchMode=yes turns a password prompt into an
    immediate error instead of a wait.

On the Windows desk the remote shell is cmd.exe, which costs three things.
%USERPROFILE% there is non-ASCII, so it is never expanded here: the command
line says %USERPROFILE% and cmd expands it, and no Hebrew ever crosses an ssh
command line whose encoding nothing in the chain agrees on. Windows sshd runs
the command as `cmd.exe /c <line>`, and cmd strips the outer quotes of a line
that BEGINS with one, so every line built here begins with a bare verb (cd,
mkdir). And %errorlevel% inside a one-liner is expanded when the line is
parsed, not when it runs, so it always reads 0: the exit code that is
believed is the one ssh itself returns, which is the last command's.
"""
import argparse
import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = Path(__file__).resolve().parent / "fleet.toml"
DEFAULT_STATE = REPO_ROOT / ".fleet" / "last_run.json"

# The agent writes here, relative to the workdir it is started in. Relative on
# purpose: an absolute path would have to carry the Windows desk's non-ASCII
# profile name through the ssh command line.
RESULT_NAME = "result.json"

REQUIRED_KEYS = ("ssh", "os", "board", "python", "workdir",
                 "has_claude_desktop")
KNOWN_OS = ("darwin", "linux", "windows")

# BatchMode turns "please enter a passphrase" into an error rather than a
# machine sitting at a prompt until somebody notices the release stalled.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]

PUSH_TIMEOUT_S = 600.0
PULL_TIMEOUT_S = 300.0
GIT_TIMEOUT_S = 60.0
# Five scenarios with their silences, plus two customer-path scenarios that
# unpack 50 MB and run an installer that self-tests the copy it made.
#
# It has to sit OUTSIDE the agent's own worst case, not on top of it. The
# customer path runs first and allows BUNDLE_TIMEOUT_S per program call for
# BUNDLE_CALLS calls (tests/fleet/agent.py) -- 4200 s -- which used to be
# exactly this constant: a slow update_path could then eat the entire desk
# budget, the four board scenarios would never run, and this end would report
# a timeout instead of a result. The two are pinned against each other by a
# test. What is left over here is ~50 minutes for the board passes, which
# take five to eight. Generous on purpose: a timeout is reported as a failed
# release, and slow is not broken.
AGENT_TIMEOUT_S = 7200.0

# How much of a failing command's own output is worth repeating. Enough for
# the sentence that names the cause, short enough that the table stays a
# table.
TAIL_CHARS = 600


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------

def load_inventory(path=DEFAULT_INVENTORY):
    """Read fleet.toml. Every complaint names the host and the missing key.

    A malformed inventory is refused here rather than halfway through a run:
    the alternative is stopping two desks' services, discovering the third
    has no `python` line, and leaving somebody's board dark over a typo.

    Two entries naming the same machine are refused for the same reason and
    then some. main() submits every desk to the thread pool at once, so a
    duplicated `ssh` -- or two entries with none, which both mean THIS machine
    -- puts two agents on one desk: both stopping and starting the same login
    service, both unpacking into the same workdir, and both asking the daemon
    for the one serial port. The second one's failures then read as a board
    fault on a board that was busy answering the first.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"The fleet inventory could not be read: {e}."
                         f" It should be a TOML file at {path}.") from e
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"The fleet inventory at {path} is not valid"
                         f" TOML: {e}") from e
    hosts = doc.get("hosts") or {}
    if not hosts:
        raise ValueError(f"The fleet inventory at {path} names no hosts."
                         f" It needs a [hosts.<name>] table per desk.")
    machines = {}
    for name, cfg in hosts.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Host {name} in {path} is not a table.")
        missing = [key for key in REQUIRED_KEYS if key not in cfg]
        if missing:
            raise ValueError(
                f"Host {name} in {path} is missing"
                f" {', '.join(missing)}. Every desk needs all of"
                f" {', '.join(REQUIRED_KEYS)}.")
        if cfg["os"] not in KNOWN_OS:
            raise ValueError(
                f"Host {name} in {path} has os = {cfg['os']!r}, which this"
                f" orchestrator has no shell for. Use one of"
                f" {', '.join(KNOWN_OS)}.")
        where = str(cfg["ssh"]).strip()
        if where in machines:
            first = machines[where]
            which = (f"ssh = {where!r}" if where else
                     "no ssh line, which means this machine")
            raise ValueError(
                f"Hosts {first} and {name} in {path} both have {which}, so"
                f" they are one desk written down twice. Both would be run at"
                f" the same time: two agents stopping and starting the same"
                f" login service, unpacking into the same workdir and"
                f" competing for one serial port, with the loser's failures"
                f" reading as a dead board. Give each desk one entry.")
        machines[where] = name
    return hosts


def is_local(cfg):
    """A desk with no ssh line is this machine, and gets no ssh."""
    return not cfg.get("ssh")


def local_workdir(cfg):
    return Path(os.path.expanduser(cfg["workdir"]))


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------

def _win_workdir(cfg):
    """cmd.exe's spelling of the workdir, with the variable left unexpanded.

    A workdir written $USERPROFILE\\... is accepted and rewritten: that is
    the POSIX habit, and being strict about it here would only produce a
    remote directory literally named $USERPROFILE.
    """
    raw = cfg["workdir"].replace("$USERPROFILE", "%USERPROFILE%")
    return raw.rstrip("\\")


def shell_path(cfg, *parts):
    """The workdir, plus optional components, as one token for that shell.

    The tilde is the subtlety: '~/blink-fleet' quoted is a directory NAMED
    tilde, because sh expands the tilde before it expands quotes and never
    inside them. "$HOME" survives double quotes, so that is what is sent.
    """
    if cfg["os"] == "windows":
        return '"' + "\\".join([_win_workdir(cfg), *parts]) + '"'
    workdir = cfg["workdir"]
    if workdir == "~" or workdir.startswith("~/"):
        tail = "/".join([p for p in (workdir[2:], *parts) if p])
        return '"$HOME"' + ("/" + shlex.quote(tail) if tail else "")
    return shlex.quote("/".join([workdir.rstrip("/"), *parts]))


def _quote(cfg, word):
    if cfg["os"] != "windows":
        return shlex.quote(word)
    # cmd.exe has no escape for a quote inside a quoted string, so this only
    # protects against the characters that actually turn up in these lines:
    # spaces in a bundle path, and cmd's own separators.
    if word and not any(ch in word for ch in ' \t&|<>^()"'):
        return word
    return '"' + word.replace('"', "") + '"'


def shell_line(cfg, argv):
    return " ".join(_quote(cfg, word) for word in argv)


def remote_cmd(cfg, argv):
    """What to hand subprocess to run argv in the desk's workdir.

    Local desks get argv back untouched -- run_host passes the workdir as the
    cwd instead, which keeps this machine off ssh entirely. Remote desks get
    a single shell line, because that is all sshd will accept.
    """
    if is_local(cfg):
        return list(argv)
    line = shell_line(cfg, argv)
    if cfg["os"] == "windows":
        # /d because the workdir may be on another drive than cmd starts on.
        line = f"cd /d {shell_path(cfg)} && {line}"
    else:
        line = f"cd {shell_path(cfg)} && {line}"
    return ["ssh", *SSH_OPTS, cfg["ssh"], line]


def push_cmd(cfg):
    """Make the workdir, drop the previous result, unpack the new snapshot.

    The delete is the point of doing these three as one command: it happens
    on the remote, immediately before the agent runs, so there is no window
    in which an agent that dies early leaves an older result.json to be
    pulled back and believed.

    Windows joins with `&` rather than `&&` because cmd's mkdir fails when
    the directory exists, which is the normal case. The exit code that
    reaches ssh is tar's -- the last command's -- which is the one worth
    having: if mkdir really failed, tar fails too.
    """
    if is_local(cfg):
        return ["tar", "-x", "-f", "-", "-C", str(local_workdir(cfg))]
    directory = shell_path(cfg)
    result = shell_path(cfg, RESULT_NAME)
    if cfg["os"] == "windows":
        line = (f"mkdir {directory} 2>nul"
                f" & del /q {result} 2>nul"
                f" & tar -x -f - -C {directory}")
    else:
        line = (f"mkdir -p {directory}"
                f" && rm -f {result}"
                f" && tar -x -f - -C {directory}")
    return ["ssh", *SSH_OPTS, cfg["ssh"], line]


def pull_cmd(cfg, dest):
    """scp the result back, addressed relative to the remote's home.

    Home-relative rather than ~/ or %USERPROFILE%\\: scp's remote path is
    resolved by sftp against the login directory, so the plainest spelling is
    the one that needs no shell and no variable on the far side.
    """
    workdir = cfg["workdir"]
    for prefix in ("~/", "%USERPROFILE%\\", "%USERPROFILE%/", "$USERPROFILE\\",
                   "$USERPROFILE/"):
        if workdir.startswith(prefix):
            workdir = workdir[len(prefix):]
            break
    workdir = workdir.replace("\\", "/").rstrip("/")
    return ["scp", *SSH_OPTS, f"{cfg['ssh']}:{workdir}/{RESULT_NAME}",
            str(dest)]


def _python(cfg):
    """The interpreter to start the agent with.

    A relative path on the LOCAL desk is resolved against this checkout, not
    against the workdir: .venv-test lives in the source tree, is gitignored,
    and is therefore not inside the snapshot that gets unpacked. It is also
    the only interpreter on that Mac with pyserial, so no default can stand
    in for it.
    """
    interpreter = cfg["python"]
    if is_local(cfg):
        interpreter = os.path.expanduser(interpreter)
        if not os.path.isabs(interpreter):
            interpreter = str(REPO_ROOT / interpreter)
    return interpreter


def agent_argv(cfg, scenarios=None, bundle=None, prev_bundle=None,
               ota_dir=None, expect_version=None, real_account=True,
               result_name=RESULT_NAME):
    """The agent's command line for one desk.

    `scenarios` is a filter by NAME and becomes the agent's --only; the
    agent's own --scenarios is a directory, and the default one inside the
    snapshot is always the right answer.

    No --port is passed. The daemon finds the board by USB VID:PID, which is
    what picks COM15 out of the Windows desk's twelve COM ports; naming a
    port here would break the first time a cable moved socket.

    The bundle paths are interpreted ON THE TARGET DESK. Release archives are
    per-platform, so one path cannot mean the same file on three machines --
    putting the right archive on each desk is a step before this one.
    """
    argv = [_python(cfg), "-m", "tests.fleet.agent",
            "--board", cfg["board"], "--out", result_name]
    if scenarios:
        if not isinstance(scenarios, str):
            scenarios = ",".join(scenarios)
        argv += ["--only", scenarios]
    for flag, value in (("--bundle", bundle), ("--prev-bundle", prev_bundle),
                        ("--ota-dir", ota_dir),
                        ("--expect-version", expect_version)):
        if value:
            argv += [flag, str(value)]
    if real_account:
        argv.append("--real-account")
    return argv


# ---------------------------------------------------------------------------
# Running one desk
# ---------------------------------------------------------------------------

def _text(raw):
    """Whatever a command said, as something printable.

    Bytes rather than text=True on the runner: cmd.exe answers in the console
    codepage, which on that machine is not UTF-8, and a decode error in the
    middle of reporting somebody's failure would replace the cause with a
    UnicodeDecodeError.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _tail(done):
    """The end of a command's output, for repeating back to a person."""
    said = (_text(getattr(done, "stderr", "")).strip()
            or _text(getattr(done, "stdout", "")).strip())
    said = " ".join(said.split())
    return said[-TAIL_CHARS:] if said else "it said nothing at all"


def _run(cmd, runner, what, **kwargs):
    """Run one command. Returns (completed or None, problems).

    Nothing raises past here: an unreachable host, a missing ssh binary and a
    timeout are all outcomes this program reports, not conditions it dies of.
    """
    kwargs.setdefault("capture_output", True)
    try:
        return runner(cmd, **kwargs), []
    except subprocess.TimeoutExpired as e:
        return None, [f"{what} timed out after {int(e.timeout or 0)} s and"
                      f" was given up on. The desk may be asleep, or the"
                      f" agent may be stuck holding the serial port."]
    except FileNotFoundError as e:
        return None, [f"{what} could not be started: {e}. The command was"
                      f" {cmd[0]!r}."]
    except OSError as e:
        return None, [f"{what} failed: {e}"]
    except Exception as e:  # noqa: BLE001 - a runner is allowed to surprise us
        return None, [f"{what} failed unexpectedly: {e!r}"]


def push_snapshot(cfg, runner, name=None, timeout=PUSH_TIMEOUT_S):
    """Send this checkout at HEAD to the desk. Returns problems, or [].

    HEAD and not the working tree, deliberately: what the fleet proves has to
    be a commit, because .fleet/last_run.json names one and a release gated
    on uncommitted edits is gated on nothing. `git archive` also skips
    everything gitignored, which is why the venv has to be named separately.
    """
    name = name or cfg.get("ssh") or "this machine"
    done, problems = _run(["git", "archive", "--format=tar", "HEAD"], runner,
                          "Taking a snapshot of this checkout",
                          cwd=str(REPO_ROOT), timeout=GIT_TIMEOUT_S)
    if problems:
        return problems
    if done.returncode != 0:
        return [f"This checkout could not be archived: {_tail(done)}"]
    archive = done.stdout
    if not archive:
        return ["This checkout archived to nothing, so there is no snapshot"
                " to send. Is HEAD a real commit?"]
    if isinstance(archive, str):
        archive = archive.encode("utf-8", "replace")

    if is_local(cfg):
        workdir = local_workdir(cfg)
        workdir.mkdir(parents=True, exist_ok=True)
        # The same stale-result delete the remote line does inline.
        (workdir / RESULT_NAME).unlink(missing_ok=True)

    done, problems = _run(push_cmd(cfg), runner,
                          f"Unpacking the snapshot on {name}",
                          input=archive, timeout=timeout)
    if problems:
        return problems
    if done.returncode != 0:
        return [f"The snapshot would not unpack on {name}"
                f" (exit {done.returncode}): {_tail(done)}"]
    return []


def _read_result(cfg, name, runner, workroot):
    """Bring the desk's result.json home and parse it. (result, problems)."""
    if is_local(cfg):
        path = local_workdir(cfg) / RESULT_NAME
    else:
        path = Path(workroot) / f"{name}-{RESULT_NAME}"
        done, problems = _run(pull_cmd(cfg, path), runner,
                              f"Fetching the result from {name}",
                              timeout=PULL_TIMEOUT_S)
        if problems:
            return None, problems
        if done.returncode != 0:
            return None, [f"The result file could not be fetched from"
                          f" {name} (exit {done.returncode}): {_tail(done)}."
                          f" The agent may have died before writing it."]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, [f"{name} left no result file to read: {e}"]
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        return None, [f"The result file from {name} is not valid JSON ({e}),"
                      f" so this desk proved nothing. It was probably cut"
                      f" short: {raw[:200]!r}"]
    if not isinstance(parsed, dict) or "ok" not in parsed:
        return None, [f"The result file from {name} is not an agent result:"
                      f" {raw[:200]!r}"]
    return parsed, []


def _failed(name, cfg, problems):
    return {"desk": name, "board": cfg.get("board"), "os": cfg.get("os"),
            "has_claude_desktop": cfg.get("has_claude_desktop"),
            "scenarios": {}, "problems": list(problems), "ok": False}


def run_host(name, cfg, runner=subprocess.run, scenarios=None, bundle=None,
             prev_bundle=None, ota_dir=None, expect_version=None,
             real_account=True, timeout=AGENT_TIMEOUT_S, workroot=None):
    """Prove one desk. Always returns a result; never raises.

    The agent's own verdict is returned as it wrote it, because its problems
    are sentences about that machine's board that nothing here could
    reconstruct -- "the board never answered: PermissionError 13 opening
    /dev/ttyUSB0" is the whole answer for the Ubuntu desk right now, and
    replacing it with "host failed" would throw away the only useful part.

    Two disagreements are refused rather than resolved. A result that says
    every scenario passed while listing no scenarios did not run anything; a
    result that says the same while the agent exited non-zero cannot have
    both halves be true. Both become failures, because a release gate that
    guesses in favour of shipping is not a gate.
    """
    try:
        problems = push_snapshot(cfg, runner, name=name)
        if problems:
            return _failed(name, cfg, problems)

        argv = agent_argv(cfg, scenarios=scenarios, bundle=bundle,
                          prev_bundle=prev_bundle, ota_dir=ota_dir,
                          expect_version=expect_version,
                          real_account=real_account)
        kwargs = {"timeout": timeout}
        if is_local(cfg):
            kwargs["cwd"] = str(local_workdir(cfg))
        done, problems = _run(remote_cmd(cfg, argv), runner,
                              f"The fleet agent on {name}", **kwargs)
        if done is None:
            # A run cut off from this end says nothing about what it left
            # behind on that end. The agent hands the serial port back from a
            # finally, but a finally does not run in a process that was
            # killed, and the shape of that failure is a desk that looks fine
            # with a dark board on it.
            problems.append(
                f"Nothing was collected from {name}, so this desk has proved"
                f" nothing about this commit. Check it by hand before"
                f" trusting it: a run cut off mid-scenario can leave the"
                f" installed service stopped and that board dark.")
            return _failed(name, cfg, problems)

        with _workroot(workroot) as root:
            result, problems = _read_result(cfg, name, runner, root)
        if result is None:
            if done.returncode != 0:
                problems.append(f"The agent on {name} exited"
                                f" {done.returncode}: {_tail(done)}")
            return _failed(name, cfg, problems)

        result.setdefault("problems", [])
        result.setdefault("scenarios", {})
        if not isinstance(result["problems"], list):
            result["problems"] = [str(result["problems"])]
        if not isinstance(result["scenarios"], dict):
            result["problems"].append("The agent's scenario list came back"
                                      " in a shape nothing can read.")
            result["scenarios"] = {}
        # A scenario entry that is not a verdict is turned into one HERE, at
        # the edge, rather than being carried into the report: format_report
        # asks every entry whether it passed, and an AttributeError there
        # would arrive after the state file had already been written -- a
        # traceback instead of a table, over a file claiming the fleet
        # passed. Unreadable is counted as failed, like everything else this
        # module cannot make sense of.
        for scenario, outcome in list(result["scenarios"].items()):
            if not isinstance(outcome, dict) or "ok" not in outcome:
                result["scenarios"][scenario] = {"ok": False, "problems": [
                    f"The verdict for {scenario} came back as {outcome!r},"
                    f" which is not a verdict. Nothing here can read it, so"
                    f" it counts as a failure."]}
        result["desk"] = name
        result["os"] = cfg.get("os")
        result["has_claude_desktop"] = cfg.get("has_claude_desktop")

        # The agent exits 1 when it has failures to report, which it has just
        # reported in its own words. Repeating "exited 1" under every failing
        # desk would bury those words in noise. Any OTHER non-zero code did
        # not come from the agent -- 9009 is cmd.exe saying it found no
        # python, 255 is ssh itself -- and a zero-code run whose result says
        # everything passed while the exit code says otherwise is a
        # contradiction that has to be refused rather than resolved.
        if done.returncode != 0:
            if result.get("ok") is True:
                result["problems"].append(
                    f"The agent on {name} exited {done.returncode}:"
                    f" {_tail(done)}. Its result file says every scenario"
                    f" passed, which cannot also be true, so {name} is"
                    f" counted as failed.")
            elif done.returncode != 1 or not result["problems"]:
                result["problems"].append(
                    f"The agent on {name} exited {done.returncode}:"
                    f" {_tail(done)}")
            result["ok"] = False
        if result.get("ok") is True and not result["scenarios"]:
            result["problems"].append(
                f"{name} reported success without running a single scenario,"
                f" which is not a pass. Check the scenario filter and the"
                f" agent's own output.")
            result["ok"] = False
        # The third contradiction, and the one that would read worst: a
        # PASSED sitting on the same line as "overage FAILED". The agent in
        # this snapshot cannot write it -- its _finish() ands the scenarios
        # together -- but the whole premise here is refusing contradictions
        # rather than picking the half that ships.
        lost = sorted(scenario for scenario, outcome
                      in result["scenarios"].items()
                      if outcome.get("ok") is not True)
        if result.get("ok") is True and lost:
            result["problems"].append(
                f"{name} reported success while {', '.join(lost)} failed on"
                f" it. Those two cannot both be true, so it is counted as"
                f" failed.")
            result["ok"] = False
        result["ok"] = result.get("ok") is True
        return result
    except Exception as e:  # noqa: BLE001 - a desk may not take the run down
        return _failed(name, cfg, [f"Proving {name} stopped on an unexpected"
                                   f" error: {e!r}"])


class _workroot:
    """A directory for pulled results: the caller's, or a temporary one."""

    def __init__(self, given):
        self.given = given
        self.tmp = None

    def __enter__(self):
        if self.given:
            Path(self.given).mkdir(parents=True, exist_ok=True)
            return Path(self.given)
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-fleet-")
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        if self.tmp:
            self.tmp.cleanup()
        return False


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def aggregate(hosts):
    """Green only if there is something to be green about, and all of it is.

    An empty fleet is a failure, not a vacuous pass: it is what a filter that
    matched nothing, or a run that collected nothing, looks like.
    """
    failed = sorted(name for name, result in hosts.items()
                    if result.get("ok") is not True)
    return {"ok": bool(hosts) and not failed, "hosts": len(hosts),
            "failed": failed}


def format_report(state):
    """The table somebody reads when a release is blocked.

    A desk that ran nothing prints "Nothing ran" rather than an empty cell,
    because an empty cell in a row of ticks reads as fine.
    """
    hosts = state.get("hosts", {})
    took = (state.get("finished_at") or 0) - (state.get("started_at") or 0)
    lines = [""]
    lines.append(f"Fleet run {_short(state.get('sha', 'unknown'))}:"
                 f" {len(hosts)} desk(s) in {took / 60:.1f} min")
    lines.append("-" * 72)
    for name in sorted(hosts):
        result = hosts[name]
        verdict = "PASSED" if result.get("ok") is True else "FAILED"
        scenarios = result.get("scenarios") or {}
        if scenarios:
            summary = ", ".join(
                f"{scenario} {'ok' if outcome.get('ok') else 'FAILED'}"
                for scenario, outcome in sorted(scenarios.items()))
        else:
            summary = "Nothing ran"
        lines.append(f"{name:<16}{verdict:<8}{summary}")
        for problem in result.get("problems") or []:
            lines.append(f"{'':<16}{problem}")
        for scenario, outcome in sorted(scenarios.items()):
            if outcome.get("ok"):
                continue
            for problem in outcome.get("problems") or []:
                lines.append(f"{'':<16}{scenario}: {problem}")
    lines.append("-" * 72)
    summary = aggregate(hosts)
    # The run's own verdict when it has one, not this function's opinion of
    # the desks. They can differ -- three green desks on a commit that could
    # not be read is still not a release anybody can point at -- and a table
    # that says PASSED under an exit code of 1 is the worst of both.
    passed = state["ok"] is True if "ok" in state else summary["ok"]
    if passed:
        lines.append("The fleet passed.")
    else:
        blocked = ", ".join(summary["failed"]) or "no desk reported at all"
        if summary["ok"]:
            blocked = "nothing on the desks, see below"
        lines.append(f"The fleet did NOT pass. Blocked by: {blocked}.")
    for problem in state.get("problems") or []:
        lines.append(problem)
    lines.append("")
    return lines


def _short(sha):
    """Seven characters of a commit, but all seven letters of "unknown"."""
    sha = str(sha)
    return sha[:7] if len(sha) > 12 else sha


def _head_sha(runner):
    """The commit the desks were sent. (sha, problems)."""
    done, problems = _run(["git", "rev-parse", "HEAD"], runner,
                          "Reading this checkout's commit",
                          cwd=str(REPO_ROOT), timeout=GIT_TIMEOUT_S)
    if problems or done.returncode != 0:
        return "unknown", (problems or
                           [f"This checkout's commit could not be read:"
                            f" {_tail(done)}. Without it the result names no"
                            f" commit and cannot gate a release."])
    return _text(done.stdout).strip() or "unknown", []


def _write_state(path, doc):
    """Write the verdict atomically, so a reader never sees half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="tools/fleet/run.py",
        description="Run the fleet scenarios on every desk and add it up.")
    ap.add_argument("--inventory", default=str(DEFAULT_INVENTORY),
                    help="The fleet.toml naming the desks")
    ap.add_argument("--state", default=str(DEFAULT_STATE),
                    help="Where the run's verdict is written")
    ap.add_argument("--only", default=None,
                    help="Comma-separated host names, instead of all of them")
    ap.add_argument("--scenarios", default=None,
                    help="Comma-separated scenario names, instead of all")
    ap.add_argument("--bundle", default=None,
                    help="Release archive for the fresh-install scenario, as"
                         " named ON EACH DESK -- archives are per-platform")
    ap.add_argument("--prev-bundle", default=None,
                    help="The previous release's archive, on each desk")
    ap.add_argument("--ota-dir", default=None,
                    help="Directory of the candidate's manifest, signature"
                         " and archive, on each desk")
    ap.add_argument("--expect-version", default=None,
                    help="The version both bundle scenarios must end at")
    ap.add_argument("--timeout", type=float, default=AGENT_TIMEOUT_S,
                    help="Seconds to allow one desk's agent")
    ap.add_argument("--no-real-account", action="store_true",
                    help="Skip the pass against each desk's own account")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the commands each desk would be sent and"
                         " stop, without connecting to anything")
    return ap.parse_args(argv)


def main(argv=None, runner=subprocess.run):
    args = parse_args(argv)
    state = Path(args.state)
    if not args.dry_run:
        # Before the inventory is even read, so that the paths which give up
        # early take the old verdict with them. A typo in --only that left
        # last week's green file behind would hand the release gate a file
        # the operator believes they just refreshed. A dry run is the one
        # exception: it decides nothing, so it disturbs nothing.
        state.parent.mkdir(parents=True, exist_ok=True)
        state.unlink(missing_ok=True)

    try:
        inventory = load_inventory(args.inventory)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    wanted = None
    if args.only:
        wanted = [name.strip() for name in args.only.split(",")
                  if name.strip()]
        unknown = [name for name in wanted if name not in inventory]
        if unknown:
            print(f"The inventory has no desk called"
                  f" {', '.join(unknown)}. It holds:"
                  f" {', '.join(sorted(inventory))}.", file=sys.stderr)
            return 2
        inventory = {name: inventory[name] for name in wanted}

    if args.dry_run:
        # This run stops somebody's daemon on three machines and takes their
        # boards for the best part of an hour. Being able to read the exact
        # lines first -- particularly the cmd.exe one, which cannot be tested
        # anywhere but on that desk -- is cheaper than finding out halfway.
        for name, cfg in sorted(inventory.items()):
            argv = agent_argv(cfg, scenarios=args.scenarios,
                              bundle=args.bundle,
                              prev_bundle=args.prev_bundle,
                              ota_dir=args.ota_dir,
                              expect_version=args.expect_version,
                              real_account=not args.no_real_account)
            print(f"\n{name} ({cfg['os']},"
                  f" {cfg['ssh'] or 'no ssh, this machine'}):")
            print(f"  push: {push_cmd(cfg)}")
            print(f"  run : {remote_cmd(cfg, argv)}")
            if not is_local(cfg):
                print(f"  pull: {pull_cmd(cfg, '<temporary file>')}")
        print("\nNothing was run and no desk was contacted.")
        return 0

    sha, problems = _head_sha(runner)
    doc = {"sha": sha, "started_at": time.time(), "finished_at": None,
           "ok": False, "hosts": {}, "problems": problems,
           # Recorded so the release gate can tell a full fleet run from a
           # one-desk or one-scenario run that happens to be green.
           "only": wanted, "scenarios": args.scenarios}
    print(f"Fleet run of {_short(sha)} on {len(inventory)} desk(s):"
          f" {', '.join(sorted(inventory))}.")
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, len(inventory))) as pool:
            futures = {
                pool.submit(run_host, name, cfg, runner=runner,
                            scenarios=args.scenarios, bundle=args.bundle,
                            prev_bundle=args.prev_bundle,
                            ota_dir=args.ota_dir,
                            expect_version=args.expect_version,
                            real_account=not args.no_real_account,
                            timeout=args.timeout): name
                for name, cfg in inventory.items()}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    doc["hosts"][name] = future.result()
                except BaseException as e:  # noqa: BLE001
                    doc["hosts"][name] = _failed(
                        name, inventory[name],
                        [f"Proving {name} died in its own thread: {e!r}"])
    finally:
        doc["finished_at"] = time.time()
        missing = sorted(set(inventory) - set(doc["hosts"]))
        for name in missing:
            doc["hosts"][name] = _failed(
                name, inventory[name],
                ["This desk never reported. The run was cut short before it"
                 " finished, so nothing is known about it."])
        summary = aggregate(doc["hosts"])
        doc["ok"] = (summary["ok"] and not doc["problems"]
                     and doc["sha"] != "unknown")
        _write_state(state, doc)

    # The verdict is already on disk by here. Whatever the table does, it
    # must not be the last thing that happens: a traceback in place of the
    # report, over a .fleet/last_run.json the gate will read as green, is the
    # worst pairing this program has. So the fallback says the two things
    # that cannot be lost -- what was decided, and where it was written.
    try:
        for line in format_report(doc):
            print(line)
    except Exception as e:  # noqa: BLE001
        print(f"\nThe result table could not be printed ({e!r}), so here is"
              f" the verdict without it.", file=sys.stderr)
        print(f"Fleet run {_short(doc['sha'])}:"
              f" {'PASSED' if doc['ok'] else 'did NOT pass'}.")
        for host, result in sorted(doc["hosts"].items()):
            state_word = "PASSED" if result.get("ok") is True else "FAILED"
            print(f"  {host}: {state_word}")
    print(f"Written to {state}")
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
