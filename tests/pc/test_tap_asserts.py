"""What the tap has to prove before a board is called healthy.

Every device verdict the fleet suite makes flows through check(), so these
tests are mostly about the ways it could pass a run that proved nothing:
counting a frame the daemon refused to write, counting the `time` message the
poll sends alongside the usage one, reading board messages out of console text
that merely echoes them, or accepting an `expect` block that demands nothing.
"""
import json
from pathlib import Path

from tests.fleet.tap_asserts import check

SCENARIOS = Path(__file__).resolve().parents[2] / "tests" / "fleet" / "scenarios"

BASE = {"min_tx": 2, "min_board_usage": 2, "min_stale_lines": 0,
        "quiet_window_s": 0}


def _tx(t, kind="usage", sent=True, **fields):
    msg = {"t": kind, "v": 1}
    msg.update(fields)
    return {"dir": "tx", "t": t, "msg": msg, "sent": sent}


def _rx(t, kind="ping"):
    return {"dir": "rx", "t": t, "msg": {"t": kind, "v": 1}}


def _applied(t, stale=False):
    """The console line proto.c prints for a usage frame it took."""
    tail = "  STALE" if stale else ""
    return {"dir": "console", "t": t,
            "line": f"[usage] session 50% (12s)  weekly 20% (30s){tail}"}


def _happy():
    return [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
            _tx(2, session_pct=60.0), _applied(2.1)]


def test_happy_path_passes():
    assert check(_happy(), BASE) == []


def test_silent_board_fails():
    lines = [_tx(1, session_pct=50.0), _tx(2, session_pct=60.0)]
    problems = check(lines, BASE)
    assert any("never sent" in p or "no message" in p.lower()
               for p in problems)


def test_board_messages_are_not_counted_from_console_text():
    """The board's JSON travels the same wire as its printk.

    A tap whose console shows a hello but has no rx record means the daemon
    never parsed one -- that is a failure, not a pass with extra steps.
    """
    lines = [{"dir": "console", "t": 0, "line": '{"t":"hello","v":1}'},
             _tx(1, session_pct=50.0), _applied(1.1),
             _tx(2, session_pct=60.0), _applied(2.1)]
    assert check(lines, BASE) != []


def test_refused_frame_is_not_counted_as_sent():
    lines = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
             _tx(2, sent=False, session_pct=60.0), _applied(2.1)]
    problems = check(lines, BASE)
    assert any("refused" in p for p in problems)
    assert any("at least 2 usage" in p for p in problems)
    # A refusal at 2am needs a diagnosis, not a mystery: name the limit that
    # almost always causes it and where the daemon wrote its own reason.
    assert any("512" in p and "NOT SENT" in p for p in problems)


def test_time_messages_do_not_count_towards_min_tx():
    lines = [_rx(0, "hello"),
             _tx(0.9, kind="time"), _tx(1, session_pct=50.0), _applied(1.1),
             _tx(1.9, kind="time"), _applied(2.1)]
    assert any("at least 2 usage" in p for p in check(lines, BASE))


def test_board_usage_lines_must_be_applied_frames():
    """Other `[usage] ...` prints exist and are not evidence of a frame."""
    lines = [_rx(0, "hello"), _tx(1, session_pct=50.0),
             {"dir": "console", "t": 1.1, "line": "[usage] view ready"},
             _tx(2, session_pct=60.0),
             {"dir": "console", "t": 2.1, "line": "[usage] no data yet"}]
    assert any("printed 0" in p for p in check(lines, BASE))


def test_stale_lines_are_counted():
    exp = dict(BASE, min_stale_lines=2)
    enough = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1, True),
              _tx(2, session_pct=60.0), _applied(2.1, True)]
    too_few = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1, True),
               _tx(2, session_pct=60.0), _applied(2.1)]
    assert check(enough, exp) == []
    assert any("STALE" in p for p in check(too_few, exp))


def test_quiet_window_needs_an_applied_frame_after_the_gap():
    exp = dict(BASE, quiet_window_s=35)
    woke = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
            _tx(45, session_pct=60.0), _applied(45.1)]
    never_slept = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
                   _tx(5, session_pct=60.0), _applied(5.1)]
    assert check(woke, exp) == []
    problems = check(never_slept, exp)
    assert any("35" in p for p in problems)


def test_quiet_window_is_not_satisfied_by_a_gap_with_no_frame_after_it():
    """A board that goes quiet and stays quiet has not proved a wake."""
    exp = dict(BASE, quiet_window_s=35)
    lines = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
             _tx(2, session_pct=60.0), _applied(2.1), _rx(60)]
    assert check(lines, exp) != []


def test_quiet_window_zero_asks_for_nothing():
    assert check(_happy(), dict(BASE, quiet_window_s=0)) == []


def test_empty_tap_fails():
    assert check([], BASE) != []


def test_expect_that_demands_nothing_is_itself_a_problem():
    weak = {"min_tx": 0, "min_board_usage": 0, "min_stale_lines": 0,
            "quiet_window_s": 0}
    assert any("expect" in p for p in check(_happy(), weak))


def test_expect_missing_a_required_key_is_a_problem():
    assert any("min_board_usage" in p
               for p in check(_happy(), {"min_tx": 2}))


def test_malformed_records_do_not_raise():
    lines = [_rx(0, "hello"), "not a dict", {"dir": "tx"}, {"t": 1},
             {"dir": "console", "t": None, "line": None},
             _tx(1, session_pct=50.0), _applied(1.1),
             _tx(2, session_pct=60.0), _applied(2.1)]
    assert check(lines, BASE) == []


def test_problems_name_the_scenario():
    problems = check([], BASE, name="overage")
    assert problems and all("overage" in p for p in problems)


def test_shipped_scenarios_pass_against_an_ideal_tap():
    """The four scenario files must be satisfiable by a board that behaves.

    An `expect` block nobody can meet would fail every desk in the fleet and
    read as broken hardware, so the shipped blocks are exercised here against
    a transcript of the run they describe.
    """
    for path in sorted(SCENARIOS.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        lines = [_rx(0, "hello")]
        for step in doc["steps"]:
            at = float(step["at"]) + 1.0
            lines.append(_tx(at, session_pct=step["session_pct"]))
            lines.append(_applied(at + 0.1, stale=bool(step.get("stale"))))
        assert check(lines, doc["expect"], name=doc["name"]) == [], path.name
