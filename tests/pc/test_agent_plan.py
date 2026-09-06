"""The per-host fleet agent, with every wire and service call faked out.

Not one test here opens a serial port, spawns the daemon or touches a login
service. The agent's own dangers are all in its plumbing -- which environment
the child gets, whether the service comes back after a failure, what happens
when the stop was skipped -- and plumbing is exactly what a fake can hold
still while it is inspected.

The one thing worth stating out loud: BLINK_SKIP_SERVICE belongs in the
child's environment and nowhere else. Set in the agent's own process it would
turn stop_service() into a no-op, leave the real daemon holding the port, and
report a hardware fault against a healthy board -- with start_service()
no-opping in turn, so nothing is left broken to diagnose from afterwards.
"""
import json
import os
import types
from pathlib import Path

import pytest

from pc.service_ctl import Outcome
from tests.fleet import agent


class _FakeProc:
    def __init__(self, events=None):
        self.terminated = False
        self.killed = False
        self._events = events if events is not None else []

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True
        self._events.append("stop")

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _tap_lines(scenario_doc=None):
    """A transcript of a board that behaved, for whatever was asked of it."""
    lines = [{"dir": "rx", "t": 0.0, "msg": {"t": "hello", "v": 1}}]
    steps = (scenario_doc or {}).get("steps", [{"at": 0}])
    for i, step in enumerate(steps):
        at = float(step.get("at", i)) + 1.0
        lines.append({"dir": "tx", "t": at, "sent": True,
                      "msg": {"t": "usage", "v": 1, "session_pct": 50.0,
                              "weekly_pct": 20.0}})
        tail = "  STALE" if step.get("stale") else ""
        lines.append({"dir": "console", "t": at + 0.1,
                      "line": f"[usage] session 50% (1s)  weekly 20% (2s){tail}"})
    return lines


def _spawner(lines_for=lambda env, attempt: _tap_lines(), events=None):
    """A stand-in for Popen that writes a tap and hands back a fake process."""
    calls = []

    def spawn(cmd, env, cwd):
        calls.append({"cmd": cmd, "env": env, "cwd": cwd})
        if events is not None:
            events.append("spawn")
        with open(env["BLINK_TAP"], "a", encoding="utf-8") as f:
            for rec in lines_for(env, len(calls)):
                f.write(json.dumps(rec) + "\n")
        return _FakeProc(events)

    spawn.calls = calls
    return spawn


def _clock():
    """A clock that advances a second per reading.

    A frozen one would let a wait for something that never arrives spin
    forever; a real one would make the same wait take its real timeout.
    """
    ticks = iter(range(10_000))
    return lambda: float(next(ticks))


def _deps(sleeps=None, **over):
    base = dict(spawn=_spawner(),
                sleep=(sleeps.append if sleeps is not None else lambda s: None),
                now=_clock(), wall=lambda: 0.0,
                runner=lambda *a, **k: types.SimpleNamespace(
                    returncode=0, stdout='{"t": "usage"}\n', stderr=""),
                stop=lambda: Outcome(True, False, "stopped"),
                start=lambda: Outcome(True, False, "started"))
    base.update(over)
    return agent.Deps(**base)


