"""Keeping ~/.blink/bridge.log readable, and keeping it small.

The log had no timestamps, no size limit and nothing that ever removed a
line. One customer's was 2 MB after a week; every machine that has run the
daemon since it shipped has been growing one without bound. It is also the
file `docs/README-full.md` asks a customer to send when something is wrong,
so it is a support artifact, not scratch output.

Three separate faults were producing that, and they need three answers:

  1. NOTHING WAS EVER REMOVED. rotate_if_full() below.

  2. MOST OF IT WAS HEARTBEAT. Profiling 3000 consecutive lines of a real
     log: 626 `-> pong`, 619 raw `{"t":"ping"}` and 581 parsed `<- ping` --
     about 60% of the file, for a 10-second keepalive that says nothing
     except that a cable nobody unplugged is still plugged in. The bridge
     now counts those instead of printing them, and says so once in a while.

  3. TWO WRITERS SHARED ONE FILE DESCRIPTOR. The board's console was echoed
     with a raw `sys.stderr.buffer.write(data)` while every other line went
     through `print(..., file=sys.stderr)`. Text-mode and binary writes to
     the same fd interleave mid-line, and the real log is full of the
     result:

         [u[bridge] <- {'t': 'ping', 'v': 2, 'up_ms': ...
         [proto] ho[bridge] <- {'t': 'pref', ...

     which breaks grep on exactly the file support greps. ConsoleEcho below
     assembles whole lines and hands them to the same print path, so there
     is one writer again.

Nothing here imports anything outside the standard library, and nothing here
looks at a payload: ConsoleEcho splits on newlines and forwards, and the
Journal counts bytes.
"""
import os
import time

# Rotate the live file at 2 MB and keep three older generations, so the whole
# log costs at most 8 MB on a customer's disk no matter how long the daemon
# runs. At the volume left after the heartbeat suppression above, that is
# months of history rather than the days a date-based rule would give on a
# busy machine -- and unlike a date rule it cannot be exceeded.
CAP_BYTES = 2 * 1024 * 1024
KEEP = 3


