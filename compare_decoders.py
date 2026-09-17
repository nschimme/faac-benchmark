"""
 * FAAC Benchmark Suite - Decoder Comparison & Leaderboard
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
import tempfile
import wave
import concurrent.futures
from collections import defaultdict

from utils import (get_binary_size, get_elf_section_sizes, decode_validate, get_ffmpeg_path,
                   get_faad_path, ffmpeg_probe, get_scenario_sort_key, safe_run, find_linked_lib,
                   resolve_wrapper_target, guess_lib_version_from_path,
                   corpus_dir, select_corpus_clips, scenario_channels, scenario_rate,
                   scenario_family, family_label, scenario_families, expand_scenario_list,
                   is_system_library, flatten_arg_list, probe_version,
                   make_unique_name_and_id, format_size, make_progress_bar, zoomed_y_range,
                   compute_snr, wav_conv, hosted_codec_ver)
from config import SCENARIOS, CORPORA, FAMILY_ORDER, GATE_CLIPS, GATE_FALLBACK_N

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)
scripts_dir = os.path.join(SCRIPT_DIR, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

def row_key(decoder):
    """Stable identity key for a decoder tool -- used for filenames and cross-phase joins."""
    return f"{decoder.tool_id}"

class Decoder:
    def __init__(self, name, binary_path, tool_id, lib_name_substr=None, lib_override=None):
        self.name = name
        self.binary_path = binary_path
        self.tool_id = tool_id
        self.lib_override = lib_override

        measure_bin = resolve_wrapper_target(binary_path) if binary_path else binary_path
        lib_path = lib_override or (find_linked_lib(measure_bin, lib_name_substr) if lib_name_substr and measure_bin else None)

        measured_path = None
        if lib_path and not lib_override and is_system_library(lib_path):
            self.size = 0
        elif lib_path:
            self.size = get_binary_size(lib_path)
            measured_path = lib_path
        else:
            if is_system_library(measure_bin):
                self.size = 0
            else:
                self.size = get_binary_size(measure_bin) if measure_bin else 0
                measured_path = measure_bin if measure_bin and not is_system_library(measure_bin) else None

        sec_sizes = get_elf_section_sizes(measured_path) if measured_path else {"text": 0, "rodata": 0, "bss": 0, "data": 0}
        self.text_size = sec_sizes.get("text", 0)
        self.rodata_size = sec_sizes.get("rodata", 0)
        self.bss_size = sec_sizes.get("bss", 0)
        self.data_size = sec_sizes.get("data", 0)

    def get_decode_cmd(self, input_path, output_path):
        raise NotImplementedError

    def get_run_env(self):
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

class FAADDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="faad", lib_override=None):
        super().__init__(name, binary_path, tool_id, lib_name_substr="libfaad", lib_override=lib_override)

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, "-q", "-o", output_path, input_path]

class FFmpegDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="ffmpeg_aac"):
        super().__init__(name, binary_path, tool_id, lib_name_substr=None)

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, "-y", "-i", input_path, "-sample_fmt", "s16", output_path]

class AFConvertDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="afconvert"):
        super().__init__(name, binary_path, tool_id, lib_name_substr="AudioToolbox")

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, "-f", "WAVE", "-d", "LEI16", input_path, output_path]

def detect_decoders(args):
    decoders = []
    existing_names = set()
    existing_ids = set()

    faad_bins = flatten_arg_list(getattr(args, "faad_bin", None))
    faad_libs = flatten_arg_list(getattr(args, "faad_lib", None))
    faad_vers = flatten_arg_list(getattr(args, "faad_bin_version", None))

    if not faad_bins:
        which_faad = get_faad_path()
        if which_faad:
            faad_bins = [which_faad]

    for idx, f_bin in enumerate(faad_bins):
        f_lib = faad_libs[idx] if idx < len(faad_libs) else (faad_libs[0] if faad_libs else None)
        ver = faad_vers[idx] if idx < len(faad_vers) else None
        if not ver:
            ver = probe_version(f_bin, ["-h", "--help", "-v", "--version"],
                                [r"FAAD2\s+v?(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("FAAD2", ver, "faad", existing_names, existing_ids)
        decoders.append(FAADDecoder(name, f_bin, tool_id=tool_id, lib_override=f_lib))

    ffmpeg_bins = flatten_arg_list(getattr(args, "ffmpeg_bin", None))
    if not ffmpeg_bins:
        which_ff = get_ffmpeg_path()
        if which_ff:
            ffmpeg_bins = [which_ff]

    for ff_bin in ffmpeg_bins:
        ver = probe_version(ff_bin, ["-version"], [r"ffmpeg\s+version\s+([^\s,]+)"])
        name, tool_id = make_unique_name_and_id("FFmpeg AAC", ver, "ffmpeg_aac", existing_names, existing_ids)
        decoders.append(FFmpegDecoder(name, ff_bin, tool_id=tool_id))

        try:
            res = subprocess.run([ff_bin, "-decoders"], capture_output=True, text=True)
            stdout = res.stdout or ""
            if "libfdk_aac" in stdout:
                name_fdk, id_fdk = make_unique_name_and_id("FFmpeg FDK-AAC", hosted_codec_ver(ff_bin, "libfdk-aac", ver), "ffmpeg_libfdk_aac", existing_names, existing_ids)
                decoders.append(FFmpegDecoder(name_fdk, ff_bin, tool_id=id_fdk))
        except Exception:
            pass

    afconvert_bins = flatten_arg_list(getattr(args, "afconvert_bin", None))
    if not afconvert_bins:
        which_afc = shutil.which("afconvert")
        if which_afc:
            afconvert_bins = [which_afc]

    for afc_bin in afconvert_bins:
        ver = probe_version(afc_bin, ["-h", "--help"], [r"afconvert\s+version\s+([^\s,]+)", r"version:?\s+([0-9.]+)"])
        name, tool_id = make_unique_name_and_id("Apple AudioToolbox", ver, "afconvert", existing_names, existing_ids)
        decoders.append(AFConvertDecoder(name, afc_bin, tool_id=tool_id))

    return decoders

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

def process_decode_task(decoder, ref_bitstream_path, ref_wav_path, scenario_name, cfg, sample, output_dir):
    output_filename = f"{row_key(decoder)}_{scenario_name}_{sample}.wav".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    cmd = decoder.get_decode_cmd(ref_bitstream_path, output_path)

    try:
        t_start = time.perf_counter()
        res = subprocess.run(cmd, capture_output=True, check=False, env=decoder.get_run_env() or None)
        t_end = time.perf_counter()
        duration = t_end - t_start

        if res.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            stderr_text = res.stderr.decode(errors="replace") if isinstance(res.stderr, bytes) else (res.stderr or "")
            err_tail = next((l for l in reversed(stderr_text.splitlines()) if l.strip()), f"exit code {res.returncode}")
            return {
                "tool": decoder.name,
                "row_key": row_key(decoder),
                "scenario": scenario_name,
                "filename": sample,
                "duration": 0,
                "audio_duration": None,
                "snr_db": None,
                "decode_valid": False,
                "decode_error": f"Decoding failed: {err_tail}",
                "wav_path": None
            }

        audio_duration = ffmpeg_probe(ref_bitstream_path)
        snr_db = compute_snr(ref_wav_path, output_path)

        return {
            "tool": decoder.name,
            "row_key": row_key(decoder),
            "scenario": scenario_name,
            "filename": sample,
            "duration": duration,
            "audio_duration": audio_duration,
            "snr_db": snr_db,
            "decode_valid": True,
            "decode_error": "",
            "wav_path": output_path
        }
    except Exception as e:
        return {
            "tool": decoder.name,
            "row_key": row_key(decoder),
            "scenario": scenario_name,
            "filename": sample,
            "duration": 0,
            "audio_duration": None,
            "snr_db": None,
            "decode_valid": False,
            "decode_error": str(e),
            "wav_path": None
        }

def prepare_reference_bitstreams(scenario_list, external_data_dir, ref_bitstream_dir, samples_by_scenario):
    """Encodes standard reference AAC bitstreams (.m4a) using ffmpeg for all scenarios."""
    os.makedirs(ref_bitstream_dir, exist_ok=True)
    ff_bin = get_ffmpeg_path()
    if not ff_bin:
        raise RuntimeError("FFmpeg is required to generate reference test bitstreams for decoder benchmarking.")

    ref_map = {}
    for scenario_name in scenario_list:
        cfg = SCENARIOS[scenario_name]
        data_dir = corpus_dir(cfg, external_data_dir)
        samples = samples_by_scenario.get(scenario_name, [])
        channels = scenario_channels(cfg)
        rate = scenario_rate(cfg)

        for sample in samples:
            input_wav = os.path.join(data_dir, sample)
            out_m4a = os.path.join(ref_bitstream_dir, f"ref_{scenario_name}_{sample}.m4a".replace(" ", "_"))

            if not os.path.exists(out_m4a):
                cmd = [ff_bin, "-y", "-i", input_wav, "-c:a", "aac", "-b:a", f"{cfg['bitrate']}k", "-ac", str(channels), "-ar", str(rate), out_m4a]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0 or not os.path.exists(out_m4a):
                    print(f"Warning: Failed to encode reference bitstream for {scenario_name}:{sample}: {res.stderr}")
                    continue
            ref_map[(scenario_name, sample)] = (input_wav, out_m4a)
    return ref_map

def main():
    parser = argparse.ArgumentParser(description="Compare AAC decoders and generate a leaderboard.")
    parser.add_argument("--mode", choices=["encoder", "decoder", "both"], default="both", help="Benchmarking mode: encoder, decoder, or both")
    parser.add_argument("--faac-bin", action="append", help="Path to faac binary")
    parser.add_argument("--faac-lib", action="append", help="Path to libfaac.so")
    parser.add_argument("--faac-bin-version", action="append", help="Explicit version label for --faac-bin")
    parser.add_argument("--fdkaac-bin", action="append", help="Path to fdkaac binary")
    parser.add_argument("--aac-enc-bin", action="append", help="Path to aac-enc binary")
    parser.add_argument("--falabaac-bin", action="append", help="Path to falabaac binary")
    parser.add_argument("--faad-bin", action="append", help="Path to faad binary")
    parser.add_argument("--faad-lib", action="append", help="Path to libfaad.so")
    parser.add_argument("--faad-bin-version", action="append", help="Explicit version label for --faad-bin")
    parser.add_argument("--ffmpeg-bin", action="append", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", action="append", help="Path to afconvert binary")
    parser.add_argument("--opusenc-bin", action="append", help="Path to opusenc binary")
    parser.add_argument("--lame-bin", action="append", help="Path to lame binary")
    parser.add_argument("--include-other-codecs", action="store_true", help="Include non-AAC codecs (Opus, LAME)")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="decoder_comparison_results.json", help="Intermediate decoder results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip transient fidelity calculation")
    parser.add_argument("--skip-graphs", action="store_true", help="Skip generating Mermaid graph blocks")
    parser.add_argument("--resume", action="store_true", help="Reload --results-json from previous run")

    args = parser.parse_args()

    if args.mode == "encoder":
        import compare_encoders
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--mode" and not a.startswith("encoder")]
        compare_encoders.main()
        return

    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(SCRIPT_DIR, "data", "external")
    output_dir = os.path.join(SCRIPT_DIR, "output", "decoder_comparison")
    ref_bitstream_dir = os.path.join(SCRIPT_DIR, "output", "ref_bitstreams")
    os.makedirs(output_dir, exist_ok=True)

    decoders = detect_decoders(args)
    if not decoders:
        print("No decoders detected!")
        sys.exit(1)

    print(f"Detected decoders: {', '.join(d.name for d in decoders)}")

    scenario_list = list(SCENARIOS.keys())
    if args.scenarios:
        scenario_list = expand_scenario_list(args.scenarios)

    samples_by_scenario = {}
    for scenario_name in scenario_list:
        if scenario_name not in SCENARIOS:
            continue
        cfg = SCENARIOS[scenario_name]
        data_dir = corpus_dir(cfg, external_data_dir)
        if not os.path.exists(data_dir):
            continue
        wavs = [f for f in os.listdir(data_dir) if f.endswith(".wav")]
        all_samples = sorted(wavs) if args.gate else select_corpus_clips(wavs, CORPORA.get(cfg["corpus"], {}))
        samples = gate_filter(scenario_name, all_samples) if args.gate else all_samples[:max(1, int(len(all_samples) * args.coverage / 100.0))]
        samples_by_scenario[scenario_name] = samples

    all_results = []
    num_cpus = os.cpu_count() or 1

    if args.resume and os.path.exists(args.results_json):
        print(f"==> --resume: reloading {args.results_json}")
        with open(args.results_json) as f:
            all_results = json.load(f)
    else:
        print("\n>>> Preparing Reference AAC Bitstreams...")
        ref_map = prepare_reference_bitstreams(scenario_list, external_data_dir, ref_bitstream_dir, samples_by_scenario)

        for scenario_name in scenario_list:
            if scenario_name not in SCENARIOS:
                continue
            cfg = SCENARIOS[scenario_name]
            samples = samples_by_scenario.get(scenario_name, [])
            if not samples:
                continue

            print(f"\n>>> Decoding Scenario: {scenario_name} ({cfg['bitrate']} kbps)")
            for decoder in decoders:
                print(f"  Decoding with {decoder.name}...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                    futures = []
                    for sample in samples:
                        if (scenario_name, sample) not in ref_map:
                            continue
                        ref_wav_path, ref_m4a_path = ref_map[(scenario_name, sample)]
                        futures.append(executor.submit(process_decode_task, decoder, ref_m4a_path, ref_wav_path, scenario_name, cfg, sample, output_dir))

                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            all_results.append(res)

        with open(args.results_json, "w") as f:
            json.dump(all_results, f, indent=2)

    # Perceptual MOS calculation
    if not args.skip_mos:
        print("\n>>> Phase 2: Perceptual Quality (MOS)")
        bridge_data = {"matrix": {}}
        valid_count = 0
        for i, res in enumerate(all_results):
            if not res.get("wav_path") or not os.path.exists(res["wav_path"]):
                continue
            key = f"res_{res['row_key']}_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "aac": res["wav_path"], # Pass decoded WAV
                "mos": None
            }
            valid_count += 1

        if valid_count > 0:
            bridge_json = "bridge_results_dec.json"
            with open(bridge_json, "w") as f:
                json.dump(bridge_data, f, indent=2)

            phase2_script = os.path.join(SCRIPT_DIR, "phase2_mos.py")
            cmd_phase2 = [sys.executable, phase2_script, bridge_json, output_dir, external_data_dir]
            safe_run(cmd_phase2, capture_output=False, check=True)

            with open(bridge_json, "r") as f:
                updated_bridge = json.load(f)

            for i, res in enumerate(all_results):
                key = f"res_{res['row_key']}_{i}"
                if key in updated_bridge["matrix"]:
                    res["mos"] = updated_bridge["matrix"][key].get("mos")

            if os.path.exists(bridge_json):
                os.remove(bridge_json)

    # Generate Leaderboard Report
    generate_decoder_leaderboard(decoders, all_results, args.output, scenario_list, skip_graphs=args.skip_graphs)

def generate_decoder_leaderboard(decoders, results, output_path, scenario_list, skip_graphs=False, encoders=None, encoder_results=None):
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "snr_sum": 0, "snr_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    for res in results:
        rk = res["row_key"]
        s = res["scenario"]
        stats[rk][s]["total_count"] += 1
        if res.get("decode_valid"):
            stats[rk][s]["valid_count"] += 1
            if res.get("mos") is not None:
                stats[rk][s]["mos_sum"] += res["mos"]
                stats[rk][s]["mos_count"] += 1
                stats[rk][s]["mos_min"] = min(stats[rk][s]["mos_min"], res["mos"])
            if res.get("snr_db") is not None and res["snr_db"] != float("inf"):
                stats[rk][s]["snr_sum"] += res["snr_db"]
                stats[rk][s]["snr_count"] += 1
            if res.get("duration", 0) > 0 and res.get("audio_duration"):
                stats[rk][s]["speed_sum"] += res["audio_duration"] / res["duration"]
                stats[rk][s]["speed_count"] += 1

    decoder_info = {row_key(d): d for d in decoders}
    overall = {}
    for rk, dec_obj in decoder_info.items():
        d_mos, d_speed, d_snr = [], [], []
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
            if st["speed_count"] > 0:
                d_speed.append(st["speed_sum"] / st["speed_count"])

        overall[rk] = {
            "tool": dec_obj.name,
            "worst_mos": d_worst_mos if d_mos else 0,
            "overall_mos": sum(d_mos) / len(d_mos) if d_mos else 0,
            "avg_snr_db": sum(d_snr) / len(d_snr) if d_snr else None,
            "avg_speed": sum(d_speed) / len(d_speed) if d_speed else 0,
            "text_size": dec_obj.text_size,
            "rodata_size": dec_obj.rodata_size,
            "valid_rate": (d_valid / d_total * 100) if d_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    # Objective ranking by Worst MOS first, then Overall MOS as tiebreaker
    sorted_rk = sorted(overall.keys(), key=lambda rk: (overall[rk]["worst_mos"], overall[rk]["overall_mos"]), reverse=True)

    with open(output_path, "w") as f:
        has_encoders = bool(encoders)
        if not has_encoders:
            f.write("# AAC Leaderboard\n\n")
            nav_links = ["[📊 Decoder Rankings](#-decoder-leaderboard)", "[📋 Decoder Scenarios](#per-scenario-decoder-breakdown)", "[⚙️ Decoder Efficiency](#decoder-efficiency--footprint)"]
            f.write(" | ".join(nav_links) + "\n\n---\n\n")

        f.write("## 🔊 Decoder Leaderboard\n\n")
        f.write("Objective evaluation of AAC decoders on Spec Conformance (SNR), Decoded Perceptual Quality (MOS), Speed, and Footprint.\n\n")

        f.write("### Overall Decoder Rankings\n\n")
        f.write("| Rank | Decoder | Status | Worst MOS | Overall MOS | Mean SNR | Speed (xRT) | ROM (Flash) |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_worst_mos = max(o["worst_mos"] for o in overall.values()) if overall else 0
        best_mos = max(o["overall_mos"] for o in overall.values()) if overall else 0
        best_speed = max(o["avg_speed"] for o in overall.values()) if overall else 0

        for i, rk in enumerate(sorted_rk):
            o = overall[rk]
            rank_str = f"🏆 {i+1}" if i == 0 and o["worst_mos"] > 0 else f"{i+1}"
            status_str = "OK" if o["valid_rate"] == 100 else f"❌ {100-o['valid_rate']:.1f}%"
            w_str = f"**{o['worst_mos']:.3f}**" if o["worst_mos"] == best_worst_mos and best_worst_mos > 0 else f"{o['worst_mos']:.3f}"
            m_str = f"**{o['overall_mos']:.3f}**" if o["overall_mos"] == best_mos and best_mos > 0 else f"{o['overall_mos']:.3f}"
            snr_str = f"{o['avg_snr_db']:.1f} dB" if o["avg_snr_db"] is not None else "Bit-exact"
            s_str = f"**{o['avg_speed']:.1f}x**" if o["avg_speed"] == best_speed and best_speed > 0 else f"{o['avg_speed']:.1f}x"
            rom_str = format_size(o["text_size"] + o["rodata_size"])

            f.write(f"| {rank_str} | {o['tool']} | {status_str} | {w_str} | {m_str} | {snr_str} | {s_str} | {rom_str} |\n")

        f.write("\n<details><summary><b>📊 View Per-Scenario Decoder Breakdowns</b></summary>\n\n")
        f.write("### Per-Scenario Decoder Breakdown\n\n")
        f.write("| Scenario | " + " | ".join(overall[rk]["tool"] for rk in sorted_rk) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_rk)) + " |\n")

        for s_name in sorted(scenario_list, key=get_scenario_sort_key):
            row_str = f"| {s_name} |"
            for rk in sorted_rk:
                st = stats[rk][s_name]
                if st["mos_count"] > 0:
                    avg_m = st["mos_sum"] / st["mos_count"]
                    row_str += f" {avg_m:.3f} |"
                else:
                    row_str += " N/A |"
            f.write(row_str + "\n")
        f.write("\n</details>\n\n")

        if not skip_graphs and sorted_rk:
            f.write("### Decoder Efficiency & Footprint\n\n")
            labels = [f'"{overall[rk]["tool"]}"' for rk in sorted_rk]
            speeds = [f"{overall[rk]['avg_speed']:.1f}" for rk in sorted_rk]
            max_s = max([overall[rk]['avg_speed'] for rk in sorted_rk] + [1.0])

            f.write("#### Decoding Speed (xRT)\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Average Decoding Throughput (xRealtime, Higher is Better)"\n')
            f.write(f"    x-axis [{', '.join(labels)}]\n")
            f.write(f'    y-axis "Speed (xRT)" 0 --> {int(max_s * 1.25) + 1}\n')
            f.write(f"    bar [{', '.join(speeds)}]\n")
            f.write("```\n\n")

    print(f"\nDecoder leaderboard generated at: {output_path}")

if __name__ == "__main__":
    main()