def _scenario(tmp_path, name="x", **over):
    doc = {"name": name, "duration_s": 1,
           "steps": [{"at": 0, "provider": "claude", "session_pct": 50.0}],
           "expect": {"min_tx": 1, "min_board_usage": 1,
                      "min_stale_lines": 0, "min_sleep_wakes": 0}}
    doc.update(over)
    d = tmp_path / "scenarios"
    d.mkdir(exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


# --- the scenario copy ------------------------------------------------

def test_codex_rewrite(tmp_path):
    src = _scenario(tmp_path) / "x.json"
    out = agent.prepare_scenario(src, board="codex", workdir=tmp_path / "w")
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["steps"][0]["provider"] == "codex"
    assert src != out, "the original scenario file must not be rewritten"
    assert json.loads(src.read_text(encoding="utf-8"))[
        "steps"][0]["provider"] == "claude"


def test_claude_board_uses_the_scenario_as_shipped(tmp_path):
    src = _scenario(tmp_path) / "x.json"
    assert agent.prepare_scenario(src, board="claude",
                                  workdir=tmp_path / "w") == src


# --- the child environment --------------------------------------------

def test_env_for_run_sandboxes_both_home_vars(tmp_path):
    env = agent.env_for_run(tmp_path, scenario=tmp_path / "s.json",
                            tap=tmp_path / "tap.jsonl", sandbox=True)
    assert env["HOME"] == str(tmp_path) and env["USERPROFILE"] == str(tmp_path)
    assert env["BLINK_SKIP_SERVICE"] == "1"
    assert env["BLINK_SCENARIO"] == str(tmp_path / "s.json")
    assert env["BLINK_TAP"] == str(tmp_path / "tap.jsonl")
    assert float(env["BLINK_POLL_INTERVAL_S"]) > 0
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_real_account_env_keeps_real_home(tmp_path):
    sandbox = tmp_path / "sandbox"
    env = agent.env_for_run(sandbox, scenario=None,
                            tap=tmp_path / "tap.jsonl", sandbox=False)
    assert "BLINK_SCENARIO" not in env
    assert env["HOME"] != str(sandbox)
    assert env["USERPROFILE"] != str(sandbox)
    assert env["BLINK_TAP"] == str(tmp_path / "tap.jsonl")


def test_skip_service_never_reaches_the_agents_own_environment(
        tmp_path, monkeypatch):
    monkeypatch.delenv("BLINK_SKIP_SERVICE", raising=False)
    agent.env_for_run(tmp_path, scenario=None, tap=tmp_path / "t.jsonl",
                      sandbox=True)
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    agent.run(args, _deps())
    assert "BLINK_SKIP_SERVICE" not in os.environ


# --- the daemon command -----------------------------------------------

def test_daemon_cmd_forwards_only_a_real_port():
    assert "--port" not in agent.daemon_cmd("auto")
    assert agent.daemon_cmd("COM15")[-2:] == ["--port", "COM15"]


# --- selection --------------------------------------------------------

def test_only_selects_named_scenarios(tmp_path):
    d = _scenario(tmp_path, "one")
    _scenario(tmp_path, "two")
    assert [p.stem for p in agent.select_scenarios(d, ["one"])] == ["one"]
    assert [p.stem for p in agent.select_scenarios(d, None)] == ["one", "two"]


def test_a_scenario_named_after_the_runs_own_directories_is_refused(tmp_path):
    """Every pass empties its working directory before its daemon starts.

    A scenario called `home` would therefore delete the run's shared sandbox
    home halfway through it, and one called `preflight` would delete the
    settle attempts. Refused up front, before the service is stopped.
    """
    d = _scenario(tmp_path, "home")
    with pytest.raises(ValueError, match="home"):
        agent.select_scenarios(d)


def test_unknown_scenario_name_is_refused(tmp_path):
    d = _scenario(tmp_path, "one")
    with pytest.raises(ValueError, match="nope"):
        agent.select_scenarios(d, ["nope"])


# --- the run ----------------------------------------------------------

def test_a_healthy_run_is_ok(tmp_path):
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps())
    assert result["ok"] is True
    assert result["scenarios"]["x"] == {"ok": True, "problems": []}
    assert result["board"] == "claude" and result["host"]


def test_a_board_that_applies_nothing_fails(tmp_path):
    quiet = [{"dir": "rx", "t": 0.0, "msg": {"t": "hello", "v": 1}},
             {"dir": "tx", "t": 1.0, "sent": True,
              "msg": {"t": "usage", "v": 1}}]
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=_spawner(lambda e, n: quiet)))
    assert result["ok"] is False
    assert any("apply" in p for p in result["scenarios"]["x"]["problems"])


def test_a_mistyped_only_never_costs_the_operator_their_daemon(tmp_path):
    stopped = []
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--only", "nope"])
    result = agent.run(args, _deps(
        stop=lambda: stopped.append(True) or Outcome(True, False, "stopped")))
    assert result["ok"] is False
    assert any("nope" in p for p in result["problems"])
    assert stopped == []


def test_run_aborts_when_the_service_stop_was_skipped(tmp_path):
    started = []
    spawn = _spawner()
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(
        spawn=spawn,
        stop=lambda: Outcome(False, True, "skipped (BLINK_SKIP_SERVICE=1)"),
        start=lambda: started.append(True) or Outcome(True, False, "started")))
    assert result["ok"] is False
    assert any("BLINK_SKIP_SERVICE" in p for p in result["problems"])
    assert spawn.calls == [], "no daemon may run while the service holds the port"
    assert started == [], "nothing was stopped, so nothing gets started"


def test_service_comes_back_even_when_a_scenario_explodes(tmp_path):
    started = []

    def boom(cmd, env, cwd):
        raise OSError("no such executable")

    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(
        spawn=boom,
        start=lambda: started.append(True) or Outcome(True, False, "started")))
    assert started == [True]
    assert result["ok"] is False


