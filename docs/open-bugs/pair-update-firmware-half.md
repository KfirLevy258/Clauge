# A pair update that installed its app half and dropped the firmware half

**Status:** the fault is fixed and verified on hardware. What TRIPPED it on the
customer's machine is still unknown -- see "Still open" at the bottom.

Reported 2026-09-10 by a fleet tester upgrading **1.2.5 -> 1.3.2**.

## What the customer saw

The board sat on **1/2** and never moved. That is not a progress bar: it is the
tethered connecting screen's stage counter (`usage_view.c:2700`, printing
`stage + 1`), and its two stages are

    usb_boot_steps[] = { "Link the PC daemon", "Fetch first usage" }

so 1/2 is stage 0, and stage 0 only advances when `proto_host_seen()` is true.
It means "no daemon has spoken to me", not "the update is halfway".

`blink status` on that machine showed the shape of it exactly:

    App         1.3.2
    Board       ... firmware 1.2.5

The app half of the pair update landed and the firmware half did not. It came
back only after a hand-run `blink install`, which is the only command that both
rewrites the shims and re-registers the service (`cli.py:1933` is the sole
caller of `install_hooks.install`, and `cmd_install` ends with
`_install_service()`); the update path does neither.

## What the log showed, and where it stopped

    [bridge] <- {'t': 'ota_flash', 'v': 2}
    [bridge] ota: updating this app to 1.3.2 first
    [update] updated to 1.3.2
    [update] restarting into the new version      <- last line in the file

Nine earlier daemon restarts in that same file all kept logging to it. The one
after the self-update wrote nothing, and 1.3.2 had no log rotation to explain
it (`pc/logbook.py` postdates that release). Unexplained, and still is.

## Ruled out, so nobody re-derives them

- **Plist path drift.** No: `swap_in()` rotates `<bin>` -> `<bin>.old` in
  place; the target path never moves.
- **KeepAlive not firing on a clean exit.** No: `cli.py:513` is
  `<key>KeepAlive</key><true/>`, unconditional, so `os._exit(0)` is restarted.
- **The bin rotation carrying the shim away.** No: the shims live at
  `~/.blink/blink-hook.sh`, outside `bin/`.
- **A new daemon choking on old-format state slots.** No: the shim gained
  `pid` and a project name between those releases, but `_slot_pid()` uses
  `.get("pid")` with a type check, so absent fields degrade.
- **Version skew in the breadcrumb.** No: both releases use
  `os.path.join(blink_home, "pending_fw.json")`, 1.2.5 really does write it
  (`bridge.py`, `_on_ota_flash`), and 1.3.2's `_resume_pending()` is
  byte-identical to the current one -- `git diff v1.3.2 -- pc/bridge.py` is
  empty.

## The fault that was fixed

`_resume_pending()` spent the consent with `take()` BEFORE attempting anything,
then returned on any failure. The resume re-runs the ordinary query first, and
that query can fail for reasons that have nothing to do with the image -- a
feed that did not answer, a signature that did not verify -- at the one moment
in an update where a network is likeliest to be unsettled: seconds after the
daemon replaced itself.

The result was the customer's exact end state. The approval was gone, the board
was told `ota_none` (which `proto.c:632` maps to `OTA_UI_UP_TO_DATE` -- false,
and silent), and nothing on the panel said an update had been agreed to and
dropped.

It compounds. `upd_prompt_done` latches when the offer is shown, and until
2026-09-11 the only thing that lifted it was the board WITHDRAWING an open
prompt. A tap closes the prompt, so a tap that led nowhere left the latch set
for the rest of the boot: no second offer, no error, nothing.

Both halves are now fixed:

- `PendingFirmware` separates `read()` from `clear()` and counts attempts
  (`RESUME_TRIES = 3`). A resume that could not START keeps the consent and
  retries on the next connect; one that reaches the flash spends it first, so
  a failing write still cannot retry forever. On the last try it clears and
  sends `ota_error`, which `proto.c:635` turns into `OTA_UI_FAILED` -- a
  reason on the panel instead of silence.
- The prompt gate moved to `firmware/src/upd_prompt.h` as a pure function, so
  the FAILED-with-no-box-open case that a tap creates is covered by
  `tests/upd_prompt/host_test.c` rather than by a finger.

## How it was verified

Rebuilt on the bench: a real 1.2.5 daemon installed under a sandbox `HOME`
(safe because `self_bin = installed_bin()`, so the whole self-update happens
inside that directory), a signed local feed, and a launchd-equivalent
supervisor. The transient was induced by hiding `manifest.json` the instant the
tap landed.

    [bridge] <- {'t': 'ota_flash', 'v': 2}                          the tap
    [bridge] ota: updating this app to 1.3.3 first
    [update] restarting into the new version
    [bridge] ota: no manifest yet; 1.3.3 stays approved (attempt 1 of 3)
    [bridge] ota: resuming the approved install of 1.3.3
    [bridge] ota: flashed 1.3.3
    [ota] update landed: wanted 1.3.3, running 1.3.3 (from 1.3.2)   the BOARD

The breadcrumb the 1.2.5 daemon left, unedited, was `{"version": "1.3.3"}` --
no counter, which is why `read()` treats a missing one as zero rather than as
grounds to discard consent.

Two false starts worth keeping, because both cost an hour:

- The first rig published firmware 1.3.3 against daemon 1.3.2. `bridge.py:336`
  correctly refuses to offer firmware an app cannot drive, so the resume bailed
  for a reason no real release can produce. One release number, both halves.
- The second hid `manifest.json.sig`, which does nothing: the firmware path
  reads the UNSIGNED manifest on purpose, since MCUboot refuses an unsigned
  image whatever a manifest claims. Hide `manifest.json` itself.

## Still open

**What tripped it on the customer's machine.** The fix makes the symptom
unreachable and any similar failure visible and retryable, but the trigger is
not identified. Two things from that machine would settle it:

- `ls -la ~/.blink/` -- whether `pending_fw.json` is still there. Present means
  the resume never ran; absent means it ran and failed after spending consent.
- The `bridge.log` generation holding the lines AFTER
  `restarting into the new version`.
