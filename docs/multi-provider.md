# The ingestion bus, as built

How usage gets from four places into two dials, and why each piece is shaped
the way it is. Read this before adding a provider, a source, or a field.

Implemented on branch `desk-hud-universal`. Companion to
`desk-hud-doc-gap-analysis.md`, which records what the handoff document asked
for and where this departs from it.

---

## 1. The shape

```
  Claude Code            Claude Desktop         Codex
  status line            plan-usage-history     rollout log
       |                        |                      |
  ~/.blink/              app's own cache        ~/.codex/sessions/
  statusline.json               |                      |
       |                        |                      |
  ClaudeCliProvider   ClaudeDesktopProvider     CodexCliProvider
  ClaudeStateProvider           |                      |
       |                        |                      |
       +------------------------+----------------------+
                                |
                          IngestionBus            pc/ingest.py
                                |
                          normalizer.select()     pc/normalizer.py
                          - group by provider
                          - merge field by field
                          - one frame wins
                                |
                     protocol.frame_to_usage()    pc/protocol.py
                                |
                        NDJSON over USB-CDC
                                |
                          proto.c -> usage_view.c
```

Four sources, three of them for Claude. Adding a provider means writing one
class and appending it to `default_providers()`; the normalizer, the protocol
and the firmware learn nothing new.

## 2. NormalizedUsageFrame

The only type that crosses from provider code into the normalizer.

| field | meaning | unknown is |
|---|---|---|
| `provider` | `"claude"`, `"codex"`, … | — (required) |
| `src` | `"cli"`, `"desktop"` | — (required) |
| `observed_at` | when the DATA was written, not when we read it | — (required) |
| `session_pct` / `weekly_pct` | rolling-window usage | `-1.0` |
| `session_resets_at` / `weekly_resets_at` | absolute epoch | `None` |
| `ctx_pct` | context window fullness | `-1.0` |
| `model` | display name | `""` |
| `state` | `idle` / `running` / `waiting` / `stuck` | `""` |
| `stale` | can we vouch for this reading | `False` |

`-1.0` and not `0.0`, everywhere. `0.0` renders as a confident "0% used",
which is a stronger claim than "we don't have this number". The same sentinel
survives all the way to `msg_get_double`'s default in the firmware, so a value
crosses every layer without being re-encoded.

`observed_at` is the field most easily got wrong. Reading a two-day-old file at
noon must not make it look like a noon reading; every freshness and conflict
decision below rests on this being honest.

## 3. What each source can and cannot tell you

|  | session % | weekly % | resets | context | model | state |
|---|---|---|---|---|---|---|
| CLI status line | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Desktop cache (`plan-usage-history.json`) | ✓ | ✓ | **✗** | — | — | — |
| Desktop Local Storage | ✓ | — | **session only** | — | — | — |
| Weekly anchor (rolled forward, §3c) | — | — | **weekly only** | — | — | — |
| Codex rollout log | ✓ | ✓ | ✓ | — | — | — |
| Hook state file | — | — | — | — | — | ✓ |

Two rows did not exist when this table was first written. Desktop Local
Storage and the weekly anchor are the two sources §3c describes; between
them they give a Claude-Desktop-only customer the countdowns the row above
still correctly says the desktop cache alone cannot.

No source is authoritative for everything. That table is the entire argument
for merging field by field rather than picking a winning source per poll — the
desktop cache has no reset timestamps at all, so letting it "win" because it is
newest would blank the only reset time anybody has.

Two source-specific traps, both discovered against real files and both absent
from the specification:

- **`samples[].t` in the desktop cache is MILLISECONDS.** Read as seconds it
  lands in the year 56649, every freshness check passes trivially, and any
  reading in history presents as current.
- **The desktop cache carries no reset timestamps.** Not "sometimes missing" —
  never present.
- **Codex's `resets_at` is SECONDS,** in the same daemon that has to read the
  desktop cache's milliseconds. Both are range-checked at the parser, which is
  the only place the unit is known.
- **`primary` / `secondary` in Codex's `rate_limits` are positions, not
  windows.** They are matched by their declared `window_minutes` (300 → the
  five-hour dial, 10080 → the seven-day one) and only fall back to position
  when that field is absent. Trusting the order would swap the two dials
  silently, which is the failure this whole document is written against.
