"""The orchestrator that runs the per-host agents, with nothing real behind it.

Not one test here opens an SSH connection, spawns the agent, or touches a
board. Every command the orchestrator would run is answered by an injected
runner, and the state file is written into tmp_path. That is not squeamishness:
this module's whole job is to decide whether a release ships, so the failure
that matters is not "it crashed" but "it said yes when it should have said
no", and only a fake fleet lets a test arrange the ways that could happen.

The four the suite pins down, because each is a release shipped on a lie:

  - A host that never ran counted as a host that passed. An unreachable
    machine, a missing interpreter, a timeout and a malformed result all have
    to arrive as a failed RESULT -- a sentence naming what went wrong -- and
    never as an exception, an empty entry, or a silently shorter host list.

  - A stale result read as a fresh one. The agent writes result.json inside
    the workdir; the workdir persists between runs. If the agent dies before
    writing, the previous run's file is still sitting there, and pulling it
    back would report last week's green run against today's commit. The old
    file is therefore deleted on the remote before the agent is started.

  - A stale .fleet/last_run.json read as this run's verdict. Task 8's gate
    reads that file, so a run that dies halfway must not leave the previous
    green one behind to be found.

  - One desk's failure taking the others' evidence with it. Three desks, one
    board each; a hung SSH to one of them must not stop the other two from
    reporting, and must not stop the file from being written.
"""
import importlib.util
import json
import pathlib
import subprocess
import threading

import pytest

_RUN_PY = (pathlib.Path(__file__).resolve().parents[2]
           / "tools" / "fleet" / "run.py")


