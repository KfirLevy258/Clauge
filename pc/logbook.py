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

# Rotate at 4 MB and keep four older generations: 20 MB on a customer's disk,
# ever, no matter how long the daemon runs.
#
# The numbers are measured rather than chosen. On a development machine under
# continuous use -- the worst case there is, because the reading genuinely
# changes almost every poll and almost nothing gets suppressed -- the log runs
# at 24 KB/hour, or about 4 MB a week. 20 MB is therefore around five weeks
# there and considerably longer on a machine that is merely in use.
#
# An earlier version of this said 2 MB and three generations, on an estimate
# that the usage frame would rarely change. It changes most minutes on a
# machine somebody is working at, which put the real retention at a fortnight.
# Disk is the cheapest thing in this trade: the daemon's own download is 25 MB,
# so a 20 MB ceiling on its log is not a number anyone will notice, and a
# ceiling is the whole point -- a date rule cannot promise one.
CAP_BYTES = 4 * 1024 * 1024
KEEP = 4


def _stamp(when=None) -> str:
    """Local time, because the reader is a person comparing it with what they
    saw on their own clock. Sortable and unambiguous; the year is there
    because these files now live long enough to span one."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


# How far back the repeat detector looks for a repeating block, and how long
# a run may go unreported. The window has to cover a Python traceback, which
# is the shape that matters -- a crash-looping login service writes the same
# five-to-ten lines every ThrottleInterval, forever.
REPEAT_WINDOW = 16
TALLY_EVERY_S = 300.0


class Journal:
    """A text stream that timestamps every line and collapses repeats.

    Wraps sys.stderr rather than replacing the 31 print() calls that write to
    it, and rides on the supervisor's own redirect: launchd and the Windows
    task both point the daemon's stderr at bridge.log, so writing through
    here is writing to that file.

    WHY IT COLLAPSES BLOCKS AND NOT LINES. The case worth handling is a
    daemon that cannot start: launchd restarts it every ThrottleInterval and
    it writes the same traceback each time, about 8,600 times a day. Adjacent
    identical lines never occur in that -- consecutive lines of a traceback
    differ; it is the BLOCK that repeats -- so a syslog-style "last message
    repeated" would not fire once. Without this, such a loop fills all five
    rotation generations in about four days and evicts the history from
    before the fault, which is the only part anyone needs.

    The block is written TWICE before anything is suppressed, so a reader
    sees the shape before they see the count.

    print() reaches write() more than once for one line -- the text, then the
    newline -- so lines are assembled here and judged whole. A partial line
    is held rather than written, because a line cannot be un-written once it
    has gone out, and flush() emits whatever is pending.
    """

    def __init__(self, stream, clock=None, window=REPEAT_WINDOW,
                 tally_every_s=TALLY_EVERY_S, now=None):
        import time as _time
        self._stream = stream
        self._clock = clock or _stamp
        self._now = now or _time.monotonic
        self._window = window
        self._tally_every = tally_every_s
        self._partial = ""          # a line not yet terminated
        self._recent = []           # lines emitted, most recent last
        self._block = None          # the repeating block, once detected
        self._pos = 0               # how far into it the current pass is
        self._reps = 0              # complete passes suppressed so far
        self._tally_at = 0.0

    # -- the raw path: stamps and writes, never consulted by the detector --
    def _emit(self, text):
        self._stream.write(self._clock() + " " + text + "\n")
        # PYTHONUNBUFFERED is set in the plist and the unit, but the Windows
        # service opens its own file with buffering=1 and this wrapper sits
        # between print() and that -- so flush here rather than trust which
        # of the two is in play. The log lagging minutes behind the daemon
        # reads as a hang; the comment on the plist says so.
        self._stream.flush()

    def _tally(self):
        """Report and clear the suppressed run, if there is one."""
        if not self._reps:
            return
        n, self._reps = self._reps, 0
        if len(self._block) == 1:
            self._emit(f"[log] last message repeated {n} times")
        else:
            self._emit(f"[log] last {len(self._block)} lines"
                       f" repeated {n} times")
        self._tally_at = self._now()

    def _period(self):
        """Shortest L for which the last 2L lines are the same block twice.

        Shortest, not longest: "A A A A" has period 1, and reporting it as 2
        would halve the count and read as though pairs were the unit.
        """
        r = self._recent
        for L in range(1, self._window + 1):
            if 2 * L > len(r):
                break
            if r[-L:] == r[-2 * L:-L]:
                return L
        return 0

    def _offer(self, text):
        """One complete line, from the caller."""
        if self._block is not None:
            if text == self._block[self._pos]:
                self._pos += 1
                if self._pos == len(self._block):
                    self._pos = 0
                    self._reps += 1
                    # A run that never ends must not leave the log silent --
                    # that reads as a daemon that died, which is the opposite
                    # of what is happening. Report and keep counting.
                    if self._now() - self._tally_at >= self._tally_every:
                        self._tally()
                return
            # The pattern broke. Say what was skipped, then carry on.
            self._tally()
            self._block = None
            self._pos = 0
            self._recent = []

        self._emit(text)
        self._recent.append(text)
        if len(self._recent) > 2 * self._window:
            del self._recent[:-2 * self._window]
        L = self._period()
        if L:
            self._block = list(self._recent[-L:])
            self._pos = 0
            self._reps = 0
            self._tally_at = self._now()

    def write(self, s) -> int:
        if not s:
            return 0
        self._partial += s
        while True:
            i = self._partial.find("\n")
            if i < 0:
                break
            line = self._partial[:i].rstrip("\r")
            self._partial = self._partial[i + 1:]
            self._offer(line)
        # A writer that never sends a newline must not grow this without
        # bound. Nothing does; the cap is here because a buffer fed from
        # somebody else's print() is the wrong place to assume it.
        if len(self._partial) > 8192:
            self._offer(self._partial)
            self._partial = ""
        return len(s)

    def flush(self):
        """Anything held goes out now.

        Called after every print() by the daemon's own flushing, and at
        shutdown -- so a partial line, or a suppressed run at the moment the
        process ends, is still reported rather than lost.
        """
        if self._partial:
            self._offer(self._partial)
            self._partial = ""
        self._tally()
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
    about 270 bytes a minute, 0.5 MB a day, which is forty days of history in
    the 20 MB the rotation allows. An idle desk was spending all of that
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


def brief(m):
    """The usage frame, in the width it deserves.

    A frame is 279 bytes as a dict repr and it goes out every poll. Once the
    heartbeat stopped being logged it was 48% of the whole file, and almost
    all of that is field names re-printed every minute -- the payload is
    twelve short values.

    Nothing parses this log (checked: no test, doc or tool greps the arrow
    lines), and `blink status --wire` prints the full frame on demand for
    anyone who wants every field. So the log gets the values.

    Returns None for anything that is not a usage frame, so the caller falls
    back to the full repr -- an unexpected message is exactly when you want
    every field, and those are rare by definition.
    """
    if m.get("t") != "usage":
        return None
    return ("usage %s/%s %s%%/%ss %s%%/%ss %s n=%s/%s age=%ss%s" % (
        m.get("provider", "?"), m.get("src", "?"),
        m.get("session_pct"), m.get("session_resets_in_s"),
        m.get("weekly_pct"), m.get("weekly_resets_in_s"),
        m.get("state", "?"), m.get("n_sess"), m.get("n_run"),
        m.get("age_s"), " STALE" if m.get("stale") else ""))


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