- **A claude.ai tab is not a source, and that is settled.** A browser
  extension was built to read usage from response headers and measured against
  the real site: 178 responses, none carrying a rate-limit header of any
  spelling. It was removed. `docs/next-steps.md` section A keeps the finding.

## 3b. What Claude Desktop cannot tell you, and what we do instead

The ✗ in the table above is the whole reason section 4 exists, and it is worth
being precise about, because it looks like a gap someone should close.

**`plan-usage-history.json` contains no reset timestamps.** Not "sometimes
missing" — never present. Every one of its 1,672 samples (a real month of one
account) is `{t, org, u:{fh, sd[, xu]}}`: a millisecond timestamp, an org id,
a five-hour percentage, a seven-day percentage, and occasionally `xu`, extra
usage in dollars. Nothing else.

**Nor is a reset time stored anywhere else the app writes.** Searched
2026-08-28: every `.json` file under its support directory, its Local Storage
and Session Storage LevelDB stores, IndexedDB, WebStorage, `shared_proto_db`,
its caches, and `com.anthropic.claudefordesktop.plist`. The only copies of a
reset time on the machine are in Claude Code's status line payload and in
evicting HTTP cache entries from an endpoint the desktop app does not use.

**Corrected 2026-09-05: that survey missed the field it should have found.**
A closer read of the same Local Storage store turned up `resetsAt` beside the
`ochre_heron_tide` key -- the five-hour window's own reset, in plain JSON,
in a store with no conversation content. Cowork's IndexedDB records carry a
reset for both windows too, though only for Cowork. Neither changes what is
true of `plan-usage-history.json` itself, which still contains no reset
timestamp anywhere in it -- it changes what is true of Claude Desktop as a
whole. See §3c and `docs/research/claude-desktop-window-sources.md` for the
evidence.

**Deriving one from the sample series was investigated and refused.** It is
possible in principle: the five-hour window measures 5.00 h from the first
non-zero sample after a reset, and the server quantises reset times to a
10-minute grid. It is not possible in practice. The app records only while it
is open — 18% of that month had samples at all, the longest gap was 409 hours,
and only 13% of windows had an observable start. A method that works one time
in eight, whose failures are indistinguishable from its successes, is not a
method; and `pc/providers/base.py` forbids handing the board a confident
number that came from a guess.

**So the panel shows a rate instead.** `pc/providers/claude_desktop.session_burn_pph`
measures the slope of the five-hour percentage over the last 30 minutes and
answers `None` on any of: a gap over 10 minutes (the app was closed, and
averaging across unobserved time is the same mistake as deriving the reset), a
stale newest sample, a reset inside the window, fewer than 3 samples, under a
10-minute span, or a non-positive result. Refusing is the common answer.

It reaches the board as `burn_pph` and is drawn where the countdown would be —
**and only when there is no countdown to draw**. The normalizer carries it only
when no source supplied a session reset time, so a frame never holds both and
nothing downstream has to choose. The weekly gauge keeps `--`: a seven-day
slope measured over half an hour is noise.

## 3c. Closing the gap: Local Storage, the weekly anchor, and what stays closed

`docs/research/claude-desktop-window-sources.md` has the evidence -- the
exact stores, the exact byte formats, and the Cowork discriminator that was
first got wrong. This section is the summary a future provider author needs
before touching any of it, and the honest limits that go with it.

**Local Storage supplies the five-hour countdown.** `pc/desktop_local_storage.py`
reads the same store Local Storage always is: a Chromium LevelDB, keyed by
`ochre_heron_tide.<organization-uuid>`. The record carries `utilization` (a
fraction, not a percentage), `resetsAt` (Unix seconds, often already in the
past) and its own `observedAt`. It holds no conversation content -- its other
keys are onboarding flags, experiment toggles, activation checklists and an
analytics queue -- which is why this is the store it reads. Nothing about
`session_burn_pph` on the older desktop provider changes: it answers a
different question and keeps working when this source is absent or the
window has just rolled.

**The seven-day reset is genuinely unavailable on disk for a chat-only
customer.** Three independent investigations established that, not one:
every LevelDB store on the machine byte-scanned for the field names and the
known epochs, every legacy Cowork audit file, and deriving the boundary from
the weekly-percentage series (which brackets it to 45-108 hour windows,
because the sample file only records while the app is open, and cannot
produce a countdown). The research doc has the numbers. This is not a gap
the code closes by reading harder.

