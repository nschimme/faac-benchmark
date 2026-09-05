"""
 * FAAC Benchmark Suite - Bjontegaard-delta rate
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

BD-rate answers the question a fixed-`-b` MOS delta cannot: how many more (or
fewer) bits does the candidate need to reach the same quality as the baseline?

Why this exists. The suite's headline metric is a MOS delta at a fixed target
bitrate, where MOS and the bitrate actually delivered are not independent -- a
build that overshoots its target is rewarded for the bits it stole, and any
rate-control fix that removes overshoot is charged for quality it never lost.
On nschimme/faac#454 the three available metrics gave three different verdicts:
raw MOS -0.021, bits-adjusted median +0.0004, BD-rate +0.8%. Only the last one
holds bitrate fixed by construction rather than by after-the-fact arithmetic,
which is why rate-control work is gated on it.

Positive BD-rate = the candidate needs MORE bits for equal quality = worse.

The fit is the standard one: for each clip, MOS is the independent variable and
log10(bitrate) the dependent one; a polynomial is fitted to each build's
rate-quality curve, both are integrated over the MOS interval the two curves
share, and the mean log-rate difference is converted back to a percentage.
"""

import argparse
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

# Default minimum rungs required for a BD-rate ladder fit. Four rungs permit a cubic (order-3)
# fit; three rungs adaptively fall back to a quadratic (order-2) fit, ensuring gate runs and
# partial scenario sweeps are evaluated without silent exclusions.
MIN_RUNGS = 3

# Clips whose fitted curves cross so badly that the integral is meaningless.
# Retained as a count in the report rather than silently dropped.
IMPLAUSIBLE_PCT = 100.0


def _scenario_corpus(scenario):
    """Which corpus a scenario reads, so a ladder holds content constant.

    Corpus, not family: 16k_mono spans both the clean and the VoIP-degraded
    speech sets, and a ladder built across those two measures the content
    change, not the encoder. Falls back to the scenario-name prefix so runs
    whose scenarios are not in config (sweeps, ad-hoc matrices) still group.
    """
    try:
        from config import SCENARIOS
        cfg = SCENARIOS.get(scenario)
        if cfg:
            return cfg["corpus"]
    except Exception:
        pass
    return scenario.rsplit("_", 1)[0]


def _scenario_target(scenario, records):
    """The rung's target bitrate, for ordering. Prefers the recorded target."""
    for rec in records:
        t = rec.get("bitrate_target") or rec.get("expected_bitrate")
        if t:
            return float(t)
    try:
        from config import SCENARIOS
        return float(SCENARIOS[scenario]["bitrate"])
    except Exception:
        pass
    tail = scenario.rsplit("_", 1)[-1]
    try:
        return float(tail.rstrip("k"))
    except ValueError:
        return 0.0


