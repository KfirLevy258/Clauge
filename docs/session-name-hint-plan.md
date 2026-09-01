# Hint Line Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The hint line under the status dot says what the tool is doing and which project it is doing it to, so a coloured dot is never left standing over a blank line.

**Architecture:** Composition is a pure C function in `fmt.c`, host-tested with no LVGL. The project name is captured by the hook shim as a single path segment, carried to the daemon in the existing per-session state file, and sent to the board as its own message type — never inside the usage frame, which has six bytes of headroom. Firmware composes the final string from facts, so no display copy lives in the daemon.

**Tech Stack:** POSIX sh (hook shim), Python 3 stdlib (daemon), C99 + LVGL 9 (firmware), pytest, standalone `cc` host tests.

**Spec:** `docs/session-name-hint-design.md`

## Global Constraints

- **Wire budget:** `MAX_LINE_BYTES = 512`. The usage frame measures 506 bytes fully loaded and `proto.c` drops an over-long line whole. **Nothing in this plan may add a byte to the usage frame.**
- **Additive protocol only:** `proto.c:609` ignores unknown message types. New types are safe on shipped boards; changing an existing type's shape is not.
- **UI copy is sentence case.** Every on-screen sentence starts with a capital letter.
- **No status text may contain `-`.** ` - ` is the separator between status and suffix.
- **Label cap: 24 bytes** on the wire, enforced daemon-side.
- **Non-ASCII must go through `fmt_ascii()`.** Built-in LVGL fonts draw anything non-ASCII as an empty box, and project directories under a non-ASCII user profile are a real configuration here.
- **Hooks never fail.** `tools/blink-hook.sh` exits 0 unconditionally; a Blink bug must not become the user's bug.
- **Firmware is not done until flashed and boot-verified**, not merely built.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `firmware/src/fmt.h` / `fmt.c` | Pure formatting, no LVGL, host-tested | Add `fmt_hint()` |
| `firmware/src/usage_view.c` | Panel widgets and state | Call `fmt_hint()`; fix `set_activity`; `LONG_DOT` on hint |
| `firmware/src/proto.c` | Wire dispatch | New `session` branch |
| `tools/blink-hook.sh` | Capture lifecycle events | Extract project name; rewrite the promise |
| `pc/providers/claude_state.py` | Per-session state → counts | Carry names; name only when unambiguous |
| `pc/providers/base.py` | Normalized frame | Add `label` field |
| `pc/protocol.py` | Message builders | Add `session()` |
| `pc/bridge.py` | Send loop | Send on change and on greet |

Tasks 1 and 2 are independent of each other. Tasks 3→4→5 are a chain. Task 6 needs all of them.

---

### Task 1: The hint line speaks for execution state

Standalone value: after this task the red-dot-over-blank-line bug is gone, with no daemon change at all. The name arrives in Task 5.

**Files:**
- Modify: `firmware/src/fmt.h`, `firmware/src/fmt.c`
- Modify: `firmware/src/usage_view.c:625-630` (hint creation), `:1719-1723` (`usage_view_set_activity`), `:1910-1998` (`usage_view_set_status`)
- Test: `tests/fmt/host_test.c`, `tests/usage_layout/host_test.c`

**Interfaces:**
- Consumes: nothing.
- Produces: `void fmt_hint(const char *status, const char *label, int n_sessions, char *buf, size_t buflen)` and `#define FMT_HINT_MAX 64`. Task 5 calls this with a real label.

- [ ] **Step 1: Write the failing test**

Append to `tests/fmt/host_test.c`, following the file's existing assertion style:

```c
static void test_fmt_hint(void)
{
	char b[FMT_HINT_MAX];

	/* Nothing to say stays empty -- the caller draws no line. */
	fmt_hint("", NULL, 0, b, sizeof(b));
	EXPECT_STR(b, "");

	/* A status alone, when there is no name and one session. */
	fmt_hint("Working", NULL, 1, b, sizeof(b));
	EXPECT_STR(b, "Working");

	/* A name when exactly one session is named. */
	fmt_hint("Waiting for you", "LiveClaudeUi", 1, b, sizeof(b));
	EXPECT_STR(b, "Waiting for you - LiveClaudeUi");

	/* A count when several share the state and no name was sent. */
	fmt_hint("Waiting for you", NULL, 3, b, sizeof(b));
	EXPECT_STR(b, "Waiting for you - 3 sessions");

	/* One session is never "1 sessions"; it is just the status. */
	fmt_hint("Finished", NULL, 1, b, sizeof(b));
	EXPECT_STR(b, "Finished");

	/* A label wins over a count if both arrive. */
	fmt_hint("Working", "Blink", 2, b, sizeof(b));
	EXPECT_STR(b, "Working - Blink");

	/* Non-ASCII is transliterated, never drawn as boxes. */
	fmt_hint("Working", "caf\xc3\xa9", 1, b, sizeof(b));
	EXPECT_STR(b, "Working - caf?");

	/* An empty label is the same as no label. */
	fmt_hint("Working", "", 1, b, sizeof(b));
	EXPECT_STR(b, "Working");

	/* Truncation never overruns and always NUL-terminates. */
	char small[12];
	fmt_hint("Waiting for you", "LiveClaudeUi", 1, small, sizeof(small));
	EXPECT_EQ((int)strlen(small), 11);
}
```