**So the seven-day boundary is an anchor, not a feed** (`pc/weekly_anchor.py`).
One instant, learned once and rolled forward in multiples of 604800 seconds --
two observed boundaries on the reference machine were exactly a week apart
and both landed on the same weekday and second, which is what makes rolling
forward valid. The anchor exists because the boundary needs one real
observation, ever, not because it can be read on demand.

Three seeders feed it, cheapest first, and that order is enforced by where
each one runs inside `IngestionBus.poll_frames()`. The first is the live-frame
learner, which runs every cycle. The other two are `pc/ingest.py`'s
`_seed_anchor_once`, which runs at the END of the same poll -- after the live
learner has had its chance -- and gets one attempt per process, taken only
while the anchor file is still empty. So on a machine running Claude Code the
status line writes the anchor first and neither of the other two is ever
opened; the pair below exists for the machine that has no such source:

1. **Any source that already publishes an exact weekly reset** -- ordinarily
   the Claude Code status line. `IngestionBus.poll_frames()` learns from
   every `claude`-provider frame each cycle, and only from `claude` frames:
   a Codex weekly reset must never become this anchor.
2. **A legacy Cowork audit file** (`pc/cowork_audit.py`). Plain JSON in a
   directory holding no chat store, but rare in practice -- 3 of 218
   `rate_limit_event`s on the reference machine carried the windows at all,
   and current managed Cowork sessions write no audit file whatsoever.
3. **Claude Desktop's IndexedDB** (`pc/desktop_idb.py`), tried last and only
   when the cheaper two have produced nothing. This is the only source in
   the project that reads a store holding the customer's conversations.
   **It has never successfully seeded an anchor, and should be treated as
   unproven.** Measured against the reference machine's live store on
   2026-09-05, with one Cowork and one Chat session deliberately open: the
   six qualifying records were found, `_wanted_key` matched every one, none
   was blob-wrapped -- and `v8_clone.parse` returned `None` for all six. The
   parser was written from format documentation and its fixture was written
   from the same reading, so it passes its own tests and refuses real
   records; the one bug already found in it (dense arrays) came from exactly
   that. Any tag it does not implement -- `Date` among them, which a 45 KB
   record from an Electron app is unlikely to avoid -- refuses the whole
   record. The failure is safe: `None` means no anchor, never a wrong
   boundary. But nothing downstream should assume this seeder contributes,
   and a machine relying on it gets no weekly countdown at all. Fixing it
   means decoding a real record end to end first, not reading the format
   documentation more carefully.

**The IndexedDB seeder is Cowork-only, in practice, and the discriminator is
the key, never the value.** `rate_limit_event` records carrying
`unifiedWindows` sat under IndexedDB keys prefixed `cowork:cse_...` on the
reference machine; plain chat conversations in the same store carried none.
Re-tested on 2026-09-05 with one Cowork and one Chat session deliberately
opened for the purpose: of eighteen conversation-sized values, all six under
a `cowork:` key carried `unifiedWindows` and none of the twelve chat values
did. (A first pass reported the opposite; it collapsed the UTF-16 keys at
the wrong alignment and labelled Cowork keys as chat. Classify on the key's
printable text, which is alignment-independent.) An
earlier investigation concluded these came from plain Chat and was **wrong**
-- the discriminator it used (no local session folder, no `audit.jsonl`)
proves nothing, because these managed sessions write neither. Provenance is
decided in `desktop_idb._wanted_key` by the IndexedDB key alone, never by
the value and never by anything else on the filesystem.

**A rolled-forward weekly reset rides the ordinary `weekly_resets_at` field
and is never marked "inferred" on the panel.** Doing that honestly would need
a new wire field and a reflash of every shipped board, which is out of scope
here. The withdrawal rules are the honest alternative: `weekly_anchor.py`
publishes a projection only while nothing contradicts it, refutes it outright
when a weekly-percentage drop lands more than 24 hours from the predicted
boundary (that drop's own resolution is measured in days, so it can refute an
anchor but is never trusted to confirm one), and withdraws it outright after
eight weeks with no fresh corroboration.

**Refusing beats guessing, at every layer of this path:**

- `pc/v8_clone.parse` returns `None` for a buffer it cannot parse, for a
  genuine `null`/`undefined`, and for an object back-reference alike -- V8
  never back-references a string, so nothing this project reads is lost by
  not resolving one, but the three cases are indistinguishable on purpose.
  `None` means absence, and it is never treated as `0.0`.
