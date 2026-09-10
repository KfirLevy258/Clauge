"""NDJSON line protocol shared with the ESP32 firmware.

One JSON object per line, UTF-8, '\n'-terminated. Every message carries
`t` (type) and `v` (version). Non-JSON lines (logs) and unknown types are
ignored by callers. v2.
"""
import json
import math

from pc.version import PROTO_VERSION
from pc.providers.base import STATE_UNKNOWN, worst_of

# Kept as a name because every message builder below already spells it this
# way. It is the protocol's version, not the product's -- see pc/version.py.
VERSION = PROTO_VERSION

# The firmware's inbound line buffer, mirrored here on purpose.
#
# proto.c:367-371 does not truncate an over-long line, it DROPS it: on
# overflow it resets line_len to 0 and the whole message is gone. There is no
# error, no partial parse and nothing on the panel to show for it -- the board
# simply stops updating while the daemon reports success. Every field added to
# a message spends this budget, so encode_checked() below refuses to put a
# line on the wire that the board could not receive.
MAX_LINE_BYTES = 512

# The one variable-length field on the usage message. "Opus 5 (1M context)" is
# 19 characters; a future model name is not bounded by anything we control, so
# it is bounded here instead rather than being allowed to push the line over
# the limit above.
MODEL_MAX_CHARS = 24

# Percentages and context fullness we do not have. Same sentinel as
# pc/providers/base.UNKNOWN and as the firmware's msg_get_double default, so a
# value crosses every layer without being re-encoded.
UNKNOWN = -1.0


