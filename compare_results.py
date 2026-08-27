"""
 * FAAC Benchmark Suite
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""

import json
import sys
import os
import argparse
from collections import defaultdict
from utils import get_scenario_sort_key
import transient


# Minimum pooled onset count before the transient-fidelity (attack-centroid-
# shift) row is even shown -- a sanity floor, not a promise of resolving
# power. A 128k-vs-96k bitrate-ladder sensitivity check (a ~25% bitrate cut,
# a plausibly PR-sized effect) found the metric mostly INCONCLUSIVE at
# single-clip scale (n=28-57 onsets, 3 of 4 clips) but resolved cleanly once
# pooled across all 4 gate clips of that one scenario pair (n=148, CI
# [+0.013, +0.045]ms, sign-test p=0.006) -- see docs/metrics.md. So this
# floor alone does not guarantee a real effect resolves; what does is
# pooling at the whole-suite level (this row, not the per-scenario table
# cells), which a default `--gate` run (all scenarios, not `--scenarios`-
# narrowed) comfortably exceeds. Below this floor the row is omitted
# entirely rather than shown as a number the data can't support.
MIN_CENTROID_ONSETS = 30


# Set from main()'s --strict-decode. When False (default), a candidate that
# failed decode validation is counted and reported but does NOT fail the run,
# so the long-reliable LC benchmark can't be red-walled by a benign ffmpeg
# stderr quirk before the strict check is proven clean on the LC corpus.
STRICT_DECODE = False


# Footprint gate. Section sums are deterministic for a fixed source and
# toolchain, so the only question is how large a legitimate commit-to-commit
# step is. Replaying 30 commits put routine work at or below +1556 bytes and
# notable work at or above +3416, so 2048 sits in an empty band. It does not
# separate wanted from unwanted growth -- four feature commits are larger than
# the regression that motivated this -- so growth is acknowledged, not
# thresholded away. See docs/footprint-gate.md.
FOOTPRINT_FAIL_BYTES = 2048
FOOTPRINT_FAIL_FRAC = 0.005
FOOTPRINT_WARN_BYTES = 512

# Bytes of growth the author has explicitly accepted (--footprint-allow).
FOOTPRINT_ALLOW = 0

# Gate names allowed to fail the run (--gates). None means all of them. A
# footprint-only job measures no MOS, and a run that cannot measure a gate must
# not fail on it -- but the gate still appears in the report, as a skip.
ENABLED_GATES = None


def bootstrap_mean_ci(values, n=2000, seed=0):
    """Percentile bootstrap CI for a mean, and a sign count.

    The block-switch retune (+0.0215) and the START/STOP tightening (+0.0124)
    were both below the per-scenario MDE and were trusted because nearly every
    clip moved the same way. That evidence has to be visible in the report, not
    reconstructed by hand afterwards.
    """
    import random

    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return None
    rng = random.Random(seed)
    n_v = len(vals)
    draws = sorted(sum(rng.choice(vals) for _ in range(n_v)) / n_v
                   for _ in range(n))
    wins = sum(1 for v in vals if v > 0.005)
    losses = sum(1 for v in vals if v < -0.005)
    return {
        "mean": sum(vals) / n_v,
        "lo": draws[int(0.025 * n)],
        "hi": draws[min(int(0.975 * n), n - 1)],
        "n": n_v,
        "wins": wins,
        "losses": losses,
    }


def add_gate(suite_results, name, status, detail):
    """Record one gate decision. "fail" is the only thing that fails the run.

    Every axis of the goal -- quality, footprint, throughput -- reports through
    here, so the verdict is a list of named decisions rather than a single
    boolean set from several places. "skip" exists because a gate that cannot
    be evaluated (no data, mismatched toolchain) must be visible in the report:
    a silently skipped gate reads exactly like a passing one, which is how a
    +13.9% library regression survived nine commits.
    """
    if ENABLED_GATES is not None and name not in ENABLED_GATES:
        if status in ("fail", "warn"):
            status = "skip"
            detail = f"not selected by --gates; would have been: {detail}"
    suite_results["gates"].append(
        {"name": name, "status": status, "detail": detail})
    if status == "fail":
        suite_results["has_regression"] = True


def check_footprint(suite_results, base, cand):
    """Gate .text + .rodata of the release shared library.

    Whole-file size moves with symbol tables, build IDs and section padding for
    reasons unrelated to code, which is why lib_size stayed display-only. The
    section sum does not move for those reasons, so it can carry a gate.
    """
    b_sec = base.get("lib_sections") or {}
    c_sec = cand.get("lib_sections") or {}
    if not b_sec or not c_sec:
        add_gate(suite_results, "footprint", "skip",
                 "no section sizes in results (pre-dates the metric?)")
        return

    b_fp = get_toolchain_fp_key(base)
    c_fp = get_toolchain_fp_key(cand)
    if b_fp != c_fp:
        add_gate(suite_results, "footprint", "skip",
                 f"toolchain differs: base {b_fp or 'unknown'} vs cand {c_fp or 'unknown'}")
        return

    b_code = b_sec.get("text", 0) + b_sec.get("rodata", 0)
    c_code = c_sec.get("text", 0) + c_sec.get("rodata", 0)
    if b_code <= 0:
        add_gate(suite_results, "footprint", "skip", "baseline code size is zero")
        return

    delta = c_code - b_code
    frac = delta / b_code
    suite_results["footprint_delta"] = delta
    suite_results["footprint_frac"] = frac * 100
    detail = (f".text+.rodata {b_code} -> {c_code} "
              f"({delta:+d} bytes, {frac * 100:+.2f}%)")

    if delta > FOOTPRINT_FAIL_BYTES and frac > FOOTPRINT_FAIL_FRAC:
        if delta <= FOOTPRINT_ALLOW:
            add_gate(suite_results, "footprint", "warn",
                     f"{detail}; accepted via --footprint-allow {FOOTPRINT_ALLOW}")
        else:
            add_gate(suite_results, "footprint", "fail",
                     f"{detail}; if intended, re-run with "
                     f"--footprint-allow {delta}")
    elif delta > FOOTPRINT_WARN_BYTES:
        add_gate(suite_results, "footprint", "warn", detail)
    else:
        add_gate(suite_results, "footprint", "pass", detail)

    # Ungated attribution: turn "the library grew" into "frame.c.o grew".
    b_obj = base.get("object_text") or {}
    c_obj = cand.get("object_text") or {}
    movers = sorted(
        ((c_obj.get(k, 0) - b_obj.get(k, 0), k)
         for k in set(b_obj) | set(c_obj)),
        key=lambda kv: -abs(kv[0]))
    suite_results["object_movers"] = [m for m in movers if m[0]][:8]


# Throughput gate. Requiring the whole confidence interval past the line, not
# just the point estimate, is what stops a noisy runner failing a clean PR.
TP_FAIL_RATIO = 1.05
TP_WARN_RATIO = 1.02
TP_BOOTSTRAP_N = 2000


def _bootstrap_tp_ratio(base_s, cand_s, rng):
    """95% CI for candidate/baseline encode time, pooled over signals.

    Resamples within each signal to carry timing noise, takes the minimum --
    the same estimator phase1 reports, since interference is one-sided -- and
    averages log-ratios across signals so no single slow signal dominates.
    """
    import math

    signals = [k for k in cand_s if k in base_s
               and base_s[k] and cand_s[k]]
    if not signals:
        return None

    draws = []
    for _ in range(TP_BOOTSTRAP_N):
        logs = []
        for k in signals:
            b = base_s[k]
            c = cand_s[k]
            bm = min(rng.choice(b) for _ in b)
            cm = min(rng.choice(c) for _ in c)
            if bm > 0 and cm > 0:
                logs.append(math.log(cm / bm))
        if logs:
            draws.append(math.exp(sum(logs) / len(logs)))

    if not draws:
        return None
    draws.sort()
    lo = draws[int(0.025 * len(draws))]
    hi = draws[min(int(0.975 * len(draws)), len(draws) - 1)]
    point = draws[len(draws) // 2]
    return point, lo, hi, len(signals)


def check_throughput(suite_results, base, cand):
    """Gate encode time on the lower bound of the cand/base ratio."""
    import random

    base_s = base.get("throughput_samples") or {}
    cand_s = cand.get("throughput_samples") or {}
    if not base_s or not cand_s:
        add_gate(suite_results, "throughput", "skip",
                 "no per-run timing samples (pre-dates the metric?)")
        return

    # Same machine, same boot, or the comparison is between two hosts rather
    # than two builds. A cached baseline carries the timings of whatever runner
    # produced it, which is why this is checked rather than assumed.
    b_host = get_fp_key(base, "host_fp")
    c_host = get_fp_key(cand, "host_fp")
    if b_host != c_host:
        add_gate(suite_results, "throughput", "skip",
                 "baseline timings came from a different machine or boot "
                 "(stale cache?); re-run the baseline with --throughput-only "
                 "on this host to gate")
        return

    res = _bootstrap_tp_ratio(base_s, cand_s, random.Random(0))
    if res is None:
        add_gate(suite_results, "throughput", "skip",
                 "no signal measured on both sides")
        return

    point, lo, hi, n = res
    detail = (f"encode time x{point:.3f} (95% CI {lo:.3f}-{hi:.3f}) "
              f"over {n} signal(s)")
    suite_results["tp_ratio"] = point

    # The CI must clear the line entirely, so an ambiguous result warns rather
    # than fails.
    if lo > TP_FAIL_RATIO:
        add_gate(suite_results, "throughput", "fail", detail)
    elif lo > TP_WARN_RATIO:
        add_gate(suite_results, "throughput", "warn", detail)
    else:
        add_gate(suite_results, "throughput", "pass", detail)


def get_fp_key(results, field):
    """Stable string identity from a fingerprint dict in a results file."""
    fp = results.get(field) or {}
    if not fp:
        return ""
    return "|".join(f"{k}={fp[k]}" for k in sorted(fp))


def get_toolchain_fp_key(results):
    """Stable string identity of the toolchain that produced a results file."""
    return get_fp_key(results, "toolchain_fp")


def analyze_pair(base_file, cand_file):
    try:
        with open(base_file, "r") as f:
            base = json.load(f)
    except Exception as e:
        sys.stderr.write(
            f"  Warning: Could not load baseline file {base_file}: {e}\n")
        base = {}

    try:
        with open(cand_file, "r") as f:
            cand = json.load(f)
    except Exception as e:
        sys.stderr.write(
            f"  Error: Could not load candidate file {cand_file}: {e}\n")
        return None

    base_m = base.get("matrix", {})
    cand_m = cand.get("matrix", {})

    suite_results = {
        "gates": [],
        "has_regression": False,
        "decode_error_count": 0,
        "missing_data": False,
        "mos_delta_sum": 0,
        "mos_count": 0,
        "missing_mos_count": 0,
        "mos_deltas": [],
        "clip_wins": 0,
        "clip_losses": 0,
        "ic_delta_sum": 0,
        "ic_count": 0,
        "worst_ic_regression": (0, "N/A"),
        "centroid_deltas": [],
        "centroid_o_abs": [],
        "centroid_b_abs": [],
        "worst_centroid_regression": (0, "N/A"),
        "tp_reduction": 0,
        "lib_size_chg": 0,
        "lib_text_chg": 0,
        "lib_rodata_chg": 0,
        "bitrate_chg_sum": 0,
        "bitrate_count": 0,
        "bitrate_acc_sum": 0,
        "bitrate_acc_count": 0,
        "bitrate_bias_sum": 0,
        "regressions": [],
        "reg_critical": [],
        "reg_significant": [],
        "reg_minor": [],
        "new_wins": [],
        "significant_wins": [],
        "opportunities": [],
        "bit_exact_count": 0,
        "total_cases": 0,
        "all_cases": [],
        "worst_mos_drop": (0, "N/A"),
        "worst_bitrate_err": (0, "N/A"),
        "scenario_stats": defaultdict(
            lambda: {
                "tp_sum_cand": 0,
                "tp_sum_base": 0,
                "mos_delta_sum": 0,
                "mos_count": 0,
                "ic_delta_sum": 0,
                "ic_count": 0,
                "bitrate_acc_sum": 0,
                "bitrate_acc_count": 0,
                "mos_deltas": [],
                "centroid_deltas": [],
                "centroid_o_abs": [],
                "centroid_b_abs": [],
                "count": 0}),
        "base_tp": base.get("throughput", {}),
        "cand_tp": cand.get("throughput", {}),
        "base_sha": base.get("sha"),
        "cand_sha": cand.get("sha"),
        "rate_control_mode": next((o.get("rate_control_mode") for o in cand_m.values() if o.get("rate_control_mode")), "abr"),
        "mos_backend": next((o.get("mos_backend") or o.get("mos_provider") for o in cand_m.values() if o.get("mos_backend") or o.get("mos_provider")), "zimtohrli")
    }

    if cand_m:
        suite_results["total_cases"] = len(cand_m)
        # Sort by dataset/bitrate, then filename. Precompute keys for performance.
        decorated = []
        for k, o in cand_m.items():
            scen_key = get_scenario_sort_key(o.get("scenario", ""))
            filename = o.get("filename", k)
            decorated.append(((scen_key, filename), k))

        decorated.sort()
        for _, k in decorated:
            o = cand_m[k]
            b = base_m.get(k, {})

            filename = o.get("filename", k)
            scenario = o.get("scenario", "")
            display_name = f"{scenario}: {filename}"

            o_mos = o.get("mos")
            b_mos = b.get("mos")
            thresh = o.get("thresh", 1.0)

            o_size = o.get("size")
            b_size = b.get("size")

            o_bitrate = o.get("bitrate")
            o_target = o.get("bitrate_target")

            acc = None
            bitrate_err = None
            if o_bitrate is not None and o_target is not None and o_target > 0:
                bitrate_err = (o_bitrate - o_target) / o_target * 100
                acc = (1.0 - abs(o_bitrate - o_target) / o_target) * 100
                suite_results["bitrate_acc_sum"] += acc
                suite_results["bitrate_acc_count"] += 1
                suite_results["bitrate_bias_sum"] += bitrate_err
                suite_results["scenario_stats"][scenario]["bitrate_acc_sum"] += acc
                suite_results["scenario_stats"][scenario]["bitrate_acc_count"] += 1

                if abs(bitrate_err) > abs(suite_results["worst_bitrate_err"][0]):
                    suite_results["worst_bitrate_err"] = (bitrate_err, display_name)

            o_time = o.get("time")
            b_time = b.get("time")
            speed_delta = None

            if o_time is not None and b_time is not None and b_time > 0:
                suite_results["scenario_stats"][scenario]["tp_sum_cand"] += o_time
                suite_results["scenario_stats"][scenario]["tp_sum_base"] += b_time
                suite_results["scenario_stats"][scenario]["count"] += 1
                speed_delta = (1 - o_time / b_time) * 100


            # Stereo image fidelity (Phase 3). ic_err = inter-channel coherence
            # error, lower = truer stereo image. Sign matches MOS: a positive
            # delta means the candidate improved fidelity (reduced the error).
            o_ic = o.get("ic_err")
            b_ic = b.get("ic_err")
            if o_ic is not None and b_ic is not None:
                ic_delta = b_ic - o_ic
                suite_results["ic_delta_sum"] += ic_delta
                suite_results["ic_count"] += 1
                suite_results["scenario_stats"][scenario]["ic_delta_sum"] += ic_delta
                suite_results["scenario_stats"][scenario]["ic_count"] += 1
                if ic_delta < suite_results["worst_ic_regression"][0]:
                    suite_results["worst_ic_regression"] = (ic_delta, display_name)

            # Transient fidelity (Phase 3 fold-in, see phase3_stereo.py).
            # attack_centroid_ms is itself a per-onset "decoded vs reference"
            # delta (ms); comparing |cand| against |base| per onset shows
            # whether the candidate smears attacks more or less than
            # baseline. Sign convention: negative == the candidate's onset
            # moved closer to 0 (the reference) == improvement, matching
            # docs/metrics.md. Paired positionally per onset -- lengths
            # match in the near-100% yield case Stage 1 validated; a clip
            # where they don't (a dropped onset on one side) is skipped
            # rather than guessed at.
            o_centroid = o.get("attack_centroid_ms")
            b_centroid = b.get("attack_centroid_ms")
            if o_centroid and b_centroid and len(o_centroid) == len(b_centroid):
                clip_deltas = [abs(ov) - abs(bv) for ov, bv in zip(o_centroid, b_centroid)]
                suite_results["centroid_deltas"].extend(clip_deltas)
                suite_results["scenario_stats"][scenario]["centroid_deltas"].extend(clip_deltas)
                # Raw |ms| pooled separately (not just the paired delta) so a
                # significant verdict can be paired with the same 0-1
                # fidelity number compare_encoders.py's leaderboard reports
                # (1 / (1 + mean|ms|)), for a maintainer to read as "how much".
                o_abs_list = [abs(v) for v in o_centroid]
                b_abs_list = [abs(v) for v in b_centroid]
                suite_results["centroid_o_abs"].extend(o_abs_list)
                suite_results["centroid_b_abs"].extend(b_abs_list)
                suite_results["scenario_stats"][scenario]["centroid_o_abs"].extend(o_abs_list)
                suite_results["scenario_stats"][scenario]["centroid_b_abs"].extend(b_abs_list)
                clip_mean = sum(clip_deltas) / len(clip_deltas)
                if clip_mean > suite_results["worst_centroid_regression"][0]:
                    suite_results["worst_centroid_regression"] = (clip_mean, display_name)

            o_md5 = o.get("md5", "")
            b_md5 = b.get("md5", "")

            if o_md5 and b_md5 and o_md5 == b_md5:
                suite_results["bit_exact_count"] += 1

            size_chg = "N/A"
            if o_size is not None and b_size is not None and b_size > 0:
                size_chg_val = (o_size - b_size) / b_size * 100
                size_chg = f"{size_chg_val:+.2f}%"
                suite_results["bitrate_chg_sum"] += size_chg_val
                suite_results["bitrate_count"] += 1
            elif o_size is None:
                suite_results["missing_data"] = True

            status = "✅"
            delta = 0
            bit_exact = "MATCH" if o_md5 and b_md5 and o_md5 == b_md5 else "❌"

            if o_mos is not None:
                if b_mos is not None:
                    delta = o_mos - b_mos
                    suite_results["mos_delta_sum"] += delta
                    suite_results["mos_count"] += 1
                    suite_results["mos_deltas"].append(delta)
                    if delta > 0.005:
                        suite_results["clip_wins"] += 1
                    elif delta < -0.005:
                        suite_results["clip_losses"] += 1
                    suite_results["scenario_stats"][scenario]["mos_delta_sum"] += delta
                    # Retained so a scenario mean can carry a CI and a sign
                    # count. A mean of +0.02 built from 49 consistent small
                    # wins and one built from 3 large ones against 46 losses
                    # print identically without this.
                    suite_results["scenario_stats"][scenario]["mos_deltas"].append(delta)
                    suite_results["scenario_stats"][scenario]["mos_count"] += 1

                    if delta < suite_results["worst_mos_drop"][0]:
                        suite_results["worst_mos_drop"] = (delta, display_name)

                if o_mos < (thresh - 0.5):
                    status = "🤮"  # Awful
                elif o_mos < thresh:
                    status = "📉"  # Bad/Poor

                if b_mos is not None:
                    if b_mos >= thresh and o_mos < thresh:
                        status = "💀" # Critical Regression
                        suite_results["has_regression"] = True
                    elif delta < -0.1:
                        status = "❌"  # Significant Regression
                        suite_results["has_regression"] = True
                    elif delta < -0.05:
                        status = "⚠️"  # Minor Regression
                    elif delta > 0.1:
                        status = "🌟"  # Significant Win

                # Check for New Win (Baseline failed, Candidate passed)
                if b_mos is not None and b_mos < thresh and o_mos >= thresh:
                    suite_results["new_wins"].append({
                        "display_name": display_name,
                        "mos": o_mos,
                        "b_mos": b_mos,
                        "delta": delta
                    })

            if o_mos is None:
                status = "❌"  # Missing MOS is a failure
                suite_results["missing_mos_count"] += 1
                suite_results["has_regression"] = True
                suite_results["missing_data"] = True
                delta = -10.0  # Force to top of regressions

            # Decode validation (phase1). A candidate that fails to decode is a
            # broken bitstream regardless of MOS. Always count it; only hard-fail
            # the run when --strict-decode is set (see STRICT_DECODE note above).
            if o.get("decode_error"):
                suite_results["decode_error_count"] += 1
                if STRICT_DECODE:
                    status = "💀"
                    suite_results["has_regression"] = True
                    delta = min(delta, -9.0)  # sort near the top of regressions

            mos_str = f"{o_mos:.2f}" if o_mos is not None else "N/A"
            b_mos_str = f"{b_mos:.2f}" if b_mos is not None else "N/A"
            delta_mos = f"{(o_mos - b_mos):+.2f}" if (
                o_mos is not None and b_mos is not None) else "N/A"
            target_str = f"{o_target}k" if o_target else "N/A"
            actual_str = f"{o_bitrate:.1f}k" if o_bitrate else "N/A"
            acc_str = f"{acc:.1f}%" if acc is not None else "N/A"
            speed_str = f"{speed_delta:+.1f}%" if speed_delta is not None else "N/A"

            case_data = {
                "display_name": display_name,
                "status": status,
                "mos": o_mos,
                "b_mos": b_mos,
                "delta": delta,
                "size_chg": size_chg,
                "line": f"| {display_name} | {status} | {mos_str} ({b_mos_str}) | {delta_mos} | {target_str} | {actual_str} | {acc_str} | {speed_str} | {bit_exact} |"
            }

            suite_results["all_cases"].append(case_data)
            if status == "💀":
                suite_results["reg_critical"].append(case_data)
                suite_results["regressions"].append(case_data)
            elif status == "❌":
                suite_results["reg_significant"].append(case_data)
                suite_results["regressions"].append(case_data)
            elif status == "⚠️":
                suite_results["reg_minor"].append(case_data)
                suite_results["regressions"].append(case_data)
            elif status == "🌟":
                suite_results["significant_wins"].append(case_data)
            elif status in ["🤮", "📉"]:
                suite_results["opportunities"].append(case_data)
    else:
        suite_results["missing_data"] = True

    # Sorts
    suite_results["reg_critical"].sort(key=lambda x: x["delta"])
    suite_results["reg_significant"].sort(key=lambda x: x["delta"])
    suite_results["reg_minor"].sort(key=lambda x: x["delta"])
    suite_results["regressions"].sort(key=lambda x: x["delta"])
    suite_results["new_wins"].sort(key=lambda x: x["delta"], reverse=True)
    suite_results["significant_wins"].sort(
        key=lambda x: x["delta"], reverse=True)
    suite_results["opportunities"].sort(
        key=lambda x: x["mos"] if x["mos"] is not None else 6.0)

    # Record the per-clip MOS verdict as a named gate too, so the gate list is
    # the whole story and not just the axes added later.
    n_crit = len(suite_results["reg_critical"])
    n_sig = len(suite_results["reg_significant"])
    n_min = len(suite_results["reg_minor"])
    if n_crit or n_sig:
        add_gate(suite_results, "mos", "fail",
                 f"{n_crit} critical, {n_sig} past -0.10, {n_min} past -0.05")
    elif n_min:
        add_gate(suite_results, "mos", "warn", f"{n_min} clip(s) past -0.05")
    elif suite_results["mos_count"]:
        add_gate(suite_results, "mos", "pass",
                 f"no clip past -0.05 over {suite_results['mos_count']} clips")
    else:
        add_gate(suite_results, "mos", "skip", "no MOS data in results")

    # Throughput
    base_tp = base.get("throughput", {})
    cand_tp = cand.get("throughput", {})
    # Exclude "overall" to avoid double-counting in manual summation
    total_base_t = sum(v for k, v in base_tp.items() if k != "overall")
    total_cand_t = sum(v for k, v in cand_tp.items() if k != "overall")
    if total_cand_t > 0 and total_base_t > 0:
        suite_results["tp_reduction"] = (1 - total_cand_t / total_base_t) * 100
    else:
        # If overall throughput is missing, try to aggregate from scenarios
        cand_t_sum = sum(s["tp_sum_cand"]
                         for s in suite_results["scenario_stats"].values())
        base_t_sum = sum(s["tp_sum_base"]
                         for s in suite_results["scenario_stats"].values())
        if cand_t_sum > 0 and base_t_sum > 0:
            suite_results["tp_reduction"] = (1 - cand_t_sum / base_t_sum) * 100
        else:
            suite_results["missing_data"] = True

    # Binary Size
    base_lib = base.get("lib_size", 0)
    cand_lib = cand.get("lib_size", 0)
    if cand_lib > 0 and base_lib > 0:
        suite_results["lib_size_chg"] = ((cand_lib / base_lib) - 1) * 100
    else:
        suite_results["missing_data"] = True

    # Granular Section Sizes (.text, .rodata)
    base_text = base.get("lib_text_size", 0)
    cand_text = cand.get("lib_text_size", 0)
    if cand_text > 0 and base_text > 0:
        suite_results["lib_text_chg"] = ((cand_text / base_text) - 1) * 100
    else:
        suite_results["lib_text_chg"] = 0.0

    base_rodata = base.get("lib_rodata_size", 0)
    cand_rodata = cand.get("lib_rodata_size", 0)
    if cand_rodata > 0 and base_rodata > 0:
        suite_results["lib_rodata_chg"] = ((cand_rodata / base_rodata) - 1) * 100
    else:
        suite_results["lib_rodata_chg"] = 0.0

    check_footprint(suite_results, base, cand)
    check_throughput(suite_results, base, cand)

    return suite_results


def aggregate_suite_metrics(items):
    """Pool the per-suite dicts from analyze_pair() into one set of averages.

    Called once over every suite for the pass/fail headline, and again per
    rate-control mode for the Summary table -- ABR and VBR runs must never be
    averaged together into a single "Bitrate Accuracy" number, since ABR's
    target-bitrate semantics and VBR's allocation semantics aren't the same
    quantity.
    """
    total_mos_delta = total_mos_count = total_missing_mos = total_decode_errors = 0
    total_clip_wins = total_clip_losses = 0
    total_ic_delta = total_ic_count = 0
    worst_ic_regression = (0, "N/A")
    total_centroid_deltas = []
    total_centroid_o_abs = []
    total_centroid_b_abs = []
    worst_centroid_regression = (0, "N/A")
    total_tp_reduction = total_lib_chg = total_lib_text_chg = total_lib_rodata_chg = 0
    total_bitrate_chg = total_bitrate_count = 0
    total_bitrate_acc_sum = total_bitrate_acc_count = total_bitrate_bias_sum = 0
    total_regressions = total_reg_critical = total_reg_significant = total_reg_minor = 0
    total_new_wins = total_significant_wins = 0
    total_bit_exact = total_cases_all = 0
    worst_mos_drop = (0, "N/A")
    worst_bitrate_err = (0, "N/A")
    scenario_tp_deltas = []
    n_suites = 0

    for name, data in items:
        n_suites += 1
        total_mos_delta += data["mos_delta_sum"]
        total_mos_count += data["mos_count"]
        total_clip_wins += data.get("clip_wins", 0)
        total_clip_losses += data.get("clip_losses", 0)
        total_missing_mos += data["missing_mos_count"]
        total_decode_errors += data["decode_error_count"]
        total_ic_delta += data["ic_delta_sum"]
        total_ic_count += data["ic_count"]
        if data["worst_ic_regression"][0] < worst_ic_regression[0]:
            worst_ic_regression = data["worst_ic_regression"]
        total_centroid_deltas.extend(data.get("centroid_deltas", []))
        total_centroid_o_abs.extend(data.get("centroid_o_abs", []))
        total_centroid_b_abs.extend(data.get("centroid_b_abs", []))
        if data.get("worst_centroid_regression", (0, "N/A"))[0] > worst_centroid_regression[0]:
            worst_centroid_regression = data["worst_centroid_regression"]
        total_tp_reduction += data["tp_reduction"]
        total_lib_chg += data["lib_size_chg"]
        total_lib_text_chg += data.get("lib_text_chg", 0)
        total_lib_rodata_chg += data.get("lib_rodata_chg", 0)
        total_bitrate_chg += data["bitrate_chg_sum"]
        total_bitrate_count += data["bitrate_count"]
        total_bitrate_acc_sum += data["bitrate_acc_sum"]
        total_bitrate_acc_count += data["bitrate_acc_count"]
        total_bitrate_bias_sum += data["bitrate_bias_sum"]

        total_regressions += len(data["regressions"])
        total_reg_critical += len(data["reg_critical"])
        total_reg_significant += len(data["reg_significant"])
        total_reg_minor += len(data["reg_minor"])

        total_new_wins += len(data["new_wins"])
        total_significant_wins += len(data["significant_wins"])
        total_bit_exact += data["bit_exact_count"]
        total_cases_all += data["total_cases"]

        if data["worst_mos_drop"][0] < worst_mos_drop[0]:
            worst_mos_drop = data["worst_mos_drop"]
        if abs(data["worst_bitrate_err"][0]) > abs(worst_bitrate_err[0]):
            worst_bitrate_err = data["worst_bitrate_err"]

        for sc_name, sc_data in data["scenario_stats"].items():
            if sc_data["tp_sum_base"] > 0:
                delta = (1 - sc_data["tp_sum_cand"] /
                         sc_data["tp_sum_base"]) * 100
                scenario_tp_deltas.append((f"{name} / {sc_name}", delta))

    avg_mos_delta_str = f"{(total_mos_delta / total_mos_count):+.3f}" if total_mos_count > 0 else "N/A"
    avg_tp_reduction = total_tp_reduction / n_suites if n_suites else 0
    avg_lib_chg = total_lib_chg / n_suites if n_suites else 0
    avg_lib_text_chg = total_lib_text_chg / n_suites if n_suites else 0
    avg_lib_rodata_chg = total_lib_rodata_chg / n_suites if n_suites else 0
    avg_bitrate_chg = total_bitrate_chg / total_bitrate_count if total_bitrate_count > 0 else 0
    avg_bitrate_acc = total_bitrate_acc_sum / total_bitrate_acc_count if total_bitrate_acc_count > 0 else 0
    avg_bitrate_bias = total_bitrate_bias_sum / total_bitrate_acc_count if total_bitrate_acc_count > 0 else 0
    bit_exact_percent = (total_bit_exact / total_cases_all * 100) if total_cases_all > 0 else 0

    worst_tp_scen, worst_tp_delta = (None, 0)
    if scenario_tp_deltas:
        worst_tp_scen, worst_tp_delta = min(scenario_tp_deltas, key=lambda x: x[1])

    return {
        "avg_mos_delta_str": avg_mos_delta_str,
        "total_mos_delta": total_mos_delta, "total_mos_count": total_mos_count,
        "total_clip_wins": total_clip_wins, "total_clip_losses": total_clip_losses,
        "total_missing_mos": total_missing_mos,
        "total_decode_errors": total_decode_errors,
        "total_ic_delta": total_ic_delta, "total_ic_count": total_ic_count,
        "worst_ic_regression": worst_ic_regression,
        "centroid_deltas": total_centroid_deltas,
        "centroid_o_abs": total_centroid_o_abs,
        "centroid_b_abs": total_centroid_b_abs,
        "worst_centroid_regression": worst_centroid_regression,
        "avg_tp_reduction": avg_tp_reduction,
        "avg_lib_chg": avg_lib_chg, "avg_lib_text_chg": avg_lib_text_chg,
        "avg_lib_rodata_chg": avg_lib_rodata_chg,
        "avg_bitrate_chg": avg_bitrate_chg,
        "avg_bitrate_acc": avg_bitrate_acc, "avg_bitrate_bias": avg_bitrate_bias,
        "total_bitrate_acc_count": total_bitrate_acc_count,
        "bit_exact_percent": bit_exact_percent,
        "total_regressions": total_regressions, "total_reg_critical": total_reg_critical,
        "total_reg_significant": total_reg_significant, "total_reg_minor": total_reg_minor,
        "total_new_wins": total_new_wins, "total_significant_wins": total_significant_wins,
        "worst_mos_drop": worst_mos_drop, "worst_bitrate_err": worst_bitrate_err,
        "worst_tp_scen": worst_tp_scen, "worst_tp_delta": worst_tp_delta,
        "tp_details_source": items,
    }


def render_summary_table(metrics, mos_label, rc_mode):
    """Render the '| Metric | Value |' rows for one metrics dict.

    Shared by the single-mode summary and by each per-mode (ABR/VBR)
    subsection, so the two paths can never drift apart in formatting.
    """
    lines = ["| Metric | Value |", "| :--- | :--- |"]

    if metrics["total_regressions"] == 0:
        lines.append("| **Regressions** | 0 ✅ |")
    else:
        reg_parts = []
        if metrics["total_reg_critical"]:
            reg_parts.append(f"{metrics['total_reg_critical']} 💀")
        if metrics["total_reg_significant"]:
            reg_parts.append(f"{metrics['total_reg_significant']} ❌")
        if metrics["total_reg_minor"]:
            reg_parts.append(f"{metrics['total_reg_minor']} ⚠️")
        lines.append(f"| **Regressions** | {', '.join(reg_parts)} |")

    if metrics["worst_mos_drop"][0] < -0.01:
        lines.append(f"| **Worst {mos_label} Drop** | {metrics['worst_mos_drop'][0]:.2f} ({metrics['worst_mos_drop'][1]}) |")

    if abs(metrics["worst_bitrate_err"][0]) > 1.0:
        err_icon = "📈" if metrics["worst_bitrate_err"][0] > 0 else "📉"
        lines.append(f"| **Max Bitrate Err** | {metrics['worst_bitrate_err'][0]:+.1f}% ({metrics['worst_bitrate_err'][1]}) {err_icon} |")

    if metrics["total_new_wins"] > 0:
        lines.append(f"| **New Wins** | {metrics['total_new_wins']} 🆕 |")

    if metrics["total_significant_wins"] > 0:
        lines.append(f"| **Significant Wins** | {metrics['total_significant_wins']} 🌟 |")

    consist_status = f"{metrics['bit_exact_percent']:.1f}%"
    if metrics["bit_exact_percent"] == 100.0:
        consist_status += " (MD5 Match)"
    lines.append(f"| **Consistency** | {consist_status} |")

    if abs(metrics["avg_tp_reduction"]) > 0.1:
        tp_icon = "🚀" if metrics["avg_tp_reduction"] > 1.0 else "📉" if metrics["avg_tp_reduction"] < -1.0 else ""
        lines.append(f"| **Throughput (Avg)** | {metrics['avg_tp_reduction']:+.1f}% {tp_icon} |")

    tp_details = []
    items = metrics["tp_details_source"]
    if items:
        _, first_data = items[0]
        base_tp = first_data.get("base_tp", {})
        cand_tp = first_data.get("cand_tp", {})
        for signal in sorted(cand_tp.keys()):
            if signal == "overall":
                continue
            if signal in base_tp and base_tp[signal] > 0:
                delta = (1 - cand_tp[signal] / base_tp[signal]) * 100
                icon = "🚀" if delta > 1.0 else "📉" if delta < -1.0 else ""
                tp_details.append(f"{signal.split('.')[0]}: {delta:+.1f}% {icon}")
    if tp_details:
        lines.append(f"| **TP Breakdown** | {', '.join(tp_details)} |")

    if metrics["worst_tp_delta"] < -1.0:
        lines.append(f"| **Worst-case TP Δ** | {metrics['worst_tp_delta']:.1f}% ({metrics['worst_tp_scen']}) ⚠️ |")

    if abs(metrics["avg_lib_chg"]) > 0.001:
        size_icon = "📉" if metrics["avg_lib_chg"] < -0.1 else "📈" if metrics["avg_lib_chg"] > 0.1 else ""
        lines.append(f"| **Library Size** | {metrics['avg_lib_chg']:+.2f}% {size_icon} |")

        sec_details = []
        if abs(metrics["avg_lib_text_chg"]) > 0.001:
            sec_details.append(f".text: {metrics['avg_lib_text_chg']:+.2f}%")
        if abs(metrics["avg_lib_rodata_chg"]) > 0.001:
            sec_details.append(f".rodata: {metrics['avg_lib_rodata_chg']:+.2f}%")
        if sec_details:
            lines.append(f"| **Footprint Breakdown** | {', '.join(sec_details)} |")

    if abs(metrics["avg_bitrate_chg"]) > 0.1:
        bitrate_icon = "📉" if metrics["avg_bitrate_chg"] < -1.0 else "📈" if metrics["avg_bitrate_chg"] > 1.0 else ""
        lines.append(f"| **Bitrate Δ** | {metrics['avg_bitrate_chg']:+.2f}% {bitrate_icon} |")

    acc_label = "Allocation Accuracy" if rc_mode == "VBR" else "Bitrate Accuracy"
    bias_label = "Allocation Bias" if rc_mode == "VBR" else "Bitrate Bias"
    if metrics["total_bitrate_acc_count"] > 0:
        acc_icon = "🎯" if metrics["avg_bitrate_acc"] > 95 else "⚠️" if metrics["avg_bitrate_acc"] < 80 else ""
        lines.append(f"| **{acc_label}** | {metrics['avg_bitrate_acc']:.1f}% {acc_icon} |")

        bias_icon = "📈" if metrics["avg_bitrate_bias"] > 2.0 else "📉" if metrics["avg_bitrate_bias"] < -2.0 else "🎯"
        bias_desc = "Overshooting" if metrics["avg_bitrate_bias"] > 0.5 else "Undershooting" if metrics["avg_bitrate_bias"] < -0.5 else "Optimal"
        lines.append(f"| **{bias_label}** | {metrics['avg_bitrate_bias']:+.1f}% ({bias_desc}) {bias_icon} |")

    mos_count = metrics["total_mos_count"]
    total_wins = metrics.get("total_clip_wins", 0)
    total_losses = metrics.get("total_clip_losses", 0)
    if mos_count > 0:
        avg_delta = metrics["total_mos_delta"] / mos_count
        if total_wins > 0 or total_losses > 0 or abs(avg_delta) > 0.001:
            if total_wins > 0 and total_losses > 0:
                if abs(avg_delta) <= 0.005:
                    icon = "⚠️"
                    clip_str = f" Quality Churn ({total_wins} clip{'s' if total_wins != 1 else ''} improved, {total_losses} degraded)"
                elif avg_delta > 0.005:
                    icon = "🚀"
                    clip_str = f" ({total_wins} clip{'s' if total_wins != 1 else ''} improved, {total_losses} degraded)"
                else:
                    icon = "📉"
                    clip_str = f" ({total_wins} clip{'s' if total_wins != 1 else ''} improved, {total_losses} degraded)"
            elif total_wins > 0:
                icon = "🚀"
                clip_str = f" ({total_wins} clip{'s' if total_wins != 1 else ''} improved)"
            elif total_losses > 0:
                icon = "📉"
                clip_str = f" ({total_losses} clip{'s' if total_losses != 1 else ''} degraded)"
            else:
                icon = "🚀" if avg_delta > 0.001 else "📉" if avg_delta < -0.001 else ""
                clip_str = ""

            val_str = f"{avg_delta:+.3f} {icon}{clip_str}".strip()
            lines.append(f"| **Avg {mos_label} Δ** | {val_str} |")

    if metrics["total_ic_count"] > 0:
        avg_ic_delta = metrics["total_ic_delta"] / metrics["total_ic_count"]
        if abs(avg_ic_delta) > 0.0005:
            ic_icon = "🎧" if avg_ic_delta > 0.002 else "📉" if avg_ic_delta < -0.002 else ""
            lines.append(f"| **Stereo Fidelity Δ** | {avg_ic_delta:+.4f} {ic_icon} |")
        if metrics["worst_ic_regression"][0] < -0.005:
            lines.append(f"| **Worst Stereo Drop** | {metrics['worst_ic_regression'][0]:+.4f} ({metrics['worst_ic_regression'][1]}) |")

    # Transient fidelity (attack-centroid-shift). Diagnostic-only, no gate --
    # report a verdict, never a bare number, and omit the row entirely below
    # MIN_CENTROID_ONSETS (see its comment for why).
    centroid_deltas = metrics.get("centroid_deltas", [])
    if len(centroid_deltas) >= MIN_CENTROID_ONSETS:
        lo, hi = transient.bootstrap_ci(centroid_deltas)
        p, _neg, n = transient.sign_test_p(centroid_deltas)
        verdict = transient.ci_signtest_verdict(
            lo, hi, p, label_decrease="improved", label_increase="regression")
        if verdict in ("regression", "improved"):
            icon = "📉" if verdict == "regression" else "📈"
            o_abs = metrics.get("centroid_o_abs", [])
            b_abs = metrics.get("centroid_b_abs", [])
            if o_abs and b_abs:
                fid_o = 1.0 / (1.0 + sum(o_abs) / len(o_abs))
                fid_b = 1.0 / (1.0 + sum(b_abs) / len(b_abs))
                fid_delta = fid_o - fid_b
                fid_str = f"{fid_delta:+.4f} {icon} (n={n}, fidelity {fid_b:.3f} → {fid_o:.3f})"
            else:
                fid_str = f"{icon} (n={n})"
            lines.append(f"| **Transient Fidelity Δ** | {fid_str} |")
        if metrics["worst_centroid_regression"][0] > 0.5:
            lines.append(
                f"| **Worst Transient Regression** | "
                f"{metrics['worst_centroid_regression'][0]:+.3f}ms "
                f"({metrics['worst_centroid_regression'][1]}) |")

    if metrics["total_decode_errors"] > 0:
        gate = "hard-failed" if STRICT_DECODE else "warning only — pass --strict-decode to gate"
        lines.append(f"| **Decode Errors** | {metrics['total_decode_errors']} 🧨 ({gate}) |")

    if metrics["total_missing_mos"] > 0:
        lines.append(f"\n⚠️ **Warning**: {metrics['total_missing_mos']} MOS scores were missing/failed (treated as ❌).")

    return lines


def main():
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(description="Consolidate FAAC benchmark results.")
    parser.add_argument("results_dir", nargs="?", default=os.path.join(SCRIPT_DIR, "results"),
                        help="Path to the directory containing result JSON files")
    parser.add_argument("--base-sha", help="Baseline commit SHA")
    parser.add_argument("--cand-sha", help="Candidate commit SHA")
    parser.add_argument("--summary-only", action="store_true", help="Generate only the high-signal summary")
    parser.add_argument("--output", help="Path to write the Markdown report file")
    parser.add_argument("--summary-output", help="Path to write the Markdown summary file")
    parser.add_argument("--strict-decode", action="store_true",
                        help="Treat candidate decode-validation failures as hard regressions "
                             "(default: report only, do not fail the run)")
    parser.add_argument("--gates", metavar="NAMES",
                        help="Comma-separated gate names allowed to fail "
                             "(mos, footprint, throughput). Default: all. "
                             "Unselected gates are reported as skips.")
    parser.add_argument("--footprint-allow", type=int, default=0, metavar="BYTES",
                        help="Accept up to BYTES of .text+.rodata growth without failing. "
                             "For changes whose size cost is intended and stated in the PR.")

    args = parser.parse_args()

    global STRICT_DECODE
    STRICT_DECODE = args.strict_decode
    global FOOTPRINT_ALLOW
    FOOTPRINT_ALLOW = args.footprint_allow
    global ENABLED_GATES
    if args.gates:
        ENABLED_GATES = {g.strip() for g in args.gates.split(",") if g.strip()}

    results_dir = args.results_dir
    summary_only = args.summary_only
    base_sha = args.base_sha
    cand_sha = args.cand_sha

    if not os.path.exists(results_dir):
        sys.stderr.write(f"Error: Results directory '{results_dir}' does not exist.\n")
        sys.exit(1)

    files = os.listdir(results_dir)

    suites = {}
    for f in files:
        if f.endswith("_cand.json"):
            suite_name = f[:-10]
            base_f = suite_name + "_base.json"
            if base_f in files:
                suites[suite_name] = (
                    os.path.join(
                        results_dir, base_f), os.path.join(
                        results_dir, f))

    if not suites:
        sys.stderr.write("No result pairs found in directory.\n")
        sys.exit(1)

    all_suite_data = {}
    overall_regression = False
    overall_missing = False
    final_base_sha = base_sha
    final_cand_sha = cand_sha

    for name, (base, cand) in sorted(suites.items()):
        data = analyze_pair(base, cand)
        if data:
            all_suite_data[name] = data
            if data["has_regression"]:
                overall_regression = True
            if data["missing_data"]:
                overall_missing = True
            if not final_base_sha and data["base_sha"]:
                final_base_sha = data["base_sha"]
            if not final_cand_sha and data["cand_sha"]:
                final_cand_sha = data["cand_sha"]

    # Pooled across every suite: drives the pass/fail headline, which must
    # reflect a regression in ANY mode, not an average across modes.
    global_metrics = aggregate_suite_metrics(sorted(all_suite_data.items()))
    avg_tp_reduction = global_metrics["avg_tp_reduction"]
    bit_exact_percent = global_metrics["bit_exact_percent"]
    total_new_wins = global_metrics["total_new_wins"]
    total_significant_wins = global_metrics["total_significant_wins"]
    total_mos_count = global_metrics["total_mos_count"]
    total_mos_delta = global_metrics["total_mos_delta"]
    worst_tp_delta = global_metrics["worst_tp_delta"]

    # Rate-control modes present in this report. When both ABR and VBR ran
    # (the common case per docs/ci.md's matrix), the Summary and Scenario
    # Performance tables below are split per mode -- pooling ABR's bitrate
    # accuracy with VBR's allocation accuracy into one averaged number would
    # hide a regression confined to a single mode.
    modes_present = sorted({d.get("rate_control_mode", "abr")
                            for d in all_suite_data.values()}) or ["abr"]

    # Name the gates that failed rather than always saying "Quality": the
    # verdict now covers footprint and throughput too, and a footprint failure
    # reported as a quality failure sends the reader to the wrong table.
    failed_gates = sorted({g["name"] for d in all_suite_data.values()
                           for g in d.get("gates", []) if g["status"] == "fail"})

    summary_lines = []
    if overall_regression:
        what = ", ".join(g.capitalize() for g in failed_gates) or "Quality"
        summary_lines.append(f"## ❌ {what} Regression Detected")
    elif worst_tp_delta < -5.0:
        summary_lines.append("## ⚠️ Performance Regression Detected")
    elif overall_missing and ENABLED_GATES is None:
        summary_lines.append("## ❌ Incomplete/Missing Data Detected")
    elif overall_missing:
        # Narrowed run: the absent numbers are absent on purpose, and the job
        # exits 0, so the headline must not read like a failure.
        summary_lines.append(
            f"## ✅ Gates Passed ({', '.join(sorted(ENABLED_GATES))})")
    elif bit_exact_percent == 100.0:
        summary_lines.append("## ✅ Refactor Verified (Bit-Identical)")
    elif total_new_wins > 0 or total_significant_wins > 0 or (total_mos_count > 0 and (total_mos_delta / total_mos_count) > 0.01) or avg_tp_reduction > 5:
        summary_lines.append("## 🚀 Perceptual & Efficiency Improvement")
    else:
        summary_lines.append("## 📊 Benchmark Summary")

    mos_label = "MOS"

    summary_lines.append("\n### Summary")
    if len(modes_present) > 1:
        # Both ABR and VBR ran: one table per mode, so a maintainer can see
        # at a glance whether either mode specifically regressed instead of
        # reading one number that pools both together.
        for mode in modes_present:
            mode_items = sorted((n, d) for n, d in all_suite_data.items()
                                 if d.get("rate_control_mode", "abr") == mode)
            mode_metrics = aggregate_suite_metrics(mode_items)
            summary_lines.append(f"\n#### {mode.upper()}")
            summary_lines.extend(render_summary_table(mode_metrics, mos_label, mode.upper()))
    else:
        summary_lines.extend(render_summary_table(global_metrics, mos_label, modes_present[0].upper()))

    # Build the full report
    report = list(summary_lines)

    if not summary_only and (final_base_sha or final_cand_sha):
        report.insert(1, "\n### Environment")
        if final_base_sha:
            report.insert(2, f"- **Baseline SHA**: `{final_base_sha}`")
        if final_cand_sha:
            report.insert(3, f"- **Candidate SHA**: `{final_cand_sha}`")

    if not summary_only:
        # Scenario Performance Table. When both ABR and VBR ran, a "Mode"
        # column keys each scenario by mode too -- otherwise an ABR scenario
        # row would silently average in its VBR counterpart's numbers.
        multi_mode = len(modes_present) > 1
        report.append("\n### Scenario Performance")
        if multi_mode:
            report.append(f"| Scenario | Mode | {mos_label} Δ | 95% Conf. Interval | Wins / Losses | Stereo Fid. Δ | Transient | Throughput Δ | Bitrate Acc |")
            report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        else:
            report.append(f"| Scenario | {mos_label} Δ | 95% Conf. Interval | Wins / Losses | Stereo Fid. Δ | Transient | Throughput Δ | Bitrate Acc |")
            report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

        # Aggregating across all suites for scenarios, keyed by mode too when
        # more than one mode is present.
        global_scenario_stats = defaultdict(lambda: {"mos_delta": 0, "mos_count": 0, "ic_delta": 0, "ic_count": 0, "tp_cand": 0, "tp_base": 0, "acc_sum": 0, "acc_count": 0, "mos_deltas": [], "centroid_deltas": [], "centroid_o_abs": [], "centroid_b_abs": []})
        for suite_data in all_suite_data.values():
            mode = suite_data.get("rate_control_mode", "abr")
            for sc_name, sc_stats in suite_data["scenario_stats"].items():
                key = (sc_name, mode) if multi_mode else sc_name
                global_scenario_stats[key]["mos_delta"] += sc_stats["mos_delta_sum"]
                global_scenario_stats[key]["mos_count"] += sc_stats["mos_count"]
                global_scenario_stats[key]["ic_delta"] += sc_stats["ic_delta_sum"]
                global_scenario_stats[key]["ic_count"] += sc_stats["ic_count"]
                global_scenario_stats[key]["tp_cand"] += sc_stats["tp_sum_cand"]
                global_scenario_stats[key]["tp_base"] += sc_stats["tp_sum_base"]
                global_scenario_stats[key]["acc_sum"] += sc_stats["bitrate_acc_sum"]
                global_scenario_stats[key]["acc_count"] += sc_stats["bitrate_acc_count"]
                global_scenario_stats[key]["mos_deltas"] += sc_stats.get("mos_deltas", [])
                global_scenario_stats[key]["centroid_deltas"] += sc_stats.get("centroid_deltas", [])
                global_scenario_stats[key]["centroid_o_abs"] += sc_stats.get("centroid_o_abs", [])
                global_scenario_stats[key]["centroid_b_abs"] += sc_stats.get("centroid_b_abs", [])

        has_sig_mark = False
        sort_key = (lambda k: (get_scenario_sort_key(k[0]), k[1])) if multi_mode else get_scenario_sort_key
        for key in sorted(global_scenario_stats.keys(), key=sort_key):
            gs = global_scenario_stats[key]
            sc_name = key[0] if multi_mode else key
            sc_mos_delta = f"{(gs['mos_delta'] / gs['mos_count']):+.3f}" if gs['mos_count'] > 0 else "N/A"
            sc_ic_delta = f"{(gs['ic_delta'] / gs['ic_count']):+.4f}" if gs['ic_count'] > 0 else "N/A"
            sc_tp_delta = f"{(1 - gs['tp_cand'] / gs['tp_base']) * 100:+.1f}%" if gs['tp_base'] > 0 else "N/A"
            sc_acc = f"{(gs['acc_sum'] / gs['acc_count']):.1f}%" if gs['acc_count'] > 0 else "N/A"

            # Same three-state verdict discipline as the summary row, at
            # scenario granularity: never a bare number, and below
            # MIN_CENTROID_ONSETS the cell just says how far short it fell
            # rather than guessing.
            sc_centroid_deltas = gs.get("centroid_deltas", [])
            n_c_total = len(sc_centroid_deltas)
            if n_c_total >= MIN_CENTROID_ONSETS:
                lo_c, hi_c = transient.bootstrap_ci(sc_centroid_deltas)
                p_c, _neg_c, n_c = transient.sign_test_p(sc_centroid_deltas)
                v_c = transient.ci_signtest_verdict(
                    lo_c, hi_c, p_c, label_decrease="improved", label_increase="regression")
                if v_c in ("regression", "improved"):
                    icon = "📉" if v_c == "regression" else "📈"
                    sc_o_abs = gs.get("centroid_o_abs", [])
                    sc_b_abs = gs.get("centroid_b_abs", [])
                    if sc_o_abs and sc_b_abs:
                        fid_o = 1.0 / (1.0 + sum(sc_o_abs) / len(sc_o_abs))
                        fid_b = 1.0 / (1.0 + sum(sc_b_abs) / len(sc_b_abs))
                        sc_fid_delta = fid_o - fid_b
                        sc_transient = f"{sc_fid_delta:+.4f} {icon} (n={n_c})"
                    else:
                        sc_transient = f"{icon} (n={n_c})"
                else:
                    sc_transient = f"➖ (n={n_c})"
            else:
                sc_transient = f"➖ (n={n_c_total})"

            # Report-only: a scenario mean is small by nature, so say whether
            # it is a consistent shift or a couple of clips carrying the rest.
            ci = bootstrap_mean_ci(gs["mos_deltas"])
            if ci:
                if ci["lo"] <= 0 <= ci["hi"]:
                    sig = ""
                else:
                    sig = " ✳"
                    has_sig_mark = True
                sc_ci = f"[{ci['lo']:+.3f}, {ci['hi']:+.3f}]{sig}"
                sc_wl = f"{ci['wins']}/{ci['losses']}"
            else:
                sc_ci, sc_wl = "N/A", "N/A"

            if multi_mode:
                report.append(
                    f"| {sc_name} | {key[1].upper()} | {sc_mos_delta} | {sc_ci} | {sc_wl} | "
                    f"{sc_ic_delta} | {sc_transient} | {sc_tp_delta} | {sc_acc} |")
            else:
                report.append(
                    f"| {sc_name} | {sc_mos_delta} | {sc_ci} | {sc_wl} | "
                    f"{sc_ic_delta} | {sc_transient} | {sc_tp_delta} | {sc_acc} |")

        report.append("\n_Transient fidelity measures attack centroid shift (smearing/delay of transient attacks, in ms). 📈 = improved, 📉 = regression, ➖ = neutral/insufficient onsets (<30)._")
        if has_sig_mark:
            report.append("_✳ Statistically significant change (95% confidence interval excludes 0)_")

        # 1. Collapsible Details: Regressions
        total_regressions = global_metrics["total_regressions"]
        if total_regressions > 0:
            report.append(
                "\n<details><summary><b>❌ View Regression Details ({})</b></summary>\n".format(total_regressions))
            for name, data in sorted(all_suite_data.items()):
                if data["regressions"]:
                    report.append(f"\n#### {name}")
                    report.append(
                        f"| Test Case | Status | {mos_label} (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
                    report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                    for r in data["regressions"]:
                        report.append(r["line"])
            report.append("\n</details>")

        # 2. Collapsible Additional Details
        report.append(
            "\n<details><summary><b>View Additional Suite Details & Wins</b></summary>\n")

        for name, data in sorted(all_suite_data.items()):
            status_icon = "✅"
            if data["has_regression"]:
                status_icon = "❌"
            elif data["missing_data"]:
                status_icon = "❌"

            avg_mos_suite = f"{(data['mos_delta_sum'] /
                                data['mos_count']):+.3f}" if data["mos_count"] > 0 else "N/A"
            suite_bit_exact_percent = (
                data["bit_exact_count"] /
                data["total_cases"] *
                100) if data["total_cases"] > 0 else 0

            report.append(f"\n#### {status_icon} {name}")
            report.append(
                f"- MOS Δ: {avg_mos_suite}, TP Δ: {data['tp_reduction']:+.1f}%, Size Δ: {data['lib_size_chg']:+.2f}%")
            report.append(
                f"- Bitstream Consistency: {suite_bit_exact_percent:.1f}%")

            # Named gate decisions. A skipped gate is printed as loudly as a
            # failing one: silence about a gate is not evidence it passed.
            if data.get("gates"):
                icons = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}
                report.append("\n**Gates**")
                for g in data["gates"]:
                    report.append(
                        f"- {icons.get(g['status'], '?')} `{g['name']}`: {g['detail']}")

            if data.get("object_movers"):
                report.append("\n**Object .text movers**")
                report.append(", ".join(
                    f"`{obj}` {d:+d}" for d, obj in data["object_movers"]))

            if data["new_wins"]:
                report.append("\n**🆕 New Wins**")
                report.append(f"| Test Case | {mos_label} (Base) | Delta |")
                report.append("| :--- | :---: | :---: |")
                for w in data["new_wins"]:
                    report.append("| {} | {:.2f} ({:.2f}) | {:+.2f} |".format(
                        w["display_name"], w["mos"], w["b_mos"], w["delta"]))

            if data["significant_wins"]:
                report.append("\n**🌟 Significant Wins**")
                report.append(
                    f"| Test Case | Status | {mos_label} (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
                report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for w in data["significant_wins"]:
                    report.append(w["line"])

            if data["opportunities"]:
                report.append("\n**💡 Opportunities**")
                report.append(
                    f"| Test Case | Status | {mos_label} (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
                report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for o in data["opportunities"]:
                    report.append(o["line"])

            if data["all_cases"]:
                report.append(
                    f"\n<details><summary>View all {len(data['all_cases'])} cases for {name}</summary>\n")
                report.append(
                    f"| Test Case | Status | {mos_label} (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
                report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for c in data["all_cases"]:
                    report.append(c["line"])
                report.append("\n</details>")

        report.append("\n</details>")

    # Prepare outputs
    full_output = "\n".join(report) + "\n"

    # Add link to full report in summary if requested
    github_server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    github_repository = os.environ.get("GITHUB_REPOSITORY", "")
    github_run_id = os.environ.get("GITHUB_RUN_ID", "")

    if github_repository and github_run_id:
        full_report_url = f"{github_server_url}/{github_repository}/actions/runs/{github_run_id}"
        summary_lines.append(f"\n[View Full Report]({full_report_url})")

    summary_output = "\n".join(summary_lines) + "\n"

    # Write to stdout
    if summary_only:
        sys.stdout.write(summary_output)
    else:
        sys.stdout.write(full_output)

    # Write to files
    if args.output:
        try:
            with open(args.output, "w") as f:
                f.write(full_output)
        except Exception as e:
            sys.stderr.write(f"Error: Could not write report to {args.output}: {e}\n")

    if args.summary_output:
        try:
            with open(args.summary_output, "w") as f:
                f.write(summary_output)
        except Exception as e:
            sys.stderr.write(f"Error: Could not write summary to {args.summary_output}: {e}\n")

    # overall_missing means "a number we expected is absent". When --gates
    # narrows the run, absence of the unselected numbers is the point, not a
    # failure.
    if overall_regression or (overall_missing and ENABLED_GATES is None):
        sys.exit(1)


if __name__ == "__main__":
    main()