- **The parser refuses rather than guesses.** It implements only the tags
  Claude Desktop's own records use -- not the dense-array hole tag, and not
  BigInt, Date, Map, Set, typed arrays or any host object. A record using one
  of those is refused whole. That is deliberate: the one bug ever found in
  this parser came from assuming a tag's shape instead of verifying it, so an
  unknown tag ends the parse rather than guessing past it. The cost is
  availability, never a wrong number.
- `pc.leveldb.scan` deliberately does not implement MANIFEST or per-entry
  sequence-number ordering. It applies immutable tables and then logs, each
  in ascending file-number order -- correct while the log holds writes the
  tables have not yet compacted, the normal running state. A caller that
  must be certain which surviving copy is truly the newest reaches for
  `scan_all` instead and breaks the tie on the record's own timestamp, the
  way `pc/desktop_local_storage.py` does.
- **The blob-wrap rule fails closed.** A value beginning `\xff\x11\x01`
  externalises to a sibling `.blob` file this project never reads, and
  finding one abandons the WHOLE IndexedDB seeding attempt rather than
  falling back to an older, readable record. The externalised value is
  disproportionately the long-running session -- the one most likely to be
  current -- so serving an older sibling instead would be exactly the
  failure this plan exists to prevent: a stale weekly boundary presented as
  today's. No answer beats a stale answer.

**Nothing leaks.** `pc/desktop_idb.py` and `pc/cowork_audit.py` are the only
modules in this project that read a store containing conversation content,
and both carry the rule README.md:90 depends on: no buffer, byte range, or
decode failure's surrounding bytes is ever logged, printed, or placed in an
exception message. `tests/pc/test_no_conversation_leak.py` pins it against
both a well-formed record and a corrupt buffer -- the failure path is where
bytes escape, not the happy one -- and separately asserts that no fixture
under `tests/fixtures/` was ever captured from a real machine.

## 4. The merge rule

For each field independently: **among the sources that actually have this
field, the freshest wins** — and freshest is not enough on its own.

**A reading taken before a window reset cannot win, however fresh it looks.**
That was a real bug, reproduced by execution 2026-08-28. The status line is
rewritten only when Claude Code renders, so its post-reset 0% is routinely
OLDER than a desktop sample taken minutes before the same reset — and the
desktop cache cannot see reset times at all, so it has no way to know its
reading has been superseded. Strict recency handed it the dial, and the panel
showed a confident, un-stale 78% with a burn rate attached for a window that
had emptied a minute earlier: exactly the "inventing usage that has already
been forgiven" this section's rejected alternative was written against.

The fix is evidence, not a heuristic. `statusline_source` already knew the
epoch the window emptied — it is the `resets_at` being discarded — and now
carries it as `session_rolled_at`. `merge()` excludes any reading of that
window taken earlier, with one exception that has to be there: the frame that
REPORTED the rollover, whose own `observed_at` is the payload's mtime and is
therefore necessarily older than the reset it is describing.

**Context is the one exception, and it is not on this bus at all.** Several
agents mean several context windows and no single number is all of them, so
`ClaudeCliProvider` combines the sessions itself and takes the **worst** — the
fullest context is the one about to end somebody's turn. The count travels with
it as `n_ctx`, so the panel says "88% of 4" rather than letting one number pass
as the only one. That rule lives in the provider rather than the normalizer
precisely because it is not recency, and putting it here would have made the
normalizer's one rule into two.

The rejected alternative, kept as a test in
`tests/pc/test_normalizer.py::test_higher_does_not_beat_fresher_across_a_reset`:

> "Prefer the higher percentage, since no source can see claude.ai."

That holds within a window and inverts across a reset. A window that rolled
over a minute ago reads 0% from the fresh source and 90% from one taken just
before, and higher-wins would show 90% until the old reading aged out.
Understating a limit costs something; inventing usage that has already been
forgiven costs more.

Two consequences worth stating:

- **`src` names the source of the session percentage**, because that is the
  number on the largest dial. Labelling the panel with anything else would put
  a caption on a figure it does not describe.
- **`stale` follows that same source**, not the whole set (and describes that
  PROVIDER, not the panel — see §4c). A fresh desktop
  percentage beside an hours-old CLI reset time is a live panel, not a stale
  one — and `secs_until()` already refuses a reset that has passed.

