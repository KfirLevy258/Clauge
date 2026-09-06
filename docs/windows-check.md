# The Windows check

Ten minutes on a real Windows machine, once per release. CI proves the
installer, the daemon's Windows branches and the real Claude Code CLI there;
it cannot prove the two things a customer hits first, because no runner has a
signed-in Claude Desktop or a board on a COM port.

**Needs:** a Windows 10/11 PC or VM with USB passthrough; Claude Desktop
installed and signed in; Claude Code installed (`npm install -g
@anthropic-ai/claude-code`); a BLINK on a USB cable.

Download `blink-windows-latest` from the latest CI run's artifacts (Actions ->
the run -> Artifacts), or the release's `blink-windows-x86_64.zip` (unzip it; run `blink\blink.exe`).

| # | Do | Expect |
|---|---|---|
| 1 | Open Claude Desktop, use it once, close it. | -- |
| 2 | In PowerShell: `dir $env:APPDATA\Claude\plan-usage-history.json` | **The file exists.** If it does not, the Desktop path assumption is wrong: find the file (`dir -Recurse $env:APPDATA,$env:LOCALAPPDATA -Filter plan-usage-history.json`) and report the real path. |
| 3 | `.\blink.exe` | The disclosure, then six steps ending `[6/6] Background service ... running`. No stack trace. Step `[5/6] USB driver` either says `already installed` with no prompt, or raises **one** Windows permission prompt and then says `installed`. **Run this NOT as administrator** -- that is what a customer's double-click does, and the permission prompt is the one path they all take. `this build carries no driver` means the build did not include `vendor/ch341ser`: a release blocker, not a test failure. |
| 4 | `.\blink.exe status` | `Bridge registered as a Scheduled Task`, `Claude Code <version>`, `Activity hooks installed (10/10 events)`, `Desktop usage cache parsed, reading N min old`. **If it says `looked at ...`, step 2's path is not the one it checked -- report both.** |
| 5 | Open a terminal, run `claude`, ask it something, wait for the reply. Then `.\blink.exe status` again. | `Usage data fresh`, `1 live session`. |
| 6 | `.\blink.exe status --wire` | One JSON line with `session_pct`, `weekly_pct`, `provider":"claude"`, `src":"cli"`, `state`. |
| 7 | Plug the board in. Device Manager -> Ports: a `USB-SERIAL CH340 (COMn)` entry. | Step 3 installed the driver, so the entry is there and clean. |
| 7a | The undriven case, on a desk whose driver already works. Elevated: `pnputil /delete-driver <oem>.inf /uninstall /force` (`<oem>` from `pnputil /enum-drivers`, the one whose Original Name is `ch341ser.inf`), then **reboot** -- the board has to enumerate with no driver, and deleting the package alone leaves the running device bound until it does. Then `.\blink.exe status`. | `Board  a board is plugged in, but Windows has no driver for it` and `run ... driver` -- **never** `not plugged in`. Then `.\blink.exe driver` says `installed`, a COM port appears within seconds, and `status` names the board. Verified 2026-09-06. |
| 7b | Note while doing 7a: `pnputil /enum-devices /problem` reports nothing, and the device's problem code is 0. | Expected. An undriven device is not a "problem" device to Windows -- detection keys on having no driver bound, not on a problem code. If a future change makes step 7a print `not plugged in`, this is the reason to look at first. |
| 8 | Within 60 s the panel shows the numbers from step 6. | Boot clip, then the gauges. |
| 9 | Start a Claude Code turn; watch the pip. Leave the terminal idle at its prompt for 4 minutes. | Pip pulses while it works, goes steady when done, **does not turn red** while idle. |
| 10 | Unplug and replug the board. | Panel back within a minute, no reboot loop, no reset of the board each time (`%USERPROFILE%\.blink\bridge.log` says `answered; not resetting it`). |
| 11 | `.\blink.exe uninstall` | Status line and hooks gone from `%USERPROFILE%\.claude\settings.json`, task gone (`schtasks /query /tn "Blink bridge"` fails), `%USERPROFILE%\.blink\bin` gone a moment after the window closes. |

Paste the outputs of steps 2, 4, 6 and 10 into the release notes. Anything
unexpected: the `bridge.log` and the output of `status --wire` are what a bug
report needs.

## Why not a VM on the Mac

Tried 2026-08-28 on an Intel Mac running macOS 26: QEMU's HVF accelerator
hangs the emulator's main loop on this host (every CPU model, entitlement
present), so Windows 11 only ran under pure software emulation -- the
install took two hours, the desktop was too slow to use, and the evaluation
build's licence read as expired, which shuts Windows down on a timer.
VirtualBox/Parallels were not tried. A real Windows PC -- any laptop, ten
minutes -- is the practical way to run this list. The unattended VM
definition is kept under `~/vm` on that Mac in case a working hypervisor
turns up.
