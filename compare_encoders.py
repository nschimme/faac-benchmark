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

from utils import get_binary_size, get_elf_section_sizes, decode_validate, get_ffmpeg_path, ffmpeg_probe, get_scenario_sort_key
from config import SCENARIOS, GATE_CLIPS, GATE_FALLBACK_N

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def find_linked_lib(binary_path, name_substr):
    """Resolve the on-disk path of a shared library linked into binary_path."""
    try:
        if sys.platform == "darwin":
            # macOS: use otool -L
            res = subprocess.run(["otool", "-L", binary_path], capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                line = line.strip()
                if name_substr in line:
                    # otool output: "\t/path/to/lib (compatibility...)"
                    lib_path = line.split(" ")[0]
                    if os.path.exists(lib_path):
                        return lib_path
        else:
            # Linux: use ldd
            res = subprocess.run(["ldd", binary_path], capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                if name_substr in line and "=>" in line:
                    lib_path = line.split("=>")[1].strip().split(" ")[0]
                    if lib_path and os.path.exists(lib_path):
                        return lib_path
    except Exception:
        pass
    return None

def is_system_library(path):
    """Checks if a library path belongs to a system directory."""
    if not path:
        return False
    if sys.platform == "darwin":
        # macOS system paths
        system_prefixes = ["/System/", "/usr/lib/libSystem", "/usr/lib/system/"]
        return any(path.startswith(prefix) for prefix in system_prefixes)
    # On Linux, we generally want to measure library size even if in /usr/lib
    return False

PROFILE_LABELS = {"lc": "LC", "he": "HE-v1"}

def profile_label(profile):
    return PROFILE_LABELS[profile]

def row_key(encoder):
    """Stable identity key for a (tool, profile) combination -- used for
    output filenames and cross-phase joins. Never used for display."""
    return f"{encoder.tool_id}_{encoder.profile}"

class Encoder:
    def __init__(self, name, binary_path, tool_id, profile, lib_name_substr=None):
        self.name = name
        self.binary_path = binary_path
        self.tool_id = tool_id
        self.profile = profile
        # Footprint is the codec *library* size, not the CLI/host binary size.
        # If the codec is linked dynamically, measure the shared library on disk;
        # otherwise (static linking) fall back to the binary itself.
        lib_path = find_linked_lib(binary_path, lib_name_substr) if lib_name_substr else None

        measured_path = None
        if lib_path and is_system_library(lib_path):
            self.size = 0  # System library, don't count towards footprint
        elif lib_path:
            self.size = get_binary_size(lib_path)
            measured_path = lib_path
        else:
            # Fallback to binary size if no library found, but ignore system binaries
            if is_system_library(binary_path):
                self.size = 0
            else:
                self.size = get_binary_size(binary_path) if binary_path else 0
                measured_path = binary_path if binary_path and not is_system_library(binary_path) else None

        sec_sizes = get_elf_section_sizes(measured_path) if measured_path else {"text": 0, "rodata": 0, "bss": 0, "data": 0}
        self.text_size = sec_sizes.get("text", 0)
        self.rodata_size = sec_sizes.get("rodata", 0)
        self.bss_size = sec_sizes.get("bss", 0)
        self.data_size = sec_sizes.get("data", 0)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels):
        raise NotImplementedError

def use_he_aac(bitrate_kbps, channels, sample_rate):
    """
    Centralized heuristic for selecting HE-AAC vs LC-AAC.
    HE-AAC is optimal at low bitrates but has both a ceiling and a floor.
    It also generally requires a minimum sample rate (typically 32kHz+).
    Typical range for HE-AAC: 10kbps to 48kbps per channel.
    """
    if sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / channels
    return 10 <= bitrate_per_ch <= 48

class FAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id, profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfaac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        object_type = "he-aac-v1" if self.profile == "he" else "lc"
        return [self.binary_path, "-w", "-b", str(bitrate_kbps), "--object-type", object_type, "-o", output_path, input_path]

class FFmpegEncoder(Encoder):
    def __init__(self, name, binary_path, codec_name, supports_nmr=False, profile="lc"):
        lib_name_substr = {
            "libfdk_aac": "libfdk-aac",
            "vo_aacenc": "vo-aacenc",
        }.get(codec_name)
        super().__init__(name, binary_path, f"ffmpeg_{codec_name}", profile, lib_name_substr=lib_name_substr)
        self.codec_name = codec_name
        self.supports_nmr = supports_nmr

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        cmd = [self.binary_path, "-y", "-i", input_path, "-c:a", self.codec_name]
        if self.codec_name == "libfdk_aac" and self.profile == "he":
            cmd.extend(["-profile:a", "aac_he"])
        if self.codec_name == "aac" and self.supports_nmr:
            cmd.extend(["-aac_coder", "nmr"])

        cmd.extend(["-b:a", f"{bitrate_kbps}k"])
        cmd.extend(["-ac", str(channels), output_path])
        return cmd

class FDKAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id, profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        profile = "5" if self.profile == "he" else "2"
        cmd = [self.binary_path, "-p", profile, "-b", f"{bitrate_kbps}k", "-m", str(channels)]
        cmd.extend(["-o", output_path, input_path])
        return cmd

class AACEncEncoder(Encoder):
    def __init__(self, name, binary_path, profile="lc"):
        super().__init__(name, binary_path, "aac_enc", profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        aot = "5" if self.profile == "he" else "2"
        # aac-enc -r <bitrate_bps> -t <aot> <in> <out>
        return [self.binary_path, "-r", str(bitrate_kbps * 1000), "-t", aot, input_path, output_path]

class FalabaacEncoder(Encoder):
    def __init__(self, name, binary_path, profile="lc"):
        super().__init__(name, binary_path, "falabaac", profile)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        # falabaac's own --help/source comment describe -b as per-channel, but its
        # runtime log ("total bitrate=X, per chn=X/channels") confirms the CLI
        # argument is actually the TOTAL bitrate -- it divides by channel count
        # itself. So pass bitrate_kbps through unchanged, like every other encoder here.
        return [self.binary_path, "-i", input_path, "-o", output_path, "-b", str(bitrate_kbps)]

class AFConvertEncoder(Encoder):
    def __init__(self, name, binary_path, profile="lc"):
        # AudioToolbox is the framework providing the AAC codec on macOS
        super().__init__(name, binary_path, "afconvert", profile, lib_name_substr="AudioToolbox")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        codec = "aach" if self.profile == "he" else "aac "
        # Use m4af format for M4A container
        return [self.binary_path, "-f", "m4af", "-d", codec, "-b", str(bitrate_kbps * 1000), "-q", "127", "-c", str(channels), input_path, output_path]

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
        encoders.append(FAACEncoder("FAAC", faac_path, "faac", profile="lc"))
        encoders.append(FAACEncoder("FAAC", faac_path, "faac", profile="he"))
        if args.faac_lib:
            sec_sizes = get_elf_section_sizes(args.faac_lib)
            for enc in encoders[-2:]:
                enc.size = get_binary_size(args.faac_lib)
                enc.text_size = sec_sizes.get("text", 0)
                enc.rodata_size = sec_sizes.get("rodata", 0)
                enc.bss_size = sec_sizes.get("bss", 0)
                enc.data_size = sec_sizes.get("data", 0)

    # 2. FFmpeg Internal AAC
    ffmpeg_path = args.ffmpeg_bin or get_ffmpeg_path()
    if ffmpeg_path:
        supports_nmr = False
        try:
            res = subprocess.run([ffmpeg_path, "-h", "encoder=aac"], capture_output=True, text=True)
            import re
            supports_nmr = bool(re.search(r"\bnmr\b", res.stdout))
        except Exception:
            pass
        encoders.append(FFmpegEncoder("FFmpeg AAC", ffmpeg_path, "aac", supports_nmr=supports_nmr))

        # Check for libfdk_aac in ffmpeg
        try:
            res = subprocess.run([ffmpeg_path, "-encoders"], capture_output=True, text=True)
            if "libfdk_aac" in res.stdout:
                encoders.append(FFmpegEncoder("FDK-AAC (FFmpeg)", ffmpeg_path, "libfdk_aac", profile="lc"))
                encoders.append(FFmpegEncoder("FDK-AAC (FFmpeg)", ffmpeg_path, "libfdk_aac", profile="he"))
            if "vo_aacenc" in res.stdout:
                encoders.append(FFmpegEncoder("VO-AAC (FFmpeg)", ffmpeg_path, "vo_aacenc"))
        except:
            pass

    # 3. Standalone FDKAAC
    fdkaac_path = args.fdkaac_bin or shutil.which("fdkaac")
    if fdkaac_path:
        encoders.append(FDKAACEncoder("fdkaac", fdkaac_path, "fdkaac", profile="lc"))
        encoders.append(FDKAACEncoder("fdkaac", fdkaac_path, "fdkaac", profile="he"))

    # 3b. AAC-ENC (alternative FDK-AAC wrapper)
    aacenc_path = getattr(args, 'aac_enc_bin', None) or shutil.which("aac-enc")
    if aacenc_path:
        encoders.append(AACEncEncoder("aac-enc", aacenc_path, profile="lc"))
        encoders.append(AACEncEncoder("aac-enc", aacenc_path, profile="he"))

    # 3c. Falabaac (LC-only; no SBR/HE-AAC support)
    falabaac_path = getattr(args, 'falabaac_bin', None) or shutil.which("falabaac")
    if falabaac_path:
        encoders.append(FalabaacEncoder("falabaac", falabaac_path))

    # 4. AFConvert (macOS)
    afconvert_path = getattr(args, 'afconvert_bin', None) or shutil.which("afconvert")
    if afconvert_path:
        encoders.append(AFConvertEncoder("Apple AAC", afconvert_path, profile="lc"))
        encoders.append(AFConvertEncoder("Apple AAC", afconvert_path, profile="he"))

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
    output_filename = f"{row_key(encoder)}_{scenario_name}_{sample}.m4a".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    channels = 1 if cfg["mode"] == "speech" else 2
    sample_rate = cfg.get("rate", 48000)
    cmd = encoder.get_encode_cmd(input_path, output_path, cfg["bitrate"], channels, sample_rate)

    try:
        t_start = time.perf_counter()
        res = subprocess.run(cmd, capture_output=True, check=False)

        if res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)

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
            "tool": encoder.name,
            "profile": encoder.profile,
            "row_key": row_key(encoder),
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
        print(f"Error encoding {sample} with {encoder.name} ({profile_label(encoder.profile)}): {e}")
        return {
            "tool": encoder.name,
            "profile": encoder.profile,
            "row_key": row_key(encoder),
            "scenario": scenario_name,
            "filename": sample,
            "duration": 0,
            "audio_duration": None,
            "size": 0,
            "actual_bitrate": None,
            "target_bitrate": cfg["bitrate"],
            "decode_valid": False,
            "decode_error": f"Encoding failed: {str(e)}",
            "aac_path": None
        }

def main():
    parser = argparse.ArgumentParser(description="Compare AAC encoders and generate a leaderboard.")
    parser.add_argument("--faac-bin", help="Path to faac binary")
    parser.add_argument("--faac-lib", help="Path to libfaac.so")
    parser.add_argument("--fdkaac-bin", help="Path to fdkaac binary")
    parser.add_argument("--aac-enc-bin", help="Path to aac-enc binary")
    parser.add_argument("--falabaac-bin", help="Path to falabaac binary")
    parser.add_argument("--ffmpeg-bin", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", help="Path to afconvert binary")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="comparison_results.json", help="Intermediate results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip transient fidelity (attack-centroid-shift) calculation")
    parser.add_argument("--backend", default="auto", help="Perceptual MOS backend")

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(script_dir, "data", "external")
    output_dir = os.path.join(script_dir, "output", "comparison")
    os.makedirs(output_dir, exist_ok=True)

    encoders = detect_encoders(args)
    if not encoders:
        print("No encoders detected!")
        sys.exit(1)

    print(f"Detected encoders: {', '.join(f'{e.name} ({profile_label(e.profile)})' for e in encoders)}")

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

        channels = 1 if cfg["mode"] == "speech" else 2
        sample_rate = cfg.get("rate", 48000)

        for encoder in encoders:
            if encoder.profile == "he" and not use_he_aac(cfg["bitrate"], channels, sample_rate):
                print(f"  Skipping {encoder.name} for {scenario_name}: bitrate/rate outside HE-AAC v1 range.")
                continue
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
        valid_count = 0
        for i, res in enumerate(all_results):
            if not res.get("aac_path") or not os.path.exists(res["aac_path"]):
                continue

            key = f"res_{res['row_key']}_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "aac": f"{key}.m4a",
                "mos": None
            }
            shutil.copy(res["aac_path"], os.path.join(output_dir, f"{key}.m4a"))
            valid_count += 1

        if valid_count == 0:
            print("No valid AAC files to score for MOS.")
            return

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
            key = f"res_{res['row_key']}_{i}"
            if key in updated_bridge["matrix"]:
                res["mos"] = updated_bridge["matrix"][key].get("mos")

    # Stereo Coherence + Transient Fidelity Phase (phase3_stereo.py computes
    # both from the same decode pass; see its module docstring).
    if not args.skip_stereo or not args.skip_transient:
        print("\n>>> Phase 3: Stereo Image Fidelity + Transient Fidelity")
        bridge_data = {"matrix": {}}
        valid_count = 0
        for i, res in enumerate(all_results):
            if not res.get("aac_path") or not os.path.exists(res["aac_path"]):
                continue

            key = f"res_{res['row_key']}_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "aac": f"{key}.m4a",
                "ic_err": None,
                "attack_centroid_ms": None
            }
            # Ensure files exist in output_dir
            target_path = os.path.join(output_dir, f"{key}.m4a")
            if not os.path.exists(target_path):
                shutil.copy(res["aac_path"], target_path)
            valid_count += 1

        if valid_count == 0:
            print("No valid AAC files to analyze for stereo/transient fidelity.")
            return

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
        if args.skip_stereo:
            cmd_phase3.append("--skip-stereo")
        if args.skip_transient:
            cmd_phase3.append("--skip-transient")
        subprocess.run(cmd_phase3, check=True)

        with open(bridge_json_stereo, "r") as f:
            updated_bridge = json.load(f)

        for i, res in enumerate(all_results):
            key = f"res_{res['row_key']}_{i}"
            if key in updated_bridge["matrix"]:
                res["ic_err"] = updated_bridge["matrix"][key].get("ic_err")
                res["attack_centroid_ms"] = updated_bridge["matrix"][key].get("attack_centroid_ms")

        if os.path.exists(bridge_json_stereo):
            os.remove(bridge_json_stereo)


    if os.path.exists("bridge_results.json"):
        os.remove("bridge_results.json")

    # Final leaderboard generation
    generate_leaderboard(encoders, all_results, args.output, scenario_list)

