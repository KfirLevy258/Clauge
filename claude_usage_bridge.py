#!/usr/bin/env python3
"""Serial daemon: bridge Claude usage to an ESP32 over USB-C.

Run inside the Zephyr venv (has pyserial) or `pip install pyserial`:
    python3 claude_usage_bridge.py --port /dev/cu.usbmodemXXXX
"""
import argparse
import json
import math
import os
import sys
import time

import serial  # pyserial
from serial.tools import list_ports

from pc import ota as ota_mod
from pc import (ingest, install_statusline, protocol, statusline_source,
                update, win_driver)
from pc.version import RELEASE_VERSION
from pc.bridge import Bridge

POLL_INTERVAL_S = 60
# How often to LOOK at the local state files. Whether anything is SENT is a
# separate decision, made in Bridge.poll_if_changed.
#
# Two seconds, and the reason it is affordable is that nothing here leaves the
# machine: every provider reads a handful of small JSON files off the local
# disk, there is no usage endpoint any more and therefore no rate limit to
# respect. The 60 s above was inherited from the board's standalone WIFI mode,
# where the fetch really does go over the air and really is throttled -- in USB
# bridge mode that reason simply does not apply, and it cost the panel up to a
# minute of lag on the one thing it is looked at for: whether a session is
# waiting on somebody.
#
# Not 0.2 s. Nobody glancing at a desk display can tell 200 ms from 2 s, and
# the difference is a stat() storm five times a second for as long as the
# machine is on. Two seconds is the scale of the signal: it matches how fast
# Claude Code's hook writes its state file in the first place.
FAST_POLL_INTERVAL_S = 2
PING_GRACE_LOG_S = 30
# How often to ask whether a newer version of THIS program exists.
#
# The first check waits for a board rather than firing at startup: a daemon
# that crashes on launch would otherwise hammer the release feed once every ten
# seconds forever, and a machine with no board attached has nothing to update
# for anyway.
UPDATE_FIRST_CHECK_S = 60
UPDATE_INTERVAL_S = 24 * 3600


# The USB-serial bridge chips these boards actually ship with, by VID:PID.
#
# Identify by ID, not by name: on macOS the CYD's CH340 reports manufacturer
# None and a device node of /dev/cu.usbserial-14140 -- neither "usbmodem" nor
# "espressif", the two things the old heuristic looked for. So autodetect never
# once matched the hardware this product runs on. It went unnoticed because
# tools/dev.sh always passes --port explicitly; nothing but a customer plugging
# in a board would have hit it.
KNOWN_USB_SERIAL = {
    (0x1A86, 0x7523),   # CH340/CH341 -- the common CYD
    (0x1A86, 0x7522),
    (0x1A86, 0x5523),
    (0x10C4, 0xEA60),   # CP2102/CP2104 -- the other CYD variant
    (0x0403, 0x6001),   # FT232R
}
ESPRESSIF_VID = 0x303A  # native USB-serial on -S2/-S3 parts


CHIP_NAMES = {
    0x1A86: "CH340",
    0x10C4: "CP210x",
    0x0403: "FTDI",
    ESPRESSIF_VID: "Espressif USB",
}


class Tap:
    """BLINK_TAP: a transcript of everything crossing the serial link.

    The fleet suite runs this daemon against a real board and then has to
    prove what happened. Nothing else can: the daemon's own stderr log is
    prose meant for a person, and the board cannot be interrogated after the
    fact. So when BLINK_TAP names a file, every message and every line the
    board printed is appended to it as JSON, one object per line, and the
    test asserts against that.

    Three streams, because two are not enough. `tx` and `rx` prove what the
    host sent and what the board answered -- but the board answers only
    hello/ping/pref/ota_*; there is no per-frame ack, so rx alone can never
    show that a usage frame was applied. What can show it is the board's own
    console: proto.c prints the usage it took ("[usage] session 50% (12s)
    ...") on the same wire. That line is the end-to-end evidence, so
    `console` records it.

    Appended and reopened per write so a test tailing the file sees whole
    lines, and so a daemon that dies mid-run leaves everything it had.

    Note the deliberate collision of two different `t`s: the tap's own field
    is the epoch timestamp of the record, while the protocol keys a message's
    type as "t" inside `msg`. They never meet -- one is the envelope, the
    other the payload.
    """

    def __init__(self, path):
        self._path = path
        # Serial hands over whatever has arrived, which routinely cuts a
        # printk in half. Hold the tail until its newline turns up.
        self._pending = b""
        self._complained = False

    def _append(self, record):
        """Append one record, and never let a bad tap path stop the daemon.

        An unwritable or full BLINK_TAP path raises OSError as the file is
        opened, and console() runs in the read loop -- whose except treats it
        as a disconnected board. Left to propagate, a broken tap would present
        as hardware that keeps dropping off the bus, which is the most
        expensive possible way to report a wrong filename. So it is caught and
        named as what it is.

        Reported once, then silently skipped, following pc/ingest's rule for a
        source that has already failed: the board pings every ten seconds and
        the loop reads continuously, so one line per failed write would bury
        the daemon's real log within minutes.
        """
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            if not self._complained:
                self._complained = True
                print(f"[tap] cannot write {self._path}: {e}."
                      f" The transcript is incomplete.", file=sys.stderr)

    def tx(self, msg, sent):
        """Record an outbound message, and whether it reached the wire.

        `sent` is not decoration. send() refuses any line over the board's
        512-byte limit -- and a fully loaded two-provider frame already
        measures 484 -- so the refusal happens in exactly the boundary cases
        this suite exists to catch. A transcript that recorded the refusal as
        a plain tx would say the host sent a frame the board ignored, and the
        hunt would start in the firmware for a bug that is in the host.

        Required rather than defaulted so no future caller can record a tx
        without having decided the question. Later tasks count sent frames, so
        the key is on every tx record, not only the failures.
        """
        self._append({"dir": "tx", "t": time.time(), "msg": msg,
                      "sent": bool(sent)})

    def rx(self, msg):
        self._append({"dir": "rx", "t": time.time(), "msg": msg})

    def console(self, data):
        """Record raw board output, whole lines only.

        Decoded with errors="replace": board output is not guaranteed clean
        UTF-8 -- a reset mid-line emits noise -- and this is called from the
        daemon's read loop, where raising would take the link down over a
        test facility.

        The board's JSON arrives on this same wire, so protocol lines appear
        here too. That is the point of a raw console tap; a test that wants
        only messages reads the rx records instead.
        """
        self._pending += data
        *whole, self._pending = self._pending.split(b"\n")
        for line in whole:
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            self._append({"dir": "console", "t": time.time(), "line": text})

    def wrap(self, write_msg, on_message):
        """(write_msg, on_message) that record, then delegate.

        Wrappers rather than edits at the call sites: the daemon's send() and
        its inbound dispatch keep their exact behaviour, and the whole
        facility stays removable.

        The tx record is written after delegating, not before, because only
        the delegate knows whether the message actually went out -- and from
        a finally, so a delegate that raises (a board unplugged mid-write)
        still leaves a not-sent record. That is the moment the transcript is
        most worth reading, so it is the last one that should be missing.
        """
        def write2(m):
            ok = False
            try:
                ok = write_msg(m)
                return ok
            finally:
                self.tx(m, sent=ok)

        def rx2(m):
            self.rx(m)
            return on_message(m)

        return write2, rx2