Register it in the file's `main()` alongside the existing `test_*` calls.

- [ ] **Step 2: Run test to verify it fails**

Run: `tests/ci/check_host_tests.sh`
Expected: `fmt BUILD FAILED` with `implicit declaration of function 'fmt_hint'`.

- [ ] **Step 3: Declare it in `fmt.h`**

Add after `fmt_ascii`:

```c
/* Longest is a 15-char status, " - ", and a 24-byte label, plus NUL. */
#define FMT_HINT_MAX 64

/*
 * The line under the status dot: what is happening, and to what.
 *
 *   status  ""      -> ""                      (nothing to say)
 *   label   set     -> "Working - Blink"
 *   n > 1           -> "Waiting for you - 3 sessions"
 *   otherwise       -> "Working"
 *
 * A label BEATS a count, because the daemon only sends one when exactly one
 * session holds the state -- see pc/providers/claude_state.py. `n == 1` adds
 * nothing a reader does not already assume, so it is not written.
 *
 * The label is user-controlled text from a directory name, so it goes through
 * fmt_ascii on the way in: the built-in fonts draw non-ASCII as empty boxes,
 * and a project living under a non-ASCII profile is an ordinary setup.
 */
void fmt_hint(const char *status, const char *label, int n_sessions,
	      char *buf, size_t buflen);
```

- [ ] **Step 4: Implement it in `fmt.c`**

```c
void fmt_hint(const char *status, const char *label, int n_sessions,
	      char *buf, size_t buflen)
{
	if (!buf || buflen == 0) {
		return;
	}
	buf[0] = '\0';
	if (!status || !status[0]) {
		return;
	}

	if (label && label[0]) {
		char ascii[FMT_HINT_MAX];

		fmt_ascii(label, ascii, sizeof(ascii));
		if (ascii[0]) {
			snprintf(buf, buflen, "%s - %s", status, ascii);
			return;
		}
	}
	if (n_sessions > 1) {
		snprintf(buf, buflen, "%s - %d sessions", status, n_sessions);
		return;
	}
	snprintf(buf, buflen, "%s", status);
}
```

Add `#include <stdio.h>` to `fmt.c` if it is not already there.

- [ ] **Step 5: Run test to verify it passes**

Run: `tests/ci/check_host_tests.sh`
Expected: `fmt` PASSES, and every other host test still passes.

- [ ] **Step 6: Give the hint label a long mode**

`usage_view.c`, in the block at 625-630 that creates `hint`, after `lv_label_set_text(hint, "")`:

```c
	/*
	 * Ellipsize rather than wrap. Every string this label held used to be
	 * a fixed literal that fit, so wrapping was unreachable; a project
	 * name removes that guarantee, and STATUS_Y (24) plus FONT_LINE_H
	 * (16) puts a second line at y=40 -- on top of the arcs at
	 * GAUGE_ARC_Y (44). Same reason and same call as provider_lbl.
	 */
	lv_label_set_long_mode(hint, LV_LABEL_LONG_DOT);
```

- [ ] **Step 7: Assert the long mode in the layout test**

Add to `tests/usage_layout/host_test.c`, following its existing style of asserting arithmetic from the header:

```c
	/*
	 * A two-line hint lands on the gauges. The label must ellipsize; this
	 * asserts the clearance that makes that non-negotiable.
	 */
	EXPECT(STATUS_Y + FONT_LINE_H < GAUGE_ARC_Y + 4);
	EXPECT(STATUS_Y + 2 * FONT_LINE_H > GAUGE_ARC_Y);
```

- [ ] **Step 8: Add the activity fallback to `usage_view_set_status`**

Replace the `case USAGE_STATUS_OK:` `else { text = ""; }` branch so the empty case defers to execution state. Add above `usage_view_set_status`:

```c
/*
 * What the execution state would say, for the line that data health left
 * empty. Separate from activity_color() because colour and words answer
 * different questions: colour says how bad, this says what.
 *
 * No dash in any of these -- fmt_hint uses " - " as its separator.
 */
static const char *activity_text(void)
{
	switch (activity) {
	case USAGE_ACTIVITY_STUCK:	return "Session is wedged";
	case USAGE_ACTIVITY_FAILED:	return "Session failed";
	case USAGE_ACTIVITY_WAITING:	return "Waiting for you";
	case USAGE_ACTIVITY_IDLE:	return "Finished";
	case USAGE_ACTIVITY_RUNNING:	return "Working";
	default:			return "";
	}
}
```

