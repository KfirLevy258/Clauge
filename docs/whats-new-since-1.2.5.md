# What's new in BLINK — 1.2.5 to 1.3.2

Website copy. Everything below is a change a customer can see or feel. Three
releases: **1.3.0**, **1.3.1**, **1.3.2**.

Boards and the app update together. If you are on 1.2.5, run `blink update`
and take the firmware update the board offers.

---

## The panel says more

**It names the project you are working in.** The line under the activity dot
carries the project a session belongs to, so a glance tells you *which* work is
running, not just that something is. Codex sessions are named too.

**One pip per session.** A small row between the clock and the brand shows how
many sessions are open and what each is doing, so two terminals no longer look
like one.

**The clock moved under the brand,** and the dials grew into the space it left.
The top-left corner now carries the activity state instead.

**It tells you when the numbers have gone quiet.** If a reading has stopped
being live, the board says how old it is rather than presenting a stale figure
as current.

**A status change announces itself** for five seconds and then gets out of the
way, instead of either shouting or saying nothing.

**Codex turns that die on an error now read as failed,** not as finished. A
rate limit is the headline this product exists for; it should not look like a
completed turn.

**The board sleeps when the numbers stop moving,** not only when the computer
goes away. A desk nobody has touched for hours stops glowing at an empty chair.

---

## Claude Desktop is properly supported

If you use Claude Desktop without Claude Code, the panel used to show
percentages only. Now:

- **a five-hour countdown**, whenever Desktop is holding a reset time;
- **a weekly countdown**, once one has been learned;
- percentages refresh on Desktop's own schedule — about every 5 minutes while
  you are using it, up to 15 when it sits idle.

Desktop keeps its reset time only while a window is running and clears it in
between, so the countdown comes and goes. The fill rate covers the gaps.

Everything is read from Claude Desktop's own local files on your machine.
Nothing about your usage or your conversations leaves the machine.

---

## Windows sets itself up

**The USB driver ships with the app and installs itself.** The board's chip
needs a driver Windows does not include. The installer now carries it and puts
it in — one Windows permission prompt, and only when there is actually
something to install. Before, a new board simply never appeared.

**A board Windows cannot use yet says so.** It used to read as *"not plugged
in"* to someone looking straight at it. Now `blink status` says *"a board is
plugged in, but Windows has no driver for it"* and names the command that fixes
it.

**The background service starts without administrator rights.** Registering it
needed admin, silently failed without it, and left the panel stuck part-way
through setup. There is a fallback now, so a normal account installs cleanly.

**`blink update` stops reporting a failure for an update that worked.** Windows
will not rename the program's folder while the app is still holding a file in
it, so the update raced its own background service and reported defeat on the
first refusal — while succeeding anyway. It now stops the service first and
waits for the handle.

---

## Things that were wrong

**The panel went blind at the moment the limit ran out.** Exactly when you most
need the number, it stopped showing one. Fixed.

**The board no longer claims "Up to date" over an app that is a release
behind.** If the two halves have come apart, the board says so, and
`blink status` names the version each half is on and the command that fixes it.

**The app stops creating that mismatch in the first place.** A firmware update
is no longer offered unless the matching app update can be worked out too, so a
network blip during a check cannot leave the board and the computer permanently
out of step.

**On Linux, finished sessions leave the panel when they finish.** A session
that ended without announcing it could linger for a full hour, depending on
something as arbitrary as the order the system listed a folder.

**The service is no longer reported as running on the strength of an exit
code.** Windows and Linux both claimed a healthy background service from a
command that had merely been accepted. They ask the system now.

**A quote in a project name no longer cuts the label short** on the panel.

**A session being written to is no longer left unnamed** for the rest of the
day.

**Claude Desktop's cache is read far more cheaply** — it was being re-parsed
in full every two seconds.

---

## Under the floor

Not visible, but it is why the above can be trusted.

**Every release is now proved on real hardware before it ships.** Three
machines — macOS, Windows and Linux — each drive a real board through scripted
scenarios: usage climbing, going past 100%, a reading going stale, and the
board falling asleep and waking. The release tooling refuses to publish unless
all three have passed on exactly the code being released.

**Factory programming no longer writes an image that undoes itself.** A board
that already had firmware would quietly revert about ninety seconds after a
re-burn that reported success.

**A release cannot ship firmware whose version did not move,** which had
previously left a week of fixes unreachable by any board.

---

## Release by release

| | |
|---|---|
| **1.3.2** | Windows update reports honestly; the app stops creating board/app mismatches; finished sessions leave the panel on Linux; factory programming fixed; every release proved on three real boards first. |
| **1.3.1** | The board stops saying "Up to date" over an app a release behind. |
| **1.3.0** | Claude Desktop countdowns; the Windows driver ships and installs itself; the background service works without administrator rights; project names, the pip row and the age caption reach the panel. |

---

## Upgrading

```
blink update
```

Then take the firmware update the board offers. The app installs first and the
board follows, so the two stay in step.

If the board is already ahead of the app — it can happen if firmware was
installed by hand — `blink status` will say so and name the same command.
