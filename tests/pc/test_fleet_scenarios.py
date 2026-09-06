"""Pin the fleet scenario library so a bad edit fails here, not on hardware.

These JSON files are what the fleet suite replays against real boards on
real machines (see pc/providers/scripted.ScriptedProvider). A scenario file
is hand-authored data, not code the test suite otherwise exercises, so
nothing stops someone from writing a scenario whose `expect` block doesn't
follow from its own `steps` -- asking for six `min_tx` from a four-step
file, say -- and only noticing when a fleet run comes back red for a reason
that has nothing to do with the daemon or the board.

The `expect` shape has no `require_ack`: the board's host-bound vocabulary
(`hello`, `ping`, `pref`, `ota_query`, `ota_flash` -- pc/bridge.py) has
nothing that acknowledges a usage frame. What proves a frame actually landed
is the board's own console line -- unconditional, one per applied frame:

    printk("[usage] session %.0f%% (%ds)  weekly %.0f%% (%ds)%s%s\\n", ...)

(firmware/src/proto.c:424) -- which the daemon echoes onto the same tap. So
`expect` counts both sides of the wire:

  - min_tx: usage messages the host sent (tap rows with dir=="tx",
    msg.t=="usage", sent==true).
  - min_board_usage: "[usage] session " lines the board printed. Can never
    exceed min_tx -- the board cannot apply a frame that was never sent.
  - min_stale_lines: how many of those board lines must carry the STALE
    marker (only stale_age.json asks for more than zero).
  - min_sleep_wakes: how many times the board must wake from sleep and then
    apply a frame -- a tap_asserts.WOKE_MARKER line off the board, followed
    by an applied one. Only sleep_wake.json asks for more than zero,
    and it earns it by stopping the daemon, not by spacing its steps out.

That last key replaced a `quiet_window_s` that could not fail. The board
stamps last_host_ms on ANY host protocol line (proto.c:262) and the daemon
answers every 10s ping with a pong (bridge.py:145-149), so HOST_TIMEOUT_MS
(30s, proto.c:29) is unreachable while the daemon is alive, at any poll
interval. Sleep is a daemon-lifecycle event: a scenario that wants one sets
`host_silence_s` and the agent stops the daemon for that long.

Step spacing (>=4s apart) matters because fleet runs set the daemon's usage
poll interval to about 3s (BLINK_POLL_INTERVAL_S) and the scripted provider
hands over one step per poll. Steps closer together than one poll cycle are
steps the replay cannot keep up with: they still all reach the board, but
later than the file says, so `duration_s` stops describing what the daemon
actually does. It is also the spacing the agent's CONNECT_TOLERANCE_S is
written against -- a first record more than 4s late means a timeline shifted
by that much, which is a run that cannot prove what it set out to.
"""
import glob
import json
import os

from pc.providers.scripted import ScriptedProvider

SCEN_DIR = os.path.join(os.path.dirname(__file__), "..", "fleet", "scenarios")
EXPECT_KEYS = ("min_tx", "min_board_usage", "min_stale_lines",
               "min_sleep_wakes")

# firmware/src/proto.c:29. The daemon has to be gone for longer than this
# before the board will admit the host is lost, and a scenario that asks for
# a sleep without allowing for it would fail on a board doing its job.
HOST_TIMEOUT_S = 30
SILENCE_MARGIN_S = 10


def _load(name):
    path = os.path.join(SCEN_DIR, name)
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def _all_scenarios():
    files = sorted(glob.glob(os.path.join(SCEN_DIR, "*.json")))
    return [(_load(os.path.basename(p))[0], p) for p in files]


def test_at_least_four_scenarios_present():
    files = sorted(glob.glob(os.path.join(SCEN_DIR, "*.json")))
    assert len(files) >= 4


def test_every_scenario_loads_and_plays_to_completion():
    """Every step becomes a frame, one new one per poll, in the order written.

    One NEW step per poll is the provider's contract
    (pc/providers/scripted.py): a poll carries one usage message to the board
    however many frames it was given, so a scenario whose min_tx is its step
    count -- which is all of them -- needs one poll per step. A daemon that
    starts polling late therefore replays the timeline late rather than
    losing the middle of it.

    Polled beyond the last step, the provider repeats that step rather than
    going empty, because a reading is a level and not an event. The check is
    therefore on the sequence of DISTINCT readings, in order, not on the
    number of frames a given number of polls happens to produce.
    """
    for doc, path in _all_scenarios():
        clock = [0.0]
        sp = ScriptedProvider(path, now=lambda: clock[0])
        clock[0] = doc["duration_s"] + 1          # every step is due at once
        played = []
        # Two extra polls: enough to show the last reading holds instead of
        # the source disappearing under the heartbeat.
        for _ in range(len(doc["steps"]) + 2):
            played += sp.poll(clock[0])

        pcts = [f.session_pct for f in played]
        want = [s["session_pct"] for s in doc["steps"]]
        assert pcts[:len(want)] == want, f"{path}: oldest first"
        assert set(pcts[len(want):]) <= {want[-1]}, (
            f"{path}: past the last step the provider must repeat it, not"
            f" invent readings -- got {pcts[len(want):]}")


