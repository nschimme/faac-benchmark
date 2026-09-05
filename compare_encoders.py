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

from utils import (get_binary_size, get_elf_section_sizes, decode_validate, get_ffmpeg_path,
                   ffmpeg_probe, get_scenario_sort_key, safe_run, find_linked_lib, is_faac_legacy,
                   corpus_dir, select_corpus_clips, scenario_channels, scenario_rate,
                   scenario_family, family_label, scenario_families, expand_scenario_list,
                   get_audio_es_bytes)
from config import SCENARIOS, CORPORA, FAMILY_ORDER, GATE_CLIPS, GATE_FALLBACK_N

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure SCRIPT_DIR and scripts directory are in sys.path
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)
scripts_dir = os.path.join(SCRIPT_DIR, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

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

PROFILE_LABELS = {"lc": "LC", "he": "HE-v1", "hev2": "HE-v2", "standard": "Standard"}

def profile_label(profile):
    return PROFILE_LABELS[profile]

def row_key(encoder):
    """Stable identity key for a (tool, profile) combination -- used for
    output filenames and cross-phase joins. Never used for display."""
    return f"{encoder.tool_id}_{encoder.profile}"

class Encoder:
    def __init__(self, name, binary_path, tool_id, profile, lib_name_substr=None, lib_override=None):
        self.name = name
        self.binary_path = binary_path
        self.tool_id = tool_id
        self.profile = profile
        # An explicit --*-lib path overrides both what we measure and what we
        # force the binary to actually load at run time (see get_run_env) --
        # otherwise the dynamic linker would silently keep resolving whatever
        # same-named system library sits on the default search path.
        self.lib_override = lib_override

        # Footprint is the codec *library* size, not the CLI/host binary size.
        # If the codec is linked dynamically, measure the shared library on disk;
        # otherwise (static linking) fall back to the binary itself.
        lib_path = lib_override or (find_linked_lib(binary_path, lib_name_substr) if lib_name_substr else None)

        measured_path = None
        if lib_path and not lib_override and is_system_library(lib_path):
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
        self.file_ext = ".m4a"
        self.text_size = sec_sizes.get("text", 0)
        self.rodata_size = sec_sizes.get("rodata", 0)
        self.bss_size = sec_sizes.get("bss", 0)
        self.data_size = sec_sizes.get("data", 0)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels):
        raise NotImplementedError

    def get_run_env(self):
        """Environment overrides needed so the binary actually loads
        self.lib_override instead of whatever the linker would resolve
        by default. No-op unless an explicit lib path was given."""
        if not self.lib_override:
            return {}
        env = dict(os.environ)
        abs_lib = os.path.abspath(self.lib_override)
        lib_dir = os.path.dirname(abs_lib)
        if sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_INSERT_LIBRARIES"] = abs_lib
        else:
            env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            env["LD_PRELOAD"] = (abs_lib + " " + env.get("LD_PRELOAD", "")).strip()
        return env

def use_he_aac(bitrate_kbps, channels, sample_rate):
    """
    Centralized heuristic for selecting HE-AAC vs LC-AAC.
    HE-AAC is optimal at low bitrates but has both a ceiling and a floor.
    It also generally requires a minimum sample rate (typically 32kHz+).
    Typical range for HE-AAC: 8kbps to 48kbps per channel. The floor is 8, not
    10, because 8 kbps/channel is HE-AAC's design floor and the whole point of
    the 32k_stereo_16k scenario; a 10 kbps floor here would have silently
    skipped every HE encoder on it and compared only LC entries.
    """
    if sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / channels
    return 8 <= bitrate_per_ch <= 48

def use_he_v2_aac(bitrate_kbps, channels, sample_rate):
    """
    Centralized heuristic for selecting HE-AAC v2 (Parametric Stereo).
    HE-v2 is specifically designed for low-bitrate stereo content.
    Typical range: 6kbps to 20kbps per channel (stereo only), sample_rate >= 32000.
    """
    if channels < 2 or sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / channels
    return 6 <= bitrate_per_ch <= 20

class FAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id, profile="lc", lib_override=None, legacy=False):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfaac", lib_override=lib_override)
        self.legacy = legacy

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.legacy:
            return [self.binary_path, "-w", "-b", str(bitrate_kbps), "--overwrite", "-o", output_path, input_path]
        object_type = "he-aac-v1" if self.profile == "he" else "lc"
        return [self.binary_path, "-b", str(bitrate_kbps), "--overwrite", "--object-type", object_type, "-o", output_path, input_path]

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
        if self.codec_name == "libfdk_aac":
            if self.profile == "he":
                cmd.extend(["-profile:a", "aac_he"])
            elif self.profile == "hev2":
                cmd.extend(["-profile:a", "aac_he_v2"])
        if self.codec_name == "aac" and self.supports_nmr:
            cmd.extend(["-aac_coder", "nmr"])

        cmd.extend(["-b:a", f"{bitrate_kbps}k"])
        cmd.extend(["-ac", str(channels), output_path])
        return cmd

class FDKAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id, profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        # fdkaac's -b takes bits/sec (no "k" suffix) and only applies in CBR
        # mode (-m 0, the default). Channel count comes from the input WAV's
        # own header -- fdkaac has no CLI flag for it.
        if self.profile == "hev2":
            profile = "29"
        elif self.profile == "he":
            profile = "5"
        else:
            profile = "2"
        cmd = [self.binary_path, "-p", profile, "-b", str(bitrate_kbps * 1000), "-m", "0"]
        cmd.extend(["-o", output_path, input_path])
        return cmd

