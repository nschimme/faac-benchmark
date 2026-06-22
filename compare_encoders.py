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

from utils import get_binary_size, decode_validate, get_ffmpeg_path, ffmpeg_probe
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

def get_audio_info(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,sample_rate", "-of", "json", path]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        s = data["streams"][0]
        return int(s["channels"]), int(s["sample_rate"])
    except:
        return None, None

def detect_encoders(args):
    encoders = []

    # 1. FAAC
    faac_path = args.faac_bin or shutil.which("faac")
    if faac_path:
        encoders.append(FAACEncoder("FAAC", faac_path, "faac"))
        if args.faac_lib:
            encoders[-1].size += get_binary_size(args.faac_lib)

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
        out_channels, out_rate = get_audio_info(output_path)
        exp_channels = 1 if cfg["mode"] == "speech" else 2
        if valid and out_channels is not None and out_channels != exp_channels:
            valid = False
            decode_err = f"Channels mismatch: {out_channels} vs {exp_channels}"

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
    parser.add_argument("--faac-lib", help="Path to libfaac.so")
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

    # Perceptual Quality (MOS)
    if not args.skip_mos:
        print("\n>>> Phase 2: Perceptual Quality (MOS)")
        bridge_data = {"matrix": {}}
        for i, res in enumerate(all_results):
            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "mos": None
            }
            shutil.copy(res["aac_path"], os.path.join(output_dir, f"{key}.aac"))

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

        for i, res in enumerate(all_results):
            key = f"res_{i}"
            res["mos"] = updated_bridge["matrix"][key].get("mos")

    # Stereo Coherence Phase
    if not args.skip_stereo:
        print("\n>>> Phase 3: Stereo Image Fidelity (inter-channel coherence)")
        bridge_data = {"matrix": {}}
        for i, res in enumerate(all_results):
            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "ic_err": None
            }
            # Ensure files exist in output_dir
            if not os.path.exists(os.path.join(output_dir, f"{key}.aac")):
                shutil.copy(res["aac_path"], os.path.join(output_dir, f"{key}.aac"))

        bridge_json_stereo = "bridge_results_stereo.json"
        with open(bridge_json_stereo, "w") as f:
            json.dump(bridge_data, f, indent=2)

        phase3_script = os.path.join(script_dir, "phase3_stereo.py")
        cmd_phase3 = [
            sys.executable, phase3_script,
            bridge_json_stereo,
            output_dir,
            external_data_dir
        ]
        subprocess.run(cmd_phase3, check=True)

        with open(bridge_json_stereo, "r") as f:
            updated_bridge = json.load(f)

        for i, res in enumerate(all_results):
            key = f"res_{i}"
            res["ic_err"] = updated_bridge["matrix"][key].get("ic_err")

        if os.path.exists(bridge_json_stereo):
            os.remove(bridge_json_stereo)

    if os.path.exists("bridge_results.json"):
        os.remove("bridge_results.json")

    # Final leaderboard generation
    generate_leaderboard(encoders, all_results, args.output, scenario_list)

