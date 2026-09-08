#!/usr/bin/env python3
"""Capture touch-trace dumps from the CYD.

Resets the board the same way tools/passive_log.py does (pulse EN via RTS,
send nothing) and records the console to a file until a deadline.

macOS has no timeout(1), so the deadline lives here rather than in the shell.

Usage:
    python3 tools/touch_trace.py --seconds 90 --out /tmp/touch.log
Run it with the zephyr venv python -- system python3 has no pyserial:
    .venv-test/bin/python
"""
import argparse
import glob
import sys
import time

import serial


def pick_port() -> str:
    # The trailing digits change with the physical USB socket, so glob.
    ports = sorted(glob.glob("/dev/cu.usbserial*"))
    if not ports:
        sys.exit("no /dev/cu.usbserial* found -- is the board plugged in?")
    return ports[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--out", default="/tmp/touch_trace.log")
    args = ap.parse_args()

    port = args.port or pick_port()

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    ser.dtr = False
    ser.rts = True          # pulse EN: clean reset into run mode
    time.sleep(0.15)
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    deadline = time.time() + args.seconds
    traces = 0
    pending = b""

    sys.stderr.write(
        f"[trace] {port} -> {args.out}, {args.seconds:.0f}s\n"
        "[trace] wait for the gauge screen, then: 5 deliberate taps, "
        "a 2 s hold, a slow drag.\n")
    sys.stderr.flush()

    with open(args.out, "wb") as fh:
        while time.time() < deadline:
            data = ser.read(512)
            if not data:
                continue
            fh.write(data)
            fh.flush()
            pending += data
            # Count completed traces so the operator knows taps registered.
            while b"#TT-END" in pending:
                pending = pending.split(b"#TT-END", 1)[1]
                traces += 1
                sys.stderr.write(f"\r[trace] captured {traces} touch(es)")
                sys.stderr.flush()

    ser.close()
    sys.stderr.write(f"\n[trace] done: {traces} touch(es) -> {args.out}\n")
    if traces == 0:
        sys.stderr.write(
            "[trace] nothing captured. Check CONFIG_BLINK_TOUCH_TRACE=y in\n"
            "        firmware/build-sb/firmware/zephyr/.config -- the flash\n"
            "        script reuses whatever is already in build-sb/.\n")


if __name__ == "__main__":
    main()
