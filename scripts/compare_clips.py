"""
 * FAAC Benchmark Suite - Per-Clip Comparison Tool
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import json
import sys
import argparse

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from utils import load_results, get_aac_path, get_scenario_sort_key

OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
EXTERNAL_DATA_DIR = os.path.join(ROOT_DIR, "data", "external")


def _band_report(rows, scenario, file_a, file_b, top, matrix_a):
    """Per-band log-spectral distortion (base vs cand) for the worst `top`
    regressors in a scenario. Imports are local so the diff tool stays usable
    without numpy when --bands is not requested."""
    try:
        from config import SCENARIOS
        from band_diag import band_errors
    except Exception as e:
        print(f"   [bands] unavailable: {e}")
        return
    cfg = SCENARIOS.get(scenario, {})
    mode = "speech" if cfg.get("mode") == "speech" else "audio"
    data_dir = os.path.join(EXTERNAL_DATA_DIR, "speech" if mode == "speech" else "audio")
    for d, k, ma, mb, *_ in sorted(rows)[:top]:
        if d >= -0.01:
            continue
        # Matrix key is f"{run_name}_{filename}"; use the recorded filename field
        # rather than parsing the key (the prefix is the run tag, not scenario).
        filename = matrix_a.get(k, {}).get("filename", k)
        ref = os.path.join(data_dir, filename)
        a_aac = get_aac_path(k, OUTPUT_DIR, file_a)
        b_aac = get_aac_path(k, OUTPUT_DIR, file_b)
        if not (os.path.exists(ref) and a_aac and b_aac):
            print(f"   [bands] {filename}: missing ref/aac, skipping")
            continue
        ea = band_errors(ref, a_aac, mode)
        eb = band_errors(ref, b_aac, mode)
        if not ea or not eb:
            continue
        print(f"   bands {filename} ({d:+.2f}):")
        print(f"     {'band':14} {'base':>8} {'cand':>8} {'Δ':>8}")
        for band in ea:
            va, vb = ea[band], eb.get(band, float('nan'))
            print(f"     {band:14} {va:8.1f} {vb:8.1f} {vb - va:+8.1f}")


def _bit_sensitivity(matrix_a):
    """How much MOS a scenario gains per +1% of bitrate, per scenario.

    A candidate that simply spends more bits scores higher, so a raw MOS delta
    cannot tell "allocates better" apart from "spent more" -- a distinction that
    has repeatedly decided rate-control work the wrong way. The baseline run
    already contains the answer: its scenarios form a bitrate ladder within each
    family (48k_stereo at 24k..256k, and so on), and the slope of MOS against
    bitrate along that ladder is exactly the exchange rate needed.

    Returns {scenario: dMOS per +1% bitrate}, omitting families too short to
    estimate a slope from.
    """
    agg = {}
    for v in matrix_a.values():
        if v.get('mos') is None or not v.get('bitrate'):
            continue
        e = agg.setdefault(v.get('scenario', 'unknown'), [0.0, 0.0, 0])
        e[0] += v['mos']
        e[1] += v['bitrate']
        e[2] += 1

    families = {}
    for s, (mos, br, n) in agg.items():
        families.setdefault(s.rsplit('_', 1)[0], []).append((br / n, mos / n, s))

    sens = {}
    for rows in families.values():
        rows.sort()
        if len(rows) < 2:
            continue
        for i, (br, _, s) in enumerate(rows):
            # Central difference where possible; one-sided at the ladder ends.
            lo = rows[i - 1] if i > 0 else rows[i]
            hi = rows[i + 1] if i + 1 < len(rows) else rows[i]
            d_br = hi[0] - lo[0]
            if d_br <= 0 or br <= 0:
                continue
            # Convert "per kbps" to "per 1% of this scenario's bitrate".
            sens[s] = (hi[1] - lo[1]) / (d_br / br * 100.0)
    return sens


def compare(file_a, file_b, bands=False, bands_top=3):
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

    sens = _bit_sensitivity(a)
    adj_totals = []

    # Sort scenarios deterministically: by rank/bitrate, then by name
    for s in sorted(scen.keys(), key=lambda s: (get_scenario_sort_key(s), s)):
        rows = scen[s]
        ds = [r[0] for r in rows]
        wins = sum(1 for d in ds if d > 0.02)
        losses = sum(1 for d in ds if d < -0.02)

        t_a_total = sum(r[6] for r in rows)
        t_b_total = sum(r[7] for r in rows)

        br_a_avg = sum(r[4] for r in rows) / len(rows) if rows else 0
        br_b_avg = sum(r[5] for r in rows) / len(rows) if rows else 0

        time_chg = (t_b_total / t_a_total - 1) * 100 if t_a_total > 0 else 0

        raw_d = sum(ds) / len(ds)
        bits_pct = (br_b_avg / br_a_avg - 1) * 100 if br_a_avg > 0 else 0.0

        print(f"{s}: n={len(rows)} avgMOSd={raw_d:+.4f} wins={wins} losses={losses} "
              f"avg_br={br_a_avg:.1f}->{br_b_avg:.1f} ({bits_pct:+.2f}%) "
              f"enc_time={t_a_total:.2f}s->{t_b_total:.2f}s ({time_chg:+.0f}%)")

        # Charge the MOS delta for the bits that bought it.
        k_bits = sens.get(s)
        if k_bits is not None and abs(bits_pct) >= 0.10:
            adj = raw_d - k_bits * bits_pct
            note = ""
            if raw_d != 0 and (adj * raw_d <= 0):
                note = ("   <-- the raw delta is explained by bit spend, not "
                        "allocation")
            elif abs(raw_d - adj) > abs(raw_d) * 0.5:
                note = "   <-- mostly bit spend"
            print(f"   bits-adjusted avgMOSd={adj:+.4f} "
                  f"(at {k_bits:+.4f} MOS per +1% bits){note}")
            adj_totals.append((s, raw_d, adj))

        # Worst 5
        for d, k, ma, mb, *_ in sorted(rows)[:5]:
            if d < -0.01:
                print(f"   worst {k}: {ma:.2f} -> {mb:.2f} ({d:+.2f})")

        # Best 3
        for d, k, ma, mb, *_ in sorted(rows)[-3:]:
            if d > 0.01:
                print(f"   best  {k}: {ma:.2f} -> {mb:.2f} ({d:+.2f})")

        if bands:
            _band_report(rows, s, file_a, file_b, bands_top, a)
        print("")

    if adj_totals:
        raw_mean = sum(r for _, r, _ in adj_totals) / len(adj_totals)
        adj_mean = sum(x for _, _, x in adj_totals) / len(adj_totals)
        flipped = [s for s, r, x in adj_totals if r != 0 and r * x <= 0]
        print(f"Across {len(adj_totals)} scenarios with a bitrate shift: "
              f"mean avgMOSd {raw_mean:+.4f}, bits-adjusted {adj_mean:+.4f}")
        if flipped:
            print("Scenarios whose result is bit spend rather than allocation: "
                  + ", ".join(flipped))
        print("Adjustment uses the baseline's own bitrate ladder. It is an "
              "estimate: to settle a close call, re-encode the candidate at a "
              "scaled -b so actual bytes match the baseline, then compare.")
        print("")

def main():
    parser = argparse.ArgumentParser(description="Ranked per-clip comparison of two benchmark JSONs.")
    parser.add_argument("file_a", help="Baseline result JSON")
    parser.add_argument("file_b", help="Candidate result JSON")
    parser.add_argument("--bands", action="store_true",
                        help="Per-band log-spectral distortion for the worst regressors "
                             "(needs numpy + the encoded .aac files in output/)")
    parser.add_argument("--bands-top", type=int, default=3,
                        help="How many worst regressors per scenario to analyze with --bands")
    args = parser.parse_args()

    compare(args.file_a, args.file_b, bands=args.bands, bands_top=args.bands_top)

if __name__ == "__main__":
    main()