def open_tap():
    """The Tap for this connection, or None when BLINK_TAP is unset.

    Separate from install_tap() because the transcript has to start earlier
    than the read loop does. The port is opened, then probed, and the probe
    reads and discards whatever the board said -- which is precisely when the
    board says what it is and what state it was in. One connection, one Tap,
    created before the first read.
    """
    path = os.environ.get("BLINK_TAP")
    return Tap(path) if path else None


def install_tap(write_msg, on_message, tap=None):
    """(tap, write_msg, on_message) for this connection.

    Unset BLINK_TAP hands back the two callables it was given, unchanged and
    unwrapped, and no file is opened. A daemon that behaves differently
    because a test facility exists is a defect, so the inert path costs one
    environment lookup and nothing else.

    A caller that already has this connection's Tap passes it in, and gets it
    back rather than a second one. That matters beyond tidiness: the Tap
    holds the tail of a console line until its newline arrives, and the
    probe's last read routinely ends mid-print. A fresh Tap here would drop
    that tail on the floor and the line would arrive in the transcript with
    its beginning missing.
    """
    tap = tap or open_tap()
    if tap is None:
        return None, write_msg, on_message
    write2, rx2 = tap.wrap(write_msg, on_message)
    return tap, write2, rx2


def poll_interval():
    """Seconds between usage polls: the shipped 60, unless a test says less.

    A fleet scenario is a timeline -- the percentage climbs, then goes past
    100 -- and the daemon emits one usage message per poll. At a poll a
    minute a thirty-second scenario produces exactly one frame, so the
    sequence the test exists to observe never reaches the board at all. The
    suite sets BLINK_POLL_INTERVAL_S to about 3 and gets its timeline.

    Worth knowing before shortening it: poll_once() writes a `time` message
    on EVERY poll, before and independently of the usage message, so this
    also multiplies `time` traffic to the board. Anything counting frames in
    the tap must count records whose msg["t"] == "usage" rather than counting
    tx records.

    Malformed or non-positive is refused rather than obeyed: 0 or a negative
    makes the poll gate true on every turn of the read loop, which would
    hammer a rate-limited usage endpoint as fast as the loop spins. It falls
    back to the default and says so -- once, because main() resolves this at
    startup and never asks again.

    Non-finite is refused for the opposite reason, and is the quieter hazard
    of the two. "nan", "inf" and "1e400" all parse without raising, and a
    positivity check alone lets them through -- every comparison against nan
    is False, so `nan <= 0` is False. Either one poisons
    `next_poll = monotonic() + interval`, after which `monotonic() >=
    next_poll` is False for the rest of the run: polling stops dead, the
    board never receives usage again, and nothing anywhere says why. Hence
    isfinite FIRST, before any comparison that nan would win by default.
    """
    raw = os.environ.get("BLINK_POLL_INTERVAL_S")
    if raw is None:
        return POLL_INTERVAL_S
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0
    if not math.isfinite(seconds) or seconds <= 0:
        # "usable" rather than "positive": inf IS positive, and telling
        # someone who typed it that it is not would send them looking in the
        # wrong direction.
        print(f"[bridge] BLINK_POLL_INTERVAL_S={raw!r} is not a usable"
              f" number of seconds; polling every {POLL_INTERVAL_S} s.",
              file=sys.stderr)
        return POLL_INTERVAL_S
    return seconds


def build_bus():
    """The daemon's usage source: scripted when BLINK_SCENARIO says so.

    A fleet test has to reproduce the same invented usage history -- "past
    100%", "four hours stale" -- on three machines, which no real provider
    can do, since a real provider reports whatever that machine's tools
    happened to write. Pointing BLINK_SCENARIO at a scenario file replaces
    the whole provider set with the one that replays it.

    It replaces rather than joins the set on purpose: a real Claude install
    on the test machine would otherwise merge its own readings into the
    scenario and the assertion would depend on whose desk it ran on.
    """
    scenario = os.environ.get("BLINK_SCENARIO")
    if not scenario:
        return ingest.IngestionBus()
    from pc.providers.scripted import ScriptedProvider
    return ingest.IngestionBus(providers=[ScriptedProvider(scenario)])


