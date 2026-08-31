"""The two env hooks the fleet tests drive the real daemon with.

BLINK_SCENARIO swaps the usage source; BLINK_TAP records the serial link.
Both must be invisible to a customer who sets neither, so the inert case is
tested as deliberately as the active one.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import claude_usage_bridge as cub


def _lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_the_tap_records_both_directions_and_still_delegates(tmp_path):
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))
    sent, received = [], []
    write, on_message = tap.wrap(sent.append, received.append)

    write({"t": "usage", "session_pct": 50})
    on_message({"t": "hello", "fw": "1.2.5"})

    # The wrapped callables are still the daemon's callables.
    assert sent == [{"t": "usage", "session_pct": 50}]
    assert received == [{"t": "hello", "fw": "1.2.5"}]

    lines = _lines(tmp_path / "tap.jsonl")
    assert [l["dir"] for l in lines] == ["tx", "rx"]
    assert lines[0]["msg"]["session_pct"] == 50
    assert lines[1]["msg"]["t"] == "hello"
    assert all(isinstance(l["t"], float) for l in lines)


def test_console_output_is_reassembled_across_chunk_boundaries(tmp_path):
    # Serial hands over whatever has arrived, which cuts lines in half. The
    # board's own [usage] print is the end-to-end evidence a fleet test reads,
    # so half of it is worth nothing.
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))
    tap.console(b"[usage] sess")
    tap.console(b"ion 50% (12s)\n")

    lines = _lines(tmp_path / "tap.jsonl")
    assert len(lines) == 1
    assert lines[0]["dir"] == "console"
    assert lines[0]["line"] == "[usage] session 50% (12s)"


def test_a_partial_console_line_waits_for_its_newline(tmp_path):
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))
    tap.console(b"[usage] sess")
    assert not (tmp_path / "tap.jsonl").exists()


def test_undecodable_console_bytes_do_not_raise(tmp_path):
    # Board output is not guaranteed clean UTF-8 -- a reset mid-line emits
    # line noise -- and this runs inside the daemon's read loop.
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))
    tap.console(b"boot \xff\xfe garbage\n")

    lines = _lines(tmp_path / "tap.jsonl")
    assert len(lines) == 1
    assert lines[0]["line"].startswith("boot ")


def test_scenario_env_selects_the_scripted_provider(tmp_path, monkeypatch):
    scen = tmp_path / "s.json"
    scen.write_text(json.dumps({"name": "t", "steps": []}), encoding="utf-8")
    monkeypatch.setenv("BLINK_SCENARIO", str(scen))

    assert cub.build_bus().provider_ids() == ["scripted"]


def test_no_scenario_env_keeps_the_real_providers(monkeypatch):
    monkeypatch.delenv("BLINK_SCENARIO", raising=False)

    ids = cub.build_bus().provider_ids()
    assert "claude" in ids


def test_without_blink_tap_nothing_is_wrapped_and_no_file_appears(
        tmp_path, monkeypatch):
    monkeypatch.delenv("BLINK_TAP", raising=False)
    monkeypatch.chdir(tmp_path)

    def write(m):
        pass

    def on_message(m):
        pass

    tap, write2, on_message2 = cub.install_tap(write, on_message)

    assert tap is None
    assert write2 is write
    assert on_message2 is on_message
    assert list(tmp_path.iterdir()) == []


def test_blink_tap_installs_the_tap(tmp_path, monkeypatch):
    path = tmp_path / "tap.jsonl"
    monkeypatch.setenv("BLINK_TAP", str(path))
    sent = []

    tap, write2, _ = cub.install_tap(sent.append, lambda m: None)

    assert tap is not None
    write2({"t": "ping"})
    assert sent == [{"t": "ping"}]
    assert [l["dir"] for l in _lines(path)] == ["tx"]