def test_a_failed_stop_is_reported_but_does_not_skip_the_restart(tmp_path):
    started = []
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(
        stop=lambda: Outcome(False, False, "could not stop the service: boom"),
        start=lambda: started.append(True) or Outcome(True, False, "started")))
    assert result["ok"] is False
    assert any("could not stop" in p for p in result["problems"])
    assert started == [True]


def test_a_restart_that_fails_after_a_failed_stop_is_reported_too(tmp_path):
    """The one path that can leave a desk with no daemon used to not look.

    The early return after a stop that failed restarted the service and threw
    the Outcome away, while the finally on every other path checked it. So the
    run most likely to have left the service in a state nobody asked for was
    the run that said nothing about it.
    """
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(
        stop=lambda: Outcome(False, False, "could not stop the service: boom"),
        start=lambda: Outcome(False, False, "could not start it: launchctl")))
    assert result["ok"] is False
    assert any("stay dark" in p for p in result["problems"]), result["problems"]


# --- the port settling after an asynchronous bootout ------------------

def test_preflight_retries_before_blaming_the_board(tmp_path):
    """launchctl bootout returns before the port is actually free."""
    def lines(env, attempt):
        return [] if attempt == 1 else _tap_lines()

    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=_spawner(lines)))
    assert result["ok"] is True
    assert "attempt 2" in result["preflight"], (
        "how long the port took to come free belongs in the results file")


def test_a_board_that_never_speaks_fails_every_scenario_by_name(tmp_path):
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=_spawner(lambda e, n: [])))
    assert result["ok"] is False
    assert result["scenarios"]["x"]["ok"] is False
    assert any("port" in p for p in result["problems"])


# --- one home for the whole run ---------------------------------------

def test_every_sandboxed_pass_shares_one_home(tmp_path):
    """A fresh home per scenario is a firmware offer per scenario.

    With no cached manifest the daemon offers an update on every hello, and
    the panel under test shows an Install prompt at the start of every pass --
    noise on the one screen the run exists to watch, and an invitation to a
    stray tap on a desk with somebody sitting at it. One home per run still
    keeps the operator's real one untouched, which is the isolation that
    matters, and it is closer to how the daemon actually runs.
    """
    d = _scenario(tmp_path, "one")
    _scenario(tmp_path, "two")
    spawn = _spawner()
    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json")])
    assert agent.run(args, _deps(spawn=spawn))["ok"] is True
    homes = {c["env"]["HOME"] for c in spawn.calls}
    assert len(homes) == 1, f"one home per run, saw {homes}"
    home = homes.pop()
    assert all(c["env"]["USERPROFILE"] == home for c in spawn.calls)
    taps = {c["env"]["BLINK_TAP"] for c in spawn.calls}
    assert len(taps) == len(spawn.calls), "each pass keeps its own transcript"
    assert not any(t.startswith(home + os.sep) for t in taps), (
        "transcripts must not accumulate inside the daemon's home")


def test_the_real_account_pass_is_not_given_the_shared_home(tmp_path):
    spawn = _spawner()
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account"])
    agent.run(args, _deps(spawn=spawn))
    real = [c for c in spawn.calls if "real_account" in c["env"]["BLINK_TAP"]]
    sandboxed = [c for c in spawn.calls if c not in real]
    assert real and real[0]["env"]["HOME"] not in {
        c["env"]["HOME"] for c in sandboxed}


# --- the sleep scenario: a daemon lifecycle, not a data timeline ------

def _sleep_scenario(tmp_path, name="naps"):
    return _scenario(
        tmp_path, name, duration_s=12, host_silence_s=45, wake_duration_s=12,
        steps=[{"at": 0, "provider": "claude", "session_pct": 40.0},
               {"at": 5, "provider": "claude", "session_pct": 45.0}],
        expect={"min_tx": 4, "min_board_usage": 4, "min_stale_lines": 0,
                "min_sleep_wakes": 1})


def _sleeper(events=None):
    """A board that sleeps while the daemon is gone and says so on waking."""
    def lines(env, attempt):
        woke = os.path.getsize(env["BLINK_TAP"]) > 0 if os.path.exists(
            env["BLINK_TAP"]) else False
        out = _tap_lines({"steps": [{"at": 0}, {"at": 5}]})
        if woke:
            out.insert(0, {"dir": "console", "t": 0.0,
                           "line": "[sleep] host back; opening eyes"})
        return out
    return _spawner(lines, events)


