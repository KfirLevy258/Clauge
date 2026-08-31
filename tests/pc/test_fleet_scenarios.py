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
  - quiet_window_s: 0 for none; >0 asks the checker to require a board
    [usage] line after a silent gap of at least that many seconds.

Step spacing (>=4s apart) matters because fleet runs set the daemon's usage
poll interval to about 3s (BLINK_POLL_INTERVAL_S) -- steps closer together
than one poll cycle risk landing on the same poll and collapsing into a
single sent frame, which would make min_tx a lie.
"""
import glob
import json
import os

from pc.providers.scripted import ScriptedProvider

SCEN_DIR = os.path.join(os.path.dirname(__file__), "..", "fleet", "scenarios")
EXPECT_KEYS = ("min_tx", "min_board_usage", "min_stale_lines", "quiet_window_s")


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
    for doc, path in _all_scenarios():
        clock = [0.0]
        sp = ScriptedProvider(path, now=lambda: clock[0])
        clock[0] = doc["duration_s"] + 1
        frames = sp.poll(clock[0])
        assert len(frames) == len(doc["steps"]), path


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
    """AGE_CAPTION_MIN_S is 600s (usage_view.c:1455), not the 120s an older
    comment mentions -- a scenario claiming staleness under that threshold
    would never trip the caption it's supposed to exercise."""
    doc, _ = _load("stale_age.json")
    stale_steps = [s for s in doc["steps"] if s.get("stale")]
    assert stale_steps, "stale_age.json should have at least one stale step"
    for s in stale_steps:
        assert s.get("age_s", 0) >= 600, s
    assert doc["expect"]["min_stale_lines"] == len(stale_steps)


def test_sleep_wake_has_a_real_quiet_window():
    doc, _ = _load("sleep_wake.json")
    ats = sorted(s["at"] for s in doc["steps"])
    gaps = [b - a for a, b in zip(ats, ats[1:])]
    biggest = max(gaps)
    assert biggest >= doc["expect"]["quiet_window_s"]
    assert biggest >= 35  # sleep threshold is 30s; leave margin
