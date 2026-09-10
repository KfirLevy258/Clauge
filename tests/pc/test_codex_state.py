"""The Codex hook slots: a second directory, read by the same machine.

Deliberately thin, because the module is: the state machine, the sweep and the
waiting marker are claude_state's and are pinned in tests/pc/test_state_machine
against that provider. What is pinned HERE is everything the second directory
adds -- which directory is read, that the caller's arguments are honoured
rather than a default falling back to the real home, that the marker survives
the delegation, and that the (state, name) pairs claude_state returns are
unwrapped to the plain states this module promises.
"""
import os

import pytest

from pc.providers import base, claude_state, codex_state

NOW = 1_700_000_000.0


def _slot(d, sid, event, t):
    """One hook slot, the shape tools/blink-hook.sh writes."""
    (d / (sid + ".state")).write_text('{"event":"%s","t":%d}' % (event, int(t)))


def _stamp(path, t):
    """Pin a file's mtime, because the marker and the agent files are read by
    mtime and the tests run at a real clock NOW is nowhere near."""
    os.utime(path, (t, t))


@pytest.fixture
def d(tmp_path):
    p = tmp_path / "state-codex"
    p.mkdir()
    return p


def test_the_default_directory_is_not_the_claude_one():
    """The whole point of the second directory.

    Two sessions with the same id are effectively impossible (both tools use
    UUIDs), but the ATTRIBUTION is the real risk: a Codex session read out of
    ~/.blink/state is reported to the board as a Claude one, on a Claude pip,
    against a Claude account's limits.
    """
    assert codex_state.STATE_DIR != claude_state.STATE_DIR
    assert codex_state.STATE_DIR.endswith("state-codex")


def test_the_arguments_are_what_is_honoured_not_a_default(monkeypatch, d):
    """No argument may quietly resolve to the real home.

    This module's default path is a directory the sweep DELETES files in, so
    "the default happened to work" is not the property to pin -- that a caller's
    path and sweep flag reach the provider is. The provider is replaced rather
    than run so the assertion is about what was asked for, not about what a
    directory happened to contain.
    """
    seen = []

    class Recorder:
        def __init__(self, path=None, sweep=True):
            seen.append((path, sweep))

        def session_states(self, now_epoch):
            return {}, 0

    monkeypatch.setattr(claude_state, "ClaudeStateProvider", Recorder)

    codex_state.scan(NOW, path=str(d), sweep=False)
    codex_state.scan(NOW)

    assert seen[0] == (str(d), False)
    # Expanded, not passed through as a literal tilde -- open() would make a
    # directory called "~" in the working directory rather than fail loudly.
    assert seen[1] == (os.path.expanduser(codex_state.STATE_DIR), True)
    # Normalised, because STATE_DIR is a "/"-separated constant and os.sep is
    # "\" on Windows: expanduser fills in the home and leaves the rest alone,
    # so the real value is "C:\Users\me/.blink/state-codex". Windows opens that
    # perfectly well -- the mixed separator is cosmetic -- but a raw startswith
    # against os.sep called it an escape from the home directory, which is the
    # one thing this line is here to rule out.
    home = os.path.normcase(os.path.normpath(os.path.expanduser("~")))
    got = os.path.normcase(os.path.normpath(seen[1][0]))
    assert got.startswith(home + os.sep)


def test_scan_reads_the_slots_the_shim_writes(d):
    """States by session id, plus the agents, with the names dropped."""
    _slot(d, "cx-1", "PermissionRequest", NOW - 3)
    _slot(d, "cx-2", "PreToolUse", NOW - 3)
    (d / "cx-2").mkdir()
    (d / "cx-2" / "agent-a").write_text("")
    _stamp(d / "cx-2" / "agent-a", NOW - 30)

    states, agents = codex_state.scan(NOW, path=str(d), sweep=False)

    assert states == {"cx-1": base.STATE_WAITING, "cx-2": base.STATE_RUNNING}
    assert agents == 1


def test_the_waiting_marker_beats_a_slot_that_says_running(d):
    """The race this whole design exists for, seen from the Codex side.

    A real Codex session fired PreToolUse and PermissionRequest in the same
    second (docs/research/codex-hook-contract.md). Two shim processes, one
    slot, each ending in mv -f: whichever finishes second wins, so a session
    blocked on a person can hold a slot that says `running` for as long as the
    person takes to answer -- which is precisely the interval this feature is
    for. The marker is the witness that cannot be overwritten, and the Codex
    reader has to honour it or the feature only works for Claude.
    """
    _slot(d, "cx-1", "PreToolUse", NOW - 2)
    marker = d / ("cx-1" + claude_state.WAITING_MARKER_SUFFIX)
    marker.write_text("")
    _stamp(marker, NOW - 2)

    states, _ = codex_state.scan(NOW, path=str(d), sweep=False)

    assert states == {"cx-1": base.STATE_WAITING}


