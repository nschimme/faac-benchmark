#!/usr/bin/env python3
"""Decode deliberately corrupted AAC streams and count crashes and hangs.

Applies the robustness pass's corruption (utils.corrupt_adts_bitstream:
dropped frames and bit flips, fixed seed per run) to each input and runs the
decoder on the result. A decoder that rejects the stream and exits non-zero
is fine; a signal (segfault, abort, or a sanitizer report) or a run that
exceeds the timeout is not.

Usage (from repo root):
  python3 scripts/corrupt_decode_check.py --decoder bin/helix-aac-dec \\
      --seeds 40 in1.aac in2.aac ...

The decoder is run as `DECODER corrupted.aac out.wav`, the same command line
the Helix wrapper from scripts/build_helix_aac.sh takes. Inputs must be ADTS
(ffmpeg -i in.m4a -c:a copy -f adts in.aac). Exits 1 if anything failed.
"""
import argparse
import collections
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from utils import corrupt_adts_bitstream


def classify(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "hang"
    if r.returncode < 0 or r.returncode == 139 or "AddressSanitizer" in r.stderr:
        return "crash"
    return "ok"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="ADTS streams to corrupt")
    ap.add_argument("--decoder", required=True, help="decoder binary, run as DECODER in.aac out.wav")
    ap.add_argument("--seeds", type=int, default=40, help="corruption seeds per input (default 40)")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds before a run counts as a hang")
    args = ap.parse_args()

    counts = collections.Counter()
    failures = []
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "corrupt.aac")
        out = os.path.join(td, "out.wav")
        for path in args.inputs:
            for seed in range(1, args.seeds + 1):
                if not corrupt_adts_bitstream(path, bad, seed=seed):
                    continue
                kind = classify([args.decoder, bad, out], args.timeout)
                counts[kind] += 1
                if kind != "ok":
                    failures.append((os.path.basename(path), seed, kind))

    total = sum(counts.values())
    print(f"{args.decoder}: {total} corrupted streams: "
          f"{counts['ok']} ok, {counts['crash']} crash, {counts['hang']} hang")
    for name, seed, kind in failures[:20]:
        print(f"  {kind}: {name} seed={seed}")
    if len(failures) > 20:
        print(f"  ... {len(failures) - 20} more")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