Then in `usage_view_set_status`, the `USAGE_STATUS_OK` default branch becomes:

```c
		} else {
			/*
			 * Data health has nothing to say, so the line reports
			 * what the tool is doing. This is the half of 6540287
			 * that was never built: the dot was already painted
			 * from execution state here and the line stayed blank,
			 * which left a red "a session failed" looking exactly
			 * like a red "the host is gone".
			 */
			static char hbuf[FMT_HINT_MAX];

			fmt_hint(activity_text(), session_label, session_n,
				 hbuf, sizeof(hbuf));
			text = hbuf;
			tc = COL_DIM;
		}
```

Declare the two statics near the other module state (around line 127, beside `data_health` and `activity`) — Task 5 fills them from the wire, and until then they are the neutral values:

```c
/* Filled by usage_view_set_session() from the daemon; see proto.c. */
static char session_label[28] = "";
static int session_n;
```

Add `#include "fmt.h"` to `usage_view.c` if it is not already present.

- [ ] **Step 9: Make `usage_view_set_activity` refresh the line, not just the dot**

`usage_view.c:1719-1723`:

```c
void usage_view_set_activity(enum usage_activity a)
{
	activity = a;
	/*
	 * The STATUS too, not only the dot. This used to call refresh_dot()
	 * alone, so once the hint learned to speak for execution state the
	 * label could not track a change to it: a session going from running
	 * to failed repainted the dot red and left the previous words under
	 * it. set_status re-runs the whole switch and calls refresh_dot at
	 * the end, so the dot is still repainted exactly once.
	 */
	usage_view_set_status(last_status);
}
```

- [ ] **Step 10: Build the firmware**

Run: `tools/dev.sh` (or the project's normal build entry point)
Expected: builds clean with no new warnings.

- [ ] **Step 11: Run the whole suite**

Run: `tests/ci/check_host_tests.sh && python3 -m pytest tests -q`
Expected: all pass.

- [ ] **Step 12: Commit**

```bash
git add firmware/src/fmt.h firmware/src/fmt.c firmware/src/usage_view.c \
        tests/fmt/host_test.c tests/usage_layout/host_test.c
git commit -m "fix: the dot changed colour and the line under it stayed blank"
```

---

### Task 2: The shim captures the project directory name

Independent of Task 1. Nothing consumes the new field until Task 3.

**Files:**
- Modify: `tools/blink-hook.sh`
- Test: `tests/ci/check_hook_shim.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: `~/.blink/state/<session_id>.state` gains an optional `"name"` key holding a sanitised final path segment, at most 24 bytes. Task 3 reads it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/ci/check_hook_shim.sh`, following the existing case style. Every one of these is a shape the session-id extractor already had to survive:

```sh
# The ordinary case: the last segment only, never the path above it.
check_name '{"session_id":"abc","cwd":"/Users/kfir/Projects/LiveClaudeUi"}' \
	'LiveClaudeUi'

# Windows, where the separator is an escaped backslash in the JSON.
check_name '{"session_id":"abc","cwd":"C:\\\\Users\\\\kfir\\\\Blink"}' 'Blink'

# A tool argument carrying its own cwd must not win. The top-level key is
# first, which is the same rule _ident relies on for session_id.
check_name '{"session_id":"abc","cwd":"/home/k/Real","tool_input":{"cwd":"/tmp/Fake"}}' \
	'Real'

# Characters that would break out of the JSON string are refused whole.
check_name '{"session_id":"abc","cwd":"/tmp/bad\"name"}' ''
check_name '{"session_id":"abc","cwd":"/tmp/bad\\\\name"}' ''
check_name '{"session_id":"abc","cwd":"/tmp/has space"}' ''

# Relative traversal names never become a label.
check_name '{"session_id":"abc","cwd":"/tmp/.."}' ''

# Over-long is refused rather than truncated mid-name.
check_name "{\"session_id\":\"abc\",\"cwd\":\"/tmp/$(printf 'a%.0s' $(seq 1 40))\"}" ''

# No cwd at all is normal: the key is omitted, not written empty.
check_no_name '{"session_id":"abc"}'
```

Write `check_name` and `check_no_name` helpers modelled on the file's existing session-id helpers: run the shim with a payload, then read `$HOME/.blink/state/abc.state` and compare the `name` field (or assert the key is absent).

- [ ] **Step 2: Run to verify they fail**

Run: `tests/ci/check_hook_shim.sh`
Expected: every new case fails — the shim writes no `name` key at all yet.

- [ ] **Step 3: Add the extractor to `tools/blink-hook.sh`**

After the `_ident` helper and its `sid=` lines:

```sh
# The project's DIRECTORY NAME, and only that: the path above it is matched
# and thrown away inside the pattern, so the full path is never held in a
# variable, never written, and never sent.
#
# Sanitised in the pattern for the same reason _ident is -- there is no
# separate validation step that can be forgotten or reordered. This value goes
# into a JSON string rather than a filename, so the class must also exclude the
# two characters that could end it early: `"` and `\`. A name that does not
# match produces nothing and the key is omitted, which already means unknown
# on the other side.
#
# `[^"]*[/\]` is greedy up to the LAST separator before the closing quote, so
# what the class captures is the final segment. Both separators, because on
# Windows the payload carries an escaped backslash and there is no `/` at all.
#
# 24 bytes, matching the daemon's cap: a first character plus 23.
_projname() {
	sed 's/"cwd"/\
&/g' |
		sed -n '2{s|^"cwd"[[:space:]]*:[[:space:]]*"[^"]*[/\]\([0-9A-Za-z][0-9A-Za-z._-]\{0,23\}\)".*|\1|p;}'
}
name=$(printf '%s' "$input" | _projname)
```

- [ ] **Step 4: Write the key, omitting it when there is nothing**

Replace the `printf` in the default `*)` branch with two branches:

```sh
	if [ -n "$name" ]; then
		printf '{"event":"%s","t":%s,"name":"%s"}' \
			"$event" "$(date +%s)" "$name" 2>/dev/null \
			> "$DIR/$sid.state.$$.tmp" &&
			mv -f "$DIR/$sid.state.$$.tmp" "$DIR/$sid.state" 2>/dev/null
	else
		printf '{"event":"%s","t":%s}' "$event" "$(date +%s)" 2>/dev/null \
			> "$DIR/$sid.state.$$.tmp" &&
			mv -f "$DIR/$sid.state.$$.tmp" "$DIR/$sid.state" 2>/dev/null
	fi