def _load():
    """Import run.py by path, not by cwd.

    tools/fleet is not a package and `run` is about as generic a module name
    as exists, so a sys.path.insert("tools/fleet") both depends on where
    pytest was started from and squats a name any dependency might want.
    """
    spec = importlib.util.spec_from_file_location("blink_fleet_run", _RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet_run = _load()


WIN = {"ssh": "galit@lenovo-r90r7u44.lan", "os": "windows",
       "python": "python", "workdir": "%USERPROFILE%\\blink-fleet",
       "board": "codex", "has_claude_desktop": False}
UBUNTU = {"ssh": "kfir@kfir-macbook", "os": "linux",
          "python": "/usr/bin/python3", "workdir": "~/blink-fleet",
          "board": "claude", "has_claude_desktop": False}


def _local(tmp_path):
    return {"ssh": "", "os": "darwin", "python": ".venv-test/bin/python",
            "workdir": str(tmp_path / "wd"), "board": "claude",
            "has_claude_desktop": True}


def _ok_result(scenarios=("overage",)):
    return {"host": "desk", "board": "claude", "ok": True, "problems": [],
            "scenarios": {name: {"ok": True, "problems": []}
                          for name in scenarios}}


class FakeRunner:
    """Answers every command the orchestrator runs, and records it.

    Keyed on the first recognisable word of the command so a test can say
    "the agent fails" or "scp cannot find it" without rebuilding the whole
    sequence. The scp branch writes the file, because a pull that quietly
    left no file behind is one of the failures under test.
    """

    def __init__(self, result=None, agent_code=0, agent_err="", fail=None):
        self.result = _ok_result() if result is None else result
        self.agent_code = agent_code
        self.agent_err = agent_err
        self.fail = fail or {}
        self.calls = []

    def __call__(self, cmd, input=None, cwd=None, timeout=None,
                 capture_output=True, **kw):
        self.calls.append(list(cmd))
        kind = self._kind(cmd)
        if kind in self.fail:
            raise self.fail[kind]
        if kind == "git":
            return subprocess.CompletedProcess(cmd, 0, b"TARBYTES", b"")
        if kind == "scp":
            dest = pathlib.Path(cmd[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(self.result), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if kind == "agent":
            out = self._agent_out(cmd)
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(self.result), encoding="utf-8")
            return subprocess.CompletedProcess(
                cmd, self.agent_code, b"", self.agent_err.encode())
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def _kind(self, cmd):
        line = " ".join(cmd)
        if cmd[0] == "git":
            return "git"
        if cmd[0] == "scp":
            return "scp"
        if "tests.fleet.agent" in line:
            return "agent"
        if "tar" in line:
            return "push"
        return "other"

    def _agent_out(self, cmd):
        """Where a LOCAL agent run would write its result, or None."""
        if cmd[0] == "ssh":
            return None
        out = cmd[cmd.index("--out") + 1]
        return pathlib.Path(self.cwd_of_local_agent) / out

    cwd_of_local_agent = None


# --------------------------------------------------------------------------
# The command lines: the part that cannot be tested against the real fleet
# without an SSH connection, so it is tested against its own text.
# --------------------------------------------------------------------------

def test_windows_line_lets_cmd_expand_the_profile():
    """%USERPROFILE% on that desk is non-ASCII, so it is never expanded here.

    Expanding it locally would mean sending Hebrew through an SSH command
    line whose encoding nothing in the chain agrees on. cmd.exe knows what
    its own profile is called; the line says %USERPROFILE% and lets it.
    """
    argv = fleet_run.agent_argv(WIN)
    cmd = fleet_run.remote_cmd(WIN, argv)
    assert cmd[0] == "ssh" and WIN["ssh"] in cmd
    line = cmd[-1]
    assert "%USERPROFILE%" in line
    assert "$USERPROFILE" not in " ".join(argv)
    assert "$USERPROFILE" not in line
    assert line.startswith("cd /d ")


def test_windows_line_never_starts_with_a_quote():
    """cmd.exe /c eats the outer quotes of a line that opens with one.

    Windows sshd hands the command to `cmd.exe /c <line>`, and cmd's own
    quote-stripping rule rewrites a line that begins with a double quote.
    Every line built here begins with a bare verb -- cd, mkdir, del -- so
    the rule never applies.
    """
    for line in (fleet_run.remote_cmd(WIN, fleet_run.agent_argv(WIN))[-1],
                 fleet_run.push_cmd(WIN)[-1]):
        assert not line.startswith('"')


def test_windows_workdir_written_posix_style_is_still_cmd():
    """An inventory that spells the workdir $USERPROFILE\\... still works."""
    cfg = dict(WIN, workdir="$USERPROFILE\\blink-fleet")
    line = fleet_run.remote_cmd(cfg, fleet_run.agent_argv(cfg))[-1]
    assert "%USERPROFILE%" in line and "$USERPROFILE" not in line


def test_posix_tilde_is_not_quoted_into_a_literal_directory():
    """'~/blink-fleet' in sh is a directory named tilde, not the home one."""
    line = fleet_run.remote_cmd(UBUNTU, fleet_run.agent_argv(UBUNTU))[-1]
    assert "'~/blink-fleet'" not in line
    assert "$HOME" in line


def test_local_host_runs_without_ssh(tmp_path):
    cmd = fleet_run.remote_cmd(_local(tmp_path), ["echo", "hi"])
    assert cmd[0] != "ssh"
    assert cmd == ["echo", "hi"]


def test_ssh_never_waits_for_a_password():
    """BatchMode and a connect timeout: a gate may not sit at a prompt."""
    cmd = fleet_run.remote_cmd(UBUNTU, ["true"])
    assert "BatchMode=yes" in cmd
    assert any(o.startswith("ConnectTimeout=") for o in cmd)


def test_agent_argv_asks_for_a_relative_result_and_a_real_account():
    argv = fleet_run.agent_argv(WIN)
    assert argv[0] == "python" and argv[1:3] == ["-m", "tests.fleet.agent"]
    assert argv[argv.index("--board") + 1] == "codex"
    out = argv[argv.index("--out") + 1]
    assert not pathlib.PurePath(out).is_absolute()
    assert "--real-account" in argv


def test_agent_argv_maps_the_scenario_filter_onto_the_agents_only_flag():
    argv = fleet_run.agent_argv(WIN, scenarios="overage,sleep_wake")
    assert argv[argv.index("--only") + 1] == "overage,sleep_wake"


def test_agent_argv_carries_the_bundles_and_the_version_they_prove():
    argv = fleet_run.agent_argv(WIN, bundle="c.zip", prev_bundle="p.zip",
                                ota_dir="feed", expect_version="1.2.6")
    for flag, value in (("--bundle", "c.zip"), ("--prev-bundle", "p.zip"),
                        ("--ota-dir", "feed"), ("--expect-version", "1.2.6")):
        assert argv[argv.index(flag) + 1] == value


def test_local_python_resolves_against_the_checkout(tmp_path):
    """.venv-test lives in the source tree and is not in the pushed snapshot.

    It is the only interpreter on that Mac with pyserial, and the agent
    spawns a daemon that imports serial at module scope, so the inventory
    names it -- relative to the checkout, which is where it is.
    """
    argv = fleet_run.agent_argv(_local(tmp_path))
    assert pathlib.PurePath(argv[0]).is_absolute()
    assert argv[0].endswith(".venv-test/bin/python")


def test_push_deletes_the_previous_result_before_unpacking():
    """Otherwise a dead agent leaves last run's verdict to be pulled back."""
    for cfg, removal in ((UBUNTU, "rm -f"), (WIN, "del ")):
        line = fleet_run.push_cmd(cfg)[-1]
        assert removal in line
        assert "tar" in line


def test_push_snapshot_pipes_the_archive_it_just_made(tmp_path):
    runner = FakeRunner()
    problems = fleet_run.push_snapshot(UBUNTU, runner)
    assert problems == []
    assert runner.calls[0][:3] == ["git", "archive", "--format=tar"]
    assert runner.calls[1][0] == "ssh"


def test_push_snapshot_reports_a_refused_connection(tmp_path):
    runner = FakeRunner(fail={"push": OSError("no route to host")})
    problems = fleet_run.push_snapshot(UBUNTU, runner)
    assert problems and "no route to host" in " ".join(problems)


# --------------------------------------------------------------------------
# run_host: every way a desk can fail to answer is a result, not a traceback.
# --------------------------------------------------------------------------

def test_unreachable_host_is_a_result_not_a_crash():
    def boom(cmd, **kw):
        raise OSError("no route")
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=boom)
    assert res["ok"] is False
    assert "no route" in " ".join(res["problems"])
    assert res["scenarios"] == {}


def test_a_hung_host_times_out_into_a_result():
    """A hung SSH is the failure mode a release gate cannot afford."""
    runner = FakeRunner(fail={"agent": subprocess.TimeoutExpired("ssh", 60)})
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    joined = " ".join(res["problems"]).lower()
    assert "60" in joined and ("timed out" in joined or "timeout" in joined)


def test_a_missing_interpreter_is_named_with_what_the_shell_said():
    runner = FakeRunner(agent_code=9009,
                        agent_err="'python' is not recognized as an internal"
                                  " or external command")
    res = fleet_run.run_host("galit-win10", WIN, runner=runner)
    assert res["ok"] is False
    assert "not recognized" in " ".join(res["problems"])


def test_a_port_the_agent_could_not_open_arrives_as_the_agents_own_words():
    """The Ubuntu desk today: kfir is not in dialout, so /dev/ttyUSB0 is shut.

    That is a failed run with a cause, not a crashed orchestrator, and the
    cause is a sentence the agent already wrote -- so it has to survive the
    trip home rather than being replaced by "host failed".
    """
    said = ("The board never answered: PermissionError 13 opening"
            " /dev/ttyUSB0. Add this user to the dialout group.")
    runner = FakeRunner(result={"host": "kfir-macbook", "board": "claude",
                                "ok": False, "problems": [said],
                                "scenarios": {}},
                        agent_code=1)
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    assert said in res["problems"]


def test_the_agents_own_exit_1_is_not_repeated_over_its_words():
    """The agent exits 1 to mean "I have failures", and it just listed them.

    An "exited 1" line under every failing desk buries the sentence that
    actually names the cause. Any other code did not come from the agent.
    """
    said = "Scenario overage: nothing reached the board."
    runner = FakeRunner(result={"host": "x", "board": "claude", "ok": False,
                                "problems": [said], "scenarios": {}},
                        agent_code=1)
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    assert res["problems"] == [said]


def test_malformed_result_json_is_a_problem_not_an_exception():
    class Truncated(FakeRunner):
        def __call__(self, cmd, **kw):
            if self._kind(cmd) == "scp":
                self.calls.append(list(cmd))
                pathlib.Path(cmd[-1]).write_text("{\"ok\": tr",
                                                 encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            return super().__call__(cmd, **kw)

    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=Truncated())
    assert res["ok"] is False
    assert "result" in " ".join(res["problems"]).lower()


def test_a_pull_that_brought_nothing_back_is_a_failure():
    runner = FakeRunner(fail={"scp": subprocess.TimeoutExpired("scp", 30)})
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False


def test_a_green_result_with_no_scenarios_is_refused():
    """"Everything passed" from a run that ran nothing is the worst answer."""
    runner = FakeRunner(result={"host": "x", "board": "claude", "ok": True,
                                "problems": [], "scenarios": {}})
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    assert "without running a single scenario" in " ".join(res["problems"])


def test_a_green_result_over_a_failing_scenario_is_refused():
    """PASSED on the same line as "overage FAILED" is worse than either.

    The agent shipped in this snapshot cannot write that -- its own _finish()
    ands the scenarios together -- but this module's premise is refusing
    contradictions rather than resolving them, and this is the same class as
    the two already refused above it.
    """
    runner = FakeRunner(result={
        "host": "x", "board": "claude", "ok": True, "problems": [],
        "scenarios": {"overage": {"ok": False,
                                  "problems": ["Nothing reached the board."]},
                      "sleep_wake": {"ok": True, "problems": []}}})
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    assert "overage" in " ".join(res["problems"])
    assert "sleep_wake" not in " ".join(res["problems"])


def test_a_scenario_verdict_that_is_not_a_verdict_becomes_a_failure():
    """A scenario entry nothing can read must not sail through as a pass.

    It also must not reach the table: format_report calls .get() on every
    scenario value, and an AttributeError there would arrive AFTER the state
    file had already been written green.
    """
    runner = FakeRunner(result={
        "host": "x", "board": "claude", "ok": True, "problems": [],
        "scenarios": {"overage": "yes"}})
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    assert res["scenarios"]["overage"]["ok"] is False
    assert "yes" in " ".join(res["scenarios"]["overage"]["problems"])
    # And the table survives it, which is the half that matters to a person.
    fleet_run.format_report({"sha": "abc1234", "ok": False,
                             "started_at": 0.0, "finished_at": 1.0,
                             "hosts": {"kfir-ubuntu": res}})


def test_a_green_result_from_a_failing_agent_is_refused():
    """Exit code and verdict disagreeing means neither can be trusted."""
    runner = FakeRunner(agent_code=1)
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is False
    assert "exit" in " ".join(res["problems"]).lower()


def test_a_passing_host_comes_back_whole():
    runner = FakeRunner(result=_ok_result(("overage", "sleep_wake")))
    res = fleet_run.run_host("kfir-ubuntu", UBUNTU, runner=runner)
    assert res["ok"] is True
    assert set(res["scenarios"]) == {"overage", "sleep_wake"}
    assert res["problems"] == []


def test_the_local_host_needs_no_ssh_and_no_scp(tmp_path):
    cfg = _local(tmp_path)
    runner = FakeRunner()
    runner.cwd_of_local_agent = cfg["workdir"]
    res = fleet_run.run_host("local-mac", cfg, runner=runner)
    assert res["ok"] is True, res["problems"]
    flat = [" ".join(c) for c in runner.calls]
    assert not any(c.startswith("ssh ") for c in flat)
    assert not any(c.startswith("scp ") for c in flat)


# --------------------------------------------------------------------------
# aggregate and the state file
# --------------------------------------------------------------------------

def test_aggregate_all_green():
    hosts = {"a": {"ok": True, "scenarios": {}}, "b": {"ok": True,
                                                       "scenarios": {}}}
    assert fleet_run.aggregate(hosts)["ok"] is True
    hosts["b"]["ok"] = False
    assert fleet_run.aggregate(hosts)["ok"] is False
    assert fleet_run.aggregate(hosts)["failed"] == ["b"]


def test_aggregate_of_nothing_is_not_a_pass():
    assert fleet_run.aggregate({})["ok"] is False


def test_state_file_carries_the_keys_the_release_gate_reads(tmp_path):
    state = tmp_path / "last_run.json"
    runner = FakeRunner()
    code = fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                           "--state", str(state), "--only", "kfir-ubuntu"],
                          runner=runner)
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert code == 0
    assert set(doc) >= {"sha", "started_at", "finished_at", "ok", "hosts"}
    assert doc["ok"] is True
    assert list(doc["hosts"]) == ["kfir-ubuntu"]
    assert doc["finished_at"] >= doc["started_at"]


