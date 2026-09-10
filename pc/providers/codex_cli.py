"""Codex CLI's rollout log, as a second provider on the ingestion bus.

Same shape as every other source here: read a figure another program has
already worked out, from a file it writes for its own reasons. Nothing in
this module authenticates to anyone, and it never reads a prompt, a reply or
a tool call -- only the `rate_limits` object Codex records alongside its
token counts.

Where the numbers are
---------------------
Codex appends one JSON object per line to

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<stamp>-<uuid>.jsonl

and after a turn completes it writes an `event_msg` whose payload type is
`token_count`. That payload carries `rate_limits`:

    "rate_limits": {
      "primary":   {"used_percent": 0.0, "window_minutes": 300,   "resets_at": ...},
      "secondary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": ...}
    }

300 minutes is the five-hour window and 10080 is the seven-day one, which is
exactly the pair the panel draws.

Three things about that file are worth stating here, because each is a way to
be confidently wrong rather than to fail:

  - `primary` and `secondary` are POSITIONS, not windows. This matches them by
    `window_minutes` and only falls back to the position when the field is
    missing, for the same reason claude_desktop range-checks its percentages:
    a rename upstream should cost us the source, not swap the two dials.

  - `resets_at` is an absolute epoch in SECONDS, unlike Claude Desktop's
    sample timestamps, which are milliseconds. Verified against a real file,
    2026-08-27; the sanity bound below is what would catch it changing.

  - Codex compresses a rollout to `.jsonl.zst` once it is a week old
    (codex-rs/rollout/src/compression.rs, MIN_ROLLOUT_AGE). The glob below
    sees only plain `.jsonl`, which is right for as long as that floor is
    days: the freshest reading is always in a live, plain file, and a file
    old enough to be compressed is a week past being stale anyway.
    tests/ci/check_codex_contract.sh fails the day the floor drops.

  - The newest file is not always the newest reading. A terminal left open
    with no turn in it produces a rollout file with no `token_count` at all,
    and its mtime still moves. So this reads the few most recently touched
    files and takes the freshest event out of all of them, rather than
    trusting the first one it finds.
"""
import glob
import json
import os
from datetime import datetime, timezone

from pc.providers import base
from pc.providers import codex_state

PROVIDER_ID = "codex"
SRC_ID = "cli"

# Same bound, and for the same reason, as the other two file-backed sources:
# Codex only writes this while it is running, so age here measures how long
# ago you last used Codex, not how wrong the figure is.
STALE_AFTER_S = 1800

# Which window each entry describes, by its own declared length. The tolerance
# is generous because these are round numbers chosen by a product, not
# measurements: anything under a day is the short window, anything longer is
# the long one.
SESSION_WINDOW_MAX_MIN = 60 * 24

# Plausible bounds for a reset timestamp: 2020-01-01 to 2100-01-01. What this
# would notice is the unit changing to milliseconds.
RESET_EPOCH_MIN = 1_577_836_800
RESET_EPOCH_MAX = 4_102_444_800

# How many rollout files to look back through. Enough to cover a handful of
# terminals open at once; small enough that this stays a few stat calls and a
# tail read rather than a walk of a year of history.
RECENT_FILES = 6

# Only the end of a rollout file can hold the newest reading, and a long
# session's file is megabytes of conversation. Read the tail and parse what
# survives; a line cut in half at the front simply fails to parse and is
# skipped, which is the behaviour we would want from a malformed line anyway.
TAIL_BYTES = 256 * 1024


def sessions_root() -> str:
    """Where Codex keeps its rollout logs.

    Returns the path whether or not it exists: not having Codex installed is
    an ordinary state, handled by the reader rather than by pretending we do
    not know where to look. CODEX_HOME is honoured because Codex itself does.
    """
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(home, "sessions")


def recent_rollouts(root: str = None, limit: int = RECENT_FILES):
    """The most recently modified rollout files, newest first."""
    root = sessions_root() if root is None else root
    try:
        paths = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl"))
    except OSError:
        return []
    out = []
    for p in paths:
        try:
            out.append((os.path.getmtime(p), p))
        except OSError:
            continue        # deleted between the glob and the stat
    out.sort(reverse=True)
    return [p for _, p in out[:limit]]