```

- [ ] **Step 5: Rewrite the header promise**

Replace the `WHAT IS CAPTURED` paragraph. The old text names the cwd as something never read, and after this change that is false:

```sh
# WHAT IS CAPTURED, exactly: an event name, a session id, an agent id, the
# PROJECT DIRECTORY NAME, and a clock reading. Nothing else is read from the
# payload -- not the prompt, not the tool arguments, not the transcript path,
# not the assistant's message, and not the path above the project directory.
#
# The project name is the second widening of this file, and a larger one than
# the first. The ids are opaque identifiers Claude Code generates; a directory
# name is content, chosen by the user, and it is rendered on a display other
# people can see. It is captured because a status with no subject cannot say
# WHICH of three open sessions is the one waiting on you. The final segment
# only: the pattern below matches the path above it and discards it, so what
# is written is "LiveClaudeUi" and never "/Users/kfir/Projects/LiveClaudeUi".
#
# This remains a policy rather than a structural guarantee, as the first
# widening already made it. Nothing here enforces the list above except the
# code below it.
```

- [ ] **Step 6: Run to verify they pass**

Run: `tests/ci/check_hook_shim.sh`
Expected: all cases pass.

- [ ] **Step 7: Confirm the shim still never fails**

Run: `printf '%s' '{"bogus"' | sh tools/blink-hook.sh PreToolUse; echo "exit=$?"`
Expected: `exit=0`.

- [ ] **Step 8: Commit**

```bash
git add tools/blink-hook.sh tests/ci/check_hook_shim.sh
git commit -m "feat: the hook records which project a session belongs to"
```

---

### Task 3: The daemon carries the name to the frame

**Files:**
- Modify: `pc/providers/base.py:125-132`, `pc/providers/claude_state.py:175-195` (`_read_state`), `:224-253` (`scan`), `:255-273` (`poll`)
- Test: `tests/pc/test_state_machine.py`

**Interfaces:**
- Consumes: the `"name"` key written in Task 2.
- Produces: `NormalizedUsageFrame.label: str = ""`, set by `ClaudeStateProvider.poll()` only when exactly one session holds the winning state and that session reported a name. `scan()` returns a third value, `names: dict[str, list[str]]`. Task 4 reads `frame.label`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/pc/test_state_machine.py`, using the file's existing fixture style for writing state files:

