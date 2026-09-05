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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import load_results, get_aac_path, get_scenario_sort_key, corpus_dir

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
    data_dir = corpus_dir(cfg, EXTERNAL_DATA_DIR)
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
    """How much MOS a clip gains per +1% of bitrate, per clip and per scenario.

    A candidate that simply spends more bits scores higher, so a raw MOS delta
    cannot tell "allocates better" apart from "spent more" -- a distinction that
    has repeatedly decided rate-control work the wrong way. The baseline run
    already contains the answer: its scenarios form a bitrate ladder within each
    corpus (48k_stereo at 24k..320k, and so on), and the slope of MOS against
    bitrate along that ladder is exactly the exchange rate needed.

    The slope is estimated per CLIP, not per scenario. A family average is one
    number applied to every clip in the scenario, and clips do not share a
    slope: on nschimme/faac#454 the scenario means disagreed by 9x, and the
    headline bits-adjusted figure (+0.0113) turned out to be carried by the one
    row with the steepest fitted slope. A clip is charged at its own exchange
    rate or it is not charged at all.

    Neighbours are taken only from the same object type. AUTO switches the
    48k_stereo ladder from HE-AAC to LC between 96k and 128k, and a difference
    taken across that step measures the codec change, not the price of bits.
    Falls back to pooling when the run predates the object_type field.

    Returns (per_clip, per_scenario): {(scenario, filename): dMOS/+1%} and
    {scenario: dMOS/+1%}, either of which may omit entries whose ladder is too
    short to difference.
    """
    try:
        from bd_rate import _scenario_corpus, _scenario_target
    except Exception:
        def _scenario_corpus(s):
            return s.rsplit('_', 1)[0]

        def _scenario_target(s, recs):
            for r in recs:
                t = r.get('bitrate_target') or r.get('expected_bitrate')
                if t:
                    return float(t)
            return 0.0

    by_scen = {}
    for v in matrix_a.values():
        scen, fn = v.get('scenario'), v.get('filename')
        if not scen or not fn or v.get('mos') is None or not v.get('bitrate'):
            continue
        by_scen.setdefault(scen, {})[fn] = v

    # (corpus, object_type) -> [(target, scenario)], the ladder a clip walks.
    ladders = {}
    for scen, clips in by_scen.items():
        recs = list(clips.values())
        ot = next((r.get('object_type') for r in recs if r.get('object_type')),
                  None)
        key = (_scenario_corpus(scen), ot)
        ladders.setdefault(key, []).append((_scenario_target(scen, recs), scen))
    for rows in ladders.values():
        rows.sort()

    def _slope(points):
        """Central difference of MOS against bitrate, as dMOS per +1% bits."""
        out = {}
        for i, (br, mos, scen) in enumerate(points):
            lo = points[i - 1] if i > 0 else points[i]
            hi = points[i + 1] if i + 1 < len(points) else points[i]
            d_br = hi[0] - lo[0]
            if d_br <= 0 or br <= 0:
                continue
            out[scen] = (hi[1] - lo[1]) / (d_br / br * 100.0)
        return out

    per_clip = {}
    per_scenario = {}
    for rows in ladders.values():
        if len(rows) < 2:
            continue
        scens = [s for _, s in rows]

        # Per clip: only clips present at every rung of this ladder, so the
        # difference is the same clip's own curve rather than a mix of clips.
        common = set.intersection(*(set(by_scen[s]) for s in scens))
        for fn in common:
            pts = [(by_scen[s][fn]['bitrate'], by_scen[s][fn]['mos'], s)
                   for s in scens]
            for scen, k in _slope(pts).items():
                per_clip[(scen, fn)] = k

        # Per scenario: the old family-average slope, kept only as the fallback
        # for clips that skip a rung (a decode error anywhere on the ladder).
        avg = []
        for s in scens:
            recs = list(by_scen[s].values())
            avg.append((sum(r['bitrate'] for r in recs) / len(recs),
                        sum(r['mos'] for r in recs) / len(recs), s))
        per_scenario.update(_slope(avg))

    return per_clip, per_scenario


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

    sens_clip, sens_scen = _bit_sensitivity(a)
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

        # Charge each clip's MOS delta for the bits that clip spent, at that
        # clip's own exchange rate, and only then average.
        adj_rows, ks = [], []
        for d, k, ma, mb, bra, brb, *_ in rows:
            # rows carry the matrix key (f"{run_name}_{filename}"); the slope
            # table is keyed by the recorded filename.
            k_bits = sens_clip.get((s, a[k].get('filename', k)))
            if k_bits is None:
                k_bits = sens_scen.get(s)
            if k_bits is None or not bra:
                continue
            ks.append(k_bits)
            adj_rows.append(d - k_bits * ((brb / bra - 1) * 100.0))

        if adj_rows and abs(bits_pct) >= 0.10:
            adj = sum(adj_rows) / len(adj_rows)
            k_lo, k_hi = min(ks), max(ks)
            note = ""
            if raw_d != 0 and (adj * raw_d <= 0):
                note = ("   <-- the raw delta is explained by bit spend, not "
                        "allocation")
            elif abs(raw_d - adj) > abs(raw_d) * 0.5:
                note = "   <-- mostly bit spend"
            print(f"   bits-adjusted avgMOSd={adj:+.4f} "
                  f"(n={len(adj_rows)}, per-clip {k_lo:+.4f}..{k_hi:+.4f} "
                  f"MOS per +1% bits){note}")
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
        print("Adjustment uses the baseline's own bitrate ladder, one slope "
              "per clip. It is still an estimate over two rungs: where the "
              "family has four or more rungs at one object type, prefer "
              "scripts/bd_rate.py, which holds quality fixed by construction "
              "rather than by arithmetic.")
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