def _tail_lines(path: str):
    """The last TAIL_BYTES of a file, as lines. [] on any read failure."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            blob = f.read()
    except OSError:
        return []
    try:
        text = blob.decode("utf-8", "replace")
    except Exception:
        return []
    return text.splitlines()


# The other end of the same file. `session_meta` -- the record that carries
# the project's cwd, and so the only name a Codex session can ever have -- is
# line 1, and the tail read above will never see it: the biggest rollout on
# the machine this was written against is 51 MB, so TAIL_BYTES would have to
# grow to the size of the file to reach the front of it. The name therefore
# gets its own small read, of the head, and the two never meet.
#
# 128 KB rather than the "a few KB" a line of JSON sounds like. `session_meta`
# embeds `base_instructions`, and the four real rollouts here have first lines
# of 18-19 KB. That length belongs to Codex -- it grows whenever upstream adds
# a paragraph to its system prompt -- so the bound is set at several times the
# observation rather than just over it, half of TAIL_BYTES, because the way
# this fails is silent: a first line that outgrows the bound costs the name on
# every session at once and looks exactly like a session that has none.
# Read once per file and then cached, so the size is paid once, not per poll.
HEAD_BYTES = 128 * 1024


def _head_line(path: str) -> str:
    """The first COMPLETE line of a file, or "".

    Complete is the whole point. A line with no newline inside HEAD_BYTES is
    refused rather than returned in part: a JSON object cut in half decodes
    as nothing, and handing the fragment back would only move the failure
    into json.loads. A rollout being written at this instant is the ordinary
    case for that, and it will have its newline by the next poll.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read(HEAD_BYTES)
    except OSError:
        return ""
    nl = blob.find(b"\n")
    if nl < 0:
        return ""
    # rstrip the CR, because a line does not include its terminator.
    #
    # The read is binary -- it has to be, to find the newline in a 51 MB file
    # without decoding it -- so Python's universal-newline translation never
    # runs and a file written with CRLF hands back a trailing "\r". Any writer
    # that opened the rollout in text mode on Windows produces exactly that.
    #
    # json.loads happens to tolerate it (a CR is whitespace), which is why this
    # went unnoticed; the substring checks in session_meta_cwd would not have.
    return blob[:nl].decode("utf-8", "replace").rstrip("\r")


def session_meta_cwd(head_line: str):
    """The `cwd` out of a rollout's first line, or None.

    None rather than "": the caller has two different failures to tell apart
    -- a head it could not read, and a directory it read and then refused --
    and only one of them is worth ever looking at again.
    """
    if "session_meta" not in head_line:
        return None         # cheap reject before parsing 19 KB of JSON
    try:
        line = json.loads(head_line)
    except ValueError:
        return None
    if not isinstance(line, dict) or line.get("type") != "session_meta":
        return None
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) else None


def rollout_session_id(path: str) -> str:
    """The session id out of a rollout's first line, or "".

    Same record as `session_meta_cwd` reads -- line 1 of every rollout, in
    both `codex exec` and interactive sessions -- and the id in it is a bare
    UUID that appears verbatim in the rollout's own filename. The plan this
    was built from expected the hook to report a differently-shaped id
    (`thr_...`) that would need reconciling with this one; a real capture
    taken 2026-09-02 showed both spellings are the same UUID, so there is
    nothing here to reconcile. This id is still the only field the hook's
    report and this file share, and it is what lets Task 7 fold one session
    into one row instead of two.

    Deliberately built on `_head_line`/`HEAD_BYTES` rather than a second
    bounded read: the two already answer "can I trust the first line of this
    file at all", and a session id one field over from `cwd` in the same
    record does not earn a second implementation of that answer.

    Every failure here -- an unreadable file, an unterminated first line, a
    first line that is not JSON, a record that is not a `session_meta`, a
    payload that is not an object, an id that is not a string -- returns ""
    rather than a guess. The caller treats "" as "cannot be matched to a
    hook slot": the session still counts once, from the rollout, and a wrong
    id merged into the wrong slot -- which would be worse than a session
    left unmatched -- never happens.
    """
    head_line = _head_line(path)
    if "session_meta" not in head_line:
        return ""           # cheap reject before parsing JSON, as above
    try:
        rec = json.loads(head_line)
    except ValueError:
        return ""
    if not isinstance(rec, dict) or rec.get("type") != "session_meta":
        return ""
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return ""
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) else ""


