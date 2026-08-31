"""Did the board take what the host sent? Read the transcript and say.

Every device verdict this suite makes comes out of check(). It is a pure
function over parsed tap records for one reason: the thing that decides
whether a release ships must itself be testable without a board, a serial
port or a daemon.

What the transcript can and cannot prove, since three plausible-looking
assertions are wrong here:

  - There is no ack. The board's entire host-bound vocabulary is hello, ping,
    pref, ota_query and ota_flash (pc/bridge.py:131-153); nothing is sent per
    usage frame. An assertion that "the board answered after the last frame"
    would be answered by the ten-second ping and would pass against a board
    that dropped every frame it was given.

  - What does prove a frame landed is the board's own console. proto.c:424
    prints one "[usage] session ..." line per applied frame, unconditionally,
    carrying STALE when the frame was stale. That line is the end-to-end
    evidence and the only one there is.

  - Silence in the DATA is not silence on the HOST, so a gap between applied
    frames says nothing about sleep. dispatch() stamps last_host_ms on any
    host protocol line (firmware/src/proto.c:262) and the daemon answers
    every ten-second ping with a pong (pc/bridge.py:145-149), so a daemon
    that is alive can never leave the board 30 s silent (HOST_TIMEOUT_MS,
    proto.c:29) at any poll interval. Sleep is a daemon-lifecycle event, not
    a data event: it fires when the daemon is GONE. What proves it happened
    is "[sleep] host back; opening eyes", printed from inside ui_sleep_run()
    (firmware/src/ui_sleep.c:100) and reachable from nowhere else.

  - Counting tx records over-counts. poll_once() (pc/bridge.py:396-401)
    writes a `time` message on EVERY poll, before and independently of the
    usage message, so a five-poll run leaves ten tx records for five frames.
    Frames are counted by msg["t"] == "usage".

And two traps in how the streams overlap:

  - `sent` is false when the daemon refused to write the frame. send() drops
    any line over the board's 512-byte limit and a loaded two-provider frame
    already measures 484 of 512, so this is a live failure mode, not a
    theoretical one. A refused frame never reached the board; counting it
    would credit the board for work it was never given.

  - Board messages are counted from `rx` records only, never by scanning
    console text. The board's JSON travels the same wire as its printk, so it
    appears in both streams -- but only an rx record proves the daemon parsed
    it. A console-only hello means the link is delivering bytes the daemon
    cannot read, which is a failure wearing the costume of a pass.

Message types key on "t", not "type": that is the protocol's own field name,
and the tap's envelope has a "t" of its own holding the epoch timestamp. They
never meet -- one is the envelope, the other the payload.
"""

# proto.c:424 prints this for every frame it applies. Other "[usage] ..."
# lines exist that are not applied frames (firmware/src/main.c:1501, :1525),
# so the marker has to include "session " to tell them apart. Searched for
# anywhere in the line rather than anchored at its start: a board that reset
# mid-print can leave noise ahead of the newline, and losing the evidence for
# a frame that WAS applied would fail a healthy desk.
APPLIED_MARKER = "[usage] session "

# The same print's flag for a frame whose reading was already old.
STALE_MARKER = "STALE"

# ui_sleep.c:100. The board prints this on the way out of its sleep loop and
# nowhere else, so it cannot appear unless sleep_should_start() fired -- which
# needs proto_host_lost(), which needs 30 s with no host line at all. It is
# also the first half of the cycle the tap can actually SEE: "[proto] host
# went away" and "[sleep] host silent; closing eyes" are both printed while
# the daemon is stopped and nothing is reading the port, so they are lost.
# This one is printed because the daemon came back, with the port open.
WOKE_MARKER = "[sleep] host back; opening eyes"

# All four, and no defaulting. A key that falls back to zero when it is
# missing deletes its own assertion: a stale_age-shaped run with no STALE
# lines in it passed while min_stale_lines was merely absent. A scenario that
# means to assert nothing on a dimension writes an explicit 0, where a reader
# can see the decision.
REQUIRED_EXPECT_KEYS = ("min_tx", "min_board_usage", "min_stale_lines",
                        "min_sleep_wakes")


