# Fleet testing

CI proves the pieces. It cannot prove the product, because no runner has a
board on a USB cable, an account signed in, or a login service that has been
running since somebody's last reboot. This suite does: it drives the **real
daemon** against a **real board** on three desks -- a Mac, an Ubuntu machine
and a Windows 10 machine -- and `tools/release.sh` refuses to build a release
until it has.

One run stops the installed service on three machines and takes their boards
for the best part of ten minutes. That is the cost, and it buys the only
evidence anyone has that a tag runs on hardware.

## The one command

```bash
python3 tools/fleet/run.py
```

Needs Python 3.11 or newer (it reads the inventory with `tomllib`), and can be
started from anywhere -- it resolves this checkout from its own path.

For each desk it takes a snapshot of **HEAD** (`git archive` piped into `tar -x`
over ssh), deletes the previous `result.json` on the far side in the same
command that unpacks the snapshot, runs `python -m tests.fleet.agent` there,
and copies the result back. The three desks run at the same time -- one board
each, no shared state. The verdicts are printed as a table and written to
`.fleet/last_run.json`, which is the file the release gate reads.

HEAD, not the working tree. Uncommitted edits are not on the desks and are not
what was proved, so commit first.

### What happens on one desk, in order

1. **The install and update scenarios**, if bundles were named. Neither goes
   near the serial port, so they run before anything is taken away from
   anybody, and a desk with its board unplugged still answers for them.
2. **The installed service is stopped.** Only one process can hold a serial
   port. Nothing is installed, moved or deleted -- a plist, a unit file and a
   Scheduled Task are all left exactly as they were found.
3. **A preflight pass**: a daemon with no scenario, up to three attempts of 15
   seconds, waiting for one message the board sends of its own accord.
   `launchctl bootout` returns before the port is actually free, so the first
   refusal is not taken as an answer. This pass also warms the run's release
   manifest cache, which is why the firmware Install prompt (below) appears at
   most once.
4. **The scenarios**, in name order: `overage`, `sleep_wake`, `stale_age`,
   `usage_climb`.
5. **The real-account pass**, unless `--no-real-account` was given.
6. **The service is started again**, from a `finally`, on every path including
   the ones that raise.

### How long it takes

The four scenarios are about four minutes of wall clock, of which `sleep_wake`
is roughly half: it contains a deliberate 45-second silence, because that is
the only thing that makes this board sleep. With the preflight and the
real-account pass a desk is busy for five to eight minutes, and the desks run
in parallel, so that is roughly the length of the whole run. Bundles add a few
minutes more per desk -- unpacking 50 MB and an installer that self-tests the
copy it made.

The orchestrator allows two hours per desk (`--timeout`) before it gives up on
one. That is generous on purpose, and it is also arithmetic rather than a round
number: the customer-path scenarios run first and allow seven minutes per
program call, so the whole of that phase has to fit inside the desk's budget
with the four board scenarios still to come. A per-desk timeout is reported as
a failed release, and slow is not broken.

### Seeing the commands without running anything

```bash
python3 tools/fleet/run.py --dry-run
```

```
galit-win10 (windows, galit@lenovo-r90r7u44.lan):
  push: ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', 'galit@lenovo-r90r7u44.lan', 'mkdir "%USERPROFILE%\\blink-fleet" 2>nul & del /q "%USERPROFILE%\\blink-fleet\\result.json" 2>nul & tar -x -f - -C "%USERPROFILE%\\blink-fleet"']
  run : ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', 'galit@lenovo-r90r7u44.lan', 'cd /d "%USERPROFILE%\\blink-fleet" && python -m tests.fleet.agent --board codex --out result.json --real-account']
  pull: ['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', 'galit@lenovo-r90r7u44.lan:blink-fleet/result.json', '<temporary file>']
...
Nothing was run and no desk was contacted.
```

