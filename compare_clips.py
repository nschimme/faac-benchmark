"""
 * FAAC Benchmark Suite - Per-Clip Comparison Tool
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import json
import sys
import argparse
from utils import load_results

def compare(file_a, file_b):
    res_a = load_results(file_a)
    res_b = load_results(file_b)

    if not res_a or not res_b:
        print("Error: Could not load one or both result files.")
        return

    a = res_a.get('matrix', {})
    b = res_b.get('matrix', {})

    scen = {}
    for k in a:
        if k not in b:
            continue

        s = a[k].get('scenario', 'unknown')

        # Extract MOS
        mos_a = a[k].get('mos')
        mos_b = b[k].get('mos')

        if mos_a is None or mos_b is None:
            continue

        d = mos_b - mos_a

        # Extract other metrics
        br_a = a[k].get('bitrate', 0) or 0
        br_b = b[k].get('bitrate', 0) or 0
        t_a = a[k].get('time', 0) or 0
        t_b = b[k].get('time', 0) or 0

        scen.setdefault(s, []).append((d, k, mos_a, mos_b, br_a, br_b, t_a, t_b))

    if not scen:
        print("No matching clips found between the two result sets.")
        return

    for s, rows in sorted(scen.items()):
        ds = [r[0] for r in rows]
        wins = sum(1 for d in ds if d > 0.02)
        losses = sum(1 for d in ds if d < -0.02)

        t_a_total = sum(r[6] for r in rows)
        t_b_total = sum(r[7] for r in rows)

        br_a_avg = sum(r[4] for r in rows) / len(rows) if rows else 0
        br_b_avg = sum(r[5] for r in rows) / len(rows) if rows else 0

        time_chg = (t_b_total / t_a_total - 1) * 100 if t_a_total > 0 else 0

        print(f"{s}: n={len(rows)} avgMOSd={sum(ds)/len(ds):+.4f} wins={wins} losses={losses} "
              f"avg_br={br_a_avg:.1f}->{br_b_avg:.1f} enc_time={t_a_total:.2f}s->{t_b_total:.2f}s ({time_chg:+.0f}%)")

        # Worst 5
        for d, k, ma, mb, *_ in sorted(rows)[:5]:
            if d < -0.01:
                print(f"   worst {k}: {ma:.2f} -> {mb:.2f} ({d:+.2f})")

        # Best 3
        for d, k, ma, mb, *_ in sorted(rows)[-3:]:
            if d > 0.01:
                print(f"   best  {k}: {ma:.2f} -> {mb:.2f} ({d:+.2f})")
        print("")

def main():
    parser = argparse.ArgumentParser(description="Ranked per-clip comparison of two benchmark JSONs.")
    parser.add_argument("file_a", help="Baseline result JSON")
    parser.add_argument("file_b", help="Candidate result JSON")
    args = parser.parse_args()

    compare(args.file_a, args.file_b)

if __name__ == "__main__":
    main()
