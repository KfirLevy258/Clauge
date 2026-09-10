# "App is old" is inferred, not reported

**Status:** FIXED 2026-09-11, the same night it was found -- on the author's
own desk, by looking at a board that was telling the truth about the wrong
question. Kept here rather than deleted because the reasoning is the point: the
bug was not a wrong answer, it was the right answer to a question nobody meant
to ask. Move it out of open-bugs/ whenever that stops being useful.

## What it says, and what it knows

The settings screen's update row reads **"App is old"** whenever
`proto_host_outdated()` is true, and that function is this
(`firmware/src/proto.c:732`):

```c
return host_seen && host_ver[0] &&
       ota_version_newer(BLINK_FW_VERSION, host_ver);
```

A version comparison between the board's own firmware and the daemon's
version, and nothing else. So the row does not mean "the app is old". It means
"the app is older than me", and it prints the first while only knowing the
second.

Those are the same sentence only while both halves ship from one tag, which is
the invariant `pc/version.py` states and `tests/ci/check_versions.sh` enforces
for releases. A board that is ahead of the published feed breaks it, and then
the row is simply false.

## How to see it

Flash a board from an unreleased tree -- `tools/burn.sh` on any working copy
whose version has moved past the newest tag -- and leave the installed daemon
alone. The board now says "App is old" about a daemon that is the newest
release there is.

That is not a corner case dressed up as one: it is every developer's desk from
the first local build onwards, and any board that ever runs ahead of the feed
for any reason.

Observed 2026-09-11 with a board on a scratch 1.3.3 and the app on 1.3.2, where
1.3.2 was the newest published release. The daemon said so on every query:

    [bridge] ota: board has 1.3.3, release has 1.3.2 -- nothing to do

while the panel called that same app old.

## Why it was written as a guess

The daemon is the only party that knows, because it is the one that fetches and
verifies the signed manifest. It already has a way to say it -- but only as a
passenger on a firmware offer (`pc/protocol.py:634`):

```python
def ota_avail(version, size, sha256, app=None):
    """`app` is the daemon version this release also carries, when it is newer
    than the one running. Additive and optional..."""

def ota_none():
    return {"t": "ota_none", "v": VERSION}      # carries nothing
```

So in the one case where this row is the only thing on screen with anything to
say -- firmware current, app behind -- the daemon answers `ota_none` and the
board is told nothing whatsoever. The version comparison is what is left.

## What was done

Additive, and the protocol was built for exactly this (same docstring): a
field older firmware ignores, so `PROTO_VERSION` did not move.

1. `ota_none` carries `app` on the two paths that know it -- the steady state
   ("nothing to do"), which costs one signed fetch and is the state a working
   desk sits in, and the pairing refusal, where it is free because
   `_app_available` has already run.
2. `proto_host_outdated()` returns what the daemon SAID. A daemon too old to
   set the field sets nothing, the row goes blank, and blank is the right way
   to be wrong: silent beats confidently false.
3. `blink status` keeps its comparison -- it is offline by design and cannot
   ask the feed -- but now names the other possibility instead of only
   prescribing a command that may do nothing.

Verified on the wire against the real feed, with a board on 1.3.3 and the
published release at 1.3.2:

    [bridge] ota: board has 1.3.3, release has 1.3.2 -- nothing to do
    [bridge] -> {'t': 'ota_none', 'v': 2}

No `app` field, so the row goes blank instead of calling the newest published
release old. Flashed and boot-verified on 20500d342b68.

## `blink status` has it too, and its advice is worse

The same comparison drives the CLI, which does name a remedy the panel has no
room for:

    Board       ... firmware 1.3.3
                the board is on 1.3.3 and this app is 1.3.2 -- they ship together
                run: /Users/kfir/.blink/bin/blink update

Observed 2026-09-11 on a desk whose app was already the newest published
release. Running that command finds nothing to do, so the one place with room
to give an instruction was spending it on one that cannot help.

Fixed differently from the panel, and deliberately. `blink status` is offline by
design -- it has to work on a plane, and it is the first thing anyone runs when
nothing works -- so it cannot ask the feed and must not pretend to. It keeps the
comparison and names the other possibility instead:

    (if that finds nothing, this board is ahead of the published release)

## The second half, which the fix mostly dissolves

The row was also a dead end: it stated a problem, offered no action, and the
label budget is about 13 characters (`ui_settings.c:1186`), so there is no room
to say `run blink update`. The button beside it is correctly disabled -- the
board cannot update the app on someone's computer.

That is much smaller now. The row no longer means "older than me", it means the
daemon has found a newer app it can actually install, so `blink update` really
is the remedy and the customer is no longer being told about a problem with no
solution. What remains is only that the panel cannot NAME the remedy in
thirteen characters, which is a copy question rather than a correctness one --
and the customer who is looking at the board is sitting at the computer the
command runs on.
