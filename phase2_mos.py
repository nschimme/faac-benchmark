"""
 * FAAC Benchmark Suite
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""

import os
import sys
import json
import math
import hashlib
import tempfile
import concurrent.futures
import subprocess
import shutil
import argparse

import numpy as np

from utils import (wav_conv, get_aac_path, calculate_provenance_hash,
                   get_cached_ref_wav, corpus_dir, scenario_channels)

try:
    import ffmpeg
except ImportError:
    ffmpeg = None

try:
    import soundfile as sf
    import scipy.signal
except ImportError:
    sf = None

try:
    import zimtohrli
    HAS_ZIMTOHRLI = True
except ImportError:
    HAS_ZIMTOHRLI = False

try:
    from visqol import VisqolApi
    HAS_VISQOL_PYTHON = True
except ImportError:
    HAS_VISQOL_PYTHON = False

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import SCENARIOS

# Process-local storage for instances
_process_zimtohrli_instances = {}
_process_visqol_api_instances = {}

def get_process_zimtohrli():
    pid = os.getpid()
    if pid not in _process_zimtohrli_instances:
        if HAS_ZIMTOHRLI:
            try:
                _process_zimtohrli_instances[pid] = zimtohrli.Pyohrli()
            except Exception as e:
                print(f" Failed to initialize Zimtohrli: {e}")
                _process_zimtohrli_instances[pid] = None
        else:
            _process_zimtohrli_instances[pid] = None
    return _process_zimtohrli_instances[pid]

def get_process_visqol_python(mode_str="speech"):
    if not HAS_VISQOL_PYTHON:
        return None

    if mode_str not in _process_visqol_api_instances:
        try:
            api = VisqolApi()
            api.create(mode=mode_str)
            _process_visqol_api_instances[mode_str] = api
        except Exception as e:
            print(f" Failed to initialize ViSQOL Python: {e}")
            _process_visqol_api_instances[mode_str] = None
    return _process_visqol_api_instances[mode_str]

def run_visqol_python_batch(pending, aac_dir, external_data_dir, results_path, aac_files=None):
    """
    Attempts to process a batch of speech/16kHz samples using visqol-python's internal parallelization.
    """
    speech_items = []
    for key, entry in pending.items():
        info = get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files=aac_files)
        if info and info["cfg"]["mode"] == "speech":
            speech_items.append((key, entry, info))

    results = {}
    if not speech_items:
        return results

    with tempfile.TemporaryDirectory() as batch_tmpdir:
        print(f"  Batch processing {len(speech_items)} speech/16kHz samples with visqol-python...")
        api = get_process_visqol_python("speech")
        if not api:
            print("    Failed to initialize VisqolApi for speech, skipping batch.")
            return results

        file_pairs = []
        valid_keys = []
        for key, entry, info in speech_items:
            v_rate = info["v_rate"]
            v_channels = info["v_channels"]
            ref_input_path = info["ref_input_path"]
            aac_path = info["aac_path"]

            if aac_path and os.path.exists(ref_input_path):
                v_ref = os.path.join(batch_tmpdir, f"{key}_ref.wav")
                v_deg = os.path.join(batch_tmpdir, f"{key}_deg.wav")

                if wav_conv(ref_input_path, v_ref, v_rate, v_channels) and \
                   wav_conv(aac_path, v_deg, v_rate, v_channels):
                    file_pairs.append((v_ref, v_deg))
                    valid_keys.append(key)

        if file_pairs:
            try:
                batch_results = api.measure_batch(file_pairs, parallel=True)
                for key, result in zip(valid_keys, batch_results):
                    if isinstance(result, Exception):
                        print(f"    Error for {key} in batch: {result}")
                    else:
                        results[key] = (float(result.moslqo), "visqol-python")
            except Exception as e:
                print(f"    Batch execution failed for speech: {e}")

    return results

def get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files=None):
    scenario_name = entry.get("scenario")
    filename = entry.get("filename")
    cfg = SCENARIOS.get(scenario_name)

    if not cfg:
        return None

    ref_input_path = os.path.join(corpus_dir(cfg, external_data_dir), filename)

    aac_path = get_aac_path(key, aac_dir, results_path, aac_files=aac_files, entry=entry)

    return {
        "cfg": cfg,
        "ref_input_path": ref_input_path,
        "aac_path": aac_path,
        "v_rate": cfg["visqol_rate"],
        # Channels come from the corpus, not from the metric mode: 24k_mono_*
        # is mono content scored in AUDIO mode, and the old ternary would have
        # silently upmixed it to stereo before scoring.
        "v_channels": scenario_channels(cfg)
    }

_ZIMTOHRLI_SUBPROCESS_SCRIPT = """
import sys, json, math, scipy.signal, soundfile as sf, numpy as np, zimtohrli
v_ref, v_deg = sys.argv[1], sys.argv[2]
try:
    ref_data, sr_r = sf.read(v_ref, dtype="float32", always_2d=True)
    dec_data, sr_d = sf.read(v_deg, dtype="float32", always_2d=True)

    ZIMT_RATE = 48000
    if sr_r != ZIMT_RATE:
        g = math.gcd(ZIMT_RATE, sr_r)
        ref_data = scipy.signal.resample_poly(ref_data, ZIMT_RATE // g, sr_r // g, axis=0)
    if sr_d != ZIMT_RATE:
        g = math.gcd(ZIMT_RATE, sr_d)
        dec_data = scipy.signal.resample_poly(dec_data, ZIMT_RATE // g, sr_d // g, axis=0)

    if len(ref_data) == 0 or len(dec_data) == 0:
        sys.exit(1)

    r_mono = ref_data.mean(axis=1)
    d_mono = dec_data.mean(axis=1)

    n_search = min(len(r_mono), len(d_mono), ZIMT_RATE * 3)
    if n_search == 0:
        sys.exit(1)

    r_norm = r_mono[:n_search] / (np.std(r_mono[:n_search]) + 1e-10)
    d_norm = d_mono[:n_search] / (np.std(d_mono[:n_search]) + 1e-10)
    corr = scipy.signal.correlate(r_norm, d_norm, mode="full")
    lag = int(np.argmax(corr)) - (n_search - 1)

    if lag < 0:
        dec_aligned = dec_data[-lag:]
        ref_aligned = ref_data[:len(ref_data) + lag]
    elif lag > 0:
        ref_aligned = ref_data[lag:]
        dec_aligned = dec_data[:len(dec_data) - lag]
    else:
        ref_aligned, dec_aligned = ref_data, dec_data

    n = min(len(ref_aligned), len(dec_aligned))
    if n <= 0:
        sys.exit(1)

    num_ref_ch = ref_aligned.shape[1]
    num_dec_ch = dec_aligned.shape[1]

    if num_ref_ch != num_dec_ch:
        if num_ref_ch == 1 and num_dec_ch > 1:
            ref_aligned = np.repeat(ref_aligned, num_dec_ch, axis=1)
        elif num_dec_ch == 1 and num_ref_ch > 1:
            dec_aligned = np.repeat(dec_aligned, num_ref_ch, axis=1)
        else:
            common_ch = min(num_ref_ch, num_dec_ch)
            ref_aligned = ref_aligned[:, :common_ch]
            dec_aligned = dec_aligned[:, :common_ch]

    z_engine = zimtohrli.Pyohrli()
    num_channels = ref_aligned.shape[1]
    per_channel_dist = [
        z_engine.distance(
            np.ascontiguousarray(ref_aligned[:n, ch], dtype=np.float32),
            np.ascontiguousarray(dec_aligned[:n, ch], dtype=np.float32)
        )
        for ch in range(num_channels)
    ]
    dist = math.sqrt(sum(d * d for d in per_channel_dist))
    mos = float(zimtohrli.mos_from_zimtohrli(dist))
    print(json.dumps({"mos": mos, "backend": "zimtohrli"}))
except Exception:
    sys.exit(1)
"""

def score_wav_pair(v_ref, v_deg, mode_str="audio", sample_rate=None):
    """Score an already-converted ref/deg WAV pair.

    Speech mode uses visqol-python (16 kHz mono) when available, falling back
    to Zimtohrli (48 kHz resampled). Audio mode uses Zimtohrli directly.
    Returns (mos, backend_used); mos is None on failure."""
    try:
        if mode_str == "speech" and HAS_VISQOL_PYTHON:
            try:
                api = get_process_visqol_python("speech")
                if api:
                    result = api.measure(v_ref, v_deg)
                    return float(result.moslqo), "visqol-python"
            except Exception as e:
                print(f"  visqol-python speech evaluation failed: {e}, falling back to zimtohrli")

        # Audio mode or speech fallback: use Zimtohrli via isolated sub-process runner to prevent C++ extension memory faults
        if HAS_ZIMTOHRLI:
            try:
                env = dict(os.environ)
                if sys.platform == "darwin":
                    env["NUMBA_THREADING_LAYER"] = "workqueue"
                else:
                    env.setdefault("NUMBA_THREADING_LAYER", "omp")

                proc = subprocess.run([sys.executable, "-c", _ZIMTOHRLI_SUBPROCESS_SCRIPT, v_ref, v_deg],
                                      capture_output=True, text=True, timeout=30, env=env)
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout.strip())
                    return data.get("mos"), data.get("backend", "zimtohrli")
            except Exception as e:
                print(f"  Zimtohrli evaluation failed: {e}")
                return None, "zimtohrli"

        print("  ERROR: zimtohrli is required for audio scoring but not available.")
        return None, "zimtohrli"

    except Exception as e:
        print(f"  Error computing MOS: {e}")

    return None, "none"


def compute_single_mos(key, entry, aac_dir, external_data_dir, results_path, aac_files=None, ref_wav_cache_dir=None):
    info = get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files=aac_files)
    if not info or not info["aac_path"]:
        return key, None, "none"

    cfg = info["cfg"]
    ref_input_path = info["ref_input_path"]
    aac_path = info["aac_path"]
    v_rate = info["v_rate"]
    v_channels = info["v_channels"]

    with tempfile.TemporaryDirectory() as tmpdir:
        v_deg = os.path.join(tmpdir, "vdeg.wav")

        if ref_wav_cache_dir:
            v_ref = get_cached_ref_wav(ref_wav_cache_dir, ref_input_path, v_rate, v_channels)
        else:
            v_ref = os.path.join(tmpdir, "vref.wav")
            if not wav_conv(ref_input_path, v_ref, v_rate, v_channels):
                v_ref = None

        if not v_ref:
            return key, None, "none"

        if not wav_conv(aac_path, v_deg, v_rate, v_channels):
            print(f"  FFmpeg decode gate failed for {key}")
            return key, 1.0, "none"

        mos, backend_used = score_wav_pair(v_ref, v_deg, cfg["mode"], sample_rate=cfg.get("rate"))
        if mos is None and backend_used != "none":
            print(f"  ERROR: backend '{backend_used}' failed for {key}")
        return key, mos, backend_used

def main():
    parser = argparse.ArgumentParser(description="Perceptual MOS computation (Phase 2)")
    parser.add_argument("results_json", help="Path to results JSON file")
    parser.add_argument("aac_dir", help="Path to directory containing AAC files")
    parser.add_argument("external_data_dir", help="Path to external data directory")
    parser.add_argument("--faac-bin", help="Path to faac binary for provenance verification")
    parser.add_argument("--lib-path", help="Path to libfaac.so for provenance verification")
    parser.add_argument("--extra-args", help="Extra arguments string for provenance verification")

    args = parser.parse_args()

    results_path = args.results_json
    aac_dir = args.aac_dir
    external_data_dir = args.external_data_dir

    with open(results_path, 'r') as f:
        data = json.load(f)

    matrix = data.get("matrix", {})
    total = len(matrix)
    num_cpus = os.cpu_count() or 1

    # Precompute AAC/M4A/MP3/Opus file list for performance
    try:
        aac_files = [f for f in os.listdir(aac_dir) if f.endswith((".m4a", ".mp4", ".aac", ".opus", ".mp3"))]
    except FileNotFoundError:
        aac_files = []

    # Provenance-aware caching: invalidate any cached MOS whose recorded
    # provenance hash no longer matches the current binary/args/env/input, so a
    # stale .aac can never be silently re-scored. Mutating entry["mos"]=None
    # here makes the single `pending` comprehension below pick it up.
    stale_count = 0
    verify_provenance = args.faac_bin and args.lib_path
    if verify_provenance:
        for key, entry in matrix.items():
            if entry.get("mos") is None:
                continue
            info = get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files)
            if not info:
                continue
            expected_hash = calculate_provenance_hash(args.faac_bin, args.lib_path, args.extra_args, info["ref_input_path"])
            if entry.get("prov_hash") != expected_hash:
                print(f"!!! Provenance mismatch for {key}: expected {expected_hash}, "
                      f"found {entry.get('prov_hash')}. The encoded .aac is STALE; refusing its MOS.")
                entry["mos"] = None
                stale_count += 1

    if stale_count > 0:
        print(f"Found {stale_count} stale entries with provenance mismatch.")

    pending = {key: entry for key, entry in matrix.items() if entry.get("mos") is None}
    skipped = total - len(pending)
    if skipped > 0:
        print(f"Skipping {skipped} entries with valid existing MOS scores.")

    if not pending:
        print("No pending MOS computations.")
        return

    mos_results = {}

    if HAS_VISQOL_PYTHON:
        batch_results = run_visqol_python_batch(pending, aac_dir, external_data_dir, results_path, aac_files)
        mos_results.update(batch_results)
        pending = {k: v for k, v in pending.items() if k not in mos_results}

    still_pending = pending
    if still_pending:
        print(f"Computing MOS for {len(still_pending)} samples (Speech/16kHz -> visqol-python, Audio -> Zimtohrli, {num_cpus} cores)...")

        with tempfile.TemporaryDirectory() as ref_wav_cache_dir, \
             concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
            futures = {
                executor.submit(compute_single_mos, key, entry, aac_dir, external_data_dir, results_path, aac_files, ref_wav_cache_dir): key
                for key, entry in still_pending.items()
            }

            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                res = future.result()
                if len(res) == 3:
                    key, mos, backend_used = res
                else:
                    key, mos = res
                    backend_used = "zimtohrli"
                if mos is not None:
                    mos_results[key] = (mos, backend_used)
                mos_str = f"{mos:.2f}" if mos is not None else "N/A"
                print(f"  ({i+1}/{len(still_pending)}) {key}: {mos_str}")

    for key, item in mos_results.items():
        if key in matrix:
            if isinstance(item, tuple):
                matrix[key]["mos"] = item[0]
                matrix[key]["mos_backend"] = item[1]
            else:
                matrix[key]["mos"] = item

    with open(results_path, 'w') as f:
        json.dump(data, f, indent=2)
    print("Phase 2 (MOS) complete.")

if __name__ == "__main__":
    main()
