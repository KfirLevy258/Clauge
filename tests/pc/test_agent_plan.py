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

import pytest

from pc.service_ctl import Outcome
from tests.fleet import agent


class _FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

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


def _spawner(lines_for=lambda env, attempt: _tap_lines()):
    """A stand-in for Popen that writes a tap and hands back a fake process."""
    calls = []

    def spawn(cmd, env, cwd):
        calls.append({"cmd": cmd, "env": env, "cwd": cwd})
        with open(env["BLINK_TAP"], "a", encoding="utf-8") as f:
            for rec in lines_for(env, len(calls)):
                f.write(json.dumps(rec) + "\n")
        return _FakeProc()

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
                now=_clock(),
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
                      "min_stale_lines": 0, "quiet_window_s": 0}}
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


# --- grace that scales with what the scenario is waiting for ----------

def test_grace_grows_with_the_quiet_window_and_the_poll_interval():
    plain = {"expect": {"quiet_window_s": 0}}
    sleeper = {"expect": {"quiet_window_s": 35}}
    assert agent.grace_s(sleeper, 3.0) > agent.grace_s(plain, 3.0)
    assert agent.grace_s(plain, 6.0) > agent.grace_s(plain, 3.0)
    assert agent.grace_s({}, 3.0) == agent.grace_s(plain, 3.0)


def test_a_scenario_that_waits_gets_time_to_finish_waiting(tmp_path):
    """The wait is duration plus this scenario's own grace, not a constant.

    sleep_wake spends 40 of its 60 seconds deliberately silent, so the frame
    that proves the wake path is the last one to arrive. A fixed grace makes
    that scenario the first to go flaky on a slow desk, and a flaky scenario
    inside a release gate teaches people to re-run until green.
    """
    doc = {"duration_s": 60,
           "expect": {"min_tx": 1, "min_board_usage": 1,
                      "min_stale_lines": 0, "quiet_window_s": 35}}
    d = _scenario(tmp_path, "waits", **doc)
    sleeps = []
    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json")])
    agent.run(args, _deps(sleeps=sleeps))
    assert max(sleeps) == 60 + agent.grace_s(doc, agent.POLL_INTERVAL_S)


def test_the_poll_interval_reaches_both_the_child_and_the_grace(tmp_path):
    spawn = _spawner()
    doc = {"duration_s": 10, "expect": {"min_tx": 1, "min_board_usage": 1,
                                        "min_stale_lines": 0,
                                        "quiet_window_s": 0}}
    d = _scenario(tmp_path, "x", **doc)
    sleeps = []
    args = agent.parse_args(["--scenarios", str(d),
                             "--out", str(tmp_path / "r.json"),
                             "--poll-interval", "7"])
    agent.run(args, _deps(sleeps=sleeps, spawn=spawn))
    assert all(c["env"]["BLINK_POLL_INTERVAL_S"] == "7.0" for c in spawn.calls)
    assert max(sleeps) == 10 + agent.grace_s(doc, 7.0)


# --- the real-account pass --------------------------------------------

def test_real_account_pass_wants_a_percentage_and_a_parseable_wire(tmp_path):
    args = agent.parse_args(["--scenarios", str(_scenario(tmp_path)),
                             "--out", str(tmp_path / "r.json"),
                             "--real-account"])
    result = agent.run(args, _deps())
    assert result["scenarios"]["real_account"]["ok"] is True


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


# --- the results file -------------------------------------------------

def test_main_writes_the_result_and_exits_by_ok(tmp_path):
    out = tmp_path / "r.json"
    code = agent.main(["--scenarios", str(_scenario(tmp_path)),
                       "--out", str(out)], deps=_deps())
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and doc["ok"] is True
    assert set(doc) >= {"host", "board", "scenarios", "ok"}