```python
def test_name_is_carried_when_one_session_holds_the_state(tmp_path):
    _write_state(tmp_path, "s1", "Notification", NOW, name="LiveClaudeUi")
    prov = ClaudeStateProvider(path=str(tmp_path), now=lambda: NOW)
    frame = prov.poll(NOW)[0]
    assert frame.state == base.STATE_WAITING
    assert frame.label == "LiveClaudeUi"


def test_no_name_when_two_sessions_share_the_state(tmp_path):
    _write_state(tmp_path, "s1", "Notification", NOW, name="Blink")
    _write_state(tmp_path, "s2", "Notification", NOW, name="Other")
    prov = ClaudeStateProvider(path=str(tmp_path), now=lambda: NOW)
    frame = prov.poll(NOW)[0]
    assert frame.n_wait == 2
    assert frame.label == ""


def test_name_comes_from_the_winning_state_not_another(tmp_path):
    # One waiting, two running. `waiting` wins, and the name must be the
    # waiting session's -- not a running one's, and not absent because the
    # runners are plural.
    _write_state(tmp_path, "s1", "Notification", NOW, name="Waiter")
    _write_state(tmp_path, "s2", "PreToolUse", NOW, name="RunnerA")
    _write_state(tmp_path, "s3", "PreToolUse", NOW, name="RunnerB")
    prov = ClaudeStateProvider(path=str(tmp_path), now=lambda: NOW)
    frame = prov.poll(NOW)[0]
    assert frame.state == base.STATE_WAITING
    assert frame.label == "Waiter"


def test_state_file_without_a_name_is_normal(tmp_path):
    # Written by a shim older than this feature. Absent is not malformed.
    _write_state(tmp_path, "s1", "Notification", NOW)
    prov = ClaudeStateProvider(path=str(tmp_path), now=lambda: NOW)
    frame = prov.poll(NOW)[0]
    assert frame.state == base.STATE_WAITING
    assert frame.label == ""


def test_a_non_string_name_is_ignored_not_fatal(tmp_path):
    path = tmp_path / "s1.state"
    path.write_text(json.dumps({"event": "Notification", "t": NOW,
                                "name": {"not": "a string"}}))
    prov = ClaudeStateProvider(path=str(tmp_path), now=lambda: NOW)
    frame = prov.poll(NOW)[0]
    assert frame.label == ""
```

Extend the file's existing state-writing helper to accept `name=None` and include the key only when given.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/pc/test_state_machine.py -q`
Expected: FAIL with `AttributeError: 'NormalizedUsageFrame' object has no attribute 'label'`.

- [ ] **Step 3: Add the field to the frame**

`pc/providers/base.py`, after `n_agents`:

```python
    # Which project the ONE session in `state` belongs to, when there is
    # exactly one. Empty when several share the state -- see
    # claude_state.poll for why naming one of several is refused rather than
    # guessed. Only the hook-backed provider can ever set it; every other
    # source leaves it empty and the board falls back to the count.
    label: str = ""
```

- [ ] **Step 4: Return the name from `_read_state`**

```python
    def _read_state(self, path, now_epoch):
        """(state, age, name) for one session's slot, or (None, None, "")."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None, None, ""
        if not isinstance(payload, dict):
            return None, None, ""
        event = payload.get("event")
        if not isinstance(event, str) or not event:
            return None, None, ""
        try:
            t = float(payload["t"])
        except (KeyError, TypeError, ValueError):
            return None, None, ""
        if not (T_EPOCH_MIN <= t <= T_EPOCH_MAX):
            return None, None, ""
        # A name is optional and its absence is ordinary: a state file written
        # by a shim older than this feature has no key, and the shim omits it
        # whenever the payload's cwd did not survive sanitising.
        name = payload.get("name")
        if not isinstance(name, str):
            name = ""
        age = now_epoch - t
        return derive_state(event, age), age, name
```

- [ ] **Step 5: Group names by state in `scan`**

Change the docstring to `"""{state: n_sessions}, {state: [names]}, total live agents."""`, add `names = {}` beside `counts`, unpack three values from `_read_state`, and after the `counts[state] = ...` line:

```python
            if name:
                names.setdefault(state, []).append(name)
```

Return `counts, names, agents`. Update the early `return {}, 0` to `return {}, {}, 0`.

- [ ] **Step 6: Name only the unambiguous case in `poll`**

```python
    def poll(self, now_epoch):
        counts, names, agents = self.scan(now_epoch)
        if not counts:
            return []
        state = worst_of(counts)
        if state == base.STATE_UNKNOWN:
            return []
        # Named only when the winning state is held by exactly ONE session.
        #
        # Naming one of several is the mistake the context row was cut for:
        # "88% of 4" qualified one number into honesty and still did not say
        # WHICH. A count says something true about all of them; a name picked
        # from three says something true about one and implies it about the
        # rest.
        label = ""
        held = names.get(state, [])
        if counts.get(state, 0) == 1 and len(held) == 1:
            label = held[0]
        frame = base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=now_epoch,
            state=state,
            label=label,
            n_run=counts.get(base.STATE_RUNNING, 0),
            n_wait=counts.get(base.STATE_WAITING, 0),
            n_stuck=(counts.get(base.STATE_STUCK, 0)
                     + counts.get(base.STATE_FAILED, 0)),
            n_idle=counts.get(base.STATE_IDLE, 0),
            n_agents=agents,
        )
        return [frame]
```

- [ ] **Step 7: Run to verify they pass**

Run: `python3 -m pytest tests/pc/test_state_machine.py -q`
Expected: PASS.

- [ ] **Step 8: Run the whole Python suite**

Run: `python3 -m pytest tests -q`
Expected: all pass. `scan()` gained a return value, so any other caller fails loudly here rather than silently.