def test_one_dead_host_does_not_stop_the_others_reporting(tmp_path):
    """Three desks, three boards: one refusing must cost only its own line."""
    state = tmp_path / "last_run.json"

    class OneDead(FakeRunner):
        def __call__(self, cmd, **kw):
            if "kfir@kfir-macbook" in cmd:
                raise OSError("no route")
            return super().__call__(cmd, **kw)

    runner = OneDead()
    runner.cwd_of_local_agent = str(tmp_path / "wd")
    code = fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                           "--state", str(state)], runner=runner)
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert code == 1 and doc["ok"] is False
    assert set(doc["hosts"]) == {"local-mac", "kfir-ubuntu", "galit-win10"}
    assert doc["hosts"]["local-mac"]["ok"] is True
    assert doc["hosts"]["kfir-ubuntu"]["ok"] is False
    assert doc["hosts"]["galit-win10"]["ok"] is True


def test_a_run_that_dies_does_not_leave_the_last_green_one_behind(tmp_path):
    """A stale green file is indistinguishable from a fresh one to the gate."""
    state = tmp_path / "last_run.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"sha": "old", "ok": True, "hosts": {}}),
                     encoding="utf-8")

    def boom(cmd, **kw):
        if cmd[0] == "git" and cmd[1] == "archive":
            raise OSError("disk is on fire")
        return subprocess.CompletedProcess(cmd, 0, b"deadbeef\n", b"")

    code = fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                           "--state", str(state)], runner=boom)
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert code == 1
    assert doc["ok"] is False and doc["sha"] != "old"