def describe_ports():
    """[(device, chip)] for every candidate port, in candidate_ports() order.

    For `blink status`: a person with three CH340 boards on the desk needs to
    see what the app sees, and which of them it is talking to.
    """
    chips = {}
    for p in list_ports.comports():
        chips[p.device] = CHIP_NAMES.get(p.vid, "USB serial")
    return [(d, chips.get(d, "USB serial")) for d in candidate_ports()]


def may_reset_port(explicit, port, known_port, patient):
    """May this silent port be reset? A reset is an action taken on a device.

    An explicit --port is the operator's choice, so yes. Otherwise only the
    port the board was last found on, and only after the PATIENT probe has
    also heard nothing. It used to be enough that the name matched, and a
    port name on a Mac is a socket, not a board: the board plugged back into
    the same socket -- or a fresh one, just burned -- was still booting when
    the 1.5 s probe ran, got no answer, and was reset for it (the "boots
    twice on plug-in" the user kept seeing; last on 2026-08-30). Eleven
    seconds of listening hears any live board; a board silent that long is
    wedged, and a reset is the recovery it needs.
    """
    if explicit:
        return True
    return port == known_port and patient


def candidate_ports():
    """Every port that might be a board, best guess first.

    A LIST, not a single answer, and that is the point. This used to return
    the first VID:PID match and the caller committed to it -- but 1a86:7523 is
    the CH340, which is in Arduino clones, USB-serial adapters and a great deal
    of other hobbyist hardware. On a desk with any of it attached, the daemon
    could pick a stranger's device, fail to talk to it, and never look at the
    real board sitting on the next port.

    With more than one BLINK attached the first that answers wins and the rest
    are ignored: one daemon drives one board. That is a real limitation rather
    than an oversight -- the protocol, the preference and the OTA path are all
    written around a single unit -- but it is at least now a defined one, and
    `--port` picks a specific board deterministically.
    """
    out = []
    for p in list_ports.comports():
        if (p.vid, p.pid) in KNOWN_USB_SERIAL or p.vid == ESPRESSIF_VID:
            out.append(p.device)
    # Name heuristics kept as a fallback for a variant carrying a chip that is
    # not in the table yet. Bluetooth ports have no VID and no matching name,
    # so they cannot be picked up by either pass.
    for p in list_ports.comports():
        if p.device in out:
            continue
        if "usbmodem" in p.device or (p.manufacturer or "").lower().startswith("espressif"):
            out.append(p.device)
    return out


def autodetect_port():
    """The first candidate, for callers that only want one."""
    ports = candidate_ports()
    return ports[0] if ports else None


# How long to wait for a device to identify itself before resetting it.
#
# The board answers `welcome` with its `pref` (proto.c), immediately, so this
# only has to cover the round trip. It did not always: the reply this relied
# on was the once-per-boot ota_query, so from the second connection of a boot
# onwards the board was silent here and got reset anyway.
PROBE_S = 1.5

# ...and how long to listen on the SECOND pass, when nothing answered quickly.
#
# Longer than the board's 10 s ping interval on purpose. The fast probe relies
# on the board ANSWERING our welcome, and there is one window where it cannot:
# a board plugged in a moment ago is booting, so our message arrives before
# proto is listening and its own hello is flushed by the buffer reset. It says
# nothing, looks like a stranger, and would be written off.
#
# A patient pass needs no answer at all -- it just waits for the next ping,
# which a healthy board sends unprompted. Measured on hardware: 0.51 s from
# reset to the first protocol message, and a ping every 10 s after that, so 11
# seconds cannot miss a live board.
PROBE_PATIENT_S = 11.0


def hold_single_instance(home, on_wait=None, poll_s=5.0):
    """Block until this process is the only daemon, then keep the lock.

    Two daemons on one board is not a hypothetical. On macOS a /dev/cu.* node
    is NOT exclusive -- two processes open it happily, and both then write
    usage messages and answer the board's pings on the same wire. Observed
    doing exactly that on 2026-08-28: a second daemon connected to a board the
    first was already driving, with no error from either. The board sees
    interleaved traffic and the user sees a panel that flickers between two
    sources with nothing in either log to explain it.

    Linux and Windows do make the port exclusive, so there the second process
    fails to open it and the message in the reconnect loop covers the case.
    This is for the platform where the OS will not say no.

    Waiting rather than exiting, for the same reason wait_for_port waits: the
    service is registered with KeepAlive/Restart=always, so exiting turns a
    duplicate launch into a process start and a log line every ten seconds for
    as long as both exist.

    Returns the open file object, which must stay referenced -- closing it
    releases the lock. POSIX only; on Windows the exclusive port is the lock.
    """
    try:
        import fcntl
    except ImportError:
        return None                      # Windows: the port itself is exclusive

    path = os.path.join(home, "bridge.lock")
    try:
        os.makedirs(home, exist_ok=True)
        fh = open(path, "w", encoding="utf-8")
    except OSError:
        # An unwritable home is not a reason to refuse to run; it only means
        # this protection is unavailable.
        return None

    said = False
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if said:
                print("[bridge] the other daemon exited; taking over",
                      file=sys.stderr)
            return fh
        except OSError:
            if not said:
                print("[bridge] another Blink daemon is already running and"
                      " has the board; waiting for it to exit. Two of them on"
                      " one cable would interleave on the wire.",
                      file=sys.stderr)
                said = True
            if on_wait:
                on_wait()
            time.sleep(poll_s)


def usb_topology():
    """A hashable snapshot of every serial device attached right now.

    The point is to answer "has anything changed?" without opening anything.
    Nothing plugged or unplugged means there is nothing new to find, so there
    is no reason to reopen a port that already declined to answer -- which is
    what turns "ask before resetting" from a good idea into a loop that pokes
    somebody's Arduino every three seconds forever.
    """
    return tuple(sorted((p.device, p.vid, p.pid, p.serial_number or "")
                        for p in list_ports.comports()))