def _stamp(when=None) -> str:
    """Local time, because the reader is a person comparing it with what they
    saw on their own clock. Sortable and unambiguous; the year is there
    because these files now live long enough to span one."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


class Journal:
    """A text stream that stamps every line with the time it was written.

    Wraps sys.stderr rather than replacing the 31 print() calls that write to
    it, and rides on the supervisor's own redirect: launchd and the Windows
    task both point the daemon's stderr at bridge.log, so writing through
    here is writing to that file.

    print() reaches write() more than once for one line -- the text, then the
    newline -- so the stamp is placed on a transition into a line rather than
    per call. A traceback arrives the same way and comes out stamped too,
    which is the case the timestamps were most missing for: an exception in
    the log used to have nothing to say when it happened.
    """

    def __init__(self, stream, clock=None):
        self._stream = stream
        self._clock = clock or _stamp
        self._fresh = True          # at the start of a line

    def write(self, s) -> int:
        if not s:
            return 0
        out = []
        for part in s.splitlines(keepends=True):
            if self._fresh:
                out.append(self._clock())
                out.append(" ")
            out.append(part)
            self._fresh = part.endswith(("\n", "\r"))
        self._stream.write("".join(out))
        # PYTHONUNBUFFERED is set in the plist and the unit, but the Windows
        # service opens its own file with buffering=1 and this wrapper sits
        # between print() and that -- so flush here rather than trust which
        # of the two is in play. The log lagging minutes behind the daemon
        # reads as a hang; the comment on the plist says so.
        self._stream.flush()
        return len(s)

    def flush(self):
        self._stream.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        """The wrapped stream's descriptor.

        rotate_if_full() needs it: it will not truncate a file until it has
        established that the file really is where our stderr goes, and after
        this wrapper is installed `sys.stderr` IS this object. Without this
        the check answers "not our file" for every machine and the rotation
        silently never runs -- a feature that is present, tested, and dead.
        """
        return self._stream.fileno()

    # The daemon's `bye` path and anything else that reaches for raw bytes
    # gets the real stream's buffer, unstamped. Nothing does today -- the
    # console echo used to and no longer does -- but a wrapper that raises
    # AttributeError on .buffer would turn a stray byte write into a crash
    # in a login service, which is a poor trade for a missing timestamp.
    @property
    def buffer(self):
        return getattr(self._stream, "buffer", None)


class ConsoleEcho:
    """The board's own console output, one whole line at a time.

    Fed the same bytes the protocol reader is fed. Emits complete lines
    only, so a read that lands mid-line does not produce half of one in the
    log while another writer is mid-line too.

    Protocol JSON is dropped. Every one of those lines is also parsed by
    protocol.LineReader and logged by the bridge in the form it acted on, so
    echoing the raw text as well wrote everything twice -- and the heartbeat,
    which is most of the traffic, is written a third time as the pong going
    back. What is left is what the echo was actually for: the board's printk
    output, its [proto] and [ota] and [usage] lines, which exist nowhere
    else.
    """

    # Deliberately a shape test on the first character and not a parse. This
    # is not deciding what a message means -- the reader does that -- only
    # whether the bridge is about to log the same bytes twice.
    def __init__(self, limit=4096):
        self._buf = bytearray()
        # A board that emits a very long line without a newline must not grow
        # this without bound. Nothing does today; the cap is here because a
        # buffer fed from a wire is the wrong place to assume that.
        self._limit = limit

    def feed(self, data):
        """Returns the complete console lines in `data`, as text."""
        self._buf.extend(data)
        lines = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            raw = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if not line.strip():
                continue
            if line.lstrip().startswith("{"):
                continue            # protocol JSON: logged parsed instead
            lines.append(line)
        if len(self._buf) > self._limit:
            del self._buf[:-self._limit]
        return lines


class Heartbeat:
    """Counts the keepalive instead of printing it.

    The board pings every 10 seconds and the bridge pongs back. Logging both
    ends produced 25,000 lines a day that carry one bit between them -- the
    link is up -- which the summary line states directly, and which the next
    real message states anyway.

    Kept rather than deleted outright because the absence of a heartbeat is
    diagnostic: a support reader needs to be able to tell "the cable went
    quiet at 14:02" from "the daemon stopped logging".
    """

    def __init__(self, every_s=600.0, now=None):
        self._every = every_s
        self._now = now or time.monotonic
        self._n = 0
        self._next = self._now() + every_s

    def beat(self) -> None:
        self._n += 1

    def due(self):
        """A line to log, or None. Call as often as you like.

        Silent when nothing has beaten since the last summary: a quiet link
        should leave a gap in the log, not a line every ten minutes saying
        there is nothing to say.
        """
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._every
        if not self._n:
            return None
        n, self._n = self._n, 0
        return f"[bridge] link ok: {n} keepalives in the last {int(self._every)}s"


class Repeats:
    """Log a message when it says something new; count it when it does not.

    The usage frame goes out once a minute whether or not anything moved, and
    once the heartbeat stopped being logged it was most of what was left:
    about 270 bytes a minute, 0.5 MB a day, which is 16 days of history in
    the 8 MB the rotation allows. An idle desk was spending all of that
    saying "still 8%".

    Comparison is on the fields that carry meaning, NOT on the whole message.
    Three of its fields -- the two countdowns and the age -- change on every
    single frame by construction, so any "is this the same message" test that
    included them would answer no every time and suppress nothing. That is
    the trap this class exists to avoid, and it is why the fields are named
    by the caller rather than inferred here.

    A change is always logged immediately. The suppression only ever applies
    to a frame that repeats what the line above it already said.
    """

    def __init__(self, fields, label="messages", every_s=600.0, now=None):
        self._fields = tuple(fields)
        self._label = label
        self._every = every_s
        self._now = now or time.monotonic
        self._last = None
        self._n = 0
        self._next = self._now() + every_s

    def _key(self, m):
        return tuple(m.get(f) for f in self._fields)

    def worth_logging(self, m) -> bool:
        key = self._key(m)
        if key != self._last:
            self._last = key
            return True
        self._n += 1
        return False

    def due(self):
        """A line for the frames that were held back, or None."""
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._every
        if not self._n:
            return None
        n, self._n = self._n, 0
        return f"[bridge] {n} unchanged {self._label} not logged"


def _is_the_same_file(path, stream) -> bool:
    """Is `stream` actually writing to `path`?

    The daemon does not open its own log on macOS -- launchd does, via
    StandardErrorPath -- so before truncating a file we have to establish
    that it is the file our own stderr goes to. On Linux the unit sets no
    StandardOutput at all and the output goes to the journal, which rotates
    itself and must not be touched from here; this returns False there and
    rotation becomes a no-op, which is the correct behaviour rather than a
    missing feature.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    try:
        a = os.fstat(fd)
        b = os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def rotate_if_full(path, stream, cap=CAP_BYTES, keep=KEEP) -> bool:
    """Copy the log aside and truncate it in place. Returns whether it ran.

    Copy-and-truncate, NOT rename-and-reopen, and the reason is that we do
    not own the file descriptor. launchd opened bridge.log before the daemon
    existed and holds that fd for the life of the job: rename the file and
    the daemon goes on writing to the same unlinked inode, so the new
    bridge.log stays empty forever and the old one is invisible but still
    growing. That failure is silent and total, which is why this is the one
    approach here.

    Truncating under a live writer is only safe because the fd is O_APPEND,
    which makes every write seek to end-of-file first: after the truncation
    the next line lands at offset 0 rather than at the old offset with two
    megabytes of NUL in front of it. Measured, not assumed -- `lsof +fg` on
    the running daemon reports FILE-FLAG `R,W,AP` on both fd 1 and fd 2, and
    the Windows path opens its own file with mode "a".

    The window between the copy and the truncation can lose a line written
    inside it. That is inherent to the approach (logrotate's copytruncate has
    the same note) and it is the right trade here: the alternative loses
    every line from the rotation onwards.
    """
    try:
        if os.path.getsize(path) < cap:
            return False
    except OSError:
        return False
    if not _is_the_same_file(path, stream):
        return False

    # Oldest first, so nothing is overwritten before it has been moved.
    try:
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            nth = f"{path}.{i}"
            if os.path.exists(nth):
                os.replace(nth, f"{path}.{i + 1}")
        _copy(path, f"{path}.1")
        os.truncate(path, 0)
    except OSError:
        # A log that cannot be rotated must never take the daemon down with
        # it. The next tick tries again; in the meantime the file grows,
        # which is what it did for the whole of this product's life so far.
        return False
    return True


def _copy(src, dst):
    """shutil.copyfile without importing shutil into the frozen bundle for
    one call. Chunked, because the file is megabytes by definition here."""
    with open(src, "rb") as r, open(dst, "wb") as w:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            w.write(chunk)
