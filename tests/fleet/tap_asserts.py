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

# Without these two the block asserts nothing about the wire at all.
REQUIRED_EXPECT_KEYS = ("min_tx", "min_board_usage")


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


def _longest_applied_gap(applied):
    """The longest silence between two applied frames, in seconds.

    Deliberately measured between applied frames rather than between any two
    tap records. The board pings every ten seconds and the poll writes a
    `time` message every three, so the transcript is never quiet for long
    enough to mean anything -- while the display going untouched for forty
    seconds and then updating is exactly the sleep-and-wake this measures.

    The gap has to end in an applied frame, so a board that went quiet and
    stayed quiet scores nothing: the pair is (earlier frame, later frame),
    never (frame, end of run).
    """
    stamps = [t for t, _ in applied if t is not None]
    return max((b - a for a, b in zip(stamps, stamps[1:])), default=0.0)


def check(tap_lines, expect, name=None):
    """Everything wrong with this run, as sentences. Empty means it passed.

    tap_lines are the parsed BLINK_TAP records; expect is the scenario's own
    block: min_tx (usage frames actually sent), min_board_usage (frames the
    board applied), min_stale_lines (how many of those carried STALE) and
    quiet_window_s (0 for no requirement; above 0, an applied frame must
    follow a silence of at least that long).

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
    missing = [k for k in REQUIRED_EXPECT_KEYS if as_number(expect.get(k)) is None]
    if missing:
        note(f"the expect block is missing {', '.join(missing)}, so this"
             f" scenario asserts nothing about the wire.")
        return problems
    min_tx = as_number(expect["min_tx"])
    min_board = as_number(expect["min_board_usage"])
    min_stale = as_number(expect.get("min_stale_lines")) or 0.0
    quiet = as_number(expect.get("quiet_window_s")) or 0.0
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
        note(f"expected at least {min_tx:.0f} usage frames on the wire, and"
             f" the host sent {sent}.")
    if refused:
        # send() refuses a line over 512 bytes. The frame never reached the
        # board, so this reads as a firmware fault unless it is named here as
        # the host-side drop it is.
        note(f"the daemon refused to write {refused} usage frame(s): they were"
             f" over the board's 512-byte line limit and never left the host.")

    applied = _applied_lines(tap_lines)
    if len(applied) < min_board:
        note(f"expected the board to apply at least {min_board:.0f} usage"
             f" frames, and it printed {len(applied)}"
             f" '{APPLIED_MARKER.strip()}' lines.")

    stale = sum(1 for _, line in applied if STALE_MARKER in line)
    if stale < min_stale:
        note(f"expected at least {min_stale:.0f} applied frames marked STALE,"
             f" and {stale} of {len(applied)} were.")

    if quiet > 0:
        gap = _longest_applied_gap(applied)
        if gap < quiet:
            note(f"expected an applied frame after at least {quiet:.0f}s of"
                 f" quiet, which is what proves the board woke from sleep and"
                 f" applied it; the longest quiet before an applied frame was"
                 f" {gap:.1f}s.")
    return problems
