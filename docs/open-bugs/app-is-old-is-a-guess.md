# "App is old" is inferred, not reported

**Status:** open. Found 2026-09-11, on the author's own desk, by looking at a
board that was telling the truth about the wrong question.

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

## The shape of the fix

Additive, and the protocol was built for exactly this (same docstring): a
field older firmware ignores, so `PROTO_VERSION` does not move.

1. Carry the fact on a message the board always gets -- `app` on `ota_none`,
   or on `welcome`, which is the one message every connection begins with.
2. `proto_host_outdated()` returns what the daemon SAID, not a comparison. A
   daemon too old to set the field sets nothing, the row goes blank, and blank
   is the right answer: better silent than confidently wrong.
3. `tests/upd_row/host_test.c` already covers the row's wording and takes
   `host_outdated` as a bool, so it needs no change beyond a case for
   "unknown". The daemon side wants a test that `ota_none` carries the app
   version when one is newer, and does not when none is.

## The second half, which the fix above does not address

Even when it is true, the row is a dead end. It states a problem, offers no
action, and the label budget is about 13 characters (`ui_settings.c:1186`), so
there is no room to say `run blink update`. The button beside it is correctly
disabled -- the board cannot update the app on someone's computer -- but the
customer is left with an amber complaint and nowhere to go.

Worth deciding deliberately rather than inheriting: either the row earns a
sentence somewhere that fits, or it says nothing at all.