class AACEncEncoder(Encoder):
    def __init__(self, name, binary_path, profile="lc"):
        super().__init__(name, binary_path, "aac_enc", profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.profile == "hev2":
            aot = "29"
        elif self.profile == "he":
            aot = "5"
        else:
            aot = "2"
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
        if self.profile == "hev2":
            codec = "aacp"
        elif self.profile == "he":
            codec = "aach"
        else:
            codec = "aac "
        # Use m4af format for M4A container
        return [self.binary_path, "-f", "m4af", "-d", codec, "-b", str(bitrate_kbps * 1000), "-q", "127", "-c", str(channels), input_path, output_path]

class OpusEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="opusenc", profile="standard", is_ffmpeg=False):
        lib_substr = None if not is_ffmpeg else "libopus"
        if not is_ffmpeg and not lib_substr:
            lib_substr = "libopus"
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=lib_substr)
        self.is_ffmpeg = is_ffmpeg
        self.file_ext = ".opus"

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.is_ffmpeg:
            return [self.binary_path, "-y", "-i", input_path, "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path]
        return [self.binary_path, "--bitrate", str(bitrate_kbps), input_path, output_path]

class LameEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="lame", profile="standard", is_ffmpeg=False):
        lib_substr = None if not is_ffmpeg else "libmp3lame"
        if not is_ffmpeg and not lib_substr:
            lib_substr = "libmp3lame"
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=lib_substr)
        self.is_ffmpeg = is_ffmpeg
        self.file_ext = ".mp3"

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.is_ffmpeg:
            return [self.binary_path, "-y", "-i", input_path, "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path]
        return [self.binary_path, "-b", str(bitrate_kbps), "-s", str(sample_rate / 1000.0), input_path, output_path]

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
        legacy = is_faac_legacy(faac_path, lib_override=args.faac_lib)
        encoders.append(FAACEncoder("FAAC", faac_path, "faac", profile="lc", lib_override=args.faac_lib, legacy=legacy))
        if not legacy:
            encoders.append(FAACEncoder("FAAC", faac_path, "faac", profile="he", lib_override=args.faac_lib, legacy=legacy))

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
                encoders.append(FFmpegEncoder("FDK-AAC (FFmpeg)", ffmpeg_path, "libfdk_aac", profile="hev2"))
            if "vo_aacenc" in res.stdout:
                encoders.append(FFmpegEncoder("VO-AAC (FFmpeg)", ffmpeg_path, "vo_aacenc"))
        except:
            pass

    # 3. Standalone FDKAAC
    fdkaac_path = args.fdkaac_bin or shutil.which("fdkaac")
    if fdkaac_path:
        encoders.append(FDKAACEncoder("fdkaac", fdkaac_path, "fdkaac", profile="lc"))
        encoders.append(FDKAACEncoder("fdkaac", fdkaac_path, "fdkaac", profile="he"))
        encoders.append(FDKAACEncoder("fdkaac", fdkaac_path, "fdkaac", profile="hev2"))

    # 3b. AAC-ENC (alternative FDK-AAC wrapper)
    aacenc_path = getattr(args, 'aac_enc_bin', None) or shutil.which("aac-enc")
    if aacenc_path:
        encoders.append(AACEncEncoder("aac-enc", aacenc_path, profile="lc"))
        encoders.append(AACEncEncoder("aac-enc", aacenc_path, profile="he"))
        encoders.append(AACEncEncoder("aac-enc", aacenc_path, profile="hev2"))

    # 3c. Falabaac (LC-only; no SBR/HE-AAC support)
    falabaac_path = getattr(args, 'falabaac_bin', None) or shutil.which("falabaac")
    if falabaac_path:
        encoders.append(FalabaacEncoder("falabaac", falabaac_path))

    # 4. AFConvert (macOS)
    afconvert_path = getattr(args, 'afconvert_bin', None) or shutil.which("afconvert")
    if afconvert_path:
        encoders.append(AFConvertEncoder("Apple AAC", afconvert_path, profile="lc"))
        encoders.append(AFConvertEncoder("Apple AAC", afconvert_path, profile="he"))
        encoders.append(AFConvertEncoder("Apple AAC", afconvert_path, profile="hev2"))

    # 5. Non-AAC Codecs (Opus, LAME) if --include-other-codecs is specified
    if getattr(args, 'include_other_codecs', False):
        # 5a. Opus
        opusenc_path = getattr(args, 'opusenc_bin', None) or shutil.which("opusenc")
        if opusenc_path:
            encoders.append(OpusEncoder("Opus", opusenc_path, tool_id="opusenc", is_ffmpeg=False))
        elif ffmpeg_path:
            try:
                res = subprocess.run([ffmpeg_path, "-encoders"], capture_output=True, text=True)
                if "libopus" in res.stdout or "opus" in res.stdout:
                    encoders.append(OpusEncoder("Opus (FFmpeg)", ffmpeg_path, tool_id="ffmpeg_opus", is_ffmpeg=True))
            except Exception:
                pass

        # 5b. LAME (MP3)
        lame_path = getattr(args, 'lame_bin', None) or shutil.which("lame")
        if lame_path:
            encoders.append(LameEncoder("LAME", lame_path, tool_id="lame", is_ffmpeg=False))
        elif ffmpeg_path:
            try:
                res = subprocess.run([ffmpeg_path, "-encoders"], capture_output=True, text=True)
                if "libmp3lame" in res.stdout:
                    encoders.append(LameEncoder("LAME (FFmpeg)", ffmpeg_path, tool_id="ffmpeg_lame", is_ffmpeg=True))
            except Exception:
                pass

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
    ext = getattr(encoder, "file_ext", ".m4a")
    output_filename = f"{row_key(encoder)}_{scenario_name}_{sample}{ext}".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    channels = scenario_channels(cfg)
    sample_rate = scenario_rate(cfg)
    cmd = encoder.get_encode_cmd(input_path, output_path, cfg["bitrate"], channels, sample_rate)

    try:
        t_start = time.perf_counter()
        res = subprocess.run(cmd, capture_output=True, check=False, env=encoder.get_run_env() or None)

        if res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)

        t_end = time.perf_counter()
        duration = t_end - t_start

        file_size = os.path.getsize(output_path)
        es_bytes = get_audio_es_bytes(output_path)

        # Calculate actual bitrate using pure audio elementary stream bytes
        actual_bitrate = None
        audio_duration = ffmpeg_probe(input_path)
        if audio_duration and audio_duration > 0:
            actual_bitrate = (es_bytes * 8) / (audio_duration * 1000)

        valid, decode_err = decode_validate(output_path)
        out_channels, out_rate = get_audio_info(output_path)
        exp_channels = scenario_channels(cfg)
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
    parser.add_argument("--opusenc-bin", help="Path to opusenc binary")
    parser.add_argument("--lame-bin", help="Path to lame binary")
    parser.add_argument("--include-other-codecs", action="store_true", help="Include non-AAC codecs (Opus, LAME)")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="comparison_results.json", help="Intermediate results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip transient fidelity (attack-centroid-shift) calculation")
    parser.add_argument("--skip-graphs", action="store_true", help="Skip generating Mermaid graph blocks in leaderboard")

    args = parser.parse_args()

    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(SCRIPT_DIR, "data", "external")
    output_dir = os.path.join(SCRIPT_DIR, "output", "comparison")
    os.makedirs(output_dir, exist_ok=True)

    encoders = detect_encoders(args)
    if not encoders:
        print("No encoders detected!")
        sys.exit(1)

    print(f"Detected encoders: {', '.join(f'{e.name} ({profile_label(e.profile)})' for e in encoders)}")

    all_results = []

    num_cpus = os.cpu_count() or 1

    scenario_list = list(SCENARIOS.keys())
    if args.scenarios:
        scenario_list = expand_scenario_list(args.scenarios)

    for scenario_name in scenario_list:
        if scenario_name not in SCENARIOS:
            print(f"Scenario {scenario_name} not found in config, skipping.")
            continue
        cfg = SCENARIOS[scenario_name]
        print(f"\n>>> Running Scenario: {scenario_name} ({cfg['bitrate']} kbps)")
        data_dir = corpus_dir(cfg, external_data_dir)
        if not os.path.exists(data_dir):
            print(f"Data directory {data_dir} not found, skipping.")
            continue

        # --gate selects from a curated list, so the corpus cap (which bounds
        # full runs) is skipped there; applying both would drop gate clips the
        # cap happened not to select.
        wavs = [f for f in os.listdir(data_dir) if f.endswith(".wav")]
        all_samples = sorted(wavs) if args.gate else select_corpus_clips(
            wavs, CORPORA.get(cfg["corpus"], {}))
        if args.gate:
            samples = gate_filter(scenario_name, all_samples)
        else:
            num_to_run = max(1, int(len(all_samples) * args.coverage / 100.0))
            step = len(all_samples) / num_to_run if num_to_run > 0 else 1
            samples = [all_samples[int(i * step)] for i in range(num_to_run)]

        print(f"Processing {len(samples)} samples...")

        channels = scenario_channels(cfg)
        sample_rate = scenario_rate(cfg)

        for encoder in encoders:
            is_faac = isinstance(encoder, FAACEncoder)
            if is_faac and encoder.profile == "he" and not use_he_aac(cfg["bitrate"], channels, sample_rate):
                print(f"  Skipping {encoder.name} for {scenario_name}: bitrate/rate outside HE-AAC v1 range.")
                continue
            if is_faac and encoder.profile == "hev2" and not use_he_v2_aac(cfg["bitrate"], channels, sample_rate):
                print(f"  Skipping {encoder.name} for {scenario_name}: bitrate/rate outside HE-AAC v2 range.")
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

            ext = os.path.splitext(res["aac_path"])[1] or ".m4a"
            key = f"res_{res['row_key']}_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "aac": f"{key}{ext}",
                "mos": None
            }
            shutil.copy(res["aac_path"], os.path.join(output_dir, f"{key}{ext}"))
            valid_count += 1

        if valid_count == 0:
            print("No valid AAC files to score for MOS.")
            return

        bridge_json = "bridge_results.json"
        with open(bridge_json, "w") as f:
            json.dump(bridge_data, f, indent=2)

        phase2_script = os.path.join(SCRIPT_DIR, "phase2_mos.py")
        cmd_phase2 = [
            sys.executable, phase2_script,
            bridge_json,
            output_dir,
            external_data_dir
        ]
        safe_run(cmd_phase2, capture_output=False, check=True)

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

            ext = os.path.splitext(res["aac_path"])[1] or ".m4a"
            key = f"res_{res['row_key']}_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "aac": f"{key}{ext}",
                "ic_err": None,
                "attack_centroid_ms": None
            }
            # Ensure files exist in output_dir
            target_path = os.path.join(output_dir, f"{key}{ext}")
            if not os.path.exists(target_path):
                shutil.copy(res["aac_path"], target_path)
            valid_count += 1

        if valid_count == 0:
            print("No valid AAC files to analyze for stereo/transient fidelity.")
            return

        bridge_json_stereo = "bridge_results_stereo.json"
        with open(bridge_json_stereo, "w") as f:
            json.dump(bridge_data, f, indent=2)

        phase3_script = os.path.join(SCRIPT_DIR, "phase3_stereo.py")
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
        safe_run(cmd_phase3, capture_output=False, check=True)

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
    generate_leaderboard(encoders, all_results, args.output, scenario_list, skip_graphs=args.skip_graphs)

