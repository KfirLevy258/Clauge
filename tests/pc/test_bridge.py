import ast
import pathlib
import unittest

import claude_usage_bridge
from pc import protocol
from pc.bridge import Bridge
from pc.providers import base


def raising(exc):
    def _f():
        raise exc
    return _f


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clock = FakeClock()
        self.bridge = Bridge(
            write_msg=lambda m: self.sent.append(m),
            fetch_usage=lambda: protocol.usage(61.0, "R1", 26.0, "R2",
                                               [{"name": "sonnet", "weekly_pct": 2.0}]),
            now=self.clock.now,
            app_ver="0.2.0",
            wall=lambda: (1752444000, 180),
        )

    def test_hello_triggers_welcome_time_usage(self):
        self.bridge.on_message({"t": "hello", "v": 1, "board_id": "ab"})
        types = [m["t"] for m in self.sent]
        self.assertEqual(types, ["welcome", "time", "usage"])
        self.assertEqual(self.sent[2]["session_pct"], 61.0)

    def test_time_uses_injected_wall_clock(self):
        b = Bridge(write_msg=self.sent.append,
                   fetch_usage=lambda: protocol.usage(1.0, "R", 2.0, "R", []),
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.on_message({"t": "hello", "v": 1})
        t = [m for m in self.sent if m["t"] == "time"][0]
        self.assertEqual(t["epoch"], 1752444000)
        self.assertEqual(t["utc_offset_min"], 180)

    def test_poll_pushes_time_then_usage(self):
        self.bridge.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])

    def test_poll_with_no_payload_yet_sends_time_only(self):
        """statusline_source.make_fetch() returns None before Claude Code has
        ever written a payload. That is not an error -- there is simply
        nothing to report yet -- so no 'usage' or 'status' message should
        follow, and the board just keeps whatever it last had."""
        b = Bridge(write_msg=self.sent.append, fetch_usage=lambda: None,
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time"])

    def test_ping_updates_liveness(self):
        self.assertFalse(self.bridge.board_alive())
        self.bridge.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertTrue(self.bridge.board_alive())
        self.clock.t += 31
        self.assertFalse(self.bridge.board_alive())

    def test_ping_is_answered_with_pong(self):
        """Liveness must be bidirectional. The daemon only pushes usage every
        60 s, so without an answer to the board's 10 s ping the board cannot
        tell 'host alive, not due to poll yet' from 'host died' -- and would
        keep showing a green dot over frozen numbers."""
        self.bridge.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertEqual([m["t"] for m in self.sent], ["pong"])

    def test_pong_costs_no_api_call(self):
        """A pong must be free. The usage endpoint is aggressively rate-limited,
        so answering a 10 s ping by fetching would get us 429'd."""
        calls = []

        def counting_fetch():
            calls.append(1)
            return protocol.usage(1.0, "R", 2.0, "R", [])

        b = Bridge(write_msg=self.sent.append, fetch_usage=counting_fetch,
                   now=self.clock.now)
        b.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertEqual(calls, [])

    def test_unknown_type_ignored(self):
        self.bridge.on_message({"t": "wat", "v": 1})
        self.assertEqual(self.sent, [])

    def test_poll_sends_usage_or_status(self):
        self.bridge.poll_once()
        self.assertEqual(self.sent[-1]["t"], "usage")

    def test_hello_with_failed_fetch_still_welcomes(self):
        """A dead API must not mute the handshake: the board needs welcome
        (mode detection) and time (clock) even when usage is unavailable,
        and the failure must arrive as a status message, not an exception."""
        def boom():
            raise RuntimeError("api down")
        b = Bridge(write_msg=self.sent.append, fetch_usage=boom,
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.on_message({"t": "hello", "v": 2, "board_id": "ab"})
        self.assertEqual([m["t"] for m in self.sent],
                         ["welcome", "time", "status"])
        self.assertEqual(self.sent[-1]["state"], "error")

    def test_poll_error_sends_status(self):
        def boom():
            raise RuntimeError("429")
        b = Bridge(write_msg=lambda m: self.sent.append(m),
                   fetch_usage=boom, now=self.clock.now, app_ver="0.2.0")
        b.poll_once()
        self.assertEqual(self.sent[-1]["t"], "status")
        self.assertEqual(self.sent[-1]["state"], "error")

    def test_stale_usage_needs_no_second_message(self):
        """The usage message carries `stale` and proto.c reads it, so the
        board colours its own dot from the reading it was just given.

        This used to assert the opposite -- that a status "rate_limited"
        followed -- which was a workaround for firmware that had no bool
        getter. It made a stale reading and a real rate limit identical on
        the panel and in the log.
        """
        b = Bridge(write_msg=self.sent.append,
                   fetch_usage=lambda: protocol.usage(
                       61.0, "R1", 26.0, "R2", [], stale=True),
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])
        self.assertTrue(self.sent[-1]["stale"])

    def test_fresh_usage_sends_no_extra_status(self):
        self.bridge.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])


