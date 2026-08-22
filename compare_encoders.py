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

class Encoder:
    def __init__(self, name, binary_path, encoder_type, lib_name_substr=None):
        self.name = name
        self.binary_path = binary_path
        self.encoder_type = encoder_type
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
    def __init__(self, name, binary_path, encoder_type):
        super().__init__(name, binary_path, encoder_type, lib_name_substr="libfaac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate, rate_control="abr", vbr_q=100):
        if rate_control == "vbr":
            return [self.binary_path, "-q", str(vbr_q), "-o", output_path, input_path]
        return [self.binary_path, "-b", str(bitrate_kbps), "-o", output_path, input_path]

class FFmpegEncoder(Encoder):
    def __init__(self, name, binary_path, codec_name, supports_nmr=False):
        lib_name_substr = {
            "libfdk_aac": "libfdk-aac",
            "vo_aacenc": "vo-aacenc",
        }.get(codec_name)
        super().__init__(name, binary_path, "ffmpeg", lib_name_substr=lib_name_substr)
        self.codec_name = codec_name
        self.supports_nmr = supports_nmr

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        cmd = [self.binary_path, "-y", "-i", input_path, "-c:a", self.codec_name]
        if self.codec_name == "libfdk_aac" and use_he_aac(bitrate_kbps, channels, sample_rate):
            cmd.extend(["-profile:a", "aac_he"])
        if self.codec_name == "aac" and self.supports_nmr:
            cmd.extend(["-aac_coder", "nmr"])
        cmd.extend(["-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path])
        return cmd

class FDKAACEncoder(Encoder):
    def __init__(self, name, binary_path, encoder_type):
        super().__init__(name, binary_path, encoder_type, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        profile = "5" if use_he_aac(bitrate_kbps, channels, sample_rate) else "2"
        # fdkaac might be picky about 'k' suffix vs bps depending on version
        return [self.binary_path, "-p", profile, "-b", f"{bitrate_kbps}k", "-m", str(channels), "-o", output_path, input_path]

class AACEncEncoder(Encoder):
    def __init__(self, name, binary_path):
        super().__init__(name, binary_path, "fdkaac", lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        aot = "5" if use_he_aac(bitrate_kbps, channels, sample_rate) else "2"
        # aac-enc -r <bitrate_bps> -t <aot> <in> <out>
        return [self.binary_path, "-r", str(bitrate_kbps * 1000), "-t", aot, input_path, output_path]

class AFConvertEncoder(Encoder):
    def __init__(self, name, binary_path):
        # AudioToolbox is the framework providing the AAC codec on macOS
        super().__init__(name, binary_path, "afconvert", lib_name_substr="AudioToolbox")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        codec = "aach" if use_he_aac(bitrate_kbps, channels, sample_rate) else "aac "
        # Use ADTS format to get a standard .aac file
        # Add -c to force channel count if needed
        return [self.binary_path, "-f", "adts", "-d", codec, "-b", str(bitrate_kbps * 1000), "-q", "127", "-c", str(channels), input_path, output_path]

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
            encoders[-1].size = get_binary_size(args.faac_lib)
            sec_sizes = get_elf_section_sizes(args.faac_lib)
            encoders[-1].text_size = sec_sizes.get("text", 0)
            encoders[-1].rodata_size = sec_sizes.get("rodata", 0)
            encoders[-1].bss_size = sec_sizes.get("bss", 0)
            encoders[-1].data_size = sec_sizes.get("data", 0)

    # 2. FFmpeg Internal AAC
    ffmpeg_path = args.ffmpeg_bin or get_ffmpeg_path()
    if ffmpeg_path:
        supports_nmr = False
        try:
            res = subprocess.run([ffmpeg_path, "-h", "encoder=aac"], capture_output=True, text=True)
            supports_nmr = "nmr" in res.stdout
        except Exception:
            pass
        encoders.append(FFmpegEncoder("FFmpeg AAC", ffmpeg_path, "aac", supports_nmr=supports_nmr))

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

    # 3b. AAC-ENC (alternative FDK-AAC wrapper)
    aacenc_path = getattr(args, 'aac_enc_bin', None) or shutil.which("aac-enc")
    if aacenc_path:
        encoders.append(AACEncEncoder("aac-enc", aacenc_path))

    # 4. AFConvert (macOS)
    afconvert_path = getattr(args, 'afconvert_bin', None) or shutil.which("afconvert")
    if afconvert_path:
        encoders.append(AFConvertEncoder("Apple AAC", afconvert_path))

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

def process_task(encoder, scenario_name, cfg, sample, data_dir, output_dir, rate_control="abr"):
    input_path = os.path.join(data_dir, sample)
    output_filename = f"{encoder.name}_{scenario_name}_{sample}.aac".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    channels = 1 if cfg["mode"] == "speech" else 2
    sample_rate = cfg.get("rate", 48000)
    vbr_q = cfg.get("vbr_q", 100)
    try:
        cmd = encoder.get_encode_cmd(input_path, output_path, cfg["bitrate"], channels, sample_rate, rate_control=rate_control, vbr_q=vbr_q)
    except TypeError:
        cmd = encoder.get_encode_cmd(input_path, output_path, cfg["bitrate"], channels, sample_rate)

    try:
        t_start = time.perf_counter()
        res = subprocess.run(cmd, capture_output=True, check=False)

        # Fallback for afconvert: if aach failed, try aac LC
        if res.returncode != 0 and isinstance(encoder, AFConvertEncoder) and "aach" in cmd:
            print(f"  [Fallback] aach failed for {sample}, trying aac LC...")
            cmd = [c if c != "aach" else "aac " for c in cmd]
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
        return {
            "encoder": encoder.name,
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
    parser.add_argument("--ffmpeg-bin", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", help="Path to afconvert binary")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="comparison_results.json", help="Intermediate results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--rate-control", choices=["abr", "vbr"], default="abr", help="Rate control mode (abr or vbr)")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--skip-zimtohrli", action="store_true", help="Skip Zimtohrli MOS calculation")
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
                futures = [executor.submit(process_task, encoder, scenario_name, cfg, sample, data_dir, output_dir, args.rate_control) for sample in samples]
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

            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "mos": None
            }
            shutil.copy(res["aac_path"], os.path.join(output_dir, f"{key}.aac"))
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
            key = f"res_{i}"
            if key in updated_bridge["matrix"]:
                res["mos"] = updated_bridge["matrix"][key].get("mos")

    # Stereo Coherence Phase
    if not args.skip_stereo:
        print("\n>>> Phase 3: Stereo Image Fidelity (inter-channel coherence)")
        bridge_data = {"matrix": {}}
        valid_count = 0
        for i, res in enumerate(all_results):
            if not res.get("aac_path") or not os.path.exists(res["aac_path"]):
                continue

            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "ic_err": None
            }
            # Ensure files exist in output_dir
            target_path = os.path.join(output_dir, f"{key}.aac")
            if not os.path.exists(target_path):
                shutil.copy(res["aac_path"], target_path)
            valid_count += 1

        if valid_count == 0:
            print("No valid AAC files to analyze for stereo fidelity.")
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
        subprocess.run(cmd_phase3, check=True)

        with open(bridge_json_stereo, "r") as f:
            updated_bridge = json.load(f)

        for i, res in enumerate(all_results):
            key = f"res_{i}"
            if key in updated_bridge["matrix"]:
                res["ic_err"] = updated_bridge["matrix"][key].get("ic_err")

        if os.path.exists(bridge_json_stereo):
            os.remove(bridge_json_stereo)

    # Phase 4 Zimtohrli MOS
    if not args.skip_zimtohrli:
        print("\n>>> Phase 4: Zimtohrli Perceptual Quality")
        bridge_data = {"matrix": {}}
        valid_count = 0
        for i, res in enumerate(all_results):
            if not res.get("aac_path") or not os.path.exists(res["aac_path"]):
                continue

            key = f"res_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "zimtohrli_mos": None
            }
            target_path = os.path.join(output_dir, f"{key}.aac")
            if not os.path.exists(target_path):
                shutil.copy(res["aac_path"], target_path)
            valid_count += 1

        if valid_count > 0:
            bridge_json_zim = "bridge_results_zim.json"
            with open(bridge_json_zim, "w") as f:
                json.dump(bridge_data, f, indent=2)

            phase4_script = os.path.join(script_dir, "phase4_zimtohrli.py")
            cmd_phase4 = [
                sys.executable, phase4_script,
                bridge_json_zim,
                output_dir,
                external_data_dir
            ]
            subprocess.run(cmd_phase4, check=True)

            with open(bridge_json_zim, "r") as f:
                updated_bridge = json.load(f)

            for i, res in enumerate(all_results):
                key = f"res_{i}"
                if key in updated_bridge["matrix"]:
                    res["zimtohrli_mos"] = updated_bridge["matrix"][key].get("zimtohrli_mos")

            if os.path.exists(bridge_json_zim):
                os.remove(bridge_json_zim)

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
    # Aggregation
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "z_mos_sum": 0, "z_mos_count": 0,
        "ic_sum": 0, "ic_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "br_err_sum": 0, "br_err_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    # Track top errors for troubleshooting
    error_counts = defaultdict(int)

    for res in results:
        e = res["encoder"]
        s = res["scenario"]

        if not res.get("decode_valid"):
            err_msg = res.get("decode_error") or "Unknown error"
            # Normalize error message for aggregation (take first line or short version)
            short_err = err_msg.split("\n")[0].split(":")[0].strip()
            error_counts[f"{e}: {short_err}"] += 1

        if res.get("mos") is not None:
            stats[e][s]["mos_sum"] += res["mos"]
            stats[e][s]["mos_count"] += 1
            stats[e][s]["mos_min"] = min(stats[e][s]["mos_min"], res["mos"])

        if res.get("zimtohrli_mos") is not None:
            stats[e][s]["z_mos_sum"] += res["zimtohrli_mos"]
            stats[e][s]["z_mos_count"] += 1

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
        e_mos, e_z_mos, e_speed, e_br_err, e_ic = [], [], [], [], []
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
            if s_stats["z_mos_count"] > 0:
                e_z_mos.append(s_stats["z_mos_sum"] / s_stats["z_mos_count"])
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
                "avg_z_mos": sum(e_z_mos) / len(e_z_mos) if e_z_mos else 0,
                "avg_ic": sum(e_ic) / len(e_ic) if e_ic else 0,
                "avg_speed": sum(e_speed) / len(e_speed) if e_speed else 0,
                "avg_br_err": sum(e_br_err) / len(e_br_err) if e_br_err else 0,
                "size_kb": encoder_info[e_name].size / 1024,
                "text_size": encoder_info[e_name].text_size,
                "rodata_size": encoder_info[e_name].rodata_size,
                "bss_size": encoder_info[e_name].bss_size,
                "data_size": encoder_info[e_name].data_size,
                "valid_rate": (e_valid / e_total * 100) if e_total > 0 else 0
            }

    # Rank by Avg MOS if available
    has_mos = any(o["avg_mos"] > 0 for o in overall.values())
    sorted_encoders = sorted(overall.keys(), key=lambda x: overall[x]["avg_mos"], reverse=True) if has_mos else sorted(overall.keys())

    has_zim = any(o.get("avg_z_mos", 0) > 0 for o in overall.values())

    with open(output_path, "w") as f:
        f.write("# AAC Encoder Leaderboard\n\n")
        f.write("## Overall Rankings\n\n")
        if has_zim:
            f.write("| Rank | Encoder | Status | ViSQOL MOS | Zimtohrli MOS | Worst MOS | Stereo Fidelity | Speed (xRT) | Bitrate Error | ROM (Flash) |\n")
            f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")
        else:
            f.write("| Rank | Encoder | Status | Avg MOS | Worst MOS | Stereo Fidelity | Speed (xRT) | Bitrate Error | ROM (Flash) |\n")
            f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_mos = max(o['avg_mos'] for o in overall.values()) if overall else 0
        best_speed = max(o['avg_speed'] for o in overall.values()) if overall else 0

        has_ic = any(o['avg_ic'] > 0 for o in overall.values())
        # Stereo Fidelity = 1.0 - error. Higher is better.
        best_ic = max(1.0 - o['avg_ic'] for o in overall.values() if o['avg_ic'] > 0) if has_ic else None

        valid_br = [o['avg_br_err'] for o in overall.values()]
        best_br = min(valid_br) if valid_br else 0

        for i, e_name in enumerate(sorted_encoders):
            o = overall[e_name]
            rank_str = f"🏆 {i+1}" if i == 0 and o['avg_mos'] > 0 else f"{i+1}"

            if o['valid_rate'] == 100:
                status_str = "OK"
            else:
                # Find most common error for this encoder
                relevant_errors = {k.split(": ", 1)[1]: v for k, v in error_counts.items() if k.startswith(f"{e_name}: ")}
                top_err = max(relevant_errors, key=relevant_errors.get) if relevant_errors else "Err"
                status_str = f"❌ {100-o['valid_rate']:.1f}% ({top_err})"

            m_str = f"**{o['avg_mos']:.3f}**" if o['avg_mos'] == best_mos and best_mos > 0 else f"{o['avg_mos']:.3f}"
            zm_str = f"{o['avg_z_mos']:.3f}" if o.get('avg_z_mos', 0) > 0 else "N/A"

            ic_val = o['avg_ic']
            if ic_val > 0:
                fid = 1.0 - ic_val
                ic_str = f"**{fid:.4f}**" if fid == best_ic else f"{fid:.4f}"
            else:
                ic_str = "N/A"

            s_str = f"**{o['avg_speed']:.1f}x**" if o['avg_speed'] == best_speed and best_speed > 0 else f"{o['avg_speed']:.1f}x"
            br_str = f"**{o['avg_br_err']:.1f}%**" if o['avg_br_err'] == best_br else f"{o['avg_br_err']:.1f}%"

            rom_str = format_size(o['text_size'] + o['rodata_size'])
            if has_zim:
                f.write(f"| {rank_str} | {e_name} | {status_str} | {m_str} | {zm_str} | {o['worst_mos']:.3f} | {ic_str} | {s_str} | {br_str} | {rom_str} |\n")
            else:
                f.write(f"| {rank_str} | {e_name} | {status_str} | {m_str} | {o['worst_mos']:.3f} | {ic_str} | {s_str} | {br_str} | {rom_str} |\n")

        # Per-Scenario Tables
        scenarios = sorted(scenario_list, key=get_scenario_sort_key)

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
        f.write("\n## Per-Scenario Stereo Fidelity\n\n")
        f.write("> **Note**: Measured as 1.0 - |Coherence(Ref) - Coherence(Deg)|. **Higher is truer** (closer to reference stereo image).\n\n")
        f.write("| Scenario | " + " | ".join(sorted_encoders) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_encoders)) + " |\n")
        for s in scenarios:
            best_val = max(1.0 - (stats[e][s]["ic_sum"]/stats[e][s]["ic_count"]) for e in sorted_encoders if stats[e][s]["ic_count"] > 0) if any(stats[e][s]["ic_count"] > 0 for e in sorted_encoders) else -1.0
            line = f"| {s} |"
            for e in sorted_encoders:
                val = stats[e][s]["ic_sum"]/stats[e][s]["ic_count"] if stats[e][s]["ic_count"] > 0 else None
                if val is not None:
                    fid = 1.0 - val
                    line += f" **{fid:.4f}** |" if fid == best_val and best_val != -1.0 else f" {fid:.4f} |"
                else:
                    line += " N/A |"
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

        if error_counts:
            f.write("\n## Failure Analysis\n\n")
            f.write("| Encoder: Error Type | Occurrences |\n")
            f.write("| :--- | :---: |\n")
            for err_key in sorted(error_counts.keys(), key=lambda x: error_counts[x], reverse=True):
                f.write(f"| {err_key} | {error_counts[err_key]} |\n")

        f.write("\n---\n")
        f.write("**Metric Legend**:\n")
        f.write("- **Avg MOS**: Perceptual quality (1-5, **Higher is Better**)\n")
        f.write("- **Stereo Fidelity**: Faithfulness of stereo image (0-1, **Higher is Better**)\n")
        f.write("- **Speed**: Encoding throughput (**Higher is Better**)\n")
        f.write("- **Bitrate Error**: Absolute deviation from target bitrate (**Lower is Better**)\n")
        f.write("- **ROM (Flash)**: Exact compiled executable code and read-only data size (.text + .rodata) inside the codec library/binary (**Lower is Better**)\n")

    print(f"\nLeaderboard generated at: {output_path}")

if __name__ == "__main__":
    main()
