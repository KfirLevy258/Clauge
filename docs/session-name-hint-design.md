# The hint line says what is happening, and to which project

**Date:** 2026-09-02
**Status:** design, not yet planned
**Scope:** `tools/blink-hook.sh`, `pc/providers/claude_state.py`, `pc/providers/base.py`, `pc/protocol.py`, `firmware/src/proto.c`, `firmware/src/usage_view.c`

---

## The problem

The top-right dot is painted from two inputs — data health and execution
state — and `refresh_dot()` takes the worse of them. The hint line beneath
it was supposed to say which of the two fired. It never did: `usage_view_set_status()`
switches on `data_health` alone, so every execution-state condition paints the
dot and writes nothing.

The result is that four of the seven conditions the panel can be in show a
coloured dot over a blank line:

| Dot | Hint | Condition |
|---|---|---|
| Amber, steady | *(empty)* | a turn finished and is waiting on you |
| Amber, pulsing | *(empty)* | a permission prompt is open |
| **Red, steady** | *(empty)* | **a session failed** |
| Green, pulsing | *(empty)* | working — fine, nothing to say |

A red dot with a blank line under it is the worst of these. It is
indistinguishable from the red that means "the host is gone", which *does*
get words. The commit that merged the two dots into one (`6540287`,
2026-08-26) named the hint line as the thing that would disambiguate them:

> They collapse into one indicator coloured by the worse of them, with the
> hint line — already there, already empty when all is well — saying which
> fired.

That half was never built. This design builds it, and carries a project name
so the line answers *which* session as well as *what*.

## What it will say

```
Working
Waiting for you - LiveClaudeUi
Waiting for you - 3 sessions
Session failed - Blink
Reading is old - showing last known      (unchanged)
Error - showing last known               (unchanged)
HOST LOST - numbers are frozen           (unchanged)
```

Data health keeps precedence. When the reading itself is in doubt the hint
says so and the execution state is not mentioned, for the same reason
`refresh_dot()` ranks them that way: a reading we cannot vouch for makes the
execution state moot.

Statuses are sentence case, and none of them contains a dash, because ` - `
is the separator.

| State | Text |
|---|---|
| `running` | `Working` |
| `waiting` | `Waiting for you` |
| `idle` | `Finished` |
| `failed` | `Session failed` |
| `stuck` | `Session is wedged` |
| `none` | *(empty)* |

`stuck` is included for completeness. The protocol still names it and no
provider produces it any more — see the `claude_state.py` docstring on why
inference from silence was removed.

## Which session gets named

**Named when exactly one session holds the winning state; a count when
several do.**

This is the rule, and it is a deliberate refusal to pick. The context row was
cut from this screen for precisely this failure:

> It showed one context window, and with several agents running there are
> several … "88% of 4" was an attempt to qualify one number into honesty and
> it did not earn its line.

A name presented while two other sessions share the state is that same
dishonesty. So: one session waiting gets a name, three sessions waiting get
`3 sessions`, and the panel never implies it knows which of them you meant.

> **Assumption, not a decision from the owner.** This rule was chosen by the
> author from the context-row precedent. If the preference is "always name the
> most recent", only `poll()` changes.

## Two constraints that shaped this

### 1. The usage frame has no room for a name

`protocol.py:331` records the measurement:

> this message was measured at 506 of `MAX_LINE_BYTES=512` once `age_s` and
> `p2_age_s` joined it. `proto.c` DROPS an over-long line, so those six bytes
> were the difference between a panel that updates and one that silently
> freezes.

`"proj":"LiveClaudeUi",` is 22 bytes. Adding it to the usage frame would push
a fully-loaded line past the limit and freeze the panel on exactly the busy
two-provider desks that need it most.

**So the name travels as its own message type.** `proto.c:609` ignores unknown
types, which makes a new one free on every board already in the field — an
older firmware sees an unrecognised `t` and drops it, and its hint line keeps
behaving exactly as it does today.

This also fits the data: the numbers change every poll, the project name
changes when you switch terminals.

### 2. The hint label will wrap into the gauges

`usage_view.c:625-630` creates the hint with a fixed width and **no long
mode**, so an over-long string wraps. `STATUS_Y` is 24 and `FONT_LINE_H` is
16, which puts a second line at y=40 — and `GAUGE_ARC_Y` is 44. The hint has
never exceeded one line because every string it could hold was a fixed
literal; a project name removes that guarantee.

`LV_LABEL_LONG_DOT` on the hint, with precedent at `usage_view.c:698` where
`provider_lbl` already does it.

## The pieces

### `tools/blink-hook.sh` — capture the directory name

The shim gains a second `sed` that extracts **only the final path segment** of
`cwd`. The full path is never materialised, never written, and never sent:
`~/Projects/AcmeCorp-Merger` yields `AcmeCorp-Merger` and the parent path is
discarded inside the pipeline.