def test_a_sleep_scenario_stops_the_daemon_and_brings_it_back(tmp_path):
    """The board can only sleep when the daemon is gone, so it is gone.

    dispatch() stamps last_host_ms on any host line and the daemon pongs
    every ping, so no poll interval can ever produce 30s of host silence.
    Stopping the daemon is the whole scenario.
    """
    events = []
    d = _sleep_scenario(tmp_path)
    spawn = _sleeper(events)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if seconds == 45:
            events.append("silence")

    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=spawn, sleep=sleep))
    assert result["ok"] is True, result["scenarios"]

    passes = [c for c in spawn.calls if "naps" in c["env"]["BLINK_TAP"]]
    assert len(passes) == 2, "one pass before the silence and one after"
    assert passes[0]["env"]["BLINK_TAP"] == passes[1]["env"]["BLINK_TAP"], (
        "both passes write one transcript, or the wake cannot be seen after"
        " the frames that preceded it")
    assert 45 in sleeps, "the daemon must be gone for the scenario's silence"

    # The silence has to be silent: preflight and the first pass are both
    # stopped before it begins, and only then does the daemon come back.
    assert events == ["spawn", "stop",      # preflight
                      "spawn", "stop",      # the first pass
                      "silence",
                      "spawn", "stop"], events


def test_the_silence_only_happens_once_the_daemon_is_confirmed_gone(tmp_path):
    """An orphan still holding the port keeps the board awake.

    Waiting out 45 seconds that something is still talking through would end
    in a board that never slept and a scenario that blamed it for that.
    """
    d = _sleep_scenario(tmp_path)
    sleeps = []
    outcome = agent.run_scenario(
        d / "naps.json", "claude", tmp_path / "w", "auto",
        _deps(sleeps=sleeps, spawn=_stubborn_spawner()))
    assert 45 not in sleeps
    assert any("silence was skipped" in p for p in outcome["problems"])


def _stubborn_spawner():
    class _Undead(_FakeProc):
        def terminate(self):
            raise OSError("no such process")

        def kill(self):
            raise OSError("no such process")

    def spawn(cmd, env, cwd):
        open(env["BLINK_TAP"], "a", encoding="utf-8").close()
        return _Undead()
    spawn.calls = []
    return spawn


def test_a_daemon_that_will_not_die_is_reported_not_ignored(tmp_path):
    """Restarting the service on top of an orphan leaves a dark desk.

    The service comes back, the port is still held by the child, the board
    never gets another frame -- and nothing anywhere says so unless this
    does.
    """
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=_stubborn_spawner()))
    assert result["ok"] is False
    everything = result["problems"] + [
        p for s in result["scenarios"].values() for p in s["problems"]]
    assert any("Neither terminate nor kill" in p for p in everything), (
        "an orphan holding the port must be named, not inferred from a"
        f" board that then looks dead: {everything}")


# --- grace that scales with what the scenario is waiting for ----------

def test_grace_grows_for_a_sleeping_scenario_and_for_a_slower_poll():
    plain = {"expect": {"min_sleep_wakes": 0}}
    sleeper = {"host_silence_s": 45, "expect": {"min_sleep_wakes": 1}}
    assert agent.grace_s(sleeper, 3.0) > agent.grace_s(plain, 3.0)
    assert agent.grace_s(plain, 6.0) > agent.grace_s(plain, 3.0)
    assert agent.grace_s({}, 3.0) == agent.grace_s(plain, 3.0)


def test_a_scenario_that_waits_gets_time_to_finish_waiting(tmp_path):
    """The wait is duration plus this scenario's own grace, not a constant.

    A fixed grace makes the longest scenario the first to go flaky on a slow
    desk, and a flaky scenario inside a release gate teaches people to re-run
    until green.
    """
    doc = {"duration_s": 60,
           "expect": {"min_tx": 1, "min_board_usage": 1,
                      "min_stale_lines": 0, "min_sleep_wakes": 0}}
    d = _scenario(tmp_path, "waits", **doc)
    sleeps = []
    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json")])
    agent.run(args, _deps(sleeps=sleeps))
    assert max(sleeps) == 60 + agent.grace_s(doc, agent.POLL_INTERVAL_S)


def test_the_poll_interval_reaches_both_the_child_and_the_grace(tmp_path):
    """Every child, the real-account daemon included.

    That pass is reached through the longest argument list in the file, and a
    parameter added in the middle of it would quietly hand this interval to
    something else -- a mistake no verdict would ever show.
    """
    spawn = _spawner()
    doc = {"duration_s": 10, "expect": {"min_tx": 1, "min_board_usage": 1,
                                        "min_stale_lines": 0,
                                        "min_sleep_wakes": 0}}
    d = _scenario(tmp_path, "x", **doc)
    sleeps = []
    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account", "--poll-interval", "7"])
    agent.run(args, _deps(sleeps=sleeps, spawn=spawn))
    assert all(c["env"]["BLINK_POLL_INTERVAL_S"] == "7.0" for c in spawn.calls)
    assert max(sleeps) == 10 + agent.grace_s(doc, 7.0)


