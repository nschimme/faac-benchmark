"""
 * FAAC Benchmark Suite - Encoder Comparison & Leaderboard
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import sys
import json
import time
import argparse
import subprocess
import shutil
import concurrent.futures
import multiprocessing
from collections import defaultdict

from utils import safe_run, get_binary_size, decode_validate, wav_conv, get_ffmpeg_path, ffmpeg_probe
from config import SCENARIOS, GATE_CLIPS, GATE_FALLBACK_N

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

class Encoder:
    def __init__(self, name, binary_path, encoder_type):
        self.name = name
        self.binary_path = binary_path
        self.encoder_type = encoder_type
        self.size = get_binary_size(binary_path) if binary_path else 0

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps):
        raise NotImplementedError

class FAACEncoder(Encoder):
    def get_encode_cmd(self, input_path, output_path, bitrate_kbps):
        return [self.binary_path, "-b", str(bitrate_kbps), "-o", output_path, input_path]

class FFmpegEncoder(Encoder):
    def __init__(self, name, binary_path, codec_name):
        super().__init__(name, binary_path, "ffmpeg")
        self.codec_name = codec_name

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps):
        return [self.binary_path, "-y", "-i", input_path, "-c:a", self.codec_name, "-b:a", f"{bitrate_kbps}k", output_path]

class FDKAACEncoder(Encoder):
    def get_encode_cmd(self, input_path, output_path, bitrate_kbps):
        # fdkaac -b <bitrate> -o <out> <in>
        # Note: fdkaac expects bitrate in bps or with 'k' suffix
        return [self.binary_path, "-b", f"{bitrate_kbps}k", "-o", output_path, input_path]

def detect_encoders(args):
    encoders = []

    # 1. FAAC
    faac_path = args.faac_bin or shutil.which("faac")
    if faac_path:
        encoders.append(FAACEncoder("FAAC", faac_path, "faac"))

    # 2. FFmpeg Internal AAC
    ffmpeg_path = args.ffmpeg_bin or get_ffmpeg_path()
    if ffmpeg_path:
        encoders.append(FFmpegEncoder("FFmpeg AAC", ffmpeg_path, "aac"))

        # Check for libfdk_aac in ffmpeg
        try:
            res = subprocess.run([ffmpeg_path, "-encoders"], capture_output=True, text=True)
            if "libfdk_aac" in res.stdout:
                encoders.append(FFmpegEncoder("FDK-AAC (FFmpeg)", ffmpeg_path, "libfdk_aac"))
            if "vo_aacenc" in res.stdout:
                encoders.append(FFmpegEncoder("VO-AAC (FFmpeg)", ffmpeg_path, "vo_aacenc"))
        except:
            pass

    # 3. Standalone FDKAAC
    fdkaac_path = args.fdkaac_bin or shutil.which("fdkaac")
    if fdkaac_path:
        encoders.append(FDKAACEncoder("fdkaac", fdkaac_path, "fdkaac"))

    return encoders

def gate_filter(name, filtered_samples):
    available = set(filtered_samples)
    picked = [c for c in GATE_CLIPS.get(name, []) if c in available]
    if picked:
        return picked
    n = min(GATE_FALLBACK_N, len(filtered_samples))
    if n <= 0:
        return []
    step = len(filtered_samples) / n
    return [filtered_samples[int(i * step)] for i in range(n)]

def process_task(encoder, scenario_name, cfg, sample, data_dir, output_dir):
    input_path = os.path.join(data_dir, sample)
    output_filename = f"{encoder.name}_{scenario_name}_{sample}.aac".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    cmd = encoder.get_encode_cmd(input_path, output_path, cfg["bitrate"])

    try:
        t_start = time.perf_counter()
        subprocess.run(cmd, capture_output=True, check=True)
        t_end = time.perf_counter()
        duration = t_end - t_start

        file_size = os.path.getsize(output_path)

        # Calculate actual bitrate
        actual_bitrate = None
        audio_duration = ffmpeg_probe(input_path)
        if audio_duration:
            actual_bitrate = (file_size * 8) / (audio_duration * 1000)

        valid, decode_err = decode_validate(output_path)

        return {
            "encoder": encoder.name,
            "scenario": scenario_name,
            "filename": sample,
            "duration": duration,
            "audio_duration": audio_duration,
            "size": file_size,
            "actual_bitrate": actual_bitrate,
            "target_bitrate": cfg["bitrate"],
            "decode_valid": valid,
            "decode_error": decode_err,
            "aac_path": output_path
        }
    except Exception as e:
        print(f"Error encoding {sample} with {encoder.name}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Compare AAC encoders and generate a leaderboard.")
    parser.add_argument("--faac-bin", help="Path to faac binary")
    parser.add_argument("--fdkaac-bin", help="Path to fdkaac binary")
    parser.add_argument("--ffmpeg-bin", help="Path to ffmpeg binary")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="comparison_results.json", help="Intermediate results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--backend", default="auto", help="ViSQOL backend")

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(script_dir, "data", "external")
    output_dir = os.path.join(script_dir, "output", "comparison")
    os.makedirs(output_dir, exist_ok=True)

    encoders = detect_encoders(args)
    if not encoders:
        print("No encoders detected!")
        sys.exit(1)

    print(f"Detected encoders: {', '.join([e.name for e in encoders])}")

    all_results = []

    num_cpus = os.cpu_count() or 1

    scenario_list = SCENARIOS.keys()
    if args.scenarios:
        scenario_list = [s.strip() for s in args.scenarios.split(",")]

    for scenario_name in scenario_list:
        if scenario_name not in SCENARIOS:
            print(f"Scenario {scenario_name} not found in config, skipping.")
            continue
        cfg = SCENARIOS[scenario_name]
        print(f"\n>>> Running Scenario: {scenario_name} ({cfg['bitrate']} kbps)")
        data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
        data_dir = os.path.join(external_data_dir, data_subdir)
        if not os.path.exists(data_dir):
            print(f"Data directory {data_dir} not found, skipping.")
            continue

        all_samples = sorted([f for f in os.listdir(data_dir) if f.endswith(".wav")])
        if args.gate:
            samples = gate_filter(scenario_name, all_samples)
        else:
            num_to_run = max(1, int(len(all_samples) * args.coverage / 100.0))
            step = len(all_samples) / num_to_run if num_to_run > 0 else 1
            samples = [all_samples[int(i * step)] for i in range(num_to_run)]

        print(f"Processing {len(samples)} samples...")

        for encoder in encoders:
            print(f"  Encoding with {encoder.name}...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                futures = [executor.submit(process_task, encoder, scenario_name, cfg, sample, data_dir, output_dir) for sample in samples]
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res:
                        all_results.append(res)

    # Save intermediate results
    with open(args.results_json, "w") as f:
        json.dump(all_results, f, indent=2)

    # MOS Phase
    if not args.skip_mos:
        print("\n>>> Phase 2: Perceptual Quality (MOS)")
        # We need to bridge this with phase2_mos.py or implement it here.
        # Let's try to reuse phase2_mos.py by creating a temporary results.json in its format.

        bridge_data = {"matrix": {}}
        for i, res in enumerate(all_results):
            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "mos": None
            }
            # Copy file to output_dir so phase2_mos can find it via get_aac_path
            shutil.copy(res["aac_path"], os.path.join(output_dir, f"{key}.aac"))
            # Add a temporary property for phase2_mos to find the file
            # Actually, phase2_mos.py:get_aac_path searches aac_dir.

        bridge_json = "bridge_results.json"
        with open(bridge_json, "w") as f:
            json.dump(bridge_data, f, indent=2)

        phase2_script = os.path.join(script_dir, "phase2_mos.py")
        cmd_phase2 = [
            sys.executable, phase2_script,
            bridge_json,
            output_dir,
            external_data_dir,
            "--backend", args.backend
        ]
        subprocess.run(cmd_phase2, check=True)

        with open(bridge_json, "r") as f:
            updated_bridge = json.load(f)

        # Map MOS back
        for i, res in enumerate(all_results):
            key = f"res_{i}"
            res["mos"] = updated_bridge["matrix"][key].get("mos")

    # Stereo Phase
    if not args.skip_stereo:
        print("\n>>> Phase 3: Stereo Image Fidelity (inter-channel coherence)")
        # Re-use bridge_json for Phase 3
        bridge_data = {"matrix": {}}
        for i, res in enumerate(all_results):
            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "ic_err": None
            }

        bridge_json = "bridge_results_stereo.json"
        with open(bridge_json, "w") as f:
            json.dump(bridge_data, f, indent=2)

        phase3_script = os.path.join(script_dir, "phase3_stereo.py")
        cmd_phase3 = [
            sys.executable, phase3_script,
            bridge_json,
            output_dir,
            external_data_dir
        ]
        subprocess.run(cmd_phase3, check=True)

        with open(bridge_json, "r") as f:
            updated_bridge = json.load(f)

        # Map Stereo Error back
        for i, res in enumerate(all_results):
            key = f"res_{i}"
            res["ic_err"] = updated_bridge["matrix"][key].get("ic_err")

        if os.path.exists(bridge_json):
            os.remove(bridge_json)

    if os.path.exists("bridge_results.json"):
        os.remove("bridge_results.json")

    # Final leaderboard generation
    generate_leaderboard(encoders, all_results, args.output, scenario_list)

def generate_leaderboard(encoders, results, output_path, scenario_list):
    # Stats aggregation
    # encoder -> scenario -> metrics
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "ic_sum": 0, "ic_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "br_err_sum": 0, "br_err_count": 0
    }))

    encoder_info = {e.name: e for e in encoders}

    for res in results:
        e = res["encoder"]
        s = res["scenario"]

        if res.get("mos") is not None:
            stats[e][s]["mos_sum"] += res["mos"]
            stats[e][s]["mos_count"] += 1
            stats[e][s]["mos_min"] = min(stats[e][s]["mos_min"], res["mos"])

        if res.get("ic_err") is not None:
            stats[e][s]["ic_sum"] += res["ic_err"]
            stats[e][s]["ic_count"] += 1

        if res["duration"] > 0 and res["audio_duration"]:
            speed = res["audio_duration"] / res["duration"]
            stats[e][s]["speed_sum"] += speed
            stats[e][s]["speed_count"] += 1

        if res["actual_bitrate"] and res["target_bitrate"]:
            err = abs(res["actual_bitrate"] - res["target_bitrate"]) / res["target_bitrate"] * 100
            stats[e][s]["br_err_sum"] += err
            stats[e][s]["br_err_count"] += 1

    # Overall rankings
    overall = {}
    for e_name in encoder_info:
        e_mos = []
        e_speed = []
        e_br_err = []
        e_ic = []
        e_mos_min = 6.0

        has_data = False
        for s_name in SCENARIOS.keys():
            s_stats = stats[e_name][s_name]
            if s_stats["mos_count"] > 0:
                e_mos.append(s_stats["mos_sum"] / s_stats["mos_count"])
                e_mos_min = min(e_mos_min, s_stats["mos_min"])
                has_data = True
            if s_stats["speed_count"] > 0:
                e_speed.append(s_stats["speed_sum"] / s_stats["speed_count"])
                has_data = True
            if s_stats["br_err_count"] > 0:
                e_br_err.append(s_stats["br_err_sum"] / s_stats["br_err_count"])
                has_data = True
            if s_stats["ic_count"] > 0:
                e_ic.append(s_stats["ic_sum"] / s_stats["ic_count"])
                has_data = True

        if has_data:
            overall[e_name] = {
                "avg_mos": sum(e_mos) / len(e_mos) if e_mos else 0,
                "worst_mos": e_mos_min if e_mos else 0,
                "avg_ic": sum(e_ic) / len(e_ic) if e_ic else 0,
                "avg_speed": sum(e_speed) / len(e_speed) if e_speed else 0,
                "avg_br_err": sum(e_br_err) / len(e_br_err) if e_br_err else 0,
                "size_mb": encoder_info[e_name].size / (1024*1024)
            }

    # Sort by Avg MOS if available, else by name
    has_mos = any(o["avg_mos"] > 0 for o in overall.values())
    if has_mos:
        sorted_encoders = sorted(overall.keys(), key=lambda x: overall[x]["avg_mos"], reverse=True)
    else:
        sorted_encoders = sorted(overall.keys())

    with open(output_path, "w") as f:
        f.write("# AAC Encoder Leaderboard\n\n")
        f.write("## Overall Rankings\n\n")
        f.write("| Rank | Encoder | Avg MOS | Worst MOS | Stereo Δ | Speed (xRT) | Bitrate Error | Footprint |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_mos = max(o['avg_mos'] for o in overall.values()) if overall else 0
        best_speed = max(o['avg_speed'] for o in overall.values()) if overall else 0
        # For stereo error, lower is better. Filter out 0 (no data)
        valid_ic = [o['avg_ic'] for o in overall.values() if o['avg_ic'] > 0]
        best_ic = min(valid_ic) if valid_ic else 0

        for i, e_name in enumerate(sorted_encoders):
            o = overall[e_name]
            m_str = f"{o['avg_mos']:.3f}"
            if o['avg_mos'] == best_mos and best_mos > 0:
                m_str = f"**{m_str}**"

            ic_str = f"{o['avg_ic']:.4f}" if o['avg_ic'] > 0 else "N/A"
            if o['avg_ic'] == best_ic and best_ic > 0:
                ic_str = f"**{ic_str}**"

            s_str = f"{o['avg_speed']:.1f}x"
            if o['avg_speed'] == best_speed and best_speed > 0:
                s_str = f"**{s_str}**"

            f.write(f"| {i+1} | {e_name} | {m_str} | {o['worst_mos']:.3f} | {ic_str} | {s_str} | {o['avg_br_err']:.1f}% | {o['size_mb']:.1f} MB |\n")

        f.write("\n## Per-Scenario Quality (MOS)\n\n")
        scenarios = sorted(scenario_list)
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")

        best_per_scen_mos = {}
        for s in scenarios:
            best_mos_val = 0
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                if s_stats["mos_count"] > 0:
                    best_mos_val = max(best_mos_val, s_stats["mos_sum"] / s_stats["mos_count"])
            best_per_scen_mos[s] = best_mos_val

        for s in scenarios:
            line = f"| {s} |"
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                mos_val = s_stats["mos_sum"] / s_stats["mos_count"] if s_stats["mos_count"] > 0 else None
                if mos_val is not None:
                    m_str = f"{mos_val:.3f}"
                    if mos_val == best_per_scen_mos[s] and best_per_scen_mos[s] > 0:
                        m_str = f"**{m_str}**"
                    line += f" {m_str} |"
                else:
                    line += " N/A |"
            f.write(line + "\n")

        f.write("\n## Per-Scenario Stereo Image Fidelity (Coherence Error)\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")

        best_per_scen_ic = {}
        for s in scenarios:
            best_ic_val = float('inf')
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                if s_stats["ic_count"] > 0:
                    best_ic_val = min(best_ic_val, s_stats["ic_sum"] / s_stats["ic_count"])
            best_per_scen_ic[s] = best_ic_val if best_ic_val != float('inf') else 0

        for s in scenarios:
            line = f"| {s} |"
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                ic_val = s_stats["ic_sum"] / s_stats["ic_count"] if s_stats["ic_count"] > 0 else None
                if ic_val is not None:
                    ic_str = f"{ic_val:.4f}"
                    if ic_val == best_per_scen_ic[s] and best_per_scen_ic[s] > 0:
                        ic_str = f"**{ic_str}**"
                    line += f" {ic_str} |"
                else:
                    line += " N/A |"
            f.write(line + "\n")

        f.write("\n## Per-Scenario Bitrate Accuracy (Error %)\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")

        best_per_scen_err = {}
        for s in scenarios:
            best_err_val = float('inf')
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                if s_stats["br_err_count"] > 0:
                    best_err_val = min(best_err_val, s_stats["br_err_sum"] / s_stats["br_err_count"])
            best_per_scen_err[s] = best_err_val

        for s in scenarios:
            line = f"| {s} |"
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                err_val = s_stats["br_err_sum"] / s_stats["br_err_count"] if s_stats["br_err_count"] > 0 else None
                if err_val is not None:
                    e_str = f"{err_val:.1f}%"
                    if err_val == best_per_scen_err[s]:
                        e_str = f"**{e_str}**"
                    line += f" {e_str} |"
                else:
                    line += " N/A |"
            f.write(line + "\n")

        f.write("\n## Per-Scenario Efficiency (Speed xRT)\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")

        best_per_scen_speed = {}
        for s in scenarios:
            best_speed_val = 0
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                if s_stats["speed_count"] > 0:
                    best_speed_val = max(best_speed_val, s_stats["speed_sum"] / s_stats["speed_count"])
            best_per_scen_speed[s] = best_speed_val

        for s in scenarios:
            line = f"| {s} |"
            for e_name in sorted_encoders:
                s_stats = stats[e_name][s]
                speed_val = s_stats["speed_sum"] / s_stats["speed_count"] if s_stats["speed_count"] > 0 else None
                if speed_val is not None:
                    s_str = f"{speed_val:.1f}x"
                    if speed_val == best_per_scen_speed[s] and best_per_scen_speed[s] > 0:
                        s_str = f"**{s_str}**"
                    line += f" {s_str} |"
                else:
                    line += " N/A |"
            f.write(line + "\n")

    print(f"\nLeaderboard generated at: {output_path}")

if __name__ == "__main__":
    main()
