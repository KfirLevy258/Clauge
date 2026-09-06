"""The pluggable ingestion bus: every provider, polled and normalized.

This is the seam the daemon holds. claude_usage_bridge asks it for a usage
message and knows nothing about which providers exist, how many sources each
one has, or what any of their files look like -- which is the property that
lets a second provider be added without touching the daemon, the protocol or
the firmware.

A provider that misbehaves is contained here rather than being allowed to
reach the reconnect loop. The daemon's job is to keep a board fed; a parser
for an app we do not control is exactly the kind of code that should never be
able to stop it.
"""
import sys
import time

from pc import cowork_audit, desktop_idb, normalizer, protocol, weekly_anchor
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from pc.providers.claude_state import ClaudeStateProvider
from pc.providers.codex_cli import CodexCliProvider
from pc.providers.weekly_anchor import DesktopHistoryProvider, WeeklyAnchorProvider


# How long a provider that raised sits out before it is tried again, and how
# far that grows while it keeps failing.
#
# Not forever, which is what it used to be. The daemon's poll dropped from 60 s
# to 2 s (claude_usage_bridge.FAST_POLL_INTERVAL_S), so a provider now gets 30
# chances a minute to catch a file mid-rewrite, an EBUSY, a half-flushed JSON
# line -- and under the old rule the first one it lost retired it until the user
# restarted the daemon. Every one of those failures is transient by
# construction: the file that could not be parsed this second is written by
# another application that is still running and will finish its write.
#
# One minute first, because a transient should cost about a minute and not a
# workday. Doubling, because a provider whose upstream really has changed shape
# should not be re-parsed thirty times an hour for the life of the process.
# Fifteen minutes at the ceiling, which is short enough that a user who fixes
# the cause -- signs in, upgrades the other app -- sees the panel come back
# without being told to restart anything.
FIRST_RETRY_AFTER_S = 60.0
MAX_RETRY_AFTER_S = 900.0


def default_providers():
    """Every provider shipped today, in preference order.

    Order is not the merge rule -- pc/normalizer merges by recency per field,
    not by position -- but it does decide which source is polled first and so
    which one a tie resolves toward.
    """
    return [ClaudeCliProvider(), ClaudeDesktopProvider(),
            ClaudeDesktopLocalStorageProvider(),
            ClaudeStateProvider(), CodexCliProvider(),
            # LAST: it only ever offers a remembered projection, and every
            # source above it is polled first so any of them carrying a real
            # weekly reset is what observe() below learns from this cycle.
            #
            # history_provider gives it something to be refuted BY: Claude
            # Desktop's own usage cache, the one file on this machine with a
            # weekly-percentage series to check the projection against. With
            # no Claude Desktop installed the file is simply absent and
            # DesktopHistoryProvider.samples() returns None -- refutation
            # cannot fire, and this is unchanged from having no
            # history_provider at all.
            WeeklyAnchorProvider(history_provider=DesktopHistoryProvider())]


