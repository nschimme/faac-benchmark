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
    CLIP_PEER_BUG_GAP, cell_peer_gap, generate_leaderboard, generate_decoder_leaderboard
)

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
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)

        audio_duration = ffmpeg_probe(input_path)
        mos_val = None
        ic_err_val = None
        centroid_deltas = None

        with tempfile.TemporaryDirectory() as td:
            decoded_wav = os.path.join(td, "decoded.wav")
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

    except Exception as e:
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


def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite - Codec Comparison & Leaderboard Generator")
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
    parser.add_argument("--ffmpeg-bin", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", help="Path to afconvert binary (macOS)")
    parser.add_argument("--opusenc-bin", help="Path to opusenc binary")
    parser.add_argument("--lame-bin", help="Path to lame binary")
    parser.add_argument("--include-other-codecs", action="store_true", help="Include non-AAC encoders (Opus, LAME MP3)")
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

    args = parser.parse_args()

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

        if args.resume and os.path.exists(args.results_json):
            print(f"==> Resume mode: Loading existing results from {args.results_json}")
            with open(args.results_json) as f:
                encoder_results = json.load(f)
        elif encoders:
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
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
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

        if valid_encoder_bitstreams and decoders:
            print(f"\n>>> Running Decoder Benchmarks across {len(valid_encoder_bitstreams)} bitstreams x {len(decoders)} decoders...")
            for decoder in decoders:
                print(f"  Decoding with {decoder.name}...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                    futures = [executor.submit(process_decoder_task, decoder, item, output_dir, args.skip_mos, ref_cache_dir) for item in valid_encoder_bitstreams]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            decoder_results.append(res)

            print(f"\n>>> Running Decoder Robustness Pass (Corrupted Bitstreams)...")
            for decoder in decoders:
                print(f"  Testing robustness for {decoder.name}...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                    futures = [executor.submit(process_decoder_robustness_task, decoder, item, output_dir) for item in valid_encoder_bitstreams]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            decoder_robustness_results.append(res)

    # Final Leaderboard Generation
    out_file = args.output
    if run_encoders and run_decoders and decoders:
        generate_leaderboard(encoders, encoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, has_decoders=True)
        generate_decoder_leaderboard(decoders, decoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, robustness_results=decoder_robustness_results, append_mode=True)
    elif run_encoders:
        generate_leaderboard(encoders, encoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs)
    elif run_decoders and decoders:
        generate_decoder_leaderboard(decoders, decoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, robustness_results=decoder_robustness_results)


if __name__ == "__main__":
    main()