# --- the port between scenarios ---------------------------------------

def test_a_settle_precedes_every_scenario(tmp_path):
    """The previous daemon's port does not come free the instant it exits."""
    d = _scenario(tmp_path, "one")
    _scenario(tmp_path, "two")
    sleeps = []
    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json")])
    agent.run(args, _deps(sleeps=sleeps))
    assert sleeps.count(agent.PORT_SETTLE_S) == 2


def test_a_late_connect_is_inconclusive_rather_than_a_board_fault(tmp_path):
    """A shifted timeline can run out of pass before it runs out of steps.

    ScriptedProvider stamps its t0 when the daemon is CONSTRUCTED, not when
    it reaches the board, and hands over one step per poll -- so a daemon kept
    waiting for the port replays the whole timeline that much later and can
    have its last steps cut off by the end of the pass, coming up short on
    min_tx. That is not the board's fault and must not be reported as though
    it were.
    """
    def late(env, attempt):
        return [dict(r, t=r["t"] + 30) for r in _tap_lines()]

    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=_spawner(late)))
    entry = result["scenarios"]["x"]
    assert entry.get("inconclusive") is True
    assert entry["ok"] is False
    assert any("30" in p and "again" in p for p in entry["problems"])


def test_a_late_connect_on_the_WAKE_pass_is_inconclusive_too(tmp_path):
    """min_tx counts on the second daemon replaying every step.

    So the pass most likely to lose the end of its timeline is the one after
    the silence -- it is the shorter of the two -- and it was the pass whose
    connect delay was thrown away.
    """
    def late_second(env, attempt):
        out = _tap_lines({"steps": [{"at": 0}, {"at": 5}]})
        if os.path.getsize(env["BLINK_TAP"]) > 0:      # the wake pass
            out.insert(0, {"dir": "console", "t": 0.0,
                           "line": "[sleep] host back; opening eyes"})
            out = [dict(r, t=r["t"] + 30) for r in out]
        return out

    d = _sleep_scenario(tmp_path)
    outcome = agent.run_scenario(d / "naps.json", "claude", tmp_path / "w",
                                 "auto", _deps(spawn=_spawner(late_second)))
    assert outcome.get("inconclusive") is True
    assert any("30" in p and "again" in p for p in outcome["problems"])


def test_a_prompt_connect_is_not_flagged(tmp_path):
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps())
    assert "inconclusive" not in result["scenarios"]["x"]


def test_the_connect_delay_is_measured_from_the_BOARD_not_from_chatter(
        tmp_path):
    """The first record of a pass is not necessarily the board.

    The daemon walks a candidate list looking for the board
    (claude_usage_bridge.py:858-863) and prints as it goes, so on a desk with
    twelve ports -- the Windows one -- the first line in the transcript can be
    a foreign device being probed seconds before the board is found. Measured
    from that line, a genuinely late connect looks prompt, and the scenario
    that follows is reported as a board fault instead of as inconclusive:
    exactly the outcome this protection exists to prevent.
    """
    def chatter_then_a_late_board(env, attempt):
        out = [{"dir": "console", "t": 0.2,
                "line": "[ports] trying /dev/cu.Bluetooth-Incoming-Port"}]
        return out + [dict(r, t=r["t"] + 30) for r in _tap_lines()]

    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json")])
    result = agent.run(args, _deps(spawn=_spawner(chatter_then_a_late_board)))
    entry = result["scenarios"]["x"]
    assert entry.get("inconclusive") is True
    assert any("30" in p and "again" in p for p in entry["problems"])


# --- the real-account pass --------------------------------------------

def test_real_account_pass_wants_a_percentage_and_a_parseable_wire(tmp_path):
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account"])
    result = agent.run(args, _deps())
    assert result["scenarios"]["real_account"]["ok"] is True


def test_the_real_account_verdict_is_printed_like_every_other(tmp_path, capsys):
    """It is the one pass that says whether this desk can read its own tools.

    Every other scenario prints its name and its problems as the run goes by,
    which is what the person at the desk is reading. This one was stored in
    the results file and never said out loud.
    """
    def no_percentage(env, attempt):
        out = _tap_lines()
        if "BLINK_SCENARIO" not in env:
            for rec in out:
                if rec["dir"] == "tx":
                    rec["msg"] = {"t": "usage", "v": 1, "session_pct": -1}
        return out

    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account"])
    agent.run(args, _deps(spawn=_spawner(no_percentage)))
    printed = capsys.readouterr().out
    assert "real_account: FAILED" in printed
    assert "percentage" in printed


