"""Execution state, derived from events Claude Code already announces.

Two halves: the pure event->state rule, and the directory scan that turns many
sessions and their agents into the handful of numbers the wire can carry.
"""
import json
import os
import subprocess
import sys

import pytest

from pc.providers import base, claude_state
from pc.providers.claude_state import (ABANDONED_AFTER_S,
                                       AGENT_ABANDONED_AFTER_S,
                                       ClaudeStateProvider, derive_state,
                                       worst_of)

NOW = 1_787_700_000.0


# --- the rule -------------------------------------------------------------


def test_a_started_turn_is_running():
    for e in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        assert derive_state(e, 2.0) == base.STATE_RUNNING, e


def test_interrupt_is_a_finished_turn_not_an_unknown_event():
    """Codex fires Interrupt where Claude Code has no equivalent event.

    Idle rather than running, on evidence: in the only real interactive
    session anyone has captured, a refused approval produced Interrupt and
    then nothing at all -- no Stop ever followed. So the turn is over and it
    is the person's turn again, which is what idle means here. Calling it
    running would leave the panel saying "Working" for an hour over a session
    that has stopped.

    And landing on unknown, which is what happens today, is worse than either:
    unknown drops the session out of the census, so interrupting a Codex turn
    makes its pip disappear rather than merely mislabelling it.
    """
    assert derive_state("Interrupt", 1.0) == base.STATE_IDLE


def test_interrupt_does_not_decay_to_stuck_from_silence():
    """A second, independent fact from the test above: Interrupt shares
    Stop's *lack* of an age test, not just its target state. A
    plausible bug this catches on its own: a version that recognises
    Interrupt only for a few seconds (say, to model "just aborted") and
    falls through to unknown once the silence stretches. The rule here is
    the same as for every other finished turn -- however long ago it ended,
    it is still ended; silence after a turn is the expected condition, not a
    fault to escalate."""
    assert derive_state("Interrupt", 40 * 60.0) == base.STATE_IDLE


def test_an_opened_session_claims_nothing_and_never_becomes_stuck():
    """`claude`, then nothing: the person is reading, or opened it for later.
    Filed as running, this went red after three minutes and stayed red for
    an hour -- on the most ordinary thing a terminal does. Filed as idle,
    it would now paint the "your turn" amber for a terminal nobody has asked
    anything of. It says nothing."""
    assert derive_state("SessionStart", 2.0) == base.STATE_UNKNOWN
    assert derive_state("SessionStart", 3000.0) == base.STATE_UNKNOWN


def test_a_slot_with_a_nonsense_timestamp_is_ignored(tmp_path):
    """NaN compares false with everything, so neither the stuck test nor the
    abandoned sweep would ever fire: a permanent 'running' nothing collects."""
    d = tmp_path / "state"
    d.mkdir()
    for bad in ("NaN", "Infinity", "1787700000000", "12"):
        (d / "x.state").write_text('{"event":"PreToolUse","t":%s}' % bad)
        counts, _, _ = ClaudeStateProvider(path=str(d)).scan(NOW)
        assert counts == {}, bad


def test_a_completed_turn_is_idle():
    assert derive_state("Stop", 2.0) == base.STATE_IDLE


def test_an_ended_session_is_not_waiting_on_anyone():
    """idle means "finished, read me". A session that is over has nothing to
    read; it must not hold the amber light for the next hour."""
    assert derive_state("SessionEnd", 2.0) == base.STATE_UNKNOWN


def test_a_notification_is_waiting_not_stuck():
    """Notification is the event Claude Code fires when it needs a human --
    authoritative, where 'is the process blocked on stdin' was a guess."""
    assert derive_state("Notification", 2.0) == base.STATE_WAITING
    assert derive_state("PermissionRequest", 2.0) == base.STATE_WAITING


def test_an_api_error_is_its_own_state():
    """StopFailure carries error: 'rate_limit' among its causes, and on a
    usage gauge that is the headline, not a detail."""
    assert derive_state("StopFailure", 2.0) == base.STATE_FAILED


def test_a_failed_turn_does_not_decay_into_stuck():
    """It is not wedged. It is finished and unsuccessful."""
    assert derive_state("StopFailure", 3000.0) == base.STATE_FAILED


def test_a_person_taking_their_time_is_still_waiting():
    assert derive_state("Notification", 3000.0) == base.STATE_WAITING


def test_a_completed_turn_stays_idle_however_long_the_silence():
    assert derive_state("Stop", 3000.0) == base.STATE_IDLE


def test_a_running_turn_stays_running_however_long_it_is_silent():
    """No `stuck` from silence. 60 s cried wolf on a test suite, 180 s on a
    nine-minute polling loop, 600 s on a seventeen-minute think with the API
    connection open the whole time (all 2026-08-29). The hooks cannot tell a
    long turn from a wedged one, so the daemon does not guess; red is kept
    for `failed`, which is an event."""
    for age in (90.0, 9 * 60.0, 10 * 60.0 + 1, 50 * 60.0):
        assert derive_state("PreToolUse", age) == base.STATE_RUNNING
    assert derive_state("PostToolUse", 40 * 60.0) == base.STATE_RUNNING