def encode(msg: dict) -> bytes:
    """Serialize a message dict to a single NDJSON line (bytes).

    ensure_ascii=False, and that is a firmware decision rather than a taste
    one. json.dumps escapes non-ASCII by default, msg_parse.c copies the
    bytes between two quotes and unescapes nothing, and fmt.c then draws
    whatever it was given -- so a project called "café" reached the panel as
    the literal characters \\u00e9 and was drawn that way. The firmware has
    had a UTF-8 decoder for this since fmt_ascii() was written
    (tests/fmt/host_test.c pins "caf\\xc3\\xa9" -> "caf?"); the escaping here
    was the only thing keeping it out of reach.

    It cannot cost line budget either, which matters because proto.c drops an
    over-long line whole: a UTF-8 sequence is at most four bytes and the
    escape it replaces is at least six, so every line this touches gets
    shorter or stays the same.
    """
    return (json.dumps(msg, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


# The longest countdown worth sending, and the ceiling is doing two jobs.
#
# Honesty first: the longest window this panel shows is the weekly one, seven
# days. A resets_at further out than that is a misparse -- a stamp in
# milliseconds, a field that changed meaning -- and rendering it as a confident
# "resets in 412 days" is exactly the sort of wrong-but-certain number the rest
# of this module works to avoid. Clamping says "a long time" instead.
#
# Then bytes: four countdown fields ride every usage line, and an unclamped one
# is eight digits where this is six. Those eight digits took the widest line
# the daemon can build to 517 of 512 -- over the cap, refused by
# encode_checked, and a panel that quietly stops updating. 999999 is a shade
# under twelve days: comfortably clear of the seven-day window, and six digits.
COUNTDOWN_MAX_S = 999999


def encode_checked(msg: dict):
    """encode(), but None when the result could not be received.

    Returns (bytes, None) on success and (None, reason) when the line exceeds
    what the firmware will accept. Callers are expected to log the reason and
    skip the message rather than write it: a line the board drops is strictly
    worse than one never sent, because only the second is visible from here.
    """
    raw = encode(msg)
    if len(raw) > MAX_LINE_BYTES:
        return None, (f"{msg.get('t', '?')} message is {len(raw)} bytes, over "
                      f"the board's {MAX_LINE_BYTES} byte line limit")
    return raw, None


def secs_until(resets_at, now_epoch: float) -> int:
    """Seconds until `resets_at`. -1 when unknown or already past.

    -1 rather than 0 for missing input: 0 renders as "resets now", which is a
    confident lie. -1 lets the display say "--".

    And -1 rather than 0 for a reset that has ALREADY passed, which is not a
    presentation choice. usage_view.c treats a countdown of exactly 0 as "this
    window just rolled over" and zeroes the percentage with it -- correct for
    the firmware's own countdown reaching zero, and wrong for a reading we are
    merely relaying, because usage may have happened from claude.ai or the
    phone since. Sending 0 hands the board a decision the parser declined to
    make, and it wipes the last-known numbers.

    Lives here rather than in a provider because it is a property of the wire
    format -- the board has no clock over USB, so the countdown is computed on
    this side for every provider, not just the first one.
    """
    if resets_at is None or resets_at <= now_epoch:
        return -1
    # Round UP, never down. int() truncates, so a window with 0.4 s left
    # returned exactly 0 -- the single value this docstring says must never
    # reach the board, reintroduced by the arithmetic two lines under the
    # paragraph forbidding it. The guard above only excludes resets_at that
    # have already passed. Ceiling keeps the value honest (the window really
    # does have "about a second" left) and keeps 0 reserved for the firmware's
    # own countdown reaching it.
    return min(COUNTDOWN_MAX_S, max(1, math.ceil(resets_at - now_epoch)))


def decode(line: str):
    """Parse one line. Returns a dict, or None for non-protocol/garbage lines."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class LineReader:
    """Buffers incoming bytes and yields decoded protocol messages."""

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        self._buf += chunk
        out = []
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            try:
                msg = decode(raw.decode("utf-8", "replace"))
            except Exception:
                msg = None
            if msg is not None:
                out.append(msg)
        return out


def welcome(app: str, app_ver: str) -> dict:
    return {"t": "welcome", "v": VERSION, "app": app, "app_ver": app_ver}


def bye() -> dict:
    """The app is going on purpose (uninstall). Without it a computer with
    no app any more looks exactly like a sleeping one, and the board would
    doze instead of saying \"connecting\" (docs/sleep-mode-design.md)."""
    return {"t": "bye", "v": VERSION}


def pong() -> dict:
    """Answer to the board's ping.

    Liveness has to run both ways. Usage is only pushed every 300 s, so with
    no answer to the board's 10 s ping the board cannot distinguish a host
    that is merely between polls from one that has died -- and would sit
    there showing a green dot over numbers that stopped updating.
    """
    return {"t": "pong", "v": VERSION}


def time_msg(epoch: int, utc_offset_min: int) -> dict:
    """Wall clock for the board.

    Sent on hello and alongside every usage push: the board has no RTC, so it
    anchors this epoch to its own uptime and re-anchors on each message,
    bounding drift to one push interval. utc_offset_min is the PC's local
    offset (DST included) -- the board does epoch + offset and renders HH:MM.
    """
    return {"t": "time", "v": VERSION, "epoch": int(epoch),
            "utc_offset_min": int(utc_offset_min)}


def _round_pct(v):
    """A percentage as it goes on the wire: one decimal, or left alone.

    Non-numbers and the -1 sentinel pass through untouched.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    if not math.isfinite(v) or v < 0:
        return v
    return round(float(v), 1)


def usage(session_pct, session_resets_at, weekly_pct, weekly_resets_at, models,
          session_resets_in_s=-1, weekly_resets_in_s=-1, stale=False,
          provider="claude", src="cli", state="", n_sess=0, n_run=0, n_wait=0, n_stuck=0,
          n_agents=0, p2="", p2_session_pct=UNKNOWN,
          p2_weekly_pct=UNKNOWN, p2_session_resets_in_s=-1,
          p2_weekly_resets_in_s=-1, p2_stale=False, burn_pph=None,
          age_s=-1, p2_age_s=-1, active_age_s=-1) -> dict:
    """A usage message.

    The *_resets_in_s fields carry the remaining seconds. The board has no
    wall clock when tethered over USB, so it cannot derive a countdown from
    the absolute resets_at timestamps; it ticks these down locally instead.
    -1 means unknown. The absolute timestamps are kept for readability and
    for any consumer that does know the time.

    Known models are ALSO flattened into sonnet_pct/opus_pct: the board's
    JSON scanner reads scalar keys only, and its per-model peek needs these
    without growing a full array parser.

    `stale` describes the FIRST provider only, and `p2_stale` the second.
    Freshness belongs to a reading, not to the panel: the two providers are
    read from different places at different times, and the board shows one
    page at a time, so a single flag for both meant a live page could be
    labelled old because the page you were not looking at had gone quiet.

    `stale` is a declared field, not an afterthought bolted on by a caller:
    this function is the one place the wire contract is defined.
    pc/statusline_source.py, the only producer of usage messages, sets it
    when the payload it read has outlived its own freshness window.

The firmware reads this field: proto.c's "usage" handler calls
    msg_get_bool(json, "stale", ...) and sets USAGE_STATUS_STALE when it is
    true, after the update and models calls that set OK internally.

    An absent key leaves it false on the board, so a daemon older than that
    firmware reads as OK rather than as a permanent warning. Older firmware
    given this field simply ignores it -- it degrades to a green dot on a
    stale reading, which is the behaviour that existed before either side
    knew about the field.

    `age_s` and `p2_age_s` are how old each READING is -- seconds between the
    moment the source observed it and the moment this message was built --
    and -1 where that is unknown.

    They exist because the board could not work this out for itself, and had
    been getting it wrong in a way that could not show. usage_view.c keeps an
    `age_s` counter, zeroes it on every arriving usage message and ticks it
    once a second, and render_age() prints the caption only once it reaches
    120. But the daemon pushes every POLL_INTERVAL_S = 60 s whether or not
    the reading changed, so that counter was reset twice as often as the
    caption needed to appear: it measured the age of the MESSAGE, never of
    the figure, and 120 was unreachable. The label, its formatter and its
    place on the panel were all built and none of them had ever appeared.

    The change-driven push (Bridge.poll_if_changed) only sharpens that point:
    a message can now also arrive the moment session state moves, so the gap
    between messages says even less about the age of the figure than it did.

    The distinction is the whole point on a source that goes quiet. Claude
    Desktop only refreshes its cache while somebody is at the machine, so a
    four-hour-old percentage was being re-sent every minute and drawn with
    the same confidence as a live one -- observed on a real desk, 2026-08-30,
    and reported as the panel being "stuck".

    Per provider, for the same reason `stale` is: the board shows one page at
    a time and the two readings are taken from different files at different
    times.

    -1 means the daemon did not say. Firmware that understands these falls
    back to counting from the message in that case, which is exactly the old
    behaviour, so an older daemon loses nothing it used to have. Older
    firmware ignores both keys.

    `active_age_s` is the other freshness question, and it is one field for
    the whole desk rather than one per provider: how long since ANY tool on
    this machine last wrote anything at all, whether or not what it wrote
    carried a percentage. The board dozes on this one and captions on
    `age_s`, which is the same split it already makes between
    SLEEP_ABSENT_AFTER_S ("is anybody here", four hours) and
    SLEEP_READING_STALE_AFTER_S ("is this number old", half an hour) --
    two questions, two numbers, and answering one with the other is the
    whole class of bug this exists to close.

    They diverge for exactly one reason. pc/providers/claude_cli remembers
    the last payload that had a five-hour window and re-offers it carrying
    its ORIGINAL mtime, because the rewrite that drops an expired window
    does not make the last real reading untrue. When that remembered
    reading wins the session dial it also becomes the frame whose age is
    reported -- so a status line written five seconds ago can arrive here
    stamped twelve hours old, and a board that dozes at four hours closes
    its eyes on somebody who is sitting in front of it (measured, field
    review 2026-09-02).

    Per-desk and not per-provider because dozing is a whole-board decision:
    the panel does not sleep one page at a time, and there is no version of
    "Claude is quiet but Codex is not" in which the screen should go dark.
    It is the age of the freshest reading on the bus, across both providers.

    -1 when unknown, like `age_s`. An absent key sends the firmware back to
    `age_s`, and that fallback is EXACT rather than approximate: the two
    numbers can only differ because of the memory described above, and a
    daemon old enough not to send this field has no such memory, so its
    freshest source IS the dial's source. Older firmware ignores the key.
    """
    # `models` itself never went on the wire usefully: the firmware reads the
    # flattened scalar keys below and never the array, and since the status
    # line became the only source it has always been empty. Dropping it buys
    # back thirteen bytes of a budget that the second provider's fields just
    # made tight.
    flat = {}
    for m in models or []:
        name = m.get("name")
        if name in ("fable", "sonnet", "opus") and "weekly_pct" in m:
            flat[f"{name}_pct"] = float(m["weekly_pct"])

    # The multi-provider fields, added WITHOUT moving PROTO_VERSION.
    #
    # pc/version.py states the rule these follow: protocol changes are
    # additive, and the version is a floor that refuses, not a format
    # selector. A board running older firmware ignores every key below --
    # msg_get_* simply never asks for them -- and goes on rendering the two
    # dials it already knows about. Moving the version instead would have
    # meant every deployed unit stops being offered updates, over the very
    # link the update travels on, which is not a mistake that can be
    # corrected remotely.
    #
    # Unknown fields are OMITTED rather than sent as sentinels. That is the
    # MAX_LINE_BYTES budget talking: an over-long line is dropped whole by the
    # board (proto.c:367-371), so every key that carries no information is
    # spending a budget a future field will need. An absent key already means
    # "unknown" on both sides.
    extra = {"provider": provider, "src": src}
    if state:
        extra["state"] = state

    # Session and agent counts. Zeros are omitted like everything else here --
    # an absent count reads as zero on both sides, and on the common machine
    # (one session, no agents) that keeps this whole block down to about
    # twenty bytes instead of sixty.
    for key, val in (("n_sess", n_sess), ("n_run", n_run), ("n_wait", n_wait),
                     ("n_stuck", n_stuck), ("n_agents", n_agents)):
        if val:
            extra[key] = int(val)

    # A second provider, drawn as the inner ring on both gauges. Sent only
    # when there IS one, which on a single-provider machine is never -- so
    # the common line pays nothing for the capability.
    if p2:
        extra["p2"] = p2[:MODEL_MAX_CHARS]
        if p2_session_pct is not None and p2_session_pct >= 0:
            extra["p2_session_pct"] = p2_session_pct
        if p2_weekly_pct is not None and p2_weekly_pct >= 0:
            extra["p2_weekly_pct"] = p2_weekly_pct
        # Short names on purpose. These are the last two fields that fit: the
        # fully-loaded line is close enough to MAX_LINE_BYTES that spelling
        # them "p2_session_resets_in_s" would cost more than they carry, and
        # nothing but proto.c ever reads them.
        if p2_session_resets_in_s is not None and p2_session_resets_in_s >= 0:
            extra["p2_s_in_s"] = int(p2_session_resets_in_s)
        if p2_weekly_resets_in_s is not None and p2_weekly_resets_in_s >= 0:
            extra["p2_w_in_s"] = int(p2_weekly_resets_in_s)
        # The second provider's own age.
        #
        # `stale` above describes the FIRST provider and nothing else, and the
        # board was showing it over whichever page happened to be on screen.
        # With one provider that was the same statement; with two it is not.
        # A machine that runs Claude Code all day and touched Codex once this
        # morning has a stale codex reading and a live claude one, and the
        # claude page was reporting "Reading is old" over numbers that were
        # updating in front of you (user-reported 2026-08-28).
        #
        # Sent only alongside the rest of p2, so a board on older firmware
        # ignores an unknown key and behaves exactly as it did.
        extra["p2_stale"] = bool(p2_stale)

    # How fast the session window is filling, percent per hour.
    #
    # Sent ONLY when no reset time could be found for it -- the normalizer
    # enforces that, and this is the second half of the same rule: a board
    # never receives both, so it never has to decide between them. One
    # decimal place, because the panel renders about six characters and the
    # difference between 14.2 and 14.23 %/h is not a difference anybody acts
    # on. Omitted when unknown, like every other optional key here.
    if burn_pph is not None and burn_pph > 0:
        extra["burn_pph"] = round(float(burn_pph), 1)

    # Omitted when unknown rather than sent as -1, like every other optional
    # key here: -1 is what the firmware already defaults to, so an absent key
    # and an explicit -1 mean the same thing on arrival and the shorter one
    # leaves the byte budget alone.
    if age_s is not None and age_s >= 0:
        extra["age_s"] = int(age_s)
    if p2 and p2_age_s is not None and p2_age_s >= 0:
        extra["p2_age_s"] = int(p2_age_s)
    # Sent whenever it is known, including when it happens to equal `age_s`.
    # Suppressing the duplicate would buy back twenty bytes on the common
    # line and cost the reader the ability to tell "the desk is as quiet as
    # the dial" from "this daemon is too old to know the difference" -- and
    # it buys nothing at all where the budget is actually decided, since the
    # worst case is the line where the two numbers disagree.
    if active_age_s is not None and active_age_s >= 0:
        extra["active_age_s"] = int(active_age_s)

    # One decimal on every percentage. Nothing downstream can see the
    # difference -- the firmware label is (int)(pct + 0.5), the arc is an
    # int32, and the 99.5 severity threshold survives a tenth -- but an
    # unrounded float("102.33333333333333") is 18 bytes, and this message was
    # measured at 506 of MAX_LINE_BYTES=512 once age_s and p2_age_s joined it.
    # proto.c DROPS an over-long line, so those six bytes were the difference
    # between a panel that updates and one that silently freezes.
    session_pct = _round_pct(session_pct)
    weekly_pct = _round_pct(weekly_pct)
    for _k in ("fable_pct", "sonnet_pct", "opus_pct"):
        if _k in flat:
            flat[_k] = _round_pct(flat[_k])
    for _k in ("p2_session_pct", "p2_weekly_pct"):
        if _k in extra:
            extra[_k] = _round_pct(extra[_k])

    # Whole seconds on the two absolute stamps, and it is the byte budget that
    # insists. They arrive straight from the provider's JSON, where a
    # "resets_at" is free to carry a fractional epoch, and json.dumps writes
    # every digit of it: 1787718000.1234567 is 18 bytes where 1787718000 is 10.
    # Two of those took the widest line this daemon can build to 521 of 512 --
    # over the cap, refused by encode_checked, and a panel that quietly stops
    # updating. Nothing reads the fraction: these exist "for readability and
    # for any consumer that does know the time" (see the docstring above),
    # while the board counts down from the *_resets_in_s fields, which are
    # already whole seconds.
    # The two absolute resets_at stamps are NOT on the wire, and their absence
    # is what bought the room for everything after them.
    #
    # Nothing has ever read them. proto.c's usage handler takes
    # session_resets_in_s and weekly_resets_in_s and says why in its own
    # comment: "the board has no wall clock when tethered over USB, so the
    # daemon does the subtraction". usage_parse.c does read a "resets_at", but
    # that is the standalone WiFi path parsing the API's own JSON body, not
    # this message. A repo-wide search for a reader finds one hit, and it is a
    # host test using the name as a sample key for the generic string parser.
    #
    # They cost about 59 bytes together, on a line that measured 511 of 512.
    # A field no consumer reads is not "kept for readability" -- the docstring's
    # old claim -- when it is the reason the next real field will not fit.
    # The countdowns remain, and a reader that genuinely wants an absolute time
    # can add the countdown to the message's own arrival.
    return {
        "t": "usage", "v": VERSION,
        "session_pct": session_pct,
        "session_resets_in_s": session_resets_in_s,
        "weekly_pct": weekly_pct,
        "weekly_resets_in_s": weekly_resets_in_s,
        "stale": stale,
        **flat,
        **extra,
    }


# The keys on a usage message that move by themselves as the clock runs.
#
# The daemon looks at the local state files every FAST_POLL_INTERVAL_S and only
# writes the wire when something a reader could act on has changed -- "this
# message differs from the last one sent, ignoring the keys below". The list is
# deliberately an EXCLUSION and not an inclusion: a comparison that enumerates
# the fields it cares about goes quietly deaf the day a new one is added, and
# the failure is invisible, because the panel still updates every 60 s from the
# heartbeat and merely lags. Everything is meaningful by default; the only kind
# of field that ever belongs here is a timer. Put anything else in and every
# tick becomes a push -- thirty times the serial traffic carrying no news.
#
# Each of these is an age or a countdown: rebuilt from the wall clock on every
# poll, so different on every poll by construction, and already ticked locally
# by the firmware between pushes (usage_freshness.c grows the daemon's figure
# by the elapsed time; see the age_s note in usage() above). Re-sending them
# sooner tells the board nothing it was not already counting on its own.
VOLATILE_USAGE_KEYS = frozenset({
    "age_s", "p2_age_s", "active_age_s",
    "session_resets_in_s", "weekly_resets_in_s",
    "p2_s_in_s", "p2_w_in_s",
})


def meaningful_usage(msg: dict) -> dict:
    """The part of a usage message that says something.

    Compare two of these to answer "would a person notice the difference?".
    The absolute *_resets_at stamps stay in on purpose: those move only when
    the window itself rolls over, which is precisely an event worth a push,
    while the *_in_s countdowns derived from them move every single second.
    """
    return {k: v for k, v in msg.items() if k not in VOLATILE_USAGE_KEYS}


def frame_to_usage(frame, now_epoch: float, secondary=None) -> dict:
    """Turn a NormalizedUsageFrame into the usage message for the board.

    The single crossing point from provider-space to wire-space. Providers
    never build protocol messages themselves -- if they did, a second provider
    would be free to invent its own field names and the firmware would need to
    learn each one. Everything upstream produces frames; this function is the
    only thing that decides what a frame looks like on the wire.
    """
    return usage(
        frame.session_pct, frame.session_resets_at,
        frame.weekly_pct, frame.weekly_resets_at,
        [],
        session_resets_in_s=secs_until(frame.session_resets_at, now_epoch),
        weekly_resets_in_s=secs_until(frame.weekly_resets_at, now_epoch),
        stale=frame.stale,
        provider=frame.provider, src=frame.src,
        # One light for the whole desk: the worse of the two providers'
        # states, and the counts of both. The light is a claim on the person
        # ("something needs you"), and a Codex session waiting on them is
        # exactly as much their turn as a Claude one -- whichever page is
        # in front.
        state=worst_of((frame.state,
                        secondary.state if secondary else STATE_UNKNOWN)),
        n_sess=frame.n_sessions() + (secondary.n_sessions() if secondary
                                     else 0),
        n_run=frame.n_run + (secondary.n_run if secondary else 0),
        n_wait=frame.n_wait + (secondary.n_wait if secondary else 0),
        n_stuck=frame.n_stuck + (secondary.n_stuck if secondary else 0),
        n_agents=frame.n_agents + (secondary.n_agents if secondary else 0),
        p2=(secondary.provider if secondary else ""),
        p2_session_pct=(secondary.session_pct if secondary else UNKNOWN),
        p2_weekly_pct=(secondary.weekly_pct if secondary else UNKNOWN),
        p2_session_resets_in_s=(secs_until(secondary.session_resets_at,
                                           now_epoch) if secondary else -1),
        p2_weekly_resets_in_s=(secs_until(secondary.weekly_resets_at,
                                          now_epoch) if secondary else -1),
        p2_stale=(secondary.stale if secondary else False),
        burn_pph=frame.session_burn_pph,
        # The reading's own age, which only this side knows: observed_at is
        # the source file's mtime and never leaves provider-space otherwise.
        age_s=_age_of(frame, now_epoch),
        p2_age_s=_age_of(secondary, now_epoch),
        # And the other question: how long since anything on this desk said
        # anything at all. The freshest of the two pages, because the panel
        # sleeps as one screen -- see usage() for why the two ages come
        # apart and what the board does with each.
        active_age_s=_active_age(frame, secondary, now_epoch),
    )


def _active_age(frame, secondary, now_epoch: float) -> int:
    """The age of the freshest contact on the bus, or -1 when nothing says.

    The MINIMUM of the two providers' active ages, not of their reading
    ages: a Codex rollout written a minute ago is evidence that somebody is
    at this machine even on a day when the Claude page holds the dial.
    """
    ages = [a for a in (_age_of(frame, now_epoch, "active_at"),
                        _age_of(secondary, now_epoch, "active_at"))
            if a >= 0]
    return min(ages) if ages else -1


def _age_of(frame, now_epoch: float, attr: str = "observed_at") -> int:
    """Seconds since `frame` was observed, or -1 when that is unknowable.

    Clamped at zero rather than allowed to go negative. observed_at is a file
    mtime from a machine whose clock we do not own, and a file stamped a few
    seconds into the future is a real thing that happens; "-3 s ago" would
    reach fmt_age() as a negative and print "never", which is the one answer
    that is certainly wrong for a reading we are holding in our hand.

    `attr` picks WHICH epoch is being aged -- the reading's own
    (`observed_at`) or the freshest contact behind it (`active_at`) -- so
    both wire ages are computed by one clamp and one unknown rule rather
    than by two that could drift apart.
    """
    if frame is None or getattr(frame, attr, None) is None:
        return -1
    return max(0, int(now_epoch - getattr(frame, attr)))


EDITIONS = ("claude", "codex")


def edition(name: str) -> dict:
    """Tell the board which edition it is -- which boot clip to play.

    A factory step, sent once after programming, not a user setting. The board
    persists it and applies it on the next boot, because what it selects is a
    boot animation.

    Additive like everything else on this wire: firmware that predates it
    ignores an unknown message type, so sending one to an older board is a
    no-op rather than an error. `blink provision` reports what the board
    actually says, which is the only way to tell those two apart.
    """
    if name not in EDITIONS:
        raise ValueError(f"unknown edition {name!r}; expected one of "
                         + ", ".join(EDITIONS))
    return {"t": "edition", "v": VERSION, "edition": name}


# 24 bytes. STATUS_MAX_W is 300 px and usage_layout.h records
# "Reading is old - showing last known" (35 characters) as the string that
# sized it, so "Waiting for you - " leaves roughly 17 characters of room.
# This is a BYTE bound and the panel's is a PIXEL one; they are different
# questions and only this one can be answered here, which is why the firmware
# also sets LV_LABEL_LONG_DOT.
SESSION_LABEL_MAX_BYTES = 24


def session(label: str, n: int) -> dict:
    """Which project the board should name, and how many share the state.

    Its own message type rather than a field on the usage frame, and that is
    a measurement rather than a preference: the usage line was measured at
    506 of MAX_LINE_BYTES=512 fully loaded, proto.c drops an over-long line
    whole, and a label is more than six bytes. Additive like `edition` --
    firmware that predates it ignores an unknown type, so an older board
    keeps the behaviour it has today.

    Sent on change and on every connect, not every poll: the numbers move
    constantly and the project name does not.
    """
    msg = {"t": "session", "v": VERSION, "n": int(n)}
    if label:
        # Truncate on a CHARACTER boundary that survives the byte bound --
        # slicing bytes can halve a multibyte sequence and produce a field
        # that cannot be decoded at the other end.
        trimmed = label.encode("utf-8")[:SESSION_LABEL_MAX_BYTES]
        msg["label"] = trimmed.decode("utf-8", "ignore")
    return msg


def status(state: str, detail: str = "") -> dict:
    return {"t": "status", "v": VERSION, "state": state, "detail": detail}


# --- OTA over the serial link (see pc/ota.py and the OTA block in proto.c) ---


def ota_avail(version, size, sha256, app=None):
    """`app` is the daemon version this release also carries, when it is newer
    than the one running. Additive and optional: firmware that predates it
    ignores the field, which is the whole reason the protocol version does not
    have to move for this."""
    msg = {"t": "ota_avail", "v": VERSION, "version": version,
           "size": int(size), "sha256": sha256}
    if app:
        msg["app"] = app
    return msg


def ota_begin(version):
    """The firmware write is starting now.

    Distinct from consent. The board used to persist "I am installing X" the
    moment it sent ota_flash, but in a pair update this program replaces
    itself first -- and opening the serial port from the new process resets
    the board, which then boots, sees a breadcrumb for a version it is not
    running, and reports "Update failed, previous version restored." before
    the firmware install has even begun. Then the real install finishes with
    the breadcrumb already spent, so the success notice never appears either.
    """
    return {"t": "ota_begin", "v": VERSION, "version": version}


def ota_resume(version):
    """Continuing an install the user already approved, after the daemon
    replaced itself. The board reopens its progress screen rather than sitting
    on an "Install?" prompt for something already under way."""
    return {"t": "ota_resume", "v": VERSION, "version": version}


def ota_none(app=None):
    """Nothing to install on the BOARD -- which says nothing about the app.

    `app` carries the same fact ota_avail does, for the case that message
    cannot reach: firmware current, daemon behind. The board had no way to
    learn it there, so it inferred one, by comparing its own firmware version
    against the daemon's and calling anything lower "old". That is a different
    question -- "older than me" rather than "older than the release" -- and it
    is false on every board running ahead of the feed, which is every
    developer's desk from the first local build onwards.

    Additive and optional, like ota_avail's: firmware that predates it ignores
    the field, so the protocol version does not move.
    """
    msg = {"t": "ota_none", "v": VERSION}
    if app:
        msg["app"] = app
    return msg




def ota_error(why=""):
    return {"t": "ota_error", "v": VERSION, "why": why}

# --------------------------------------------------------------- overage cap

# The first firmware that will accept a percentage above 100.
#
# Every earlier build parses the usage percentages with
# num(json, "weekly_pct", &wp, -1, 100), and proto.c's num() does not clamp an
# out-of-range value -- it returns without writing, leaving the caller's own
# `double sp = 0, wp = 0` initialiser in place. So a reading of 102 does not
# arrive as 100, it arrives as ZERO: at the exact moment somebody crosses their
# weekly limit into extra usage, the panel drops from full to empty.
#
# Observed on a customer's board 2026-08-31. He was running two sources, and
# they disagreed in a way that made it unmistakable: Claude Desktop's cache
# caps its own figure at 100 and drew a full ring, Claude Code reported the
# true 102 and drew nothing. The normalizer merges field-by-field by recency,
# so the two took turns being newest and the ring flipped 100 -> 0 -> 100 -> 0
# every minute.
#
# Holding the number at 100 for those boards is a loss -- "at the limit" and
# "2% past it" become the same picture -- but it is the honest half of the
# truth instead of the opposite of it, and it needs no reflash, which is what
# matters for boards already on desks. Newer firmware is sent the real number
# and draws a full ring under it.
#
# This is 1.2.5 and not 1.2.4 on purpose. The PCT_MAX change and the version
# bump are separate hunks, so a 1.2.4 build without PCT_MAX is constructible --
# one was flashed to a bench board while this was being written. Gating on the
# version that shipped the two together is the only claim the daemon can make
# honestly from a version string alone. The principled fix is for hello to
# advertise the capability rather than have the daemon infer it; that is worth
# doing the next time hello changes for another reason.
FW_ACCEPTS_OVERAGE = (1, 2, 5)

# Every field proto.c reads with a 0..100 range. sonnet_pct and opus_pct are
# flattened by usage() but no firmware parses them yet, so they are not here;
# add them the day one does.
_PCT_KEYS = ("session_pct", "weekly_pct", "fable_pct",
             "p2_session_pct", "p2_weekly_pct")


def _fw_tuple(fw):
    """("1.2.4") -> (1, 2, 4). None for anything unparseable."""
    if not isinstance(fw, str):
        return None
    parts = fw.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(x) for x in parts)
    except ValueError:
        return None


def cap_overage_for_fw(msg, board_fw):
    """Hold percentages at 100 for a board that would otherwise show zero.

    Returns a new message; the input is not mutated. Only values ABOVE 100 are
    touched -- -1 is the "unknown" sentinel and 0 is a real reading, and both
    have to travel untouched.

    An unknown board version caps. The daemon greets a board before it has
    heard a hello (bridge.greet), so `None` here means "not yet told", not
    "modern", and guessing modern is the one guess that puts a zero on a panel.
    """
    if not isinstance(msg, dict):
        return msg
    out = dict(msg)
    fw = _fw_tuple(board_fw)
    if fw is not None and fw >= FW_ACCEPTS_OVERAGE:
        return out
    for k in _PCT_KEYS:
        v = out.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if not math.isfinite(v):
            # nan fails every comparison, so it would pass the cap below
            # untouched, and json.dumps writes a bare NaN -- not valid JSON.
            # msg_get_double then leaves proto.c's `double wp = 0` in place:
            # the original bug, by another road.
            out[k] = -1.0
        elif v > 100:
            out[k] = 100.0
    return out