def test_real_account_pass_fails_when_no_percentage_goes_out(tmp_path):
    def lines(env, attempt):
        out = _tap_lines()
        if "BLINK_SCENARIO" not in env:      # the real-account daemon
            for rec in out:
                if rec["dir"] == "tx":
                    rec["msg"] = {"t": "usage", "v": 1, "session_pct": -1,
                                  "weekly_pct": -1}
        return out

    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account"])
    result = agent.run(args, _deps(spawn=_spawner(lines)))
    problems = result["scenarios"]["real_account"]["problems"]
    assert any("percentage" in p for p in problems)


# The shape of a real desk, measured on the Mac on 2026-08-31: the daemon's
# probe window puts the board's own greeting into the transcript as raw
# console bytes before the read loop is running, the usage frame leaves the
# host at once, and the board's apply line and its next ping land seconds
# later. The pass used to stop the daemon at the frame and then blame the
# board for the two records that had not arrived yet.
_PROBE_CONSOLE = [
    {"dir": "console", "t": 0.0, "line": "[proto] host connected"},
    {"dir": "console", "t": 0.1, "line": '{"t": "pref", "v": 1}'},
    {"dir": "console", "t": 0.2, "line": '{"t": "ota_query", "v": 1}'},
]
_REAL_FRAME = {"dir": "tx", "t": 0.3, "sent": True,
               "msg": {"t": "usage", "v": 2, "session_pct": 9.0,
                       "weekly_pct": 88.0}}
_BOARD_APPLIED = {"dir": "console", "t": 4.0,
                  "line": "[usage] session 9% (1s)  weekly 88% (2s)"}
_BOARD_PING = {"dir": "rx", "t": 10.0, "msg": {"t": "ping", "v": 1}}


def _real_account_tap(work):
    return work / agent.REAL_ACCOUNT / "tap.jsonl"


def _board_answers_late(tap, events, late):
    """A `deps.sleep` that lets the board's own records land mid-wait.

    Nothing else in this file can express "after the frame went out": the
    spawner writes its whole transcript at spawn time, which is the one thing
    a real board never does. Releasing on the second sleep puts these records
    inside the settle wait -- the first sleep is PORT_SETTLE_S, and the wait
    for the usage frame finds it already written and never sleeps at all.
    """
    def sleep(_seconds):
        events.append("sleep")
        if late and events.count("sleep") >= 2:
            with open(tap, "a", encoding="utf-8") as f:
                for rec in late:
                    f.write(json.dumps(rec) + "\n")
            events.append("board")
            del late[:]
    return sleep


def test_real_account_pass_waits_for_the_board_before_stopping_the_daemon(
        tmp_path):
    """The frame leaving the host is not the end of the pass.

    The board still has to apply it and print, and its next ping still has to
    be read. Stopping the daemon at the tx ends the pass before either can
    happen and reports a healthy desk as a board that never spoke.
    """
    work = tmp_path / "w"
    events = []
    late = [_BOARD_APPLIED, _BOARD_PING]
    spawn = _spawner(lambda env, attempt: _PROBE_CONSOLE + [_REAL_FRAME],
                     events=events)
    result = agent.run_real_account(work, "auto", _deps(
        spawn=spawn,
        sleep=_board_answers_late(_real_account_tap(work), events, late)))
    assert "board" in events, (
        "the daemon was stopped before the board could answer at all")
    assert events.index("board") < events.index("stop")
    assert result["ok"] is True, result["problems"]


def test_real_account_pass_needs_an_rx_not_just_the_boards_printout(tmp_path):
    """Both halves of the evidence, and neither standing in for the other.

    The board's greeting reaches the transcript twice on a real desk -- once
    as raw console bytes during the port probe, once as `rx` when the daemon's
    read loop parses it -- so a transcript with the apply line and console
    chatter but no `rx` proves bytes are moving and not that this daemon is
    listening. The settle wait has to keep waiting, and the pass has to fail.
    """
    work = tmp_path / "w"
    events = []
    late = [_BOARD_APPLIED]
    spawn = _spawner(lambda env, attempt: _PROBE_CONSOLE + [_REAL_FRAME],
                     events=events)
    result = agent.run_real_account(work, "auto", _deps(
        spawn=spawn,
        sleep=_board_answers_late(_real_account_tap(work), events, late)))
    lines = agent.read_tap(_real_account_tap(work))
    assert _BOARD_APPLIED in lines, (
        "the apply line arrived while the daemon was up, so the settle wait"
        " must have been waiting for the missing rx rather than for this")
    assert result["ok"] is False
    assert any("rx" in p for p in result["problems"])