Providers are never merged into each other. Two providers are two accounts with
two separate limits; `select()` chooses between them (preferred first, then
freshest) rather than blending. What a genuinely two-provider panel should look
like is a hardware design question, not one the normalizer may answer by
averaging.

## 4b. Two providers, two pages

**One provider per page.** The gauges show one provider's numbers at a time,
and a vertical swipe or a tap moves between them.

The first version put both on one pair of gauges: a second, inner ring per
gauge, with a coloured ball at each arc's tip saying whose it was. It worked
and it was still wrong. Two rings inside a 120 px circle leaves the hollow too
small for the two percentages that belong in it, identity had to be carried by
a fingernail-sized dot, and each countdown had to be labelled with a provider
name -- so the panel spent a colour channel, a dot and two words answering
"whose is this" on every element, continuously, for a question that has one
answer at a time.

A page answers it once. The provider's name sits at the bottom, the rings are
free to be rings, and the severity ramp -- green under 60%, amber to 90%, red
beyond -- is the only thing colour has to mean.

### Saying which page you are on, and getting to the other one

Three things, and each does one job:

- **The rail**, two marks along the bottom edge, position carried by WIDTH so
  colour stays free. Each mark is coloured by ITS OWN page's severity, which
  buys back the one thing splitting the providers cost: with both on one gauge
  you could see the second one going red without looking for it.
- **The pill**, naming the provider you are looking at. It is also the button
  that changes it -- the value IS the control, which is the idiom the settings
  panel's old "Main source" row used before pages replaced it. The fill only
  appears when there is a second page, because a control that looks live and
  answers nothing is worse than a label.
- **The gesture**, up or down. At the end of the stack either direction goes
  the only way it can, so with two pages both work. "Which direction is
  forwards" is a question a two-item stack does not have, and the ask was for
  a swipe that switches, not one that advances an ordered list.

The tap path is not a convenience. See §4d -- the panel is genuinely bad at
swipes, and left/right only ever felt reliable because a chevron sat behind
each one.

### The page change is the needle moving

Not a transition between two pictures. Three of those were tried -- a cut, a
wipe, and a wipe with a travelling edge -- and all three read wrong for the
same reason: the two pages are one layout with different numbers in it, so the
boundary between them has almost nothing to be made of.

This is an instrument, so the rings travel from the reading they were showing
to the other provider's, and the number under each counts along. Nothing is
covered or revealed.

What travels and what does not is decided by whether a midpoint exists:

| | |
|---|---|
| percentages | **travel** — a value between two values is a real value |
| severity colour | follows the value, so it changes at the threshold — blending green to amber goes through olive, which means nothing |
| provider name, countdowns | **cross the middle** — there is no midpoint between "Claude" and "Codex", and rolling `6d 22h` toward `4d 15h` invents a duration true of nothing |
| a blank reading (`--%`) | jumps — there is no path between a number and the absence of one |
| the rail | leads, then finishes under its own power (§4d) |

It is also the only motion this hardware renders smoothly, which is not a
coincidence: a full-screen transition costs one whole repaint however finely
it is chopped, while the arcs and their labels are a fraction of the panel and
LVGL invalidates only what moved.

### Beyond two

`select_pair()` drops the third rather than rotating through them, and
`RAIL_PAGES_MAX` is 2. A ring that silently changes whose number it is showing
is worse than one that never shows it.

## 4c. Freshness is per page

`stale` on the wire describes the FIRST provider. `p2_stale` describes the
second. Each page carries its own age and the "Reading is old" warning appears
only on a page it is true of.

One flag for both was a real bug, reported 2026-08-28. A machine that runs
Claude Code all day and touched Codex once that morning has a stale codex
reading and a live claude one -- and the claude page announced that its numbers
were old while they updated in front of the user. Exactly the frozen-meter
misreading the flag exists to prevent, pointed at the wrong page.

The mirror case was there too and is fixed with it: a fresh first provider
beside a stale second one left the status unarmed entirely, so the second page
claimed to be current. The board raises the status if EITHER page is old and
decides per page where to show it, which means `proto.c` sets it after BOTH
providers are parsed -- and still after `usage_view_update()` and
`set_models()`, which set OK internally and would overwrite it.

Both directions stay compatible. An older daemon sends no `p2_stale`, which
reads as fresh: the reading it sent IS the latest one it has, and the
alternative is a page permanently labelled old by a missing key. An older board
ignores a key it does not know. `p2_stale` rides with the rest of `p2`, so a
board is never given an age for a page it has not been told exists.