if __name__ == "__main__":
    unittest.main()


# TestRateLimit lived here and is gone with the code it covered.
#
# Seven tests of an HTTP 429 ladder, against a fetch that now reads a local
# file and cannot be rate limited by anyone. They passed by injecting an
# exception production could not produce, which is the shape of a test that
# outlives its subject: green, specific, and describing something that stopped
# being true when pc/usage_api.py was deleted.


class PrefMessage(unittest.TestCase):
    """The board announces which provider the user made primary."""

    def _bridge(self, applied):
        return Bridge(write_msg=lambda m: None, fetch_usage=lambda: None,
                      set_preferred=lambda p: applied.append(p) or True)

    def test_a_pref_message_reaches_the_bus(self):
        applied = []
        self._bridge(applied).on_message(
            {"t": "pref", "v": 2, "provider": "codex"})
        self.assertEqual(applied, ["codex"])

    def test_a_pref_without_a_provider_is_ignored(self):
        applied = []
        self._bridge(applied).on_message({"t": "pref", "v": 2})
        self.assertEqual(applied, [])

    def test_a_non_string_provider_is_ignored(self):
        applied = []
        self._bridge(applied).on_message(
            {"t": "pref", "v": 2, "provider": 7})
        self.assertEqual(applied, [])

    def test_a_daemon_without_a_bus_does_not_crash_on_pref(self):
        """The tests wire a Bridge with no bus at all; a board that announces
        its preference to one must not take it down."""
        b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None)
        b.on_message({"t": "pref", "v": 2, "provider": "codex"})


# --- the OTA guard must not depend on a reset --------------------------------
#
# hello is sent once, at boot. The daemon now connects to an already-running
# board without resetting it, so there is no hello -- and _board_ahead() gated
# the one operation that writes slot0 in place with no revert behind it.


def test_the_protocol_version_is_learned_without_a_hello():
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None)
    assert b._board_proto is None
    b.on_message({"t": "ping", "v": 99, "up_ms": 10})
    assert b._board_proto == 99
    assert b._board_ahead() is True


def test_a_board_ahead_is_refused_an_update_on_a_no_reset_connection(capsys):
    """The regression in full: connect with no hello, then a query."""
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None)
    b.on_message({"t": "ping", "v": 99, "up_ms": 10})      # no hello anywhere
    b.on_message({"t": "ota_query", "v": 99, "cur": "9.9.9"})
    assert any(m.get("t") == "ota_none" for m in sent)
    assert not any(m.get("t") == "ota_avail" for m in sent)


def test_the_firmware_version_is_learned_from_the_query():
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None)
    b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})
    assert b._board_fw == "0.6.0"


def test_hello_still_wins_and_does_not_double_announce(capsys):
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None)
    b.on_message({"t": "hello", "v": 2, "fw": "0.6.0", "board": "cyd"})
    assert b._board_proto == 2
    assert b._board_fw == "0.6.0"
    assert b._board_ahead() is False


# --- the pair must not split ------------------------------------------------
#
# Firmware and the daemon ship from one tag and install together: _on_ota_flash
# does the app first, so the newest firmware is never driven by the oldest app.
# The split happens when the app half cannot be worked out at all -- a network
# blip or a signature that will not verify makes fetch_signed_manifest return
# None, and _app_available answers None, which is indistinguishable from "this
# release has no newer app". Offering the firmware anyway turned a transient
# failure into a permanent mismatch, and the half left behind is the one the
# panel cannot reach.

