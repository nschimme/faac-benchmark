#!/usr/bin/env python3
"""Per-function cycle profile of FAAD3 on a board (diagnostic; see docs/scripts.md).

Builds the FAAD3 adapter with -finstrument-functions (CODEC_PROFILE=1), decodes one ADTS clip for a single
pass and prints exclusive cycles per function. Hook cost lands in the callee, so small and frequently called
functions read high; the ranking of the big ones is what to trust. Functions inlined by the compiler are
charged to their caller.

    python3 scripts/esp32/esp32_dec_profile.py --target esp32 --port /dev/ttyUSB0 clip.aac
"""
import argparse
import os
import shlex
import struct
import sys
import time

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import esp32_dec_bench as eb  # noqa: E402


def build_flash(target, port, sbr):
    b = eb.build_dir("faad3prof", target)
    args = (f"-B {b} -DSDKCONFIG={b}/sdkconfig -DCODEC=faad3 -DCODEC_SBR={sbr} -DCODEC_PROFILE=1 "
            f"-DCODEC_FAAD3_OPEN=1")
    for step in (f"idf.py {args} set-target {target}", f"idf.py {args} build",
                 f"idf.py {args} -p {shlex.quote(port)} flash"):
        res = eb.idf(step)
        if res.returncode:
            sys.exit(f"`{step}` failed:\n" + "\n".join((res.stdout + res.stderr).splitlines()[-25:]))
    return os.path.join(eb.FIRMWARE, b, "aac_bench.elf")


def capture(port, clip, timeout):
    ser = serial.Serial(port, 921600, timeout=1)
    ser.dtr, ser.rts = False, True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(1.0)
    ser.reset_input_buffer()
    ser.write(b"AACB" + struct.pack("<III", len(clip), 1, 0))
    for i in range(0, len(clip), 4096):
        ser.write(clip[i:i + 4096])
    ser.flush()
    buf, deadline = b"", time.monotonic() + timeout
    while time.monotonic() < deadline and b"PROFEND" not in buf:
        buf += ser.read(4096)
    time.sleep(0.5)
    buf += ser.read(4096)
    ser.close()
    return buf.decode("latin1")


ADDR2LINE = {"esp32": "xtensa-esp32-elf-addr2line", "esp32s3": "xtensa-esp32s3-elf-addr2line",
             "esp32c6": "riscv32-esp-elf-addr2line"}


def symbolize(target, elf, addrs):
    res = eb.idf(f"{ADDR2LINE[target]} -f -e {shlex.quote(elf)} " + " ".join(f"0x{a:x}" for a in addrs))
    lines = res.stdout.splitlines()
    return {a: lines[2 * i] for i, a in enumerate(addrs)} if len(lines) >= 2 * len(addrs) else {a: f"0x{a:x}" for a in addrs}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip")
    ap.add_argument("--target", default="esp32", choices=sorted(eb.CPU_MHZ))
    ap.add_argument("--port", required=True)
    ap.add_argument("--sbr", type=int, default=1, choices=(0, 1))
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()
    elf = os.path.join(eb.FIRMWARE, eb.build_dir("faad3prof", a.target), "aac_bench.elf")
    if not a.no_build:
        elf = build_flash(a.target, a.port, a.sbr)
    out = capture(a.port, open(a.clip, "rb").read(), a.timeout)
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].endswith("PROF"):
            rows.append((int(parts[1], 16), int(parts[2], 16), int(parts[3])))
    if not rows:
        sys.exit("no PROF lines received:\n" + out[-500:])
    names = symbolize(a.target, elf, [r[0] for r in rows])
    total = sum(r[1] for r in rows)
    print(f"{a.target} @ {eb.CPU_MHZ[a.target]} MHz, {a.clip}: {total:,} cycles in instrumented functions")
    print(f"{'function':32} {'cycles':>14} {'%':>6} {'calls':>8} {'cyc/call':>10}")
    for addr, cyc, calls in sorted(rows, key=lambda r: -r[1]):
        print(f"{names[addr]:32} {cyc:14,} {100 * cyc / total:6.1f} {calls:8} {cyc // max(calls, 1):10,}")


if __name__ == "__main__":
    main()
