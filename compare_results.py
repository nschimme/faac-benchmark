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

    suite_results = {
        "has_regression": False,
        "missing_data": False,
        "mos_delta_sum": 0,
        "mos_count": 0,
        "missing_mos_count": 0,
        "tp_reduction": 0,
        "lib_size_chg": 0,
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
                "bitrate_acc_sum": 0,
                "bitrate_acc_count": 0,
                "count": 0}),
        "base_tp": base.get("throughput", {}),
        "cand_tp": cand.get("throughput", {}),
        "base_sha": base.get("sha"),
        "cand_sha": cand.get("sha")
    }

    base_m = base.get("matrix", {})
    cand_m = cand.get("matrix", {})

    if cand_m:
        suite_results["total_cases"] = len(cand_m)
        for k in sorted(cand_m.keys()):
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

            o_md5 = o.get("md5", "")
            b_md5 = b.get("md5", "")

            if o_md5 and b_md5 and o_md5 == b_md5:
                suite_results["bit_exact_count"] += 1

            size_chg = "N/A"
            if o_size is not None and b_size is not None:
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
                    suite_results["scenario_stats"][scenario]["mos_delta_sum"] += delta
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
            else:
                status = "❌"  # Missing MOS is a failure
                suite_results["missing_mos_count"] += 1
                suite_results["has_regression"] = True
                suite_results["missing_data"] = True
                delta = -10.0  # Force to top of regressions

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

    return suite_results


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

    args = parser.parse_args()

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
    total_mos_delta = 0
    total_mos_count = 0
    total_missing_mos = 0
    total_tp_reduction = 0
    total_lib_chg = 0
    total_bitrate_chg = 0
    total_bitrate_count = 0
    total_bitrate_acc_sum = 0
    total_bitrate_acc_count = 0
    total_bitrate_bias_sum = 0

    total_regressions = 0
    total_reg_critical = 0
    total_reg_significant = 0
    total_reg_minor = 0
    total_new_wins = 0
    total_significant_wins = 0
    total_bit_exact = 0
    total_cases_all = 0

    final_base_sha = base_sha
    final_cand_sha = cand_sha

    worst_mos_drop = (0, "N/A")
    worst_bitrate_err = (0, "N/A")

    # For worst-case scenario throughput
    scenario_tp_deltas = []

    for name, (base, cand) in sorted(suites.items()):
        data = analyze_pair(base, cand)
        if data:
            all_suite_data[name] = data
            if data["has_regression"]:
                overall_regression = True
            if data["missing_data"]:
                overall_missing = True
            total_mos_delta += data["mos_delta_sum"]
            total_mos_count += data["mos_count"]
            total_missing_mos += data["missing_mos_count"]
            total_tp_reduction += data["tp_reduction"]
            total_lib_chg += data["lib_size_chg"]
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

            if not final_base_sha and data["base_sha"]:
                final_base_sha = data["base_sha"]
            if not final_cand_sha and data["cand_sha"]:
                final_cand_sha = data["cand_sha"]

            if data["worst_mos_drop"][0] < worst_mos_drop[0]:
                worst_mos_drop = data["worst_mos_drop"]
            if abs(data["worst_bitrate_err"][0]) > abs(worst_bitrate_err[0]):
                worst_bitrate_err = data["worst_bitrate_err"]

            for sc_name, sc_data in data["scenario_stats"].items():
                if sc_data["tp_sum_base"] > 0:
                    delta = (1 - sc_data["tp_sum_cand"] /
                             sc_data["tp_sum_base"]) * 100
                    scenario_tp_deltas.append((f"{name} / {sc_name}", delta))

    avg_mos_delta_str = f"{(total_mos_delta /
                            total_mos_count):+.3f}" if total_mos_count > 0 else "N/A"
    avg_tp_reduction = total_tp_reduction / \
        len(all_suite_data) if all_suite_data else 0
    avg_lib_chg = total_lib_chg / len(all_suite_data) if all_suite_data else 0
    avg_bitrate_chg = total_bitrate_chg / \
        total_bitrate_count if total_bitrate_count > 0 else 0
    avg_bitrate_acc = total_bitrate_acc_sum / \
        total_bitrate_acc_count if total_bitrate_acc_count > 0 else 0
    avg_bitrate_bias = total_bitrate_bias_sum / \
        total_bitrate_acc_count if total_bitrate_acc_count > 0 else 0

    bit_exact_percent = (
        total_bit_exact /
        total_cases_all *
        100) if total_cases_all > 0 else 0

    # Worst-case throughput
    worst_tp_scen, worst_tp_delta = (None, 0)
    if scenario_tp_deltas:
        worst_tp_scen, worst_tp_delta = min(
            scenario_tp_deltas, key=lambda x: x[1])

    summary_lines = []
    if overall_regression:
        summary_lines.append("## ❌ Quality Regression Detected")
    elif worst_tp_delta < -5.0:
        summary_lines.append("## ⚠️ Performance Regression Detected")
    elif overall_missing:
        summary_lines.append("## ❌ Incomplete/Missing Data Detected")
    elif bit_exact_percent == 100.0:
        summary_lines.append("## ✅ Refactor Verified (Bit-Identical)")
    elif total_new_wins > 0 or total_significant_wins > 0 or (total_mos_count > 0 and (total_mos_delta / total_mos_count) > 0.01) or avg_tp_reduction > 5:
        summary_lines.append("## 🚀 Perceptual & Efficiency Improvement")
    else:
        summary_lines.append("## 📊 Benchmark Summary")

    summary_lines.append("\n### Summary")
    summary_lines.append("| Metric | Value |")
    summary_lines.append("| :--- | :--- |")

    # Regressions (Always shown)
    if total_regressions == 0:
        summary_lines.append(f"| **Regressions** | 0 ✅ |")
    else:
        reg_parts = []
        if total_reg_critical: reg_parts.append(f"{total_reg_critical} 💀")
        if total_reg_significant: reg_parts.append(f"{total_reg_significant} ❌")
        if total_reg_minor: reg_parts.append(f"{total_reg_minor} ⚠️")
        summary_lines.append(f"| **Regressions** | {', '.join(reg_parts)} |")

    # Worst-Case Outliers
    if worst_mos_drop[0] < -0.01:
        summary_lines.append(f"| **Worst MOS Drop** | {worst_mos_drop[0]:.2f} ({worst_mos_drop[1]}) |")

    if abs(worst_bitrate_err[0]) > 1.0:
        err_icon = "📈" if worst_bitrate_err[0] > 0 else "📉"
        summary_lines.append(f"| **Max Bitrate Err** | {worst_bitrate_err[0]:+.1f}% ({worst_bitrate_err[1]}) {err_icon} |")

    # New Wins (Only if baseline < threshold and candidate >= threshold)
    if total_new_wins > 0:
        summary_lines.append(f"| **New Wins** | {total_new_wins} 🆕 |")

    # Significant Wins (MOS delta > 0.1)
    if total_significant_wins > 0:
        summary_lines.append(f"| **Significant Wins** | {total_significant_wins} 🌟 |")

    # Bitstream Consistency (Against baseline)
    consist_status = f"{bit_exact_percent:.1f}%"
    if bit_exact_percent == 100.0:
        consist_status += " (MD5 Match)"
    summary_lines.append(f"| **Consistency** | {consist_status} |")

    # Throughput
    if abs(avg_tp_reduction) > 0.1:
        tp_icon = "🚀" if avg_tp_reduction > 1.0 else "📉" if avg_tp_reduction < -1.0 else ""
        summary_lines.append(
            f"| **Throughput (Avg)** | {avg_tp_reduction:+.1f}% {tp_icon} |")

    # Per-signal throughput deltas if available
    tp_details = []
    if all_suite_data:
        first_data = list(all_suite_data.values())[0]
        base_tp = first_data.get("base_tp", {})
        cand_tp = first_data.get("cand_tp", {})
        for signal in sorted(cand_tp.keys()):
            if signal == "overall":
                continue
            if signal in base_tp and base_tp[signal] > 0:
                delta = (1 - cand_tp[signal] / base_tp[signal]) * 100
                icon = "🚀" if delta > 1.0 else "📉" if delta < -1.0 else ""
                tp_details.append(
                    f"{signal.split('.')[0]}: {delta:+.1f}% {icon}")

    if tp_details:
        summary_lines.append(f"| **TP Breakdown** | {', '.join(tp_details)} |")

    if worst_tp_delta < -1.0:
        summary_lines.append(
            f"| **Worst-case TP Δ** | {worst_tp_delta:.1f}% ({worst_tp_scen}) ⚠️ |")

    # Binary Size
    if abs(avg_lib_chg) > 0.01:
        size_icon = "📉" if avg_lib_chg < -0.1 else "📈" if avg_lib_chg > 0.1 else ""
        summary_lines.append(
            f"| **Library Size** | {avg_lib_chg:+.2f}% {size_icon} |")


    # Bitrate Δ
    if abs(avg_bitrate_chg) > 0.1:
        bitrate_icon = "📉" if avg_bitrate_chg < - \
            1.0 else "📈" if avg_bitrate_chg > 1.0 else ""
        summary_lines.append(
            f"| **Bitrate Δ** | {avg_bitrate_chg:+.2f}% {bitrate_icon} |")

    # Bitrate Accuracy & Bias
    if total_bitrate_acc_count > 0:
        acc_icon = "🎯" if avg_bitrate_acc > 95 else "⚠️" if avg_bitrate_acc < 80 else ""
        summary_lines.append(
            f"| **Bitrate Accuracy** | {avg_bitrate_acc:.1f}% {acc_icon} |")

        bias_icon = "📈" if avg_bitrate_bias > 2.0 else "📉" if avg_bitrate_bias < -2.0 else "🎯"
        bias_desc = "(Overshooting)" if avg_bitrate_bias > 0.5 else "(Undershooting)" if avg_bitrate_bias < -0.5 else "(Balanced)"
        summary_lines.append(
            f"| **Bitrate Bias** | {avg_bitrate_bias:+.1f}% {bias_desc} {bias_icon} |")

    # Avg MOS Delta
    if total_mos_count > 0 and abs(total_mos_delta / total_mos_count) > 0.001:
        summary_lines.append(f"| **Avg MOS Delta** | {avg_mos_delta_str} |")

    if total_missing_mos > 0:
        summary_lines.append(
            f"\n⚠️ **Warning**: {total_missing_mos} MOS scores were missing/failed (treated as ❌).")

    # Build the full report
    report = list(summary_lines)

    if not summary_only and (final_base_sha or final_cand_sha):
        report.insert(1, "\n### Environment")
        if final_base_sha:
            report.insert(2, f"- **Baseline SHA**: `{final_base_sha}`")
        if final_cand_sha:
            report.insert(3, f"- **Candidate SHA**: `{final_cand_sha}`")

    if not summary_only:
        # Scenario Performance Table
        report.append("\n### Scenario Performance")
        report.append("| Scenario | Avg MOS Δ | Throughput Δ | Bitrate Acc |")
        report.append("| :--- | :---: | :---: | :---: |")

        # Aggregating across all suites for scenarios
        global_scenario_stats = defaultdict(lambda: {"mos_delta": 0, "mos_count": 0, "tp_cand": 0, "tp_base": 0, "acc_sum": 0, "acc_count": 0})
        for suite_data in all_suite_data.values():
            for sc_name, sc_stats in suite_data["scenario_stats"].items():
                global_scenario_stats[sc_name]["mos_delta"] += sc_stats["mos_delta_sum"]
                global_scenario_stats[sc_name]["mos_count"] += sc_stats["mos_count"]
                global_scenario_stats[sc_name]["tp_cand"] += sc_stats["tp_sum_cand"]
                global_scenario_stats[sc_name]["tp_base"] += sc_stats["tp_sum_base"]
                global_scenario_stats[sc_name]["acc_sum"] += sc_stats["bitrate_acc_sum"]
                global_scenario_stats[sc_name]["acc_count"] += sc_stats["bitrate_acc_count"]

        for sc_name in sorted(global_scenario_stats.keys()):
            gs = global_scenario_stats[sc_name]
            sc_mos_delta = f"{(gs['mos_delta'] / gs['mos_count']):+.3f}" if gs['mos_count'] > 0 else "N/A"
            sc_tp_delta = f"{(1 - gs['tp_cand'] / gs['tp_base']) * 100:+.1f}%" if gs['tp_base'] > 0 else "N/A"
            sc_acc = f"{(gs['acc_sum'] / gs['acc_count']):.1f}%" if gs['acc_count'] > 0 else "N/A"
            report.append(f"| {sc_name} | {sc_mos_delta} | {sc_tp_delta} | {sc_acc} |")

        # 1. Collapsible Details: Regressions
        if total_regressions > 0:
            report.append(
                "\n<details><summary><b>❌ View Regression Details ({})</b></summary>\n".format(total_regressions))
            for name, data in sorted(all_suite_data.items()):
                if data["regressions"]:
                    report.append(f"\n#### {name}")
                    report.append(
                        "| Test Case | Status | MOS (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
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

            if data["new_wins"]:
                report.append("\n**🆕 New Wins**")
                report.append("| Test Case | MOS (Base) | Delta |")
                report.append("| :--- | :---: | :---: |")
                for w in data["new_wins"]:
                    report.append("| {} | {:.2f} ({:.2f}) | {:+.2f} |".format(
                        w["display_name"], w["mos"], w["b_mos"], w["delta"]))

            if data["significant_wins"]:
                report.append("\n**🌟 Significant Wins**")
                report.append(
                    "| Test Case | Status | MOS (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
                report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for w in data["significant_wins"]:
                    report.append(w["line"])

            if data["opportunities"]:
                report.append("\n**💡 Opportunities**")
                report.append(
                    "| Test Case | Status | MOS (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
                report.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for o in data["opportunities"]:
                    report.append(o["line"])

            if data["all_cases"]:
                report.append(
                    f"\n<details><summary>View all {len(data['all_cases'])} cases for {name}</summary>\n")
                report.append(
                    "| Test Case | Status | MOS (Base) | Delta | Target | Actual | Acc % | Speed Δ | Bit-Exact |")
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

    if overall_regression or overall_missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