def test_a_settle_that_expires_is_reported_once(tmp_path):
    """The expiry is check()'s sentence to say, and it says it already.

    A problem added here for the timeout would print beside check()'s "no rx
    records" line and describe the same missing record twice, which reads as
    two faults on a desk that has one.
    """
    work = tmp_path / "w"
    events = []
    late = [_BOARD_APPLIED]
    spawn = _spawner(lambda env, attempt: _PROBE_CONSOLE + [_REAL_FRAME],
                     events=events)
    result = agent.run_real_account(work, "auto", _deps(
        spawn=spawn,
        sleep=_board_answers_late(_real_account_tap(work), events, late)))
    assert result["problems"] == [p for p in result["problems"] if "rx" in p]
    assert len(result["problems"]) == 1, result["problems"]


def test_real_account_pass_fails_when_status_wire_prints_no_json(tmp_path):
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account"])
    result = agent.run(args, _deps(
        runner=lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="no board found\n", stderr="")))
    problems = result["scenarios"]["real_account"]["problems"]
    assert any("--wire" in p for p in problems)


# --- reading the transcript back --------------------------------------

def test_read_tap_survives_a_torn_final_line(tmp_path):
    path = tmp_path / "tap.jsonl"
    path.write_text('{"dir": "rx", "t": 1, "msg": {"t": "hello"}}\n'
                    '{"dir": "tx", "t": 2, "ms', encoding="utf-8")
    records = agent.read_tap(path)
    assert [r["dir"] for r in records] == ["rx"]


def test_read_tap_of_a_daemon_that_never_started_is_empty(tmp_path):
    assert agent.read_tap(tmp_path / "missing.jsonl") == []


# --- one run's transcript is never another run's evidence -------------

def _silent():
    """A daemon that writes nothing at all: no board, or a port still held."""
    return _spawner(lambda env, attempt: [])


def test_a_second_run_in_one_workdir_cannot_pass_on_the_first_ones_tap(
        tmp_path):
    """The work root survives between runs and Tap opens the file with "a".

    So without a clear, the second run judges the union of both transcripts:
    a desk whose board was unplugged after a green run reports green forever
    after, from records the daemon under test never wrote.
    """
    d = _scenario(tmp_path, "x")
    work = tmp_path / "work"
    assert agent.run_scenario(d / "x.json", "claude", work, "auto",
                              _deps())["ok"] is True
    second = agent.run_scenario(d / "x.json", "claude", work, "auto",
                                _deps(spawn=_silent()))
    assert second["ok"] is False, (
        "a silent daemon judged against last run's transcript reads as a pass")


def test_preflight_does_not_answer_from_an_earlier_runs_tap(tmp_path):
    """Preflight's whole job is catching "no board attached".

    A leftover tap-1.jsonl answers it before this run's daemon has written a
    byte, which is the one check that cannot be allowed to pass on credit.
    """
    work = tmp_path / "work"
    assert agent.preflight(work, "auto", _deps())[0] is True
    ready, detail = agent.preflight(work, "auto", _deps(spawn=_silent()))
    assert ready is False, f"a leftover transcript answered for the board: {detail}"


