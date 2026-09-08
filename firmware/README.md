# Blink firmware

The firmware that runs on the **ESP32-2432S028 "Cheap Yellow Display" (CYD)**: two
270° gauges for your Claude Code **session (5 h)** and **weekly (7 d)** usage, a wall
clock, per-model breakdown, and over-the-air updates. Built on **Zephyr** + LVGL,
booting through **MCUboot**, with flash encryption on units whose eFuses are burned.

The firmware version lives in [`src/version.h`](src/version.h) and is reported over
serial in the boot `hello` message.

## How it gets your usage

The board picks a data source at boot - you never choose one:

1. **USB bridge.** If a host daemon answers the boot `hello` over serial, the board
   takes its usage from there (streamed as NDJSON). No Wi-Fi or token on the device.
2. **Wi-Fi.** Otherwise, if it has stored Wi-Fi credentials and a sign-in token, it
   fetches usage itself over TLS, about once a minute.
3. **First-time setup.** With neither available, it shows a setup screen and hosts a
   short phone flow to get onto Wi-Fi and signed in. The setup Wi-Fi is WPA2 with a
   random per-device password carried in the on-screen QR, so the phone types nothing.

## Prerequisites

- **Zephyr v4.3.x** with its Python venv, and **Zephyr SDK 0.17.4**. The build
  scripts find the workspace themselves (`tools/lib_zephyr.sh` looks under
  `~/zephyrproject`, `~/zephyr-v*` and the same two under `~/Projects`); set
  `BLINK_ZEPHYR` to override.

  **Not v4.4.0.** Measured 2026-09-09: its mbedTLS is 4.1.0, which removed the
  legacy `mbedtls/sha256.h` that [`src/ota.c`](src/ota.c) includes, and the
  LVGL it pins moved `gesture_limit` out of the public `lv_indev` headers,
  which [`src/ui_settings.c`](src/ui_settings.c) reads. 4.3.1 has both
  (mbedTLS 3.6.6, `gesture_limit` in `lv_indev_private.h`) and wants SDK
  0.17.4 rather than the 1.0.1 that 4.4.0 asks for. Do not register both SDKs
  at once -- 1.0.1's CMake config breaks 4.3.1's configure even when 0.17.4 is
  registered alongside it.
