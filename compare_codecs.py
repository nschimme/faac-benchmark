"""
 * FAAC Benchmark Suite - Codec Comparison & Leaderboard Orchestrator
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

from utils import (get_scenario_sort_key, safe_run, corpus_dir,
                   select_corpus_clips, scenario_channels, scenario_rate,
                   get_audio_es_bytes, measure_peak_ram, wav_conv,
                   decode_validate, ffmpeg_probe, expand_scenario_list,
                   get_cached_ref_wav)
import os
if sys.platform == "darwin":
    os.environ["NUMBA_THREADING_LAYER"] = "workqueue"
else:
    os.environ.setdefault("NUMBA_THREADING_LAYER", "omp")
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

def init_worker():
    """Initializes worker process with single-threaded constraints and CPU core affinity pinning."""
    # Set explicitly in the initializer (not just relied on via fork
    # inheriting the parent's already-mutated os.environ) so a pool worker
    # is single-threaded regardless of process start method, and every BLAS
    # call a worker makes -- or any subprocess it launches with env=None --
    # picks these up rather than oversubscribing the CPU count with 16
    # workers each spawning their own thread pool.
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"
    if hasattr(os, "sched_getaffinity") and hasattr(os, "sched_setaffinity"):
        try:
            pid = os.getpid()
            cpus = list(os.sched_getaffinity(0))
            if cpus:
                target_cpu = cpus[pid % len(cpus)]
                os.sched_setaffinity(0, {target_cpu})
        except Exception:
            pass

import soundfile as sf
import phase2_mos
import phase3_stereo
import transient
from config import SCENARIOS, CORPORA, FAMILY_ORDER, GATE_CLIPS, GATE_FALLBACK_N

# Re-export everything from codec_bench package for 100% backward compatibility
from codec_bench import (
    PROFILE_LABELS, profile_label, encoder_row_key, decoder_row_key, row_key,
    use_he_aac, use_he_v2_aac, Encoder, FAACEncoder, FFmpegEncoder,
    FDKAACEncoder, AACEncEncoder, FalabaacEncoder, AFConvertEncoder,
    OpusEncoder, LameEncoder, probe_faac_version, probe_encoder_capability,
    detect_encoders, Decoder, FAADDecoder, FFmpegDecoder, AFConvertDecoder,
    detect_decoders, process_decoder_task, process_decoder_robustness_task,
    CLIP_PEER_BUG_GAP, cell_peer_gap, generate_leaderboard, generate_decoder_leaderboard,
    generate_decoder_report, get_conformance_ref_wav, CONFORMANCE_SNR_FLOOR_DB
)
from muxer_bench import run_muxer_bench
from gate_check import evaluate_gate

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)
scripts_dir = os.path.join(SCRIPT_DIR, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)


def gate_filter(name, filtered_samples):
    """Filters dataset samples for --gate mode."""
    gate_list = GATE_CLIPS.get(name)
    if gate_list:
        gate_set = set(gate_list)
        return [f for f in filtered_samples if f in gate_set]
    return filtered_samples[:GATE_FALLBACK_N]


def get_audio_info(path):
    """Probes sample rate and channel count of a WAV file."""
    try:
        with wave.open(path, "rb") as w:
            return w.getframerate(), w.getnchannels()
    except Exception:
        return 44100, 2


def process_encoder_task(encoder, scenario_name, cfg, sample, data_dir, output_dir, skip_mos=False, skip_stereo=False, skip_transient=False, ref_cache_dir=None):
    """Executes single encoder task in worker thread with colocated metric evaluation."""
    input_path = os.path.join(data_dir, sample)
    sample_rate, channels = get_audio_info(input_path)
    bitrate_kbps = cfg["bitrate"]

    supported, reason = encoder.supports_scenario(bitrate_kbps, channels, sample_rate)
    if not supported:
        return None

    output_filename = f"{encoder_row_key(encoder)}_{scenario_name}_{sample}{encoder.file_ext}".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    cmd = encoder.get_encode_cmd(input_path, output_path, bitrate_kbps, channels, sample_rate)

    try:
        res, duration, peak_ram_kb = measure_peak_ram(cmd, env=encoder.get_run_env() or None)

        if res.returncode != 0:
            stderr_text = res.stderr.decode(errors="replace") if isinstance(res.stderr, bytes) else (res.stderr or "")
            stderr_clean = stderr_text.strip()
            if stderr_clean:
                stderr_tail = next((l for l in reversed(stderr_clean.splitlines()) if l.strip()), "")
                detail = f"exit code {res.returncode}: {stderr_tail}"
            elif res.returncode < 0:
                detail = f"Process terminated by signal {-res.returncode}"
            else:
                detail = f"exit code {res.returncode}"

            return {
                "tool": encoder.name,
                "row_key": encoder_row_key(encoder),
                "scenario": scenario_name,
                "filename": sample,
                "profile": encoder.profile,
                "duration": 0,
                "audio_duration": None,
                "peak_ram_kb": None,
                "size": 0,
                "actual_bitrate": None,
                "target_bitrate": bitrate_kbps,
                "decode_valid": False,
                "decode_error": f"Encoding failed: {detail}",
                "aac_path": None,
                "ref_path": input_path
            }

        # A variant that muxes its elementary stream through a second tool
        # (e.g. faam, to exercise a signalling form no encoder emits).
        mux_cmd = encoder.get_mux_cmd(output_path)
        if mux_cmd:
            es_path = output_path + ".es"
            res = subprocess.run(mux_cmd, capture_output=True)
            if os.path.exists(es_path):
                os.remove(es_path)
            if res.returncode != 0 or not os.path.exists(output_path):
                return {
                    "tool": encoder.name, "row_key": encoder_row_key(encoder),
                    "scenario": scenario_name, "filename": sample, "profile": encoder.profile,
                    "duration": 0, "audio_duration": None, "peak_ram_kb": None, "size": 0,
                    "actual_bitrate": None, "target_bitrate": bitrate_kbps, "decode_valid": False,
                    "decode_error": f"Muxing failed: exit code {res.returncode}",
                    "aac_path": None, "ref_path": input_path,
                }

        audio_duration = ffmpeg_probe(input_path)
        mos_val = None
        ic_err_val = None
        centroid_deltas = None

        with tempfile.TemporaryDirectory() as td:
            decoded_wav = os.path.join(td, "decoded.wav")
            # Decode once, natively, into the same disk cache the decoder
            # phase's conformance check reads later (keyed by output_path's
            # hash) instead of decoding this bitstream again per decoder
            # under test. The scenario-rate copy used for MOS/stereo/transient
            # below is then a cheap WAV->WAV resample of that cached decode.
            native_ref_wav = get_conformance_ref_wav(output_path, ref_cache_dir) if ref_cache_dir else None
            if native_ref_wav:
                decode_ok = wav_conv(native_ref_wav, decoded_wav, rate=sample_rate, channels=channels)
            else:
                decode_ok = wav_conv(output_path, decoded_wav, rate=sample_rate, channels=channels)
            if decode_ok:
                valid, decode_err = decode_validate(output_path)

                if valid:
                    ref_wav = get_cached_ref_wav(ref_cache_dir or td, input_path, sample_rate, channels) if ref_cache_dir else None
                    if not ref_wav:
                        ref_wav = os.path.join(td, "ref_conv.wav")
                        if not wav_conv(input_path, ref_wav, rate=sample_rate, channels=channels):
                            ref_wav = None

                    if ref_wav and os.path.exists(ref_wav):
                        if not skip_mos:
                            mos_val, _backend = phase2_mos.score_wav_pair(ref_wav, decoded_wav, mode_str=cfg.get("mode", "audio"), sample_rate=cfg.get("rate"))

                        if not skip_stereo:
                            ic_err_val = phase3_stereo.coherence_error(ref_wav, decoded_wav)

                        if not skip_transient:
                            try:
                                r_data, r_sr = sf.read(ref_wav, dtype="float32", always_2d=True)
                                c_data, c_sr = sf.read(decoded_wav, dtype="float32", always_2d=True)
                                centroid_deltas = transient.attack_centroid_deltas(r_data.mean(axis=1), c_data.mean(axis=1), r_sr)
                            except Exception:
                                pass
            else:
                valid, decode_err = False, "Decode failed (wav_conv)"

        es_bytes = get_audio_es_bytes(output_path) if valid else None
        actual_br = None
        if es_bytes and audio_duration and audio_duration > 0:
            actual_br = (es_bytes * 8.0 / audio_duration) / 1000.0

        return {
            "tool": encoder.name,
            "row_key": encoder_row_key(encoder),
            "scenario": scenario_name,
            "filename": sample,
            "profile": encoder.profile,
            "duration": duration,
            "audio_duration": audio_duration,
            "peak_ram_kb": peak_ram_kb,
            "size": os.path.getsize(output_path) if os.path.exists(output_path) else 0,
            "actual_bitrate": actual_br,
            "target_bitrate": bitrate_kbps,
            "decode_valid": valid,
            "decode_error": decode_err,
            "mos": mos_val,
            "ic_err": ic_err_val,
            "attack_centroid_ms": centroid_deltas,
            "aac_path": output_path if valid else None,
            "ref_path": input_path
        }

    except BaseException as e:
        detail = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            stderr_text = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            stderr_tail = next((l for l in reversed(stderr_text.splitlines()) if l.strip()), "")
            if stderr_tail:
                detail = f"exit code {e.returncode}: {stderr_tail}"
        return {
            "tool": encoder.name,
            "row_key": encoder_row_key(encoder),
            "scenario": scenario_name,
            "filename": sample,
            "profile": encoder.profile,
            "duration": 0,
            "audio_duration": None,
            "peak_ram_kb": None,
            "size": 0,
            "actual_bitrate": None,
            "target_bitrate": bitrate_kbps,
            "decode_valid": False,
            "decode_error": f"Encoding failed: {detail}",
            "aac_path": None,
            "ref_path": input_path
        }


def auto_detect_saved_json_files(explicit_results_json):
    """Auto-detects saved JSON result files in the current working directory and results/."""
    json_paths = []
    if explicit_results_json and os.path.exists(explicit_results_json):
        json_paths.append(explicit_results_json)

    search_dirs = [".", "results"]
    for d in search_dirs:
        if os.path.isdir(d):
            for fname in os.listdir(d):
                if fname.endswith(".json") and not fname.endswith(".decoders.json"):
                    full_p = os.path.join(d, fname)
                    if full_p not in json_paths:
                        json_paths.append(full_p)
    return json_paths


def load_all_saved_results(json_paths):
    """Loads and aggregates encoder, decoder, and robustness results from JSON files."""
    loaded_encoders = []
    loaded_decoders = []
    loaded_robustness = []

    for jp in json_paths:
        try:
            with open(jp, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    loaded_encoders.extend(data)
                elif isinstance(data, dict):
                    if "matrix" in data and isinstance(data["matrix"], dict):
                        for k, v in data["matrix"].items():
                            if isinstance(v, dict):
                                loaded_encoders.append(v)
                    elif "encoder_results" in data and isinstance(data["encoder_results"], list):
                        loaded_encoders.extend(data["encoder_results"])

            dec_jp = f"{jp}.decoders.json"
            if os.path.exists(dec_jp):
                with open(dec_jp, "r") as f:
                    dec_data = json.load(f)
                    if isinstance(dec_data, dict):
                        loaded_decoders.extend(dec_data.get("decoder_results", []))
                        loaded_robustness.extend(dec_data.get("decoder_robustness_results", []))
        except Exception:
            pass

    return loaded_encoders, loaded_decoders, loaded_robustness


def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite - Codec Comparison & Leaderboard Generator")
    parser.add_argument("saved_jsons", nargs="*", help="Optional positional path(s) to saved benchmark result JSON file(s)")
    parser.add_argument("--mode", choices=["encoder", "decoder", "both"], default="both", help="Benchmarking mode: encoder, decoder, or both (default: both)")
    parser.add_argument("--faac-bin", help="Path to faac binary (supports comma-separated list or multiple flags)")
    parser.add_argument("--faac-lib", help="Path to libfaac.so library override")
    parser.add_argument("--faac-bin-version", help="Explicit version string for faac binary")
    parser.add_argument("--fdkaac-bin", action="append", help="Path to fdkaac binary")
    parser.add_argument("--aac-enc-bin", action="append", help="Path to aac-enc binary")
    parser.add_argument("--falabaac-bin", action="append", help="Path to falabaac binary")
    parser.add_argument("--faad-bin", action="append", help="Path to faad binary")
    parser.add_argument("--faad-lib", action="append", help="Path to libfaad library override")
    parser.add_argument("--faad-bin-version", action="append", help="Explicit version string for faad binary")
    parser.add_argument("--helix-bin", action="append", help="Path to Helix AAC decoder binary")
    parser.add_argument("--fdkdec-bin", action="append", help="Path to fdkdec (libfdk-aac) decoder binary")
    parser.add_argument("--ffmpeg-bin", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", help="Path to afconvert binary (macOS)")
    parser.add_argument("--opusenc-bin", help="Path to opusenc binary")
    parser.add_argument("--lame-bin", help="Path to lame binary")
    parser.add_argument("--include-other-codecs", action="store_true", help="Include non-AAC encoders (Opus, LAME MP3)")
    parser.add_argument("--include-pns-off", "--pns-off", action="store_true", help="Include PNS-off encoder variations")
    parser.add_argument("--include-adts", "--adts-variations", action="store_true", help="Include ADTS container encoder variations")
    parser.add_argument("--include-faam-variations", action="store_true", help="Include faam SBR/PS signaling encoder variations")
    parser.add_argument("--include-encoder-variations", action="store_true", help="Include all non-standard encoder variations (PNS-off, ADTS, faam signaling)")
    parser.add_argument("--output", "-o", default="leaderboard.md", help="Output Markdown report path (default: leaderboard.md)")
    parser.add_argument("--results-json", default="comparison_results.json", help="Path to save raw benchmark results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use fast fixed gate clip subset")
    parser.add_argument("--coverage", type=int, default=100, help="Percentage of dataset to cover (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip perceptual MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip inter-channel coherence calculation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip attack centroid shift calculation")
    parser.add_argument("--skip-graphs", action="store_true", help="Skip generating Mermaid xychart-beta plots")
    parser.add_argument("--resume", action="store_true", help="Reuse existing comparison_results.json if available")
    parser.add_argument("--iterations", type=int, default=1,
                        help="Repeat each decode this many times (output discarded) for mean/std latency, xRT, MB/s (default: 1 = single timed decode only)")
    parser.add_argument("--faam-bin", help="Path to faam binary, for --muxer-bench")
    parser.add_argument("--muxer-bench", action="store_true",
                        help="Benchmark faam vs ffmpeg -c:a copy vs MP4Box on the largest ADTS stream from this run")
    parser.add_argument("--decoder-report", help="Write a decoder/muxer Markdown report (see docs/) to this path")
    parser.add_argument("--keep-decodes", action="store_true",
                        help="Keep each decoder's decoded WAV on disk instead of deleting it once its metrics are computed")

    args = parser.parse_args()

    if args.gate:
        args.iterations = max(args.iterations, 3)

    run_encoders = args.mode in ("encoder", "both")
    run_decoders = args.mode in ("decoder", "both")

    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(SCRIPT_DIR, "data", "external")
    output_dir = os.path.join(SCRIPT_DIR, "output", "compare_codecs")
    os.makedirs(output_dir, exist_ok=True)

    if args.scenarios:
        scenario_list = expand_scenario_list(args.scenarios)
    else:
        scenario_list = sorted(SCENARIOS.keys(), key=get_scenario_sort_key)

    num_cpus = max(1, os.cpu_count() or 1)
    encoder_results = []
    encoders = []

    if run_encoders:
        encoders = detect_encoders(args)
        if not encoders:
            print("No encoders detected!")
            if not run_decoders:
                sys.exit(1)
        else:
            print(f"Detected encoders ({len(encoders)} variants): {', '.join(f'{e.name} ({profile_label(e.profile)})' for e in encoders)}")

        # Auto-detect and merge saved runs
        candidate_jsons = args.saved_jsons if args.saved_jsons else auto_detect_saved_json_files(args.results_json)
        loaded_enc, loaded_dec, loaded_rob = load_all_saved_results(candidate_jsons)
        if loaded_enc:
            print(f"==> Auto-detected and loaded {len(loaded_enc)} encoder entries from: {', '.join(candidate_jsons)}")
            encoder_results = loaded_enc

        # Identify missing encoder tasks using smart reuse (encoder_version, scenario, filename)
        existing_keys = {(r.get("tool"), r.get("scenario"), r.get("filename")) for r in encoder_results if r.get("decode_valid") is not None}

        total_tasks = 0
        tasks = []
        for s_name in scenario_list:
            cfg = SCENARIOS[s_name]
            d_dir = corpus_dir(cfg, external_data_dir)
            if not os.path.exists(d_dir):
                continue

            corpus = CORPORA.get(cfg["corpus"])
            samples = sorted([f for f in os.listdir(d_dir) if f.endswith(".wav")])

            if args.coverage < 100 and not args.gate:
                max_clips = max(1, int(len(samples) * (args.coverage / 100.0)))
                samples = select_corpus_clips(samples, {"max_clips": max_clips, "strata": (corpus or {}).get("strata")})

            if args.gate:
                samples = gate_filter(s_name, samples)

            for sample in samples:
                for encoder in encoders:
                    if (encoder.name, s_name, sample) not in existing_keys:
                        tasks.append((encoder, s_name, cfg, sample, d_dir))

        if tasks:
            total_tasks = len(tasks)
            print(f"\n>>> Running missing Encoder Scenarios across {total_tasks} tasks ({num_cpus} threads)...")

            ref_cache_dir = os.path.join(output_dir, "ref_cache")
            os.makedirs(ref_cache_dir, exist_ok=True)

            completed = 0
            with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus, initializer=init_worker) as executor:
                futures = [executor.submit(process_encoder_task, enc, s_name, cfg, sample, d_dir, output_dir,
                                          args.skip_mos, args.skip_stereo, args.skip_transient, ref_cache_dir)
                           for enc, s_name, cfg, sample, d_dir in tasks]

                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    completed += 1
                    if res:
                        encoder_results.append(res)
                        status_mark = "OK" if res["decode_valid"] else "FAIL"
                        print(f"  [{completed}/{total_tasks}] {res['tool']} ({profile_label(res['profile'])}) | {res['scenario']} | {res['filename']} -> {status_mark}")

            with open(args.results_json, "w") as f:
                json.dump(encoder_results, f, indent=2)
        elif encoders:
            print("==> All encoder tasks satisfied from cached JSON runs!")

        if not encoder_results and encoders:
            total_tasks = 0
            tasks = []
            for s_name in scenario_list:
                cfg = SCENARIOS[s_name]
                d_dir = corpus_dir(cfg, external_data_dir)
                if not os.path.exists(d_dir):
                    continue

                corpus = CORPORA.get(cfg["corpus"])
                samples = sorted([f for f in os.listdir(d_dir) if f.endswith(".wav")])

                if args.coverage < 100 and not args.gate:
                    max_clips = max(1, int(len(samples) * (args.coverage / 100.0)))
                    samples = select_corpus_clips(samples, {"max_clips": max_clips, "strata": (corpus or {}).get("strata")})

                if args.gate:
                    samples = gate_filter(s_name, samples)

                for sample in samples:
                    for encoder in encoders:
                        tasks.append((encoder, s_name, cfg, sample, d_dir))

            total_tasks = len(tasks)
            print(f"\n>>> Running Encoder Scenarios across {total_tasks} tasks ({num_cpus} threads)...")

            ref_cache_dir = os.path.join(output_dir, "ref_cache")
            os.makedirs(ref_cache_dir, exist_ok=True)

            completed = 0
            with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus, initializer=init_worker) as executor:
                futures = [executor.submit(process_encoder_task, enc, s_name, cfg, sample, d_dir, output_dir,
                                          args.skip_mos, args.skip_stereo, args.skip_transient, ref_cache_dir)
                           for enc, s_name, cfg, sample, d_dir in tasks]

                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    completed += 1
                    if res:
                        encoder_results.append(res)
                        status_mark = "OK" if res["decode_valid"] else "FAIL"
                        print(f"  [{completed}/{total_tasks}] {res['tool']} ({profile_label(res['profile'])}) | {res['scenario']} | {res['filename']} -> {status_mark}")

            with open(args.results_json, "w") as f:
                json.dump(encoder_results, f, indent=2)

    # Decoder Benchmarking Phase
    decoder_results = []
    decoder_robustness_results = []
    decoders = []
    if run_decoders:
        if not encoder_results and os.path.exists(args.results_json):
            try:
                with open(args.results_json) as f:
                    encoder_results = json.load(f)
            except Exception:
                pass

        decoders = detect_decoders(args)
        if not decoders:
            print("No decoders detected!")
            if not run_encoders:
                sys.exit(1)
        else:
            print(f"Detected decoders: {', '.join(d.name for d in decoders)}")

        valid_encoder_bitstreams = [r for r in encoder_results if r.get("decode_valid") and r.get("aac_path") and os.path.exists(r["aac_path"])]
        if args.gate and valid_encoder_bitstreams:
            gate_filenames = set()
            for s_name in scenario_list:
                gate_filenames.update(GATE_CLIPS.get(s_name, []))
            if gate_filenames:
                valid_encoder_bitstreams = [r for r in valid_encoder_bitstreams if r.get("filename") in gate_filenames]
        ref_cache_dir = os.path.join(output_dir, "ref_cache")
        os.makedirs(ref_cache_dir, exist_ok=True)

        # Repeated timing and corruption of every bitrate/scenario of a clip
        # add nothing over one scenario per clip and encoder row, and they
        # dominate a full run; sample them. The gate is already one clip per
        # scenario, so it keeps its own robustness sampling below.
        sampled_ids = set()
        seen_sample = set()
        for item in sorted(valid_encoder_bitstreams, key=lambda r: r.get("scenario", "")):
            key = (item.get("filename"), item.get("row_key"))
            if key not in seen_sample:
                seen_sample.add(key)
                sampled_ids.add(id(item))

        # Load any existing decoder results
        candidate_jsons = args.saved_jsons if args.saved_jsons else auto_detect_saved_json_files(args.results_json)
        _enc_res, loaded_dec, loaded_rob = load_all_saved_results(candidate_jsons)
        if loaded_dec:
            decoder_results.extend(loaded_dec)
        if loaded_rob:
            decoder_robustness_results.extend(loaded_rob)

        existing_dec_keys = {(r.get("tool"), r.get("encoder_row_key"), r.get("scenario"), r.get("filename")) for r in decoder_results if r.get("decode_valid") is not None}
        existing_rob_keys = {(r.get("tool"), r.get("encoder_row_key"), r.get("scenario"), r.get("filename")) for r in decoder_robustness_results if r.get("passed") is not None}

        if valid_encoder_bitstreams and decoders:
            total_dec_tasks = len(valid_encoder_bitstreams)
            print(f"\n>>> Running Decoder Benchmarks across {total_dec_tasks} bitstreams x {len(decoders)} decoders...")
            for decoder in decoders:
                # Fast path in --mode both: when evaluating any decoder matching the pass's decoder (e.g. FFmpeg, FAAD, FDK), directly reuse metrics!
                dec_tasks = []
                for item in valid_encoder_bitstreams:
                    dec_key = (decoder.name, item.get("row_key"), item.get("scenario"), item.get("filename"))
                    if dec_key in existing_dec_keys:
                        continue

                    item_dec_tool = item.get("decoder_id") or item.get("decoder_name") or ""
                    is_matching_dec = (
                        (decoder.tool_id in ("ffmpeg_aac", "ffmpeg")) or
                        (item_dec_tool and (decoder.tool_id.lower() in item_dec_tool.lower() or decoder.name.lower() in item_dec_tool.lower()))
                    )

                    if is_matching_dec and item.get("mos") is not None:
                        reused_res = {
                            "tool": decoder.name,
                            "row_key": decoder_row_key(decoder),
                            "encoder_row_key": item["row_key"],
                            "scenario": item["scenario"],
                            "filename": item["filename"],
                            "profile": item.get("profile", "lc"),
                            "container": "ADTS" if item.get("aac_path", "").lower().endswith((".aac", ".adts")) else "M4A",
                            "duration": item.get("duration", 0),
                            "audio_duration": item.get("audio_duration"),
                            "decode_valid": item.get("decode_valid", True),
                            "decode_error": item.get("decode_error"),
                            "mos": item.get("mos"),
                            "mos_source": "inherited",
                            "snr_db": float("inf"),
                            "conformance_snr_db": "ref",
                            "alignment_delay_ms": 0.0,
                            "gapless_offset_samples": 0,
                            "gapless_length_delta": 0,
                            "peak_ram_kb": item.get("peak_ram_kb"),
                            "decoded_wav": None,
                            "dec_channels": None,
                            "mono_downmix": False
                        }
                        decoder_results.append(reused_res)
                        existing_dec_keys.add(dec_key)
                    else:
                        dec_tasks.append(item)

                if dec_tasks:
                    print(f"  Decoding with {decoder.name} ({len(dec_tasks)} pending bitstreams)...")
                    completed_dec = 0
                    with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus, initializer=init_worker) as executor:
                        futures = [executor.submit(process_decoder_task, decoder, item, output_dir, args.skip_mos, ref_cache_dir,
                                                   args.iterations if (args.gate or id(item) in sampled_ids) else 1, args.keep_decodes)
                                   for item in dec_tasks]
                        for future in concurrent.futures.as_completed(futures):
                            res = future.result()
                            completed_dec += 1
                            if res:
                                decoder_results.append(res)
                                status_mark = "TIMEOUT" if res.get("timeout") else ("OK" if res["decode_valid"] else "FAIL")
                                mos_tag = {"inherited": " inh", "scored": ""}.get(res.get("mos_source"), "")
                                mos_str = f", MOS: {res['mos']:.2f}{mos_tag}" if res.get("mos") is not None else ""
                                snr_str = f", SNR: {res['snr_db']:.1f} dB" if res.get("snr_db") is not None else ""
                                ch_tag = " (1ch)" if res.get("mono_downmix") else ""
                                prof_str = profile_label(res.get('profile', 'lc'))
                                print(f"    [{completed_dec}/{len(dec_tasks)}] {decoder.name} ({prof_str}) | {res['scenario']} | {res['filename']} -> {status_mark}{ch_tag}{mos_str}{snr_str}")
                else:
                    print(f"  Decoder tasks for {decoder.name} satisfied from cache/reuse.")

            mos_scored = decoder_results and any(r.get("mos_source") for r in decoder_results)
            if mos_scored:
                n_inherited = sum(1 for r in decoder_results if r.get("mos_source") == "inherited")
                n_scored = sum(1 for r in decoder_results if r.get("mos_source") == "scored")
                print(f"\n>>> MOS: {n_inherited} inherited from the encoder phase's ffmpeg-decode score "
                      f"(conformance SNR >= {CONFORMANCE_SNR_FLOOR_DB:.0f} dB), {n_scored} scored directly")

            print(f"\n>>> Running Decoder Robustness Pass (Corrupted Bitstreams)...")
            robustness_bitstreams = valid_encoder_bitstreams
            if args.gate and valid_encoder_bitstreams:
                seen_scenarios = set()
                gate_robustness = []
                for item in valid_encoder_bitstreams:
                    sc_key = (item.get("scenario"), item.get("row_key"))
                    if sc_key not in seen_scenarios:
                        seen_scenarios.add(sc_key)
                        gate_robustness.append(item)
                robustness_bitstreams = gate_robustness
            else:
                robustness_bitstreams = [r for r in valid_encoder_bitstreams if id(r) in sampled_ids]

            for decoder in decoders:
                rob_tasks = [item for item in robustness_bitstreams if (decoder.name, item.get("row_key"), item.get("scenario"), item.get("filename")) not in existing_rob_keys]
                if rob_tasks:
                    print(f"  Testing robustness for {decoder.name} ({len(rob_tasks)} pending bitstreams)...")
                    completed_rob = 0
                    with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus, initializer=init_worker) as executor:
                        futures = [executor.submit(process_decoder_robustness_task, decoder, item, output_dir) for item in rob_tasks]
                        for future in concurrent.futures.as_completed(futures):
                            res = future.result()
                            completed_rob += 1
                            if res:
                                decoder_robustness_results.append(res)
                                status_mark = "TIMEOUT" if res.get("timeout") else ("RUNAWAY" if res.get("runaway") else ("PASS" if res.get("passed") else "FAIL"))
                                prof_str = profile_label(res.get('profile', 'lc'))
                                print(f"    [{completed_rob}/{len(rob_tasks)}] {decoder.name} ({prof_str}) | {res['scenario']} | {res['filename']} -> {status_mark}")
                else:
                    print(f"  Robustness tasks for {decoder.name} satisfied from cache/reuse.")

    # Decoder results carry different fields (mos/snr_db/alignment/speed per
    # decoder+profile) than encoder_results, and args.results_json is kept as
    # a bare list for backward compatibility with the reload logic above and
    # external consumers. Persist decoder data to a sibling file instead of
    # reshaping args.results_json.
    if run_decoders and decoders:
        decoders_json_path = f"{args.results_json}.decoders.json"
        with open(decoders_json_path, "w") as f:
            json.dump({
                "decoder_results": decoder_results,
                "decoder_robustness_results": decoder_robustness_results,
            }, f, indent=2)
        print(f"==> Decoder results saved to {decoders_json_path}")

    # Muxer benchmark: times faam vs ffmpeg vs MP4Box on the largest ADTS
    # elementary stream this run produced. Independent of encoder/decoder
    # mode since it only needs one bitstream to work with.
    muxer_results = []
    if args.muxer_bench:
        adts_candidates = [r["aac_path"] for r in encoder_results
                           if r.get("aac_path") and os.path.exists(r["aac_path"])
                           and r["aac_path"].lower().endswith((".aac", ".adts"))]
        largest_adts = max(adts_candidates, key=os.path.getsize) if adts_candidates else None
        faam_bin = args.faam_bin or shutil.which("faam")
        ffmpeg_bin = args.ffmpeg_bin or shutil.which("ffmpeg")
        if not largest_adts and ffmpeg_bin:
            # An M4A-only run still has bitstreams: pull the largest one out
            # as an ADTS elementary stream so the muxers have something to mux.
            m4a_candidates = [r["aac_path"] for r in encoder_results
                              if r.get("aac_path") and os.path.exists(r["aac_path"])
                              and r["aac_path"].lower().endswith((".m4a", ".mp4"))]
            if m4a_candidates:
                src = max(m4a_candidates, key=os.path.getsize)
                largest_adts = os.path.join(output_dir, "muxer_bench_src.aac")
                res = subprocess.run([ffmpeg_bin, "-y", "-v", "error", "-i", src, "-c:a", "copy",
                                      "-f", "adts", largest_adts], capture_output=True)
                if res.returncode != 0:
                    largest_adts = None
        if not largest_adts:
            print("==> --muxer-bench: no ADTS bitstream available in this run, skipping")
        else:
            print(f"\n>>> Running Muxer Benchmark on {os.path.basename(largest_adts)}...")
            muxer_results = run_muxer_bench(faam_bin, ffmpeg_bin, largest_adts, output_dir, iterations=args.iterations)
            for r in muxer_results:
                status = r["note"] if r.get("note") else f"{r['mean_ms']:.2f} ms"
                print(f"    {r['tool']:<10} {r['op']:<20} {status}")

    # Final Leaderboard Generation
    out_file = args.output
    if run_encoders and run_decoders and decoders:
        generate_leaderboard(encoders, encoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, has_decoders=True)
        generate_decoder_leaderboard(decoders, decoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, robustness_results=decoder_robustness_results, append_mode=True)
    elif run_encoders:
        generate_leaderboard(encoders, encoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs)
    elif run_decoders and decoders:
        generate_decoder_leaderboard(decoders, decoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, robustness_results=decoder_robustness_results)

    if args.decoder_report and run_decoders and decoders:
        generate_decoder_report(decoders, decoder_results, decoder_robustness_results, args.decoder_report,
                                muxer_results=muxer_results)

    if args.gate and run_decoders and decoders:
        gate_ok, gate_lines = evaluate_gate(decoder_results, decoder_robustness_results)
        print()
        for line in gate_lines:
            print(f"  {line}")
        print(f"\nGATE: {'PASS' if gate_ok else 'FAIL'}")
        if not gate_ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