def test_every_scenario_has_a_well_formed_expect_block():
    for doc, path in _all_scenarios():
        expect = doc.get("expect")
        assert expect is not None, path
        assert set(expect.keys()) == set(EXPECT_KEYS), path
        for key in EXPECT_KEYS:
            value = expect[key]
            assert isinstance(value, int) and not isinstance(value, bool), \
                f"{path}: {key} must be an int, got {value!r}"
        assert expect["min_tx"] >= 1, path
        assert expect["min_board_usage"] <= expect["min_tx"], path


def test_every_scenario_duration_covers_its_last_step():
    for doc, path in _all_scenarios():
        last_at = max(s["at"] for s in doc["steps"])
        assert doc["duration_s"] > last_at, path


def test_every_scenario_steps_are_spaced_for_a_poll_cycle():
    for doc, path in _all_scenarios():
        ats = sorted(s["at"] for s in doc["steps"])
        gaps = [b - a for a, b in zip(ats, ats[1:])]
        for gap in gaps:
            assert gap >= 4, f"{path}: steps closer than 4s apart ({gap})"


def test_every_scenario_name_matches_its_filename():
    for doc, path in _all_scenarios():
        stem = os.path.splitext(os.path.basename(path))[0]
        assert doc["name"] == stem, path


def test_overage_goes_past_100():
    doc, _ = _load("overage.json")
    assert any(s.get("session_pct", 0) > 100 for s in doc["steps"])


def test_stale_age_steps_use_realistic_ages():
    """AGE_CAPTION_MIN_S is 600s (usage_view.c:1474), not the 120s an older
    comment mentions -- a scenario claiming staleness under that threshold
    would never trip the caption it's supposed to exercise."""
    doc, _ = _load("stale_age.json")
    stale_steps = [s for s in doc["steps"] if s.get("stale")]
    assert stale_steps, "stale_age.json should have at least one stale step"
    for s in stale_steps:
        assert s.get("age_s", 0) >= 600, s
    assert doc["expect"]["min_stale_lines"] == len(stale_steps)


def test_a_scenario_that_expects_a_sleep_stops_the_daemon_long_enough():
    """The two halves of a sleep scenario have to agree with each other.

    Asking for a wake without stopping the daemon for longer than the host
    timeout is an assertion that cannot pass; stopping the daemon without
    asking for a wake is a minute of silence that proves nothing. Neither
    half is visible from the other, so they are pinned together here.
    """
    for doc, path in _all_scenarios():
        silence = doc.get("host_silence_s", 0)
        wants_sleep = doc["expect"]["min_sleep_wakes"] > 0
        if wants_sleep:
            assert silence >= HOST_TIMEOUT_S + SILENCE_MARGIN_S, path
            assert doc.get("wake_duration_s", 0) > 0, path
        if silence:
            assert wants_sleep, path


def test_sleep_wake_is_a_lifecycle_scenario_not_a_spacing_one():
    """Its evidence must come from the daemon stopping, not from step gaps.

    The version this replaced asked for a board [usage] line after a 40s gap
    between steps -- which the scenario produced by construction, since
    ScriptedProvider emits each step once and there was nothing between
    at:5 and at:45. It would have passed identically against firmware with
    sleep deleted.
    """
    doc, path = _load("sleep_wake.json")
    assert doc["expect"]["min_sleep_wakes"] >= 1
    ats = sorted(s["at"] for s in doc["steps"])
    gaps = [b - a for a, b in zip(ats, ats[1:])] or [0]
    assert max(gaps) < HOST_TIMEOUT_S, (
        f"{path}: step spacing is not what makes this board sleep; a gap that"
        f" big only disguises a lifecycle test as a data one")


def test_a_scenario_that_restarts_the_daemon_expects_the_replay():
    """ScriptedProvider stamps t0 at construction, so pass two replays it."""
    for doc, path in _all_scenarios():
        cap = len(doc["steps"]) * (2 if doc.get("host_silence_s") else 1)
        assert doc["expect"]["min_tx"] <= cap, path