from pc.version import RELEASE_VERSION as _APP_VER    # noqa: E402


def _ahead_of_the_app():
    major, minor, patch = (int(n) for n in _APP_VER.split("."))
    return f"{major + 1}.{minor}.{patch}"


def _ota_bridge(sent, manifest, app_update, self_update=lambda *a: True):
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None,
               fetch_manifest=lambda: manifest, self_update=self_update)
    b._app_available = lambda m: app_update
    return b


def _newer_release(version):
    return {"version": version, "size": 100, "sha256": "ab" * 32}


def test_firmware_newer_than_this_app_is_not_offered_without_its_app_half(
        capsys):
    sent = []
    version = _ahead_of_the_app()
    b = _ota_bridge(sent, _newer_release(version), None)
    b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})

    assert any(m.get("t") == "ota_none" for m in sent)
    assert not any(m.get("t") == "ota_avail" for m in sent)
    err = capsys.readouterr().err
    assert "not offering" in err
    assert "signed manifest could not be read" in err


def test_firmware_newer_than_this_app_IS_offered_with_its_app_half(capsys):
    """The ordinary case, and the reason the rule is about the app half
    rather than about the version: both are in hand, so they install
    together and nothing is left behind."""
    sent = []
    version = _ahead_of_the_app()
    b = _ota_bridge(sent, _newer_release(version),
                    (version, {"size": 1, "sha256": "cd" * 32}))
    b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})

    avail = [m for m in sent if m.get("t") == "ota_avail"]
    assert avail, sent
    assert avail[0].get("app") == version


def test_a_release_level_with_this_app_is_offered_as_it_always_was():
    """A firmware-only release -- the manifest carries no newer daemon -- is
    exactly the case _app_available answers None for legitimately. The rule
    must not swallow it."""
    sent = []
    b = _ota_bridge(sent, _newer_release(_APP_VER), None)
    b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})

    assert any(m.get("t") == "ota_avail" for m in sent), sent


def test_a_platform_with_no_published_app_still_gets_its_firmware(capsys):
    """The pair CANNOT be kept on a machine the release publishes no daemon
    for. Refusing firmware there would strand it forever, so it is offered
    with the reason in the log."""
    sent = []
    version = _ahead_of_the_app()
    b = _ota_bridge(sent, _newer_release(version), None)

    from pc import update as update_mod
    real = update_mod.platform_key
    update_mod.platform_key = lambda: None
    try:
        b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})
    finally:
        update_mod.platform_key = real

    assert any(m.get("t") == "ota_avail" for m in sent), sent
    assert "publishes no app for this platform" in capsys.readouterr().err


def test_a_daemon_that_cannot_replace_itself_is_not_held_back():
    """Nothing wires self_update outside claude_usage_bridge -- a checkout, a
    test, `blink run` from source. There is no app half to pair with there,
    and refusing firmware would break the development path for a rule that
    exists to protect customers."""
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None,
               fetch_manifest=lambda: _newer_release(_ahead_of_the_app()))
    b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})
    assert any(m.get("t") == "ota_avail" for m in sent), sent


class OverageCapIsWiredTest(unittest.TestCase):
    """poll_once must actually apply the cap.

    protocol.cap_overage_for_fw was well covered on its own, but nothing
    exercised the one line in poll_once that calls it -- deleting that line
    left the whole suite green while every board in the field went back to
    drawing 0% the moment its owner crossed into extra usage.
    """

    def _bridge(self, sent):
        return Bridge(write_msg=sent.append,
                      fetch_usage=lambda: {"t": "usage", "v": protocol.VERSION,
                                           "session_pct": 22.0,
                                           "weekly_pct": 102.0})

    def _weekly(self, sent):
        return [m for m in sent if m.get("t") == "usage"][0]["weekly_pct"]

    def test_an_old_board_is_capped_on_the_way_out(self):
        sent = []
        b = self._bridge(sent)
        b.on_message({"t": "hello", "v": 2, "fw": "1.2.3",
                      "board_id": "abc"})
        sent.clear()
        b.poll_once()
        self.assertEqual(self._weekly(sent), 100.0)

    def test_a_new_board_receives_the_true_number(self):
        sent = []
        b = self._bridge(sent)
        b.on_message({"t": "hello", "v": 2, "board_id": "abc",
                      "fw": ".".join(str(x)
                                     for x in protocol.FW_ACCEPTS_OVERAGE)})
        sent.clear()
        b.poll_once()
        self.assertEqual(self._weekly(sent), 102.0)

    def test_before_any_hello_the_push_is_capped(self):
        """greet() can push before a board has said hello."""
        sent = []
        self._bridge(sent).poll_once()
        self.assertEqual(self._weekly(sent), 100.0)