## 4d. The panel is bad at swipes, and that is physics

Worth reading before touching `firmware/src/ui_swipe.c`, because every
plausible-sounding fix in this area has already been tried and measured.

**A sliding finger loses contact.** Traced with `CONFIG_BLINK_TOUCH_TRACE`:
five deliberate swipes produced **thirty separate press-release cycles**, press
durations 17-140 ms, inter-report gaps running to 90 ms at the top decile and
779 ms at worst. The xpt2046 driver reports a release the first time it reads
PENIRQ deasserted, so one stroke arrives as five or six short presses. A
pressing finger does not do this; a sliding one does.

That defeats LVGL's own gesture detector outright, and not by a tunable margin:

- It resets its accumulator at every press boundary, so most strokes never
  reach a threshold.
- `gesture_min_velocity` does not mean what it sounds like — a sample that
  moved less than it in BOTH axes **zeroes the accumulated total**. LVGL
  samples faster than this panel reports, so a large share of ticks discard the
  stroke. The floor cannot go below 1, and at 1 a repeated identical point
  still trips it.
- Zephyr's `lvgl_pointer_input` queues every report and LVGL pops ONE per
  refresh. The panel reports every ~13 ms and LVGL drains every ~33, so during
  a stroke the queue fills and LVGL replays the touch in slow motion, falling
  further behind the longer it runs. Measured effect: strokes firing at 125-181
  px against a 36 px threshold — "I should swipe the entire screen".

So `ui_swipe.c` reads the panel's own reports through an input callback and
does four things LVGL cannot:

1. **Stitches across brief releases.** A gap under 250 ms is contact bounce
   mid-stroke, not the end of one. This is the single most important part; 120
   ms was too short and tore ordinary swipes into two refused halves. It
   re-arms in 80 ms once a swipe has fired, so a deliberate second swipe is not
   swallowed — patient while gathering, quick to reset once it has acted.
2. **Decides during the stroke**, with the finger still down, at 36 px of
   travel. Waiting for the release cost the whole stitch window and read the
   travel from the sample most likely to be short.
3. **Requires one axis to beat the other by 1.5x**, so a near-diagonal does
   nothing rather than picking the axis that won by a pixel.
4. **Publishes live progress** for the rail to draw, at the panel's rate.

The rules live in `ui_swipe_geom.h` as pure functions and
`tests/ui_swipe_geom/host_test.c` pins them to displacements actually recorded
on the board — a rule that stops admitting those has broken the panel it was
written for, whatever it does to invented numbers.

**Even so, it lands about half the time**, and the residue is strokes that were
genuinely small (15-22 px) or genuinely diagonal. A floor low enough to catch
those is low enough to turn a mis-aimed tap into a page change: the tap-like
strokes sit at 11-15 px, directly underneath. That is why every navigation on
this panel has a tap path, and why the vertical one finally got its own —
horizontal swipes miss at the same rate and only ever felt dependable because
a chevron sat behind them.

The rail's indicator follows from the same measurement. The finger cannot
supply enough samples to animate it — the window between the drag line and the
threshold usually contains none at all — so the finger starts the handover and
an animation finishes it.

## 5. The wire

Additive fields on protocol v2. **`PROTO_VERSION` did not move, and should
not.** `pc/version.py` sets the rule: the version is a floor that refuses, so
bumping it stops every deployed board being offered updates — over the same
link the update travels on. That is not a mistake that can be corrected
remotely.

New keys, in the order they were added: `provider`, `src`, `ctx_pct`, `model`,
`state`, the session/agent counts (`n_sess`, `n_run`, `n_wait`, `n_stuck`,
`n_agents`), the second provider (`p2`, `p2_session_pct`, `p2_weekly_pct`,
`p2_s_in_s`, `p2_w_in_s`, `p2_stale`), `edition`, and `burn_pph`. Unknown
values are **omitted, not sent as sentinels**, because of the budget below.

Two of those are collected and never displayed: **`ctx_pct` and `model` are
parsed by the daemon and read by nothing in the firmware.** The readouts were
removed from the panel — with several agents running there are several context
windows and no single number is any of them — but the fields stayed on the
wire, where they cost about 40 bytes of a 512-byte line. Either give them a
home or drop them; leaving them undocumented is how someone concludes the
panel is broken.