def _scenario_object_type(records):
    """The object type AUTO resolved to for this scenario, or None.

    Resolution depends on sample rate, channel count and target bitrate -- all
    constants of the scenario -- so every clip in a scenario agrees and a
    majority vote is only defence against a partly-populated matrix.
    """
    votes = {}
    for rec in records:
        ot = rec.get("object_type")
        if ot:
            votes[ot] = votes.get(ot, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def index_by_scenario(matrix):
    """{scenario: {filename: record}} from a results matrix."""
    out = {}
    for rec in matrix.values():
        scen = rec.get("scenario")
        fn = rec.get("filename")
        if scen and fn:
            out.setdefault(scen, {})[fn] = rec
    return out


def find_ladders(base_by_scen, cand_by_scen, min_rungs=MIN_RUNGS):
    """Split the run into ladders that a BD-rate fit is actually valid over.

    One ladder = one corpus at one object type. The object-type split is
    load-bearing, not tidiness: the 48k_stereo ladder crosses AUTO's HE->LC
    switch between 96k and 128k, and fitting a single curve across that
    discontinuity halved the apparent loss on #454 (+0.401% pooled against
    +0.816% / +0.705% for the two segments measured separately).

    Returns (ladders, notes) where a ladder is a dict with keys
    corpus / object_type / rungs, and notes are human-readable exclusions.
    """
    notes = []
    groups = {}
    shared = sorted(set(base_by_scen) & set(cand_by_scen))

    for scen in shared:
        b_ot = _scenario_object_type(base_by_scen[scen].values())
        c_ot = _scenario_object_type(cand_by_scen[scen].values())
        if b_ot and c_ot and b_ot != c_ot:
            # The builds disagree about what codec this rung is. That is a real
            # and possibly intended change, but the two curves no longer
            # describe the same codec, so no BD-rate over this rung is
            # meaningful. Surface it rather than averaging through it.
            notes.append(
                f"{scen}: object type differs between builds "
                f"(base {b_ot}, candidate {c_ot}) -- excluded from every ladder")
            continue
        ot = b_ot or c_ot
        key = (_scenario_corpus(scen), ot)
        groups.setdefault(key, []).append(scen)

    ladders = []
    for (corpus, ot), scens in sorted(groups.items(), key=lambda kv: str(kv[0])):
        scens.sort(key=lambda s: _scenario_target(s, base_by_scen[s].values()))
        if len(scens) < min_rungs:
            notes.append(
                f"{corpus} / {ot or 'object type unrecorded'}: "
                f"{len(scens)} rung(s), needs {min_rungs} -- skipped")
            continue
        ladders.append({"corpus": corpus, "object_type": ot, "rungs": scens})
    return ladders, notes


def bd_rate_curve(base_points, cand_points, order=None):
    """BD-rate for one clip from two (bitrate, mos) point lists.

    Returns a percentage, or None when the curves share no quality overlap or
    the fit is degenerate.
    """
    import numpy as np

    if len(base_points) != len(cand_points) or len(base_points) < 3:
        return None

    if order is None:
        order = 3 if len(base_points) >= 4 else 2
    else:
        order = min(order, len(base_points) - 1)

    b = sorted(base_points, key=lambda p: p[1])
    c = sorted(cand_points, key=lambda p: p[1])
    if any(p[0] is None or p[0] <= 0 or p[1] is None for p in b + c):
        return None

    b_mos = np.array([p[1] for p in b], dtype=float)
    c_mos = np.array([p[1] for p in c], dtype=float)
    b_lr = np.log10(np.array([p[0] for p in b], dtype=float))
    c_lr = np.log10(np.array([p[0] for p in c], dtype=float))

    lo = max(b_mos.min(), c_mos.min())
    hi = min(b_mos.max(), c_mos.max())
    if hi <= lo:
        return None

    try:
        b_poly = np.polyfit(b_mos, b_lr, order)
        c_poly = np.polyfit(c_mos, c_lr, order)
    except Exception:
        return None

    b_int = np.polyint(b_poly)
    c_int = np.polyint(c_poly)
    area_b = np.polyval(b_int, hi) - np.polyval(b_int, lo)
    area_c = np.polyval(c_int, hi) - np.polyval(c_int, lo)

    avg_diff = (area_c - area_b) / (hi - lo)
    return float((10.0 ** avg_diff - 1.0) * 100.0)


def bd_rate_ladder(base_by_scen, cand_by_scen, rungs, order=None):
    """Per-clip BD-rate across one ladder.

    Returns (results, skipped) where results is [(filename, bdrate)] for every
    clip present, decodable and scored at every rung in both builds.
    """
    results = []
    skipped = 0

    sets = []
    for r in rungs:
        if r not in base_by_scen or r not in cand_by_scen:
            return [], 0
        sets.append(set(base_by_scen[r]) & set(cand_by_scen[r]))
    common = set.intersection(*sets) if sets else set()

    for fn in sorted(common):
        base_pts, cand_pts, usable = [], [], True
        for r in rungs:
            brec = base_by_scen[r][fn]
            crec = cand_by_scen[r][fn]
            if brec.get("decode_error") or crec.get("decode_error"):
                usable = False
                break
            if brec.get("mos") is None or crec.get("mos") is None:
                usable = False
                break
            base_pts.append((brec.get("bitrate"), brec["mos"]))
            cand_pts.append((crec.get("bitrate"), crec["mos"]))
        if not usable:
            skipped += 1
            continue

        bd = bd_rate_curve(base_pts, cand_pts, order=order)
        if bd is None or abs(bd) > IMPLAUSIBLE_PCT:
            skipped += 1
            continue
        results.append((fn, bd))

    return results, skipped


def summarize(results):
    """Mean/median/spread over a ladder's per-clip BD-rates."""
    import numpy as np
    if not results:
        return None
    vals = np.array([r[1] for r in results], dtype=float)
    return {
        "n": int(vals.size),
        "mean": float(vals.mean()),
        "median": float(np.median(vals)),
        "min": float(vals.min()),
        "max": float(vals.max()),
        "better": int((vals < 0).sum()),
        "worse": int((vals > 0).sum()),
    }


def analyze(base_matrix, cand_matrix, min_rungs=MIN_RUNGS):
    """Full BD-rate analysis of two result matrices.

    Returns {"segments": [...], "notes": [...]}, one segment per valid ladder.
    """
    base_by_scen = index_by_scenario(base_matrix)
    cand_by_scen = index_by_scenario(cand_matrix)
    ladders, notes = find_ladders(base_by_scen, cand_by_scen, min_rungs)

    any_object_type = any(
        rec.get("object_type") for rec in list(base_matrix.values())[:5000])
    if not any_object_type:
        notes.append(
            "No object_type recorded in the baseline results: ladders are "
            "pooled per corpus. A pooled fit across an HE->LC switch understates "
            "the loss (it halved it on #454). Re-run phase1_encode.py with a "
            "build whose frontend prints the resolved object type.")

    segments = []
    for lad in ladders:
        n = len(lad["rungs"])
        order = 3 if n >= 4 else 2
        results, skipped = bd_rate_ladder(
            base_by_scen, cand_by_scen, lad["rungs"], order=order)
        stats = summarize(results)
        segments.append({
            "corpus": lad["corpus"],
            "object_type": lad["object_type"],
            "rungs": lad["rungs"],
            "order": order,
            "clips": results,
            "skipped": skipped,
            "stats": stats,
        })
    return {"segments": segments, "notes": notes}


def self_check(matrix, min_rungs=MIN_RUNGS):
    """BD-rate of a run against itself. Must be identically zero.

    Cheap, and it is the only test that catches a fit or overlap bug without a
    second run to compare against: identical curves have to integrate to
    identical areas whatever the polynomial does in between.
    """
    out = analyze(matrix, matrix, min_rungs)
    worst = 0.0
    for seg in out["segments"]:
        for _, bd in seg["clips"]:
            worst = max(worst, abs(bd))
    return worst, out


def format_report(analysis, top=5):
    lines = []
    for seg in analysis["segments"]:
        ot = seg["object_type"] or "object type unrecorded"
        label = f"{seg['corpus']} / {ot}"
        rungs = ", ".join(seg["rungs"])
        lines.append(f"## BD-rate: {label}")
        lines.append("")
        lines.append(f"- rungs ({len(seg['rungs'])}, order-{seg['order']} fit): {rungs}")
        st = seg["stats"]
        if not st:
            lines.append("- no clip scored at every rung in both builds")
            lines.append("")
            continue
        lines.append(f"- clips: {st['n']} (skipped {seg['skipped']})")
        lines.append(f"- **mean BD-rate: {st['mean']:+.3f}%**  "
                     f"(median {st['median']:+.3f}%, "
                     f"range {st['min']:+.3f}%..{st['max']:+.3f}%)")
        lines.append(f"- candidate better on {st['better']} clip(s), "
                     f"worse on {st['worse']}")
        lines.append("")
        ranked = sorted(seg["clips"], key=lambda r: r[1])
        lines.append("| clip | BD-rate % |")
        lines.append("|---|---|")
        for fn, bd in ranked[:top]:
            lines.append(f"| {fn} | {bd:+.3f} |")
        if len(ranked) > 2 * top:
            lines.append("| ... | |")
        for fn, bd in ranked[-top:][::-1]:
            lines.append(f"| {fn} | {bd:+.3f} |")
        lines.append("")

    if analysis["notes"]:
        lines.append("### Notes")
        lines.append("")
        for n in analysis["notes"]:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("Positive = candidate needs more bits for equal quality "
                 "= worse. Never pool segments: an object-type switch inside a "
                 "ladder makes the pooled figure smaller than either half.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Bjontegaard-delta rate between two benchmark result JSONs.")
    parser.add_argument("base", help="Baseline result JSON")
    parser.add_argument("cand", nargs="?",
                        help="Candidate result JSON (omit for --self-check)")
    parser.add_argument("--self-check", action="store_true",
                        help="BD-rate of the baseline against itself; must be 0")
    parser.add_argument("--min-rungs", type=int, default=MIN_RUNGS,
                        help=f"Rungs a ladder needs to be fitted (default {MIN_RUNGS})")
    parser.add_argument("--top", type=int, default=5,
                        help="Best/worst clips to list per segment")
    args = parser.parse_args()

    from utils import load_results

    base = (load_results(args.base) or {}).get("matrix", {})
    if not base:
        sys.stderr.write(f"No matrix in {args.base}\n")
        return 2

    if args.self_check:
        worst, out = self_check(base, args.min_rungs)
        print(format_report(out, args.top))
        print(f"\nSelf-check: max |BD-rate| = {worst:.6f}%")
        if worst > 1e-6:
            sys.stderr.write("Self-check FAILED: a run must score 0 against itself\n")
            return 1
        return 0

    if not args.cand:
        parser.error("candidate JSON is required unless --self-check is given")

    cand = (load_results(args.cand) or {}).get("matrix", {})
    if not cand:
        sys.stderr.write(f"No matrix in {args.cand}\n")
        return 2

    print(format_report(analyze(base, cand, args.min_rungs), args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
