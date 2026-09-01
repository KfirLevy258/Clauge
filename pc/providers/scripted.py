"""Replay a scenario timeline instead of reading real tools.

The fleet test suite runs the real daemon against real boards on several
machines, and every one of them needs to reproduce the same made-up usage
history -- "percentage climbs past 100", "this reading is four hours old" --
identically. A real provider reads whatever a tool happened to write on that
particular machine at that particular moment, which is exactly the opposite
of a repeatable scenario. This module trades the real sources for a JSON
script: the scenario becomes the source of truth instead of the machine, so
the same file drives the same board behaviour everywhere.

Scenario JSON format:

    {
      "name": "overage",
      "steps": [
        {"at": 0,  "provider": "claude", "session_pct": 97.0, "state": "running"},
        {"at": 10, "provider": "claude", "session_pct": 102.0, "state": "failed", "age_s": 3600}
      ]
    }

Each step is a NormalizedUsageFrame expressed as plain JSON, plus two fields
that are not part of the frame itself:

  - `at`: seconds since the provider was constructed. poll() emits a step
    once elapsed real time (per the `now` clock) reaches it, and only once --
    a scenario step is an event, not a level, so replaying it on every poll
    would misrepresent a single reading as a continuous one. One step per
    poll, oldest first: a poll turns into a single usage message however many
    frames it carries, so a late first poll has to delay the timeline rather
    than collapse the steps that came due while it waited. See poll().

  - `age_s` (default 0): how old the reading should claim to be, i.e.
    `observed_at = now - age_s`. This exists because staleness and "how old
    is this" captions are themselves scenarios under test, not just an
    artifact of when the script happens to run -- a step can claim to be
    four hours old the instant it fires.
"""
import json
import sys
import time

from pc.providers.base import NormalizedUsageFrame, ProviderParser


def _is_number(v):
    # bool is an int subclass; a step should never mean "at": true.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class ScriptedProvider(ProviderParser):
    def __init__(self, path, now=time.time):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.name = doc.get("name", "scenario")
        self._steps = sorted(self._usable_steps(doc.get("steps", [])),
                              key=lambda s: s["at"])
        self._emitted = set()
        self._now = now
        self._t0 = now()

    @staticmethod
    def _usable_steps(raw_steps):
        """Scenario steps shaped enough to sort and replay.

        A scenario file is hand-authored, and a typo in it -- a step with no
        `at`, or one that is not even an object -- must not take the
        provider down before it can construct at all. Same "return nothing
        usable" spirit as parse_cli_event; here that means dropping the one
        step rather than the whole scenario.
        """
        for i, step in enumerate(raw_steps):
            if not isinstance(step, dict) or not _is_number(step.get("at")):
                print(f"scripted: skipping step {i}, no numeric 'at'",
                      file=sys.stderr)
                continue
            yield step

    def get_provider_id(self) -> str:
        return "scripted"

    def poll(self, now_epoch):
        """The OLDEST step that has come due and not yet been emitted. One.

        Not every due step, which is what this used to return, because the
        daemon collapses one poll into one usage message: IngestionBus.poll
        hands its frames to normalizer.select_pair(), which picks a primary
        and a secondary and sends exactly one frame to the board. Two steps
        emitted together are therefore one frame on the wire, not two.

        And two steps do come due together on a healthy desk. The daemon's
        first poll is gated on board_alive() (claude_usage_bridge.py:1076-1084),
        false until the board's first ping; a scenario pass connects to a board
        that is already running, so there is no boot hello and the first ping
        lands up to PING_INTERVAL_MS (10 s) later -- and a poll skipped by that
        gate is lost rather than deferred, since next_poll advances either way.
        Meanwhile this provider stamps t0 at construction. Every fleet scenario
        writes min_tx as its own step count, so merging two steps into one
        frame fails a perfectly healthy board -- and a flaky release gate
        teaches people to re-run until green, which ends the gate.

        One per call turns a late start into a delayed timeline instead of a
        lost frame: each step gets its own poll, its own message, and its own
        [usage] line off the board. The scenarios have the headroom for it --
        the six-step usage_climb, polled every 3 s, delivers its last step
        around 27 s into a 44 s pass even if the first poll only fires at 12 s.

        A step whose frame cannot be built does not consume the call: it is a
        typo in a hand-written scenario, not a reading, and dropping the poll
        with it would cost the run a frame it was owed.
        """
        elapsed = self._now() - self._t0
        for i, step in enumerate(self._steps):
            if i in self._emitted or step["at"] > elapsed:
                continue
            self._emitted.add(i)
            frame = self._build_frame(step)
            if frame is not None:
                return [frame]
        return []

    def _build_frame(self, step):
        """One step as a frame, or None when the step doesn't fit.

        A field name that doesn't match NormalizedUsageFrame -- a typo, or a
        step that collides with `provider`/`src`/`observed_at` -- raises
        TypeError out of the constructor. This is the fleet-test analogue of
        the no-raise rule every other provider follows (base.py:154-157): a
        daemon on all three machines must not die because one scenario file
        has a typo in it. The step is dropped instead, same as an unusable
        real-source reading.
        """
        try:
            age = step.get("age_s", 0)
            fields = {k: v for k, v in step.items()
                      if k not in ("at", "age_s", "provider")}
            return NormalizedUsageFrame(
                provider=step.get("provider", "claude"),
                src="scripted",
                observed_at=self._now() - age,
                **fields)
        except TypeError as e:
            print(f"scripted: skipping malformed step ({e})", file=sys.stderr)
            return None
