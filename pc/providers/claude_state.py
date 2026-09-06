"""Execution state: what every live Claude Code session is doing, and its agents.

The handoff document specifies this state machine in terms of things a daemon
cannot see from outside -- "CLI process waiting on stdin", "tool execution with
0% CPU". Both mean finding the right process among several, sampling it, and
inferring intent from a number that is legitimately zero whenever a tool waits
on the network. That is a guess dressed as a measurement.

Claude Code announces every one of these transitions through its hook
interface, so this reads the announcements instead:

    UserPromptSubmit  -> a turn started              -> running
    PreToolUse        -> a tool started              -> running
    PostToolUse       -> a tool finished             -> running
    Notification      -> Claude Code wants the user  -> waiting
    Stop              -> the turn completed          -> idle (your turn)
    StopFailure       -> the turn died on an API error -> failed
    SessionStart/End  -> the session's lifetime      -> no claim
    SubagentStart/Stop-> one agent's lifetime

`idle` means "finished, and waiting on you" -- it is a claim on the person's
attention, and on the panel it is amber, not green. That is why a session
that merely OPENED is not idle: a terminal somebody opened and has not typed
into yet needs nothing from anybody, and a session that ENDED needs nothing
ever again. Both say nothing rather than call for attention.

`waiting` is authoritative rather than inferred: Notification is the event
Claude Code fires when it needs a human, which is what "is the process blocked
on stdin" was trying to establish indirectly. `failed` earns its own state
because on a usage gauge it is the headline -- StopFailure carries
`error: "rate_limit"` among its causes, and being rate limited is the single
thing this product exists to warn about.

`stuck` is a state the protocol still names and no provider produces any
more. It was inferred from silence -- a turn announced and then nothing for
N seconds -- and every N cried wolf on the desk: 60 s on a test suite, 180 s
on a nine-minute polling loop, 600 s on a seventeen-minute think with the
API connection open the whole time (all 2026-08-29). The hooks cannot tell
a long turn from a wedged one, so the daemon no longer guesses. A turn is
`running` for as long as it takes; a session that ends without saying so
drops out after ABANDONED_AFTER_S. Red is reserved for `failed`, which is
an event, not an inference.

What silence cannot say, a pid can. A session whose terminal was closed, or
that was killed, fires no SessionEnd and its slot simply stops changing --
indistinguishable from a seventeen-minute think, which is why the hour-long
ABANDONED_AFTER_S was the only thing that ever collected it. The hook now
records the process it ran from, so the daemon can ask the kernel a question
with a true answer instead of inferring one from a clock: a process that no
longer exists is not thinking. See LIVENESS below, and the latch that guards
it, because what $PPID means in Claude Code's hook runner has not been
measured yet.

ON DISK
-------
    ~/.blink/state/<session_id>.state   one JSON slot, newest event wins
    ~/.blink/state/<session_id>.waiting  present while a human is being asked
    ~/.blink/state/<session_id>.ended    left by SessionEnd where the slot was
    ~/.blink/state/<session_id>/<agent_id>   one empty file per live agent

One file per session because a single global slot silently misreports the
moment a second terminal exists. One file per AGENT because that makes the
count exact without a lock: two agents starting at once cannot race on a
shared counter, and a stop removes precisely the agent that stopped rather
than decrementing and hoping.

And a separate file for WAITING, for the same reason one step further in.
"Newest event wins" is only true if the newest event is the last one WRITTEN,
and on Codex it is not: the first interactive session anyone captured fired
PreToolUse and PermissionRequest in the same second, twice
(docs/research/codex-hook-contract.md). Those are two shim processes racing
for one slot, and the shim ends with mv -f, so whichever finishes second wins
regardless of which event happened first. Lose that race and the slot says
`running` over a session that is blocked on a person -- for exactly as long as
the person takes to answer, which is the whole interval this feature exists to
show. The next event does correct it, but the next event IS the answer, so the
correction lands the moment it stops mattering.

A marker cannot lose that race because it does not share a file with anything:
PermissionRequest creates it, the events that mean the person answered
(PostToolUse, Interrupt, Stop, UserPromptSubmit) remove it, and create and
unlink are each atomic on their own path. PreToolUse deliberately does NOT
remove it -- that is the one event known to arrive in the same second, so
letting it clear the marker would reintroduce the race it exists to escape.
"""
import json
import os
import sys
import time