- The **CYD board** on USB (a CH340 serial port, `/dev/cu.usbserial-*` on macOS).
- Your **signing keys** (see [Keys](#keys)).

## Build

```bash
. tools/lib_zephyr.sh && blink_zephyr_activate   # from the repo root

cd firmware
west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
  -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
  -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.blink/ota_signing_key_p256.pem\""
```

Notes:

- **This builds the shipped configuration: USB only.** `CONFIG_BLINK_WIFI_MODE`
  defaults to `n`, so the captive-portal setup, the OAuth sign-in, the refresh-token
  store and the on-device usage fetch are never handed to the compiler -- absent from
  the image rather than unreachable within it, and about half its size. The source
  stays in `src/`; add `-DEXTRA_CONF_FILE=wifi.conf` to build it back in. `tools/release.sh`
  refuses to publish an image that contains it, checking the artifact and not only the
  config.
- **Sysbuild + MCUboot is required.** Flash encryption needs the MCUboot boot chain;
  a plain single-image build produces something the encrypted chip can't boot.
- The board target is **`esp32_devkitc/esp32/procpu`** - there's no upstream CYD board,
  so [`boards/esp32_devkitc_esp32_procpu.overlay`](boards/) describes the wiring.
  Building with a different `-b` pollutes the build dir; recover with `-p always`.
- **Display SPI is pinned at 25 MHz.** The common 40 MHz CYD overclock white-screens
  some panels (init never latches) - see the overlay comment.

## Flash

**Not every CYD is fused, so check which one you have first.** A unit whose
flash-encryption eFuses are burned boots *only* images encrypted with its device key;
an unfused unit boots plaintext and cannot read an encrypted image at all. Cross the
two and the board sits in a boot loop (`invalid header: ...`) until it is re-flashed
the other way. Nothing is bricked - flashing burns no eFuses - but a cycle is lost
finding out.

```bash
espefuse.py --port <port> summary | grep FLASH_CRYPT_CNT
```

An **odd** number of bits set means encryption is on; `0` is a plaintext board.

**Fused unit** - use the script, which re-reads the eFuse itself and refuses a
plaintext chip (override with `BLINK_SKIP_EFUSE_CHECK=1`):

```bash
tools/flash_encrypted.sh        # defaults to the first /dev/cu.usbserial* port
```

**Plaintext unit** - two commands, because `west flash` writes only the app at
`0x20000` and leaves MCUboot at `0x1000` untouched:

```bash
west flash -d build-sb --esp-device <port> --esp-baud-rate 115200
esptool.py --port <port> --baud 115200 write_flash 0x1000 build-sb/mcuboot/zephyr/zephyr.bin
```

Port notes: opening the port resets the board, and only one process can own it at a
time. To watch the console without disturbing behavior, use
`tools/passive_log.py <port>` (resets once on open, then read-only). To wipe stored
Wi-Fi / token / setup password for a clean first-run test, erase the NVS region:
`esptool.py --port <port> erase_region 0x3b0000 0x30000`.

Flash map (4 MB): MCUboot `0x1000`, slot 0 `0x20000`, slot 1 `0x170000`,
scratch `0x2C0000` (unused in swap-move mode), **company logo `0x330000`
(512 KB, see `src/logo_parse.h`)**, settings `0x3B0000`. The logo partition is
written only by `tools/burn.sh --logo` / `tools/flash_encrypted.sh --logo` and
read memory-mapped at boot; erase it (`erase_region 0x330000 0x80000`) to turn
a bench board back into an individual unit. `tools/flash_encrypted.sh` erases
it by default -- pass `--keep-logo` when re-flashing a dev board that should
keep its logo.

## Keys

Two secrets live **outside the repo**, in `~/.blink/`:

| Key | Purpose |
|-----|---------|
| `flash_key.bin` | Encrypts what's written to flash. Override with `BLINK_FLASH_KEY`. |
| `ota_signing_key_p256.pem` | Signs firmware images (ECDSA P-256) so the board will boot them. Override with `OTA_SIGNING_KEY`. |

**Back both up off this machine.** The eFuse copy of the flash key is sealed and
unreadable - the file is the only usable copy. Lose the signing key and you can no
longer ship updates that installed devices will accept (only a USB reflash with a new
key recovers).

## Releasing an update

```bash
# 1. bump BLINK_FW_VERSION in src/version.h, commit
# 2. from the repo root:
tools/release.sh
```

`release.sh` builds from the committed tree (it refuses a dirty tree or a reused
version), signs the image, and attaches `blink-fw.bin` + `manifest.json` to the
`v<version>` release on **`KfirLevy258/Blink`** - which is the feed every board reads.

On the device there are two ways in, and it picks by itself:

- **Standalone (WiFi).** The board streams the image into its spare slot, verifies the
  SHA-256, reboots, and MCUboot swaps it in.
- **Tethered (USB bridge).** This mode has no network of its own -- `run_usb()` never
  starts `net_worker` -- so the board only *approves*, and the daemon fetches the
  release and writes slot0 with esptool. About 75 s, and no swap, because the bytes
  land where the bootloader already looks. The trade is no automatic rollback on that
  route; it is acceptable only because a machine that can reflash the board is by
  definition already cabled to it. Requires `pip install esptool` wherever the daemon
  runs.

A check runs as soon as the board is up (and daily after that). Anything newer raises a
prompt on the gauge screen -- **Update now** / **Later** -- and the settings row still
offers **Software update → Install x.y.z** on demand. Either route reports the outcome
on the next boot.

## How updates stay safe

Four independent layers, each guarding one thing:

- **TLS with pinned roots** authenticates the *download*.
- **The manifest's SHA-256** pins the *exact bytes*, end to end.
- **MCUboot's ECDSA P-256 signature check** gates *what may boot* - only images signed
  with your private key run, so even a hash-passing forgery is rejected.
- **Automatic rollback** - a fresh image boots in test mode and must prove itself
  (fetch usage) within 90 s. If it hangs or crashes, the watchdog reboots and MCUboot
  restores the previous version, which then reports *"Update failed, previous version
  restored."*

The first three hold on both routes. The fourth does not apply to the USB one, which
writes the running slot in place -- there is no spare copy to fall back to. The daemon
compensates where it can: it verifies the asset against its manifest before writing,
and it reads the chip's eFuses and refuses outright if it cannot tell whether flash
encryption is burned, since a plaintext write to a fused board leaves it dark.

On a fused unit, flash encryption keeps secrets (Wi-Fi password, token) unreadable at
rest. **An unfused development board has no such protection** - its stored Wi-Fi
password and OAuth token are readable from a flash dump, so treat one as a device that
holds live credentials in the clear. Published images hold no secrets either way -
installability is gated by the signing key, not secrecy.

## Hardware notes

Things worth knowing when hacking on the CYD:

- The radio occasionally boots blind (all scans empty) after a soft reset; the firmware
  self-reboots to re-roll it, bounded by a `.noinit` counter.
- Touch (XPT2046) needs averaging (`reads = <8>`), and devicetree properties are
  last-one-wins - a stray duplicate silently degrades it.
- The backlight (GPIO 21) runs on LEDC PWM and is adjustable in Settings.
- A Wi-Fi join only completes from a clean boot on this driver, which is why the
  first-run flow reboots on purpose (invisibly - the CONNECTING bar spans the reset).

## Source map

| Area | Files |
|------|-------|
| Boot, orchestration, workers | `main.c` |
| Boot splash (streamed past LVGL) | `ui_boot.c`, `bootanim_dec.c` |
| First-run setup screen | `ui_setup.c` |
| Phone captive portal | `portal.c`, `dns_hijack.c` |
| Gauges, warnings, model card | `usage_view.c` |
| Settings + software-update UI | `ui_settings.c` |
| OTA engine (check, install, verify) | `ota.c`, `ota_parse.c` |
| Networking, time, sign-in, fetch | `net_wifi.c`, `net_time.c`, `oauth.c`, `usage_client.c`, `usage_parse.c`, `tz_fetch.c` |
| Persisted config (A/B, CRC-sealed) | `cfg_store.c` |
| Serial NDJSON protocol | `proto.c`, `msg_parse.c` |

Parsers and the protocol have host-side unit tests under [`../tests/`](../tests/).