class SessionMessageIsSent(unittest.TestCase):
    """The project name, in its own message beside the usage line.

    The fetch callable carries it as an attribute: the Bridge is handed a
    zero-arg callable returning a finished usage dict and never sees the
    frame the name lives on (pc/ingest.make_fetch hangs session_pair on it).
    """

    def setUp(self):
        self.sent = []
        self.pair = ("LiveClaudeUi", 1)

        def fetch():
            return protocol.usage(61.0, "R1", 26.0, "R2", [])

        fetch.session_pair = lambda: self.pair
        self.bridge = Bridge(write_msg=self.sent.append, fetch_usage=fetch,
                             now=FakeClock().now, app_ver="0.2.0",
                             wall=lambda: (1752444000, 180))

    def _sessions(self):
        return [m for m in self.sent if m.get("t") == "session"]

    def test_session_message_is_sent_when_the_label_changes(self):
        self.bridge.poll_once()
        self.assertEqual(self._sessions()[-1]["label"], "LiveClaudeUi")

        self.bridge.poll_once()          # nothing changed
        self.assertEqual(len(self._sessions()), 1)

        self.pair = ("Blink", 2)
        self.bridge.poll_once()
        self.assertEqual(self._sessions()[-1]["label"], "Blink")
        self.assertEqual(self._sessions()[-1]["n"], 2)

    def test_session_message_is_resent_on_greet(self):
        # A board that just booted holds nothing. Without this a replugged
        # board shows a bare status until the next time the project happens
        # to change -- the same reason firmware currency is re-offered on
        # every connect rather than once per daemon lifetime.
        self.bridge.poll_once()
        self.sent.clear()
        self.bridge.greet()
        self.assertTrue(self._sessions())

    def test_the_count_travels_even_when_the_label_is_empty(self):
        """Several sessions share the state, so none of them can be named --
        the board falls back to the count, which still has to arrive."""
        self.pair = ("", 3)
        self.bridge.poll_once()
        m = self._sessions()[-1]
        self.assertNotIn("label", m)
        self.assertEqual(m["n"], 3)

    def test_the_fast_tick_sees_a_project_change_the_usage_line_cannot(self):
        """A session in project A ends and one in project B starts running.
        `state` is still "running", every count is where it was, and the
        usage dict is byte-identical -- so the tick that exists to push
        session moves within two seconds found nothing to send, and the panel
        named the wrong project until the next heartbeat, up to a minute
        later."""
        self.bridge.poll_once()
        self.sent.clear()

        self.pair = ("Blink", 1)
        self.bridge.poll_if_changed()

        self.assertEqual(self._sessions()[-1]["label"], "Blink")

    def test_a_name_alone_does_not_re_send_the_usage_line(self):
        """The tick's whole reason for comparing: the fully-loaded usage line
        is 509 of the 512 bytes the firmware accepts, and the name has its
        own message precisely so it need not ride one."""
        self.bridge.poll_once()
        self.sent.clear()

        self.pair = ("Blink", 1)
        self.bridge.poll_if_changed()

        self.assertEqual([m["t"] for m in self.sent], ["session"])

    def test_an_unchanged_desk_still_sends_nothing(self):
        """The property the comparison protects, which the new branch must
        not spend: thirty ticks a minute, all silent, when nothing moved."""
        self.bridge.poll_once()
        self.sent.clear()

        for _ in range(5):
            self.bridge.poll_if_changed()

        self.assertEqual(self.sent, [])

    def test_a_fetch_without_the_accessor_sends_nothing(self):
        """Every other fetch in this file is a bare lambda, and the
        single-source fetch has no session to report. Sending nothing is
        exactly what an older daemon did."""
        sent = []
        b = Bridge(write_msg=sent.append,
                   fetch_usage=lambda: protocol.usage(1.0, "R", 2.0, "R", []),
                   now=FakeClock().now, wall=lambda: (1752444000, 180))
        b.poll_once()
        self.assertEqual([m["t"] for m in sent], ["time", "usage"])