from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "cli"

# Expanded when a provider is built, not here: a module-level expanduser is
# evaluated at import, before a test can move HOME, and this provider DELETES
# files under it. See tests/conftest.py and the note that names this constant.
STATE_DIR = "~/.blink/state"

# Past this, assume the session is gone rather than that it has been idle
# since. Also the sweep threshold: a session that ended without SessionEnd
# firing (a killed terminal, a crash) leaves files behind, and nothing else
# will ever collect them.
ABANDONED_AFTER_S = 3600.0

# The sibling of <sid>.state that says a person is being asked something. Not
# a suffix on the slot itself: the point of it is to be a different file from
# the one two hook processes race for. See the ON DISK note above.
WAITING_MARKER_SUFFIX = ".waiting"

# The sibling that says a session is OVER, left by SessionEnd where the slot
# used to be. Nothing in this module's own census needs it -- a slot that is
# gone is already uncounted here -- but Codex's rollout reader cannot see an
# ending at all, and a file that stopped growing looks exactly like a long
# quiet turn. Its union therefore has to tell "this session never had a hook"
# (count it, the hooks fire only from the next turn on) from "the hook watched
# this session end" (do not), and absence cannot say which. This file can.
#
# Read from here rather than from codex_state so that the shim writes one
# shape into both directories and one sweep collects it out of both.
ENDED_MARKER_SUFFIX = ".ended"

# An AGENT file, though, is created once and never touched again -- its mtime
# is the agent's start, not its last sign of life -- so the session threshold
# swept a long-running subagent out of the count while it was still working.
# Four hours is past what any single agent run takes and short enough that a
# file whose SubagentStop never fired does not inflate the count for a day.
AGENT_ABANDONED_AFTER_S = 4 * 3600.0

# A slot timestamp outside this is not a reading: 2020-01-01 to 2100-01-01,
# as the other file-backed sources bound theirs. What it excludes in practice
# is NaN (every comparison false, so the abandoned test
# ever fires) and a millisecond epoch from some future shim.
T_EPOCH_MIN = 1_577_836_800.0
T_EPOCH_MAX = 4_102_444_800.0

# LIVENESS
# --------
# The hook writes the pid of the process it ran from. A slot whose pid names
# no living process belongs to a session that is over, whatever its clock
# says, and it is dropped at once rather than held for ABANDONED_AFTER_S.
#
# A pid only means something in the namespace that issued it, and the hook
# writes into the same ~/.blink/state the daemon reads. That was read as "both
# under one HOME on one host, so there is no remote case to defend against",
# and one shared HOME is not one pid namespace: Claude Code running in a
# devcontainer with ~/.blink bind-mounted writes a CONTAINER pid, which the
# daemon on the host resolves against the host's table. Nothing was copied
# between machines -- the file never moved -- and the number in it still does
# not name the process that wrote it.
#
# That is the one direction this check is not allowed to fail in: an absent
# host pid reads as ProcessLookupError, so a session that is alive and working
# is dropped from the panel at once. Every other way of being wrong keeps a
# session too long.
#
# It is the suspension below that defends against it, and it is why the
# suspension is deliberately eager and no longer permanent: the first slot the
# daemon sees written seconds ago by a pid the host does not have suspends the
# feature for as long as no pid proves otherwise, which on a container desk is
# forever. What that leaves open is a MIXED desk -- host sessions and
# container sessions at once -- where a live host pid keeps re-arming the check
# that the container sessions then fail. Closing that needs a marker in the
# slot itself (the hook writing a boot id or nodename, and the daemon ignoring
# a pid from another one), which is a change to tools/blink-hook.sh and to
# every shim already installed; noted, not done.
#
# PID REUSE is the one wrong answer available: the kernel eventually hands a
# dead session's number to some unrelated new process, and the slot then looks
# alive. That is a miss, not a false alarm -- the session simply keeps being
# reported until ABANDONED_AFTER_S collects it, which is exactly today's
# behaviour and the reason no process start time is recorded alongside. Every
# way this check can be wrong has to point in that direction; a false DEATH
# would blank a working session off the panel, which is worse than the bug
# being fixed here.
_PID_MAX = 2 ** 31 - 1

