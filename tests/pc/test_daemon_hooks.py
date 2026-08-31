"""The two env hooks the fleet tests drive the real daemon with.

BLINK_SCENARIO swaps the usage source; BLINK_TAP records the serial link.
Both must be invisible to a customer who sets neither, so the inert case is
tested as deliberately as the active one.
"""
import json
import os
import sys

import pytest

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


def test_without_the_env_the_poll_interval_is_the_shipped_default(monkeypatch):
    monkeypatch.delenv("BLINK_POLL_INTERVAL_S", raising=False)

    # The customer path is the constant, untouched.
    assert cub.POLL_INTERVAL_S == 60
    assert cub.poll_interval() == 60


def test_the_poll_interval_can_be_shortened_for_a_scenario_timeline(monkeypatch):
    # A scenario is a timeline. At one poll a minute a thirty-second scenario
    # emits a single usage frame, so the sequence under test never happens.
    monkeypatch.setenv("BLINK_POLL_INTERVAL_S", "3")

    assert cub.poll_interval() == 3


def test_a_fractional_poll_interval_is_honoured(monkeypatch):
    monkeypatch.setenv("BLINK_POLL_INTERVAL_S", "2.5")

    assert cub.poll_interval() == 2.5


@pytest.mark.parametrize("junk", ["abc", "0", "-5", "", "  ",
                                  "nan", "inf", "-inf", "1e400"])
def test_a_nonsense_poll_interval_falls_back_and_says_so(
        junk, monkeypatch, capsys):
    # Zero or negative would busy-poll a rate-limited endpoint as fast as the
    # read loop turns, so it is refused rather than obeyed.
    #
    # nan and inf parse without raising and are the quietest failure of the
    # lot: next_poll becomes nan or inf, `monotonic() >= next_poll` is then
    # False for the rest of the run, and the board silently never receives
    # usage again with nothing anywhere saying why. Note nan <= 0 is False,
    # so a bare positivity check does not catch it. "1e400" is the same trap
    # spelled as a number a person might plausibly type.
    monkeypatch.setenv("BLINK_POLL_INTERVAL_S", junk)

    assert cub.poll_interval() == 60
    assert "BLINK_POLL_INTERVAL_S" in capsys.readouterr().err


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


class _FakeSerial:
    """Enough of a serial port for the probe: it answers once, then nothing.

    read(n) returning one chunk that holds several lines is not a contrivance
    -- it is what the real port does. ser.read(n) waits for n bytes or the
    full timeout, so a 0.2 s probe read comes back with everything the board
    said in that window, glued together.
    """

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.written = []

    def reset_input_buffer(self):
        pass

    def write(self, raw):
        self.written.append(raw)

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""


# What the board says when the probe's welcome wakes it: proto.c prints on
# connect, answers with pref, and the sleep loop notices within a frame or
# two at 12 fps. All of it arrives inside the probe's window, in one chunk.
WAKE_CHUNK = (b'[proto] host connected\n'
              b'{"t":"pref","v":1,"provider":"claude"}\n'
              b'[sleep] host back; opening eyes\n')


def test_the_probe_window_is_in_the_transcript(tmp_path):
    """The board's answer to the probe is the wire, so it belongs on the tap.

    The probe runs before the read loop exists, reads a chunk, takes its
    identification from it and drops the rest. Everything the board says
    about what it is and what state it woke in is in that chunk -- including
    the one line that proves it was asleep, which is printed BECAUSE the
    probe's welcome arrived. A transcript that starts after the probe cannot
    show any of it.
    """
    tap = cub.Tap(str(tmp_path / "tap.jsonl"))

    assert cub.probe_is_our_board(_FakeSerial([WAKE_CHUNK]), 0.2, tap) is True

    lines = [l["line"] for l in _lines(tmp_path / "tap.jsonl")
             if l["dir"] == "console"]
    assert "[sleep] host back; opening eyes" in lines
    assert "[proto] host connected" in lines


def test_one_tap_spans_the_probe_and_the_read_loop(tmp_path):
    """A line torn across the two must not be torn in the transcript.

    The probe's last read routinely ends mid-print. Only the same Tap object
    can hold that tail until the read loop delivers its newline, which is why
    the daemon builds the tap once per connection and hands it to
    install_tap rather than letting install_tap make a second one.
    """
    path = tmp_path / "tap.jsonl"
    tap = cub.Tap(str(path))
    cub.probe_is_our_board(
        _FakeSerial([b'{"t":"ping","v":1}\n[sleep] host back;']), 0.2, tap)

    same, _, _ = cub.install_tap(lambda m: None, lambda m: None, tap)
    assert same is tap
    same.console(b" opening eyes\n")

    lines = [l["line"] for l in _lines(path) if l["dir"] == "console"]
    assert "[sleep] host back; opening eyes" in lines


def test_the_probe_is_unchanged_for_anyone_not_recording(tmp_path,
                                                         monkeypatch):
    monkeypatch.delenv("BLINK_TAP", raising=False)
    monkeypatch.chdir(tmp_path)
    port = _FakeSerial([WAKE_CHUNK])

    assert cub.open_tap() is None
    assert cub.probe_is_our_board(port, 0.2, cub.open_tap()) is True
    assert cub.probe_is_our_board(port, 0.2) is False   # nothing left to say
    assert list(tmp_path.iterdir()) == []


def test_open_tap_follows_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BLINK_TAP", str(tmp_path / "tap.jsonl"))
    tap = cub.open_tap()
    assert tap is not None
    tap.console(b"hello\n")
    assert _lines(tmp_path / "tap.jsonl")[0]["line"] == "hello"


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