@pytest.mark.parametrize("clearing_event, expected", [
    ("PostToolUse", base.STATE_RUNNING),
    ("UserPromptSubmit", base.STATE_RUNNING),
    ("Stop", base.STATE_IDLE),
    ("Interrupt", base.STATE_IDLE),
])
def test_every_event_that_can_follow_a_prompt_clears_the_wait(
        d, clearing_event, expected):
    """A waiting state with no way out is worse than no waiting state.

    These are the four events Codex can fire after a PermissionRequest: the
    tool was approved and ran, the person typed something else, the turn
    finished, or the person pressed Esc. Each is asserted separately because
    the failure mode is one of them being forgotten, not all four. Note that
    the shim removes the marker on these same four, so what is pinned here is
    the other half of the same rule -- the slot's own reading.
    """
    _slot(d, "cx-1", "PermissionRequest", NOW - 3)
    states, _ = codex_state.scan(NOW, path=str(d), sweep=False)
    assert states == {"cx-1": base.STATE_WAITING}

    _slot(d, "cx-1", clearing_event, NOW - 1)
    states, _ = codex_state.scan(NOW, path=str(d), sweep=False)
    assert states == {"cx-1": expected}


def test_sweeping_is_the_callers_decision(tmp_path):
    """A look must be able to be only a look.

    An abandoned slot -- a terminal closed without SessionEnd -- is collected
    on a poll and left alone on a read-only pass, and nothing else ever
    collects it. Both directions are asserted because a flag that is ignored
    is ignored in one of two ways, and each is a different bug: never
    collecting leaks the directory, always collecting means `blink status`
    deletes the evidence somebody ran it to see.
    """
    swept = tmp_path / "swept"
    swept.mkdir()
    kept = tmp_path / "kept"
    kept.mkdir()
    for d in (swept, kept):
        _slot(d, "cx-old", "Stop", NOW - claude_state.ABANDONED_AFTER_S - 60)

    assert codex_state.scan(NOW, path=str(swept), sweep=True) == ({}, 0)
    assert codex_state.scan(NOW, path=str(kept), sweep=False) == ({}, 0)

    assert not (swept / "cx-old.state").exists()
    assert (kept / "cx-old.state").exists()


def test_a_missing_directory_is_an_ordinary_state(tmp_path):
    """Codex not installed, or the hook never registered. Both are normal."""
    assert codex_state.scan(NOW, path=str(tmp_path / "nope"),
                            sweep=False) == ({}, 0)


# --- the tombstone -----------------------------------------------------------
#
# scan() answers "which sessions are alive". ended() answers the question the
# rollout reader cannot: "did anything ever watch this one die". Both are
# claude_state's machinery; what is pinned here is the same three things the
# rest of this file pins about the second directory -- which one is read, that
# the caller's arguments are honoured rather than a default reaching the real
# home, and that the answer arrives in the shape this module promises.


def _tomb(d, sid, t):
    """The file SessionEnd leaves where the slot was, aged by its mtime."""
    p = d / (sid + ".ended")
    p.write_text("")
    _stamp(p, t)
    return p


def test_a_tombstone_names_the_session_the_hook_buried(d):
    """A set of plain session ids -- the same keys the rollout reader uses,
    since matching them is the only thing this answer is for."""
    _tomb(d, "cx-1", NOW - 30)
    _tomb(d, "cx-2", NOW - 30)
    _slot(d, "cx-3", "PreToolUse", NOW - 30)

    assert codex_state.ended(NOW, path=str(d), sweep=False) == {"cx-1", "cx-2"}


def test_a_tombstone_past_the_hour_is_no_longer_evidence(d):
    """Bounded by the same clock as the slots and the waiting marker. An
    unbounded tombstone would suppress a session id forever, and this
    directory is the one place a session id can come back: `codex resume`
    reopens one under its own id."""
    _tomb(d, "cx-old", NOW - claude_state.ABANDONED_AFTER_S - 60)

    assert codex_state.ended(NOW, path=str(d), sweep=False) == set()


def test_collecting_tombstones_is_the_callers_decision(tmp_path):
    """Both directions, for the reason the slot sweep pins both: never
    collecting leaks one empty file per session forever, since nothing else
    ever looks at these names again, and always collecting means `blink
    status` deletes the evidence somebody ran it to see."""
    swept = tmp_path / "swept"
    swept.mkdir()
    kept = tmp_path / "kept"
    kept.mkdir()
    for p in (swept, kept):
        _tomb(p, "cx-old", NOW - claude_state.ABANDONED_AFTER_S - 60)
        _tomb(p, "cx-new", NOW - 30)

    assert codex_state.ended(NOW, path=str(swept), sweep=True) == {"cx-new"}
    assert codex_state.ended(NOW, path=str(kept), sweep=False) == {"cx-new"}

    assert not (swept / "cx-old.ended").exists()
    assert (kept / "cx-old.ended").exists()
    assert (swept / "cx-new.ended").exists(), "a live tombstone is not litter"


def test_reading_tombstones_never_falls_back_to_the_real_home(monkeypatch, d):
    """The same rule the scan above is held to, and for a sharper reason: this
    call DELETES files under the path it is given, so a default quietly
    resolving to the real state-codex directory would reach the slots driving
    the board on the owner's desk."""
    seen = []

    class Spy:
        def __init__(self, path=None, sweep=True):
            seen.append((path, sweep))

        def ended_sessions(self, now_epoch):
            return set()

    monkeypatch.setattr(claude_state, "ClaudeStateProvider", Spy)
    codex_state.ended(NOW, path=str(d), sweep=False)
    codex_state.ended(NOW)

    assert seen[0] == (str(d), False)
    assert seen[1] == (os.path.expanduser(codex_state.STATE_DIR), True)


def test_a_missing_directory_has_buried_nothing(tmp_path):
    """Codex not installed, or no session has ever ended. Both are ordinary,
    and neither may raise on the poll path."""
    assert codex_state.ended(NOW, path=str(tmp_path / "nope"),
                             sweep=False) == set()