It contacts nothing and, alone among the run modes, leaves
`.fleet/last_run.json` alone -- every other run deletes it before it starts, so
that a run which dies halfway leaves no file rather than last week's green one.
The Windows line is the reason this flag exists: it is built for a shell that
cannot be tested anywhere but on that desk.

### Narrowing a run

| Flag | Means |
|---|---|
| `--only local-mac,kfir-ubuntu` | These desks, by their inventory names |
| `--scenarios overage,stale_age` | These scenario names, on every desk |
| `--state .fleet/scratch.json` | Write the verdict somewhere else |
| `--no-real-account` | Skip the pass against each desk's own account |
| `--timeout 3600` | Seconds to allow one desk |

Both narrowing flags are recorded in the result, and **the release gate refuses
a run that used either** -- see [The release gate](#the-release-gate). Use
`--state` whenever you narrow, so an experiment does not delete the verdict
from the full run you meant to release on.

Exit status is 0 when the fleet passed, 1 when it did not, and 2 for an
inventory that will not load or an `--only` naming a desk that does not exist.

### Where the evidence lands

Only `result.json` comes home. Everything else stays on the desk that produced
it, under the workdir named in the inventory:

```
~/blink-fleet/result.json                      # the verdict, copied back
~/blink-fleet/fleet-work/overage/tap.jsonl     # every message on the wire
~/blink-fleet/fleet-work/overage/tap.jsonl.log # the daemon's own output
~/blink-fleet/fleet-work/preflight/            # the settle attempts
```

When a desk goes red and its sentence is not enough, those two files beside
each other are the whole story: what crossed the wire, and what the daemon
thought it was doing at the time.

**Everything under `fleet-work/` is from the run you are looking at.** The
workdir survives between runs, but each pass empties its own directory before
its daemon starts -- transcript, daemon log and all -- so what is on the disk
is what this run's daemon wrote and nothing else. The verdict is a count over
those records, and a transcript that accumulated across runs would let a desk
whose board was unplugged pass on last week's evidence: a daemon that writes
nothing at all is exactly what an unplugged board, a held port and a wedged
daemon all look like from here. Copy a transcript elsewhere if you want to
keep it; the next run will not.

The one thing that spans two daemons is `sleep_wake`, which is one directory
and one transcript for both of its passes -- the wake has to be readable in
the same file as the frames either side of it.

## The inventory

`tools/fleet/fleet.toml` is one `[hosts.<name>]` table per desk, and **one
entry per machine**: two entries sharing an `ssh` line, or two with none
(which both mean this machine), are refused, because the desks run in
parallel and two agents on one machine would stop and start the same login
service, unpack into the same workdir and compete for the one serial port --
with the loser's failures reading as a dead board. Every field is
required, and a malformed inventory is refused before anything is touched --
the alternative is stopping two desks' services, discovering the third has no
`python` line, and leaving somebody's board dark over a typo.

| Field | Means |
|---|---|
| `ssh` | The ssh destination. **Empty means this machine**, which is then run locally with no ssh at all. |
| `os` | `darwin`, `linux` or `windows`. Windows means the remote shell is `cmd.exe`, which changes how every command line is built. |
| `board` | `claude` or `codex` -- which edition of the board is on that desk. |
| `python` | An interpreter on that machine **that has pyserial**. |
| `workdir` | Where the snapshot is unpacked and the agent runs. Created if absent; it survives between runs, though the transcripts under it do not -- see [Where the evidence lands](#where-the-evidence-lands). |
| `has_claude_desktop` | Recorded in that desk's result for the reader. The orchestrator does not act on it. |

There is deliberately **no port** in this file. The daemon finds the board by
USB vendor and product ID (CH340, `0x1A86:0x7523`), which is how it picks COM15
out of the Windows desk's twelve mostly-Bluetooth COM ports. A port written
down here would work until somebody moved the cable to the next socket, and
would then fail as a dead board. `run.py` has no `--port` flag for the same
reason.

`python` is named per desk rather than assumed, because the agent spawns the
daemon and the daemon imports `serial` at module scope: on the Mac only
`.venv-test` has pyserial, and a system Python would fail as a board fault.
Checking it is a one-liner per desk and is worth doing before blaming the
hardware:

```bash
/usr/bin/python3 -c "import serial; print(serial.__version__)"
```

A relative `python` path on the **local** desk is resolved against this
checkout, not the workdir -- the fleet venv lives in the source tree and is
gitignored, so it is not inside the snapshot that gets pushed.

### Moving a board between desks

Edit `board =` on both desks. Nothing else: no port, no serial number, no
per-desk scenario.

The `board` values carry a `# VERIFY before the run` comment because they are
the one thing in the file the suite cannot check for itself. A `codex` value
makes the agent replay the same timeline attributed to Codex, in a copy of the
scenario file; the board applies whatever frame it is given and prints its
`[usage] session ...` line either way. So a stale `board` value does not go
red -- it quietly proves the wrong page. That is a job for the person at the
desk, which is also why the panels are worth watching while a run goes by.

## Scenarios

A scenario is a JSON file in `tests/fleet/scenarios/`. Its file stem is its
name everywhere: in `--scenarios`, in the result table, and in the problem
sentences. `BLINK_SCENARIO` replaces the daemon's entire provider set with the
one that replays the file, so a real Claude install on the desk cannot merge
its own readings into the timeline and change what the assertion means.

Here is `stale_age.json`, with its `note` shortened:

```json
{
  "name": "stale_age",
  "note": "Percentages hold constant across all three steps -- the subject is age, not movement. AGE_CAPTION_MIN_S is 600s (firmware/src/usage_view.c), so the two aged steps sit well past it (30 min, 4 hours). This path has shipped twice and never been confirmed on real hardware; it matters more than the other three files.",
  "steps": [
    {"at": 0,  "provider": "claude", "session_pct": 55.0, "weekly_pct": 30.0, "state": "running", "age_s": 0,     "stale": false},
    {"at": 6,  "provider": "claude", "session_pct": 55.0, "weekly_pct": 30.0, "state": "running", "age_s": 1800,  "stale": true},
    {"at": 12, "provider": "claude", "session_pct": 55.0, "weekly_pct": 30.0, "state": "running", "age_s": 14400, "stale": true}
  ],
  "duration_s": 20,
  "expect": {"min_tx": 3, "min_board_usage": 3, "min_stale_lines": 2, "min_sleep_wakes": 0}
}
```

| Key | Means |
|---|---|
| `name`, `note` | For the reader. `note` is where the reason this file exists goes. |
| `steps[].at` | Seconds since the provider was constructed. A step fires **once**, when elapsed time reaches it: a step is an event, not a level. One step per poll, oldest first -- a poll becomes a single usage message however many steps have come due, so the replay delays a late timeline rather than collapsing it. |
| `steps[].age_s` | How old the reading claims to be (`observed_at = now - age_s`), so a step can be four hours old the instant it fires. Defaults to 0. |
| Other `steps[]` fields | A usage frame as plain JSON -- `provider`, `session_pct`, `weekly_pct`, `state`, `stale`. A field name that does not exist on the frame drops that step, with a line on stderr, rather than taking the daemon down on three machines. |
| `duration_s` | How long the daemon runs. The agent adds its own grace on top for the last frame to be polled, sent and applied. |
| `host_silence_s` | Seconds with the **daemon stopped** in the middle. Only a scenario that has this sleeps. |
| `wake_duration_s` | How long the second daemon runs, after the silence. |

### The expect block

All four keys are required and none of them defaults. A key that fell back to
zero when it was missing would delete its own assertion -- a `stale_age`-shaped
run with no STALE lines in it once passed while `min_stale_lines` was merely
absent. A scenario that means to assert nothing on a dimension writes an
explicit `0`, where a reader can see the decision -- except for `min_tx` and
`min_board_usage`, which must be at least 1: a block asking for zero frames
sent and zero applied would be satisfied by a board that never woke up, and is
refused as a scenario that asserts nothing.

- **`min_tx`** -- usage frames that actually went out. Counted as `tx` records
  whose message type is `usage` **and** which the daemon reported writing. The
  `time` message every poll also sends does not count, and neither does a frame
  the daemon refused: `send()` drops a line over the board's 512-byte limit, and
  a loaded two-provider frame already measures 484, so a refused frame is a live
  failure mode. It never reached the board, and counting it would credit the
  board for work it was never given.
- **`min_board_usage`** -- `[usage] session ` lines the **board** printed on its
  own console. There is no ack in the protocol: the board's entire host-bound
  vocabulary is `hello`, `ping`, `pref`, `ota_query` and `ota_flash`, and none
  of it is sent per frame. The firmware prints that line once per applied frame,
  unconditionally, and it is the only end-to-end evidence there is.
- **`min_stale_lines`** -- how many of those applied lines carried `STALE`.
- **`min_sleep_wakes`** -- how many times `[sleep] host back; opening eyes` was
  followed, later in the transcript, by an applied frame. Both halves are
  required because both halves are the feature: a board that wakes and shows
  nothing has failed exactly as badly as one that never wakes.

Sleep is a daemon-**lifecycle** event, not a data event. The firmware stamps
its last-host time on any host protocol line and the daemon answers every
ten-second ping with a pong, so no poll interval leaves the board 30 seconds
silent while a daemon is alive. Quiet data is not a quiet host. That is why
`sleep_wake` is run in two passes with the daemon killed in between, and why
its `min_tx` is twice its step count: the second daemon replays the timeline
from its own start.

### Adding one

1. Write the file, named after what it proves -- but not `home`, `preflight`,
   `real_account`, `fresh_install` or `update_path`, which are directories the
   run makes for itself and are refused as scenario names.
2. Keep steps **at least 4 seconds apart**, which is longer than a fleet run's
   3-second poll. The provider hands the daemon **one step per poll**, so
   steps closer together than a poll cycle still all reach the board but
   later than the file says, and `duration_s` stops describing what actually
   happens. A daemon kept waiting for a busy port starts its timeline early
   for the same reason; the agent reports a run whose first traffic came more
   than 4 seconds late as *inconclusive* rather than failed, and that
   tolerance is what the spacing has to survive.
3. Set `duration_s` past the last `at`. The daemon polls every 3 seconds during
   a fleet run (the shipped interval is 60, at which a thirty-second scenario
   would produce one frame and never reach the sequence under test).
4. Write all four `expect` keys, counting `min_tx` yourself -- and doubling it
   if the scenario has a `host_silence_s`.
5. Try it on one desk before it goes to three:

```bash
python3 tools/fleet/run.py --only local-mac --scenarios my_scenario \
    --state .fleet/scratch.json
```

A name that matches no file is refused rather than skipped, on both sides: a
typo in `--scenarios` would otherwise produce a green result that proves less
than the operator believes it does.

## The real-account pass

Every scenario replays an invented timeline. That proves the wire and the
firmware and says nothing at all about whether a desk can read the tools
installed on it. So one pass per desk runs with **no scenario and the
operator's real home directory** -- the only time in the run that the daemon
sees the real account.

**Each machine has to be signed in** to whatever it is supposed to be reading:
Claude Code, Codex CLI, and Claude Desktop on the desk whose inventory entry
says `has_claude_desktop = true`. A desk that is signed out fails this pass and
only this pass, which is exactly the signal wanted -- the other four scenarios
would sail past it.

It asserts three things:

1. Within 120 seconds, a usage frame carrying a real `session_pct` or
   `weekly_pct` left the host.
2. At least one frame sent and at least one `[usage] session ` line back from
   the board.
3. Afterwards -- not alongside, so it is not competing for the serial port --
   `blink status --wire` prints one line that parses as a JSON object. That is
   the command a support conversation starts with, and on the Windows desk its
   message carries a path through a non-ASCII profile name, which is the
   decode error that once left a whole machine with no figure on its board.

It does not assert any particular number. The percentages come from a live
account, so no count of stale lines and no sleep window can be asked of it
either.

## The install and update pass

Two more scenarios run when bundles are named, and neither touches the board.
They prove the part of the product a customer meets before the panel ever
lights up, and the part no amount of running from a source checkout exercises.

- **`fresh_install`** -- unpack the release archive with the machine's own tar
  or unzip, check the unpacked program reports the expected version, run
  `blink install` into a sandbox home, and check the copy it left at
  `~/.blink/bin` runs and reports the same version. The third question is the
  reason the scenario exists: an installer that prints its way to a cheerful
  ending while leaving a program that will not start is not hypothetical here.
- **`update_path`** -- install the **previous** release, then have the
  **installed** copy run `blink update` against a local feed, and check the
  installed program afterwards reports the candidate version. Running the
  update from the installed copy is the whole point: `update.apply` renames the
  directory the running executable is inside of, and run from an unpacked
  bundle that rename is of a directory nothing is running from -- which cannot
  fail the way Windows fails.

Both run in a sandbox with `HOME` **and** `USERPROFILE` redirected, with every
inherited `BLINK_*` variable swept out of the environment, and with
`BLINK_SKIP_SERVICE` set in the child so that nothing registers a login agent
on somebody's desk.

### Where the bundles come from

**A fabricated manifest cannot drive the update.** `fetch_signed_manifest`
verifies the manifest's signature against the public key compiled into the
shipped binaries (`pc/update.py:196-198`) and returns `None` when it does not
verify. Handed a hand-written `manifest.json`, `blink update` therefore prints
"Could not read the release feed, or it is not properly signed. Nothing was
changed." and exits 1 -- which the scenario reports as it stands, so a red run
says what was wrong with the feed rather than leaving somebody to guess. The
feed has to come from a genuinely signed release.

`tools/release.sh` produces one without publishing anything:

```bash
BLINK_RELEASE_DRAFT=1 tools/release.sh
```

That builds and signs everything, attaches it to a **draft** release, and
stops. Then, on each desk, fetch that platform's files into the workdir:

```bash
cd ~/blink-fleet
gh release download v1.2.6 --repo KfirLevy258/Blink -D feed \
    -p manifest.json -p manifest.json.sig -p blink-macos-arm64.tar.gz
gh release download v1.2.5 --repo KfirLevy258/Blink -D bundles/previous \
    -p blink-macos-arm64.tar.gz
```

The feed directory has to hold all three of `manifest.json`,
`manifest.json.sig` and this platform's archive; the agent checks that before
it runs anything, because the far more common mistake is a directory with the
archive but no `.sig`, where `blink update` quietly refuses the feed and the
run reads as a broken update path.

### Running them

| Flag | Means |
|---|---|
| `--bundle` | The candidate release's archive for that desk's platform. |
| `--prev-bundle` | The release before it. Refused if it already reports the expected version, because `blink update` answers "Already up to date." with exit 0 and the scenario would pass without updating anything. |
| `--ota-dir` | The feed directory. Required with `--prev-bundle`. |
| `--expect-version` | The version both scenarios must end at. Required with either bundle flag -- the version is the entire claim. |

These three paths are forwarded **verbatim and interpreted on the target
desk**, and release archives are per-platform: `blink-macos-arm64.tar.gz`,
`blink-linux-x86_64.tar.gz`, `blink-windows-x86_64.zip`. One `--bundle` string
therefore cannot name the right file on all three machines. A *directory* name
can be shared (relative paths resolve against each desk's workdir, so
`--ota-dir feed` works everywhere), but an archive name cannot, so the
customer-path scenarios are run one desk at a time:

```bash
python3 tools/fleet/run.py --only galit-win10 --state .fleet/galit-bundles.json \
    --bundle bundles/candidate/blink-windows-x86_64.zip \
    --prev-bundle bundles/previous/blink-windows-x86_64.zip \
    --ota-dir feed --expect-version 1.2.6
```

Two consequences worth being plain about. A `--only` run is refused by the
release gate, so these runs prove the customer path but do not license a
release by themselves -- read their table and keep them. And `--state` is not
optional decoration: without it this run would delete the full fleet run's
verdict on its way past. Do the per-desk bundle runs first, or write them
somewhere else, and finish with the full run the gate will read.

## The release gate

Before it builds anything, `tools/release.sh` runs:

```bash
python3 -m pc.fleet_gate .fleet/last_run.json "$(git rev-parse HEAD)" \
    tools/fleet/fleet.toml
```

You can run exactly that by hand from the checkout root. It exits 0 and prints
one line when the commit may be released, or exits 1 with the reason on stderr;
with no arguments it prints its usage and exits 2.

It insists on all six of:

1. The run's own verdict is `ok: true`, with no top-level problems.
2. It names **this** commit. The desks proved code, and a run for another
   commit proved other code.
3. It finished within the last **twelve hours**. Long enough to prove a commit
   in the morning and release it after lunch; short enough that a run from
   before yesterday's rebase cannot vouch for today's desks, which drift on
   their own -- an unplugged board, a Windows update, a daemon left stopped.
4. It was not narrowed with `--only`.
5. It was not narrowed with `--scenarios`.
6. Every desk named in `tools/fleet/fleet.toml` is present and green.

The last three are why the gate is not simply a `grep ok`. `run.py --only
kfir-ubuntu` exits 0 and writes `ok: true`, and is *right* to -- that run did
pass -- so a single-desk result is both honest and nowhere near enough to ship
on. The desk list is read from `fleet.toml` rather than from the result,
because a file cannot vouch for its own completeness.

It refuses whenever it **cannot** check: no file, an unreadable one, a damaged
one, a missing inventory, or a Python too old for `tomllib` (`release.sh` calls
plain `python3`, and `/usr/bin/python3` on this Mac is still 3.9). Silence from
a gate must never read as approval.

### Releasing without it

```bash
BLINK_SKIP_FLEET=1 tools/release.sh
```

prints, and means:

```
WARNING: BLINK_SKIP_FLEET=1 -- v1.2.6 is being built with no proof
         that it runs on any board. Nothing below checks that.
```

Nothing below it checks. The firmware in that release goes to every board in
the field, signed with a key that exists only on this Mac, and the fleet run is
the last thing between a bad build and all of them.

## Troubleshooting

| What you see | What it is | What to do |
|---|---|---|
| `PermissionError 13` opening `/dev/ttyUSB0`, every scenario red on `kfir-ubuntu` | That user is not in the `dialout` group, and the device is `crw-rw---- root dialout`. A healthy board looks broken. | `sudo usermod -aG dialout kfir`, then re-login **on that machine**. A new ssh connection picks up the group; the desktop's own login service does not until that session restarts. |
| `PermissionError(13, 'Access is denied.')` on COM15 | On Windows this normally means the installed daemon still holds the port -- which is why the agent stops the service first. Seeing it means the stop did not take. | Check the Scheduled Task is really stopped, and that no daemon from an earlier run survived. Do not add a port to `fleet.toml`: that desk has twelve COM ports, eleven of them Bluetooth, and the daemon finds the right one by USB vendor and product ID, not by name. |
| The run aborts naming `BLINK_SKIP_SERVICE` | That variable is set in the operator's shell, which would make the service stop a silent no-op -- the real daemon keeps the port, ours is refused it, and a healthy board is reported as a hardware fault. | `unset BLINK_SKIP_SERVICE` and start again. Nothing was run and the service was left alone. |
| "No board message arrived within 15s on any of 3 attempts" | Either the port is still held by the service that was just stopped, or no board is attached. The message deliberately does not guess: those two look identical from here. | Look at the desk. |
| "the daemon's first traffic came 7.2s after it was started" | The timeline was shifted by that much, so its last steps may not have been reached before the daemon was stopped. The run is reported as *inconclusive*, not as a failed board. | Run it again rather than reading anything into it. |
| "Nothing was collected from `<desk>`" | The run was cut off from this end, which says nothing about what it left behind on that end. | Check that desk by hand before trusting it: a run cut off mid-scenario can leave the installed service stopped and that board dark. |
| The agent exited `9009` | `cmd.exe` saying it found no `python`. | Fix the `python` line for that desk in `fleet.toml`. |
| ssh fails at once instead of hanging | `BatchMode=yes` turns a passphrase prompt into an immediate error, on purpose -- a desk sitting at a prompt would hold the release open forever. | Load the key into an agent, or use one without a passphrase. |
| The gate says the run is "too old" or "for commit `<x>`, HEAD is `<y>`" | The result on disk is real, but not about this build. | Run the fleet again on this commit. |

### Output that reads like a failure and is not

**`[ingest] board asked for provider 'claude', which is not reporting; keeping
'claude'`** in a scenario's `tap.jsonl.log`. The board announces the provider
its settings screen is set to with every hello, and under a scenario the whole
provider set has been replaced by the one that replays the file, whose id is
`scripted` -- so the announced preference never matches anything and the
daemon says so. It is harmless: with a single provider reporting,
`select_pair()` returns it as the primary anyway, so the frame under test is
still the one on the big number. On a `codex` desk the same line names
`'codex'`.

**Probes of devices that are not boards.** Under `BLINK_TAP` on a multi-port
host the transcript records the daemon walking its candidate list, so the first
records of a pass are routinely a foreign device being greeted seconds before
the real board answers. The agent knows this and measures its connect delay
from wire records only.

**A firmware Install prompt on a panel.** The run's sandbox home starts with no
cached release manifest, so the daemon offers the current firmware on its first
hello. It needs a tap, so nothing flashes unattended; the shared sandbox home
and the preflight pass exist partly so this happens at most once per run
instead of once per scenario.

## What a green run proves

- Usage frames really left the host -- counted from what the daemon reported
  writing, so a frame refused for the board's 512-byte line limit is not
  credited to anybody.
- The board applied them and said so **in its own words**: one
  `[usage] session ...` line per frame, printed by the firmware, read back off
  the same wire.
- The board slept for real -- 45 seconds with no host at all -- woke, and put a
  fresh frame back on the dashboard.
- On that operating system, the archive we publish unpacks, installs, and the
  installed copy runs; and the previous release can replace itself off a
  properly signed feed, rotating a directory it is running from.
- That desk can read the account it actually has, and `blink status --wire`
  answers in something a support conversation can parse.

## What it does not prove

**It does not correlate values.** A green `overage` proves three frames were
sent and three were applied. It does not prove that `102%` reached the panel.
`check()` counts records and matches markers; it never compares a number sent
against a number displayed. Firmware that clamped 102 to 100, or swapped the
session and weekly rings, would pass every scenario in this suite.

**It does not verify pixels.** Nothing here looks at the screen. The age
caption is the sharpest case: it is drawn to the LCD and never printed, so
`stale_age` can prove the daemon sent a four-hour-old reading, the board
applied the frame, and it printed `STALE` -- and still say nothing about
whether the caption appeared, where, or in what words. That path has shipped
twice and has never been confirmed by eyes on a panel.

Pixel-level verification was deliberately left out of scope: it needs a camera
rig and a comparison that goes red on a font change, and the cost of a flaky
check inside a release gate is that people learn to re-run it until it is
green, which ends the gate.

**It does not prove the right board is on the right desk.** `board =` is a
written claim, and a wrong one goes green on the wrong page.

**It says nothing about desks it did not run.** That is the gate's entire
reason for reading `fleet.toml` instead of the result file.

So watch the panels while the run goes by. That is not ceremony: several of the
things this suite cannot see are the things most likely to be wrong.