class IngestionBus:
    def __init__(self, providers=None, preferred_provider="claude",
                 now=time.time):
        self._providers = (default_providers() if providers is None
                           else list(providers))
        self._preferred = preferred_provider
        self._now = now
        # {id(provider): {"since", "retry_at", "wait"}} for providers sitting
        # out after a failure. Keyed by IDENTITY, not by class: two instances
        # of the same provider class are two sources -- two Codex homes, the
        # same parser pointed at a second directory -- and under the old class
        # key one of them raising silenced its healthy twin, which reads on the
        # panel as the second source simply having no data. Identity is safe as
        # a key here because self._providers holds every provider for the life
        # of the bus, so no id can be recycled underneath us.
        self._broken = {}
        # (label, n) for the frame the last poll() chose. Recorded there
        # rather than recomputed here, so session_pair() answers for the same
        # poll the board's usage message came from -- polling the providers a
        # second time could pick a different winner and name a project that
        # does not belong to the state on the panel.
        self._session_pair = ("", 0)
        # Guards the one-shot anchor seeding below: see
        # _seed_anchor_once. False until the first poll_frames() call,
        # regardless of whether that call finds anything to seed.
        self._seeded = False

    def provider_ids(self):
        """Which providers this bus is polling, by id.

        A public accessor so a caller that needs to know -- the fleet tests
        checking that BLINK_SCENARIO really did replace the real sources --
        does not have to reach into _providers and freeze that name in place.
        """
        return [p.get_provider_id() for p in self._providers]

    def add_provider(self, provider):
        """Onboard a provider at runtime. Nothing else has to change."""
        self._providers.append(provider)

    def set_preferred(self, provider):
        """Which provider gets the outer ring and the big number.

        Set from the BOARD, not from here: the user picks it on the settings
        screen, the board persists it and announces it, and this follows. A
        preference that lived only in the daemon would reset every time the
        daemon restarted, and the person choosing it is looking at the panel,
        not at a config file.

        An unknown name is ignored rather than applied. select_pair() falls
        back to the freshest provider when its preference matches nothing, so
        a typo would silently hand the outer ring to whichever source wrote
        last -- which looks like a bug in the merge, not a bad setting.
        """
        if not provider:
            return False
        known = {p.get_provider_id() for p in self._providers}
        if provider not in known:
            print(f"[ingest] board asked for provider {provider!r}, which is"
                  f" not reporting; keeping {self._preferred!r}",
                  file=sys.stderr)
            return False
        self._preferred = provider
        return True

    def _seed_anchor_once(self):
        """One-shot fallback: seed the weekly anchor from a legacy Cowork
        audit file, or failing that from Claude Desktop's IndexedDB, when
        nothing has ever populated it.

        Runs at most once per process, guarded by self._seeded -- set before
        any work is attempted, so a raise on the first try still counts as
        the one try this process gets. Without that, a machine with no
        audit file (the common case: see pc/cowork_audit's own docstring)
        would walk its whole session tree on every single poll, forever.

        Tried only while the anchor file itself is empty, and wrapped whole
        in try/except: this walks a directory tree and parses files owned
        by another application, and none of that may be allowed to take a
        poll down.

        The order is deliberate and is a cost order. The audit file is plain
        JSON in a directory holding no chat store; the IndexedDB store holds
        the customer's conversations, so it is tried last, only when the
        cheaper source has produced nothing, and only within the same single
        attempt this process gets.

        Called at the END of poll_frames, after every provider has been polled
        and after weekly_anchor.observe has seen their frames. The cheapest
        and most authoritative source of a weekly boundary is a live frame
        that already carries one -- ordinarily the Claude Code status line --
        and it costs this function nothing to let that land first: the guard
        above then finds an anchor and neither seeder runs at all. Running
        before the loop, as this used to, spent an os.walk of the session tree
        and a V8 decode of the conversation store on the first poll of every
        process to learn something the next few lines were about to be told.
        """
        if self._seeded:
            return
        self._seeded = True
        try:
            path = weekly_anchor.anchor_path()
            if weekly_anchor.load(path) is not None:
                return
            found = cowork_audit.seven_day_reset()
            if found is None:
                found = desktop_idb.seven_day_reset()
            if found is None:
                return
            resets_at, observed_at = found
            weekly_anchor.save(path, resets_at, observed_at)
        except Exception:
            pass

    def poll_frames(self):
        """Every frame every provider can produce right now.

        Each provider is polled inside its own try. A provider that raises is
        reported once and then sits out a cooldown: the alternative is a stack
        trace on the daemon's log thirty times a minute for as long as some
        other application's file stays malformed.

        Skipped means skipped. This used to add the provider to _broken and
        then go on polling it every cycle regardless -- only the log line was
        suppressed, so a second, different failure was invisible and the
        docstring above was describing a policy the loop did not have.

        But skipped no longer means retired. A cooldown, not a tombstone: see
        FIRST_RETRY_AFTER_S for why a two-second poll makes the difference
        between the two enormous.

        Reported ONCE all the same, and that is deliberate. The log line marks
        the transition from working to broken, not each failure -- a provider
        that keeps failing keeps quiet, and only says anything again when it
        recovers, or when it breaks again after having recovered. A daemon
        that logs the same traceback on a timer is its own defect, and it is
        the reason the retry could not simply be "try it every poll".
        """
        frames = []
        now = self._now()
        for p in self._providers:
            key = id(p)
            name = p.__class__.__name__
            sitting_out = self._broken.get(key)
            if (sitting_out is not None
                    and not self._cooldown_over(sitting_out, now)):
                continue
            try:
                frames.extend(p.poll(now) or [])
            except Exception as e:
                # Doubled from whatever the last wait was, so a provider whose
                # upstream genuinely changed shape backs off to the ceiling on
                # its own without anyone deciding it is hopeless.
                wait = (min(sitting_out["wait"] * 2, MAX_RETRY_AFTER_S)
                        if sitting_out is not None else FIRST_RETRY_AFTER_S)
                if sitting_out is None:
                    print(f"[ingest] {name} failed and will be skipped: {e}",
                          file=sys.stderr)
                self._broken[key] = {"since": now, "retry_at": now + wait,
                                     "wait": wait}
            else:
                if sitting_out is not None:
                    # Worth a line precisely because the failure only got one:
                    # without this, a provider that came back left no trace of
                    # having done so, and the log said only that it had died.
                    del self._broken[key]
                    print(f"[ingest] {name} is working again",
                          file=sys.stderr)
        try:
            # Bonus learning, never a poll cost: whatever just carried a real
            # weekly reset becomes tomorrow's WeeklyAnchorProvider projection.
            # Anything this raises -- a bad path, a permissions error, a race
            # on the file -- must never take today's poll down with it.
            #
            # Filtered to the claude provider ONLY. weekly_anchor.observe is
            # deliberately source-agnostic about WHICH store a reset came
            # from -- the status line, a legacy Cowork audit, the IndexedDB
            # seeder -- but it is never agnostic about WHOSE account it
            # describes. Codex has its own weekly_resets_at (codex_cli.py),
            # and handing that to observe() unfiltered would let a Codex
            # boundary become the anchor that WeeklyAnchorProvider then
            # republishes with provider="claude" -- a different account's
            # reset, presented as this one's.
            claude_frames = [f for f in frames if f.provider == "claude"]
            weekly_anchor.observe(
                claude_frames, weekly_anchor.anchor_path(), now)
        except Exception:
            pass

        # LAST, and that ordering is the whole point. observe() has just had
        # its chance at every live frame, so on any machine running Claude
        # Code the anchor file is already written and _seed_anchor_once's own
        # `load(path) is not None` guard turns this into a no-op that costs a
        # single small read. Called before the loop -- as it was -- it walked
        # the session tree and V8-decoded the customer's conversation store on
        # the first poll of every process, to learn a boundary the status line
        # was about to hand over for free.
        self._seed_anchor_once()
        return frames

    @staticmethod
    def _cooldown_over(sitting_out, now) -> bool:
        """Has a skipped provider's cooldown elapsed?

        The second arm is for a clock that went backwards. self._now is wall
        time -- the whole bus reasons in it, because every frame's age is a
        wall-clock reading from another application's file -- and a sleeping
        laptop, an NTP step or a timezone-clumsy VM can put `now` before the
        moment the provider was quarantined. Left to arithmetic alone that
        would hold a healthy provider out for however far the clock moved,
        which is exactly the permanent skip this cooldown exists to end.
        """
        return now >= sitting_out["retry_at"] or now < sitting_out["since"]

    def poll(self):
        """The usage message for the board, or None when nothing is known."""
        primary, secondary = normalizer.select_pair(
            self.poll_frames(), preferred=self._preferred)
        if primary is None:
            return None
        msg = protocol.frame_to_usage(primary, self._now(), secondary)
        # Against the state the message actually carries, which is not always
        # the primary frame's own -- see _pair_from.
        self._session_pair = _pair_from(
            primary, secondary, msg.get("state", base.STATE_UNKNOWN))
        return msg

    def session_pair(self):
        """(label, n) for the state the last poll() put on the panel.

        Deliberately NOT part of the usage message: that line was measured at
        506 of protocol.MAX_LINE_BYTES=512 fully loaded and proto.c drops an
        over-long line whole, so the project name travels as its own message
        (protocol.session) and this is where the daemon reads it.
        """
        return self._session_pair

    def fetch(self):
        """The callable to hand Bridge(fetch_usage=...).

        The ONE way to obtain a fetch, and that is the whole point of it
        existing. The daemon used to write `fetch = bus.poll` while the tests
        built theirs through make_fetch, and a bound method proxies attribute
        reads to the plain function underneath -- so the Bridge's
        getattr(fetch, "session_pair", None) came back None on every real
        desk and the board was never named, with the entire suite green.
        Two ways to build the same object is what allowed that, so there is
        now one.
        """
        return _BusFetch(self)


