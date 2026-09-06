"""What the tap has to prove before a board is called healthy.

Every device verdict the fleet suite makes flows through check(), so these
tests are mostly about the ways it could pass a run that proved nothing:
counting a frame the daemon refused to write, counting the `time` message the
poll sends alongside the usage one, reading board messages out of console text
that merely echoes them, or accepting an `expect` block that demands nothing.
"""
import json
from pathlib import Path

from tests.fleet.tap_asserts import WOKE_MARKER, check

SCENARIOS = Path(__file__).resolve().parents[2] / "tests" / "fleet" / "scenarios"

BASE = {"min_tx": 2, "min_board_usage": 2, "min_stale_lines": 0,
        "min_sleep_wakes": 0}


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


def _woke(t):
    """The line the board prints when its sleep loop exits.

    Built FROM the marker rather than repeating it. This helper used to carry
    its own copy of the string, so the checker and its tests agreed with each
    other and with nothing else -- the suite was green while the marker was a
    sentence the firmware has never printed, and the first real fleet run
    failed a board that had slept and woken correctly.
    """
    return {"dir": "console", "t": t, "line": WOKE_MARKER}


def test_the_wake_marker_is_a_string_the_firmware_prints():
    """The one assertion the fleet's sleep verdict rests on.

    Nothing else in the suite can catch this: a marker the firmware never
    prints makes every sleep_wake run fail, and a marker that is merely
    out of date makes it fail on the desk rather than here, hours later,
    looking like a hardware fault.
    """
    src = (Path(__file__).resolve().parents[2]
           / "firmware" / "src" / "ui_sleep.c").read_text(encoding="utf-8")
    assert f'printk("{WOKE_MARKER}\\n")' in src, (
        f"{WOKE_MARKER!r} is not printed by ui_sleep.c any more -- the fleet"
        " suite's sleep verdict is asserting a line no board will emit")


def test_a_sleep_wake_needs_the_board_to_say_it_woke_and_then_apply():
    """Spacing between frames proves nothing; this line proves it slept.

    The board stamps last_host_ms on ANY host protocol line (proto.c:262) and
    the daemon pongs every ping, so with a daemon alive the 30s host timeout
    is unreachable at any poll interval -- a gap between applied frames is
    just a gap. WOKE_MARKER is printed nowhere but where ui_sleep_run()'s
    sleep loop exits, which happens only when the host was genuinely lost.
    """
    exp = dict(BASE, min_sleep_wakes=1)
    slept = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
             _woke(60), _tx(61, session_pct=60.0), _applied(61.1)]
    assert check(slept, exp) == []


def test_wide_spacing_alone_is_not_a_sleep():
    exp = dict(BASE, min_sleep_wakes=1)
    spaced = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
              _tx(45, session_pct=60.0), _applied(45.1)]
    problems = check(spaced, exp)
    assert any("never slept" in p or "opening eyes" in p for p in problems)


def test_a_wake_with_no_frame_after_it_does_not_count():
    """Waking up and showing nothing is the half of the feature that fails."""
    exp = dict(BASE, min_sleep_wakes=1)
    lines = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
             _tx(2, session_pct=60.0), _applied(2.1), _woke(60)]
    assert check(lines, exp) != []


def test_each_wake_is_counted_once():
    exp = dict(BASE, min_sleep_wakes=2)
    once = [_rx(0, "hello"), _tx(1, session_pct=50.0), _applied(1.1),
            _woke(60), _tx(61, session_pct=60.0), _applied(61.1),
            _applied(62)]
    twice = once + [_woke(90), _applied(91)]
    assert any("sleep and wake 2" in p for p in check(once, exp))
    assert check(twice, exp) == []


def test_zero_sleep_wakes_asks_for_nothing():
    assert check(_happy(), dict(BASE, min_sleep_wakes=0)) == []


def test_empty_tap_fails():
    assert check([], BASE) != []


def test_expect_that_demands_nothing_is_itself_a_problem():
    weak = {"min_tx": 0, "min_board_usage": 0, "min_stale_lines": 0,
            "min_sleep_wakes": 0}
    assert any("expect" in p for p in check(_happy(), weak))


def test_every_expect_key_is_required_not_just_the_counting_ones():
    """A key that falls back to zero deletes its own assertion.

    A stale_age-shaped run with no STALE lines at all passed when
    min_stale_lines was merely absent. A scenario that means to assert
    nothing on a dimension writes an explicit 0 and says so.
    """
    for missing in ("min_tx", "min_board_usage", "min_stale_lines",
                    "min_sleep_wakes"):
        expect = {k: v for k, v in BASE.items() if k != missing}
        problems = check(_happy(), expect)
        assert any(missing in p for p in problems), missing


def test_a_key_that_is_there_but_not_a_number_says_so():
    problems = check(_happy(), dict(BASE, min_tx="two"))
    assert any("not a number" in p for p in problems)
    assert not any("missing" in p for p in problems)


def test_malformed_records_do_not_raise():
    lines = [_rx(0, "hello"), "not a dict", {"dir": "tx"}, {"t": 1},
             {"dir": "console", "t": None, "line": None},
             _tx(1, session_pct=50.0), _applied(1.1),
             _tx(2, session_pct=60.0), _applied(2.1)]
    assert check(lines, BASE) == []


def test_counts_of_one_are_not_pluralised():
    """Every count in every message, not just the two that were noticed."""
    exp = {"min_tx": 1, "min_board_usage": 1, "min_stale_lines": 1,
           "min_sleep_wakes": 1}
    problems = check([_rx(0, "hello")], exp)
    assert len(problems) == 4, problems
    # "0 times" is right; only a count of one takes the singular.
    for wrong in ("1 usage frames", "1 applied frames", "1 times",
                  "time(s)", "frame(s)"):
        assert not any(wrong in p for p in problems), wrong
    assert any("1 applied frame marked STALE" in p for p in problems)
    assert any("sleep and wake 1 time " in p for p in problems)


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

        def replay(t0):
            out = []
            for step in doc["steps"]:
                at = t0 + float(step["at"])
                out.append(_tx(at, session_pct=step["session_pct"]))
                out.append(_applied(at + 0.1, stale=bool(step.get("stale"))))
            return out

        lines = [_rx(0, "hello")] + replay(1.0)
        if doc.get("host_silence_s"):
            # The daemon is stopped, the board loses the host and sleeps, the
            # daemon comes back and the scenario replays from its own start.
            woke_at = doc["duration_s"] + doc["host_silence_s"]
            lines += [_woke(woke_at)] + replay(woke_at + 1)
        assert check(lines, doc["expect"], name=doc["name"]) == [], path.name