def generate_leaderboard(encoders, results, output_path, scenario_list):
    # Aggregation
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "ic_sum": 0, "ic_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "br_err_sum": 0, "br_err_count": 0,
        "valid_count": 0, "total_count": 0
    }))

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

        stats[e][s]["total_count"] += 1
        if res.get("decode_valid"):
            stats[e][s]["valid_count"] += 1

    # Overall rankings
    encoder_info = {e.name: e for e in encoders}
    overall = {}
    for e_name in encoder_info:
        e_mos, e_speed, e_br_err, e_ic = [], [], [], []
        e_mos_min = 6.0
        e_total = e_valid = 0

        has_data = False
        for s_name in scenario_list:
            s_stats = stats[e_name][s_name]
            e_total += s_stats["total_count"]
            e_valid += s_stats["valid_count"]
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
                "size_mb": encoder_info[e_name].size / (1024*1024),
                "valid_rate": (e_valid / e_total * 100) if e_total > 0 else 0
            }

    # Rank by Avg MOS if available
    has_mos = any(o["avg_mos"] > 0 for o in overall.values())
    sorted_encoders = sorted(overall.keys(), key=lambda x: overall[x]["avg_mos"], reverse=True) if has_mos else sorted(overall.keys())

    with open(output_path, "w") as f:
        f.write("# AAC Encoder Leaderboard\n\n")
        f.write("## Overall Rankings\n\n")
        f.write("| Rank | Encoder | Status | Avg MOS | Worst MOS | Stereo Coh. Error | Speed (xRT) | Bitrate Error | Footprint |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_mos = max(o['avg_mos'] for o in overall.values()) if overall else 0
        best_speed = max(o['avg_speed'] for o in overall.values()) if overall else 0

        has_ic = any(o['avg_ic'] > 0 for o in overall.values())
        best_ic = min(o['avg_ic'] for o in overall.values() if o['avg_ic'] > 0) if has_ic else None

        valid_br = [o['avg_br_err'] for o in overall.values()]
        best_br = min(valid_br) if valid_br else 0

        for i, e_name in enumerate(sorted_encoders):
            o = overall[e_name]
            rank_str = f"🏆 {i+1}" if i == 0 and o['avg_mos'] > 0 else f"{i+1}"
            status_str = "✅ OK" if o['valid_rate'] == 100 else f"❌ {100-o['valid_rate']:.1f}% Err"

            m_str = f"**{o['avg_mos']:.3f}**" if o['avg_mos'] == best_mos and best_mos > 0 else f"{o['avg_mos']:.3f}"

            ic_val = o['avg_ic']
            if ic_val > 0:
                ic_str = f"**{ic_val:.4f}**" if ic_val == best_ic else f"{ic_val:.4f}"
            else:
                ic_str = "N/A"

            s_str = f"**{o['avg_speed']:.1f}x**" if o['avg_speed'] == best_speed and best_speed > 0 else f"{o['avg_speed']:.1f}x"
            br_str = f"**{o['avg_br_err']:.1f}%**" if o['avg_br_err'] == best_br else f"{o['avg_br_err']:.1f}%"

            f.write(f"| {rank_str} | {e_name} | {status_str} | {m_str} | {o['worst_mos']:.3f} | {ic_str} | {s_str} | {br_str} | {o['size_mb']:.1f} MB |\n")

        # Per-Scenario Tables
        scenarios = sorted(scenario_list)

        # 1. Quality
        f.write("\n## Per-Scenario Quality (MOS)\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")
        for s in scenarios:
            best_val = max(stats[e][s]["mos_sum"]/stats[e][s]["mos_count"] for e in sorted_encoders if stats[e][s]["mos_count"] > 0) if any(stats[e][s]["mos_count"] > 0 for e in sorted_encoders) else 0
            line = f"| {s} |"
            for e in sorted_encoders:
                val = stats[e][s]["mos_sum"]/stats[e][s]["mos_count"] if stats[e][s]["mos_count"] > 0 else None
                line += f" **{val:.3f}** |" if val == best_val and best_val > 0 else (f" {val:.3f} |" if val is not None else " N/A |")
            f.write(line + "\n")

        # 2. Stereo
        f.write("\n## Per-Scenario Stereo Image Fidelity (Coherence Error)\n\n")
        f.write("> **Note**: Measured as |Coherence(Ref) - Coherence(Deg)|. **Lower is truer** (closer to reference stereo image).\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")
        for s in scenarios:
            best_val = min(stats[e][s]["ic_sum"]/stats[e][s]["ic_count"] for e in sorted_encoders if stats[e][s]["ic_count"] > 0) if any(stats[e][s]["ic_count"] > 0 for e in sorted_encoders) else float('inf')
            line = f"| {s} |"
            for e in sorted_encoders:
                val = stats[e][s]["ic_sum"]/stats[e][s]["ic_count"] if stats[e][s]["ic_count"] > 0 else None
                line += f" **{val:.4f}** |" if val == best_val and best_val != float('inf') else (f" {val:.4f} |" if val is not None else " N/A |")
            f.write(line + "\n")

        # 3. Bitrate Error
        f.write("\n## Per-Scenario Bitrate Accuracy (Error %)\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")
        for s in scenarios:
            best_val = min(stats[e][s]["br_err_sum"]/stats[e][s]["br_err_count"] for e in sorted_encoders if stats[e][s]["br_err_count"] > 0) if any(stats[e][s]["br_err_count"] > 0 for e in sorted_encoders) else float('inf')
            line = f"| {s} |"
            for e in sorted_encoders:
                val = stats[e][s]["br_err_sum"]/stats[e][s]["br_err_count"] if stats[e][s]["br_err_count"] > 0 else None
                line += f" **{val:.1f}%** |" if val == best_val and best_val != float('inf') else (f" {val:.1f}% |" if val is not None else " N/A |")
            f.write(line + "\n")

        # 4. Efficiency
        f.write("\n## Per-Scenario Efficiency (Speed xRT)\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")
        for s in scenarios:
            best_val = max(stats[e][s]["speed_sum"]/stats[e][s]["speed_count"] for e in sorted_encoders if stats[e][s]["speed_count"] > 0) if any(stats[e][s]["speed_count"] > 0 for e in sorted_encoders) else 0
            line = f"| {s} |"
            for e in sorted_encoders:
                val = stats[e][s]["speed_sum"]/stats[e][s]["speed_count"] if stats[e][s]["speed_count"] > 0 else None
                line += f" **{val:.1f}x** |" if val == best_val and best_val > 0 else (f" {val:.1f}x |" if val is not None else " N/A |")
            f.write(line + "\n")

        f.write("\n---\n")
        f.write("**Metric Legend**:\n")
        f.write("- **Avg MOS**: Perceptual quality (1-5, **Higher is Better**)\n")
        f.write("- **Stereo Coh. Error**: deviation from reference stereo image (**Lower is Better**)\n")
        f.write("- **Speed**: Encoding throughput (**Higher is Better**)\n")
        f.write("- **Bitrate Error**: Absolute deviation from target bitrate (**Lower is Better**)\n")
        f.write("- **Footprint**: Combined binary and library size (**Lower is Better**)\n")

    print(f"\nLeaderboard generated at: {output_path}")

if __name__ == "__main__":
    main()
