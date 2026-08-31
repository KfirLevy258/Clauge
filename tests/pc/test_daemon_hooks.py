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
from pc import protocol


def _lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def _like_send(sent):
    """A stand-in for the daemon's send(), including its over-limit refusal.

    send() is a closure inside main() and cannot be reached from a test, so
    this mirrors its contract: encode through the real encode_checked, drop
    the message when it is over the board's line limit, and report whether
    anything actually went out.
    """
    def send(m):
        raw, _why = protocol.encode_checked(m)
        if raw is None:
            return False
        sent.append(m)
        return True
    return send


def test_the_tap_records_both_directions_and_still_delegates(tmp_path):
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))
    sent, received = [], []
    write, on_message = tap.wrap(_like_send(sent), received.append)

    write({"t": "usage", "session_pct": 50})
    on_message({"t": "hello", "fw": "1.2.5"})

    # The wrapped callables are still the daemon's callables.
    assert sent == [{"t": "usage", "session_pct": 50}]
    assert received == [{"t": "hello", "fw": "1.2.5"}]

    lines = _lines(tmp_path / "tap.jsonl")
    assert [l["dir"] for l in lines] == ["tx", "rx"]
    assert lines[0]["msg"]["session_pct"] == 50
    assert lines[0]["sent"] is True
    assert lines[1]["msg"]["t"] == "hello"
    assert all(isinstance(l["t"], float) for l in lines)


def test_a_message_the_host_refused_to_send_is_marked_not_sent(tmp_path):
    # A frame over the board's 512-byte line limit is dropped by send() and
    # never reaches the wire. Recording it as a plain tx would make the
    # transcript lie in exactly the boundary case the fleet suite exists to
    # catch, and send us hunting the firmware for a host-side refusal.
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))
    sent = []
    write, _ = tap.wrap(_like_send(sent), lambda m: None)

    oversized = {"t": "usage", "pad": "x" * protocol.MAX_LINE_BYTES}
    assert protocol.encode_checked(oversized)[0] is None   # genuinely over
    write(oversized)

    assert sent == []
    lines = _lines(tmp_path / "tap.jsonl")
    assert [l["dir"] for l in lines] == ["tx"]
    assert lines[0]["sent"] is False


def test_a_write_that_blew_up_is_still_recorded_as_not_sent(tmp_path):
    # A board unplugged mid-write raises out of ser.write. The exception has
    # to reach the reconnect loop, and the transcript has to keep the attempt:
    # the last thing the host tried before the link died is exactly what a
    # failed fleet run gets read for.
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))

    def exploding(m):
        raise OSError("device not configured")

    write, _ = tap.wrap(exploding, lambda m: None)

    try:
        write({"t": "usage"})
    except OSError:
        pass
    else:
        raise AssertionError("the write error must reach the reconnect loop")

    lines = _lines(tmp_path / "tap.jsonl")
    assert [(l["dir"], l["sent"]) for l in lines] == [("tx", False)]


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

    tap, write2, _ = cub.install_tap(_like_send(sent), lambda m: None)

    assert tap is not None
    write2({"t": "ping"})
    assert sent == [{"t": "ping"}]
    assert [l["dir"] for l in _lines(path)] == ["tx"]


def test_an_unwritable_tap_is_reported_but_never_reaches_the_read_loop(
        tmp_path, capsys):
    # console() runs inside the daemon's read loop, whose except catches
    # OSError and reads it as a disconnected board. An unwritable tap path
    # would therefore present as a board that keeps dropping off the bus --
    # a hardware fault that is really a test facility. Say so instead.
    tap = cub.Tap(str(tmp_path / "no-such-dir" / "tap.jsonl"))

    tap.console(b"[usage] session 50% (12s)\n")
    tap.tx({"t": "ping"}, sent=True)
    tap.rx({"t": "hello"})

    assert "[tap]" in capsys.readouterr().err


def test_an_unwritable_tap_is_complained_about_only_once(tmp_path, capsys):
    # The board pings every ten seconds and the loop reads continuously; one
    # line per failed write would bury the daemon's real log.
    tap = cub.Tap(str(tmp_path / "no-such-dir" / "tap.jsonl"))

    for _ in range(5):
        tap.console(b"noise\n")

    assert capsys.readouterr().err.count("[tap]") == 1