def test_an_abandoned_session_says_nothing_rather_than_idle():
    assert derive_state("Stop", ABANDONED_AFTER_S + 1) == base.STATE_UNKNOWN


def test_an_unknown_event_says_nothing():
    assert derive_state("SomethingNew", 1.0) == base.STATE_UNKNOWN


def test_a_clock_that_went_backwards_reads_as_fresh():
    """A negative age is clamped to zero rather than allowed to sail under
    every threshold -- so it is the running state a fresh event earns, not a
    confident 'stuck' or 'abandoned' from a broken measurement."""
    assert derive_state("PreToolUse", -5000.0) == base.STATE_RUNNING


# --- collapsing many sessions to one indicator ----------------------------


def test_worst_of_ranks_a_rate_limit_above_everything():
    """A rate limit is the one condition this gauge exists to surface; a
    wedged tool is a distant second."""
    assert worst_of({base.STATE_FAILED, base.STATE_STUCK,
                     base.STATE_RUNNING}) == base.STATE_FAILED


def test_worst_of_ranks_stuck_above_waiting_above_idle_above_running():
    assert worst_of({base.STATE_STUCK, base.STATE_WAITING}) == base.STATE_STUCK
    assert worst_of({base.STATE_WAITING, base.STATE_RUNNING}) == base.STATE_WAITING
    assert worst_of({base.STATE_WAITING, base.STATE_IDLE}) == base.STATE_WAITING
    assert worst_of({base.STATE_IDLE}) == base.STATE_IDLE


def test_one_finished_session_shows_through_any_number_of_running_ones():
    """The light is a claim on the person. A finished answer is waiting on
    them; the sessions still working are not. Ranking running above idle
    (the old order) hid the finished one behind a green pulse for as long as
    anything else was busy (user decision 2026-08-29)."""
    assert worst_of({base.STATE_RUNNING, base.STATE_IDLE}) == base.STATE_IDLE
    assert worst_of({base.STATE_RUNNING, base.STATE_RUNNING,
                     base.STATE_IDLE}) == base.STATE_IDLE


def test_worst_of_nothing_is_nothing():
    assert worst_of(set()) == base.STATE_UNKNOWN


# --- the directory --------------------------------------------------------


def write_session(d, sid, event, t, name=None, pid=None):
    payload = {"event": event, "t": t}
    if name is not None:
        payload["name"] = name
    if pid is not None:
        payload["pid"] = pid
    (d / f"{sid}.state").write_text(json.dumps(payload))


def add_agent(d, sid, aid, t=None):
    sd = d / sid
    sd.mkdir(exist_ok=True)
    p = sd / aid
    p.write_text("")
    if t is not None:
        os.utime(p, (t, t))


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def provider(state_dir, **kw):
    return ClaudeStateProvider(path=str(state_dir), **kw)


def test_no_directory_at_all_is_silence(tmp_path):
    assert ClaudeStateProvider(path=str(tmp_path / "nope")).poll(NOW) == []


def test_an_empty_directory_is_silence(state_dir):
    assert provider(state_dir).poll(NOW) == []


