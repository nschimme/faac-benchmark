"""
 * FAAC Benchmark Suite - Leaderboard & Report Generation
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import re
import statistics
from collections import defaultdict

from utils import (format_size, make_progress_bar, zoomed_y_range,
                   get_scenario_sort_key, scenario_channels, scenario_rate,
                   scenario_family, family_label, scenario_families)
from codec_bench.encoders import PROFILE_LABELS, profile_label, encoder_row_key, FAACEncoder
from codec_bench.decoders import decoder_row_key, CONFORMANCE_SNR_FLOOR_DB

CLIP_PEER_BUG_GAP = 0.75

def cell_peer_gap(clip_mos, rk, s_name, filename):
    if not filename:
        return None
    clip_scores = clip_mos.get((s_name, filename), {})
    this_mos = clip_scores.get(rk)
    peers = {other_rk: m for other_rk, m in clip_scores.items() if other_rk != rk}
    if this_mos is None or not peers:
        return None
    peer_avg = sum(peers.values()) / len(peers)
    if peer_avg - this_mos > CLIP_PEER_BUG_GAP:
        return (this_mos, peer_avg)
    return None


def generate_leaderboard(encoders, results, output_path, scenario_list, skip_graphs=False, has_decoders=False):
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0, "mos_min_file": None,
        "ic_sum": 0, "ic_count": 0,
        "centroid_sum": 0, "centroid_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "br_err_sum": 0, "br_err_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    error_counts = defaultdict(int)
    clip_mos = defaultdict(dict)
    bug_flags = []

    for res in results:
        e = res["row_key"]
        s = res["scenario"]

        if not res.get("decode_valid"):
            err_msg = res.get("decode_error") or "Unknown error"
            short_err = err_msg.split("\n")[0].split(":")[0].strip()
            error_counts[(e, short_err)] += 1

        if res.get("mos") is not None:
            stats[e][s]["mos_sum"] += res["mos"]
            stats[e][s]["mos_count"] += 1
            if res["mos"] < stats[e][s]["mos_min"]:
                stats[e][s]["mos_min"] = res["mos"]
                stats[e][s]["mos_min_file"] = res["filename"]

            if res.get("filename"):
                clip_mos[(s, res["filename"])][e] = res["mos"]

        if res.get("ic_err") is not None:
            stats[e][s]["ic_sum"] += res["ic_err"]
            stats[e][s]["ic_count"] += 1

        if res.get("attack_centroid_ms"):
            for d in res["attack_centroid_ms"]:
                stats[e][s]["centroid_sum"] += abs(d)
                stats[e][s]["centroid_count"] += 1

        if res["duration"] > 0 and res["audio_duration"]:
            speed = res["audio_duration"] / res["duration"]
            stats[e][s]["speed_sum"] += speed
            stats[e][s]["speed_count"] += 1

        if res["actual_bitrate"] and res["target_bitrate"]:
            err = abs(res["actual_bitrate"] - res["target_bitrate"]) / res["target_bitrate"] * 100
            stats[e][s]["br_err_sum"] += err
            stats[e][s]["br_err_count"] += 1

        if res.get("peak_ram_kb") is not None and res["peak_ram_kb"] > 0:
            stats[e][s]["ram_sum"] = stats[e][s].get("ram_sum", 0) + res["peak_ram_kb"]
            stats[e][s]["ram_count"] = stats[e][s].get("ram_count", 0) + 1

        stats[e][s]["total_count"] += 1
        if res.get("decode_valid"):
            stats[e][s]["valid_count"] += 1

    encoder_info = {encoder_row_key(e): e for e in encoders}
    all_row_keys = sorted(stats.keys())

    for rk in all_row_keys:
        if rk not in encoder_info:
            tool_name = next((r.get("tool") for r in results if r.get("row_key") == rk), rk)
            profile = next((r.get("profile") for r in results if r.get("row_key") == rk), "lc")
            encoder_info[rk] = type("DummyEncoder", (), {
                "name": tool_name,
                "profile": profile,
                "tool_id": rk,
                "text_size": 0,
                "rodata_size": 0
            })()

    tools = sorted(list({encoder_info[rk].name for rk in all_row_keys if rk in encoder_info}))

    def is_suboptimal(tool_name, s_name, profile, check_mos):
        tool_row_keys = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].name == tool_name]
        for other_rk in tool_row_keys:
            if encoder_info[other_rk].profile != profile:
                other_mos = stats[other_rk][s_name]["mos_sum"] / stats[other_rk][s_name]["mos_count"] if stats[other_rk][s_name]["mos_count"] > 0 else 0
                if other_mos > check_mos + 0.01:
                    return True
        return False

    def scenario_best_row_key(candidates, s_name):
        valid_candidates = [rk for rk in candidates if stats[rk][s_name]["valid_count"] > 0]
        if not valid_candidates:
            return None

        def key_fn(rk):
            st = stats[rk][s_name]
            avg_mos = st["mos_sum"] / st["mos_count"] if st["mos_count"] > 0 else 0
            worst_mos = st["mos_min"] if st["mos_count"] > 0 else 0
            p_order = {"lc": 3, "he": 2, "hev2": 1, "standard": 0}.get(encoder_info[rk].profile, 0)
            return (avg_mos, worst_mos, p_order)

        return max(valid_candidates, key=key_fn)

    tool_overall = {}
    for tool_name in tools:
        candidates = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].name == tool_name]

        e_mos, e_speed, e_br_err, e_ic, e_centroid, e_ram = [], [], [], [], [], []
        e_mos_min = 6.0
        scenario_count = 0
        has_data = False

        tool_valid = sum(sum(stats[rk][s]["valid_count"] for s in scenario_list) for rk in candidates)
        tool_total = sum(sum(stats[rk][s]["total_count"] for s in scenario_list) for rk in candidates)

        for s_name in scenario_list:
            rk = scenario_best_row_key(candidates, s_name)
            if rk is None:
                continue
            s_stats = stats[rk][s_name]
            s_has_data = False
            if s_stats["mos_count"] > 0:
                e_mos.append(s_stats["mos_sum"] / s_stats["mos_count"])
                e_mos_min = min(e_mos_min, s_stats["mos_min"])
                s_has_data = True
            if s_stats["speed_count"] > 0:
                e_speed.append(s_stats["speed_sum"] / s_stats["speed_count"])
                s_has_data = True
            if s_stats["br_err_count"] > 0:
                e_br_err.append(s_stats["br_err_sum"] / s_stats["br_err_count"])
                s_has_data = True
            if s_stats["ic_count"] > 0:
                e_ic.append(s_stats["ic_sum"] / s_stats["ic_count"])
                s_has_data = True
            if s_stats["centroid_count"] > 0:
                e_centroid.append(s_stats["centroid_sum"] / s_stats["centroid_count"])
                s_has_data = True
            if s_stats.get("ram_count", 0) > 0:
                e_ram.append(s_stats["ram_sum"] / s_stats["ram_count"])
                s_has_data = True
            if s_has_data:
                has_data = True
                scenario_count += 1

        if not has_data:
            continue

        enc_obj = next((e for e in encoders if e.name == tool_name), None)
        tool_overall[tool_name] = {
            "tool": tool_name,
            "overall_mos": sum(e_mos) / len(e_mos) if e_mos else 0,
            "worst_mos": e_mos_min if e_mos else 0,
            "avg_ic": sum(e_ic) / len(e_ic) if e_ic else 0,
            "avg_centroid_ms": sum(e_centroid) / len(e_centroid) if e_centroid else 0,
            "avg_speed": sum(e_speed) / len(e_speed) if e_speed else 0,
            "avg_br_err": sum(e_br_err) / len(e_br_err) if e_br_err else 0,
            "avg_ram_kb": sum(e_ram) / len(e_ram) if e_ram else 0,
            "text_size": enc_obj.text_size if enc_obj else 0,
            "rodata_size": enc_obj.rodata_size if enc_obj else 0,
            "valid_rate": (tool_valid / tool_total * 100) if tool_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    has_mos = any(o["overall_mos"] > 0 for o in tool_overall.values())
    sorted_tools = sorted(tool_overall.keys(), key=lambda x: (tool_overall[x]["worst_mos"], tool_overall[x]["overall_mos"]), reverse=True) if has_mos else sorted(tool_overall.keys())

    has_non_aac = any(e.profile == "standard" for e in encoders)
    title_str = "# Audio Encoder Leaderboard\n\n" if has_non_aac else "# AAC Encoder Leaderboard\n\n"

    with open(output_path, "w") as f:
        if has_decoders:
            f.write("# 🎛️ Audio Codec Leaderboard\n\n")
            nav_links = [
                "[🎙️ Encoder Rankings](#encoder-leaderboard)",
                "[🔊 Decoder Rankings](#decoder-leaderboard)",
                "[📋 Encoder Breakdowns](#per-scenario-encoder-breakdowns)",
                "[📋 Decoder Breakdowns](#per-scenario-decoder-breakdowns)"
            ]
            f.write(" | ".join(nav_links) + "\n\n---\n\n")
            f.write('<a name="encoder-leaderboard"></a>\n')
            f.write("## 🎙️ Encoder Leaderboard\n\n")
        else:
            f.write(title_str)

        f.write("Quality scores are objective proxy estimates (Zimtohrli/ViSQOL), not blind ABX listening test results.\n\n")
        f.write("### Overall Encoder Rankings\n\n")
        f.write("> **Note**: Overall MOS averages the scenario set listed below, so absolute "
                "values are only comparable between leaderboards built from the same set of "
                "scenarios. Relative ranking is unaffected.\n\n")
        f.write("| Rank | Encoder | Status | Worst MOS | Overall MOS | Scenarios | Stereo Fidelity | Transient Fidelity | Speed (xRT) | Bitrate Error | Peak RAM | ROM (Flash) |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_mos = max(o['overall_mos'] for o in tool_overall.values()) if tool_overall else 0
        best_worst_mos = max(o['worst_mos'] for o in tool_overall.values()) if tool_overall else 0
        best_speed = max(o['avg_speed'] for o in tool_overall.values()) if tool_overall else 0
        has_ic = any(o['avg_ic'] > 0 for o in tool_overall.values())
        best_ic = max(1.0 - o['avg_ic'] for o in tool_overall.values() if o['avg_ic'] > 0) if has_ic else None
        has_centroid = any(o['avg_centroid_ms'] > 0 for o in tool_overall.values())
        best_centroid_fid = max(1.0 / (1.0 + o['avg_centroid_ms']) for o in tool_overall.values() if o['avg_centroid_ms'] > 0) if has_centroid else None
        valid_br = [o['avg_br_err'] for o in tool_overall.values()]
        best_br = min(valid_br) if valid_br else 0

        for i, tool_name in enumerate(sorted_tools):
            o = tool_overall[tool_name]
            rank_str = f"🏆 {i+1}" if i == 0 and o['worst_mos'] > 0 else f"{i+1}"

            if o['valid_rate'] == 100:
                status_str = "OK"
            else:
                err_summaries = []
                for (rk_err, err_msg), count in error_counts.items():
                    if rk_err in encoder_info and encoder_info[rk_err].name == tool_name:
                        err_summaries.append(f"{err_msg} ({count}x)")
                err_text = ", ".join(err_summaries) if err_summaries else "Errors"
                status_str = f"⚠️ {err_text}"

            w_str = f"**{o['worst_mos']:.3f}**" if abs(o['worst_mos'] - best_worst_mos) < 1e-6 and o['worst_mos'] > 0 else f"{o['worst_mos']:.3f}"
            m_str = f"**{o['overall_mos']:.3f}**" if abs(o['overall_mos'] - best_mos) < 1e-6 and o['overall_mos'] > 0 else f"{o['overall_mos']:.3f}"
            sc_str = f"{o['scenario_count']}/{o['scenario_total']}"

            ic_fid = 1.0 - o['avg_ic']
            ic_str = f"**{ic_fid:.4f}**" if best_ic and abs(ic_fid - best_ic) < 1e-6 and o['avg_ic'] > 0 else (f"{ic_fid:.4f}" if o['avg_ic'] > 0 else "N/A")

            cent_fid = 1.0 / (1.0 + o['avg_centroid_ms'])
            centroid_str = f"**{cent_fid:.4f}**" if best_centroid_fid and abs(cent_fid - best_centroid_fid) < 1e-6 and o['avg_centroid_ms'] > 0 else (f"{cent_fid:.4f}" if o['avg_centroid_ms'] > 0 else "N/A")

            s_str = f"**{o['avg_speed']:.1f}x**" if abs(o['avg_speed'] - best_speed) < 1e-6 and o['avg_speed'] > 0 else f"{o['avg_speed']:.1f}x"
            br_str = f"**{o['avg_br_err']:.1f}%**" if abs(o['avg_br_err'] - best_br) < 1e-6 and o['avg_br_err'] >= 0 else f"{o['avg_br_err']:.1f}%"
            ram_str = format_size(int(o['avg_ram_kb'] * 1024)) if o['avg_ram_kb'] > 0 else "N/A"
            rom_str = format_size(o['text_size'] + o['rodata_size'])

            f.write(f"| {rank_str} | {o['tool']} | {status_str} | {w_str} | {m_str} | {sc_str} | {ic_str} | {centroid_str} | {s_str} | {br_str} | {ram_str} | {rom_str} |\n")

        f.write("\n<a name=\"per-scenario-encoder-breakdowns\"></a>\n")
        f.write("<details><summary><b>📊 View Per-Scenario Breakdowns & Visualizations</b></summary>\n\n")
        f.write("## Per-Scenario Breakdown & Visualizations\n\n")

        present_families = scenario_families(scenario_list)
        for fam in present_families:
            fam_label = family_label(fam)
            fam_scenarios = [s for s in scenario_list if scenario_family(s) == fam]
            if not fam_scenarios:
                continue

            f.write(f"### {fam_label} Quality Across Bitrates\n\n")
            x_labels = [s.rsplit("_", 1)[-1] for s in fam_scenarios]

            tool_line_data = {}
            for tool_name in sorted_tools:
                candidates = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].name == tool_name]
                vals = []
                for s in fam_scenarios:
                    best_rk = scenario_best_row_key(candidates, s)
                    if best_rk and stats[best_rk][s]["mos_count"] > 0:
                        vals.append(stats[best_rk][s]["mos_sum"] / stats[best_rk][s]["mos_count"])
                    else:
                        vals.append(None)
                if any(v is not None for v in vals):
                    tool_line_data[tool_name] = vals

            if not skip_graphs and tool_line_data:
                chart_vals = [v for vals in tool_line_data.values() for v in vals if v is not None]
                axis_lo, axis_hi = zoomed_y_range(chart_vals, "1.0 --> 5.0")
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "{fam_label} Quality across Bitrates (Average MOS)"\n')
                f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                f.write(f'    y-axis "MOS Score" {axis_lo:.4g} --> {axis_hi:.4g}\n')
                for tool_name, vals in tool_line_data.items():
                    last_v = next((v for v in vals if v is not None), 0.0)
                    clean_v = []
                    for v in vals:
                        if v is not None:
                            last_v = v
                        clean_v.append(f"{last_v:.4f}")
                    f.write(f'    line "{tool_name}" [{", ".join(clean_v)}]\n')
                f.write("```\n\n")

            f.write(f"<details><summary><b>View Detailed {fam_label} Average & Worst MOS Tables</b></summary>\n\n")
            f.write(f"#### Per-Scenario Average MOS ({fam_label})\n\n")

            table_used_strikethrough = False
            for p in ["lc", "he", "hev2", "standard"]:
                p_rks = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].profile == p]
                if not p_rks:
                    continue
                p_has_data = any(stats[rk][s_name]["mos_count"] > 0 for rk in p_rks for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                f.write(f"#### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(encoder_info[rk].name for rk in p_rks) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(p_rks)) + " |\n")

                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    p_valid_mos = [stats[rk][s_name]["mos_sum"] / stats[rk][s_name]["mos_count"] for rk in p_rks if stats[rk][s_name]["mos_count"] > 0]
                    best_p_mos = max(p_valid_mos) if p_valid_mos else None

                    for rk in p_rks:
                        st = stats[rk][s_name]
                        if st["mos_count"] > 0:
                            avg_m = st["mos_sum"] / st["mos_count"]
                            subopt = is_suboptimal(encoder_info[rk].name, s_name, p, avg_m)
                            if subopt:
                                table_used_strikethrough = True
                            is_best = best_p_mos and abs(avg_m - best_p_mos) < 1e-6
                            p_bar = make_progress_bar(avg_m, 5.0)

                            if is_best and subopt:
                                cell = f" _**{avg_m:.3f}**_{p_bar} *"
                            elif is_best:
                                cell = f" **{avg_m:.3f}**{p_bar}"
                            elif subopt:
                                cell = f" _{avg_m:.3f}_{p_bar} *"
                            else:
                                cell = f" {avg_m:.3f}{p_bar}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")

            f.write(f"#### Per-Scenario Worst MOS (Min Clip MOS - {fam_label})\n\n")
            f.write("> **Note**: Minimum perceptual MOS score observed across any clip in the scenario. "
                    "Highlights edge-case clip degradation. "
                    "A 🐛 names the clip when every other encoder scored ≥0.75 MOS higher on that exact clip "
                    "-- likely a defect specific to this encoder; see Quality Outliers under Issues Worth Investigating below.\n\n")

            for p in ["lc", "he", "hev2", "standard"]:
                p_rks = [rk for rk in all_row_keys if encoder_info[rk].profile == p]
                if not p_rks:
                    continue
                p_has_data = any(stats[rk][s_name]["mos_count"] > 0 for rk in p_rks for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                f.write(f"#### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(encoder_info[rk].name for rk in p_rks) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(p_rks)) + " |\n")

                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    p_valid_min = [stats[rk][s_name]["mos_min"] for rk in p_rks if stats[rk][s_name]["mos_count"] > 0]
                    best_p_min = max(p_valid_min) if p_valid_min else None

                    for rk in p_rks:
                        st = stats[rk][s_name]
                        if st["mos_count"] > 0:
                            min_m = st["mos_min"]
                            min_f = st["mos_min_file"]
                            gap = cell_peer_gap(clip_mos, rk, s_name, min_f)
                            if gap is not None:
                                bug_flags.append((encoder_info[rk].name, p, s_name, min_f, gap[0], gap[1]))

                            is_best = best_p_min and abs(min_m - best_p_min) < 1e-6
                            bug_mark = " 🐛" if gap is not None else ""
                            p_bar = make_progress_bar(min_m, 5.0)

                            if is_best:
                                cell = f" **{min_m:.3f}**{p_bar}{bug_mark}"
                            else:
                                cell = f" {min_m:.3f}{p_bar}{bug_mark}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")

            if table_used_strikethrough:
                f.write("_\\* Italicized scores indicate sub-optimal profile performance superseded by another profile from the same encoder at this bitrate._\n\n")

            f.write("</details>\n\n")

            # 4. Stereo Image Fidelity per rate family (only for multi-channel / stereo families)
            is_stereo_family = any(scenario_channels(s) >= 2 for s in fam_scenarios)
            if is_stereo_family:
                f.write(f"### Stereo Image Fidelity ({fam_label})\n\n")
                f.write("> **Note**: Measured as 1.0 - |Coherence(Ref) - Coherence(Deg)|. **Higher is truer** (closer to reference stereo image).\n\n")

                tool_line_data_ic = {}
                for tool_name in sorted_tools:
                    candidates = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].name == tool_name]
                    vals = []
                    for s in fam_scenarios:
                        best_rk = scenario_best_row_key(candidates, s)
                        if best_rk and stats[best_rk][s]["ic_count"] > 0:
                            vals.append(1.0 - (stats[best_rk][s]["ic_sum"] / stats[best_rk][s]["ic_count"]))
                        else:
                            vals.append(None)
                    if any(v is not None for v in vals):
                        tool_line_data_ic[tool_name] = vals

                if not skip_graphs and tool_line_data_ic:
                    chart_vals_ic = [v for vals in tool_line_data_ic.values() for v in vals if v is not None]
                    axis_lo_ic, axis_hi_ic = zoomed_y_range(chart_vals_ic, "0.0 --> 1.0")
                    f.write("```mermaid\n")
                    f.write("xychart-beta\n")
                    f.write(f'    title "Stereo Image Fidelity across Bitrates - {fam_label} (Higher is Better)"\n')
                    f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                    f.write(f'    y-axis "Stereo Fidelity" {axis_lo_ic:.4g} --> {axis_hi_ic:.4g}\n')
                    for tool_name, vals in tool_line_data_ic.items():
                        last_v = next((v for v in vals if v is not None), 0.0)
                        clean_v = []
                        for v in vals:
                            if v is not None:
                                last_v = v
                            clean_v.append(f"{last_v:.4f}")
                        f.write(f'    line "{tool_name}" [{", ".join(clean_v)}]\n')
                    f.write("```\n\n")

                f.write(f"<details><summary><b>View Detailed Stereo Fidelity Table ({fam_label})</b></summary>\n\n")
                for p in ["lc", "he", "hev2", "standard"]:
                    p_rks = [rk for rk in all_row_keys if encoder_info[rk].profile == p]
                    if not p_rks:
                        continue
                    p_has_data = any(stats[rk][s_name]["ic_count"] > 0 for rk in p_rks for s_name in fam_scenarios)
                    if not p_has_data:
                        continue

                    f.write(f"#### {profile_label(p)} Profile\n\n")
                    f.write("| Scenario | " + " | ".join(encoder_info[rk].name for rk in p_rks) + " |\n")
                    f.write("| :--- | " + " | ".join([":---:"] * len(p_rks)) + " |\n")

                    for s_name in fam_scenarios:
                        row_str = f"| {s_name} |"
                        p_valid_ic = [1.0 - (stats[rk][s_name]["ic_sum"] / stats[rk][s_name]["ic_count"]) for rk in p_rks if stats[rk][s_name]["ic_count"] > 0]
                        best_p_ic = max(p_valid_ic) if p_valid_ic else None

                        for rk in p_rks:
                            st = stats[rk][s_name]
                            if st["ic_count"] > 0:
                                ic_fid = 1.0 - (st["ic_sum"] / st["ic_count"])
                                is_best = best_p_ic and abs(ic_fid - best_p_ic) < 1e-6
                                p_bar = make_progress_bar(ic_fid, 1.0)
                                if is_best:
                                    cell = f" **{ic_fid:.4f}**{p_bar}"
                                else:
                                    cell = f" {ic_fid:.4f}{p_bar}"
                                row_str += f"{cell} |"
                            else:
                                row_str += " N/A |"
                        f.write(row_str + "\n")
                    f.write("\n")
                f.write("</details>\n\n")

            # 5. Transient Fidelity per rate family
            f.write(f"### Transient Fidelity ({fam_label})\n\n")
            f.write("> **Note**: Measured as 1 / (1 + mean |attack-centroid-shift| ms) across onsets. **Higher is truer** (attack timing closer to reference).\n\n")

            tool_line_data_cent = {}
            for tool_name in sorted_tools:
                candidates = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].name == tool_name]
                vals = []
                for s in fam_scenarios:
                    best_rk = scenario_best_row_key(candidates, s)
                    if best_rk and stats[best_rk][s]["centroid_count"] > 0:
                        vals.append(1.0 / (1.0 + (stats[best_rk][s]["centroid_sum"] / stats[best_rk][s]["centroid_count"])))
                    else:
                        vals.append(None)
                if any(v is not None for v in vals):
                    tool_line_data_cent[tool_name] = vals

            if not skip_graphs and tool_line_data_cent:
                chart_vals_cent = [v for vals in tool_line_data_cent.values() for v in vals if v is not None]
                axis_lo_c, axis_hi_c = zoomed_y_range(chart_vals_cent, "0.0 --> 1.0")
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "Transient Fidelity across Bitrates - {fam_label} (Higher is Better)"\n')
                f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                f.write(f'    y-axis "Transient Fidelity" {axis_lo_c:.4g} --> {axis_hi_c:.4g}\n')
                for tool_name, vals in tool_line_data_cent.items():
                    last_v = next((v for v in vals if v is not None), 0.0)
                    clean_v = []
                    for v in vals:
                        if v is not None:
                            last_v = v
                        clean_v.append(f"{last_v:.4f}")
                    f.write(f'    line "{tool_name}" [{", ".join(clean_v)}]\n')
                f.write("```\n\n")

            f.write(f"<details><summary><b>View Detailed Transient Fidelity Table ({fam_label})</b></summary>\n\n")
            for p in ["lc", "he", "hev2", "standard"]:
                p_rks = [rk for rk in all_row_keys if encoder_info[rk].profile == p]
                if not p_rks:
                    continue
                p_has_data = any(stats[rk][s_name]["centroid_count"] > 0 for rk in p_rks for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                f.write(f"#### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(encoder_info[rk].name for rk in p_rks) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(p_rks)) + " |\n")

                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    p_valid_c = [1.0 / (1.0 + (stats[rk][s_name]["centroid_sum"] / stats[rk][s_name]["centroid_count"])) for rk in p_rks if stats[rk][s_name]["centroid_count"] > 0]
                    best_p_c = max(p_valid_c) if p_valid_c else None

                    for rk in p_rks:
                        st = stats[rk][s_name]
                        if st["centroid_count"] > 0:
                            cent_fid = 1.0 / (1.0 + (st["centroid_sum"] / st["centroid_count"]))
                            is_best = best_p_c and abs(cent_fid - best_p_c) < 1e-6
                            p_bar = make_progress_bar(cent_fid, 1.0)
                            if is_best:
                                cell = f" **{cent_fid:.4f}**{p_bar}"
                            else:
                                cell = f" {cent_fid:.4f}{p_bar}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")
            f.write("</details>\n\n")

            # 6. Bitrate Accuracy per rate family
            f.write(f"### Bitrate Accuracy ({fam_label})\n\n")
            f.write("> **Note**: Deviation from target bitrate calculated from pure elementary stream audio bytes. **Lower is Better**.\n\n")

            tool_line_data_br = {}
            for tool_name in sorted_tools:
                candidates = [rk for rk in all_row_keys if rk in encoder_info and encoder_info[rk].name == tool_name]
                vals = []
                for s in fam_scenarios:
                    best_rk = scenario_best_row_key(candidates, s)
                    if best_rk and stats[best_rk][s]["br_err_count"] > 0:
                        vals.append(stats[best_rk][s]["br_err_sum"] / stats[best_rk][s]["br_err_count"])
                    else:
                        vals.append(None)
                if any(v is not None for v in vals):
                    tool_line_data_br[tool_name] = vals

            if not skip_graphs and tool_line_data_br:
                chart_vals_br = [v for vals in tool_line_data_br.values() for v in vals if v is not None]
                axis_lo_br, axis_hi_br = zoomed_y_range(chart_vals_br, "0.0 --> 20.0")
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "Bitrate Accuracy across Bitrates - {fam_label} (Lower is Better)"\n')
                f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                f.write(f'    y-axis "Bitrate Error (%)" {axis_lo_br:.4g} --> {axis_hi_br:.4g}\n')
                for tool_name, vals in tool_line_data_br.items():
                    last_v = next((v for v in vals if v is not None), 0.0)
                    clean_v = []
                    for v in vals:
                        if v is not None:
                            last_v = v
                        clean_v.append(f"{last_v:.4f}")
                    f.write(f'    line "{tool_name}" [{", ".join(clean_v)}]\n')
                f.write("```\n\n")

            f.write(f"<details><summary><b>View Detailed Bitrate Accuracy Table ({fam_label})</b></summary>\n\n")
            for p in ["lc", "he", "hev2", "standard"]:
                p_rks = [rk for rk in all_row_keys if encoder_info[rk].profile == p]
                if not p_rks:
                    continue
                p_has_data = any(stats[rk][s_name]["br_err_count"] > 0 for rk in p_rks for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                f.write(f"#### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(encoder_info[rk].name for rk in p_rks) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(p_rks)) + " |\n")

                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    p_valid_br = [stats[rk][s_name]["br_err_sum"] / stats[rk][s_name]["br_err_count"] for rk in p_rks if stats[rk][s_name]["br_err_count"] > 0]
                    best_p_br = min(p_valid_br) if p_valid_br else None

                    for rk in p_rks:
                        st = stats[rk][s_name]
                        if st["br_err_count"] > 0:
                            err_m = st["br_err_sum"] / st["br_err_count"]
                            is_best = best_p_br is not None and abs(err_m - best_p_br) < 1e-6
                            if is_best:
                                cell = f" **{err_m:.1f}%**"
                            else:
                                cell = f" {err_m:.1f}%"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")
            f.write("</details>\n\n")

        # 7. Clean vs VoIP-Degraded Speech (16 kHz family)
        clean_scenarios = [s for s in scenario_list if scenario_family(s) == "16k_mono"]
        voip_scenarios = [s for s in scenario_list if scenario_family(s) == "16k_mono_voip"]

        if clean_scenarios and voip_scenarios:
            f.write("### Speech Quality: Clean vs. VoIP-Degraded Input\n\n")
            f.write("> **Note**: Evaluates encoder robustness against pre-degraded telephony / VoIP input signals.\n\n")

            if not skip_graphs:
                clean_sc = clean_scenarios[0]
                voip_sc = voip_scenarios[0]
                tool_labels_fam = [f'"{encoder_info[rk].name}"' for rk in all_row_keys if stats[rk][clean_sc]["mos_count"] > 0 or stats[rk][voip_sc]["mos_count"] > 0]

                clean_raw = [stats[rk][clean_sc]["mos_sum"] / stats[rk][clean_sc]["mos_count"] if stats[rk][clean_sc]["mos_count"] > 0 else None for rk in all_row_keys if stats[rk][clean_sc]["mos_count"] > 0 or stats[rk][voip_sc]["mos_count"] > 0]
                voip_raw = [stats[rk][voip_sc]["mos_sum"] / stats[rk][voip_sc]["mos_count"] if stats[rk][voip_sc]["mos_count"] > 0 else None for rk in all_row_keys if stats[rk][clean_sc]["mos_count"] > 0 or stats[rk][voip_sc]["mos_count"] > 0]

                all_speech_v = [v for v in clean_raw + voip_raw if v is not None]
                axis_lo, axis_hi = zoomed_y_range(all_speech_v, "1.0 --> 5.0")

                clean_vals = [f"{v:.3f}" if v is not None else "0.0" for v in clean_raw]
                voip_vals = [f"{v:.3f}" if v is not None else "0.0" for v in voip_raw]
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write('    title "Speech Quality: Clean vs VoIP-Degraded Input"\n')
                f.write(f"    x-axis [{', '.join(tool_labels_fam)}]\n")
                f.write(f'    y-axis "MOS Score" {axis_lo:.4g} --> {axis_hi:.4g}\n')
                f.write(f'    bar "Clean" [{", ".join(clean_vals)}]\n')
                f.write(f'    bar "VoIP-Degraded" [{", ".join(voip_vals)}]\n')
                f.write("```\n\n")

        # BD-Rate Analysis vs Baseline Encoder (FAAC if present, else first encoder)
        try:
            import bd_rate as bdr

            def faac_version_key(rk):
                name = encoder_info[rk].name
                m = re.search(r"(\d+)\.(\d+)\.(\d+)", name)
                ver = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
                return (ver, 1 if "(" in name else 0)

            faac_lc_keys = [rk for rk in all_row_keys
                             if isinstance(encoder_info.get(rk), FAACEncoder) and encoder_info[rk].profile == "lc"]
            base_key = max(faac_lc_keys, key=faac_version_key) if faac_lc_keys else (all_row_keys[0] if all_row_keys else None)
            if base_key:
                base_tool_name = encoder_info[base_key].name if base_key in encoder_info else base_key
                f.write(f"### BD-Rate Relative Efficiency (vs {base_tool_name})\n\n")
                f.write("> **Note**: Bjontegaard-delta rate (BD-rate) measures the average percentage difference in bitrate for equal perceptual quality (MOS). "
                        "**Negative % = candidate is more efficient** (uses fewer bits for same quality). "
                        "BD-rate holds quality fixed by construction, avoiding bitrate-bias traps of raw fixed-rate MOS deltas.\n\n")

                base_recs = [r for r in results if r["row_key"] == base_key and r.get("decode_valid") and r.get("mos") is not None and r.get("actual_bitrate")]
                base_mat = {f"{r['scenario']}_{r['filename']}": {
                    "scenario": r["scenario"], "filename": r["filename"],
                    "mos": r["mos"], "bitrate": r["actual_bitrate"],
                    "bitrate_target": r["target_bitrate"],
                    "object_type": r.get("profile", "lc")
                } for r in base_recs}

                bd_rows = []
                for cand_key in all_row_keys:
                    if cand_key == base_key:
                        continue
                    cand_recs = [r for r in results if r["row_key"] == cand_key and r.get("decode_valid") and r.get("mos") is not None and r.get("actual_bitrate")]
                    cand_mat = {f"{r['scenario']}_{r['filename']}": {
                        "scenario": r["scenario"], "filename": r["filename"],
                        "mos": r["mos"], "bitrate": r["actual_bitrate"],
                        "bitrate_target": r["target_bitrate"],
                        "object_type": r.get("profile", "lc")
                    } for r in cand_recs}

                    if not cand_mat:
                        continue

                    cand_name = encoder_info[cand_key].name if cand_key in encoder_info else cand_key
                    cand_prof = encoder_info[cand_key].profile if cand_key in encoder_info else "lc"

                    bdr_analysis = bdr.analyze(base_mat, cand_mat)
                    segs = bdr_analysis.get("segments", [])
                    valid_means = [s["stats"]["mean"] for s in segs if s.get("stats") and s["stats"].get("mean") is not None]

                    if valid_means:
                        overall_bd = sum(valid_means) / len(valid_means)
                        bd_str = f"**{overall_bd:+.2f}%**" if overall_bd < 0 else f"{overall_bd:+.2f}%"
                        bd_rows.append((cand_name, profile_label(cand_prof), bd_str))

                if bd_rows:
                    f.write("| Encoder | Profile | BD-Rate % vs Baseline |\n")
                    f.write("| :--- | :---: | :---: |\n")
                    for name, prof, val in bd_rows:
                        f.write(f"| {name} | {prof} | {val} |\n")
                    f.write("\n")
                else:
                    f.write("_No valid BD-rate ladders found between baseline and candidate encoders._\n\n")
        except Exception as e:
            f.write(f"_BD-rate analysis unavailable ({e})._\n\n")

        # 8. Encoder Efficiency & Footprint
        f.write("### Encoder Efficiency & Footprint\n\n")

        if not skip_graphs and sorted_tools:
            tool_labels = [f'"{tool_overall[t]["tool"]}"' for t in sorted_tools if t in tool_overall]
            tool_speeds = [f"{tool_overall[t]['avg_speed']:.1f}" for t in sorted_tools if t in tool_overall]
            tool_roms = [f"{(tool_overall[t]['text_size'] + tool_overall[t]['rodata_size']) / 1024.0:.1f}" for t in sorted_tools if t in tool_overall]

            max_speed = max([tool_overall[t]['avg_speed'] for t in sorted_tools if t in tool_overall] + [1.0])
            max_rom = max([(tool_overall[t]['text_size'] + tool_overall[t]['rodata_size']) / 1024.0 for t in sorted_tools if t in tool_overall] + [1.0])

            f.write("#### Encoding Speed (xRT)\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Average Encoding Speed (xRealtime, Higher is Better)"\n')
            f.write(f"    x-axis [{', '.join(tool_labels)}]\n")
            f.write(f'    y-axis "Speed (xRT)" 0 --> {int(max_speed * 1.25) + 1}\n')
            f.write(f'    bar "Encoding Speed" [{", ".join(tool_speeds)}]\n')
            f.write("```\n\n")

            f.write("#### Codec ROM (Flash) Size\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Codec Code + Read-Only Data Size (KB, Lower is Better)"\n')
            f.write(f"    x-axis [{', '.join(tool_labels)}]\n")
            f.write(f'    y-axis "ROM Size (KB)" 0 --> {int(max_rom * 1.25) + 1}\n')
            f.write(f'    bar "ROM Size" [{", ".join(tool_roms)}]\n')
            f.write("```\n\n")

        f.write("<details><summary><b>View Detailed Per-Scenario Efficiency Table</b></summary>\n\n")
        for p in ["lc", "he", "hev2", "standard"]:
            p_rks = [rk for rk in all_row_keys if encoder_info[rk].profile == p]
            if not p_rks:
                continue
            p_has_data = any(stats[rk][s_name]["speed_count"] > 0 for rk in p_rks for s_name in scenario_list)
            if not p_has_data:
                continue

            f.write(f"#### {profile_label(p)} Profile\n\n")
            f.write("| Scenario | " + " | ".join(encoder_info[rk].name for rk in p_rks) + " |\n")
            f.write("| :--- | " + " | ".join([":---:"] * len(p_rks)) + " |\n")

            max_p_speed = max([stats[rk][s_name]["speed_sum"] / stats[rk][s_name]["speed_count"] for rk in p_rks for s_name in scenario_list if stats[rk][s_name]["speed_count"] > 0] + [1.0])

            for s_name in sorted(scenario_list, key=get_scenario_sort_key):
                row_str = f"| {s_name} |"
                p_valid_speed = [stats[rk][s_name]["speed_sum"] / stats[rk][s_name]["speed_count"] for rk in p_rks if stats[rk][s_name]["speed_count"] > 0]
                best_p_speed = max(p_valid_speed) if p_valid_speed else None

                for rk in p_rks:
                    st = stats[rk][s_name]
                    if st["speed_count"] > 0:
                        speed = st["speed_sum"] / st["speed_count"]
                        is_best = best_p_speed and abs(speed - best_p_speed) < 1e-6
                        p_bar = make_progress_bar(speed, max_p_speed)
                        if is_best:
                            cell = f" **{speed:.1f}x**{p_bar}"
                        else:
                            cell = f" {speed:.1f}x{p_bar}"
                        row_str += f"{cell} |"
                    else:
                        row_str += " N/A |"
                f.write(row_str + "\n")
            f.write("\n")
        f.write("</details>\n\n")

        f.write("</details>\n\n")

        # 9. Quality Outliers (Outlier clip bug flags)
        if bug_flags:
            f.write("<details><summary><b>🐛 View Quality Outliers (Issues Worth Investigating)</b></summary>\n\n")
            f.write("### Quality Outliers (Issues Worth Investigating)\n\n")
            f.write("> **Note**: Flags clips where this encoder scored **≥0.75 MOS lower** than the average of all other encoders on the exact same clip. "
                    "These clips represent isolated codec defects, killer clips, or tuning bugs.\n\n")

            f.write("| Encoder | Profile | Scenario | Outlier Clip | Encoder MOS | Peer Avg MOS | Defect Gap |\n")
            f.write("| :--- | :---: | :--- | :--- | :---: | :---: | :---: |\n")

            sorted_flags = sorted(bug_flags, key=lambda x: (x[5] - x[4]), reverse=True)
            for tool_name, p, s_name, filename, this_mos, peer_avg in sorted_flags:
                gap = peer_avg - this_mos
                f.write(f"| {tool_name} | {profile_label(p)} | {s_name} | `{filename}` | {this_mos:.2f} | {peer_avg:.2f} | **-{gap:.2f} MOS** |\n")
            f.write("\n</details>\n\n")

        # Metric Legend & Footnotes
        f.write("\n---\n")
        f.write("**Metric Legend**:\n")
        f.write("- **Ranking**: by Worst MOS, then Overall MOS as tiebreaker.\n")
        f.write("- **Quality (MOS)**: Perceptual audio quality (1-5, **Higher is Better**)\n")
        f.write("- **Stereo Fidelity**: Faithfulness of stereo image (0-1, **Higher is Better**)\n")
        f.write("- **Transient Fidelity**: How little attacks are smeared/delayed (0-1, **Higher is Better**)\n")
        f.write("- **Speed**: Encoding throughput (**Higher is Better**)\n")
        f.write("- **Bitrate Error**: Deviation from target bitrate (**Lower is Better**)\n")
        f.write("- **ROM (Flash)**: Codec code + read-only data size (**Lower is Better**)\n")

    print(f"\nLeaderboard generated at: {output_path}")


def generate_decoder_leaderboard(decoders, results, output_path, scenario_list, skip_graphs=False, encoders=None, encoder_results=None, robustness_results=None, run_encoders_leaderboard=False, append_mode=False):
    if run_encoders_leaderboard and encoders and encoder_results:
        generate_leaderboard(encoders, encoder_results, output_path, scenario_list, skip_graphs=skip_graphs, has_decoders=True)
        append_mode = True

    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "snr_sum": 0, "snr_count": 0,
        "delay_sum": 0, "delay_count": 0,
        "ram_sum": 0, "ram_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    p_stats = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0, "mos_min_file": None,
        "snr_sum": 0, "snr_count": 0,
        "delay_sum": 0, "delay_count": 0,
        "ram_sum": 0, "ram_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "valid_count": 0, "total_count": 0
    })))

    decoder_info = {decoder_row_key(d): d for d in decoders}
    clip_mos = defaultdict(dict)
    bug_flags = []
    mono_downmix_counts = defaultdict(int)
    timeout_counts = defaultdict(int)

    for res in results:
        rk = res["row_key"]
        s = res["scenario"]
        p = res.get("profile", "lc")

        if rk not in decoder_info:
            tool_name = res.get("tool") or rk
            decoder_info[rk] = type("DummyDecoder", (), {
                "name": tool_name,
                "tool_id": rk,
                "text_size": 0,
                "rodata_size": 0
            })()

        stats[rk][s]["total_count"] += 1
        p_stats[rk][p][s]["total_count"] += 1

        if res.get("mono_downmix"):
            mono_downmix_counts[rk] += 1

        if res.get("timeout") or "timed out" in (res.get("decode_error") or "").lower() or "timeout" in (res.get("decode_error") or "").lower():
            timeout_counts[rk] += 1
            if res.get("filename"):
                bug_flags.append((decoder_info[rk].name, p, s, res["filename"], 0.0, 0.0, "Timeout expired"))

        if res.get("decode_valid"):
            stats[rk][s]["valid_count"] += 1
            p_stats[rk][p][s]["valid_count"] += 1

            if res.get("mos") is not None:
                stats[rk][s]["mos_sum"] += res["mos"]
                stats[rk][s]["mos_count"] += 1
                if res["mos"] < stats[rk][s].get("mos_min", 6.0):
                    stats[rk][s]["mos_min"] = res["mos"]
                    stats[rk][s]["mos_min_file"] = res.get("filename")

                p_stats[rk][p][s]["mos_sum"] += res["mos"]
                p_stats[rk][p][s]["mos_count"] += 1
                if res["mos"] < p_stats[rk][p][s].get("mos_min", 6.0):
                    p_stats[rk][p][s]["mos_min"] = res["mos"]
                    p_stats[rk][p][s]["mos_min_file"] = res.get("filename")

                if res.get("filename"):
                    clip_mos[(s, res["filename"])][rk] = res["mos"]

            if res.get("snr_db") is not None and res["snr_db"] != float("inf"):
                stats[rk][s]["snr_sum"] += res["snr_db"]
                stats[rk][s]["snr_count"] += 1

                p_stats[rk][p][s]["snr_sum"] += res["snr_db"]
                p_stats[rk][p][s]["snr_count"] += 1

            if res.get("alignment_delay_ms") is not None:
                stats[rk][s]["delay_sum"] += abs(res["alignment_delay_ms"])
                stats[rk][s]["delay_count"] += 1

                p_stats[rk][p][s]["delay_sum"] += abs(res["alignment_delay_ms"])
                p_stats[rk][p][s]["delay_count"] += 1

            if res.get("peak_ram_kb") is not None and res["peak_ram_kb"] > 0:
                stats[rk][s]["ram_sum"] += res["peak_ram_kb"]
                stats[rk][s]["ram_count"] += 1

                p_stats[rk][p][s]["ram_sum"] += res["peak_ram_kb"]
                p_stats[rk][p][s]["ram_count"] += 1

            if res.get("duration", 0) > 0 and res.get("audio_duration"):
                spd = res["audio_duration"] / res["duration"]
                stats[rk][s]["speed_sum"] += spd
                stats[rk][s]["speed_count"] += 1

                p_stats[rk][p][s]["speed_sum"] += spd
                p_stats[rk][p][s]["speed_count"] += 1

    rob_stats = defaultdict(lambda: {"passed": 0, "total": 0})
    if robustness_results:
        for r_res in robustness_results:
            rk = r_res["row_key"]
            rob_stats[rk]["total"] += 1
            if r_res.get("passed"):
                rob_stats[rk]["passed"] += 1

    overall = {}
    for rk, dec_obj in decoder_info.items():
        d_mos, d_speed, d_snr, d_delay, d_ram = [], [], [], [], []
        d_worst_mos = 6.0
        d_total = d_valid = 0
        scenario_count = 0

        for s_name in scenario_list:
            st = stats[rk][s_name]
            d_total += st["total_count"]
            d_valid += st["valid_count"]
            if st["mos_count"] > 0:
                d_mos.append(st["mos_sum"] / st["mos_count"])
                d_worst_mos = min(d_worst_mos, st["mos_min"])
                scenario_count += 1
            if st["snr_count"] > 0:
                d_snr.append(st["snr_sum"] / st["snr_count"])
            if st["delay_count"] > 0:
                d_delay.append(st["delay_sum"] / st["delay_count"])
            if st["ram_count"] > 0:
                d_ram.append(st["ram_sum"] / st["ram_count"])
            if st["speed_count"] > 0:
                d_speed.append(st["speed_sum"] / st["speed_count"])

        rob_info = rob_stats[rk]
        robustness_pct = (rob_info["passed"] / rob_info["total"] * 100.0) if rob_info["total"] > 0 else 100.0

        overall[rk] = {
            "tool": dec_obj.name,
            "worst_mos": d_worst_mos if d_mos else 0,
            "overall_mos": sum(d_mos) / len(d_mos) if d_mos else 0,
            "avg_snr_db": sum(d_snr) / len(d_snr) if d_snr else None,
            "avg_delay_ms": sum(d_delay) / len(d_delay) if d_delay else 0.0,
            "avg_ram_kb": sum(d_ram) / len(d_ram) if d_ram else 0,
            "avg_speed": sum(d_speed) / len(d_speed) if d_speed else 0,
            "robustness_pct": robustness_pct,
            "text_size": dec_obj.text_size,
            "rodata_size": dec_obj.rodata_size,
            "valid_rate": (d_valid / d_total * 100) if d_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    sorted_rk = sorted(overall.keys(), key=lambda rk: (overall[rk]["worst_mos"], overall[rk]["overall_mos"]), reverse=True)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    open_mode = "a" if append_mode else "w"
    with open(output_path, open_mode) as f:
        if append_mode:
            f.write("\n\n---\n\n")
        else:
            f.write("# 🔊 AAC Decoder Leaderboard\n\n")
            nav_links = ["[📊 Decoder Rankings](#decoder-leaderboard)", "[📋 Decoder Breakdowns](#per-scenario-decoder-breakdowns)"]
            f.write(" | ".join(nav_links) + "\n\n---\n\n")

        f.write('<a name="decoder-leaderboard"></a>\n')
        f.write("## 🔊 Decoder Leaderboard\n\n")
        f.write("Objective evaluation of AAC decoders on Spec Conformance (SNR), Decoded Quality (MOS), Timing Alignment Error, Robustness, Speed, and Footprint.\n\n")

        f.write("### Overall Decoder Rankings\n\n")
        f.write("| Rank | Decoder | Status | Worst MOS | Overall MOS | Mean SNR | Timing Error | Robustness | Speed (xRT) | Peak RAM | ROM (Flash) |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_worst_mos = max(o["worst_mos"] for o in overall.values()) if overall else 0
        best_mos = max(o["overall_mos"] for o in overall.values()) if overall else 0
        best_speed = max(o["avg_speed"] for o in overall.values()) if overall else 0
        best_robustness = max(o["robustness_pct"] for o in overall.values()) if overall else 100.0

        for i, rk in enumerate(sorted_rk):
            o = overall[rk]
            rank_str = f"🏆 {i+1}" if i == 0 and o["worst_mos"] > 0 else f"{i+1}"

            if timeout_counts[rk] > 0:
                status_str = f"⚠️ ({timeout_counts[rk]}x timeout)"
            elif mono_downmix_counts[rk] > 0:
                status_str = "No HE-v2 PS (1ch)"
            elif o["valid_rate"] == 100:
                status_str = "OK"
            else:
                status_str = f"⚠️ ({o['valid_rate']:.0f}% valid)"

            w_str = f"**{o['worst_mos']:.3f}**" if abs(o['worst_mos'] - best_worst_mos) < 1e-6 and o['worst_mos'] > 0 else f"{o['worst_mos']:.3f}"
            m_str = f"**{o['overall_mos']:.3f}**" if abs(o['overall_mos'] - best_mos) < 1e-6 and o['overall_mos'] > 0 else f"{o['overall_mos']:.3f}"

            snr_str = f"{o['avg_snr_db']:.1f} dB" if o['avg_snr_db'] is not None else "Bit-Exact / N/A"
            delay_str = f"{o['avg_delay_ms']:.2f} ms" if o['avg_delay_ms'] is not None else "N/A"
            rob_str = f"**{o['robustness_pct']:.1f}%**" if abs(o['robustness_pct'] - best_robustness) < 1e-6 else f"{o['robustness_pct']:.1f}%"

            s_str = f"**{o['avg_speed']:.1f}x**" if abs(o['avg_speed'] - best_speed) < 1e-6 and o['avg_speed'] > 0 else f"{o['avg_speed']:.1f}x"
            ram_str = format_size(int(o["avg_ram_kb"] * 1024)) if o["avg_ram_kb"] > 0 else "N/A"
            rom_str = format_size(o["text_size"] + o["rodata_size"])

            f.write(f"| {rank_str} | {o['tool']} | {status_str} | {w_str} | {m_str} | {snr_str} | {delay_str} | {rob_str} | {s_str} | {ram_str} | {rom_str} |\n")

        f.write('\n<a name="per-scenario-decoder-breakdowns"></a>\n')
        f.write("<details><summary><b>📊 View Per-Scenario Decoder Breakdowns</b></summary>\n\n")
        f.write("### Detailed Per-Scenario Decoder Breakdowns\n\n")

        present_families = scenario_families(scenario_list)
        for fam in present_families:
            fam_label = family_label(fam)
            fam_scenarios = [s for s in scenario_list if scenario_family(s) == fam]
            if not fam_scenarios:
                continue

            f.write(f"#### {fam_label}\n\n")
            x_labels = [s.rsplit("_", 1)[-1] for s in fam_scenarios]

            # 1. Per-Scenario Average MOS
            f.write(f"##### Per-Scenario Average MOS ({fam_label})\n\n")

            dec_line_data_mos = {}
            for rk in sorted_rk:
                vals = [stats[rk][s]["mos_sum"] / stats[rk][s]["mos_count"] if stats[rk][s]["mos_count"] > 0 else None for s in fam_scenarios]
                if any(v is not None for v in vals):
                    dec_line_data_mos[rk] = vals

            if not skip_graphs and dec_line_data_mos:
                chart_vals_mos = [v for vals in dec_line_data_mos.values() for v in vals if v is not None]
                axis_lo_m, axis_hi_m = zoomed_y_range(chart_vals_mos, "1.0 --> 5.0")
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "Decoder Perceptual Quality across Bitrates - {fam_label} (Average MOS)"\n')
                f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                f.write(f'    y-axis "MOS Score" {axis_lo_m:.4g} --> {axis_hi_m:.4g}\n')
                for rk, vals in dec_line_data_mos.items():
                    v_str = [f"{v:.4f}" if v is not None else "0.0" for v in vals]
                    f.write(f'    line "{overall[rk]["tool"]}" [{", ".join(v_str)}]\n')
                f.write("```\n\n")

            table_used_1ch_note = False
            for p in ["lc", "he", "hev2"]:
                p_has_data = any(p_stats[rk][p][s_name]["mos_count"] > 0 for rk in sorted_rk for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                max_p_dec_speed = max([p_stats[rk][p][s_name]["speed_sum"] / p_stats[rk][p][s_name]["speed_count"] for rk in sorted_rk for s_name in fam_scenarios if p_stats[rk][p][s_name]["speed_count"] > 0] + [1.0])

                f.write(f"###### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(overall[rk]["tool"] for rk in sorted_rk) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(sorted_rk)) + " |\n")
                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    valid_mos = [p_stats[rk][p][s_name]["mos_sum"] / p_stats[rk][p][s_name]["mos_count"] for rk in sorted_rk if p_stats[rk][p][s_name]["mos_count"] > 0]
                    best_m = max(valid_mos) if valid_mos else None
                    for rk in sorted_rk:
                        st = p_stats[rk][p][s_name]
                        if st["mos_count"] > 0:
                            avg_m = st["mos_sum"] / st["mos_count"]
                            is_best = best_m and abs(avg_m - best_m) < 1e-6
                            p_bar = make_progress_bar(avg_m, 5.0)

                            min_f = st.get("mos_min_file")
                            gap = cell_peer_gap(clip_mos, rk, s_name, min_f)
                            if gap is not None:
                                issue_text = "Mono output (No PS)" if (p == "hev2" and mono_downmix_counts[rk] > 0) else "MOS quality gap vs peers"
                                bug_flags.append((decoder_info[rk].name, p, s_name, min_f, gap[0], gap[1], issue_text))

                            is_1ch = (p == "hev2" and mono_downmix_counts[rk] > 0)
                            if is_1ch:
                                table_used_1ch_note = True

                            ch_tag = " (1ch)" if is_1ch else ""
                            if is_best and is_1ch:
                                cell = f" _**{avg_m:.3f}**_{ch_tag}{p_bar}"
                            elif is_best:
                                cell = f" **{avg_m:.3f}**{p_bar}"
                            elif is_1ch:
                                cell = f" _{avg_m:.3f}_{ch_tag}{p_bar}"
                            else:
                                cell = f" {avg_m:.3f}{p_bar}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")

            if table_used_1ch_note:
                f.write("_Scores marked (1ch) indicate mono output on HE-v2 stereo bitstreams due to lack of Parametric Stereo decoding._\n\n")

            # 2. Spec Conformance (SNR)
            f.write(f"##### Spec Conformance (Mean SNR - {fam_label})\n\n")

            dec_line_data_snr = {}
            for rk in sorted_rk:
                vals = [stats[rk][s]["snr_sum"] / stats[rk][s]["snr_count"] if stats[rk][s]["snr_count"] > 0 else None for s in fam_scenarios]
                if any(v is not None for v in vals):
                    dec_line_data_snr[rk] = vals

            if not skip_graphs and dec_line_data_snr:
                chart_vals_snr = [v for vals in dec_line_data_snr.values() for v in vals if v is not None]
                axis_lo_s, axis_hi_s = zoomed_y_range(chart_vals_snr, "0.0 --> 60.0")
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "Decoder Spec Conformance across Bitrates - {fam_label} (Mean SNR dB)"\n')
                f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                f.write(f'    y-axis "SNR (dB)" {axis_lo_s:.4g} --> {axis_hi_s:.4g}\n')
                for rk, vals in dec_line_data_snr.items():
                    v_str = [f"{v:.4f}" if v is not None else "0.0" for v in vals]
                    f.write(f'    line "{overall[rk]["tool"]}" [{", ".join(v_str)}]\n')
                f.write("```\n\n")

            for p in ["lc", "he", "hev2"]:
                p_has_data = any(p_stats[rk][p][s_name]["snr_count"] > 0 for rk in sorted_rk for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                max_p_snr = max([p_stats[rk][p][s_name]["snr_sum"] / p_stats[rk][p][s_name]["snr_count"] for rk in sorted_rk for s_name in fam_scenarios if p_stats[rk][p][s_name]["snr_count"] > 0] + [1.0])

                f.write(f"###### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(overall[rk]["tool"] for rk in sorted_rk) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(sorted_rk)) + " |\n")
                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    valid_snr = [p_stats[rk][p][s_name]["snr_sum"] / p_stats[rk][p][s_name]["snr_count"] for rk in sorted_rk if p_stats[rk][p][s_name]["snr_count"] > 0]
                    best_snr = max(valid_snr) if valid_snr else None
                    for rk in sorted_rk:
                        st = p_stats[rk][p][s_name]
                        if st["snr_count"] > 0:
                            avg_snr = st["snr_sum"] / st["snr_count"]
                            is_best = best_snr and abs(avg_snr - best_snr) < 1e-6
                            p_bar = make_progress_bar(avg_snr, max_p_snr)
                            cell = f" **{avg_snr:.1f} dB**{p_bar}" if is_best else f" {avg_snr:.1f} dB{p_bar}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " Bit-Exact / N/A |"
                    f.write(row_str + "\n")
                f.write("\n")

            # 3. Timing Alignment Delay
            f.write(f"##### Timing Alignment Delay (ms - {fam_label})\n\n")
            for p in ["lc", "he", "hev2"]:
                p_has_data = any(p_stats[rk][p][s_name]["delay_count"] > 0 for rk in sorted_rk for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                max_p_delay = max([p_stats[rk][p][s_name]["delay_sum"] / p_stats[rk][p][s_name]["delay_count"] for rk in sorted_rk for s_name in fam_scenarios if p_stats[rk][p][s_name]["delay_count"] > 0] + [0.1])

                f.write(f"###### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(overall[rk]["tool"] for rk in sorted_rk) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(sorted_rk)) + " |\n")
                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    valid_del = [p_stats[rk][p][s_name]["delay_sum"] / p_stats[rk][p][s_name]["delay_count"] for rk in sorted_rk if p_stats[rk][p][s_name]["delay_count"] > 0]
                    best_del = min(valid_del) if valid_del else None
                    for rk in sorted_rk:
                        st = p_stats[rk][p][s_name]
                        if st["delay_count"] > 0:
                            avg_del = st["delay_sum"] / st["delay_count"]
                            is_best = best_del is not None and abs(avg_del - best_del) < 1e-6
                            p_bar = make_progress_bar(avg_del, max_p_delay, lower_is_better=True)
                            cell = f" **{avg_del:.2f} ms**{p_bar}" if is_best else f" {avg_del:.2f} ms{p_bar}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")

            # 4. Decoding Speed
            f.write(f"##### Decoding Speed (xRT - {fam_label})\n\n")

            dec_line_data_speed = {}
            for rk in sorted_rk:
                vals = [stats[rk][s]["speed_sum"] / stats[rk][s]["speed_count"] if stats[rk][s]["speed_count"] > 0 else None for s in fam_scenarios]
                if any(v is not None for v in vals):
                    dec_line_data_speed[rk] = vals

            if not skip_graphs and dec_line_data_speed:
                chart_vals_speed = [v for vals in dec_line_data_speed.values() for v in vals if v is not None]
                axis_lo_sp, axis_hi_sp = zoomed_y_range(chart_vals_speed, "0.0 --> 200.0")
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "Decoding Speed across Bitrates - {fam_label} (xRealtime)"\n')
                f.write(f"    x-axis [{', '.join([f'\"{x}\"' for x in x_labels])}]\n")
                f.write(f'    y-axis "Speed (xRT)" {axis_lo_sp:.4g} --> {axis_hi_sp:.4g}\n')
                for rk, vals in dec_line_data_speed.items():
                    v_str = [f"{v:.4f}" if v is not None else "0.0" for v in vals]
                    f.write(f'    line "{overall[rk]["tool"]}" [{", ".join(v_str)}]\n')
                f.write("```\n\n")

            for p in ["lc", "he", "hev2"]:
                p_has_data = any(p_stats[rk][p][s_name]["speed_count"] > 0 for rk in sorted_rk for s_name in fam_scenarios)
                if not p_has_data:
                    continue

                max_p_dec_speed = max([p_stats[rk][p][s_name]["speed_sum"] / p_stats[rk][p][s_name]["speed_count"] for rk in sorted_rk for s_name in fam_scenarios if p_stats[rk][p][s_name]["speed_count"] > 0] + [1.0])

                f.write(f"###### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(overall[rk]["tool"] for rk in sorted_rk) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(sorted_rk)) + " |\n")
                for s_name in fam_scenarios:
                    row_str = f"| {s_name} |"
                    valid_spd = [p_stats[rk][p][s_name]["speed_sum"] / p_stats[rk][p][s_name]["speed_count"] for rk in sorted_rk if p_stats[rk][p][s_name]["speed_count"] > 0]
                    best_spd = max(valid_spd) if valid_spd else None
                    for rk in sorted_rk:
                        st = p_stats[rk][p][s_name]
                        if st["speed_count"] > 0:
                            avg_spd = st["speed_sum"] / st["speed_count"]
                            is_best = best_spd and abs(avg_spd - best_spd) < 1e-6
                            p_bar = make_progress_bar(avg_spd, max_p_dec_speed)
                            cell = f" **{avg_spd:.1f}x**{p_bar}" if is_best else f" {avg_spd:.1f}x{p_bar}"
                            row_str += f"{cell} |"
                        else:
                            row_str += " N/A |"
                    f.write(row_str + "\n")
                f.write("\n")

        if not skip_graphs and sorted_rk:
            f.write("\n### Decoder Efficiency & Footprint\n\n")
            labels = [f'"{overall[rk]["tool"]}"' for rk in sorted_rk]
            speeds = [f"{overall[rk]['avg_speed']:.1f}" for rk in sorted_rk]
            max_s = max([overall[rk]['avg_speed'] for rk in sorted_rk] + [1.0])

            f.write("#### Decoding Speed (xRT)\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Average Decoding Throughput (xRealtime, Higher is Better)"\n')
            f.write(f"    x-axis [{', '.join(labels)}]\n")
            f.write(f'    y-axis "Speed (xRT)" 0 --> {int(max_s * 1.25) + 1}\n')
            f.write(f'    bar "Decoding Speed" [{", ".join(speeds)}]\n')
            f.write("```\n\n")

        f.write("\n</details>\n\n")

        if bug_flags:
            f.write("<details><summary><b>🐛 View Quality Outliers (Issues Worth Investigating)</b></summary>\n\n")
            f.write("### Decoder Quality Outliers (Issues Worth Investigating)\n\n")
            f.write("> **Note**: Flags clips where this decoder scored **≥0.75 MOS lower** than the average of all other decoders on the exact same clip or produced mono output on stereo bitstreams.\n\n")

            f.write("| Decoder | Profile | Scenario | Outlier Clip | Decoder MOS | Peer Avg MOS | Defect Gap | Issue / Cause |\n")
            f.write("| :--- | :---: | :--- | :--- | :---: | :---: | :---: | :--- |\n")

            sorted_flags = sorted(bug_flags, key=lambda x: (x[5] - x[4]), reverse=True)
            for tool_name, p, s_name, filename, this_mos, peer_avg, issue in sorted_flags:
                gap = peer_avg - this_mos
                f.write(f"| {tool_name} | {profile_label(p)} | {s_name} | `{filename}` | {this_mos:.2f} | {peer_avg:.2f} | **-{gap:.2f} MOS** | {issue} |\n")
            f.write("\n</details>\n\n")

    print(f"\nDecoder leaderboard generated at: {output_path}")


def _fmt(val, spec="{:.2f}"):
    if val is None:
        return "n/a"
    if isinstance(val, str):
        return val
    try:
        return spec.format(val)
    except Exception:
        return "n/a"


def _kb(bytes_val):
    if not bytes_val:
        return "n/a"
    return f"{bytes_val / 1024.0:.1f} KB"


def generate_decoder_report(decoders, decoder_results, robustness_results, output_path,
                             muxer_results=None, faam_footprint=None):
    """Renders the measured-only decoder/muxer report matching the sections
    of docs/DECODER_MUXER_BENCHMARK_ANALYSIS.md: overall comparison,
    footprint, latency, muxer throughput, and multichannel/robustness
    (including gapless offset). Every number here comes from decoder_results
    / robustness_results / muxer_results -- nothing is fabricated, and a
    cell reads "n/a" when the run didn't measure it (e.g. --skip-mos, or a
    tool that wasn't found)."""
    muxer_results = muxer_results or []
    by_tool = defaultdict(list)
    for r in decoder_results:
        by_tool[r["row_key"]].append(r)

    decoder_order = {d.tool_id: i for i, d in enumerate(decoders)}
    profile_order = [("lc", "LC"), ("he", "HE-v1"), ("hev2", "HE-v2")]
    if not any(r.get("profile") == "hev2" for r in decoder_results):
        profile_order = [p for p in profile_order if p[0] != "hev2"]

    def _decoder_name(row_key, fallback_row=None):
        return next((d.name for d in decoders if d.tool_id == row_key),
                     fallback_row["tool"] if fallback_row else row_key)

    ffmpeg_tool_id = next((d.tool_id for d in decoders if "ffmpeg" in d.tool_id.lower()), None)
    ffmpeg_mos_by_key = {}
    if ffmpeg_tool_id:
        for r in by_tool.get(ffmpeg_tool_id, []):
            if r.get("decode_valid") and isinstance(r.get("mos"), (int, float)):
                ffmpeg_mos_by_key[(r.get("encoder_row_key"), r["scenario"], r["filename"])] = r["mos"]

    with open(output_path, "w") as f:
        f.write("# FAAD Decoder & Muxer Benchmark (measured)\n\n")

        # --- Section 1: overall comparison -----------------------------
        f.write("## 1. Overall Comparison\n\n")
        header = "| Decoder | .text | Peak RSS (median) |"
        for _, label in profile_order:
            header += f" Mean MOS ({label}) |"
        header += " ΔMOS vs FFmpeg | Conformance SNR (median, dB) | Conformance SNR (min, dB) | xRT (median) |"
        f.write(header + "\n")
        f.write("| :--- | ---: | ---: |" + " ---: |" * len(profile_order) + " ---: | ---: | ---: | ---: |\n")

        for dec in decoders:
            rows = [r for r in by_tool.get(dec.tool_id, []) if r.get("decode_valid")]
            rss = [r["peak_ram_kb"] for r in rows if r.get("peak_ram_kb")]
            csnr = [r["conformance_snr_db"] for r in rows if isinstance(r.get("conformance_snr_db"), (int, float))]
            xrt = [r["speed_xrt"] for r in rows if r.get("speed_xrt")]

            line = (f"| {dec.name} | {_kb(getattr(dec, 'text_size', None))} | "
                    f"{(f'{statistics.median(rss) / 1024.0:.1f} MB' if rss else 'n/a')} |")
            for prof_key, _ in profile_order:
                mos_vals = [r["mos"] for r in rows if r.get("profile") == prof_key and isinstance(r.get("mos"), (int, float))]
                line += f" {_fmt(statistics.mean(mos_vals)) if mos_vals else 'n/a'} |"

            deltas = []
            for r in rows:
                if not isinstance(r.get("mos"), (int, float)):
                    continue
                ref_mos = ffmpeg_mos_by_key.get((r.get("encoder_row_key"), r["scenario"], r["filename"]))
                if ref_mos is not None:
                    deltas.append(r["mos"] - ref_mos)
            line += f" {_fmt(statistics.mean(deltas), '{:+.2f}') if deltas else 'n/a'} |"

            line += (f" {_fmt(statistics.median(csnr)) if csnr else 'n/a'} |"
                      f" {_fmt(min(csnr)) if csnr else 'n/a'} |"
                      f" {_fmt(statistics.median(xrt), '{:.1f}x') if xrt else 'n/a'} |")
            f.write(line + "\n")
        f.write("\n")

        n_inherited = sum(1 for r in decoder_results if r.get("mos_source") == "inherited")
        n_scored = sum(1 for r in decoder_results if r.get("mos_source") == "scored")
        n_total = n_inherited + n_scored
        if n_total:
            f.write(f"*MOS: {n_inherited} row(s) ({n_inherited / n_total * 100:.0f}%) inherited the encoder "
                    f"phase's ffmpeg-decode score (conformance SNR >= {CONFORMANCE_SNR_FLOOR_DB:.0f} dB vs that "
                    f"decode), {n_scored} row(s) ({n_scored / n_total * 100:.0f}%) scored directly.*\n\n")

        # --- Section 2: footprint --------------------------------------
        f.write("## 2. Binary Footprint\n\n")
        f.write("| Component | .text | .rodata | .bss | .data |\n")
        f.write("| :--- | ---: | ---: | ---: | ---: |\n")
        for dec in decoders:
            f.write(f"| {dec.name} | {_kb(dec.text_size)} | {_kb(dec.rodata_size)} | {_kb(dec.bss_size)} | {_kb(dec.data_size)} |\n")
        if faam_footprint:
            f.write(f"| faam | {_kb(faam_footprint.get('text'))} | {_kb(faam_footprint.get('rodata'))} "
                    f"| {_kb(faam_footprint.get('bss'))} | {_kb(faam_footprint.get('data'))} |\n")
        f.write("\n")

        # --- Section 3: latency -----------------------------------------
        f.write("## 3. Decoder Throughput & Execution Latency\n\n")
        f.write("| Profile | Decoder | Streams | Median Mean (ms) | Median xRT | Median MB/s | Median Peak RAM (MB) |\n")
        f.write("| :---: | :--- | ---: | ---: | ---: | ---: | ---: |\n")

        by_profile_decoder = defaultdict(list)
        for r in decoder_results:
            if r.get("speed_mean_ms") is None:
                continue
            by_profile_decoder[(r.get("profile"), r["row_key"])].append(r)

        for prof_key, prof_label in profile_order:
            dec_ids = sorted({rk for (p, rk) in by_profile_decoder if p == prof_key},
                              key=lambda rk: decoder_order.get(rk, 999))
            for rk in dec_ids:
                grp = by_profile_decoder[(prof_key, rk)]
                mean_ms = [r["speed_mean_ms"] for r in grp]
                xrt = [r["speed_xrt"] for r in grp if r.get("speed_xrt") is not None]
                mbps = [r["speed_mbps"] for r in grp if r.get("speed_mbps") is not None]
                ram = [r["peak_ram_kb"] for r in grp if r.get("peak_ram_kb")]
                f.write(f"| {prof_label} | {_decoder_name(rk, grp[0])} | {len(grp)} | {_fmt(statistics.median(mean_ms))} "
                        f"| {_fmt(statistics.median(xrt), '{:.1f}x') if xrt else 'n/a'} "
                        f"| {_fmt(statistics.median(mbps)) if mbps else 'n/a'} "
                        f"| {(f'{statistics.median(ram) / 1024.0:.1f}' if ram else 'n/a')} |\n")
        f.write("\n")
        f.write("*Latency is measured inside the parallel worker pool, so per-stream std dev is inflated by "
                "concurrent MOS scoring; the medians above are the comparable figure.*\n\n")

        # --- Section 4: conformance vs ffmpeg reference ------------------
        f.write("## 4. Conformance vs FFmpeg reference\n\n")
        f.write("| Profile | Decoder | Streams | Median dB | Min dB | Count >= 60 dB | Count < 60 dB |\n")
        f.write("| :---: | :--- | ---: | ---: | ---: | ---: | ---: |\n")

        by_profile_decoder_csnr = defaultdict(list)
        for r in decoder_results:
            if not isinstance(r.get("conformance_snr_db"), (int, float)):
                continue
            by_profile_decoder_csnr[(r.get("profile"), r["row_key"])].append(r)

        for prof_key, prof_label in profile_order:
            dec_ids = sorted({rk for (p, rk) in by_profile_decoder_csnr if p == prof_key},
                              key=lambda rk: decoder_order.get(rk, 999))
            for rk in dec_ids:
                grp = by_profile_decoder_csnr[(prof_key, rk)]
                vals = [r["conformance_snr_db"] for r in grp]
                n_ge = sum(1 for v in vals if v >= CONFORMANCE_SNR_FLOOR_DB)
                n_lt = sum(1 for v in vals if v < CONFORMANCE_SNR_FLOOR_DB)
                f.write(f"| {prof_label} | {_decoder_name(rk, grp[0])} | {len(grp)} | {_fmt(statistics.median(vals))} "
                        f"| {_fmt(min(vals))} | {n_ge} | {n_lt} |\n")
        f.write("\n")

        def _is_pns_free(erk):
            if not erk:
                return False
            if "_nopns" in erk:
                return True
            return erk.startswith("fdkaac") and (erk.endswith("_he") or erk.endswith("_hev2"))

        defect_rows = [r for r in decoder_results
                       if isinstance(r.get("conformance_snr_db"), (int, float))
                       and r["conformance_snr_db"] < CONFORMANCE_SNR_FLOOR_DB
                       and _is_pns_free(r.get("encoder_row_key"))]
        if defect_rows:
            f.write("### Conformance defects on PNS-free streams (< 60 dB)\n\n")
            f.write("| Scenario | Clip | Profile | Decoder | Encoder | SNR (dB) |\n")
            f.write("| :--- | :--- | :---: | :--- | :--- | ---: |\n")
            for r in defect_rows[:40]:
                f.write(f"| {r['scenario']} | `{r['filename']}` | {profile_label(r.get('profile', 'lc'))} "
                        f"| {r['tool']} | {r.get('encoder_row_key', 'n/a')} | {_fmt(r['conformance_snr_db'])} |\n")
            if len(defect_rows) > 40:
                f.write(f"| … {len(defect_rows) - 40} more | | | | | |\n")
            f.write("\n")

        f.write("*Streams encoded with PNS are expected to diverge from the ffmpeg reference decode "
                "(non-normative noise substitution); the leaderboard gates only PNS-free streams.*\n\n")

        # --- Section 5: muxer ---------------------------------------
        f.write("## 5. Container Manipulation Benchmark\n\n")
        f.write("| Tool | Operation | Mean (ms) | Std Dev (ms) |\n")
        f.write("| :--- | :--- | ---: | ---: |\n")
        for r in muxer_results:
            if r.get("note"):
                f.write(f"| {r['tool']} | {r['op']} | n/a | n/a ({r['note']}) |\n")
            else:
                f.write(f"| {r['tool']} | {r['op']} | {_fmt(r['mean_ms'])} | {_fmt(r['std_ms'])} |\n")
        f.write("\n")

        # --- Section 6: multichannel + robustness ------------------------
        f.write("## 6. Multichannel & Bitstream Robustness\n\n")
        f.write("### Gapless / alignment offset (vs source WAV)\n\n")
        f.write("| Container | Decoder | Streams | Offset == 0 | \\|Offset\\| <= 2 | Max \\|Offset\\| | Length delta == 0 |\n")
        f.write("| :---: | :--- | ---: | ---: | ---: | ---: | ---: |\n")

        by_container_decoder = defaultdict(list)
        for r in decoder_results:
            if not r.get("decode_valid") or r.get("gapless_offset_samples") is None:
                continue
            by_container_decoder[(r.get("container", "n/a"), r["row_key"])].append(r)

        for (container, rk), grp in sorted(by_container_decoder.items(),
                                            key=lambda kv: (kv[0][0], decoder_order.get(kv[0][1], 999))):
            offsets = [r["gapless_offset_samples"] for r in grp]
            n_zero = sum(1 for o in offsets if o == 0)
            n_close = sum(1 for o in offsets if abs(o) <= 2)
            max_abs = max(abs(o) for o in offsets)
            n_len_ok = sum(1 for r in grp if r.get("gapless_length_delta") == 0)
            f.write(f"| {container} | {_decoder_name(rk, grp[0])} | {len(grp)} | {n_zero} | {n_close} "
                    f"| {max_abs} | {n_len_ok} |\n")
        f.write("\n")

        bad_offset_rows = [r for r in decoder_results
                           if r.get("decode_valid") and r.get("gapless_offset_samples") is not None
                           and abs(r["gapless_offset_samples"]) > 2]
        if bad_offset_rows:
            f.write("#### Streams with |offset| > 2 samples\n\n")
            f.write("| Scenario | Clip | Encoder | Decoder | Offset (samples) | Length delta |\n")
            f.write("| :--- | :--- | :--- | :--- | ---: | ---: |\n")
            for r in bad_offset_rows[:40]:
                f.write(f"| {r['scenario']} | `{r['filename']}` | {r.get('encoder_row_key', 'n/a')} | {r['tool']} "
                        f"| {r['gapless_offset_samples']} | {_fmt(r.get('gapless_length_delta'), '{}')} |\n")
            if len(bad_offset_rows) > 40:
                f.write(f"| … {len(bad_offset_rows) - 40} more | | | | | |\n")
            f.write("\n")

        f.write("*fdkaac HE/HE-v2 M4A priming assumes the SBR delay is removed by the decoder, so spec decoders "
                "land 961 samples late on those streams by design.*\n\n")

        f.write("### Robustness under corrupted bitstreams (seed 42)\n\n")
        f.write("| Decoder | Decoded to end (exit 0) | Exited with error | Timeout | Runaway (>4× intact output) |\n")
        f.write("| :--- | ---: | ---: | ---: | ---: |\n")
        rob_by_tool = defaultdict(list)
        for r in robustness_results:
            rob_by_tool[r["row_key"]].append(r)
        for dec in decoders:
            rows = rob_by_tool.get(dec.tool_id, [])
            n_pass = sum(1 for r in rows if r.get("passed") and not r.get("runaway"))
            n_timeout = sum(1 for r in rows if r.get("timeout"))
            n_runaway = sum(1 for r in rows if r.get("runaway"))
            n_fail = sum(1 for r in rows if not r.get("passed") and not r.get("timeout") and not r.get("runaway"))
            f.write(f"| {dec.name} | {n_pass} | {n_fail} | {n_timeout} | {n_runaway} |\n")
        f.write("\n")
        f.write("*An error exit on a corrupted stream is acceptable; a timeout or runaway is not.*\n\n")

    print(f"Decoder report generated at: {output_path}")