def as_number(value):
    """value as a float, or None when it is not a number.

    Records come off a file the daemon appends to while it runs, so a torn
    last line or a null timestamp is ordinary rather than exceptional. A
    checker that raised on one would report a crash where the honest answer
    is "this record tells us nothing".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _frames(n):
    """'1 usage frame', '3 usage frames'.

    Trivial, and worth a function: "expected at least 1 usage frames" is the
    kind of line that makes a reader wonder whether the tool knows what it is
    talking about, at the moment they most need to trust it.
    """
    return f"{n:.0f} usage frame" + ("" if n == 1 else "s")


def _records(tap_lines, direction):
    for rec in tap_lines:
        if isinstance(rec, dict) and rec.get("dir") == direction:
            yield rec


def _usage_tx(tap_lines):
    """(sent, refused) counts of outbound usage frames."""
    sent = refused = 0
    for rec in _records(tap_lines, "tx"):
        msg = rec.get("msg")
        if not isinstance(msg, dict) or msg.get("t") != "usage":
            continue
        if rec.get("sent") is True:
            sent += 1
        else:
            refused += 1
    return sent, refused


def _applied_lines(tap_lines):
    """[(t, line)] for every frame the board said it applied, in wire order."""
    out = []
    for rec in _records(tap_lines, "console"):
        line = rec.get("line")
        if not isinstance(line, str) or APPLIED_MARKER not in line:
            continue
        out.append((as_number(rec.get("t")), line))
    return out


def _sleep_wakes(tap_lines):
    """How many times the board woke from sleep and then applied a frame.

    A cycle is the wake line followed, later in the transcript, by an applied
    frame. Both halves are required because both halves are the feature: a
    board that wakes and then shows nothing has failed exactly as badly as
    one that never wakes, and it is the half more likely to break, since
    waking restores the dashboard with its figures flagged stale
    (ui_sleep.c:104-108) and only the daemon's next poll refills them.

    Counted rather than measured. Nothing here looks at timestamps at all,
    which also means nothing here can be fooled by records that arrive out of
    order -- the transcript is append-ordered, and order is all this needs.
    """
    wakes = 0
    armed = False
    for rec in _records(tap_lines, "console"):
        line = rec.get("line")
        if not isinstance(line, str):
            continue
        if WOKE_MARKER in line:
            armed = True
        elif armed and APPLIED_MARKER in line:
            wakes += 1
            armed = False
    return wakes


def check(tap_lines, expect, name=None):
    """Everything wrong with this run, as sentences. Empty means it passed.

    tap_lines are the parsed BLINK_TAP records; expect is the scenario's own
    block: min_tx (usage frames actually sent), min_board_usage (frames the
    board applied), min_stale_lines (how many of those carried STALE) and
    min_sleep_wakes (how many times the board must have woken from sleep and
    then applied a frame). All four are required; see REQUIRED_EXPECT_KEYS.

    name is the scenario, and rides along only so the problems can say which
    run they came from. The person reading them is looking at three machines'
    results at once and cannot be expected to keep the order in their head.
    """
    problems = []
    label = f"Scenario {name}: " if name else ""

    def note(text):
        problems.append(label + text if label else text[:1].upper() + text[1:])

    # An `expect` block that demands nothing would pass a dead board, so a
    # malformed one is reported here rather than silently defaulted to zero.
    # Absent and unusable are told apart: reporting a key that is sitting
    # right there as "missing" sends the reader hunting for the wrong thing.
    missing = [k for k in REQUIRED_EXPECT_KEYS if k not in expect]
    unusable = [k for k in REQUIRED_EXPECT_KEYS
                if k in expect and as_number(expect[k]) is None]
    if missing:
        note(f"the expect block is missing {', '.join(missing)}, so this"
             f" scenario asserts nothing on"
             f" {'those dimensions' if len(missing) > 1 else 'that dimension'}."
             f" A scenario that means to assert nothing writes an explicit 0.")
    if unusable:
        note("the expect block has " + ", ".join(
            f"{k}={expect[k]!r}" for k in unusable)
            + ", which is not a number.")
    if missing or unusable:
        return problems
    min_tx = as_number(expect["min_tx"])
    min_board = as_number(expect["min_board_usage"])
    min_stale = as_number(expect["min_stale_lines"])
    min_wakes = as_number(expect["min_sleep_wakes"])
    if min_tx < 1 or min_board < 1:
        note(f"the expect block asks for {min_tx:.0f} frames sent and"
             f" {min_board:.0f} applied, which a board that never woke up"
             f" would satisfy.")
        return problems

    if not tap_lines:
        note("the tap has no records at all, so the daemon never reached the"
             " point of writing one. Check that it started and that nothing"
             " else holds the serial port.")
        return problems

    inbound = list(_records(tap_lines, "rx"))
    if not inbound:
        note("the board never sent a message the daemon could parse (no rx"
             " records). Either nothing is attached to the port or the link"
             " is delivering bytes that do not parse as protocol messages.")

    sent, refused = _usage_tx(tap_lines)
    if sent < min_tx:
        note(f"expected at least {_frames(min_tx)} on the wire, and the host"
             f" sent {sent}.")
    if refused:
        # send() refuses a line over 512 bytes, which is nearly always what
        # this is: a loaded two-provider frame already measures 484. The
        # frame never reached the board, so without naming the host-side drop
        # this reads as a firmware fault and the hunt starts at the wrong end.
        # The one other way to land here is the link dropping mid-write, and
        # the daemon's own log tells the two apart, so it is pointed at.
        note(f"the daemon refused to write {refused} usage frame(s), which"
             f" never left the host -- almost certainly the board's 512-byte"
             f" line limit, which a loaded two-provider frame comes within 28"
             f" bytes of. The daemon logged its own reason as 'NOT SENT'"
             f" beside the tap.")

    applied = _applied_lines(tap_lines)
    if len(applied) < min_board:
        note(f"expected the board to apply at least {_frames(min_board)}, and"
             f" it printed {len(applied)} '{APPLIED_MARKER.strip()}' lines.")

    stale = sum(1 for _, line in applied if STALE_MARKER in line)
    if stale < min_stale:
        note(f"expected at least {min_stale:.0f} applied frames marked STALE,"
             f" and {stale} of {len(applied)} were.")

    if min_wakes > 0:
        wakes = _sleep_wakes(tap_lines)
        if wakes < min_wakes:
            note(f"expected the board to sleep and wake"
                 f" {min_wakes:.0f} time(s) -- a"
                 f" '{WOKE_MARKER}' line followed by an applied frame -- and"
                 f" it did that {wakes} time(s), so it never slept, or woke"
                 f" and showed nothing. The board prints that line only from"
                 f" inside its sleep loop, which needs the DAEMON stopped for"
                 f" longer than the firmware's 30s host timeout: check the"
                 f" scenario's host_silence_s, and that the daemon really did"
                 f" stop (an orphan holding the port keeps the board awake).")
    return problems