# Both, not os.sep. This file may be read on one platform and written on
# another -- a synced home directory is ordinary -- and Codex on Windows
# writes C:\Users\....
_NAME_SEPARATORS = "/\\"


def _project_name(cwd) -> str:
    """The project name a Codex `cwd` implies, or "".

    The last path component, with three refusals. The first two are the ones
    the Claude hook shim already applies to its own cwd: `.` and `..` are
    directory entries rather than names, and control characters are stripped
    because this string is JSON-encoded into a line the firmware scans for
    quotes.

    The double quote goes with them, on that same sentence's reason rather
    than a new one, and it was the half the sentence promised and did not
    do. firmware/src/msg_parse.c reads a string value by copying the bytes
    between the first `"` after the key's colon and the NEXT `"`, and
    unescapes nothing. So a project called mid\"way -- json.dumps writes it
    onto the wire correctly, as a backslash and a quote -- arrives at the
    panel as `mid` and a trailing backslash, because msg_get_str stops at the
    escaped quote and hands back the escape character with it. A double quote
    is a legal character in a directory name on every platform this reads
    rollouts from.

    Stripped, not refused, which is how the control characters beside it are
    handled and for the same reason: mid\"way is still recognisably the
    owner's project as `midway`, while a name that was ONLY quotes falls
    through to the drawable-ASCII rule below and lets the count speak.

    The backslash needs no rule of its own: it is the other path separator,
    so the rsplit above has already thrown away everything up to the last
    one. Quote and backslash are the only two characters json.dumps escapes
    once the control characters are gone, so with those two accounted for
    nothing reaches msg_get_str that it will misread.

    The third is about the panel rather than about safety, and it is the one
    the shim has no need of. firmware/src/fmt.c draws a label through
    fmt_ascii(), which replaces every codepoint it has no ASCII spelling for
    with "?" -- tests/fmt/host_test.c pins the UTF-8 bytes of an e-acute
    (\\xc3\\xa9) arriving as "?".
    A wholly non-Latin name therefore reaches the desk as a row of question
    marks, which says less than the count the panel falls back to when there
    is no name at all. So a name has to carry at least one ASCII letter or
    digit to be worth sending: a name with a Latin stem keeps it and loses
    the rest, and one with no Latin at all is refused so the count speaks
    instead.

    Not capped here, and the quote rule above is the same division read from
    the other side. protocol.session owns the WIRE: the byte bound, and the
    truncation on a UTF-8 boundary that the bound needs. This function owns
    what may be a NAME, which is what a character class is -- and putting the
    quote rule in protocol.session would apply one provider's bug to every
    provider's label, while a second byte cap here would be a second thing to
    keep in step with the firmware.
    """
    if not isinstance(cwd, str):
        return ""
    name = cwd
    while name and name[-1] in _NAME_SEPARATORS:
        name = name[:-1]
    for sep in _NAME_SEPARATORS:
        name = name.rsplit(sep, 1)[-1]
    name = "".join(c for c in name
                   if c >= " " and c != "\x7f" and c != '"')
    # Deliberately redundant with the drawable-ASCII rule below, which
    # refuses "." and ".." as well: neither carries a letter or a digit. The
    # two answer different questions, and the ASCII one is much the likelier
    # to be relaxed -- the day the firmware grows a font for it, a directory
    # entry must still not become a project name.
    if name in ("", ".", ".."):
        return ""
    if not any(("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9")
               for c in name):
        return ""
    return name