def test_a_subset_run_says_so_in_the_file(tmp_path):
    """The gate must be able to tell a full fleet run from --only local-mac."""
    state = tmp_path / "last_run.json"
    runner = FakeRunner()
    runner.cwd_of_local_agent = str(tmp_path / "wd")
    fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                    "--state", str(state), "--only", "local-mac",
                    "--scenarios", "overage"], runner=runner)
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["only"] == ["local-mac"]
    assert doc["scenarios"] == "overage"


def test_a_dry_run_contacts_nothing(tmp_path, capsys):
    """Reading the cmd.exe line before committing three desks to an hour."""
    state = tmp_path / "last_run.json"
    state.write_text('{"ok": true}', encoding="utf-8")

    def boom(cmd, **kw):
        raise AssertionError(f"A dry run ran {cmd}")

    code = fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                           "--state", str(state), "--dry-run"], runner=boom)
    assert code == 0
    assert state.read_text(encoding="utf-8") == '{"ok": true}'
    out = capsys.readouterr().out
    assert "%USERPROFILE%" in out and "$HOME" in out


def test_an_unknown_host_takes_the_old_verdict_with_it(tmp_path):
    """Exit 2 is still a run that happened, so the old verdict cannot stay.

    A typo in --only that left last week's green file sitting there would
    hand the gate a file the operator believes they just refreshed.
    """
    state = tmp_path / "last_run.json"
    state.write_text('{"sha": "old", "ok": true, "hosts": {}}',
                     encoding="utf-8")
    code = fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                           "--state", str(state), "--only", "nosuchdesk"],
                          runner=FakeRunner())
    assert code == 2
    assert not state.exists()