- [ ] **Step 9: Commit**

```bash
git add pc/providers/base.py pc/providers/claude_state.py tests/pc/test_state_machine.py
git commit -m "feat: the frame names its session, when there is only one to name"
```

---

### Task 4: A message of its own, sent on change and on connect

**Files:**
- Modify: `pc/protocol.py` (beside `status()` around line 437), `pc/bridge.py:74-95` (`greet`), `:412-428` (send site)
- Test: `tests/pc/test_protocol.py`, `tests/pc/test_bridge.py`

**Interfaces:**
- Consumes: `NormalizedUsageFrame.label` from Task 3.
- Produces: `protocol.session(label: str, n: int) -> dict` emitting `{"t": "session", "v": VERSION, "label": ..., "n": ...}` with `label` omitted when empty. Task 5 parses it.

- [ ] **Step 1: Write the failing tests**

`tests/pc/test_protocol.py`:

```python
def test_session_message_shape():
    m = protocol.session("LiveClaudeUi", 1)
    assert m["t"] == "session"
    assert m["label"] == "LiveClaudeUi"
    assert m["n"] == 1


def test_session_omits_an_empty_label():
    # Absent already means unknown on both sides, and every optional key on
    # this wire is omitted rather than sent as a sentinel.
    assert "label" not in protocol.session("", 3)


def test_session_caps_the_label():
    m = protocol.session("x" * 100, 1)
    assert len(m["label"].encode()) == 24


def test_session_label_survives_multibyte_truncation():
    # Cutting a UTF-8 sequence in half must not produce an undecodable field.
    m = protocol.session("\u05d0" * 40, 1)
    m["label"].encode()  # must not raise
    assert len(m["label"].encode()) <= 24


def test_usage_frame_did_not_grow(fully_loaded_usage_kwargs):
    # The frame was measured at 506 of 512 and proto.c drops an over-long
    # line whole. This is the regression that would freeze panels.
    raw = protocol.encode(protocol.usage(**fully_loaded_usage_kwargs))
    assert len(raw) <= protocol.MAX_LINE_BYTES
    assert "label" not in raw
    assert "\"proj\"" not in raw
```

Build `fully_loaded_usage_kwargs` from the existing worst-case fixture in that file if one exists; if not, construct it with every optional field populated — two providers, all per-model percentages, both ages, both countdowns.

`tests/pc/test_bridge.py`:

```python
def test_session_message_is_sent_when_the_label_changes(bridge, written):
    bridge.poll_once()
    first = [m for m in written() if m.get("t") == "session"]
    assert first and first[-1]["label"] == "LiveClaudeUi"

    bridge.poll_once()  # nothing changed
    assert len([m for m in written() if m.get("t") == "session"]) == 1


def test_session_message_is_resent_on_greet(bridge, written):
    # A board that just booted holds nothing. Without this a replugged board
    # shows a bare status until the next time the project happens to change --
    # the same reason firmware currency is re-offered on every connect.
    bridge.poll_once()
    written.clear()
    bridge.greet()
    assert any(m.get("t") == "session" for m in written())
```

Follow the fixture and capture style already used in `test_bridge.py`.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/pc/test_protocol.py tests/pc/test_bridge.py -q`
Expected: FAIL with `AttributeError: module 'pc.protocol' has no attribute 'session'`.

- [ ] **Step 3: Add the builder to `pc/protocol.py`**

Beside `status()`:

```python
# 24 bytes. STATUS_MAX_W is 300 px and usage_layout.h records
# "Reading is old - showing last known" (35 characters) as the string that
# sized it, so "Waiting for you - " leaves roughly 17 characters of room.
# This is a BYTE bound and the panel's is a PIXEL one; they are different
# questions and only this one can be answered here, which is why the firmware
# also sets LV_LABEL_LONG_DOT.
SESSION_LABEL_MAX_BYTES = 24


def session(label: str, n: int) -> dict:
    """Which project the board should name, and how many share the state.

    Its own message type rather than a field on the usage frame, and that is
    a measurement rather than a preference: the usage line was measured at
    506 of MAX_LINE_BYTES=512 fully loaded, proto.c drops an over-long line
    whole, and a label is more than six bytes. Additive like `edition` --
    firmware that predates it ignores an unknown type, so an older board
    keeps the behaviour it has today.

    Sent on change and on every connect, not every poll: the numbers move
    constantly and the project name does not.
    """
    msg = {"t": "session", "v": VERSION, "n": int(n)}
    if label:
        # Truncate on a CHARACTER boundary that survives the byte bound --
        # slicing bytes can halve a multibyte sequence and produce a field
        # that cannot be decoded at the other end.
        trimmed = label.encode("utf-8")[:SESSION_LABEL_MAX_BYTES]
        msg["label"] = trimmed.decode("utf-8", "ignore")
    return msg
