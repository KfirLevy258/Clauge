# CH340 driver for Windows (WCH CH341SER)

The USB-serial chip on every CYD is a WCH CH340. macOS and Linux drive it from
the kernel. Windows has never shipped a driver for it, so without this the
board enumerates, gets no COM port, and does nothing — see `pc/win_driver.py`
for what that looked like to a customer.

`tools/build_binary.sh` bundles this directory into the **Windows** build only,
at `_internal/drivers/ch341ser/`. `blink install` then stages it into Windows'
driver store with `pnputil /add-driver … /install`, so a board plugged in
afterwards binds to it with no prompt at all.

## What these files are

| File | |
|---|---|
| `CH341SER.INF` | the driver package Windows installs |
| `CH341SER.CAT` | its signature catalogue |
| `CH341S64.SYS` | the kernel driver |
| `CH341PT.DLL`, `CH341PTA64.DLL`, `CH341PORTSA64.DLL` | co-installer support |

Windows' own `.PNF` (a precompiled copy of the `.INF`) is deliberately absent:
it is generated on install and is not part of the package.

## Provenance

- **Vendor:** WCH (Nanjing Qinheng Microelectronics), <https://www.wch-ic.com/downloads/CH341SER_EXE.html>
- **Version:** 3.9.2024.09, `DriverVer` 09/16/2024
- **Signed by:** Microsoft Windows Hardware Compatibility Publisher (WHQL)
- **Covers:** `VID_1A86` `PID_7523` (ours), `PID_7522`, `PID_5523`, `PID_E523`
- **Taken from:** the Windows 10 release desk's own driver store
  (`ch341ser.inf_amd64_34a3206305b40d57`), 2026-09-06 — i.e. a copy Windows
  had already accepted and bound to a working board.

The `.CAT` must stay Microsoft-signed or Windows refuses the install with no
useful message, which is why the copy came from a driver store rather than
from a re-zipped download of unknown history.

## Updating it

Download the current package from the link above, extract it (it ships as a
self-extracting `.EXE`; the driver files are inside — do **not** commit the
`.EXE`, `pnputil` takes the `.INF` and nothing here can drive a window with a
button on it), and replace these files. Then re-run the Windows checks in
`docs/windows-check.md`, steps 7a and 7b — the ones that delete the driver and
reboot. A driver that installs but does not bind looks exactly like a driver
that installed fine, right up until a customer's board stays dark.