def format_size(bytes_val):
    if bytes_val is None or bytes_val == 0:
        return "0 B"
    if bytes_val < 1024:
        return f"{bytes_val} B"
    return f"{bytes_val / 1024:.1f} KB"

def generate_leaderboard(encoders, results, output_path, scenario_list, skip_graphs=False):
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

    has_non_aac = any(e.profile == "standard" for e in encoders)
    title_str = "# Audio Encoder Leaderboard\n\n" if has_non_aac else "# AAC Encoder Leaderboard\n\n"

    with open(output_path, "w") as f:
        f.write(title_str)
        f.write("Quality scores are objective proxy estimates (Zimtohrli/ViSQOL), not blind ABX listening test results.\n\n")
        f.write("## Overall Rankings\n\n")
        # Overall MOS is a mean of per-scenario means, so the scenario set is
        # part of the number: adding or removing a family shifts every
        # encoder's absolute score even though nothing about the encoders
        # changed. Rankings stay valid (all encoders see the same set).
        f.write("> **Note**: Overall MOS averages the scenario set listed below, so absolute "
                "values are only comparable between leaderboards built from the same set of "
                "scenarios. Relative ranking is unaffected.\n\n")
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

        scenarios = sorted(scenario_list, key=get_scenario_sort_key)
        all_em_keys_sorted = sorted(overall.keys())

        # Group encoder keys by profile ("lc", "he", "hev2", "standard")
        profile_order = ["lc", "he", "hev2", "standard"]
        profile_keys = {p: [rk for rk in all_em_keys_sorted if overall[rk]["profile"] == p] for p in profile_order}

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

        chart_tools = sorted_tools[:10]  # Top 10 for clean bar display
        top_tools = chart_tools[:5]      # Top 5 for clean line display


        def make_progress_bar(val, max_val=1.0, width=8, lower_is_better=False):
            if val is None or max_val <= 0:
                return ""
            ratio = max(0.0, min(1.0, val / max_val))
            if lower_is_better:
                ratio = 1.0 - ratio
            filled = int(round(ratio * width))
            return " " + "█" * filled + "░" * (width - filled)

        def render_metric_tables(extract_val_fn, fmt_fn, lower_is_better=False, filter_scenarios=None, max_scale=None):
            scen_list = filter_scenarios if filter_scenarios is not None else scenarios
            for p in profile_order:
                # Filter out encoder keys that have no valid data for this scenario subset
                keys = [rk for rk in profile_keys[p] if any(extract_val_fn(rk, s) is not None for s in scen_list)]
                if not keys:
                    continue

                headers = [overall[rk]["tool"] for rk in keys]

                # Determine max_scale automatically if not explicitly provided
                table_max_scale = max_scale
                if table_max_scale is None:
                    vals = [extract_val_fn(rk, s) for rk in keys for s in scen_list if extract_val_fn(rk, s) is not None]
                    table_max_scale = max(vals) if vals else 1.0

                f.write(f"#### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(headers) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(headers)) + " |\n")

                for s in scen_list:
                    row_vals = [extract_val_fn(rk, s) for rk in keys]
                    valid_vals = [v for v in row_vals if v is not None]

                    best_val = None
                    if valid_vals:
                        best_val = min(valid_vals) if lower_is_better else max(valid_vals)

                    line = f"| {s} |"
                    for val in row_vals:
                        if val is None:
                            line += " N/A |"
                        else:
                            formatted = fmt_fn(val)
                            bar_str = make_progress_bar(val, table_max_scale, lower_is_better=lower_is_better)
                            is_best = (val == best_val)
                            line += f" **{formatted}**{bar_str} |" if is_best else f" {formatted}{bar_str} |"
                    f.write(line + "\n")

                f.write("\n")

        f.write("\n## Per-Scenario Breakdown & Visualizations\n\n")

        # 1-3. Quality per rate family.
        #
        # These sections used to be a hard-coded "Stereo Audio" / "Mono Speech"
        # pair, which worked only while the suite had exactly one sample rate
        # per channel count. The charts plot MOS against BITRATE, so mixing
        # rates into one chart draws a line through unrelated configurations --
        # 32k_stereo_48k and 48k_stereo_48k would both land on the "48k" tick.
        # One section per family keeps every curve meaningful.
        def family_chart(fam_scenarios, title, y_label, y_range, value_fn):
            """Emit one MOS-style xychart for a single family.

            Skipped below three points: a two-point line chart says less than
            the table underneath it.
            """
            if skip_graphs or not top_tools or len(fam_scenarios) < 3:
                return
            # The x-axis is bitrate, so two scenarios at the same bitrate (the
            # clean and VoIP-degraded 16 kHz corpora, say) would collide on one
            # tick and read as a single curve through unrelated content.
            bitrates = [SCENARIOS.get(sc, {}).get("bitrate") for sc in fam_scenarios]
            if len(set(bitrates)) != len(bitrates):
                return
            scen_labels = [f'"{SCENARIOS.get(sc, {}).get("bitrate", sc)}k"' for sc in fam_scenarios]
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write(f'    title "{title}"\n')
            f.write(f"    x-axis [{', '.join(scen_labels)}]\n")
            f.write(f'    y-axis "{y_label}" {y_range}\n')
            for t in top_tools:
                candidates = tool_row_keys[t]
                vals = []
                for sc in fam_scenarios:
                    rk = scenario_best_row_key(candidates, sc)
                    v = value_fn(rk, sc) if rk else None
                    vals.append(f"{v:.4f}" if v is not None else "0.0")
                f.write(f'    line "{t}" [{", ".join(vals)}]\n')
            f.write("```\n\n")

        avg_mos = (lambda rk, sc: stats[rk][sc]["mos_sum"] / stats[rk][sc]["mos_count"]
                   if stats[rk][sc]["mos_count"] > 0 else None)
        worst_mos = (lambda rk, sc: stats[rk][sc]["mos_min"]
                     if stats[rk][sc]["mos_count"] > 0 else None)
        stereo_fid = (lambda rk, sc: 1.0 - (stats[rk][sc]["ic_sum"] / stats[rk][sc]["ic_count"])
                      if stats[rk][sc]["ic_count"] > 0 else None)

        for fam in scenario_families(scenarios):
            fam_scenarios = sorted(
                [sc for sc in scenarios if scenario_family(sc) == fam],
                key=get_scenario_sort_key)
            if not fam_scenarios:
                continue
            label = family_label(fam)

            f.write(f"### {label} Quality Across Bitrates\n\n")
            family_chart(fam_scenarios,
                         f"{label} Quality across Bitrates (Average MOS)",
                         "MOS Score", "1.0 --> 5.0", avg_mos)

            f.write(f"<details><summary><b>View Detailed {label} Average & Worst MOS Tables</b></summary>\n\n")
            f.write(f"#### Per-Scenario Average MOS ({label})\n\n")
            render_metric_tables(avg_mos, lambda v: f"{v:.3f}",
                                 filter_scenarios=fam_scenarios, max_scale=5.0)
            f.write(f"#### Per-Scenario Worst MOS (Min Clip MOS - {label})\n\n")
            f.write("> **Note**: Minimum perceptual MOS score observed across any clip in the scenario. Highlights edge-case clip degradation.\n\n")
            render_metric_tables(worst_mos, lambda v: f"{v:.3f}",
                                 filter_scenarios=fam_scenarios, max_scale=5.0)
            f.write("</details>\n\n")

            # Stereo image fidelity is undefined for mono families.
            if scenario_channels(SCENARIOS.get(fam_scenarios[0], {})) < 2:
                continue
            f.write(f"### Stereo Image Fidelity ({label})\n\n")
            f.write("> **Note**: Measured as 1.0 - |Coherence(Ref) - Coherence(Deg)|. **Higher is truer** (closer to reference stereo image).\n\n")
            family_chart(fam_scenarios,
                         f"Stereo Image Fidelity across Bitrates - {label} (Higher is Better)",
                         "Stereo Fidelity", "0.0 --> 1.0", stereo_fid)
            f.write(f"<details><summary><b>View Detailed Stereo Fidelity Table ({label})</b></summary>\n\n")
            render_metric_tables(stereo_fid, lambda v: f"{v:.4f}",
                                 filter_scenarios=fam_scenarios, max_scale=1.0)
            f.write("</details>\n\n")

        # 4. Transient Fidelity
        f.write("### Transient Fidelity\n\n")
        f.write("> **Note**: Measured as 1 / (1 + mean |attack-centroid-shift| ms) across onsets. **Higher is truer** (attack timing closer to reference).\n\n")
        f.write("<details><summary><b>View Detailed Transient Fidelity Table</b></summary>\n\n")
        render_metric_tables(
            lambda rk, s: 1.0 / (1.0 + (stats[rk][s]["centroid_sum"] / stats[rk][s]["centroid_count"])) if stats[rk][s]["centroid_count"] > 0 else None,
            lambda v: f"{v:.4f}",
            max_scale=1.0
        )
        f.write("</details>\n\n")

        # 5. Bitrate Accuracy & BD-Rate Efficiency
        f.write("### Bitrate Accuracy (Error %)\n\n")
        f.write("> **Note**: Deviation from target bitrate calculated from pure elementary stream audio bytes. **Lower is Better**.\n\n")
        f.write("<details><summary><b>View Detailed Bitrate Accuracy Table</b></summary>\n\n")
        render_metric_tables(
            lambda rk, s: stats[rk][s]["br_err_sum"] / stats[rk][s]["br_err_count"] if stats[rk][s]["br_err_count"] > 0 else None,
            lambda v: f"{v:.1f}%",
            lower_is_better=True
        )
        f.write("</details>\n\n")

        # BD-Rate Analysis vs Baseline Encoder (FAAC if present, else first encoder)
        try:
            import bd_rate as bdr

            # Find reference baseline encoder key (prefer FAAC LC)
            base_key = next((rk for rk in all_row_keys if "faac" in rk), all_row_keys[0] if all_row_keys else None)
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

                    analysis = bdr.analyze(base_mat, cand_mat)
                    scored = [seg for seg in analysis.get("segments", []) if seg.get("stats")]
                    if scored:
                        avg_bd = sum(seg["stats"]["mean"] for seg in scored) / len(scored)
                        cand_obj = encoder_info.get(cand_key)
                        c_label = f"{cand_obj.name} ({profile_label(cand_obj.profile)})" if cand_obj else cand_key
                        bd_rows.append((c_label, avg_bd, len(scored)))

                if bd_rows:
                    f.write(f"| Candidate Encoder | Mean BD-Rate vs {base_tool_name} | Valid Ladders |\n")
                    f.write("| :--- | :---: | :---: |\n")
                    for c_label, avg_bd, n_ladders in sorted(bd_rows, key=lambda x: x[1]):
                        icon = "🚀" if avg_bd < -0.5 else "📉" if avg_bd > 0.5 else "🎯"
                        f.write(f"| {c_label} | **{avg_bd:+.2f}%** {icon} | {n_ladders} |\n")
                    f.write("\n")
                else:
                    f.write("_No valid BD-rate ladders found between baseline and candidate encoders._\n\n")
        except Exception as e:
            f.write(f"### BD-Rate Relative Efficiency\n\n_BD-rate evaluation skipped due to error: {e}_\n\n")

        # 6. Efficiency & Footprint
        f.write("### Encoder Efficiency & Footprint\n\n")
        if not skip_graphs and chart_tools:
            tool_labels = [f'"{t}"' for t in chart_tools]
            speed_vals = [f"{tool_overall[t]['avg_speed']:.1f}" for t in chart_tools]
            max_speed = max([tool_overall[t]['avg_speed'] for t in chart_tools] + [1.0])
            rom_vals = [f"{(tool_overall[t]['text_size'] + tool_overall[t]['rodata_size']) / 1024.0:.1f}" for t in chart_tools]
            max_rom = max([(tool_overall[t]['text_size'] + tool_overall[t]['rodata_size']) / 1024.0 for t in chart_tools] + [10.0])

            f.write("#### Encoding Speed (xRT)\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Average Encoding Speed (xRealtime, Higher is Better)"\n')
            f.write(f"    x-axis [{', '.join(tool_labels)}]\n")
            f.write(f'    y-axis "Speed (xRT)" 0 --> {int(max_speed * 1.25) + 1}\n')
            f.write(f"    bar [{', '.join(speed_vals)}]\n")
            f.write("```\n\n")

            f.write("#### Codec ROM (Flash) Size\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Codec Code + Read-Only Data Size (KB, Lower is Better)"\n')
            f.write(f"    x-axis [{', '.join(tool_labels)}]\n")
            f.write(f'    y-axis "ROM Size (KB)" 0 --> {int(max_rom * 1.25) + 1}\n')
            f.write(f"    bar [{', '.join(rom_vals)}]\n")
            f.write("```\n\n")

        f.write("<details><summary><b>View Detailed Per-Scenario Efficiency Table</b></summary>\n\n")
        render_metric_tables(
            lambda rk, s: stats[rk][s]["speed_sum"] / stats[rk][s]["speed_count"] if stats[rk][s]["speed_count"] > 0 else None,
            lambda v: f"{v:.1f}x"
        )
        f.write("</details>\n\n")

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