def format_size(bytes_val):
    if bytes_val is None or bytes_val == 0:
        return "0 B"
    if bytes_val < 1024:
        return f"{bytes_val} B"
    return f"{bytes_val / 1024:.1f} KB"

def generate_leaderboard(encoders, results, output_path, scenario_list):
    # Aggregation keyed by row_key (tool, profile). Every encoder is compared
    # at the same target bitrate (there is no cross-encoder VBR/quality mode
    # here -- each encoder's own quality knob isn't comparable to any other's,
    # see compare_encoders.py's Encoder.get_encode_cmd), so there is nothing
    # left to key on beyond the (tool, profile) combination itself.
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "ic_sum": 0, "ic_count": 0,
        "centroid_sum": 0, "centroid_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "br_err_sum": 0, "br_err_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    error_counts = defaultdict(int)

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
            stats[e][s]["mos_min"] = min(stats[e][s]["mos_min"], res["mos"])

        if res.get("ic_err") is not None:
            stats[e][s]["ic_sum"] += res["ic_err"]
            stats[e][s]["ic_count"] += 1

        # Transient fidelity: mean |attack-centroid-shift| in ms across this
        # clip's onsets (lower is better), unlike Stereo Fidelity above which
        # is reported as 1 - error (higher is better).
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

        stats[e][s]["total_count"] += 1
        if res.get("decode_valid"):
            stats[e][s]["valid_count"] += 1

    encoder_info = {row_key(e): e for e in encoders}
    all_row_keys = sorted(stats.keys())

    overall = {}
    for e_name in all_row_keys:
        e_mos, e_speed, e_br_err, e_ic, e_centroid = [], [], [], [], []
        e_mos_min = 6.0
        e_total = e_valid = 0

        has_data = False
        scenario_count = 0
        for s_name in scenario_list:
            s_stats = stats[e_name][s_name]
            e_total += s_stats["total_count"]
            e_valid += s_stats["valid_count"]
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
            if s_has_data:
                has_data = True
                scenario_count += 1

        if not has_data:
            continue

        enc_obj = encoder_info.get(e_name)
        overall[e_name] = {
            "tool": enc_obj.name if enc_obj else e_name,
            "profile": enc_obj.profile if enc_obj else "lc",
            "overall_mos": sum(e_mos) / len(e_mos) if e_mos else 0,
            "worst_mos": e_mos_min if e_mos else 0,
            "avg_ic": sum(e_ic) / len(e_ic) if e_ic else 0,
            "avg_centroid_ms": sum(e_centroid) / len(e_centroid) if e_centroid else 0,
            "avg_speed": sum(e_speed) / len(e_speed) if e_speed else 0,
            "avg_br_err": sum(e_br_err) / len(e_br_err) if e_br_err else 0,
            "text_size": enc_obj.text_size if enc_obj else 0,
            "rodata_size": enc_obj.rodata_size if enc_obj else 0,
            "valid_rate": (e_valid / e_total * 100) if e_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    # Overall Rankings roll up to one row per tool: for each scenario, use
    # whichever profile that tool actually produced the best (or only) result
    # for. This is deliberate -- a tool that supports HE-AAC gets its
    # low-bitrate efficiency counted as part of what it can achieve, the same
    # way a real deployment would pick the better profile automatically. Tools
    # without HE-AAC support are simply scored on the one profile they have.
    # The LC/HE split is still fully visible in the per-scenario tables below.
    tool_row_keys = defaultdict(list)
    for e in encoders:
        tool_row_keys[e.name].append(row_key(e))

    def scenario_best_row_key(candidates, s_name):
        available = [rk for rk in candidates if stats[rk][s_name]["total_count"] > 0]
        if not available:
            return None
        def sort_key(rk):
            st = stats[rk][s_name]
            mos_avg = st["mos_sum"] / st["mos_count"] if st["mos_count"] > 0 else None
            valid_rate = st["valid_count"] / st["total_count"] if st["total_count"] > 0 else 0
            return (mos_avg is not None, mos_avg if mos_avg is not None else 0, valid_rate)
        return max(available, key=sort_key)

    tool_overall = {}
    for tool_name, candidates in tool_row_keys.items():
        e_mos, e_speed, e_br_err, e_ic, e_centroid = [], [], [], [], []
        e_mos_min = 6.0
        has_data = False
        scenario_count = 0
        tool_total = tool_valid = 0
        for rk in candidates:
            for s_name in scenario_list:
                tool_total += stats[rk][s_name]["total_count"]
                tool_valid += stats[rk][s_name]["valid_count"]

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
            "text_size": enc_obj.text_size if enc_obj else 0,
            "rodata_size": enc_obj.rodata_size if enc_obj else 0,
            "valid_rate": (tool_valid / tool_total * 100) if tool_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    # Rank by worst-case MOS first, overall (mean) MOS as tiebreaker -- a strong
    # mean can hide a bad worst-case scenario, and worst-case robustness is what
    # matters most in practice (see Metric Legend below).
    has_mos = any(o["overall_mos"] > 0 for o in tool_overall.values())
    sorted_tools = sorted(tool_overall.keys(), key=lambda x: (tool_overall[x]["worst_mos"], tool_overall[x]["overall_mos"]), reverse=True) if has_mos else sorted(tool_overall.keys())

    with open(output_path, "w") as f:
        f.write("# AAC Encoder Leaderboard\n\n")
        f.write("Quality scores are objective proxy estimates (Zimtohrli/ViSQOL), not blind ABX listening test results.\n\n")
        f.write("## Overall Rankings\n\n")
        f.write("| Rank | Encoder | Status | Worst MOS | Overall MOS | Scenarios | Stereo Fidelity | Transient Fidelity | Speed (xRT) | Bitrate Error | ROM (Flash) |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

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
                candidates = tool_row_keys[tool_name]
                relevant_errors = {err: v for (rk, err), v in error_counts.items() if rk in candidates}
                top_err = max(relevant_errors, key=relevant_errors.get) if relevant_errors else "Err"
                status_str = f"❌ {100-o['valid_rate']:.1f}% ({top_err})"

            w_str = f"**{o['worst_mos']:.3f}**" if o['worst_mos'] == best_worst_mos and best_worst_mos > 0 else f"{o['worst_mos']:.3f}"
            m_str = f"**{o['overall_mos']:.3f}**" if o['overall_mos'] == best_mos and best_mos > 0 else f"{o['overall_mos']:.3f}"
            scenarios_str = f"{o['scenario_count']}/{o['scenario_total']}"

            ic_val = o['avg_ic']
            if ic_val > 0:
                fid = 1.0 - ic_val
                ic_str = f"**{fid:.4f}**" if fid == best_ic else f"{fid:.4f}"
            else:
                ic_str = "N/A"

            centroid_val = o['avg_centroid_ms']
            if centroid_val > 0:
                centroid_fid = 1.0 / (1.0 + centroid_val)
                centroid_str = f"**{centroid_fid:.4f}**" if centroid_fid == best_centroid_fid else f"{centroid_fid:.4f}"
            else:
                centroid_str = "N/A"

            s_str = f"**{o['avg_speed']:.1f}x**" if o['avg_speed'] == best_speed and best_speed > 0 else f"{o['avg_speed']:.1f}x"
            br_str = f"**{o['avg_br_err']:.1f}%**" if o['avg_br_err'] == best_br else f"{o['avg_br_err']:.1f}%"
            rom_str = format_size(o['text_size'] + o['rodata_size'])

            f.write(f"| {rank_str} | {tool_name} | {status_str} | {w_str} | {m_str} | {scenarios_str} | {ic_str} | {centroid_str} | {s_str} | {br_str} | {rom_str} |\n")

        # Per-Scenario Tables in Collapsible Section
        f.write("\n<details><summary><b>View Per-Scenario Quality, Stereo & Efficiency Breakdown</b></summary>\n")

        scenarios = sorted(scenario_list, key=get_scenario_sort_key)
        all_em_keys_sorted = sorted(overall.keys())
        tool_profile_counts = defaultdict(set)
        for em in all_em_keys_sorted:
            tool_profile_counts[overall[em]['tool']].add(overall[em]['profile'])
        em_headers = [
            overall[em]['tool'] if len(tool_profile_counts[overall[em]['tool']]) == 1
            else f"{overall[em]['tool']} {profile_label(overall[em]['profile'])}"
            for em in all_em_keys_sorted
        ]

        # 1. Perceptual Quality (MOS)
        f.write("\n### Per-Scenario Perceptual Quality (MOS)\n\n")
        f.write("| Scenario | " + " | ".join(em_headers) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(em_headers)) + " |\n")
        for s in scenarios:
            best_val = max(stats[em][s]["mos_sum"]/stats[em][s]["mos_count"] for em in all_em_keys_sorted if stats[em][s]["mos_count"] > 0) if any(stats[em][s]["mos_count"] > 0 for em in all_em_keys_sorted) else 0
            line = f"| {s} |"
            for em in all_em_keys_sorted:
                val = stats[em][s]["mos_sum"]/stats[em][s]["mos_count"] if stats[em][s]["mos_count"] > 0 else None
                line += f" **{val:.3f}** |" if val == best_val and best_val > 0 else (f" {val:.3f} |" if val is not None else " N/A |")
            f.write(line + "\n")

        # 2. Stereo
        f.write("\n### Per-Scenario Stereo Fidelity\n\n")
        f.write("> **Note**: Measured as 1.0 - |Coherence(Ref) - Coherence(Deg)|. **Higher is truer** (closer to reference stereo image).\n\n")
        f.write("| Scenario | " + " | ".join(em_headers) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(em_headers)) + " |\n")
        for s in scenarios:
            best_val = max(1.0 - (stats[em][s]["ic_sum"]/stats[em][s]["ic_count"]) for em in all_em_keys_sorted if stats[em][s]["ic_count"] > 0) if any(stats[em][s]["ic_count"] > 0 for em in all_em_keys_sorted) else -1.0
            line = f"| {s} |"
            for em in all_em_keys_sorted:
                val = stats[em][s]["ic_sum"]/stats[em][s]["ic_count"] if stats[em][s]["ic_count"] > 0 else None
                if val is not None:
                    fid = 1.0 - val
                    line += f" **{fid:.4f}** |" if fid == best_val and best_val != -1.0 else f" {fid:.4f} |"
                else:
                    line += " N/A |"
            f.write(line + "\n")

        # 2b. Transient Fidelity
        f.write("\n### Per-Scenario Transient Fidelity\n\n")
        f.write("> **Note**: Measured as 1 / (1 + mean |attack-centroid-shift| ms) across onsets. **Higher is truer** (attack timing closer to reference).\n\n")
        f.write("| Scenario | " + " | ".join(em_headers) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(em_headers)) + " |\n")
        for s in scenarios:
            fids = [1.0 / (1.0 + stats[em][s]["centroid_sum"]/stats[em][s]["centroid_count"]) for em in all_em_keys_sorted if stats[em][s]["centroid_count"] > 0]
            best_val = max(fids) if fids else None
            line = f"| {s} |"
            for em in all_em_keys_sorted:
                val = 1.0 / (1.0 + stats[em][s]["centroid_sum"]/stats[em][s]["centroid_count"]) if stats[em][s]["centroid_count"] > 0 else None
                if val is not None:
                    line += f" **{val:.4f}** |" if val == best_val else f" {val:.4f} |"
                else:
                    line += " N/A |"
            f.write(line + "\n")

        # 3. Bitrate Error
        f.write("\n### Per-Scenario Bitrate Accuracy (Error %)\n\n")
        f.write("| Scenario | " + " | ".join(em_headers) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(em_headers)) + " |\n")
        for s in scenarios:
            best_val = min(stats[em][s]["br_err_sum"]/stats[em][s]["br_err_count"] for em in all_em_keys_sorted if stats[em][s]["br_err_count"] > 0) if any(stats[em][s]["br_err_count"] > 0 for em in all_em_keys_sorted) else float('inf')
            line = f"| {s} |"
            for em in all_em_keys_sorted:
                val = stats[em][s]["br_err_sum"]/stats[em][s]["br_err_count"] if stats[em][s]["br_err_count"] > 0 else None
                line += f" **{val:.1f}%** |" if val == best_val and best_val != float('inf') else (f" {val:.1f}% |" if val is not None else " N/A |")
            f.write(line + "\n")

        # 4. Efficiency
        f.write("\n### Per-Scenario Efficiency (Speed xRT)\n\n")
        f.write("| Scenario | " + " | ".join(em_headers) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(em_headers)) + " |\n")
        for s in scenarios:
            best_val = max(stats[em][s]["speed_sum"]/stats[em][s]["speed_count"] for em in all_em_keys_sorted if stats[em][s]["speed_count"] > 0) if any(stats[em][s]["speed_count"] > 0 for em in all_em_keys_sorted) else 0
            line = f"| {s} |"
            for em in all_em_keys_sorted:
                val = stats[em][s]["speed_sum"]/stats[em][s]["speed_count"] if stats[em][s]["speed_count"] > 0 else None
                line += f" **{val:.1f}x** |" if val == best_val and best_val > 0 else (f" {val:.1f}x |" if val is not None else " N/A |")
            f.write(line + "\n")

        f.write("\n</details>\n")

        if error_counts:
            f.write("\n## Failure Analysis\n\n")
            f.write("| Encoder: Error Type | Occurrences |\n")
            f.write("| :--- | :---: |\n")
            for row_k, err in sorted(error_counts.keys(), key=lambda k: error_counts[k], reverse=True):
                enc_obj = encoder_info.get(row_k)
                label = f"{enc_obj.name} {profile_label(enc_obj.profile)}" if enc_obj else row_k
                f.write(f"| {label}: {err} | {error_counts[(row_k, err)]} |\n")

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

if __name__ == "__main__":
    main()
