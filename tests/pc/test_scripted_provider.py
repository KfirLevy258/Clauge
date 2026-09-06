import json
from pc.providers.scripted import ScriptedProvider


def _write(tmp_path, steps, **top):
    doc = {"name": "t", "steps": steps, **top}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_steps_emit_when_due_and_only_once(tmp_path):
    clock = [1000.0]
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "session_pct": 50.0, "state": "running"},
        {"at": 10, "provider": "claude", "session_pct": 102.0, "state": "failed"},
    ])
    sp = ScriptedProvider(p, now=lambda: clock[0])
    first = sp.poll(clock[0])
    assert [f.session_pct for f in first] == [50.0]
    assert sp.poll(clock[0]) == []          # emitted once
    clock[0] += 10
    second = sp.poll(clock[0])
    assert [f.session_pct for f in second] == [102.0]
    assert second[0].provider == "claude"
    assert second[0].state == "failed"


def test_two_steps_due_at_once_are_still_two_frames(tmp_path):
    """One step per poll, oldest first -- a late start delays, never merges.

    The daemon's first poll can land ten seconds after the provider was
    constructed: the poll gate only fires once the board has pinged, and a
    skipped poll is lost rather than deferred (claude_usage_bridge.py:1076).
    Every due step emitted in that one call would be collapsed by
    IngestionBus.poll into a single usage message, so a scenario whose min_tx
    is its step count -- which is how they are all written -- would come up
    short on a perfectly healthy board.
    """
    clock = [1000.0]
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "session_pct": 10.0},
        {"at": 5, "provider": "claude", "session_pct": 25.0},
    ])
    sp = ScriptedProvider(p, now=lambda: clock[0])
    clock[0] += 12                      # both steps came due before any poll
    assert [f.session_pct for f in sp.poll(clock[0])] == [10.0]
    assert [f.session_pct for f in sp.poll(clock[0])] == [25.0]
    assert sp.poll(clock[0]) == []


def test_age_s_backdates_observed_at(tmp_path):
    clock = [5000.0]
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "session_pct": 10.0, "age_s": 7200},
    ])
    sp = ScriptedProvider(p, now=lambda: clock[0])
    (f,) = sp.poll(clock[0])
    assert f.observed_at == 5000.0 - 7200


def test_provider_id():
    import pc.providers.scripted as m
    assert m.ScriptedProvider.__name__  # module imports cleanly


def test_unknown_key_step_is_skipped(tmp_path):
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "sesion_pct": 50.0},   # typo key
        {"at": 0, "provider": "claude", "session_pct": 60.0},  # good step
    ])
    sp = ScriptedProvider(p, now=lambda: 1000.0)
    frames = sp.poll(1000.0)
    assert [f.session_pct for f in frames] == [60.0]


def test_step_missing_at_is_skipped(tmp_path):
    p = _write(tmp_path, [
        {"provider": "claude", "session_pct": 10.0},           # no "at"
        {"at": 0, "provider": "claude", "session_pct": 20.0},  # good step
    ])
    sp = ScriptedProvider(p, now=lambda: 1000.0)
    frames = sp.poll(1000.0)
    assert [f.session_pct for f in frames] == [20.0]
