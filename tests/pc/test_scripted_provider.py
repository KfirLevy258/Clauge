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
