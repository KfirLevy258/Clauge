"""bridge.log: timestamped, de-duplicated, and bounded.

One customer's log was 2 MB after a week, with no timestamps and nothing that
ever removed a line. This is the file README-full.md asks a customer to send
when something goes wrong, so every property here is a support property: can
a reader tell WHEN, can they grep it, and is it still there next month.

The rotation tests care most about not truncating the wrong file. The daemon
does not open this file -- launchd does, and holds the descriptor -- so a
rotation that guesses wrong truncates something it does not own.
"""
import io
import os

from pc import logbook


class _Clock:
    """A stamp that does not depend on when the suite runs."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"T{self.n}"


# ------------------------------------------------------------------ stamping

def test_every_line_is_stamped_once():
    buf = io.StringIO()
    j = logbook.Journal(buf, clock=_Clock())
    print("hello", file=j)
    print("world", file=j)
    assert buf.getvalue() == "T1 hello\nT2 world\n"


def test_print_reaches_write_twice_and_still_gets_one_stamp():
    """print() writes the text and the newline separately.

    A stamp per write() call would put one in the middle of every line. The
    flag tracks the transition into a line instead, which is also what makes
    a multi-line traceback come out with a stamp on each of its lines rather
    than one on the first.
    """
    buf = io.StringIO()
    j = logbook.Journal(buf, clock=_Clock())
    j.write("hello")
    j.write("\n")
    assert buf.getvalue() == "T1 hello\n"


def test_a_multi_line_write_stamps_each_line():
    buf = io.StringIO()
    j = logbook.Journal(buf, clock=_Clock())
    j.write("one\ntwo\nthree\n")
    assert buf.getvalue() == "T1 one\nT2 two\nT3 three\n"


def test_a_line_split_across_writes_is_stamped_at_its_start():
    buf = io.StringIO()
    j = logbook.Journal(buf, clock=_Clock())
    j.write("[bridge] ")
    j.write("connected\n")
    assert buf.getvalue() == "T1 [bridge] connected\n"


def test_the_real_stamp_is_sortable_and_carries_the_year():
    """A log that now survives months cannot use a stamp without a date."""
    s = logbook._stamp(0)
    assert len(s) == 19 and s[4] == "-" and s[7] == "-" and s[13] == ":"


# ------------------------------------------------------------ console echo

def test_only_whole_lines_come_out():
    e = logbook.ConsoleEcho()
    assert e.feed(b"[proto] host con") == []
    assert e.feed(b"nected\n") == ["[proto] host connected"]


def test_protocol_json_is_not_echoed():
    """It is logged parsed, by the bridge, in the form it acted on.

    Echoing it here as well is how a 10-second ping came to cost three lines:
    the raw text, the parsed form, and the pong going back.
    """
    e = logbook.ConsoleEcho()
    out = e.feed(b'{"t":"ping","v":2,"up_ms":900}\n[usage] session 5%\n')
    assert out == ["[usage] session 5%"]


def test_blank_lines_are_dropped():
    e = logbook.ConsoleEcho()
    assert e.feed(b"\n\r\n  \n[ota] done\n") == ["[ota] done"]


def test_a_board_that_never_sends_a_newline_does_not_grow_the_buffer():
    e = logbook.ConsoleEcho(limit=64)
    for _ in range(50):
        assert e.feed(b"x" * 100) == []
    assert len(e._buf) <= 64


def test_undecodable_bytes_do_not_raise():
    """Line noise on a serial port is ordinary, and it must not take the
    daemon down -- this runs inside the read loop of a login service."""
    e = logbook.ConsoleEcho()
    assert e.feed(b"\xff\xfe bad\n") == ["�� bad"]


# --------------------------------------------------------------- heartbeat

def test_the_heartbeat_says_nothing_until_it_is_due():
    t = [0.0]
    h = logbook.Heartbeat(every_s=600, now=lambda: t[0])
    for _ in range(60):
        h.beat()
    assert h.due() is None
    t[0] = 601
    assert "60 keepalives" in h.due()


def test_a_quiet_link_leaves_a_gap_rather_than_a_line():
    """Silence is diagnostic. A summary every ten minutes saying nothing
    happened would hide the difference between a cable that went quiet and a
    daemon that stopped logging."""
    t = [0.0]
    h = logbook.Heartbeat(every_s=600, now=lambda: t[0])
    t[0] = 601
    assert h.due() is None


def test_the_count_resets_after_it_is_reported():
    t = [0.0]
    h = logbook.Heartbeat(every_s=600, now=lambda: t[0])
    h.beat()
    t[0] = 601
    assert "1 keepalives" in h.due()
    h.beat()
    h.beat()
    t[0] = 1202
    assert "2 keepalives" in h.due()


# ----------------------------------------------------------------- repeats

_FIELDS = ("session_pct", "weekly_pct", "state")


def _frame(**kw):
    """A usage frame whose countdowns move on every one, as the real ones do."""
    _frame.n = getattr(_frame, "n", 0) + 1
    m = {"t": "usage", "session_pct": 8.0, "weekly_pct": 98.0, "state": "idle",
         "session_resets_in_s": 15943 - _frame.n, "age_s": _frame.n}
    m.update(kw)
    return m


def test_the_first_frame_is_always_logged():
    r = logbook.Repeats(_FIELDS)
    assert r.worth_logging(_frame()) is True


def test_a_frame_that_only_moved_its_countdown_is_held_back():
    """The trap this class exists for.

    session_resets_in_s and age_s differ on EVERY frame by construction, so
    comparing whole messages would answer "new" every time and suppress
    nothing -- a feature that looks implemented and does nothing.
    """
    r = logbook.Repeats(_FIELDS)
    r.worth_logging(_frame())
    for _ in range(20):
        assert r.worth_logging(_frame()) is False


def test_a_real_change_is_logged_at_once():
    r = logbook.Repeats(_FIELDS)
    r.worth_logging(_frame())
    assert r.worth_logging(_frame()) is False
    assert r.worth_logging(_frame(session_pct=9.0)) is True
    assert r.worth_logging(_frame(state="running")) is True


def test_going_back_to_a_previous_value_is_a_change():
    """Only the line immediately above is compared, not a history.

    8% -> 9% -> 8% is three things happening, and a reader needs to see the
    third. A set of everything seen would swallow it.
    """
    r = logbook.Repeats(_FIELDS)
    r.worth_logging(_frame(session_pct=8.0))
    r.worth_logging(_frame(session_pct=9.0))
    assert r.worth_logging(_frame(session_pct=8.0)) is True


def test_what_was_held_back_is_reported():
    """The label is the caller's, because two of these run side by side.

    A summary that said "messages" for both the usage frames and the clock
    syncs would leave a support reader unable to tell which stream went
    quiet.
    """
    t = [0.0]
    r = logbook.Repeats(_FIELDS, "usage frames", every_s=600,
                        now=lambda: t[0])
    r.worth_logging(_frame())
    for _ in range(9):
        r.worth_logging(_frame())
    assert r.due() is None
    t[0] = 601
    assert "9 unchanged usage frames" in r.due()


def test_a_clock_sync_only_matters_when_the_offset_moves():
    """Its epoch is new on every single frame by construction, so the offset
    is the only thing in it that can carry news: a customer travelling, or
    the twice-yearly DST step."""
    r = logbook.Repeats(("utc_offset_min",), "clock syncs")
    assert r.worth_logging({"epoch": 1, "utc_offset_min": 180}) is True
    assert r.worth_logging({"epoch": 2, "utc_offset_min": 180}) is False
    assert r.worth_logging({"epoch": 3, "utc_offset_min": 120}) is True


def test_nothing_held_back_means_nothing_said():
    t = [0.0]
    r = logbook.Repeats(_FIELDS, every_s=600, now=lambda: t[0])
    t[0] = 601
    assert r.due() is None


# ---------------------------------------------------------------- rotation

def _live(tmp_path, size):
    """A log file, plus a stream whose fileno() really is that file."""
    p = tmp_path / "bridge.log"
    p.write_bytes(b"x" * size)
    return str(p), open(str(p), "a")


def test_a_small_log_is_left_alone(tmp_path):
    path, f = _live(tmp_path, 100)
    try:
        assert logbook.rotate_if_full(path, f, cap=1000) is False
        assert os.path.getsize(path) == 100
    finally:
        f.close()


def test_a_full_log_is_copied_aside_and_emptied(tmp_path):
    path, f = _live(tmp_path, 2000)
    try:
        assert logbook.rotate_if_full(path, f, cap=1000, keep=3) is True
    finally:
        f.close()
    assert os.path.getsize(path) == 0
    assert os.path.getsize(path + ".1") == 2000


def test_generations_shift_and_the_oldest_is_dropped(tmp_path):
    path, f = _live(tmp_path, 2000)
    for i in (1, 2, 3):
        (tmp_path / f"bridge.log.{i}").write_text(f"gen{i}")
    try:
        assert logbook.rotate_if_full(path, f, cap=1000, keep=3) is True
    finally:
        f.close()
    # gen3 fell off the end; the rest moved down one.
    assert (tmp_path / "bridge.log.3").read_text() == "gen2"
    assert (tmp_path / "bridge.log.2").read_text() == "gen1"
    assert os.path.getsize(str(tmp_path / "bridge.log.1")) == 2000


def test_disk_use_is_bounded_by_cap_times_generations(tmp_path):
    """The whole point of a size rule over a date rule: this cannot be
    exceeded no matter how chatty the daemon gets."""
    path = str(tmp_path / "bridge.log")
    for _ in range(12):
        with open(path, "wb") as w:
            w.write(b"x" * 2000)
        with open(path, "a") as f:
            logbook.rotate_if_full(path, f, cap=1000, keep=3)
    total = sum(os.path.getsize(str(p)) for p in tmp_path.iterdir())
    assert total <= 4 * 2000


def test_a_stream_pointing_somewhere_else_is_never_truncated(tmp_path):
    """The guard that matters most.

    On Linux the unit sets no StandardOutput, so the daemon's stderr goes to
    the journal and bridge.log may be an unrelated leftover. Truncating a
    file we are not writing to is destroying somebody's data on a hunch.
    """
    path = str(tmp_path / "bridge.log")
    with open(path, "wb") as w:
        w.write(b"x" * 5000)
    elsewhere = open(str(tmp_path / "other"), "a")
    try:
        assert logbook.rotate_if_full(path, elsewhere, cap=1000) is False
        assert os.path.getsize(path) == 5000
    finally:
        elsewhere.close()


def test_a_stream_with_no_fileno_is_never_truncated(tmp_path):
    path = str(tmp_path / "bridge.log")
    with open(path, "wb") as w:
        w.write(b"x" * 5000)
    assert logbook.rotate_if_full(path, io.StringIO(), cap=1000) is False
    assert os.path.getsize(path) == 5000


def test_rotation_works_through_the_journal_wrapper(tmp_path):
    """Once installed, sys.stderr IS a Journal.

    Without Journal.fileno() the same-file check answers no on every machine
    and the rotation is present, tested and dead.
    """
    path, f = _live(tmp_path, 2000)
    j = logbook.Journal(f)
    try:
        assert logbook.rotate_if_full(path, j, cap=1000) is True
    finally:
        f.close()
    assert os.path.getsize(path) == 0


def test_a_missing_log_is_not_an_error(tmp_path):
    assert logbook.rotate_if_full(str(tmp_path / "nope.log"),
                                  io.StringIO()) is False


def test_a_rotation_that_cannot_be_written_does_not_raise(tmp_path,
                                                          monkeypatch):
    """This runs in the daemon's poll loop. A log that cannot be rotated is
    a log that keeps growing, which is what it did before -- not a login
    service that falls over."""
    path, f = _live(tmp_path, 2000)

    def boom(*a, **k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(logbook, "_copy", boom)
    try:
        assert logbook.rotate_if_full(path, f, cap=1000) is False
    finally:
        f.close()