# POSIX ONLY, and this is not a portability nicety. On POSIX, os.kill(pid, 0)
# asks a question. On Windows, CPython implements os.kill as
# TerminateProcess(handle, sig) for every signal but the two console events --
# so "is Claude Code still alive?" would KILL Claude Code, with exit code 0.
# There is a right answer on Windows (OpenProcess + GetExitCodeProcess through
# ctypes) and it is not written yet, so Windows keeps today's rules: the pid is
# read, bounds-checked and ignored. A missing feature there beats a daemon that
# shoots the sessions it is reporting on.
_PID_LIVENESS_AVAILABLE = os.name == "posix"

# A slot no older than this was written by a process that was alive when it
# wrote. That is the whole self-test below.
FRESH_SLOT_S = 10.0

# THE SUSPENSION, and why it is not optional -- and why it is no longer a
# one-way latch.
#
# Nobody has measured whether $PPID in the hook is Claude Code's own process
# or a short-lived shell Claude Code spawned to run the hook. If it is the
# latter, the pid is dead within milliseconds of being written and the naive
# reading of this feature would drop EVERY live session -- a blank panel, far
# worse than an hour-stale one. A bind-mounted ~/.blink (see LIVENESS above)
# produces the same symptom for a different reason and needs the same answer.
#
# So the feature tests its own premise on real data, in both directions:
#
#   - A slot written seconds ago whose pid is already gone cannot have been
#     written by that pid's process. Stop believing pids, fall back to
#     ABANDONED_AFTER_S exactly as before, and say so once on stderr.
#   - A slot whose pid DOES name a living process is the same kind of
#     evidence pointing the other way, and it is the stronger of the two:
#     it is a pid out of this very directory resolving in this very process
#     table. It restores belief.
#
# It used to latch off permanently, and that was wrong on an ordinary event.
# A terminal closed within FRESH_SLOT_S of its last hook event fires no
# SessionEnd (SIGHUP does not), leaving a fresh slot with a dead pid -- which
# is indistinguishable from the wrapper case at the instant it is seen. On a
# launchd daemon that runs for weeks, one closed terminal disabled the feature
# until the next reboot, and printed a diagnosis that has since been MEASURED
# FALSE on this machine ($PPID is `claude` on every live slot, checked with
# ps -o comm= on three of them, 2026-09-02). Making it recoverable is what
# makes the eagerness affordable: the check may now suspend itself on weak
# evidence, because the next living pid takes it back.
#
# What does not change: the fail-safe direction. Suspended means "behave
# exactly as the daemon did before this feature existed", never "guess".
#
# Process-wide rather than per-provider because what a slot's pid means is a
# fact about this machine, not about a directory: `blink status` builds its
# own short-lived provider and should inherit the conclusion rather than
# re-derive it. The message is printed at most once per process for the same
# reason it always was -- a two-second poll would otherwise turn a real
# warning into 43,200 log lines a day.
#
# TO RETIRE THIS: measure it on Linux and Windows too. Run a real session,
# read a fresh slot's pid, and check it against the `claude` process
# (ps -p <pid> -o comm=). macOS is done. If the pid is the CLI itself
# everywhere, this and FRESH_SLOT_S can both be deleted and the pid trusted
# outright -- though the container case above would still want the marker it
# asks for.
_pid_trusted = True
# Whether stderr has already carried the explanation. Separate from the trust
# flag itself now that trust can come back: without it, a desk that suspends
# and re-arms would print on every cycle.
_pid_suspension_announced = False


def pid_liveness_trusted():
    """Whether the pid in a state slot is still believed to mean anything."""
    return _pid_trusted