def test_a_scenario_judges_only_the_records_of_its_own_run(tmp_path):
    """Belt and braces for a caller that hands run_scenario a used tap.

    The clear above is the fix; this is the second lock on it, so that a
    future caller reusing a work directory cannot resurrect the same bug.
    """
    d = _scenario(tmp_path, "x")
    work = tmp_path / "work" / "x"
    work.mkdir(parents=True)
    (work / "tap.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _tap_lines()), encoding="utf-8")
    outcome = agent.run_scenario(d / "x.json", "claude", tmp_path / "work",
                                 "auto", _deps(spawn=_silent()))
    assert outcome["ok"] is False


# --- what the run is allowed to delete --------------------------------
#
# Every pass empties its own working directory before its daemon starts, so
# the name that directory is built from decides what rmtree touches. It has to
# come from the vetted filename and nowhere else: the JSON `name` is a field
# in a hand-edited file, and --scenarios takes any directory.

def _scenario_file(tmp_path, filename, json_name):
    """A scenario file whose JSON `name` disagrees with its filename."""
    d = tmp_path / "scenarios"
    d.mkdir(exist_ok=True)
    path = d / filename
    path.write_text(json.dumps(
        {"name": json_name, "duration_s": 1,
         "steps": [{"at": 0, "provider": "claude", "session_pct": 50.0}],
         "expect": {"min_tx": 1, "min_board_usage": 1, "min_stale_lines": 0,
                    "min_sleep_wakes": 0}}), encoding="utf-8")
    return path


def test_a_json_name_of_home_cannot_empty_the_runs_shared_home(tmp_path):
    """select_scenarios vets the filename; the delete must use the same value.

    A file called anything at all can carry "name": "home" in its body, and
    the shared sandbox home is a directory of this run's own -- emptied
    mid-run, it takes the cached release manifest and everything else with it.
    """
    path = _scenario_file(tmp_path, "mine.json", "home")
    work = tmp_path / "fleet-work"
    keep = agent.sandbox_home(work) / "keep.txt"
    keep.write_text("the run's own home", encoding="utf-8")

    agent.run_scenario(path, "claude", work, "auto", _deps())

    assert keep.exists(), "the scenario emptied the run's shared home"
    assert (work / "mine" / "tap.jsonl").exists(), (
        "the transcript belongs in a directory named after the vetted file")


def test_a_json_name_that_climbs_out_of_the_work_root_deletes_nothing(
        tmp_path):
    """`..` in a scenario's name is a path traversal into an rmtree."""
    path = _scenario_file(tmp_path, "climber.json", "../results-from-last-week")
    root = tmp_path / "run"
    work = root / "fleet-work"
    work.mkdir(parents=True)                 # run() makes the work root first
    sibling = root / "results-from-last-week"
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_text("last week's evidence", encoding="utf-8")

    agent.run_scenario(path, "claude", work, "auto", _deps())

    assert (sibling / "keep.txt").exists(), (
        "a scenario reached outside the work root and deleted a sibling")


# "." and ".." are absent on purpose: no filesystem will hold a file by those
# names, so a scenario can never arrive as one. The guard still refuses them
# for a caller passing a bare path string, which is what the stem check is.
@pytest.mark.parametrize("filename", ["...json", "..json", "....json", "..."])
def test_a_filename_that_is_not_a_usable_directory_name_is_refused(
        tmp_path, filename):
    """Even the FILENAME needs vetting, because the stem becomes a directory
    that _fresh_work rmtrees.

    Stated as the invariant rather than as one interpreter's parsing, because
    pathlib's is not stable across versions. On 3.12 `...json` has the stem
    `..` -- a traversal wearing a filename. On 3.14 leading dots stopped being
    suffix separators, so the same name has the stem `...json`, which is an
    odd directory name and a perfectly safe one. Asserting the 3.12 answer
    failed on the Ubuntu desk against code that was never wrong.

    So: for every hostile name, either it is refused, or the stem it yields
    is a plain name that cannot reach outside the work root. That holds on
    both, and would still hold if pathlib changed again.
    """
    try:
        path = _scenario_file(tmp_path, filename, "climber")
    except OSError:
        # Windows will not hold a name that is only dots, and strips trailing
        # ones. A scenario can never arrive as a name the filesystem refuses
        # to create, so there is nothing here for the guard to be right or
        # wrong about.
        pytest.skip(f"this filesystem cannot hold a file called {filename!r}")
    stem = Path(filename).stem
    dangerous = not stem.strip(".") or any(c in stem for c in "/\\")

    if dangerous:
        if filename.endswith(".json"):
            # select_scenarios globs *.json, so that is the only shape it can
            # ever be handed. A file called `...` is not a scenario at all and
            # is right to be ignored there.
            with pytest.raises(ValueError):
                agent.select_scenarios(path.parent)
        # And again at the delete itself, which is the thing being protected,
        # for a caller that never went through select_scenarios.
        with pytest.raises(ValueError):
            agent.run_scenario(path, "claude", tmp_path / "w", "auto", _deps())
        return

    # Not dangerous on this interpreter -- but only because of what pathlib
    # made of it, so say what makes it safe rather than merely passing.
    assert ".." not in Path(stem).parts, stem
    assert not os.path.isabs(stem), stem
    work = tmp_path / "w"
    agent.run_scenario(path, "claude", work, "auto", _deps())
    assert (work / stem).resolve().is_relative_to(work.resolve()), (
        f"{filename!r} produced a work directory outside the work root")


# --- the results file -------------------------------------------------

def test_main_writes_the_result_and_exits_by_ok(tmp_path):
    out = tmp_path / "r.json"
    code = agent.main(["--scenarios", str(_scenario(tmp_path)),
                       "--out", str(out)], deps=_deps())
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and doc["ok"] is True
    assert set(doc) >= {"host", "board", "scenarios", "ok"}