class FastTickSendsOnlyRealNews(unittest.TestCase):
    """The change-driven push, in both directions.

    Session state is a *now* signal and the panel was up to a minute behind it.
    The fast tick closes that -- but several fields on a usage message move on
    every poll by their nature (the ages, the two countdowns), so a comparison
    of whole messages is always "changed" and would be a 2 s UNCONDITIONAL
    push wearing the costume of a conditional one. Both halves are asserted
    here, and the silent direction first proves the message really did change
    so that it cannot pass by testing a static fixture.
    """

    def setUp(self):
        self.sent = []
        self.now = 1_787_700_000.0
        self.state = "running"
        self.n_run = 1
        self.n_wait = 0
        # Absolute and fixed, which is what makes the ages and the countdowns
        # move: the files were written once, and every second of self.now that
        # passes makes the reading older and the reset nearer. Tying them to
        # self.now instead would hold every volatile field still and the test
        # below would prove nothing.
        self.observed_at = self.now - 30
        self.active_at = self.now - 5
        self.session_resets_at = self.now + 3600
        self.weekly_resets_at = self.now + 86400

        def fetch():
            # Through frame_to_usage, because that is the only thing that puts
            # a usage line on the wire -- and the only thing that fills in the
            # ages and countdowns from the clock.
            f = base.NormalizedUsageFrame(
                provider="claude", src="cli",
                observed_at=self.observed_at, active_at=self.active_at,
                session_pct=61.0,
                session_resets_at=self.session_resets_at,
                weekly_pct=26.0, weekly_resets_at=self.weekly_resets_at,
                state=self.state, n_run=self.n_run, n_wait=self.n_wait)
            return protocol.frame_to_usage(f, self.now)

        self.fetch = fetch
        self.bridge = Bridge(write_msg=self.sent.append, fetch_usage=fetch,
                             now=FakeClock().now, app_ver="0.2.0",
                             wall=lambda: (int(self.now), 180))
        self.bridge.poll_once()      # the heartbeat sets the baseline
        self.sent.clear()

    def _usage(self):
        return [m for m in self.sent if m.get("t") == "usage"]

    def test_a_tick_where_only_the_clock_moved_sends_nothing(self):
        """Compare whole messages instead of protocol.meaningful_usage() and
        this fails: every field asserted below moved, none of them is news."""
        before = self.fetch()
        self.now += 12
        after = self.fetch()

        # The fixture is genuinely different from one tick to the next. Without
        # this the silence proved below could just be a constant payload.
        self.assertNotEqual(after, before)
        self.assertNotEqual(after["age_s"], before["age_s"])
        self.assertNotEqual(after["active_age_s"], before["active_age_s"])
        self.assertNotEqual(after["session_resets_in_s"],
                            before["session_resets_in_s"])

        self.bridge.poll_if_changed()
        self.assertEqual(self.sent, [])

    def test_a_state_change_reaches_the_wire_on_the_very_next_tick(self):
        """Keep the 60 s gate and this fails: two seconds in, the panel has
        the new state."""
        self.now += 2
        self.state = "waiting"
        self.n_wait = 1
        self.bridge.poll_if_changed()

        self.assertEqual(len(self._usage()), 1)
        self.assertEqual(self._usage()[-1]["state"], "waiting")
        self.assertEqual(self._usage()[-1]["n_wait"], 1)

    def test_a_count_alone_is_enough(self):
        """`state` is the loud one, but the pips are drawn from the counts and
        a second session appearing is the same kind of news."""
        self.now += 2
        self.n_run = 2
        self.bridge.poll_if_changed()
        self.assertEqual(self._usage()[-1]["n_run"], 2)

    def test_the_fast_tick_does_not_re_anchor_the_clock(self):
        """`time` belongs to the heartbeat alone. The board re-anchors from it
        once a minute and has no reason to do so fifteen hundred times an
        hour."""
        self.now += 2
        self.state = "waiting"
        self.bridge.poll_if_changed()
        self.assertEqual([m["t"] for m in self.sent], ["usage"])

    def test_a_change_is_sent_once_and_not_again(self):
        """The baseline is what was sent, so the tick after a push is quiet
        again -- otherwise one state change turns into a push every two
        seconds for as long as it lasts."""
        self.now += 2
        self.state = "waiting"
        self.bridge.poll_if_changed()
        self.assertEqual(len(self._usage()), 1)

        self.now += 2
        self.bridge.poll_if_changed()
        self.assertEqual(len(self._usage()), 1)

    def test_the_heartbeat_still_pushes_when_nothing_changed(self):
        """The 60 s push is unchanged and unconditional: it is what re-states
        everything to a board that may have missed a message, and the only
        thing that proves a panel sitting on the same numbers all afternoon is
        still connected."""
        self.now += 12
        self.bridge.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])


