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

from pc import normalizer, protocol
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider
from pc.providers.claude_state import ClaudeStateProvider
from pc.providers.codex_cli import CodexCliProvider


def default_providers():
    """Every provider shipped today, in preference order.

    Order is not the merge rule -- pc/normalizer merges by recency per field,
    not by position -- but it does decide which source is polled first and so
    which one a tie resolves toward.
    """
    return [ClaudeCliProvider(), ClaudeDesktopProvider(),
            ClaudeStateProvider(), CodexCliProvider()]


class IngestionBus:
    def __init__(self, providers=None, preferred_provider="claude",
                 now=time.time):
        self._providers = (default_providers() if providers is None
                           else list(providers))
        self._preferred = preferred_provider
        self._now = now
        self._broken = set()

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

    def poll_frames(self):
        """Every frame every provider can produce right now.

        Each provider is polled inside its own try. A provider that raises is
        reported once and then skipped for the rest of the process: the
        alternative is a stack trace on the daemon's log every sixty seconds
        for as long as some other application's file stays malformed.

        Skipped means skipped. This used to add the provider to _broken and
        then go on polling it every cycle regardless -- only the log line was
        suppressed, so a second, different failure was invisible and the
        docstring above was describing a policy the loop did not have.
        """
        frames = []
        now = self._now()
        for p in self._providers:
            key = f"{p.__class__.__name__}"
            if key in self._broken:
                continue
            try:
                frames.extend(p.poll(now) or [])
            except Exception as e:
                print(f"[ingest] {key} failed and will be skipped: {e}",
                      file=sys.stderr)
                self._broken.add(key)
        return frames

    def poll(self):
        """The usage message for the board, or None when nothing is known."""
        primary, secondary = normalizer.select_pair(
            self.poll_frames(), preferred=self._preferred)
        if primary is None:
            return None
        return protocol.frame_to_usage(primary, self._now(), secondary)


def make_fetch(providers=None, preferred_provider="claude"):
    """Zero-arg callable for Bridge(fetch_usage=...).

    Same shape as the single-source make_fetch it replaces, so the daemon's
    wiring did not have to learn that there is now more than one source.
    """
    bus = IngestionBus(providers=providers,
                       preferred_provider=preferred_provider)
    return bus.poll