def test_a_bad_inventory_also_takes_the_old_verdict_with_it(tmp_path):
    state = tmp_path / "last_run.json"
    state.write_text('{"sha": "old", "ok": true, "hosts": {}}',
                     encoding="utf-8")
    bad = tmp_path / "bad.toml"
    bad.write_text('[hosts.a]\nssh = ""\n', encoding="utf-8")
    code = fleet_run.main(["--inventory", str(bad), "--state", str(state)],
                          runner=FakeRunner())
    assert code == 2
    assert not state.exists()


def test_a_table_that_cannot_be_printed_is_not_the_last_word(tmp_path):
    """The verdict is on disk by then; a formatting fault must not hide it.

    Losing the table to a traceback while .fleet/last_run.json sits there
    green is the worst pairing there is for a release gate, so the fallback
    says what was decided and where it was written.
    """
    state = tmp_path / "last_run.json"
    runner = FakeRunner()
    runner.cwd_of_local_agent = str(tmp_path / "wd")

    def boom(doc):
        raise AttributeError("'str' object has no attribute 'get'")

    original = fleet_run.format_report
    fleet_run.format_report = boom
    try:
        code = fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                               "--state", str(state), "--only", "local-mac"],
                              runner=runner)
    finally:
        fleet_run.format_report = original
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["ok"] is True and code == 0


