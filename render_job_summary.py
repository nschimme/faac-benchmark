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


def format_bytes(b):
    if b is None or b < 0:
        return "N/A"
    if b >= 1024 * 1024:
        return f"{b} ({b / (1024 * 1024):.2f} MB)"
    elif b >= 1024:
        return f"{b} ({b / 1024:.2f} KB)"
    return f"{b} B"


def render_job_summary(data):
    lines = []
    run_name = data.get("name") or "Benchmark Run"
    sha = data.get("sha")
    faac_git_sha = data.get("faac_git_sha")

    title = f"## 📊 Benchmark Job Summary: {run_name}"
    lines.append(title)

    meta_bits = []
    if sha:
        meta_bits.append(f"**Commit**: `{sha[:8]}`")
    if faac_git_sha:
        meta_bits.append(f"**FAAC SHA**: `{faac_git_sha[:8]}`")
    if meta_bits:
        lines.append(" | ".join(meta_bits) + "\n")

    # 1. Code Footprint Section
    lib_size = data.get("lib_size")
    lib_sections = data.get("lib_sections", {})
    text_sz = data.get("lib_text_size", lib_sections.get(".text", 0))
    rodata_sz = data.get("lib_rodata_size", lib_sections.get(".rodata", 0))
    data_sz = data.get("lib_data_size", lib_sections.get(".data", 0))
    bss_sz = data.get("lib_bss_size", lib_sections.get(".bss", 0))
    frontend_sz = data.get("frontend_size", 0)

    lines.append("### 💾 Code Footprint")
    lines.append("| Component | Size | Breakdown |")
    lines.append("| :--- | :--- | :--- |")
    lines.append(f"| **ROM Footprint** (`.text` + `.rodata` + `.data`) | **{format_bytes(lib_size)}** | Text: {format_bytes(text_sz)}, Rodata: {format_bytes(rodata_sz)}, Data: {format_bytes(data_sz)} |")
    if bss_sz > 0:
        lines.append(f"| **RAM Footprint** (`.bss`) | {format_bytes(bss_sz)} | |")
    if frontend_sz > 0:
        lines.append(f"| **Frontend Binary** | {format_bytes(frontend_sz)} | |")
    lines.append("")

    # Matrix Analysis
    matrix = data.get("matrix", {})
    if not matrix:
        lines.append("*No encoding matrix executed in this run (Footprint/Throughput-only).*")
        return "\n".join(lines)

    # Group matrix entries by scenario
    scenario_samples = defaultdict(list)
    rc_modes = set()
    decode_errors = []

    for key, sample in matrix.items():
        scenario = sample.get("scenario") or key.split("_")[0]
        scenario_samples[scenario].append(sample)
        if sample.get("rate_control_mode"):
            rc_modes.add(sample.get("rate_control_mode"))
        if sample.get("decode_error"):
            decode_errors.append((sample.get("filename", key), sample.get("decode_error")))

    rc_mode_str = "/".join(sorted(rc_modes)).upper() if rc_modes else "ABR"

    # Aggregate scenario metrics
    scenario_rows = []
    total_clips = 0
    total_mos_sum = 0.0
    mos_clip_count = 0
    total_target_br = 0.0
    total_actual_br = 0.0
    br_count = 0

    sorted_scenarios = sorted(scenario_samples.keys(), key=get_scenario_sort_key)

    for sc_name in sorted_scenarios:
        samples = scenario_samples[sc_name]
        clip_cnt = len(samples)
        total_clips += clip_cnt

        mos_vals = [s["mos"] for s in samples if s.get("mos") is not None]
        avg_mos = (sum(mos_vals) / len(mos_vals)) if mos_vals else None

        if avg_mos is not None:
            total_mos_sum += sum(mos_vals)
            mos_clip_count += len(mos_vals)

        target_brs = [s["expected_bitrate"] for s in samples if s.get("expected_bitrate") is not None]
        actual_brs = [s["bitrate"] for s in samples if s.get("bitrate") is not None]

        mean_target_br = (sum(target_brs) / len(target_brs)) if target_brs else None
        mean_actual_br = (sum(actual_brs) / len(actual_brs)) if actual_brs else None

        if mean_target_br and mean_actual_br:
            total_target_br += sum(target_brs)
            total_actual_br += sum(actual_brs)
            br_count += len(actual_brs)

        # Sc_errors
        sc_errs = sum(1 for s in samples if s.get("decode_error"))
        status = "✅ OK" if sc_errs == 0 else f"❌ {sc_errs} decode err"

        scenario_rows.append({
            "name": sc_name,
            "clips": clip_cnt,
            "target_br": mean_target_br,
            "actual_br": mean_actual_br,
            "mos": avg_mos,
            "status": status
        })

    # Overall Metrics
    overall_mos = (total_mos_sum / mos_clip_count) if mos_clip_count > 0 else None
    overall_target_br = (total_target_br / br_count) if br_count > 0 else None
    overall_actual_br = (total_actual_br / br_count) if br_count > 0 else None

    lines.append(f"### 🎯 Performance & Bitrate Summary ({rc_mode_str} Mode)")

    summary_bullets = []
    if overall_mos is not None:
        summary_bullets.append(f"- **Overall Average MOS**: `{overall_mos:.3f} / 5.000`")
    if overall_actual_br is not None:
        if "ABR" in rc_mode_str and overall_target_br is not None:
            accuracy = max(0.0, 1.0 - abs((overall_actual_br - overall_target_br) / overall_target_br)) * 100
            bias = ((overall_actual_br - overall_target_br) / overall_target_br) * 100
            bias_str = f"+{bias:.2f}%" if bias >= 0 else f"{bias:.2f}%"
            summary_bullets.append(f"- **Mean Bitrate**: `{overall_actual_br:.2f} kbps` (Target: `{overall_target_br:.2f} kbps`, Accuracy: `{accuracy:.1f}%`, Bias: `{bias_str}`)")
        else:
            summary_bullets.append(f"- **Mean Bitrate**: `{overall_actual_br:.2f} kbps`")

    # Throughput
    tp_data = data.get("throughput", {})
    tp_metric = data.get("throughput_metric", "timing")
    if tp_data:
        if "overall" in tp_data:
            val = tp_data["overall"]
            if tp_metric == "cachegrind":
                summary_bullets.append(f"- **Throughput**: `{val:,.0f} I-refs / slice`")
            else:
                summary_bullets.append(f"- **Throughput**: `{val:.4f} sec / clip`")

    # Decode Errors Summary
    if decode_errors:
        summary_bullets.append(f"- **Decode Status**: ❌ `{len(decode_errors)} / {total_clips}` clips failed decode validation")
    else:
        summary_bullets.append(f"- **Decode Status**: ✅ Clean (`0` errors across {total_clips} clips)")

    lines.extend(summary_bullets)
    lines.append("")

    # Per-scenario Table
    lines.append("#### Per-Scenario Breakdown")
    if "ABR" in rc_mode_str:
        lines.append("| Scenario | Clips | Target Bitrate | Actual Bitrate | MOS | Status |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
        for r in scenario_rows:
            target_str = f"{r['target_br']:.1f} kbps" if r['target_br'] else "N/A"
            actual_str = f"{r['actual_br']:.1f} kbps" if r['actual_br'] else "N/A"
            mos_str = f"{r['mos']:.3f}" if r['mos'] is not None else "N/A"
            lines.append(f"| `{r['name']}` | {r['clips']} | {target_str} | {actual_str} | {mos_str} | {r['status']} |")
    else:
        lines.append("| Scenario | Clips | Actual Bitrate | MOS | Status |")
        lines.append("| :--- | :---: | :---: | :---: | :---: |")
        for r in scenario_rows:
            actual_str = f"{r['actual_br']:.1f} kbps" if r['actual_br'] else "N/A"
            mos_str = f"{r['mos']:.3f}" if r['mos'] is not None else "N/A"
            lines.append(f"| `{r['name']}` | {r['clips']} | {actual_str} | {mos_str} | {r['status']} |")

    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Render Job Summary to Markdown for GitHub Step Summary")
    parser.add_argument("result_json", help="Path to result JSON file")
    parser.add_argument("--output", help="Path to output Markdown file (default: stdout)")

    args = parser.parse_args()

    if not os.path.exists(args.result_json):
        sys.stderr.write(f"Error: Result file {args.result_json} not found.\n")
        sys.exit(1)

    with open(args.result_json, "r") as f:
        data = json.load(f)

    summary_md = render_job_summary(data)

    if args.output:
        with open(args.output, "w") as f:
            f.write(summary_md + "\n")
    else:
        print(summary_md)


if __name__ == "__main__":
    main()