def _known_path(home):
    return os.path.join(home, "board.json")


def remembered_board(home):
    """{"port": ..., "board_id": ...} from the last successful connection."""
    try:
        with open(_known_path(home), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def remember_board(home, port, board_id, fw=None):
    """Write down which port worked, so the next start goes straight to it.

    This is what makes the polite-probe cheap in the case that matters: a
    machine that has connected before does not scan at all, it opens the port
    it used last. It is also what licenses a RESET -- see the connect loop:
    a device we have identified as ours before may be reset when it goes
    quiet, and one we have never identified may not, ever.
    """
    try:
        os.makedirs(home, exist_ok=True)
        tmp = _known_path(home) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": port, "board_id": board_id, "fw": fw}, f)
        os.replace(tmp, _known_path(home))
    except OSError:
        pass                    # a convenience, never a requirement


def learn_board(known, port, msg):
    """What board.json should say after `msg` arrived on `port`.

    Only a `hello` carries the board's id and firmware, and after an app
    restart the first thing off the wire is a `pref` or an `ota_query`, so
    a record taken from the first message alone stayed at null for both --
    every machine checked on 2026-08-30 had nulls there. Take what each
    message offers and keep what it does not.

    `ota_query` spells the firmware `cur`, not `fw`, and that is the whole
    reason this record went stale rather than merely empty. A board only says
    `hello` when it boots, so after a USB flash -- where the board is already
    running by the time the daemon reconnects -- the first and every
    subsequent message is an `ota_query`, and `msg.get("fw")` was None every
    time. The old version stayed in board.json and `blink status` reported it
    for as long as the daemon ran: on 2026-08-31 it insisted a board was on
    1.2.4 while that board was answering `cur: 1.2.5` in the same log. Worse,
    it lied in exactly the moment someone reads it -- checking whether an
    update landed.

    bridge.py has always read `cur` for its in-memory _board_fw, which is why
    the overage cap gates correctly; only the persisted record missed it.
    """
    return {"port": port,
            "board_id": msg.get("board_id") or known.get("board_id"),
            "fw": msg.get("fw") or msg.get("cur") or known.get("fw")}


def probe_is_our_board(ser, timeout=PROBE_S, tap=None):
    """Ask the thing on this port whether it is a Blink board, without a reset.

    Why this exists: the reset below is not free, and it is not aimed at a
    board we have identified -- it is aimed at whatever matched a VID:PID.
    1a86:7523 is the CH340, which is in Arduino clones, USB-serial adapters and
    a great deal of other hobbyist hardware. On a desk with any of it plugged
    in, the daemon could open a stranger's device and pulse its reset line.

    A `welcome` is the safe question. It is 60 bytes of JSON that a device
    which is not ours will ignore (it is text, and it toggles no control line),
    and one that IS ours answers within milliseconds. Answer -> skip the reset
    entirely; no answer -> fall through to the reset, which a mute or wedged
    board genuinely needs.

    A pleasant side effect: our own board stops being rebooted every time the
    daemon restarts, which it was, on every login and every service restart.

    `tap` is the connection's transcript, when one is being kept, and this is
    the only reason it is a parameter. What the board says here would
    otherwise be read and thrown away: ser.read(n) does not return early on
    partial data, so a single 0.2 s read comes back holding everything said
    in that window -- the connect print, the pref reply, and anything the
    welcome itself provoked. The one that provoked it matters most: a
    sleeping board wakes on this welcome and prints that it is waking, so the
    only evidence that it ever slept was being consumed by the question that
    woke it. Unset BLINK_TAP means tap is None and this path is exactly what
    it always was.
    """
    try:
        ser.reset_input_buffer()
        ser.write(protocol.encode(protocol.welcome("blink-bridge",
                                                   RELEASE_VERSION)))
        deadline = time.time() + timeout
        reader = protocol.LineReader()
        while time.time() < deadline:
            chunk = ser.read(256)
            if not chunk:
                continue
            if tap:
                # Before the identification below returns, not after: the
                # chunk that identifies the board is usually the one carrying
                # everything else it had to say.
                tap.console(chunk)
            for msg in reader.feed(chunk):
                # Any well-formed message of ours will do. The board sends
                # ota_query on welcome and pings on its own schedule; which one
                # arrives first is timing, and identity is the only question
                # being asked here.
                if isinstance(msg, dict) and msg.get("t"):
                    return True
    except Exception:
        # A probe that fails is not evidence of anything; fall through and let
        # the usual path decide.
        return False
    return False


def wait_for_port(explicit=None, poll_s=3.0, on_wait=None):
    """Block until there is a board to talk to, then return its device path.

    `on_wait` runs once per poll while we wait. Host-side upkeep that has
    nothing to do with the cable belongs here: a machine sitting with its
    board unplugged is exactly where this function spends its time, and until
    the callback existed the daemon did nothing at all in that state -- so a
    statusLine hook wiped while the board was out stayed wiped until somebody
    plugged it back in.

    Waiting rather than exiting is what keeps the installed service honest
    about "plug it in and it works". install.sh registers this daemon with
    launchd (KeepAlive) or systemd (Restart=always), which bring it back after
    every exit -- so exiting when no board is attached turns "the cable is
    unplugged" into a process launch and a log line every 10 seconds, all day,
    growing bridge.log without bound. One sleeping process is the cheaper
    answer, and the user never had to do anything for it.

    Re-detecting on each call also survives the board returning on a different
    device node, which a port resolved once at startup would not.
    """
    announced = False
    while True:
        port = explicit or autodetect_port()
        # os.path.exists() only means anything on POSIX, where a serial port
        # IS a filesystem node. Windows ports are DOS device names -- COM3 --
        # and os.stat on one fails, so this test was false for every Windows
        # machine with a board plugged in and the daemon there waited forever
        # for hardware it had already found.
        if port and (sys.platform == "win32" or os.path.exists(port)):
            if announced:
                print(f"[bridge] board found at {port}", file=sys.stderr)
            return port
        if not announced:
            # Once, not per attempt: this is the steady state on a machine
            # whose board is simply unplugged, and it is not an error.
            print(f"[bridge] waiting for the board ({explicit or 'USB'})...",
                  file=sys.stderr)
            # On Windows it may not be unplugged at all. A CH340 with no
            # driver is on no serial port, so the wait above is what a
            # customer's log showed for hours while the board sat in front of
            # them. Named here because the log is the first thing a support
            # conversation asks for.
            undriven = win_driver.undriven_boards()
            if undriven:
                print(f"[bridge] {win_driver.summary(undriven)}",
                      file=sys.stderr)
                for line in win_driver.advice(undriven):
                    print(f"[bridge] {line}", file=sys.stderr)
            announced = True
        if on_wait is not None:
            try:
                on_wait()
            except Exception as e:
                # Upkeep must never strand the wait. Failing here would leave
                # a plugged-in board undetected, which is a far worse outcome
                # than whatever the callback was trying to do.
                print(f"[bridge] upkeep failed while waiting: {e}",
                      file=sys.stderr)
        time.sleep(poll_s)


def _self_update_tick(target):
    """Check the signed feed and, if the release says so, replace ourselves.

    `auto` in the manifest is a switch we hold, not the customer: it ships
    false, and turning it on is a decision made per release. Without a remote
    off switch, a bad build would keep installing itself on every machine that
    checked, and nothing here could stop it.
    """
    home = os.path.dirname(os.path.dirname(target))   # ~/.blink
    manifest = update.fetch_signed_manifest()
    found = update.available(manifest)
    if not found:
        return
    version, artifact = found
    if not ((manifest.get("daemon") or {}).get("auto")):
        print(f"[update] {version} is available; run `blink update` to install"
              " it", file=sys.stderr)
        return
    if not update.auto_update_allowed(home):
        print(f"[update] {version} is available; automatic updates are turned"
              " off on this machine", file=sys.stderr)
        return
    print(f"[update] installing {version}", file=sys.stderr)
    try:
        blob = update.download(update.platform_key(), artifact)
    except Exception as e:
        print(f"[update] download failed: {e}", file=sys.stderr)
        return
    ok, message = update.apply(blob, target, version)
    print(f"[update] {message}", file=sys.stderr)
    if ok:
        update.restart_from_daemon(target)


def main(argv=None):
    """argv is passed explicitly by the `blink run` subcommand.

    Without it this parsed sys.argv[1:], which inside the packaged binary is
    ["run"] -- the subcommand name itself. argparse rejected it, the process
    exited immediately, and the login service restarted it every ten seconds
    forever. It never ran once.
    """
    ap = argparse.ArgumentParser(prog="blink run")
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args(argv)

    # Before anything else: if a previous update died between renaming the old
    # binary aside and moving the new one in, the login service is pointing at
    # a path that does not exist -- and would go on doing so at every boot,
    # silently. This is the one moment that can notice.
    from pc.cli import (_self_path, blink_home as _blink_home,
                        hook_shim_path, installed_bin, settings_path,
                        shim_path)
    self_bin = installed_bin()
    blink_home = _blink_home()
    update.recover(self_bin)

    # Outside the reconnect loop, unlike next_poll. A board that comes and goes
    # -- a nudged cable, a laptop waking -- would otherwise re-arm the 60 s
    # first check on every reconnect, turning a daily update check into one per
    # reconnection.
    next_update = time.monotonic() + UPDATE_FIRST_CHECK_S
    report_failure = None       # a flash failure waiting for the board to return

    # Outside the reconnect loop for the same reason next_update is: its
    # interval and its give-up counter describe this machine, not this cable.
    #
    # statusLine is a single slot in a file shared with the user and with
    # Claude Code's own updates, and anything that rewrites settings.json can
    # drop our command silently. The symptom is not an error -- it is a panel
    # that stops updating while this program reports success, and the desktop
    # cache hides it further by going on feeding numbers for hours. See
    # install_statusline.drift_check for the one rule that matters: a missing
    # marker means the user uninstalled, and that is never overridden.
    #
    # The hook shim fails the other way round: its entry in settings.json
    # stays perfect while its contents fall behind, because `blink update`
    # swaps the program directory and has never rewritten a shim. Both shims
    # are listed rather than just the hook, so the statusline shim gets the
    # same protection it turned out never to have had either.
    watchdog = install_statusline.DriftWatchdog(
        settings_path(), shim_path(),
        shims=((shim_path(), "blink-statusline.sh"),
               (hook_shim_path(), "blink-hook.sh")))

    # Before the port is touched. The lock lives for the life of the process --
    # the file object is bound here so it is not garbage collected, which would
    # release it.
    _instance_lock = hold_single_instance(   # noqa: F841 -- held, not used
        blink_home, on_wait=lambda: watchdog.tick())

    # Record the pid so uninstall can stop US specifically. Ending the login
    # service is not the same as ending this program, and killing by image name
    # is how the uninstaller once killed itself; a pid is unambiguous.
    #
    # Written NEXT TO THE BINARY rather than under ~/.blink, because those are
    # not always the same place. A login service runs in the user's own
    # environment, not in whatever environment registered it -- so under the CI
    # harness, which redirects HOME to a temporary directory, the daemon
    # resolved ~ to the real profile and left its pid somewhere uninstall was
    # never going to look. Deriving it from sys.executable ties it to the
    # directory that actually has to be deleted, which is the thing the pid is
    # for.
    # ~/.blink/bridge.pid, beside the program's directory rather than in it:
    # an update rotates that directory to bin.old with this daemon still
    # inside, and the pid has to stay where cli.restart_service() looks.
    # Frozen: the executable is ~/.blink/bin/blink, two levels down. From a
    # checkout: the ordinary ~/.blink (a developer, not a login service).
    pid_file = (os.path.join(os.path.dirname(os.path.dirname(_self_path())),
                             "bridge.pid") if getattr(sys, "frozen", False)
                else os.path.join(blink_home, "bridge.pid"))
    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        print(f"[bridge] could not record the pid: {e}", file=sys.stderr)

    # Host-side upkeep that runs whether or not a board is attached. Passed
    # into wait_for_port below so an unplugged machine still repairs a wiped
    # hook instead of sitting idle until the cable comes back.
    def _upkeep():
        drifted = watchdog.tick()
        if drifted:
            print(f"[watchdog] {drifted}", file=sys.stderr)

    # Every provider, every source, behind one callable.
    #
    # Claude Code owns the credential and computes these numbers; we read
    # files it and the desktop app have already written. Nothing here
    # authenticates to Anthropic, and the daemon deliberately does not know
    # which providers exist -- pc/ingest owns that, so onboarding a second
    # tool never reaches this loop.
    bus = build_bus()
    # bus.fetch(), never bus.poll: the fetch has to carry the project name
    # beside the numbers (Bridge.poll_once reads session_pair off it), and a
    # bound method proxies attribute reads to the plain function underneath,
    # so `fetch = bus.poll` left every real desk unnamed while the tests --
    # which built their own fetch -- stayed green.
    fetch = bus.fetch()
    # Resolved once, here, rather than read inside the loop: the interval is
    # a property of this run, and the read loop turns often enough that an
    # environment lookup per pass would be a strange place to spend time.
    poll_every = poll_interval()

    last_err = None
    explicit_port = bool(args.port)

    # What we learned last time. A machine that has connected before opens the
    # port it used, with no scanning at all.
    known = remembered_board(blink_home)
    # The USB layout we have already searched without finding anything. While
    # it is unchanged there is nothing new to look at, so we wait instead of
    # reopening ports on a timer.
    searched = None

    def next_port():
        """The port to try, or None if there is nothing new to try.

        Order matters and is the whole design: the remembered board first
        (usually the only candidate, and the only one we may reset), then
        anything else attached, each asked once per USB layout.
        """
        nonlocal searched, patient
        if explicit_port:
            return args.port
        cands = candidate_ports()
        want = known.get("port")
        if want in cands:
            cands = [want] + [c for c in cands if c != want]
        here = usb_topology()
        if searched == here:
            return None
        if searched is not None:
            # Something was plugged or unplugged. Start over from the quick
            # question rather than inheriting the previous round's patience.
            searched = None
            patient = False
        if not cands:
            searched = here
            return None
        return cands[0]

    port = wait_for_port(args.port, on_wait=_upkeep)
    # Ports asked politely under the CURRENT layout. Not a permanent blacklist:
    # it is discarded the moment anything is plugged or unplugged.
    asked = set()
    # False on the fast pass (answer my welcome), True on the patient one
    # (just say anything). See PROBE_PATIENT_S.
    patient = False

    while True:  # reconnect loop
        try:
            # A PLAIN open, with DTR and RTS left exactly as the OS sets them.
            #
            # On the CYD, DTR drives GPIO0 and RTS drives EN through the CH340's
            # auto-reset circuit: EN is pulled low only while RTS is asserted
            # AND DTR is not. The OS asserts both on open, which is harmless --
            # both transistors are off. This code used to pre-set dtr=False and
            # rts=False "so the open would not toggle anything", and that was
            # the toggle: pyserial applies them after the open, DTR first, and
            # for the instant between clearing DTR and clearing RTS the board
            # is in the reset condition. Every open rebooted the board, and
            # the probe below then heard the boot-time hello of the board it
            # had just reset and reported "answered; not resetting it".
            #
            # Measured on the desk board 2026-08-28: configure-then-open
            # rebooted it 3 of 3 times; a plain open 0 of 5, and closing the
            # port (which drops both lines) 0 of 4. The symptom it caused was
            # the one the user kept reporting -- plug the board in, it starts
            # to boot, resets a couple of seconds later, then boots for real.
            #
            # The explicit pulse further down still reboots a silent board on
            # purpose, and clears DTR first so GPIO0 is high when EN releases
            # (run mode, not the ROM loader).
            ser = serial.Serial(port, args.baud, timeout=0.2)
            # The transcript starts at the port, not at the read loop. Built
            # here, per connection, and handed to install_tap further down so
            # a console line torn between the probe and the loop is still one
            # line. None unless BLINK_TAP is set, which is every customer.
            tap = open_tap()
            # Ask before pulling the reset line. See probe_is_our_board: the
            # VID:PID that got us here belongs to a chip used by a great deal
            # of hardware that is not ours, and a reset is not a question, it
            # is an action taken on someone's device.
            already_running = probe_is_our_board(
                ser, PROBE_PATIENT_S if patient else PROBE_S, tap)
            asked.add(port)

            # May this port be reset if it stays silent?
            #
            # Only if we have identified it as ours before, or the operator
            # named it. A reset is an action taken on a device, and an
            # unidentified CH340 is far more likely to be an Arduino than a
            # wedged board -- 1a86:7523 is in a great deal of hardware that is
            # not ours. A board that has connected here before is a different
            # matter: it IS ours, silence means it is wedged, and a reset is
            # exactly the recovery it needs.
            may_reset = may_reset_port(explicit_port, port, known.get("port"),
                                       patient)

            if already_running:
                print(f"[bridge] {port} answered; not resetting it",
                      file=sys.stderr)
            elif may_reset:
                ser.dtr = False      # GPIO0 HIGH -> boot the app, not the ROM loader
                ser.rts = True       # EN LOW  -> hold in reset
                time.sleep(0.15)
                ser.rts = False      # EN HIGH -> release; board boots
                time.sleep(0.3)
                ser.reset_input_buffer()
            else:
                # Silent, and not ours as far as we know. Leave it alone and
                # look elsewhere -- and if there is nowhere else under this USB
                # layout, stop looking until something is plugged or unplugged.
                ser.close()
                nxt = next((p for p in candidate_ports() if p not in asked), None)
                if nxt is not None:
                    print(f"[bridge] {port} did not answer; trying {nxt}",
                          file=sys.stderr)
                    port = nxt
                    continue
                if not patient:
                    # Nothing answered quickly. Before writing this layout off,
                    # go round once more listening long enough to hear an
                    # unprompted ping -- which is the only thing a board that
                    # was still booting during the fast pass will send.
                    patient = True
                    asked.clear()
                    port = next_port() or port
                    print("[bridge] nothing answered quickly; listening"
                          " longer before giving up", file=sys.stderr)
                    continue
                searched = usb_topology()
                asked.clear()
                patient = False
                print("[bridge] nothing here answers; waiting for a board to"
                      " be plugged in", file=sys.stderr)
                while True:
                    _upkeep()
                    time.sleep(3.0)
                    nxt = next_port()
                    if nxt is not None:
                        break
                port = nxt
                continue
        except Exception as e:
            # Deduplicated: a board left unplugged, or a port the user lacks
            # permission to open, would otherwise write this same line to
            # bridge.log every three seconds for as long as the service runs.
            # Name the two failures a person can actually act on. Everything
            # else stays a bare message, but "busy" and "permission" were both
            # being reported as an unexplained open failure and then repeated
            # forever, which is how "the panel does nothing" became a support
            # question instead of a one-line fix.
            text = str(e).lower()
            if "busy" in text or "resource temporarily unavailable" in text:
                err = (f"{port} is busy -- something else has the board open"
                       " (a serial monitor, esptool, the Arduino IDE, or a"
                       " second copy of this daemon). Close it and this"
                       " reconnects on its own")
            elif "permission" in text or "access is denied" in text:
                err = (f"cannot open {port}: permission denied."
                       " On Linux the port is usually group `dialout` --"
                       " `sudo usermod -aG dialout $USER`, then log out and"
                       " back in")
            else:
                err = f"open {port} failed: {e}"
            if err != last_err:
                print(f"[bridge] {err}", file=sys.stderr)
                last_err = err
            time.sleep(3)
            port = wait_for_port(args.port, on_wait=_upkeep)
            continue
        # Cleared on success so a later, genuine failure is reported again
        # rather than silenced by having happened once before.
        last_err = None
        print(f"[bridge] connected on {port}", file=sys.stderr)
        reader = protocol.LineReader()

        def send(m):
            # Returns whether the message reached the wire. Nothing in Bridge
            # reads it; the tap does, so a message refused below is recorded
            # as refused instead of as sent.
            #
            # ota_data is not logged: an image is ~5000 chunks and each line
            # carries 344 characters of base64, which would bury every other
            # message in the log. Bridge prints its own progress every 200.
            if m.get("t") != "ota_data":
                print(f"[bridge] -> {m}", file=sys.stderr)
            # encode_CHECKED. This is the only writer, and it used to call
            # plain encode() -- so protocol.encode_checked, written precisely
            # to guard the board's 512-byte cliff and documented as the thing
            # callers use, had no production caller at all and only tests
            # exercised it.
            #
            # It matters because the board does not truncate an over-long
            # line, it DROPS it whole (proto.c) with no error on either side:
            # the panel silently stops updating while this log keeps printing
            # the message as sent. A fully loaded two-provider frame already
            # measures 484 of the 512 bytes.
            raw, why = protocol.encode_checked(m)
            if raw is None:
                print(f"[bridge] NOT SENT: {why}", file=sys.stderr)
                return False
            ser.write(raw)
            return True

        # The board approved an update. esptool needs the port to itself, so
        # close it, write slot0, and let the outer reconnect loop pick the
        # board back up -- esptool resets it into the new image on the way out.
        class Reflashed(Exception):
            """Carries the reason when the write failed.

            The port is closed by the time flash() returns and the board has
            just been reset, so it cannot be told on this connection. The
            reason rides out to the reconnect below and is delivered once the
            board is back -- the first moment it can be shown at all.
            """

            def __init__(self, why=None):
                super().__init__(why or "")
                self.why = why

        def flash_image(blob, version):
            # Let the board paint its warning first. esptool resets it into
            # the ROM loader, after which the panel is dead until the new
            # image boots -- so the "the screen goes dark for about 2 minutes"
            # frame has to be on screen BEFORE we take the port away, or the
            # blackout arrives with no explanation.
            time.sleep(4)
            print(f"[bridge] ota: closing port to flash {version}",
                  file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            ok, why = ota_mod.flash(port, blob)
            print(f"[bridge] ota: {'flashed ' + version if ok else 'FAILED: ' + why}",
                  file=sys.stderr)
            raise Reflashed(None if ok else why)

        def self_update(version, artifact):
            """Replace this program, then hand the cable to the new one.

            The port is closed first. On Windows the replacement is started
            before this process exits, and two daemons racing for one serial
            port is a failure that would look exactly like a broken board.
            """
            try:
                blob = update.download(update.platform_key(), artifact)
            except Exception as e:
                print(f"[update] download failed: {e}", file=sys.stderr)
                return False
            ok, message = update.apply(blob, self_bin, version)
            print(f"[update] {message}", file=sys.stderr)
            if not ok:
                return False
            try:
                ser.close()
            except Exception:
                pass
            update.restart_from_daemon(self_bin)     # does not return
            return True

        # The tap sits around the two callables that carry the link. It was
        # built at the top of this connection (so the probe's reads are in it)
        # and is not shared with the next one: a half-line left over from a
        # board unplugged mid-print cannot glue itself to a later session.
        # dispatch reaches `bridge` late, by closure: it is assigned just
        # below and nothing calls dispatch before the read loop.
        def dispatch(m):
            return bridge.on_message(m)

        tap, write_msg, dispatch = install_tap(send, dispatch, tap)

        bridge = Bridge(write_msg=write_msg, fetch_usage=fetch,
                        flash_image=flash_image,
                        report_failure=report_failure,
                        set_preferred=bus.set_preferred,
                        self_update=self_update,
                        pending=update.PendingFirmware(
                            os.path.join(blink_home, "pending_fw.json")))
        report_failure = None   # handed to the Bridge above; never repeated
        # No reset means no boot `hello`, so nothing would trigger the
        # greeting -- see Bridge.greet.
        if already_running:
            bridge.greet()
            # No hello means the board never said which firmware it runs;
            # the last one it told us is in board.json. This is the path an
            # app update takes -- the board keeps running through it -- and
            # the one that left boards on old firmware (2026-08-29).
            bridge.offer_if_newer(known.get("fw"))
        next_poll = time.monotonic()
        next_fast_poll = time.monotonic()
        # The rollback copy is kept until a board has actually talked to this
        # build. Running at all is weak evidence; holding a conversation with
        # the hardware is the thing the update was for.
        proven = False
        try:
            while True:
                # in_waiting first, then a blocking read(1) as the idle wait.
                #
                # ser.read(n) does NOT return early on partial data: it waits
                # for n bytes or the full timeout. With a 0.2 s timeout that
                # cost 0.2 s per OTA chunk -- and since the transfer is
                # stop-and-wait, that stall WAS the transfer rate: 213 B/s
                # measured, ~100 minutes for a 1.3 MB image. Draining what has
                # actually arrived keeps the link busy instead.
                data = ser.read(ser.in_waiting or 1)
                if data:
                    # Echo raw board console (logs + its [usage] prints) for visibility.
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
                    if tap:
                        tap.console(data)
                    for msg in reader.feed(data):
                        print(f"[bridge] <- {msg}", file=sys.stderr)
                        dispatch(msg)
                        # A message of ours off this port is the only
                        # positive identification there is. Write it down:
                        # the next start opens this port directly instead
                        # of scanning, and this is also what licenses a
                        # reset if it ever goes quiet. Written again whenever
                        # a message adds something -- a hello after a `pref`,
                        # a new firmware after a flash.
                        learned = learn_board(known, port, msg)
                        if not proven or learned != known:
                            known = learned
                            remember_board(blink_home, known["port"],
                                           known.get("board_id"),
                                           known.get("fw"))
                        if not proven:
                            update.cleanup(self_bin)
                            proven = True
                if time.monotonic() >= next_poll:
                    # Poll only while the board is provably alive (pings within
                    # the liveness window): there is no point talking to a
                    # panel that is not answering, and a daemon that pushes
                    # into a dead port learns nothing from the writes going
                    # through. The hello handler still pushes immediately on
                    # (re)connect.
                    #
                    # (What used to be written here was about the usage
                    # endpoint's rate limit. There is no endpoint: fetch_usage
                    # reads local files, which is exactly why the fast tick
                    # below is free.)
                    if bridge.board_alive():
                        bridge.poll_once()
                    next_poll = time.monotonic() + poll_every
                if time.monotonic() >= next_fast_poll:
                    # The fast tick, after the heartbeat on purpose: when both
                    # come due in the same pass the heartbeat has already sent
                    # everything, so this one finds nothing changed and stays
                    # quiet instead of sending the same line twice.
                    #
                    # Gated on board_alive() for the same reason as above --
                    # the cheap read is not the expensive part, the write into
                    # a port nobody is listening on is.
                    next_fast_poll = time.monotonic() + FAST_POLL_INTERVAL_S
                    if bridge.board_alive():
                        bridge.poll_if_changed()
                # Not gated on board_alive(): drift is a fact about this
                # machine, not about the cable. The same _upkeep runs from
                # inside wait_for_port, so an unplugged machine repairs a
                # wiped hook too -- the watchdog's own interval keeps either
                # path from checking more often than it should.
                _upkeep()
                if time.monotonic() >= next_update:
                    next_update = time.monotonic() + UPDATE_INTERVAL_S
                    try:
                        _self_update_tick(self_bin)
                    except Exception as e:
                        # Never take the bridge down over an update check. The
                        # gauge working is worth more than the gauge being new.
                        print(f"[update] check failed: {e}", file=sys.stderr)
        except Reflashed as r:
            # Expected: the port is already closed and the board is rebooting
            # into what we just wrote. Give it a moment, then reconnect.
            #
            # Any failure reason travels with it. Without this the board sat
            # on "keep the cable connected" until its own deadline expired and
            # then blamed the timeout, while the daemon had the real reason
            # ("chip has flash encryption", "esptool not found") in its log and
            # no way to say it.
            report_failure = r.why
            time.sleep(2)
            continue
        except (serial.SerialException, OSError) as e:
            print(f"[bridge] serial lost: {e}; looking for the board on"
                  " every port", file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            # The board is gone from THIS port. It may come back on the same
            # one, on another socket, or as a different unit -- so nothing
            # learned about the old layout still holds. Start the search
            # over: the remembered port is tried first, every other candidate
            # after it, and whichever answers is the board from then on.
            asked.clear()
            searched = None
            patient = False
            time.sleep(1)
            port = wait_for_port(args.port, on_wait=_upkeep)


if __name__ == "__main__":
    main()