def test_hosts_run_at_the_same_time(tmp_path):
    """One board per desk and no shared state, so three at once or it is slow.

    Serialised, three desks running five scenarios each is most of an hour.
    The barrier proves the threads overlap rather than measuring a clock.
    """
    state = tmp_path / "last_run.json"
    gate = threading.Barrier(3, timeout=10)

    class Barriered(FakeRunner):
        def __call__(self, cmd, **kw):
            if self._kind(cmd) == "agent":
                gate.wait()
            return super().__call__(cmd, **kw)

    runner = Barriered()
    runner.cwd_of_local_agent = str(tmp_path / "wd")
    fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                    "--state", str(state)], runner=runner)
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["ok"] is True, doc["hosts"]


def test_the_table_says_nothing_ran_rather_than_leaving_a_blank(tmp_path):
    lines = fleet_run.format_report({
        "sha": "abc1234", "ok": False, "started_at": 0.0, "finished_at": 1.0,
        "hosts": {"kfir-ubuntu": {"ok": False, "scenarios": {},
                                  "problems": ["The board never answered."]}}})
    text = "\n".join(lines)
    assert "kfir-ubuntu" in text
    assert "Nothing ran" in text
    assert "The board never answered." in text


def test_the_table_never_says_passed_under_a_failing_verdict():
    """Green desks on a commit nobody could read is still not a release.

    The run's own ok is the answer; the desks are only part of how it was
    reached. A table that prints PASSED beside an exit code of 1 teaches the
    reader to trust the wrong one.
    """
    lines = fleet_run.format_report({
        "sha": "unknown", "ok": False, "started_at": 0.0, "finished_at": 1.0,
        "problems": ["This checkout's commit could not be read."],
        "hosts": {"local-mac": {"ok": True, "problems": [],
                                "scenarios": {"overage": {"ok": True}}}}})
    text = "\n".join(lines)
    assert "The fleet passed." not in text
    assert "did NOT pass" in text
    assert "This checkout's commit could not be read." in text


# --------------------------------------------------------------------------
# The inventory
# --------------------------------------------------------------------------

def _inventory(tmp_path):
    """A three-desk inventory shaped like the real one, in tmp_path."""
    path = tmp_path / "fleet.toml"
    path.write_text(
        '[hosts.local-mac]\n'
        'ssh = ""\nos = "darwin"\nboard = "claude"\n'
        'python = ".venv-test/bin/python"\n'
        f'workdir = "{(tmp_path / "wd").as_posix()}"\n'
        'has_claude_desktop = true\n\n'
        '[hosts.kfir-ubuntu]\n'
        'ssh = "kfir@kfir-macbook"\nos = "linux"\nboard = "claude"\n'
        'python = "/usr/bin/python3"\nworkdir = "~/blink-fleet"\n'
        'has_claude_desktop = false\n\n'
        '[hosts.galit-win10]\n'
        'ssh = "galit@lenovo-r90r7u44.lan"\nos = "windows"\nboard = "codex"\n'
        'python = "python"\nworkdir = "%USERPROFILE%\\\\blink-fleet"\n'
        'has_claude_desktop = false\n', encoding="utf-8")
    return path


def test_the_shipped_inventory_holds_the_three_measured_desks():
    hosts = fleet_run.load_inventory(fleet_run.DEFAULT_INVENTORY)
    assert set(hosts) == {"local-mac", "kfir-ubuntu", "galit-win10"}
    assert hosts["local-mac"]["ssh"] == ""
    assert hosts["kfir-ubuntu"]["ssh"] == "kfir@kfir-macbook"
    assert hosts["galit-win10"]["os"] == "windows"
    # Every python named has to be one with pyserial on that machine: the
    # agent spawns the daemon, which imports serial at module scope.
    assert hosts["local-mac"]["python"].endswith(".venv-test/bin/python")
    assert hosts["galit-win10"]["python"] == "python"