def reset_pid_liveness():
    """Restore trust and the right to explain a suspension again.

    For tests only. Trust is process-wide and outlives any one provider, so a
    test that suspended it would otherwise silently disarm the liveness checks
    in every test that ran after it."""
    global _pid_trusted, _pid_suspension_announced
    _pid_trusted = True
    _pid_suspension_announced = False


def _slot_pid(payload):
    """The pid recorded in a slot, or None when there is nothing usable.

    Strict on purpose: anything that is not a plain in-range integer falls
    back to today's rules rather than being coerced. A shim old enough to omit
    the key entirely is the ordinary case, not an error -- customers run stale
    shims for months -- and a shim NEWER than this code, writing a shape this
    version does not understand, deserves the same treatment.
    """
    pid = payload.get("pid")
    # bool is an int in Python, and `True` is not a process.
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    # 1 and up only. Zero and negatives are not processes to os.kill: they
    # name process groups and, for -1, every process the user can signal.
    # Signal 0 delivers nothing, so nothing would come of it, but a number
    # like that in a slot is a malformed reading and gets treated as one.
    if not (1 <= pid <= _PID_MAX):
        return None
    return pid


def _process_is_gone(pid):
    """True only when the kernel says that pid names no process.

    os.kill(pid, 0) delivers no signal; it asks the question and costs a
    syscall, not a fork. That matters because this now runs for every slot on
    a two-second poll.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The process exists and belongs to somebody else. ALIVE. Saying
        # otherwise here would drop a session for running under another
        # account -- the false-death direction this must never take.
        return False
    except OSError:
        # An errno this code did not anticipate. Silence beats a guess, and
        # the guess would be the dangerous one.
        return False
    return False


def _slot_age(payload, now_epoch):
    """How long ago this slot was written, or None when it does not say.

    The same validity gate _read_state applies, so the pass that settles trust
    considers exactly the slots the pass that judges them will look at.
    """
    event = payload.get("event")
    if not isinstance(event, str) or not event:
        return None
    try:
        t = float(payload["t"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (T_EPOCH_MIN <= t <= T_EPOCH_MAX):
        return None
    return now_epoch - t


def settle_pid_trust(slots, now_epoch):
    """Decide ONCE PER POLL whether a pid still means anything.

    This used to happen slot by slot, inside _pid_says_ended, which both read
    the trust flag and wrote it: a slot whose pid resolves restored trust, and
    a fresh slot whose pid is gone suspended it. Applied one file at a time in
    os.listdir order, the answer came down to which file the kernel happened to
    hand back last -- and it came down that way twice over, because a slot read
    while trust still held was dropped and the same slot read a moment later
    was kept.

    It really does differ by platform. On the Ubuntu desk listdir returned
    open.state then closed.state and pid_liveness_trusted() came back False
    after a poll in which a live pid was present; macOS returns the other order
    and the same code passed. That is the whole reason three tests in
    tests/pc/test_state_machine.py failed on Linux and nowhere else. On a real
    machine it is not only a test problem: a directory that lists the other way
    round silently disables pid liveness, and sessions that end without
    SessionEnd then linger the full ABANDONED_AFTER_S instead of dropping.

    So the evidence is weighed across every slot first, and _pid_says_ended
    only reads the result. Two kinds of evidence, and they are not equal:

      - a pid out of this directory that resolves in this process table is the
        premise holding, demonstrated. Nothing outranks it, and it already did
        not (test_a_living_pid_takes_the_suspension_back) -- it merely had to
        be read second to win;
      - a slot written seconds ago whose pid is already gone is the premise in
        doubt. It suspends, but only in a poll that saw no living pid at all.
    """
    global _pid_trusted, _pid_suspension_announced
    if not _PID_LIVENESS_AVAILABLE:
        return
    doubt = None
    for path, payload in slots:
        pid = _slot_pid(payload)
        if pid is None:
            continue
        if not _process_is_gone(pid):
            _pid_trusted = True
            return
        if doubt is not None:
            continue
        age = _slot_age(payload, now_epoch)
        # A stamp further into the future than any ordinary skew is not a
        # measurement, so it is evidence of nothing -- see the note in
        # _pid_says_ended. Ordinary skew still counts as fresh: a slot stamped
        # three seconds ahead was written three seconds ago by a fast clock.
        if age is not None and -FRESH_SLOT_S <= age <= FRESH_SLOT_S:
            doubt = (pid, age, path)

    if doubt is None or not _pid_trusted:
        return
    pid, age, path = doubt
    _pid_trusted = False
    if not _pid_suspension_announced:
        _pid_suspension_announced = True
        print(f"[claude] {path or 'a state slot'} was written"
              f" {age:.0f}s ago but its pid {pid} is already gone:"
              " that pid may not name the session's own process on this"
              " machine, so pid liveness is DISABLED until one proves"
              " live -- until then, sessions that end without SessionEnd"
              f" drop out after {ABANDONED_AFTER_S:.0f}s as before",
              file=sys.stderr)


def _pid_says_ended(pid, age_s, slot_path=""):
    """Whether the pid proves this session is over.

    Reads the trust flag and never writes it: whether a pid means anything is
    settled once per poll, across every slot, by settle_pid_trust(). Deciding
    it here made the answer depend on which file os.listdir handed back last.

    Returns False for every uncertain case, so the caller keeps whatever
    today's rules already decided.
    """
    if pid is None or not _PID_LIVENESS_AVAILABLE:
        return False
    if not _pid_trusted:
        return False
    if not _process_is_gone(pid):
        return False
    if age_s < -FRESH_SLOT_S:
        # A stamp further into the future than any ordinary skew. derive_state
        # meets the same number and clamps it, because "how long since the
        # last event" has a sane answer for a session that is on screen; here
        # it does not. So nothing is taken from it: no drop, because the
        # session may well be alive. A slot with a broken clock takes today's
        # rules, which is where every uncertain case in this function goes.
        #
        # It used to sail straight into the test below -- a negative number is
        # less than ten -- and one NTP step could disable pid liveness for the
        # life of a daemon that runs for weeks.
        return False
    if age_s <= FRESH_SLOT_S:
        # Written seconds ago BY A PROCESS THAT DOES NOT EXIST -- and ordinary
        # skew lands here on purpose, because a slot stamped three seconds
        # ahead was still written three seconds ago, by a clock that is ahead.
        # Refusing to call that fresh would send it to the drop below, which
        # is the one thing this feature must never do to a live session.
        #
        # Three things look like this and only one of them is ordinary: the
        # hook's pid may be a wrapper the session outlives, the pid may belong
        # to another namespace (a bind-mounted ~/.blink), or the session may
        # simply have been closed inside FRESH_SLOT_S of its last event, which
        # fires no SessionEnd. Keep the session either way. Whether this also
        # suspends the feature is settle_pid_trust's call, not this one's --
        # it is the only caller that can see the whole directory at once.
        return False
    return True


_RUNNING_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                   "SubagentStop", "PreCompact", "PostCompact")
# Only Stop is idle: the turn completed and the answer is waiting to be read.
#
# SessionStart used to be idle too (and before that, running -- which turned
# red "stuck" three minutes after every `claude`). Now that idle means "your
# turn" and paints amber, an opened-but-untouched terminal must not claim it:
# nothing has been asked, so nothing is waiting. SessionEnd likewise -- a
# session that is over is not waiting on anyone. Both are filed as no claim,
# and the file is left for the abandon sweep. A turn announces itself with
# UserPromptSubmit; until then the session is simply open.
# Interrupt is Codex's event for "the person pressed Esc", and it belongs with
# Stop rather than with PostToolUse, on evidence rather than on taste. In the
# one real interactive session anyone has captured (docs/research/
# codex-hook-contract.md), a refused approval produced
# UserPromptSubmit -> PreToolUse -> PermissionRequest -> Interrupt and then
# NOTHING: no Stop ever followed. So Interrupt is terminal for its turn, and
# filing it under running would leave the panel saying "Working" for a session
# that has actually stopped -- for a full hour, until ABANDONED_AFTER_S finally
# times it out.
#
# Which is also what the owner's three-state ruling asks for: an aborted turn is
# "finished, and it is your turn again", and that is what idle means here.
#
# What matters most is that it lands somewhere other than unknown: unknown drops
# a session out of the census entirely, so today pressing Esc on a Codex
# approval makes its pip vanish rather than merely mislabelling it.
_IDLE_EVENTS = ("Stop", "Interrupt")
_OPEN_EVENTS = ("SessionStart", "SessionEnd")
_WAITING_EVENTS = ("Notification", "PermissionRequest")
_FAILED_EVENTS = ("StopFailure",)

# The severity order lives in base (SEVERITY): it is shared with Codex and
# with the wire layer, which collapses both providers into one light.
_SEVERITY = base.SEVERITY


def derive_state(event: str, age_s: float) -> str:
    """The execution state implied by the last event and how long ago it was."""
    if age_s < 0:
        # A clock that went backwards -- a laptop waking, an NTP step. Treat
        # it as fresh rather than as a negative age that would sail under
        # every threshold below and report a confident state from a broken
        # measurement.
        age_s = 0.0

    if age_s > ABANDONED_AFTER_S:
        return base.STATE_UNKNOWN

    if event in _IDLE_EVENTS:
        # No age test. A completed turn stays completed; silence after it is
        # the expected condition, not a fault.
        return base.STATE_IDLE

    if event in _OPEN_EVENTS:
        return base.STATE_UNKNOWN

    if event in _FAILED_EVENTS:
        # Also no stuck test. A turn that died on an API error is not wedged,
        # it is finished and unsuccessful, and it stays that way until
        # something else happens.
        return base.STATE_FAILED

    if event in _WAITING_EVENTS:
        # A prompt waiting for a human is not wedged, and it stays waiting
        # for as long as the human takes.
        return base.STATE_WAITING

    if event in _RUNNING_EVENTS:
        # However long ago. See the module docstring on `stuck`.
        return base.STATE_RUNNING

    # An event this version does not know. Newer Claude Code, most likely.
    # Silence beats guessing.
    return base.STATE_UNKNOWN


worst_of = base.worst_of


class ClaudeStateProvider(base.ProviderParser):
    """Execution state only -- this source never carries a usage percentage.

    That matters to the normalizer: a frame with no percentage can never
    become the primary source, so this cannot make a panel look fresher than
    its numbers actually are. It contributes execution fields and nothing else.
    """

    def __init__(self, path=None, now=time.time, sweep=True):
        self._dir = (path if path is not None
                     else os.path.expanduser(STATE_DIR))
        self._now = now
        self._sweep = sweep

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._dir

    # --- one session ------------------------------------------------------

    @staticmethod
    def _load_slot(path):
        """One slot's payload, or None when there is nothing usable in it.

        Split out of _read_state so session_states can read the directory once
        and then walk it twice -- see settle_pid_trust.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _read_state(self, path, now_epoch, payload=None):
        """(state, age, name) for one session's slot, or (None, None, "").

        Whether a pid means anything has already been settled for this poll,
        across every slot, by settle_pid_trust -- see the note there.
        """
        if payload is None:
            payload = self._load_slot(path)
        if payload is None:
            return None, None, ""
        event = payload.get("event")
        if not isinstance(event, str) or not event:
            return None, None, ""
        try:
            t = float(payload["t"])
        except (KeyError, TypeError, ValueError):
            return None, None, ""
        if not (T_EPOCH_MIN <= t <= T_EPOCH_MAX):
            return None, None, ""
        # A name is optional and its absence is ordinary: a state file written
        # by a shim older than this feature has no key, and the shim omits it
        # whenever the payload's cwd did not survive sanitising.
        name = payload.get("name")
        if not isinstance(name, str):
            name = ""
        age = now_epoch - t
        state = derive_state(event, age)
        # The pid overrules the clock in one direction only: it can end a
        # session early, never keep one alive past ABANDONED_AFTER_S. A pid
        # that has been reused is the case that would otherwise argue for
        # "alive", and it is not allowed a vote.
        if _pid_says_ended(_slot_pid(payload), age, path):
            return base.STATE_UNKNOWN, age, name
        return state, age, name

    def _count_agents(self, session_dir, now_epoch):
        """Live agents for one session.

        An agent file older than the abandon threshold is swept: its
        SubagentStop never fired, because the session died mid-flight. Without
        this the count only ever grows, and a panel that says "3 agents"
        forever is worse than one that says nothing.
        """
        try:
            names = os.listdir(session_dir)
        except OSError:
            return 0
        live = 0
        for name in names:
            p = os.path.join(session_dir, name)
            try:
                age = now_epoch - os.path.getmtime(p)
            except OSError:
                continue
            if age > AGENT_ABANDONED_AFTER_S:
                if self._sweep:
                    _unlink(p)
                continue
            live += 1
        return live

    def _waiting_marked(self, path, now_epoch):
        """Is the waiting marker present, and recent enough to mean it?

        Existence is nearly the whole answer -- the shim creates the file when
        a person is asked something and removes it when they answer -- but a
        marker whose removal never ran (a crash between the two, a directory
        that turned unwritable) would otherwise pin a session on "Waiting for
        you" for as long as the session lasts. Bounding it by the same hour
        that bounds every other file here means the marker can never outlive
        the census entry it modifies.
        """
        try:
            age = now_epoch - os.path.getmtime(path)
        except OSError:
            return False
        # A clock that stepped backwards, exactly as derive_state treats it:
        # a negative age is a broken measurement, not a fresh file, and the
        # marker is present either way.
        return age <= ABANDONED_AFTER_S

    def ended_sessions(self, now_epoch):
        """The session ids the hook has buried, and swept once they age out.

        Bounded by the same hour as everything else here, and that bound is
        what keeps the tombstone honest in both directions. Too short and a
        closed Codex session comes back on the panel before its rollout ages
        out; unbounded and the directory grows one empty file per session
        forever, since nothing else would ever look at these names.

        Sweeping is the caller's decision for the same reason the slot sweep
        is: `blink status` must be able to look at this directory without
        deleting what somebody ran it to see.
        """
        try:
            entries = os.listdir(self._dir)
        except OSError:
            return set()
        ended = set()
        for name in entries:
            if not name.endswith(ENDED_MARKER_SUFFIX):
                continue
            path = os.path.join(self._dir, name)
            try:
                age = now_epoch - os.path.getmtime(path)
            except OSError:
                continue
            if age > ABANDONED_AFTER_S:
                if self._sweep:
                    _unlink(path)
                continue
            # A negative age is a clock that stepped backwards, not a file
            # from the future, and derive_state treats one the same way: the
            # tombstone is present either way and presence is the claim.
            ended.add(name[: -len(ENDED_MARKER_SUFFIX)])
        return ended

    # --- the whole directory ----------------------------------------------

    def session_states(self, now_epoch):
        """({session_id: (state, name)}, live agents) for this directory.

        The per-session view, split out of scan() because the Codex union
        needs the ids: a session that both the hook slots and the rollout
        reader can see has to be counted once, and the session id is the only
        thing those two sources have in common. Counts alone cannot be
        de-duplicated.

        Sessions in STATE_UNKNOWN are absent from the mapping and are swept
        exactly as scan() always swept them.
        """
        try:
            entries = os.listdir(self._dir)
        except OSError:
            # No hooks installed, or nothing has happened yet. Both normal.
            return {}, 0

        # Called for what it COLLECTS rather than for what it returns. The
        # shim leaves a tombstone in this directory as well as in the Codex
        # one, and nothing else here would ever look at the name again: the
        # slot it replaced is gone, so the session is already uncounted. Only
        # the Codex union needs the ids, and it asks for them itself.
        #
        # Deliberately NOT used to drop a slot that still exists. A tombstone
        # beside a live slot means the session was resumed under its own id,
        # and the slot is the newer witness; the shim removes the tombstone on
        # any event for exactly that case, so this pairing should not outlive
        # one hook firing anyway.
        self.ended_sessions(now_epoch)

        # Read every slot first, settle the pid question across all of them,
        # and only then judge each one. Deciding it slot by slot made the
        # answer depend on the order os.listdir returns, which differs between
        # macOS and Linux -- see settle_pid_trust.
        slots = []
        for name in entries:
            if not name.endswith(".state"):
                continue
            path = os.path.join(self._dir, name)
            slots.append((name, path, self._load_slot(path)))
        settle_pid_trust([(p, pl) for _, p, pl in slots if pl is not None],
                         now_epoch)

        states = {}
        agents = 0
        for name, state_path, payload in slots:
            sid = name[: -len(".state")]
            marker_path = os.path.join(self._dir, sid + WAITING_MARKER_SUFFIX)
            state, age, sess_name = self._read_state(state_path, now_epoch,
                                                     payload)

            if state is None or state == base.STATE_UNKNOWN:
                # Unreadable, or so old the session is certainly gone. Collect
                # the whole session rather than leaving a directory that will
                # never be looked at again.
                #
                # A session dropped for a DEAD PID is deliberately not swept
                # here: it stops being reported at once -- which is the whole
                # user-visible point -- and its files are collected by the
                # same hour-long rule as before. Deleting them the instant the
                # pid check fires would destroy the only evidence of why a
                # session disappeared (its last event, its clock, its pid) at
                # exactly the moment somebody would want to look, and this
                # check is new enough that somebody will. Nothing is bought by
                # sweeping sooner: an unreported slot costs a few hundred
                # bytes and one stat per poll, and the hour still ends.
                if age is not None and age > ABANDONED_AFTER_S and self._sweep:
                    _unlink(state_path)
                    _unlink(marker_path)
                    _rmtree(os.path.join(self._dir, sid))
                continue

            # The marker overrules the slot, and only ever in this direction.
            # The slot cannot be trusted to say `waiting`, because the write
            # that would have said so is the one that loses the race described
            # in ON DISK; the marker is the only witness that cannot be
            # overwritten by a same-second PreToolUse. Nothing goes the other
            # way: no slot event clears a marker, because deciding a person
            # has answered is the shim's job, where the event order is known.
            if self._waiting_marked(marker_path, now_epoch):
                state = base.STATE_WAITING

            states[sid] = (state, sess_name)
            agents += self._count_agents(os.path.join(self._dir, sid),
                                         now_epoch)
        return states, agents

    def scan(self, now_epoch):
        """{state: n_sessions}, {state: [names]}, total live agents.

        Counts derived from session_states() rather than gathered beside it,
        so the two views cannot drift apart. Sweeps what it finds dead.
        """
        states, agents = self.session_states(now_epoch)
        counts = {}
        names = {}
        for state, sess_name in states.values():
            counts[state] = counts.get(state, 0) + 1
            if sess_name:
                names.setdefault(state, []).append(sess_name)
        return counts, names, agents

    def poll(self, now_epoch):
        counts, names, agents = self.scan(now_epoch)
        if not counts:
            return []
        state = worst_of(counts)
        if state == base.STATE_UNKNOWN:
            return []
        # Named only when the winning state is held by exactly ONE session.
        #
        # Naming one of several is the mistake the context row was cut for:
        # "88% of 4" qualified one number into honesty and still did not say
        # WHICH. A count says something true about all of them; a name picked
        # from three says something true about one and implies it about the
        # rest.
        label = ""
        held = names.get(state, [])
        if counts.get(state, 0) == 1 and len(held) == 1:
            label = held[0]
        frame = base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=now_epoch,
            state=state,
            label=label,
            n_run=counts.get(base.STATE_RUNNING, 0),
            n_wait=counts.get(base.STATE_WAITING, 0),
            n_stuck=(counts.get(base.STATE_STUCK, 0)
                     + counts.get(base.STATE_FAILED, 0)),
            n_idle=counts.get(base.STATE_IDLE, 0),
            n_agents=agents,
        )
        return [frame]


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rmtree(path):
    try:
        for name in os.listdir(path):
            _unlink(os.path.join(path, name))
        os.rmdir(path)
    except OSError:
        pass