class _BusFetch:
    """A zero-arg callable that also knows the project name.

    An object rather than a function with an attribute bolted on: the Bridge
    is handed a zero-arg callable returning a finished usage dict and never
    sees the frame the name lives on. Widening that contract instead (a
    tuple, or the label smuggled inside the usage dict) would either break
    every injected fake or put the label one slip away from the byte-capped
    usage line -- which is the one thing this whole message type exists to
    avoid.
    """

    def __init__(self, bus):
        self._bus = bus

    def __call__(self):
        return self._bus.poll()

    def session_pair(self):
        return self._bus.session_pair()


# Which count describes which state. STATE_FAILED reads n_stuck because that
# is where the provider put it: claude_state.poll folds a failed session into
# the stuck count, so mapping it to nothing would leave two failed sessions
# reporting zero -- "Session failed" where the panel should say "Session
# failed - 2 sessions".
_COUNT_FOR_STATE = {
    base.STATE_RUNNING: "n_run",
    base.STATE_WAITING: "n_wait",
    base.STATE_STUCK: "n_stuck",
    base.STATE_FAILED: "n_stuck",
    base.STATE_IDLE: "n_idle",
}


def _pair_from(primary, secondary, wire_state):
    """(label, n) for the state the usage message actually carries.

    Against the WIRE state rather than the primary frame's own, because
    frame_to_usage sends worst_of(primary, secondary) and the summed counts:
    one light for the whole desk. A Claude session sitting idle-but-named
    beside a waiting Codex one would otherwise put "Waiting for you -
    MyProject" over a project that is not waiting -- a wrong statement, not
    a vague one, and a line nobody can trust is worse than no line.

    So the label survives only when the frame carrying it is in the state on
    the panel, and only when nothing else shares that state. Naming one of
    several is refused rather than guessed, which is the same rule
    claude_state.poll applies when it decides whether to set `label` at all.
    """
    frames = [f for f in (primary, secondary) if f is not None]
    field = _COUNT_FOR_STATE.get(wire_state)
    n = sum(getattr(f, field, 0) for f in frames) if field else 0
    named = [f for f in frames
             if f.state == wire_state and getattr(f, "label", "")]
    label = named[0].label if len(named) == 1 and n <= 1 else ""
    return (label, int(n))