class FastTickStaysQuietAboutTrouble(unittest.TestCase):
    """The fast tick reports neither a failing fetch nor a missing source.

    Both conditions persist -- they will be just as true two seconds from now
    -- so saying so at 0.5 Hz turns one message a minute into thirty. poll_once
    already reports both at a cadence a person can read, and it still does.
    """

    def test_a_raising_fetch_is_silent_on_the_fast_tick(self):
        sent = []
        b = Bridge(write_msg=sent.append,
                   fetch_usage=raising(RuntimeError("boom")),
                   now=FakeClock().now, wall=lambda: (1752444000, 180))
        b.poll_if_changed()
        self.assertEqual(sent, [])
        # ...and the heartbeat still says it.
        b.poll_once()
        self.assertEqual([m["t"] for m in sent], ["time", "status"])

    def test_no_payload_yet_is_silent_on_the_fast_tick(self):
        sent = []
        b = Bridge(write_msg=sent.append, fetch_usage=lambda: None,
                   now=FakeClock().now, wall=lambda: (1752444000, 180))
        b.poll_if_changed()
        self.assertEqual(sent, [])


def _daemon_tree():
    src = (pathlib.Path(claude_usage_bridge.__file__).resolve()
           .with_name("claude_usage_bridge.py"))
    return ast.parse(src.read_text(encoding="utf-8"))


def _calls(node, attr):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


def test_the_daemon_actually_runs_the_fast_tick():
    """Everything above tests a method nothing has to call.

    The Bridge can be perfect and the panel still a minute behind if the loop
    only ever reaches poll_once -- and no unit test of the Bridge can see
    that, because the loop it lives in has a serial port in it. So read the
    daemon's own source, which is the same guard pc/ingest's tests use for the
    same class of mistake (a correct object nobody wired up).
    """
    assert _calls(_daemon_tree(), "poll_if_changed"), \
        "the daemon never calls poll_if_changed: the fast tick is dead code"


def test_the_fast_tick_is_gated_on_a_board_that_answers():
    """Same gate as the heartbeat. Reading local files costs nothing, but
    writing into a port that stopped answering pings is how a daemon spends
    all afternoon talking to a panel that is not there -- and now it would do
    it thirty times a minute."""
    tree = _daemon_tree()
    ticks = _calls(tree, "poll_if_changed")
    assert ticks

    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _calls(node.test, "board_alive"):
            for call in _calls(node, "poll_if_changed"):
                guarded.add(id(call))
    ungated = [ast.dump(c) for c in ticks if id(c) not in guarded]
    assert not ungated, ungated


def test_the_two_cadences_are_far_apart_and_the_heartbeat_stayed():
    """The exact 2 s is a judgment call -- what must not drift is the shape:
    the look is human-scale and the unconditional push is still a minute.
    A fast tick anywhere near the heartbeat has stopped being a fast tick.

    The upper bound used to be spelled twice. `fast <= 5` made the ratio below
    arithmetic rather than a claim -- it could not fail while the bound held,
    which a mutation survey confirmed by moving the ratio and watching all 620
    tests stay green. The ratio is the real statement, so it is now the only
    one: how far apart the two cadences must be, rather than a number the fast
    one may not exceed.
    """
    fast = claude_usage_bridge.FAST_POLL_INTERVAL_S
    assert fast >= 1, fast
    assert claude_usage_bridge.POLL_INTERVAL_S == 60
    assert fast * 5 < claude_usage_bridge.POLL_INTERVAL_S