```

- [ ] **Step 4: Send it from the bridge**

In `__init__`, beside the other last-sent trackers around line 43:

```python
        self._last_session = None        # (label, n) last sent; see poll_once
```

At the send site, immediately after `self._write(usage)` at line 428:

```python
        # The project name, on change only. It rides its own message because
        # the usage line above has six bytes of headroom.
        pair = (frame_label, n_in_state)
        if pair != self._last_session:
            self._write(protocol.session(*pair))
            self._last_session = pair
```

Derive `frame_label` and `n_in_state` from the same normalized frame the usage message was built from — the label is `frame.label`, and the count is the frame's `n_run`/`n_wait`/`n_stuck`/`n_idle` entry matching `frame.state`.

In `greet()`, after `self.poll_once()`:

```python
        # A board that just booted holds no session message, and poll_once
        # only sends on change -- so on a reconnect the tracker is what makes
        # it silent. Clear it and push, the same shape as offer_if_newer on
        # every connect rather than once per daemon lifetime.
        self._last_session = None
```

Move this so the clear happens *before* `poll_once()`, so the push occurs within it.

- [ ] **Step 5: Run to verify they pass**

Run: `python3 -m pytest tests/pc/test_protocol.py tests/pc/test_bridge.py -q`
Expected: PASS.

- [ ] **Step 6: Run the whole Python suite**

Run: `python3 -m pytest tests -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add pc/protocol.py pc/bridge.py tests/pc/test_protocol.py tests/pc/test_bridge.py
git commit -m "feat: the project name rides its own message, not the full usage line"
```

---

### Task 5: The board reads it and the line says it

**Files:**
- Modify: `firmware/src/proto.c` (dispatch chain, beside the `status` branch at 601), `firmware/src/usage_view.h`, `firmware/src/usage_view.c`
- Test: `tests/msg_parse/host_test.c`

**Interfaces:**
- Consumes: the `session` message from Task 4; `fmt_hint()` and the `session_label` / `session_n` statics from Task 1.
- Produces: `void usage_view_set_session(const char *label, int n)`.

- [ ] **Step 1: Write the failing test**

`tests/msg_parse/host_test.c` — assert the wire shape parses, since `usage_view.c` cannot be host-tested against LVGL:

```c
	/* The session message: an optional label and a count. */
	{
		char lbl[28] = "";
		double n = 0;
		const char *j =
			"{\"t\":\"session\",\"v\":1,"
			"\"label\":\"LiveClaudeUi\",\"n\":1}";

		EXPECT(msg_get_str(j, "label", lbl, sizeof(lbl)));
		EXPECT_STR(lbl, "LiveClaudeUi");
		EXPECT(msg_get_num(j, "n", &n));
		EXPECT_EQ((int)n, 1);
	}
	{
		/* Absent label is the normal several-sessions case. */
		char lbl[28] = "x";
		const char *j = "{\"t\":\"session\",\"v\":1,\"n\":3}";

		EXPECT(!msg_get_str(j, "label", lbl, sizeof(lbl)));
	}
	{
		/* A label longer than the buffer must truncate, not overrun. */
		char lbl[8] = "";
		const char *j =
			"{\"t\":\"session\",\"label\":\"abcdefghijklmno\"}";

		msg_get_str(j, "label", lbl, sizeof(lbl));
		EXPECT_EQ((int)strlen(lbl), 7);
	}
```

Use whatever numeric accessor `msg_parse.h` actually exposes; match the existing assertions in that file.

- [ ] **Step 2: Run to verify it fails or passes**

Run: `tests/ci/check_host_tests.sh`
Expected: passes if `msg_get_str` already truncates correctly; if the truncation case fails, that is a real pre-existing bug — fix it in `msg_parse.c` and keep the test.

- [ ] **Step 3: Declare the setter**

`firmware/src/usage_view.h`, beside `usage_view_set_activity`:

```c
/*
 * Which project the panel should name, and how many sessions hold the state.
 *
 * `label` may be empty, which is the ordinary case whenever several sessions
 * share the state -- the daemon refuses to pick one, so the line falls back
 * to the count. See pc/providers/claude_state.py.
 */
