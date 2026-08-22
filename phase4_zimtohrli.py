"""
 * FAAC Benchmark Suite — Phase 4: Zimtohrli Perceptual Metric Evaluation
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
import argparse
import subprocess
import tempfile
import concurrent.futures
import numpy as np
import soundfile as sf
import scipy.signal

try:
    import zimtohrli
    HAS_ZIMTOHRLI = True
except ImportError:
    HAS_ZIMTOHRLI = False

from config import SCENARIOS
from utils import get_aac_path

_ZIMTOHRLI_INSTANCES = {}


def get_process_zimtohrli():
    global _ZIMTOHRLI_INSTANCES
    pid = os.getpid()
    if pid not in _ZIMTOHRLI_INSTANCES:
        if HAS_ZIMTOHRLI:
            try:
                _ZIMTOHRLI_INSTANCES[pid] = zimtohrli.Pyohrli()
            except Exception as e:
                print(f"Failed to initialize Zimtohrli for PID {pid}: {e}", file=sys.stderr)
                _ZIMTOHRLI_INSTANCES[pid] = None
        else:
            _ZIMTOHRLI_INSTANCES[pid] = None
    return _ZIMTOHRLI_INSTANCES[pid]


def decode_to_mono_wav(input_path, output_wav_path, target_sr=48000):
    """Decode or transcode any audio file to target_sr mono 16-bit PCM WAV via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ar", str(target_sr), "-ac", "1",
        "-sample_fmt", "s16", output_wav_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed for {input_path}:\n{res.stderr}")


def load_mono(path):
    data, sr = sf.read(path, dtype='float32', always_2d=True)
    return data.mean(axis=1), int(sr)


def find_lag(ref_mono, dec_mono, sr, search_seconds=3):
    n = min(len(ref_mono), len(dec_mono), sr * search_seconds)
    r = ref_mono[:n] / (np.std(ref_mono[:n]) + 1e-10)
    d = dec_mono[:n] / (np.std(dec_mono[:n]) + 1e-10)
    corr = scipy.signal.correlate(r, d, mode='full')
    lag = int(np.argmax(corr)) - (n - 1)
    return lag


def align_signals(ref, dec, lag):
    if lag < 0:
        dec_out = dec[-lag:]
        ref_out = ref[:len(ref) + lag]
    elif lag > 0:
        ref_out = ref[lag:]
        dec_out = dec[:len(dec) - lag]
    else:
        ref_out, dec_out = ref.copy(), dec.copy()
    n = min(len(ref_out), len(dec_out))
    return ref_out[:n], dec_out[:n]


def compute_zimtohrli_single(key, entry, aac_path, external_data_dir):
    scenario_name = entry.get("scenario")
    filename = entry.get("filename")
    cfg = SCENARIOS.get(scenario_name)
    if not cfg or not aac_path or not os.path.exists(aac_path):
        return key, None

    data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
    ref_input_path = os.path.join(external_data_dir, data_subdir, filename)
    if not os.path.exists(ref_input_path):
        return key, None

    z_engine = get_process_zimtohrli()
    if not z_engine:
        return key, None

    with tempfile.TemporaryDirectory() as tmpdir:
        v_ref = os.path.join(tmpdir, "ref.wav")
        v_deg = os.path.join(tmpdir, "deg.wav")

        try:
            decode_to_mono_wav(ref_input_path, v_ref, target_sr=48000)
            decode_to_mono_wav(aac_path, v_deg, target_sr=48000)

            ref_mono, sr = load_mono(v_ref)
            dec_mono, _ = load_mono(v_deg)

            lag = find_lag(ref_mono, dec_mono, sr)
            ref_aligned, dec_aligned = align_signals(ref_mono, dec_mono, lag)

            if len(ref_aligned) < 4800:  # < 100ms
                return key, None

            dist = z_engine.distance(
                np.ascontiguousarray(ref_aligned, dtype=np.float32),
                np.ascontiguousarray(dec_aligned, dtype=np.float32)
            )
            mos = float(zimtohrli.mos_from_zimtohrli(dist))
            return key, mos
        except Exception as e:
            print(f"  Zimtohrli calculation error for {key}: {e}", file=sys.stderr)
            return key, None


def main():
    parser = argparse.ArgumentParser(description="Zimtohrli MOS computation (Phase 4)")
    parser.add_argument("results_json", help="Path to results JSON file")
    parser.add_argument("aac_dir", help="Path to directory containing AAC files")
    parser.add_argument("external_data_dir", help="Path to external data directory")

    args = parser.parse_args()

    if not HAS_ZIMTOHRLI:
        print("zimtohrli python package not installed; skipping Phase 4.")
        return

    with open(args.results_json, 'r') as f:
        data = json.load(f)

    matrix = data.get("matrix", {})
    total = len(matrix)

    try:
        aac_files = [f for f in os.listdir(args.aac_dir) if f.endswith(".aac")]
    except FileNotFoundError:
        aac_files = []

    pending = {key: entry for key, entry in matrix.items() if entry.get("zimtohrli_mos") is None}
    skipped = total - len(pending)
    if skipped > 0:
        print(f"Skipping {skipped} entries with valid existing Zimtohrli MOS scores.")

    if not pending:
        print("No pending Zimtohrli MOS computations.")
        return

    num_cpus = os.cpu_count() or 1
    print(f"Computing Zimtohrli MOS for {len(pending)} samples ({num_cpus} cores)...")

    results = {}
    resolved = {}
    for k, entry in pending.items():
        p = get_aac_path(k, args.aac_dir, args.results_json, aac_files=aac_files, entry=entry)
        if p:
            resolved[k] = (entry, p)

    if not resolved:
        print("No resolvable AAC paths for pending Zimtohrli samples.")
        return

    with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
        futures = {
            executor.submit(compute_zimtohrli_single, k, entry, aac_path, args.external_data_dir): k
            for k, (entry, aac_path) in resolved.items()
        }
        tot = len(futures)
        for i, fut in enumerate(concurrent.futures.as_completed(futures)):
            key, z_mos = fut.result()
            if z_mos is not None:
                results[key] = z_mos
            z_str = f"{z_mos:.2f}" if z_mos is not None else "N/A"
            print(f"  ({i+1}/{tot}) {key}: {z_str}")

    for key, z_mos in results.items():
        if key in matrix:
            matrix[key]["zimtohrli_mos"] = z_mos

    with open(args.results_json, 'w') as f:
        json.dump(data, f, indent=2)
    print("Phase 4 (Zimtohrli MOS) complete.")


if __name__ == "__main__":
    main()
