"""Transport-agnostic bridge logic: react to board messages, poll usage, track
liveness. I/O (serial) is injected so this is unit-testable without hardware."""
import datetime
import hashlib
import sys
import time

from pc import ota as ota_mod
from pc import protocol
from pc.version import PROTO_VERSION, RELEASE_VERSION

LIVENESS_WINDOW_S = 30.0

# The 429 backoff that lived here is gone with the endpoint it was for.
#
# fetch_usage now reads a file Claude Code wrote (pc/statusline_source.py); it
# has no server to be throttled by, and read_payload() turns every failure into
# None rather than an exception. The ladder, the Retry-After parsing, the
# _throttled_until gate and their tests all described a branch that could not
# execute -- which is worse than absent, because it read as "rate limits are
# handled here" to anyone deciding what to do next.


def _local_wall():
    """(unix seconds, local UTC offset in minutes) -- DST-aware."""
    now = datetime.datetime.now().astimezone()
    return int(now.timestamp()), int(now.utcoffset().total_seconds() // 60)


class Bridge:
    def __init__(self, write_msg, fetch_usage, now=time.monotonic,
                 set_preferred=None,
                 app_ver=RELEASE_VERSION,
                 wall=_local_wall, fetch_manifest=None, fetch_firmware=None,
                 flash_image=None, self_update=None, pending=None,
                 fetch_signed_manifest=None, report_failure=None):
        self._write = write_msg          # callable(dict)
        self._fetch = fetch_usage        # callable() -> usage message dict
        # The board owns the primary-provider preference; this applies it.
        # None on a daemon wired without a bus (the tests do this).
        self._set_preferred = set_preferred
        self._now = now
        self._last_query_at = None       # see offer_if_newer
        self._wall = wall                # callable() -> (epoch_s, utc_offset_min)
        self._app_ver = app_ver
        self._last_ping = None
        # OTA. Injected so the transfer is testable without GitHub.
        self._fetch_manifest = fetch_manifest or ota_mod.fetch_manifest
        self._fetch_firmware = fetch_firmware or ota_mod.fetch_firmware
        self._manifest = None            # release offered to the board
        self._flash = flash_image        # callable(blob, version); owns the port
        # What the board says it is. Set from hello; None until it speaks.
        self._board_proto = None
        self._board_fw = None
        self._announced_ahead = False
        # Pair updates. self_update replaces this program and does not return
        # when it works; pending remembers the consent across that restart;
        # _fetch_signed is the only manifest source allowed to decide that a
        # binary should be installed (see _app_available).
        self._self_update = self_update
        self._pending = pending
        # A flash that failed on the previous connection, to be delivered once
        # the board is back. It cannot be told at the time: the port is closed
        # for esptool and the board is mid-reset.
        self._report_failure = report_failure
        self._said_no_source = False
        if fetch_signed_manifest is None:
            from pc import update as _u
            fetch_signed_manifest = _u.fetch_signed_manifest
        self._fetch_signed = fetch_signed_manifest
        self._app_update = None          # (version, artifact) from last query
        self._last_session = None        # (label, n) last sent; see poll_once
        # The last usage message actually PUT ON THE WIRE, minus the fields
        # that move on their own (protocol.VOLATILE_USAGE_KEYS). This is what
        # poll_if_changed decides against: "sent", not "read", because the
        # capping on the way out is part of what the board received.
        self._last_pushed = None

    def greet(self):
        """Introduce ourselves and push what we have, immediately.

        Normally this answers the board's boot `hello`. It is also called
        directly when the daemon found a board that was ALREADY running and
        chose not to reset it (claude_usage_bridge.probe_is_our_board): there
        is no hello in that case, and without this the first usage message
        waits for the 60 s poll -- which is itself gated on board_alive(),
        false until the first ping, so the real wait is a minute of blank
        panel after every service restart. Measured doing exactly that before
        this existed.
        """
        self._write(protocol.welcome("blink-bridge", self._app_ver))
        if self._report_failure:
            self._write(protocol.ota_error(self._report_failure))
            self._report_failure = None
        # A board that just booted holds no session message, and poll_once
        # only sends on change -- so on a reconnect the tracker is what makes
        # it silent. Clear it BEFORE the poll, so the push happens inside it,
        # the same shape as offer_if_newer on every connect rather than once
        # per daemon lifetime.
        self._last_session = None
        self.poll_once()                 # push current data immediately
        # Only once the board has been heard. On the no-reset connect path
        # greet() runs before any message has reached on_message, so
        # _board_proto is still None -- and _board_ahead() reads None as
        # "not ahead", which let a resumed flash skip the one guard that
        # keeps this daemon from writing slot0 on a board it does not
        # understand, while _on_ota_query latched _board_fw to the fallback
        # "0.0.0". The next message the board sends (its pref answers our
        # welcome; a ping follows within 10 s) arms the guard, and
        # on_message resumes then.
        if self._board_proto is not None:
            self._resume_pending()

    # --- inbound ---
    def on_message(self, msg: dict):
        t = msg.get("t")
        # Learn the board's protocol version from ANY message, not just hello.
        #
        # _board_ahead() gates the one operation that can leave a customer
        # holding a device that does not start, and it used to depend entirely
        # on _note_board, reachable only from the hello branch. hello is sent
        # once, at boot (proto.c send_hello) -- so as soon as the daemon
        # learned to connect to an already-running board WITHOUT resetting it,
        # there was no hello, _board_proto stayed None, and the guard degraded
        # silently to "allow". Found by review the same night that path landed.
        #
        # Every message the board sends carries its own "v", so the guard no
        # longer needs a reboot to arm. Firmware version comes from ota_query's
        # "cur" below, which is the only other thing _note_board supplied.
        if self._board_proto is None:
            try:
                self._board_proto = int(msg["v"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                self._announce_if_ahead()
                # The board has spoken, so the guard is armed: pick up an
                # approved install that greet() had to leave waiting. Not on
                # hello -- greet() below does it there, after the welcome,
                # so the board is greeted before it is asked to resume.
                if t != "hello":
                    self._resume_pending()
        if t == "pref":
            # Which provider the user picked on the board's settings screen.
            # It arrives with every hello as well as on change, so a daemon
            # that restarts picks the choice back up without the user
            # touching anything.
            want = msg.get("provider")
            if self._set_preferred and isinstance(want, str):
                if self._set_preferred(want):
                    print(f"[bridge] main source: {want}", file=sys.stderr)
            return
        if t == "hello":
            self._note_board(msg)
            self.greet()
            self.offer_if_newer(msg.get("fw"))
        elif t == "ping":
            self._last_ping = self._now()
            # Free: never fetch here. The usage endpoint is aggressively
            # rate-limited and this fires every 10 s.
            self._write(protocol.pong())
        elif t == "ota_query":
            self._on_ota_query(msg.get("cur", ""))
        elif t == "ota_flash":
            self._on_ota_flash()
        # unknown types ignored

    # --- OTA, for a board with no network of its own -------------------
    #
    # The board asks and approves; we fetch and write. The image is NOT sent
    # over this protocol -- an earlier revision did that in base64 chunks and
    # managed 213 B/s, and MCUboot still had to swap slot1 afterwards. esptool
    # against slot0 does the same job in about 75 s with no swap, and it is
    # the same command this project has always flashed with by hand.

    def _note_board(self, hello):
        """Record what the board is, and notice when it outranks us.

        Both sides have always stamped "v" on every message and neither side
        has ever read one. That is fine while the protocol only grows -- new
        fields are ignored by whoever does not know them -- but it leaves no
        way to refuse the one case that is genuinely unsafe: firmware that
        speaks a protocol this daemon does not, being driven by this daemon
        through a firmware update.
        """
        try:
            self._board_proto = int(hello.get("v"))
        except (TypeError, ValueError):
            self._board_proto = None
        self._board_fw = hello.get("fw")
        self._announce_if_ahead()

    def _announce_if_ahead(self):
        if self._board_ahead() and not self._announced_ahead:
            print(f"[bridge] the board speaks protocol {self._board_proto} and"
                  f" this app speaks {PROTO_VERSION} -- update the app on this"
                  " computer; firmware updates are held until you do",
                  file=sys.stderr)
            self._announced_ahead = True

    def _board_ahead(self) -> bool:
        return self._board_proto is not None and self._board_proto > PROTO_VERSION

    def _ota_reset(self):
        self._manifest = None

    def offer_if_newer(self, fw):
        """Check the feed for a board running `fw`, unasked.

        A board asks for itself once per boot and when its update row is
        tapped, and that is all it ever asked. So a board that stayed
        plugged in through three app updates never heard about the firmware
        that shipped with them (desk board on 1.0.0 with 1.1.1 published,
        2026-08-29), and a tap that landed while the app was restarting for
        one of those updates went unanswered forever. Now every connect --
        the board's hello, or a reconnect to a board already running --
        is a check, and the reply either shows "Install x.y.z" or "Up to
        date", clearing a stale "Checking..." either way. Skipped when the
        board itself asked within the last minute, so its boot-time query
        is not answered twice.
        """
        if not fw or self._fetch_manifest is None:
            return
        if self._last_query_at is not None and \
                self._now() - self._last_query_at < 60:
            return
        self._on_ota_query(fw)

    def _on_ota_query(self, cur):
        self._last_query_at = self._now()
        # The board's firmware version, from the board, on every query. The
        # other half of what _note_board used to be the only source of -- and
        # _resume_pending falls back to "0.0.0" without it, which would compare
        # a real release against a fabricated version.
        if cur and not self._board_fw:
            self._board_fw = cur
        # Refuse to drive a board we may not understand. This daemon writes
        # slot0 in place, with no test boot behind it, so "probably fine" is
        # not a good enough basis for the one operation that can leave a
        # customer holding a device that does not start.
        if self._board_ahead():
            print(f"[bridge] ota: board speaks protocol {self._board_proto},"
                  f" this app speaks {PROTO_VERSION} -- not offering an update",
                  file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_none())
            return
        m = self._fetch_manifest()
        # A release may declare the protocol it needs to be installed over.
        # Absent (every release so far) means no floor.
        floor = ((m or {}).get("fw") or {}).get("proto_min")
        if isinstance(floor, int) and floor > PROTO_VERSION:
            print(f"[bridge] ota: {m.get('version')} needs protocol {floor} and"
                  f" this app speaks {PROTO_VERSION} -- update the app first",
                  file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_none())
            return
        if not m or not ota_mod.is_newer(m.get("version", ""), cur):
            have = m.get("version", "?") if m else "unreachable"
            # Costs one signed fetch, and buys the board the answer to a
            # question it was otherwise left to guess at. This is the state a
            # working desk sits in, so it is the one that decides whether the
            # update row tells the truth.
            self._app_update = self._app_available(m)
            app = self._app_update[0] if self._app_update else None
            print(f"[bridge] ota: board has {cur}, release has {have}"
                  " -- nothing to do" + (f", app {app}" if app else ""),
                  file=sys.stderr)
            self._manifest = None
            self._write(protocol.ota_none(app=app))
            return
        self._manifest = m
        # Does this release also carry a newer version of THIS program? If so
        # the board says so on the confirmation screen, because the customer is
        # about to approve two installs with one tap and should know it.
        self._app_update = self._app_available(m)
        why = self._app_pairing(m)
        if why:
            # Not offered rather than offered half. A board a release ahead of
            # its app is a mismatch nothing on the panel can fix, and this is
            # the one moment where declining costs only a retry.
            print(f"[bridge] ota: not offering {m.get('version')} -- {why}",
                  file=sys.stderr)
            app = self._app_update[0] if self._app_update else None
            self._ota_reset()
            self._write(protocol.ota_none(app=app))
            return
        app = self._app_update[0] if self._app_update else None
        print(f"[bridge] ota: offering {m['version']} ({m['size']} bytes)"
              + (f", app {app}" if app else ""), file=sys.stderr)
        self._write(protocol.ota_avail(m["version"], m["size"], m["sha256"],
                                       app=app))

    def _app_available(self, manifest):
        """(version, artifact) if this release has a newer daemon for us.

        Deliberately ignores the manifest it is handed. That one came from
        ota.fetch_manifest(), which does NOT check the signature -- and does
        not need to for firmware, because MCUboot will refuse an image that
        was not signed with the release key no matter what a manifest claims.

        A daemon binary has no such backstop. It is about to be run as a login
        service on the customer's machine, and the only thing standing between
        it and an attacker who can answer for the release URL is the manifest
        signature. Taking the version, size and sha256 from an unverified
        manifest would have meant the board-initiated update -- the one a
        customer actually taps -- skipping the check that the whole signing
        arrangement exists to perform, while the daily background check kept
        it. Fetch a signed one instead.
        """
        if self._self_update is None:
            return None
        try:
            from pc import update
            return update.available(self._fetch_signed())
        except Exception:
            return None

    def _app_pairing(self, m):
        """Why this app cannot pair with release `m`, or "" when it can.

        The board can end up a release AHEAD of the app on this computer, and
        then the half that is behind is the half the panel cannot reach: the
        board's update row says the app is old and no tap on it can help.
        1.3.1 made the board tell the truth about that; this is what stops it
        happening by accident.

        Firmware and the daemon ship from one tag and are meant to install
        together -- _on_ota_flash does the app first, precisely so the newest
        firmware is never driven by the oldest app. The pair splits when the
        app half cannot be worked out at all: fetch_signed_manifest returns
        None on a network blip or a signature that does not verify, and
        _app_available answers None either way, which is indistinguishable
        from "this release has no newer app". Offering the firmware anyway
        turns a transient failure into a permanent mismatch.

        A platform with no published daemon build is a different thing and
        gets a different answer -- there the pair CANNOT be kept, by design,
        and refusing firmware forever would strand those machines. They are
        offered it, with a line in the log saying why.
        """
        version = m.get("version", "")
        if not ota_mod.is_newer(version, RELEASE_VERSION):
            return ""           # this app is level with the release or ahead
        if self._self_update is None:
            return ""           # not running as a daemon that can replace itself
        try:
            from pc import update
            if update.platform_key() is None:
                print(f"[bridge] ota: {version} publishes no app for this"
                      " platform; the board will run ahead of the app",
                      file=sys.stderr)
                return ""
        except Exception:
            pass
        if self._app_update is not None:
            return ""           # both halves in hand: _on_ota_flash pairs them
        return (f"release {version} is newer than this app ({RELEASE_VERSION})"
                " but its signed manifest could not be read")

    def _resume_pending(self):
        """Finish an install the user approved before we replaced ourselves.

        Re-runs the ordinary query and flash path rather than trusting anything
        recorded on disk: the version is a note about consent, not about what
        is safe to install, so the protocol floor, the size and the hash are
        all checked again from the live manifest.
        """
        if not self._pending:
            return
        from pc import update
        version, tries = self._pending.read()
        if not version:
            return
        print(f"[bridge] ota: resuming the approved install of {version}",
              file=sys.stderr)
        self._on_ota_query(self._board_fw or "0.0.0")
        if not self._manifest:
            #
            # NOTHING WAS ATTEMPTED, so the consent is not spent.
            #
            # This branch is reached when the query could not produce a
            # manifest at all -- the feed did not answer, or the signature did
            # not verify. It says nothing about the image the user approved,
            # and it is the likeliest moment in the whole update for a
            # transient to land: the daemon has just replaced itself and is
            # asking the network a question within seconds of starting.
            #
            # Spending the approval here is what left a customer's board on
            # 1.2.5 with the app on 1.3.2 (2026-09-10), the daemon answering
            # `ota_none`, and nothing on the panel to say an update had been
            # agreed to and dropped. The board will not re-offer inside the
            # same boot either, so the two faults compound into a board that
            # simply never updates again.
            #
            # RESUME_TRIES bounds it: retry on the next connect, and when the
            # tries are gone say so out loud rather than looping in silence.
            attempt = tries + 1
            if attempt >= update.RESUME_TRIES:
                self._pending.clear()
                print(f"[bridge] ota: could not resume {version} after"
                      f" {attempt} attempts; giving up", file=sys.stderr)
                self._write(protocol.ota_error(
                    "could not read the release feed"))
            else:
                self._pending.bump()
                print(f"[bridge] ota: no manifest yet; {version} stays"
                      f" approved (attempt {attempt} of"
                      f" {update.RESUME_TRIES})", file=sys.stderr)
            self._ota_reset()
            return
        if self._manifest.get("version") != version:
            # A different fault: the feed answered, and what it is serving is
            # not what the user agreed to. Retrying cannot change that, so the
            # approval goes rather than riding along onto an image nobody
            # consented to.
            self._pending.clear()
            print("[bridge] ota: the release moved on; not resuming",
                  file=sys.stderr)
            self._ota_reset()
            return
        # From here an install is genuinely attempted, which is what the
        # approval was for. Spend it BEFORE the flash, so a failure that gets
        # as far as writing does not come back on every reconnect.
        self._pending.clear()
        # Put the board back on its progress screen. It has been sitting on an
        # "Install?" prompt for something it already agreed to.
        self._write(protocol.ota_resume(version))
        self._on_ota_flash()

    def _on_ota_flash(self):
        """The board approved. Fetch the image and hand it to the flasher.

        When the release also carries a newer daemon, that goes FIRST and this
        process is replaced -- the new daemon is the half that knows how to
        drive the new firmware, and installing them the other way round would
        leave the newest firmware being driven by the oldest app.
        """
        if self._manifest and self._app_update and self._self_update:
            version, artifact = self._app_update
            self._app_update = None
            fw_version = self._manifest["version"]
            print(f"[bridge] ota: updating this app to {version} first",
                  file=sys.stderr)
            if self._pending:
                self._pending.set(fw_version)
            if self._self_update(version, artifact):
                return          # unreachable in practice: we exit into the new
                                # binary, which picks the firmware back up
            # It failed and we are still here. The old app can still install
            # the firmware -- the protocol floor was already checked -- so do
            # that rather than leaving the customer with nothing.
            if self._pending:
                self._pending.take()
            print("[bridge] ota: app update failed; installing the firmware"
                  " with the current app", file=sys.stderr)
        if not self._manifest:
            self._write(protocol.ota_error("nothing staged"))
            return
        if self._flash is None:
            self._write(protocol.ota_error("no flasher configured"))
            return
        try:
            blob = self._fetch_firmware()
        except Exception as e:
            print(f"[bridge] ota: download failed: {e}", file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_error("download failed"))
            return
        # Refuse an image that already disagrees with its own manifest rather
        # than writing it to slot0, where there is no auto-revert to catch it.
        if len(blob) != self._manifest["size"]:
            print(f"[bridge] ota: asset is {len(blob)} bytes, manifest says"
                  f" {self._manifest['size']}", file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_error("size mismatch"))
            return
        # And check the hash, which for a while nobody did.
        #
        # The manifest has always carried sha256 and the board has always been
        # sent it, but over USB the board never sees the bytes -- the daemon
        # runs esptool -- so the verification that pc/ota.py's docstring claimed
        # was happening on the board could not have been. Length agreed with the
        # manifest and that was the whole check.
        #
        # It matters more here than it would over the WiFi path: slot0 is
        # written in place, so a bad image is not caught by a test boot and
        # rolled back, it just does not boot.
        digest = hashlib.sha256(blob).hexdigest()
        want = str(self._manifest["sha256"]).strip().lower()
        if digest != want:
            print(f"[bridge] ota: sha256 {digest} != manifest {want}",
                  file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_error("sha256 mismatch"))
            return
        version = self._manifest["version"]
        self._ota_reset()
        print(f"[bridge] ota: flashing {version} ({len(blob)} bytes)",
              file=sys.stderr)
        # Tell the board to remember what it is about to become, now that
        # nothing else can come between this and esptool taking it away.
        self._write(protocol.ota_begin(version))
        self._flash(blob, version)

    def board_alive(self) -> bool:
        return (self._last_ping is not None
                and self._now() - self._last_ping <= LIVENESS_WINDOW_S)

    # --- outbound ---
    #
    # Two cadences, answering two different questions.
    #
    # poll_once is the heartbeat: everything, unconditionally, once every
    # POLL_INTERVAL_S. It is also the only thing that sends `time`, which is
    # what the board re-anchors its clock from and has no reason to happen
    # more often than that.
    #
    # poll_if_changed is the fast tick, and it exists because session state --
    # RUNNING, WAITING, the per-session pips -- is a *now* signal. Claude
    # Code's hook rewrites its state file within a second or two of an event
    # (1.8 s on the live install, measured), and the panel was up to a minute
    # behind it: by the time "a session is waiting on you" reached the glass,
    # the owner had already looked at the screen and turned away.
    #
    # What this does NOT do is make the dials fast, and nothing here should be
    # read as claiming it does. The percentages come from Claude Desktop's own
    # cache, which that app refreshes every 5-15 minutes, so the arcs move no
    # sooner than they ever did. This is about state, not numbers.
    def poll_once(self):
        # Time rides along with every heartbeat so the board's clock re-anchors
        # at the same cadence as the data.
        epoch, off = self._wall()
        self._write(protocol.time_msg(epoch, off))

        try:
            usage = self._read_usage()
        except Exception as e:
            # Not expected: statusline_source swallows its own read errors and
            # returns None. Anything that reaches here is a bug worth putting
            # on the panel rather than only in a log nobody opens.
            self._write(protocol.status("error", str(e)[:80]))
            return

        if usage is None:
            # No statusline payload yet. The board keeps its last values and
            # its own dot, which is right -- but the daemon used to say nothing
            # at all, so a machine where Claude Code has simply never rendered
            # looked identical in the log to one where everything works.
            if not self._said_no_source:
                print("[bridge] no usage data yet -- open Claude Code once so"
                      " it renders its status line", file=sys.stderr)
                self._said_no_source = True
            return
        self._said_no_source = False

        # Unconditional. The heartbeat is what re-states everything to a board
        # that may have missed a message, and what keeps a panel that has been
        # sitting on the same numbers all afternoon provably connected.
        self._send_usage(usage)

    def poll_if_changed(self):
        """Send only when something a reader could act on has moved.

        Called every FAST_POLL_INTERVAL_S, on the same thread that reads the
        board's serial messages -- so whatever looking costs is latency this
        loop pays, not background work.

        Looking is no longer only JSON. The bus also walks Claude Desktop's
        LevelDB stores, and re-parsing those on every poll measured 0.47 s
        against a 769 KB store on the reference machine: a fifth of a core
        continuously, and a serial read loop stalled for a quarter of every
        interval, growing with the store. pc/leveldb now caches each parsed
        file against its (size, mtime), which puts an unchanged store back to
        0.0004 s; a poll costs a stat per file plus a fresh parse of whatever
        Chromium actually rewrote -- ordinarily just the small .log.

        Writing was never free either: the fully-loaded usage line runs to 509
        of the 512 bytes the firmware will accept, and putting one on the wire
        every two seconds when nothing changed is thirty times the traffic
        carrying no news.

        Deliberately quieter than the heartbeat about failure. A fetch that
        raises, or a source that has not appeared yet, will be in the same
        state two seconds from now, so complaining here would turn one message
        a minute into thirty; poll_once already reports both at a cadence a
        person can actually read.
        """
        try:
            usage = self._read_usage()
        except Exception:
            return
        if usage is None:
            return
        if protocol.meaningful_usage(usage) != self._last_pushed:
            self._send_usage(usage)
            return
        # The usage line is not the whole message the panel draws from, and
        # the project name is the part it cannot see. `label` rides its own
        # `session` message -- deliberately, because the usage line has six
        # bytes of headroom and a name is not six bytes -- so when a session
        # in project A ends and one in project B starts running, `state`
        # stays "running", every count stays where it was, and the usage dict
        # is byte-identical. The fast tick found nothing to send and the panel
        # went on naming project A until the next heartbeat, up to a minute
        # later. Naming the wrong project is a wrong statement, not a vague
        # one, and this tick exists precisely so that session moves reach the
        # board in two seconds rather than sixty.
        #
        # Only the name goes out in that case. Re-sending an identical usage
        # line to carry it would spend the budget this comparison exists to
        # protect.
        self._send_session_if_changed()

    def _read_usage(self):
        """The usage message as it would go out right now, or None if there is
        nothing to report yet.

        Raises whatever the fetch raises: the two callers disagree about how
        loud to be, so neither the logging nor the status message belongs here.
        """
        usage = self._fetch()
        if usage is None:
            return None
        # Percentages above 100 are real -- extra usage puts them there -- but
        # firmware older than protocol.FW_ACCEPTS_OVERAGE turns them into 0 on
        # the panel rather than clamping. Hold them at 100 for those boards.
        return protocol.cap_overage_for_fw(usage, self._board_fw)

    def _send_usage(self, usage):
        """Put a usage message on the wire and remember what was in it."""
        self._write(usage)
        # Recorded here, once, so that no path can push without moving the
        # baseline -- a push that forgot to would leave the fast tick
        # re-sending the same message every two seconds forever.
        self._last_pushed = protocol.meaningful_usage(usage)

        self._send_session_if_changed()

        # No second message for staleness any more. The usage message carries
        # `stale` and the firmware reads it (proto.c, via msg_get_bool), so the
        # board colours its own dot from the reading it was just given.
        #
        # What was here sent status "rate_limited" whenever the payload was
        # stale, purely because that string already mapped to amber -- which
        # left a stale reading and a real rate limit indistinguishable on the
        # panel, and put the wrong words in the log.

    def _send_session_if_changed(self):
        """Put the project name on the wire, on change only.

        Its own message because the usage line has six bytes of headroom and
        a project name is not six bytes. Split out of _send_usage so the fast
        tick can send a name WITHOUT re-sending an unchanged usage line --
        see poll_if_changed, where a project change is otherwise invisible.

        Read off the fetch callable rather than passed through it: the Bridge
        is handed a zero-arg callable returning a finished usage dict and
        never sees the frame the name lives on. A fetch without the accessor
        -- every test fake, and the single-source fetch -- simply sends
        nothing, which is what an older daemon did anyway.
        """
        session_pair = getattr(self._fetch, "session_pair", None)
        if session_pair is None:
            return
        pair = tuple(session_pair())
        if pair != self._last_session:
            self._write(protocol.session(*pair))
            self._last_session = pair