void usage_view_set_session(const char *label, int n);
```

- [ ] **Step 4: Implement it**

`usage_view.c`, beside `usage_view_set_activity`:

```c
void usage_view_set_session(const char *label, int n)
{
	if (label) {
		strncpy(session_label, label, sizeof(session_label) - 1);
		session_label[sizeof(session_label) - 1] = '\0';
	} else {
		session_label[0] = '\0';
	}
	session_n = n;
	/* Re-run the status switch so the line picks the new subject up. The
	 * dot is unaffected -- neither input to refresh_dot changed -- but
	 * set_status owns the composition and calling it is how the label is
	 * rebuilt. */
	usage_view_set_status(last_status);
}
```

- [ ] **Step 5: Parse it in `proto.c`**

Add a branch before the closing `/* unknown types ignored */`:

```c
	} else if (strcmp(type, "session") == 0) {
		/*
		 * Which project is doing the thing the dot is already
		 * colouring. Its own message rather than a field on `usage`
		 * because that line is at 506 of the 512 this parser accepts
		 * and an over-long one is dropped whole -- see
		 * pc/protocol.py:331.
		 *
		 * An absent label is the normal several-sessions case, not a
		 * parse failure: the daemon refuses to name one of several,
		 * and `n` carries the meaning instead.
		 */
		char lbl[28] = "";
		double n = 0;

		msg_get_str(json, "label", lbl, sizeof(lbl));
		num(json, "n", &n, 0, 9999);
		usage_view_set_session(lbl, (int)n);
```

Match the surrounding style for the numeric accessor — the `usage` branch uses `num(json, key, &out, min, max)`.

- [ ] **Step 6: Build**

Run: `tools/dev.sh`
Expected: builds clean.

- [ ] **Step 7: Run everything**

Run: `tests/ci/check_host_tests.sh && python3 -m pytest tests -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add firmware/src/proto.c firmware/src/usage_view.c firmware/src/usage_view.h \
        tests/msg_parse/host_test.c
git commit -m "feat: the line under the dot names the project it is talking about"
```

---

### Task 6: Flash it and watch it

Not optional and not a formality: in this project "done" means flashed to the board and boot-verified, not built. Every case below is one the host tests structurally cannot answer.

**Files:** none.

- [ ] **Step 1: Check the board before flashing**

Run the project's eFuse check before any flash — there are two units and `FLASH_CRYPT_CNT` decides which script is safe.
Expected: a clear answer for the unit in hand.

- [ ] **Step 2: Flash and boot**

Run: the normal flash path for this unit.
Expected: the board boots and the panel draws.

- [ ] **Step 3: Verify one named session**

With exactly one Claude Code session open in a project, trigger a permission prompt.
Expected: the dot goes amber and pulses, and the line reads `Waiting for you - <project>`.

- [ ] **Step 4: Verify the plural fallback**

Open a second and third session in different projects and get two of them waiting.
Expected: the line reads `Waiting for you - 3 sessions` and names nobody.

- [ ] **Step 5: Verify the case that started this**

Cause a turn to fail (`StopFailure`).
Expected: the dot is red **and the line says `Session failed`** — the blank line is the bug this whole plan exists for.

- [ ] **Step 6: Verify data health still wins**

Close Claude Code and wait for the reading to go stale.
Expected: the line reverts to `Reading is old - showing last known` and stops reporting execution state.

- [ ] **Step 7: Verify the reconnect push**

Unplug the board mid-session and plug it back in.
Expected: the project name is on screen again without waiting for a project change — this is the `greet()` path from Task 4, and it is the one most likely to be wrong.

- [ ] **Step 8: Verify a long name at desk distance**

Open a session in a directory with a name at or past 24 characters. Stand back 60 cm.
Expected: it ellipsizes on one line, never wraps onto the arcs, and the status half is still readable.

- [ ] **Step 9: Record the result**

Note what was verified and on which unit, in the release notes or `docs/next-steps.md`, following the existing convention for hardware-verified work.

- [ ] **Step 10: Commit any notes**

```bash
git add docs/
git commit -m "docs: hint line verified on hardware"
```

---

## Self-Review

**Spec coverage:** every section maps to a task — the four blank-line conditions and the copy table (Task 1), the wrap hazard and `LONG_DOT` (Task 1), `set_activity` not refreshing (Task 1), directory-name capture and the promise rewrite (Task 2), name-one-else-count (Task 3), the frame field (Task 3), the separate message and the 24-byte cap (Task 4), send-on-change-and-connect (Task 4), firmware parse and compose (Task 5), hardware verification (Task 6). The two explicit non-goals — Codex/desktop parity and always-name-the-most-recent — have no tasks by design.

**Type consistency:** `fmt_hint(status, label, n_sessions, buf, buflen)` is defined in Task 1 and called in Task 1 Step 8 and through `usage_view_set_session` in Task 5. `session_label` / `session_n` are declared in Task 1 Step 8 and written in Task 5 Step 4. `NormalizedUsageFrame.label` is added in Task 3 and read in Task 4. `protocol.session(label, n)` is defined in Task 4 Step 3 and parsed in Task 5 Step 5 with matching key names `label` and `n`.

**Known soft spots, called out rather than hidden:** Task 4 Step 4 says to derive the count "matching `frame.state`" without giving the mapping code, because the exact accessor depends on how the bridge holds the normalized frame at that point — the implementer should read `poll_once` and write it explicitly. Task 5 Step 1 leaves the numeric accessor to match `msg_parse.h`. Task 2's `check_name` helper is described rather than written, because it must follow harness conventions in `check_hook_shim.sh` that are better read than guessed.