def _epoch(value):
    """A reset timestamp, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not (RESET_EPOCH_MIN <= value <= RESET_EPOCH_MAX):
        return None
    return int(value)


def _pct(window):
    """`used_percent` out of one window object, or UNKNOWN.

    Range-checked rather than merely type-checked: a number outside 0-100
    means the field has changed meaning upstream, and the honest answer to
    that is "we don't know", not a saturated dial.
    """
    if not isinstance(window, dict):
        return base.UNKNOWN
    v = window.get("used_percent")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return base.UNKNOWN
    # 1000, not 100 -- see claude_desktop._pct. A percentage above 100 is what
    # extra usage looks like, not a field that changed meaning.
    if not (0 <= v <= 1000):
        return base.UNKNOWN
    return float(v)


def _classify(rate_limits: dict):
    """(session_window, weekly_window) out of a `rate_limits` object.

    By declared length first, by position second. Both may be None.
    """
    if not isinstance(rate_limits, dict):
        return None, None
    primary = rate_limits.get("primary")
    secondary = rate_limits.get("secondary")

    session = weekly = None
    for w in (primary, secondary):
        if not isinstance(w, dict):
            continue
        minutes = w.get("window_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            continue
        if minutes <= SESSION_WINDOW_MAX_MIN:
            session = session or w
        else:
            weekly = weekly or w

    # Nothing declared its length. Fall back to the positional meaning, which
    # is right for every file seen so far and is only reached when the field
    # this prefers has gone away. Objects only: a string here reached
    # `.get("resets_at")` in the caller and took the source down.
    if session is None and weekly is None:
        session = primary if isinstance(primary, dict) else None
        weekly = secondary if isinstance(secondary, dict) else None
    return session, weekly


def _observed_at(line: dict, mtime: float) -> float:
    """When Codex wrote the reading, not when we read it.

    The event carries its own ISO-8601 timestamp, which survives the file
    being copied or touched. mtime is the fallback, and is what every other
    source here uses.
    """
    stamp = line.get("timestamp")
    if isinstance(stamp, str):
        try:
            # Python 3.11 parses "Z"; earlier versions do not, and the daemon
            # ships frozen against 3.12, so this is belt and braces.
            t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            epoch = t.timestamp()
        except (ValueError, OverflowError, OSError):
            # OSError: Windows refuses to convert dates before 1970.
            return mtime
        # Bounded like resets_at below. A stamp from a wrong clock -- or a
        # crafted one -- in the far future would otherwise read as fresh
        # forever and win every recency contest.
        if RESET_EPOCH_MIN <= epoch <= RESET_EPOCH_MAX:
            return epoch
    return mtime


def _has_window(limits) -> bool:
    """Does this `rate_limits` object yield a percentage for either window?"""
    session, weekly = _classify(limits)
    return _pct(session) >= 0 or _pct(weekly) >= 0


def parse_rollout_tail(lines, mtime: float):
    """The newest USABLE (rate_limits, observed_at, limit_reached), or Nones.

    Scanned backwards: the answer is at the end of the file, and a long
    session has thousands of lines that are not it.

    "Usable" is the whole point of this function, and it was learned the hard
    way. When the five-hour limit runs out, Codex does not write 100 -- it
    writes the SAME `rate_limits` envelope with `primary` and `secondary` both
    null, and flips `limit_id` from "codex" to "premium". Measured on this
    desk 2026-09-05, half a second apart:

        08:05:52.357Z  limit_id=codex    5h=98    wk=16
        08:05:52.914Z  limit_id=premium  5h=null  wk=null

    An earlier version stopped at the newest line merely CONTAINING
    "rate_limits". That is the null one, `parse_cli_event` rightly refuses a
    frame with no numbers in it, and the caller then dropped the entire file
    -- including the 98 sitting two lines above. The freshest reading left
    anywhere was a twenty-minute-old 29 from a Codex desktop thread, and the
    panel showed THAT, aged, at the exact moment the person was blocked and
    most needed the dial to be right. Reported from the field by the owner.

    So: keep scanning past window-less envelopes to the newest one that
    actually carries a number, and tell the caller whether the account walked
    off the end. `limit_reached` is deliberately keyed on `limit_id` CHANGING
    rather than on the nulls alone -- a transient null from an upstream that
    momentarily omits the block would otherwise saturate the dial to 100 and
    tell someone they are blocked when they are not, which is the worse of
    the two wrong answers.
    """
    top = None                  # newest envelope, usable or not
    top_observed = None
    for raw in reversed(lines):
        if "rate_limits" not in raw:
            continue        # cheap reject before the parse
        try:
            line = json.loads(raw)
        except ValueError:
            continue        # a half line at the head of the tail, or garbage
        if not isinstance(line, dict):
            continue
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            continue
        observed = _observed_at(line, mtime)
        if top is None:
            top, top_observed = limits, observed
        if _has_window(limits):
            if limits is top:
                return limits, observed, False
            # An older reading, reached only because everything newer was
            # window-less. Report it as observed WHEN THE ACCOUNT RAN OUT,
            # not when this older line was written: the fact being reported
            # is current even though the number carrying it is not, and
            # dating it backwards would make the panel call it stale and
            # hide it.
            reached = _limit_id(top) != _limit_id(limits)
            return limits, (top_observed if reached else observed), reached
    # Either no envelope at all, or every one of them window-less. Hand the
    # newest back regardless: `parse_cli_event` refuses it, which keeps a
    # file with nothing to say from winning a contest about numbers.
    return top, top_observed, False


def _limit_id(limits):
    """`limit_id`, or None. The bucket the account is being charged against."""
    if not isinstance(limits, dict):
        return None
    value = limits.get("limit_id")
    return value if isinstance(value, str) else None


# --- execution state ---------------------------------------------------------
#
# Codex has no hook interface, but its rollout log is a journal of the same
# transitions: `task_started` when a turn begins, `task_complete` when the
# answer is in -- or, when that record carries an `error`, when the turn died
# instead -- and `turn_aborted` when the person stopped it. The newest of
# these in a file is that session's state, aged by its own timestamp, with
# the same threshold Claude's hooks use -- anything past ABANDONED_AFTER_S is
# a session that is gone. No `stuck` from silence, for the reason given in
# claude_state's docstring: a long turn and a wedged one look the same.
#
# Permission prompts are not in the journal as far as anyone has observed, so
# there is no `waiting` for Codex; a prompt shows as `running` until it is
# answered. Honest, if less useful.
from pc.providers.claude_state import (ABANDONED_AFTER_S,  # noqa: E402
                                       T_EPOCH_MIN, T_EPOCH_MAX)

STATE_SRC_ID = "cli-state"
_TURN_EVENTS = {
    "task_started": base.STATE_RUNNING,
    "task_complete": base.STATE_IDLE,
    "turn_aborted": base.STATE_IDLE,
}

# `turn_aborted` is NOT a failure, and the temptation to make it one is why
# this paragraph exists. Its `reason` is one of `interrupted`, `replaced`,
# `review_ended` or `budget_limited` (protocol.rs TurnAbortReason): Esc, a
# new message typed over the running turn, a review closing, a budget
# stopping it. Every one of those is something the PERSON did, and idle --
# "finished, your turn" -- is the right colour for all four. Red is reserved
# for what the model or the API did.
#
# That is `task_complete` carrying an `error`. TurnCompleteEvent.error is
# documented upstream as "Terminal error details when the turn completed
# unsuccessfully", is skipped entirely on success, and its CodexErrorInfo
# includes UsageLimitExceeded -- which is the single event this product
# exists to warn about, and the mirror of Claude's StopFailure error:
# "rate_limit".
#
# NEVER OBSERVED. Every rollout captured on the machine this was written
# against is a success, so this branch rests entirely on a reading of
# upstream's Rust schema -- TurnCompleteEvent.error, CodexErrorInfo, and the
# two deny-listed variants -- rather than on a real file. The contract
# script's checks for that schema (tests/ci/check_codex_contract.sh) arrive
# with Task 8 of this plan, not this one. It is therefore written to degrade
# toward idle: an `error` that is not an object leaves the mapping above
# exactly as it was.

# The two errors upstream itself says do not fail a turn
# (CodexErrorInfo::affects_turn_status). Both are failures of a client
# OPERATION rather than of the turn, and painting the panel red because a
# thread rollback failed would cry wolf with the one colour that must not.
#
# A deny-list rather than an allow-list, and that direction is deliberate: a
# variant added upstream tomorrow lands on the failing side, which is where
# `Other` already is. A unit variant serialises as a bare snake_case string
# and a struct variant as a single-key object, so both shapes are checked.
_NOT_A_TURN_FAILURE = ("thread_rollback_failed", "active_turn_not_steerable")


def _is_turn_failure(error) -> bool:
    """Does this `task_complete` error mean the turn itself failed?"""
    if not isinstance(error, dict):
        return False
    info = error.get("codex_error_info")
    if isinstance(info, str):
        return info not in _NOT_A_TURN_FAILURE
    if isinstance(info, dict) and len(info) == 1:
        return next(iter(info)) not in _NOT_A_TURN_FAILURE
    # Absent, null, or a shape this version has never seen. Upstream's rule
    # for a missing CodexErrorInfo is that the turn failed
    # (ErrorEvent::affects_turn_status is is_none_or), and so is this: an
    # error object with nothing legible in it is still an error object.
    return True


def parse_rollout_state(lines, now_epoch):
    """The execution state one rollout file implies, or STATE_UNKNOWN.

    Scanned backwards like the rate limits: the newest turn event is the
    answer. A file with no turn event yet -- a session opened and not typed
    into -- makes no claim, for the same reason a Claude SessionStart does not.
    """
    for raw in reversed(lines):
        if "task_" not in raw and "turn_aborted" not in raw:
            continue        # cheap reject before the parse
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict) or line.get("type") != "event_msg":
            continue
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        state = _TURN_EVENTS.get(kind)
        if state is None:
            continue
        if kind == "task_complete" and _is_turn_failure(payload.get("error")):
            state = base.STATE_FAILED
        t = _observed_at(line, float("nan"))
        if not (T_EPOCH_MIN <= t <= T_EPOCH_MAX):
            return base.STATE_UNKNOWN   # no usable timestamp: no age, no claim
        age = now_epoch - t
        if age < 0:
            age = 0.0                   # a clock that stepped; treat as fresh
        if age > ABANDONED_AFTER_S:
            return base.STATE_UNKNOWN
        return state
    return base.STATE_UNKNOWN


class CodexCliProvider(base.ProviderParser):
    def __init__(self, root=None, state_dir=None, sweep=True):
        self._root = root
        # The hook slot directory, injectable for exactly the reason `root`
        # is: a test must never be able to reach the real one, because the
        # scan behind it DELETES abandoned files. `sweep` is injectable for
        # the other half of that -- a caller that only wants to look, such as
        # `blink status`, must be able to look without collecting, since a
        # diagnostic that deletes what it is diagnosing destroys the evidence
        # somebody ran it to see.
        self._state_dir = state_dir
        self._sweep = sweep
        # path -> project name, for the one record in a rollout that cannot
        # change. `session_meta` is line 1 of an append-only file whose name
        # carries a UUID, so a path is never reused and the answer for a path
        # is fixed for the life of the file.
        #
        # The saving is not CPU. Even at the two-second fast poll, 19 KB of
        # json.loads is under a millisecond. It is that the SIZE of that
        # line belongs to Codex: base_instructions is embedded in it,
        # and re-deriving an immutable value from a blob upstream is free to
        # grow is waste that grows with it. Pruned to the current file set on
        # every poll, so this is bounded by RECENT_FILES rather than by how
        # long the daemon has been up.
        #
        # Per instance, not per class: a mutable default here would make the
        # cache shared by every provider in the process.
        self._names = {}

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def root(self):
        return self._root if self._root is not None else sessions_root()

    def _name_for(self, path):
        """The project name for one rollout, read once per file.

        An answer is cached once the first line has been READ, and only then.
        That is the whole of the rule, and the distinction it turns on is the
        one `session_meta_cwd` was written to hand back: a head this could
        not read at all, and a head it read and then found nothing usable in.

        The second is permanent and is cached, empty answer included. A
        rollout is append-only and its line 1 never changes, so a first line
        that is complete and carries no `cwd` -- or a `cwd` `_project_name`
        refuses -- will still carry none on the ten thousandth poll, and
        re-deriving that from 19 KB of embedded system prompt every two
        seconds is waste that grows with whatever upstream puts in that
        record next.

        The first is not permanent, and caching it was a bug. Codex creates a
        rollout and then writes its ~19 KB `session_meta` into it, and this
        daemon now looks every two seconds on the fast poll -- not the
        minute this cache was written under -- so globbing that file between
        the two is ordinary rather than rare. `_head_line` correctly refuses
        the unterminated line, "" came back, and "" was then remembered
        FOREVER: `_prune_names` only forgets paths that have left the recent
        set, and the session being written to is the one that stays in it for
        hours. The panel then said "A session is waiting for you" with no
        subject, permanently, for the session the owner is most likely
        looking at. So an unread head is left uncached and simply asked again
        next poll, which is a 128 KB read of a file that was written
        milliseconds ago and is still in the page cache.

        A first line longer than HEAD_BYTES is the one case that pays that
        re-read forever rather than for one poll. It is not worth a third
        branch: that session has already lost its name on every poll anyway,
        which is the failure HEAD_BYTES is set several times over the
        observed length to avoid in the first place.
        """
        cached = self._names.get(path)
        if cached is not None:
            return cached           # "" is a real answer here, not a miss
        head = _head_line(path)
        if not head:
            return ""               # unread, not unnamed: ask again next poll
        name = _project_name(session_meta_cwd(head))
        self._names[path] = name
        return name

    def _prune_names(self, known):
        """Forget every cached name whose file is no longer being read."""
        self._names = {p: n for p, n in self._names.items() if p in known}

    def parse_cli_event(self, raw_payload, now_epoch, observed_at,
                        limit_reached=False):
        """One `rate_limits` object, already read, as a frame.

        Returns None when neither window yields a percentage: a frame with no
        numbers must not be allowed to win a recency contest for numbers it
        does not have.

        `limit_reached` says the account has run out and this reading is the
        last one taken before it did -- see parse_rollout_tail. The session
        window is then reported as full, because 98 and "you cannot send
        anything" are the same fact to the person looking at the panel, and
        the second one is what they need to know. Only the session window:
        the weekly one was at 16 when this fired and is genuinely not spent.
        """
        session, weekly = _classify(raw_payload)
        s_pct, w_pct = _pct(session), _pct(weekly)
        if s_pct < 0 and w_pct < 0:
            return None
        if limit_reached and s_pct >= 0:
            s_pct = 100.0
        return base.NormalizedUsageFrame(
            provider=PROVIDER_ID,
            src=SRC_ID,
            observed_at=observed_at,
            session_pct=s_pct,
            session_resets_at=_epoch((session or {}).get("resets_at")),
            weekly_pct=w_pct,
            weekly_resets_at=_epoch((weekly or {}).get("resets_at")),
            stale=(now_epoch - observed_at) > STALE_AFTER_S,
        )

    def poll(self, now_epoch):
        """The freshest reading Codex has written, as a one-frame list.

        One frame, not one per rollout file: the two percentages are
        account-wide, so several open terminals all describe the same pair of
        windows and handing the normalizer six copies of it would only make
        the freshest one win a contest it has already won here.
        """
        best = None
        # Keyed by session, not counted, because a hook slot below may be
        # describing one of these same sessions and has to REPLACE its entry
        # rather than land beside it. Counting first would make that
        # impossible: two counts cannot be de-duplicated after the fact, and
        # the session id is the only thing the two sources share.
        rollout_states = {}
        rollout_names = {}
        paths = recent_rollouts(self._root)
        self._prune_names(set(paths))
        for path in paths:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            lines = _tail_lines(path)
            # Every rollout is one session, so every one of them votes on
            # the execution state -- unlike the percentages, which are one
            # account-wide pair however many terminals are open.
            state = parse_rollout_state(lines, now_epoch)
            if state != base.STATE_UNKNOWN:
                # A rollout whose meta line could not be read falls back to
                # its own path as the key. It still counts exactly once, it
                # simply cannot be matched to a slot -- and a filesystem path
                # can never collide with a session id, so the fallback can
                # never merge a session into the wrong one, which is the
                # worse of the two errors.
                key = rollout_session_id(path) or path
                rollout_states[key] = state
                # A name is only collected from a session that made a claim.
                # A terminal opened and not typed into has a cwd and no turn,
                # and letting it lend its name would rename the panel after
                # the session that is actually doing something.
                rollout_names[key] = self._name_for(path)
            limits, observed_at, limit_reached = parse_rollout_tail(lines,
                                                                    mtime)
            if limits is None:
                continue
            frame = self.parse_cli_event(limits, now_epoch, observed_at,
                                         limit_reached)
            if frame is None:
                continue
            if best is None or frame.observed_at > best.observed_at:
                best = frame
        frames = [best] if best is not None else []

        # The hooks' answer, and where it wins.
        #
        # The rollout cannot see a permission prompt at all: Codex files its
        # approval events in the never-persisted arm of its own persistence
        # policy, and the real rollouts on this desk contain none of them. So
        # `running` from the rollout for a session whose hook said `waiting`
        # is not a disagreement to be settled on recency -- it is the older
        # and blinder of two answers, and the newer one simply replaces it.
        # That is the whole reason this is a dict update in one direction
        # rather than a merge that compares timestamps.
        #
        # Unioned rather than substituted, though: a Codex session that was
        # already open when the hooks were installed has no slot and never
        # will get one, because the hooks only fire from the next turn on.
        # Dropping it would make a running terminal vanish from the panel,
        # which is a worse error than not knowing that it is waiting.
        # Unioned, but not over a session the hook has BURIED.
        #
        # That is the correction the paragraph above needed. "No slot" was
        # read as "the hooks never saw this session", and it is also what a
        # session whose SessionEnd has already fired looks like -- the shim
        # removes the slot -- so the union put back every Codex session the
        # hook had just ended, and it kept putting them back until the rollout
        # aged out an hour later. Measured on the owner's desk: one live slot,
        # two Claude sessions, four recent rollouts, and a panel saying six.
        #
        # The tombstone is what tells the two absences apart. A rollout whose
        # id has one is a session somebody closed; a rollout with no tombstone
        # is a session nothing ever watched end, which is exactly the
        # pre-hooks case this union exists for, and it counts as it always did.
        #
        # Filtered before the update, never after: a hook slot is the newest
        # word on its own session, so a session that ended and was resumed
        # under the same id counts again the moment its slot reappears.
        hook_states, agents = codex_state.scan(
            now_epoch, path=self._state_dir, sweep=self._sweep)
        buried = codex_state.ended(
            now_epoch, path=self._state_dir, sweep=self._sweep)
        merged = {key: state for key, state in rollout_states.items()
                  if key not in buried}
        merged.update(hook_states)

        counts = {}
        for merged_state in merged.values():
            counts[merged_state] = counts.get(merged_state, 0) + 1

        if counts:
            # A separate frame with no percentages, exactly as Claude's state
            # provider does it: it can never win a recency contest for
            # numbers, and the normalizer merges its state field by field.
            state = base.worst_of(counts)
            # The names of the sessions holding the winning state, out of the
            # MERGED census rather than the rollout tally -- otherwise a
            # session the hook moved from `running` to `waiting` would be
            # looked up under the state it no longer holds. Only rollouts
            # carry a name at all: codex_state.scan drops the ones its slots
            # hold on purpose, so a hook-only session is a nameless holder
            # and silences the label exactly as an unreadable rollout does.
            held = [rollout_names.get(key, "")
                    for key, held_state in merged.items()
                    if held_state == state and rollout_names.get(key)]
            frames.append(base.NormalizedUsageFrame(
                provider=PROVIDER_ID,
                src=STATE_SRC_ID,
                observed_at=now_epoch,
                state=state,
                # Named only when exactly ONE session holds the state the
                # frame is reporting, which is the rule claude_state.poll
                # applies and for the same reason: a count says something
                # true about all of them, and a name picked from three says
                # something true about one and implies it about the rest.
                #
                # Both halves are needed. counts == 1 is what makes the name
                # unambiguous; len(held) == 1 is not implied by it, because a
                # session whose session_meta could not be read still votes on
                # the state and still has no name to lend -- two holders, one
                # of them nameless, must stay a count.
                label=(held[0] if counts.get(state, 0) == 1 and len(held) == 1
                       else ""),
                n_run=counts.get(base.STATE_RUNNING, 0),
                # n_wait was absent for as long as nothing could produce a
                # waiting Codex session. The hooks can, and protocol._pair_from
                # reads this field to decide what the panel's line says -- so
                # leaving it out would light an amber pip beside the words
                # "0 sessions".
                n_wait=counts.get(base.STATE_WAITING, 0),
                n_idle=counts.get(base.STATE_IDLE, 0),
                # Codex has no `stuck` -- no silence-based state, per the
                # module docstring above -- so counts.get(base.STATE_STUCK, 0)
                # is always 0 here and this fold is really just the failed
                # count. Written as a sum anyway, for symmetry with
                # claude_state.poll's identically-named field: the wire has
                # one count for "not working and not finished" shared by both
                # providers, and `state` above already says which of the two
                # it is. Mapping failed to nothing would leave a failed
                # session reporting zero -- "Session failed" where the panel
                # should say "Session failed - 2 sessions".
                n_stuck=(counts.get(base.STATE_STUCK, 0)
                         + counts.get(base.STATE_FAILED, 0)),
                n_agents=agents,
            ))
        return frames