Sanitised **in the pattern**, exactly as `_ident` already does for the session
id, and for the same stated reason — "there is no separate validation step
that can be forgotten or reordered". The value lands in a JSON string rather
than a filename, so the class must exclude `"` and `\` as well; a name that
fails to match is omitted entirely, and an absent key already means unknown on
both sides.

The state file becomes:

```json
{"event":"PreToolUse","t":1788000000,"name":"LiveClaudeUi"}
```

**The header promise is rewritten in the same commit.** The current text says
the cwd is not read, and after this it is. The replacement states what is
captured — event name, session id, agent id, project directory name, clock
reading — and keeps the existing list of what is still never read: the prompt,
the tool arguments, the transcript, the assistant's message. The comment's own
note that this is policy rather than structure stays; it is now carrying more
weight, and the honest thing is to say so rather than quietly widen it.

### `pc/providers/claude_state.py` — carry the name to the frame

- `_read_state()` returns the name alongside state and age. A `.state` file
  written by an older shim has no `name` key; absent is normal and reads as
  unknown, which is the convention the rest of this module already uses.
- `scan()` currently returns `{state: count}` and discards which session held
  which state. It gains a parallel `{state: [names]}` so `poll()` can tell one
  from several.
- `poll()` sets the frame's label only when the winning state is held by
  exactly one session **and** that session reported a name. Everything else
  leaves it unset, and the count already on the frame carries the meaning.

### `pc/providers/base.py` — a field on the frame

`NormalizedUsageFrame` gains an optional label. It defaults to unset so every
other provider — `claude_desktop`, `codex_cli`, `scripted` — is unchanged and
keeps working without knowing this field exists.

### `pc/protocol.py` — a new message

A `session()` builder emitting:

```json
{"t":"session","v":1,"label":"LiveClaudeUi","n":1}
```

`label` is omitted when there is no name — absent means unknown, the same
convention the usage frame uses for every optional key. `n` is the number of
sessions holding the winning state.

**Sent on change, and on reconnect.** The bridge compares the composed
`(label, n)` pair against the last one it sent and emits the message only when
it differs — but a board that has just booted holds nothing, so this must also
be re-sent on `hello`/`welcome`, the same way firmware currency is re-offered
on every connect rather than once per daemon lifetime. Without that, a board
replugged mid-session shows a bare status until the next time you happen to
switch projects.

The label is capped daemon-side at **24 bytes**. `STATUS_MAX_W` is 300 px and the sizing case
recorded in `usage_layout.h` is `"Reading is old - showing last known"` at 35
characters, so `"Waiting for you - "` leaves roughly 17 characters of budget.
The cap is a byte bound on the wire; `LV_LABEL_LONG_DOT` is what handles the
visual overflow, because character counts and pixel widths are not the same
question and only one of them can be answered on the daemon side.

### `firmware/src/proto.c` — parse it

One more branch in the type dispatch, storing into a bounded buffer and
calling the view. Bounds come from the buffer, not from trust in the daemon.

### `firmware/src/usage_view.c` — compose the line

Three changes:

1. **`usage_view_set_status()` gains an activity fallback.** Where the
   data-health switch currently yields `text = ""`, it composes from the
   execution state instead. Data health keeps precedence; this only fills the
   silence.
2. **`usage_view_set_activity()` refreshes the hint.** It calls only
   `refresh_dot()` today, so the label cannot track an activity change even
   once it has something to say. This is a real defect independent of the
   feature — with it unfixed, a session going from running to failed repaints
   the dot red and leaves whatever the hint last said.
3. **`LV_LABEL_LONG_DOT` on the hint**, per the wrap hazard above.

The suffix is composed on the firmware side from the facts the daemon sends —
a name, or a count — rather than the daemon sending finished prose. Every
other string on this screen is a firmware literal and they should not start
living in two places.

## Testing

- **`tests/ci/check_hook_shim.sh`** — already exists. Extend with the
  sanitiser cases the session-id extractor taught: a name containing a quote,
  a backslash, a slash, a space, a newline; `.` and `..`; an over-long name;
  a payload where a tool argument also carries a `cwd`, which is the exact
  shape of the first-occurrence bug already fixed for `session_id`.
- **`tests/pc/`** — `_read_state` with and without a name; `scan()` grouping
  names by state; `poll()` naming on exactly one and refusing on two; an old
  `.state` file with no name.
- **Protocol** — the new message round-trips; the label is capped; **the usage
  frame's byte count is unchanged**, which is the regression that would
  otherwise freeze panels.
- **`tests/usage_layout/host_test.c`** — the hint's long mode, so a future
  edit cannot silently reintroduce the wrap into the arcs.
- **On hardware** — per the standing rule that firmware work is not done until
  it is flashed and boot-verified. The case to watch is a long project name on
  a real panel at desk distance, which is the one thing none of the above can
  answer.

## Deliberately not in scope

- **Codex and desktop parity.** Only the hook path can produce a name. A
  Codex page will show a status with no name, and a desktop-sourced page shows
  neither. That asymmetry is real and is accepted here rather than papered
  over; `codex_cli` has a `cwd` in its rollout records and could grow the same
  field later, as its own piece of work.
- **Naming the most recent of several.** See the assumption above.
- **Anything about the dot itself.** Colour and pulse are unchanged. This
  design only fills the line under it.