### The 512-byte cliff

The fully loaded two-provider frame measures **484 of the 512 bytes**, and
`pc/protocol.encode_checked` is what stands between that and the cliff. Note
that it was written, documented as the thing callers use, and then not used:
the daemon's only writer called plain `encode()` until 2026-08-28, so the
guard had no production caller at all and only tests exercised it.

`proto.c:367-371` does not truncate an over-long line. It drops it:

```c
} else if (line_len < LINE_MAX - 1) {
        line[line_len++] = (char)c;
} else {
        line_len = 0; /* overflow: drop the line */
}
```

No error, no partial parse, no sign on the panel — the board simply stops
updating while the daemon reports success. `protocol.encode_checked()` refuses
to write a line the board could not receive, and
`test_the_real_capture_fits_the_board_line_limit` pins the real payload against
the limit. A realistic message today is ~300 bytes. **Every field you add
spends this budget.**

## 6. Execution state, sessions and agents

Derived from events Claude Code already announces, not from process
inspection:

| event | state |
|---|---|
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStop`, `PreCompact` | `running` |
| `Notification`, `PermissionRequest` | `waiting` |
| `Stop` | `idle` - finished, the person's turn |
| `StopFailure` | `failed` |
| a `running` event, then silence past the threshold | `stuck` |
| `SessionStart`, `SessionEnd`, anything unrecognised, or an hour of silence | `""` (no claim) |

`idle` is a claim on the person's attention (amber on the panel), which is why
an opened-but-untouched terminal and an ended session both say nothing rather
than "idle" (2026-08-29; before that both were `idle`, and before that
`SessionStart` was `running` and went red three minutes after every `claude`).

**Severity**, worst first, in `base.SEVERITY`: `failed`, `stuck`, `waiting`,
`idle`, `running`. `idle` above `running` is the product decision that makes
the light useful: "one finished, two working" is "your turn", not "busy". The
wire layer (`protocol.frame_to_usage`) collapses BOTH providers with the same
order and sums their session counts, so the single pip is the worst of
everything on the desk, whichever page is in front.

**Codex** has no hooks. `CodexCliProvider` reads the same rollout log it reads
for usage: the newest `task_started` / `task_complete` / `turn_aborted` in each
file is that session's state, aged by the event's own timestamp with the same
thresholds. Every rollout votes (one session each) and the provider emits one
percentage-free state frame, exactly as `ClaudeStateProvider` does. No
permission event has been observed in the log, so Codex never reports
`waiting`; a prompt is `running` until answered, however long that takes.

`failed` earns its own state because `StopFailure` runs instead of `Stop` when
a turn dies on an API error and carries `error: "rate_limit"` among its causes.
On a usage gauge that is the headline, not a detail — which is also why
`worst_of()` ranks it above `stuck`.

`stuck` is no longer produced (2026-08-29; the protocol keeps the word). It was
inferred from silence, and every threshold cried wolf on the desk within a day:
60 s on a test suite, 180 s on a nine-minute Bash polling loop (the hooks say
nothing between `PreToolUse` and `PostToolUse`), 600 s on a seventeen-minute
think with the API connection open throughout. The hooks cannot distinguish a
long turn from a wedged one, so the daemon does not guess: a turn is `running`
until an event says otherwise, and a session that never says drops out after
`ABANDONED_AFTER_S`. Red is `failed` alone -- an event, not an inference.

### On disk

```
~/.blink/state/<session_id>.state      one JSON slot, newest event wins
~/.blink/state/<session_id>/<agent_id> one empty file per live agent
```

One file per **session** because a single global slot silently misreports the
moment a second terminal exists — two sessions overwrite each other and the
panel confidently shows the wrong one.

One file per **agent** because that makes the count exact without a lock. Two
agents starting at once cannot race on a shared counter, and `SubagentStop`
carries `agent_id`, so a stop removes precisely the agent that stopped rather
than decrementing and hoping. Session slots are swept by mtime after an hour,
for the sessions that die without `SessionEnd` firing; agent files after four,
because an agent file's mtime is its start and a long run is not a dead one.

### What the shim captures

An event name, a timestamp, a session id and an agent id. **Nothing else** —
no prompt, no tool arguments, no transcript path, no cwd, no message text.
`check_hook_shim.sh` asserts the other payload fields never reach disk.

The ids are a real widening. The first version captured an event name and a
timestamp, which made the metadata-only promise *structural* — there was
nothing there to leak. It is now a policy, and the reason for accepting that is
that the single slot was wrong the moment a second session existed.

The session id becomes a **filename**, which makes it the only attacker-shaped
input on a path in this product. The character class in the shim's extraction
pattern is the sanitiser rather than a separate validation step that can be
forgotten or reordered: a value containing a slash or a quote simply fails to
match and falls through to `unknown`. Traversal and injection are both pinned
in CI under sh, bash and dash.

### Counts, not a list

`n_sess`, `n_run`, `n_wait`, `n_stuck`, `n_agents` — and zeros are omitted. The
second provider adds `p2`, `p2_session_pct`, `p2_weekly_pct`, `p2_s_in_s`,
`p2_w_in_s` and `p2_stale`, all absent until a second provider reports.
`p2_s_in_s` / `p2_w_in_s` are deliberately short: the fully-loaded line is
close enough to the limit that spelling them out would cost more than they
carry.

`p2_stale` is the second provider's own age — see §4c. It rides with the rest
of `p2` rather than standing alone, so a board is never handed an age for a
page it has not been told exists.

Three fields were removed rather than added. `models` never reached the
firmware usefully. `ctx_pct` and `model` went with the widgets that showed
them — see below.

### What the panel deliberately does not show

**Context window.** It showed one, and with several agents running there are
several, at different levels, belonging to conversations the panel cannot name.
`"88% of 4"` was an attempt to qualify one number into honesty and it did not
earn its line: knowing the fullest of four contexts is at 88% does not tell you
which one, and there is nothing to do about it from across the room.

**The model in use.** It answered a question nobody glances at a desk gauge to
ask.

Both removals bought space for the gauges, which are what the panel is for. A
consequence worth stating precisely: the status line shim keeps one file, not
one per session — but that file is the **whole payload** Claude Code sends,
which carries the session id, working directory and transcript path alongside
the two figures. The daemon reads only `rate_limits` out of it; the file is
written readable by its owner alone; and the install disclosure and README say
what is in it. (An earlier version of this paragraph claimed the shim captured
no session id. It did not, and the claim has been corrected everywhere it was
repeated.) The hook shim records session and agent ids by design —
multi-session *state* is real and worth having.
A per-session array would blow the 512-byte budget at around four sessions,
taking the panel dark with no error on exactly the busy machine most likely to
have four. A busy machine measures 351 bytes; a typical one, 297.

The scalar `state` stays, computed as worst-of, so firmware that predates the
counts keeps working unchanged.

## 7. Self-healing

`install_statusline.drift_check()` restores the `statusLine` hook when
something wipes it. The rule that matters:

- **marker present, hook gone** → something wiped it. Put it back.
- **marker absent** → `uninstall()` removed it. That is intent, and it is never
  overridden.

A program that restores its own hook after being told to go away is not
self-healing. Three other refusals: it will not touch an unparseable
`settings.json`, it chains rather than clobbers a status line the user set
after installing, and after three restorations it says so and stops — a write
fight with another program on the machine is worse than a hook that stays
missing.

Polled on an interval, not watched. A file watcher would add a dependency the
daemon does not otherwise carry, for a fault measured in "since the last CLI
update" rather than in milliseconds.

## 8. Adding a provider

1. Subclass `base.ProviderParser` in `pc/providers/`.
2. Implement `get_provider_id()` and `poll(now_epoch)`; add `parse_cli_event`
   or `parse_cache_file` as the shape of your source warrants.
3. **Never raise.** The bus catches and disables a provider that throws, but a
   parser for an application we do not control is exactly the code that must
   not be able to stop a daemon whose job is keeping a board fed.
4. Return `-1.0` / `None` / `""` for anything you do not know. Do not guess,
   and do not return `0.0`.
5. Append it to `default_providers()`. **This step is not optional and it is
   the one that gets forgotten.** `IngestionBus.set_preferred()` refuses a
   provider that is not in that list, so a board whose settings screen offers
   "Codex" will announce that choice at every boot and have it declined, and
   `page_count()` on the firmware stays at 1 — a second page that exists in
   the firmware, in the protocol and on the settings screen, and never once
   appears. That was the state of Codex support until 2026-08-27, when
   `pc/providers/codex_cli.py` gave it something to report.

Nothing else changes — not the normalizer, not the protocol, not the firmware.
That is the property the whole structure exists to have.