def test_one_running_session(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.state == base.STATE_RUNNING
    assert (f.n_run, f.n_sessions()) == (1, 1)


def test_two_sessions_do_not_overwrite_each_other(state_dir):
    """The whole reason for one file per session. A single global slot showed
    whichever fired most recently and silently misreported the other."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    write_session(state_dir, "s2", "Notification", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_sessions() == 2
    assert (f.n_run, f.n_wait) == (1, 1)
    assert f.state == base.STATE_WAITING     # worst of the two


def test_the_indicator_shows_the_worst_session(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    write_session(state_dir, "s2", "Stop", NOW - 5)
    write_session(state_dir, "s3", "StopFailure", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.state == base.STATE_FAILED
    assert f.n_sessions() == 3


def test_agents_are_counted_exactly(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    add_agent(state_dir, "s1", "a1")
    add_agent(state_dir, "s1", "a2")
    add_agent(state_dir, "s1", "a3")
    assert provider(state_dir).poll(NOW)[0].n_agents == 3


def test_agents_are_summed_across_sessions(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    write_session(state_dir, "s2", "PreToolUse", NOW - 5)
    add_agent(state_dir, "s1", "a1")
    add_agent(state_dir, "s2", "b1")
    add_agent(state_dir, "s2", "b2")
    assert provider(state_dir).poll(NOW)[0].n_agents == 3


def test_a_session_with_no_agents_counts_zero(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    assert provider(state_dir).poll(NOW)[0].n_agents == 0


def test_an_abandoned_agent_is_swept_not_counted_forever(state_dir):
    """Its SubagentStop never fired because the session died mid-flight.
    A panel that says '3 agents' forever is worse than one that says
    nothing."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    add_agent(state_dir, "s1", "live")
    # Older than the SESSION threshold but younger than the agent one: an
    # agent file's mtime is its start, and a long run is not a dead one.
    add_agent(state_dir, "s1", "long", t=NOW - ABANDONED_AFTER_S - 100)
    add_agent(state_dir, "s1", "dead", t=NOW - AGENT_ABANDONED_AFTER_S - 100)
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_agents == 2
    assert not (state_dir / "s1" / "dead").exists()
    assert (state_dir / "s1" / "long").exists()
    assert (state_dir / "s1" / "live").exists()


def test_an_abandoned_session_is_swept_entirely(state_dir):
    """A killed terminal leaves files and nothing else will collect them."""
    write_session(state_dir, "s1", "PreToolUse", NOW - ABANDONED_AFTER_S - 100)
    add_agent(state_dir, "s1", "a1")
    assert provider(state_dir).poll(NOW) == []
    assert not (state_dir / "s1.state").exists()
    assert not (state_dir / "s1").exists()


def test_sweeping_can_be_turned_off(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - ABANDONED_AFTER_S - 100)
    provider(state_dir, sweep=False).poll(NOW)
    assert (state_dir / "s1.state").exists()


def test_a_live_session_is_never_swept(state_dir):
    write_session(state_dir, "s1", "Stop", NOW - 5)
    provider(state_dir).poll(NOW)
    assert (state_dir / "s1.state").exists()


def test_a_malformed_session_file_is_skipped_not_fatal(state_dir):
    (state_dir / "bad.state").write_text("{not json")
    write_session(state_dir, "good", "PreToolUse", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_sessions() == 1


def test_a_session_file_without_a_timestamp_is_skipped(state_dir):
    (state_dir / "s1.state").write_text(json.dumps({"event": "PreToolUse"}))
    assert provider(state_dir).poll(NOW) == []


def test_stray_files_in_the_directory_are_ignored(state_dir):
    (state_dir / "README").write_text("hello")
    (state_dir / "s1.state").write_text(json.dumps(
        {"event": "PreToolUse", "t": NOW - 5}))
    assert provider(state_dir).poll(NOW)[0].n_sessions() == 1


def test_name_is_carried_when_one_session_holds_the_state(tmp_path):
    write_session(tmp_path, "s1", "Notification", NOW, name="LiveClaudeUi")
    prov = ClaudeStateProvider(path=str(tmp_path))
    frame = prov.poll(NOW)[0]
    assert frame.state == base.STATE_WAITING
    assert frame.label == "LiveClaudeUi"


def test_no_name_when_two_sessions_share_the_state(tmp_path):
    write_session(tmp_path, "s1", "Notification", NOW, name="Blink")
    write_session(tmp_path, "s2", "Notification", NOW, name="Other")
    prov = ClaudeStateProvider(path=str(tmp_path))
    frame = prov.poll(NOW)[0]
    assert frame.n_wait == 2
    assert frame.label == ""


def test_no_name_when_two_hold_the_state_but_only_one_is_named(tmp_path):
    # Only one of the two waiting sessions named itself. The guard is
    # `counts.get(state, 0) == 1`, not `len(held) == 1` -- a simplification
    # to the latter alone would leak this one name onto a two-holder frame.
    write_session(tmp_path, "s1", "Notification", NOW, name="Named")
    write_session(tmp_path, "s2", "Notification", NOW)
    prov = ClaudeStateProvider(path=str(tmp_path))
    frame = prov.poll(NOW)[0]
    assert frame.n_wait == 2
    assert frame.label == ""


def test_name_comes_from_the_winning_state_not_another(tmp_path):
    # One waiting, two running. `waiting` wins, and the name must be the
    # waiting session's -- not a running one's, and not absent because the
    # runners are plural.
    write_session(tmp_path, "s1", "Notification", NOW, name="Waiter")
    write_session(tmp_path, "s2", "PreToolUse", NOW, name="RunnerA")
    write_session(tmp_path, "s3", "PreToolUse", NOW, name="RunnerB")
    prov = ClaudeStateProvider(path=str(tmp_path))
    frame = prov.poll(NOW)[0]
    assert frame.state == base.STATE_WAITING
    assert frame.label == "Waiter"


def test_state_file_without_a_name_is_normal(tmp_path):
    # Written by a shim older than this feature. Absent is not malformed.
    write_session(tmp_path, "s1", "Notification", NOW)
    prov = ClaudeStateProvider(path=str(tmp_path))
    frame = prov.poll(NOW)[0]
    assert frame.state == base.STATE_WAITING
    assert frame.label == ""


def test_a_non_string_name_is_ignored_not_fatal(tmp_path):
    path = tmp_path / "s1.state"
    path.write_text(json.dumps({"event": "Notification", "t": NOW,
                                "name": {"not": "a string"}}))
    prov = ClaudeStateProvider(path=str(tmp_path))
    frame = prov.poll(NOW)[0]
    assert frame.label == ""


def test_this_source_carries_no_usage_percentage(state_dir):
    """It contributes execution fields. It must never be able to make a panel
    look fresher than its numbers are, which is why merge() cannot pick it as
    the primary source."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.session_pct == base.UNKNOWN
    assert f.weekly_pct == base.UNKNOWN
    assert f.has_usage() is False


# --- process liveness -----------------------------------------------------
#
# The bug: a session that dies without firing SessionEnd -- a closed terminal,
# a crash, kill -9 -- showed as live for the full ABANDONED_AFTER_S hour. The
# fix is not a shorter silence threshold (every value cried wolf; see the
# module docstring) but a fact -- the hook records the pid it ran from, and a
# pid that names no process is not thinking.


@pytest.fixture(autouse=True)
def _pid_trust():
    """The latch is process-lifetime by design, so it MUST be reset around
    every test here. Without this, the first test that trips it silently
    disarms the liveness checks in every test that runs after it, and this
    branch has already shipped nine tests that could not fail."""
    claude_state.reset_pid_liveness()
    yield
    claude_state.reset_pid_liveness()


@pytest.fixture
def dead():
    """A factory for pids that are reliably gone, not hoped to be free.

    Spawn the shortest possible child, wait for it, and the wait REAPS it: a
    reaped child is not a zombie, so the kernel no longer holds the number and
    os.kill raises ProcessLookupError. That is a measured fact about this
    machine at this instant, where a hardcoded 999999 is a guess about
    somebody else's machine, and where spawn-then-kill races the child's own
    exit and its reaping.

    The one hole is pid reuse in the microseconds between the wait and the
    check below, so the factory VERIFIES deadness rather than trusting the
    reasoning: if the number ever comes back alive, the tests that depend on
    it say so loudly instead of passing vacuously.
    """
    def _dead():
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        if claude_state._PID_LIVENESS_AVAILABLE:
            assert claude_state._process_is_gone(proc.pid), (
                f"pid {proc.pid} was reused between reaping and this check;"
                " the liveness tests using it would have been meaningless")
        return proc.pid
    return _dead


# Not skipped for tidiness: on Windows CPython's os.kill(pid, 0) is
# TerminateProcess(handle, 0), so the probe is switched off there and there is
# nothing to assert. The other half --  that switching it off leaves today's
# behaviour intact -- is asserted by a test that runs everywhere.
posix_only = pytest.mark.skipif(
    not claude_state._PID_LIVENESS_AVAILABLE,
    reason="pid liveness is POSIX-only; os.kill(pid, 0) kills on Windows")


@posix_only
def test_a_fresh_slot_whose_process_is_alive_is_live(state_dir):
    """The pid of this very test run: alive, by construction."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5, pid=os.getpid())
    f = provider(state_dir).poll(NOW)[0]
    assert f.state == base.STATE_RUNNING
    assert f.n_run == 1
    assert claude_state.pid_liveness_trusted() is True


@posix_only
def test_a_session_whose_process_is_gone_drops_out_at_once(state_dir, dead):
    """The bug, in one test. Five minutes after a kill -9 the panel still
    showed this session as running, and would have for another fifty-five."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 300, pid=dead())
    assert provider(state_dir).poll(NOW) == []


@posix_only
def test_a_dead_process_need_not_be_the_only_session(state_dir, dead):
    """It has to leave the counts, not just the headline: a session that no
    longer exists cannot contribute to "2 running"."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 300, pid=os.getpid())
    write_session(state_dir, "s2", "PreToolUse", NOW - 300, pid=dead())
    f = provider(state_dir).poll(NOW)[0]
    assert (f.n_run, f.n_sessions()) == (1, 1)


@posix_only
def test_the_agents_of_a_dead_session_are_not_counted_either(state_dir, dead):
    """Their SubagentStop never fired for the same reason its SessionEnd did
    not. Counting them moves the bug from the light to the number."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 300, pid=dead())
    add_agent(state_dir, "s1", "a1")
    assert provider(state_dir).poll(NOW) == []


def test_a_slot_with_no_pid_at_all_behaves_exactly_as_today(state_dir):
    """The ordinary case for months: a customer runs the shim from whichever
    version they installed. An absent key is not malformed, and it is not a
    reason to drop anything -- or to keep it past the hour."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 300)
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    write_session(state_dir, "s2", "PreToolUse", NOW - ABANDONED_AFTER_S - 1)
    assert provider(state_dir).poll(NOW)[0].n_sessions() == 1


@posix_only
def test_a_malformed_or_out_of_range_pid_falls_back_to_todays_rules(state_dir):
    """A pid this code cannot make sense of is evidence of nothing. Note 0 and
    -1 especially: os.kill reads those as "this whole process group" and
    "every process I am allowed to signal", so they are refused before the
    syscall rather than passed to it."""
    for bad in ('"1234"', "0", "-1", "-99", "1e9", "12.5", "true", "false",
                "null", "99999999999999999999", '{"pid":1}', "[1]", '""'):
        (state_dir / "s1.state").write_text(
            '{"event":"PreToolUse","t":%r,"pid":%s}' % (NOW - 300, bad))
        counts, _, _ = provider(state_dir).scan(NOW)
        assert counts == {base.STATE_RUNNING: 1}, bad
        assert claude_state.pid_liveness_trusted() is True, bad


@posix_only
def test_a_process_owned_by_somebody_else_is_alive_not_gone(monkeypatch,
                                                            state_dir):
    """os.kill raises PermissionError for a process this user may not signal,
    and that is proof it EXISTS. Reading it as death would drop a session for
    running under another account -- the false-death direction this feature is
    not allowed to fail in."""
    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(claude_state.os, "kill", denied)
    write_session(state_dir, "s1", "PreToolUse", NOW - 300, pid=4242)
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    assert claude_state.pid_liveness_trusted() is True


def test_a_platform_without_a_safe_probe_keeps_todays_rules(monkeypatch,
                                                            state_dir, dead):
    """Windows: CPython's os.kill(pid, 0) is TerminateProcess(handle, 0), so
    asking whether Claude Code is alive would kill it. The probe is switched
    off there, the hour is the only rule, and no latch fires either -- there
    was never a reading to distrust."""
    monkeypatch.setattr(claude_state, "_PID_LIVENESS_AVAILABLE", False)
    write_session(state_dir, "s1", "PreToolUse", NOW - 300, pid=dead())
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    write_session(state_dir, "s1", "PreToolUse", NOW - 3, pid=dead())
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    assert claude_state.pid_liveness_trusted() is True


# --- the latch: the feature testing its own premise -----------------------


@posix_only
def test_a_fresh_slot_with_a_dead_pid_latches_the_feature_off(state_dir, dead,
                                                              capsys):
    """THE CENTREPIECE. Nobody has measured whether $PPID in the hook is
    Claude Code's own process or a shell it spawned to run the hook. If it is
    the shell, every LIVE session has a dead pid and the naive feature blanks
    the panel -- worse than the bug it fixes.

    A slot written three seconds ago cannot have come from a process that no
    longer exists. That combination is proof the number does not mean what
    this code hopes, so: keep the session, stop believing pids, and say so."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 3, pid=dead())
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_run == 1                        # kept, not dropped
    assert claude_state.pid_liveness_trusted() is False
    err = capsys.readouterr().err
    assert "pid liveness is DISABLED" in err
    assert "3600s as before" in err


@posix_only
def test_once_latched_a_genuinely_dead_session_is_not_dropped_early(state_dir,
                                                                    dead):
    """The fail-safe half. After the latch, BLINK behaves exactly as it does
    today -- an hour, plus a log line saying why -- rather than acting on a
    number it has just proved it cannot read."""
    write_session(state_dir, "fresh", "PreToolUse", NOW - 3, pid=dead())
    write_session(state_dir, "gone", "PreToolUse", NOW - 300, pid=dead())
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_sessions() == 2
    # ...and the hour still collects it, which is the whole of today's rules.
    assert provider(state_dir).poll(NOW + ABANDONED_AFTER_S) == []


@posix_only
def test_the_latch_message_is_printed_once_not_every_poll(state_dir, dead,
                                                          capsys):
    """The scan runs every two seconds. A per-poll complaint is 43,200 lines a
    day in the daemon's log, which is how a real message gets ignored."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 3, pid=dead())
    prov = provider(state_dir)
    for _ in range(5):
        prov.poll(NOW)
    assert capsys.readouterr().err.count("pid liveness is DISABLED") == 1


@posix_only
def test_a_second_provider_inherits_the_latch(state_dir, dead, capsys):
    """What $PPID means is a fact about this machine's Claude Code, not about
    a directory: `blink status` builds its own short-lived provider, and it
    must not re-learn -- or re-print -- what the daemon already established."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 3, pid=dead())
    provider(state_dir).poll(NOW)
    capsys.readouterr()
    provider(state_dir).poll(NOW)
    assert claude_state.pid_liveness_trusted() is False
    assert capsys.readouterr().err == ""


# --- and how it comes back --------------------------------------------------
#
# The suspension used to be one-way. It fires on an ordinary event -- a
# terminal closed within FRESH_SLOT_S of its last hook event gets no
# SessionEnd, because SIGHUP fires none, so the slot survives fresh with a
# dead pid -- and the daemon it disabled is a launchd service that runs for
# weeks. The premise it claimed to have disproved has since been MEASURED on
# this machine: $PPID in the hook is `claude` itself on every live slot.


@posix_only
def test_a_living_pid_takes_the_suspension_back(state_dir, dead):
    """The evidence that outranks the suspension, because it is the same
    question answered the strong way: a pid out of this very directory
    resolving in this very process table. One closed terminal must not
    disable the feature until the next reboot."""
    write_session(state_dir, "closed", "PreToolUse", NOW - 3, pid=dead())
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    assert claude_state.pid_liveness_trusted() is False

    # Somebody opens a terminal. Its slot carries a pid that is alive.
    write_session(state_dir, "open", "PreToolUse", NOW - 300, pid=os.getpid())
    prov = provider(state_dir)
    prov.poll(NOW)
    assert claude_state.pid_liveness_trusted() is True

    # ...and the feature works again on the next pass: the closed session,
    # now well past FRESH_SLOT_S, is dropped rather than held for the hour.
    f = prov.poll(NOW + 60)
    assert (f[0].n_run, f[0].n_sessions()) == (1, 1)


@posix_only
@pytest.mark.parametrize("order", [
    ["closed.state", "open.state"],
    ["open.state", "closed.state"],
])
def test_one_poll_gives_one_answer_whatever_order_the_directory_comes_back_in(
        state_dir, dead, monkeypatch, order):
    """The Linux failure, pinned so it cannot come back on either platform.

    A fresh slot with a dead pid suspends trust; a slot with a live pid
    restores it. Both were applied file by file in os.listdir order, so
    whichever the kernel handed back LAST decided -- and it differs by
    platform. On the Ubuntu desk listdir returned open.state then
    closed.state and this came back False; macOS returns the other order and
    the same code passed. Three tests here failed on Linux and nowhere else
    for exactly this reason.

    A live pid was present in this poll either way, so trust holds either
    way.
    """
    write_session(state_dir, "closed", "PreToolUse", NOW - 3, pid=dead())
    write_session(state_dir, "open", "PreToolUse", NOW - 300, pid=os.getpid())

    real = os.listdir

    def fixed(path):
        entries = real(path)
        if set(order) <= set(entries):
            return order + [e for e in entries if e not in order]
        return entries

    monkeypatch.setattr(claude_state.os, "listdir", fixed)
    provider(state_dir).poll(NOW)
    assert claude_state.pid_liveness_trusted() is True


@posix_only
@pytest.mark.parametrize("order", [
    ["fresh.state", "gone.state"],
    ["gone.state", "fresh.state"],
])
def test_a_suspending_poll_keeps_every_session_whatever_the_order(
        state_dir, dead, monkeypatch, order):
    """The other half of the same bug, and the one that showed on the desk.

    `fresh` suspends the feature; `gone` is what the feature would have
    dropped. Read fresh-then-gone, the suspension is already in force and gone
    is kept -- which is the documented fail-safe. Read the other way, gone was
    judged while trust still held and vanished off the panel.

    One poll cannot both trust pids and not trust them. It settles the
    question first, so both sessions survive either way.
    """
    write_session(state_dir, "fresh", "PreToolUse", NOW - 3, pid=dead())
    write_session(state_dir, "gone", "PreToolUse", NOW - 300, pid=dead())

    real = os.listdir

    def fixed(path):
        entries = real(path)
        if set(order) <= set(entries):
            return order + [e for e in entries if e not in order]
        return entries

    monkeypatch.setattr(claude_state.os, "listdir", fixed)
    assert provider(state_dir).poll(NOW)[0].n_sessions() == 2
    assert claude_state.pid_liveness_trusted() is False


@posix_only
def test_the_explanation_is_printed_once_even_across_a_recovery(state_dir,
                                                                dead, capsys):
    """A suspension that can come back can also happen again, and the poll is
    every two seconds. The trust flag stops being the thing that remembers
    whether the log line was written, so it needs its own."""
    write_session(state_dir, "closed", "PreToolUse", NOW - 3, pid=dead())
    provider(state_dir).poll(NOW)
    write_session(state_dir, "open", "PreToolUse", NOW - 300, pid=os.getpid())
    provider(state_dir).poll(NOW)
    assert claude_state.pid_liveness_trusted() is True

    # That terminal closes -- its slot goes with it. Without this the
    # directory would still hold a pid that resolves, and a poll that can
    # SEE one is not allowed to doubt that pids resolve; the suspension
    # below would never fire, and this test would be checking nothing.
    (state_dir / "open.state").unlink()

    write_session(state_dir, "closed2", "PreToolUse", NOW - 3, pid=dead())
    provider(state_dir).poll(NOW)
    assert claude_state.pid_liveness_trusted() is False
    assert capsys.readouterr().err.count("pid liveness is DISABLED") == 1


@posix_only
def test_a_poll_that_can_see_a_living_pid_does_not_doubt_that_pids_resolve(
        state_dir, dead):
    """The rule that makes the order stop mattering, stated on its own.

    The suspension doubts one thing: whether the pid a hook records names the
    session's process on this machine. A slot in the same directory whose pid
    resolves in this process table answers that in the strong direction, in
    the same poll -- and a living pid was already documented to outrank a
    suspension. All that changes is that it no longer has to be read second
    to win.
    """
    write_session(state_dir, "open", "PreToolUse", NOW - 300, pid=os.getpid())
    write_session(state_dir, "closed", "PreToolUse", NOW - 3, pid=dead())
    provider(state_dir).poll(NOW)
    assert claude_state.pid_liveness_trusted() is True


@posix_only
def test_the_message_no_longer_diagnoses_what_it_cannot_know(state_dir, dead,
                                                             capsys):
    """It used to state as fact that "the hook's pid is not the session's own
    process", which is measurably false on this machine. Three causes produce
    this observation and the log must not name one of them."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 3, pid=dead())
    provider(state_dir).poll(NOW)
    err = capsys.readouterr().err
    assert "is not the session's own process" not in err
    assert "until one proves live" in err


@posix_only
def test_a_clock_that_is_wildly_ahead_decides_nothing(state_dir, dead):
    """A slot stamped an hour into the future gives a negative age, and a
    negative number is less than ten -- so it read as FRESH and one NTP step
    could suspend pid liveness for the life of the daemon.

    An age that broken is not a measurement, so nothing is taken from it:
    the session is kept, exactly as today's rules would keep it, and trust is
    left where it was."""
    write_session(state_dir, "s1", "PreToolUse", NOW + 3600, pid=dead())
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    assert claude_state.pid_liveness_trusted() is True


@posix_only
def test_an_ordinary_clock_skew_still_counts_as_a_fresh_slot(state_dir, dead):
    """The other side of that boundary, and the reason it is not simply
    `0 <= age_s`. A slot stamped three seconds ahead was written three
    seconds ago by a clock that runs fast; calling it stale would drop a live
    session off the panel, which is the one direction this feature is not
    allowed to fail in."""
    write_session(state_dir, "s1", "PreToolUse", NOW + 3, pid=dead())
    assert provider(state_dir).poll(NOW)[0].n_run == 1
    assert claude_state.pid_liveness_trusted() is False


# --- what a dropped session leaves behind ---------------------------------


@posix_only
def test_a_pid_dead_session_is_not_swept_the_instant_it_is_dropped(state_dir,
                                                                   dead):
    """Deliberate. The slot stops being REPORTED at once, which is the whole
    user-visible fix, but the file survives to be looked at: it is the only
    evidence of why a session vanished (last event, clock, pid), and this
    check is new enough that somebody will want it. The hour still collects
    the file, so nothing accumulates for longer than it used to."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 300, pid=dead())
    add_agent(state_dir, "s1", "a1")
    assert provider(state_dir).poll(NOW) == []
    assert (state_dir / "s1.state").exists()
    assert (state_dir / "s1" / "a1").exists()
    # An hour on, the ordinary sweep takes it -- pid or no pid.
    assert provider(state_dir).poll(NOW + ABANDONED_AFTER_S + 1) == []
    assert not (state_dir / "s1.state").exists()
    assert not (state_dir / "s1").exists()


# --- the per-session view, and the marker that beats the slot -------------
#
# scan() collapses sessions into counts before anyone sees them, and the Codex
# union needs the ids: a session both the hook slots and the rollout reader can
# see must be counted once, and an id is the only thing those two sources
# share. session_states() is that view; scan() is now derived from it.
#
# The marker is the other half. `waiting` cannot be read off the slot on Codex,
# because PreToolUse and PermissionRequest fire in the same second from two
# separate shim processes and the slot's mv -f is last-writer-wins. See ON DISK
# in pc/providers/claude_state.py.


def mark_waiting(d, sid, t=None):
    p = d / f"{sid}{claude_state.WAITING_MARKER_SUFFIX}"
    p.write_text("")
    if t is not None:
        os.utime(p, (t, t))
    return p


def test_session_states_reports_each_session_by_id(state_dir):
    """Who is in which state, not just how many, and under which name."""
    write_session(state_dir, "sess-a", "PermissionRequest", NOW - 5,
                  name="Alpha")
    write_session(state_dir, "sess-b", "PreToolUse", NOW - 5)
    states, agents = provider(state_dir, sweep=False).session_states(NOW)
    assert states == {"sess-a": (base.STATE_WAITING, "Alpha"),
                      "sess-b": (base.STATE_RUNNING, "")}
    assert agents == 0


def test_scan_still_agrees_with_session_states(state_dir):
    """The counts and the names are derived from the mapping, so they cannot
    drift from it -- which is the only reason splitting the pass out is safe."""
    write_session(state_dir, "sess-a", "PermissionRequest", NOW - 5,
                  name="Alpha")
    write_session(state_dir, "sess-b", "PreToolUse", NOW - 5)
    counts, names, agents = provider(state_dir, sweep=False).scan(NOW)
    assert counts == {base.STATE_WAITING: 1, base.STATE_RUNNING: 1}
    assert names == {base.STATE_WAITING: ["Alpha"]}
    assert agents == 0


def test_a_marker_beats_a_slot_that_still_says_running(state_dir):
    """THE RACE, and the whole reason the marker exists.

    This is the losing order written down: PermissionRequest fired, and then
    PreToolUse's rename landed on top of it in the same second, so the slot
    reads `running` over a session that is blocked on a person. Nothing in the
    slot can distinguish that from a session that really is working -- the
    marker is the only witness left, and it wins."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    mark_waiting(state_dir, "s1", NOW - 5)
    states, _ = provider(state_dir).session_states(NOW)
    assert states == {"s1": (base.STATE_WAITING, "")}


def test_a_session_with_no_marker_is_whatever_its_slot_says(state_dir):
    """The control the test above needs. Without it, a scanner that simply
    reported `waiting` for every session would pass that one."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    states, _ = provider(state_dir).session_states(NOW)
    assert states == {"s1": (base.STATE_RUNNING, "")}


def test_a_marker_older_than_the_hour_is_not_believed(state_dir):
    """A marker whose removal never ran -- a crash between the answer and the
    unlink -- would otherwise pin a session on "Waiting for you" for the rest
    of its life. It is bounded by the same hour every other file here is."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    mark_waiting(state_dir, "s1", NOW - ABANDONED_AFTER_S - 1)
    counts, _, _ = provider(state_dir).scan(NOW)
    assert counts == {base.STATE_RUNNING: 1}


def test_a_marker_from_a_clock_that_runs_fast_is_still_a_marker(state_dir):
    """A file stamped in the future was written by a clock that runs fast, not
    by a session that has not started yet. Refusing it would drop a real
    `waiting` -- the one direction this feature is not allowed to fail in."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    mark_waiting(state_dir, "s1", NOW + 30)
    counts, _, _ = provider(state_dir).scan(NOW)
    assert counts == {base.STATE_WAITING: 1}


def test_a_swept_session_takes_its_marker_with_it(state_dir):
    """An abandoned slot is not a session, it is litter, and it is collected --
    the marker beside it too. Nothing else ever looks at these files again, and
    the shim only removes a marker on an event that will never come."""
    dead_t = NOW - ABANDONED_AFTER_S - 60
    write_session(state_dir, "gone", "PreToolUse", dead_t)
    marker = mark_waiting(state_dir, "gone", dead_t)
    states, agents = provider(state_dir).session_states(NOW)
    assert (states, agents) == ({}, 0)
    assert not (state_dir / "gone.state").exists()
    assert not marker.exists()




# --- the tombstone ------------------------------------------------------------
#
# SessionEnd leaves a `<sid>.ended` file where the slot was. This provider's own
# census has no use for it -- the slot is gone, so the session is already
# uncounted here -- and it exists entirely for the Codex union, which cannot see
# a session end any other way. What this directory still owes it is COLLECTION:
# the shim writes one here as well, and if the ordinary poll does not sweep them
# nothing ever will, and ~/.blink/state grows one empty file per session for the
# life of the install.


def mark_ended(d, sid, t):
    """The tombstone SessionEnd leaves, aged by its mtime."""
    p = d / f"{sid}.ended"
    p.write_text("")
    os.utime(p, (t, t))
    return p


def test_an_ordinary_poll_collects_a_tombstone_that_has_aged_out(state_dir):
    """The leak this sweep prevents. Nothing else in the Claude directory ever
    reads these names again, so the poll that walks the directory anyway is the
    only thing that can collect them."""
    old = mark_ended(state_dir, "gone", NOW - ABANDONED_AFTER_S - 60)
    fresh = mark_ended(state_dir, "recent", NOW - 60)

    provider(state_dir).poll(NOW)

    assert not old.exists()
    assert fresh.exists(), "a tombstone inside the hour is still evidence"


def test_a_look_that_does_not_sweep_leaves_the_tombstone_alone(state_dir):
    """`blink status` must be able to look without collecting, exactly as it
    can for the slots: a diagnostic that deletes what it is diagnosing destroys
    the evidence somebody ran it to see."""
    old = mark_ended(state_dir, "gone", NOW - ABANDONED_AFTER_S - 60)

    provider(state_dir, sweep=False).poll(NOW)

    assert old.exists()


def test_a_tombstone_does_not_silence_the_session_that_resumed(state_dir):
    """A live slot beside a tombstone means the session came back under its own
    id -- `claude --resume` reuses it -- and the slot is the newer witness. The
    shim removes the tombstone on any event for exactly this case, so the pair
    should not outlive one hook firing; if it does, the panel must still show
    the session somebody is sitting in front of."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    mark_ended(state_dir, "s1", NOW - 900)

    counts, _, _ = provider(state_dir).scan(NOW)

    assert counts == {base.STATE_RUNNING: 1}