def test_the_inventory_names_no_serial_port():
    """Ports are found by USB VID:PID, never by name or by first-port-wins.

    A port hint in the inventory is a foot-gun: it works until a desk is
    replugged into another socket, and then it fails as a dead board.
    """
    hosts = fleet_run.load_inventory(fleet_run.DEFAULT_INVENTORY)
    assert not any("port" in cfg for cfg in hosts.values())
    argv = fleet_run.agent_argv(hosts["galit-win10"])
    assert "--port" not in argv


def test_a_host_missing_a_key_is_refused_with_the_key_named(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[hosts.a]\nssh = ""\nos = "darwin"\n', encoding="utf-8")
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(path)
    assert "board" in str(e.value) and "a" in str(e.value)


def _two_desk_inventory(tmp_path, second_ssh, second_workdir="~/other"):
    path = tmp_path / "dupe.toml"
    path.write_text(
        '[hosts.desk-one]\n'
        'ssh = "kfir@kfir-macbook"\nos = "linux"\nboard = "claude"\n'
        'python = "python3"\nworkdir = "~/blink-fleet"\n'
        'has_claude_desktop = false\n\n'
        '[hosts.desk-two]\n'
        f'ssh = "{second_ssh}"\nos = "linux"\nboard = "claude"\n'
        f'python = "python3"\nworkdir = "{second_workdir}"\n'
        'has_claude_desktop = false\n', encoding="utf-8")
    return path


def test_two_entries_for_one_machine_are_refused_naming_both(tmp_path):
    """One desk written down twice is two agents fighting over one board.

    main() submits every host to the thread pool at once, so the second entry
    is not a slower version of the first: both stop and start the same login
    service, both unpack into the same workdir, and both ask the daemon for
    the one serial port. Whichever loses reports a healthy board as dead.
    """
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(
            _two_desk_inventory(tmp_path, "kfir@kfir-macbook"))
    assert "desk-one" in str(e.value) and "desk-two" in str(e.value)


def test_two_local_desks_are_refused_too(tmp_path):
    """An empty ssh means THIS machine, so two of them are the same machine."""
    path = tmp_path / "local.toml"
    path.write_text(
        '[hosts.mine]\n'
        'ssh = ""\nos = "darwin"\nboard = "claude"\n'
        'python = "python3"\nworkdir = "~/a"\nhas_claude_desktop = true\n\n'
        '[hosts.also-mine]\n'
        'ssh = ""\nos = "darwin"\nboard = "codex"\n'
        'python = "python3"\nworkdir = "~/b"\nhas_claude_desktop = false\n',
        encoding="utf-8")
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(path)
    assert "mine" in str(e.value) and "also-mine" in str(e.value)


def test_the_shipped_inventory_still_loads(tmp_path):
    """The rule above has to be one the fleet as it stands satisfies."""
    assert fleet_run.load_inventory(fleet_run.DEFAULT_INVENTORY)


# --------------------------------------------------------------------------
# The two timeouts, against each other
# --------------------------------------------------------------------------

def test_a_slow_customer_path_cannot_eat_the_whole_desk_budget():
    """The agent's own worst case has to fit INSIDE the orchestrator's wait.

    The customer-path scenarios run first and allow BUNDLE_TIMEOUT_S per
    program call, BUNDLE_CALLS of them. When that product equalled
    AGENT_TIMEOUT_S -- which it exactly did -- a slow update_path could spend
    the entire per-desk budget before the board was asked for anything, and
    this end would report a timed-out desk instead of four board verdicts.
    """
    from tests.fleet import agent

    worst_customer_path = agent.BUNDLE_TIMEOUT_S * agent.BUNDLE_CALLS
    assert fleet_run.AGENT_TIMEOUT_S >= worst_customer_path * 1.5, (
        "the orchestrator has to outlive the bundle scenarios by enough for"
        " the board scenarios to run afterwards")
    # And the per-command cap has to stay outside the update's own self-test
    # allowance (pc/update.py), or it fires first and replaces that command's
    # message with "could not be run at all".
    assert agent.BUNDLE_TIMEOUT_S >= 400
