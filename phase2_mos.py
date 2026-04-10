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
import tempfile
import concurrent.futures
import subprocess
import shutil
import argparse

try:
    import ffmpeg
except ImportError:
    ffmpeg = None

# Support for multiple Python ViSQOL implementations
try:
    from visqol import VisqolApi
    HAS_VISQOL_PYTHON = True
except ImportError:
    HAS_VISQOL_PYTHON = False

try:
    import visqol_py
    from visqol_py import ViSQOLMode
    HAS_VISQOL_PY = True
except ImportError:
    HAS_VISQOL_PY = False

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import SCENARIOS

# Global paths for binary mode
VISQOL_BIN = os.environ.get("VISQOL_BIN")
MODEL_DIR = os.environ.get("VISQOL_MODEL_DIR")

def find_visqol_assets():
    global VISQOL_BIN, MODEL_DIR

    if not VISQOL_BIN:
        VISQOL_BIN = shutil.which("visqol")

    if not VISQOL_BIN:
        # Check common location in Docker or build dirs
        common_locs = ["/app/visqol/bazel-bin/visqol"]
        for loc in common_locs:
            if os.path.exists(loc):
                VISQOL_BIN = loc
                break

    if VISQOL_BIN and not MODEL_DIR:
        # Try to find model relative to binary
        # Case 1: /app/visqol/bazel-bin/visqol -> /app/visqol/model
        base_dir = os.path.dirname(VISQOL_BIN)
        rel_model = os.path.abspath(os.path.join(base_dir, "..", "model"))
        if os.path.exists(rel_model):
            MODEL_DIR = rel_model
        else:
            # Case 2: /usr/local/bin/visqol -> /usr/local/share/visqol/model?
            # For now, check /app/visqol/model as a fallback
            if os.path.exists("/app/visqol/model"):
                MODEL_DIR = "/app/visqol/model"

# Initialize paths
find_visqol_assets()

# Process-local storage for ViSQOL instances (Python mode)
_process_visqol_instances = {}
_process_visqol_api_instances = {}

def get_process_visqol_python(mode_str):
    if not HAS_VISQOL_PYTHON:
        return None
    if mode_str not in _process_visqol_api_instances:
        try:
            api = VisqolApi()
            api.create(mode=mode_str)
            _process_visqol_api_instances[mode_str] = api
        except Exception as e:
            print(f" Failed to initialize ViSQOL Python (modern): {e}")
            _process_visqol_api_instances[mode_str] = None
    return _process_visqol_api_instances[mode_str]

def get_process_visqol_py(mode_str):
    if not HAS_VISQOL_PY:
        return None
    if mode_str not in _process_visqol_instances:
        try:
            mode = ViSQOLMode.SPEECH if mode_str == "speech" else ViSQOLMode.AUDIO
            _process_visqol_instances[mode_str] = visqol_py.ViSQOL(mode=mode)
        except Exception as e:
            print(f" Failed to initialize ViSQOL Python (legacy): {e}")
            _process_visqol_instances[mode_str] = None
    return _process_visqol_instances[mode_str]

def get_aac_path(key, aac_dir, results_path, aac_files=None):
    results_filename = os.path.basename(results_path)
    precision_suffix = ""
    if "_base.json" in results_filename:
        precision_suffix = "_base"
    elif "_cand.json" in results_filename:
        precision_suffix = "_cand"

    # Try exact match first
    target_filename = f"{key}{precision_suffix}.aac"
    aac_path = os.path.join(aac_dir, target_filename)
    if os.path.exists(aac_path):
        return aac_path

    # Fallback to prefix matching
    if aac_files is None:
        try:
            aac_files = [f for f in os.listdir(aac_dir) if f.endswith(".aac")]
        except FileNotFoundError:
            return None

    matching = [f for f in aac_files if f.startswith(key)]
    if not matching:
        return None
    return os.path.join(aac_dir, matching[0])

def convert_to_wav(input_path, output_path, rate, channels):
    try:
        if ffmpeg:
            ffmpeg.input(input_path).output(
                output_path, ar=rate, ac=channels, sample_fmt='s16').run(
                quiet=True, overwrite_output=True)
        else:
            subprocess.run(['ffmpeg', '-i', input_path, '-ar', str(rate), '-ac', str(channels), '-sample_fmt', 's16', output_path],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"  FFmpeg conversion failed for {input_path}: {e}")
        return False

def get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files=None):
    scenario_name = entry.get("scenario")
    filename = entry.get("filename")
    cfg = SCENARIOS.get(scenario_name)

    if not cfg:
        return None

    data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
    ref_input_path = os.path.join(external_data_dir, data_subdir, filename)

    aac_path = get_aac_path(key, aac_dir, results_path, aac_files=aac_files)

    return {
        "cfg": cfg,
        "ref_input_path": ref_input_path,
        "aac_path": aac_path,
        "v_rate": cfg["visqol_rate"],
        "v_channels": 1 if cfg["mode"] == "speech" else 2
    }

def compute_single_mos(key, entry, aac_dir, external_data_dir, results_path, backend="auto", aac_files=None):
    info = get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files=aac_files)
    if not info or not info["aac_path"]:
        return key, None

    cfg = info["cfg"]
    ref_input_path = info["ref_input_path"]
    aac_path = info["aac_path"]
    v_rate = info["v_rate"]
    v_channels = info["v_channels"]

    with tempfile.TemporaryDirectory() as tmpdir:
        v_ref = os.path.join(tmpdir, "vref.wav")
        v_deg = os.path.join(tmpdir, "vdeg.wav")

        if not convert_to_wav(ref_input_path, v_ref, v_rate, v_channels):
            return key, None

        # Combined decode gate and conversion
        if not convert_to_wav(aac_path, v_deg, v_rate, v_channels):
            # If conversion fails, it might be a decode error. Returning 1.0 as "worst case"
            # though convert_to_wav already prints the error.
            print(f"  FFmpeg decode gate failed for {key}")
            return key, 1.0

        try:
            # 1. Try Binary Mode (Highest consistency with known-good results)
            if backend in ["auto", "visqol"]:
                if VISQOL_BIN and os.path.exists(VISQOL_BIN):
                    cmd = [VISQOL_BIN, "--reference_file", v_ref, "--degraded_file", v_deg]
                    if cfg["mode"] == "speech":
                        cmd.append("--use_speech_mode")
                        if MODEL_DIR:
                            cmd.extend(["--similarity_to_quality_model", os.path.join(MODEL_DIR, "lattice_tcditugenmeetpackhref_ls2_nl60_lr12_bs2048_learn.005_ep2400_train1_7_raw.tflite")])
                    else:
                        if MODEL_DIR:
                            cmd.extend(["--similarity_to_quality_model", os.path.join(MODEL_DIR, "libsvm_nu_svr_model.txt")])

                    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                    for line in result.stdout.splitlines():
                        if "MOS-LQO:" in line:
                            mos = float(line.split()[-1])
                            return key, mos
                elif backend == "visqol":
                    print(f"  ERROR: visqol binary not found but requested for {key}")
                    return key, None

            # 2. Try visqol_py (Legacy)
            if backend in ["auto", "visqol-py"]:
                if HAS_VISQOL_PY:
                    visqol = get_process_visqol_py(cfg["mode"])
                    if visqol:
                        result = visqol.measure(v_ref, v_deg)
                        return key, float(result.moslqo)
                elif backend == "visqol-py":
                    print(f"  ERROR: visqol-py not found but requested for {key}")
                    return key, None

            # 3. Try visqol-python (Modern) - Moved to end due to MOS discrepancy in speech mode
            if backend in ["auto", "visqol-python"]:
                if HAS_VISQOL_PYTHON:
                    api = get_process_visqol_python(cfg["mode"])
                    if api:
                        result = api.measure(v_ref, v_deg)
                        return key, float(result.moslqo)
                elif backend == "visqol-python":
                    print(f"  ERROR: visqol-python not found but requested for {key}")
                    return key, None

        except Exception as e:
            print(f"  Error computing MOS for {key}: {e}")

    return key, None

def run_visqol_python_batch(pending, aac_dir, external_data_dir, results_path, aac_files=None):
    print(f"Using visqol-python batch mode for {len(pending)} samples...")

    # We need to group by mode (audio vs speech)
    modes = {"audio": [], "speech": []}
    for key, entry in pending.items():
        info = get_sample_info(key, entry, aac_dir, external_data_dir, results_path, aac_files=aac_files)
        if info:
            modes[info["cfg"]["mode"]].append((key, entry, info))

    results = {}
    with tempfile.TemporaryDirectory() as batch_tmpdir:
        for mode, items in modes.items():
            if not items:
                continue

            print(f"  Processing {len(items)} {mode} samples...")
            api = get_process_visqol_python(mode)
            if not api:
                print(f"    Failed to get VisqolApi for {mode}, skipping batch.")
                continue

            file_pairs = []
            valid_keys = []
            for key, entry, info in items:
                v_rate = info["v_rate"]
                v_channels = info["v_channels"]
                ref_input_path = info["ref_input_path"]
                aac_path = info["aac_path"]

                if aac_path and os.path.exists(ref_input_path):
                    v_ref = os.path.join(batch_tmpdir, f"{key}_ref.wav")
                    v_deg = os.path.join(batch_tmpdir, f"{key}_deg.wav")

                    if convert_to_wav(ref_input_path, v_ref, v_rate, v_channels) and \
                       convert_to_wav(aac_path, v_deg, v_rate, v_channels):
                        file_pairs.append((v_ref, v_deg))
                        valid_keys.append(key)
                else:
                    print(f"    Missing file for {key}, skipping.")

            if file_pairs:
                batch_results = api.measure_batch(file_pairs, parallel=True)
                for key, result in zip(valid_keys, batch_results):
                    if isinstance(result, Exception):
                        print(f"    Error for {key}: {result}")
                    else:
                        results[key] = float(result.moslqo)

    return results

def main():
    parser = argparse.ArgumentParser(description="ViSQOL MOS computation (Phase 2)")
    parser.add_argument("results_json", help="Path to results JSON file")
    parser.add_argument("aac_dir", help="Path to directory containing AAC files")
    parser.add_argument("external_data_dir", help="Path to external data directory")
    parser.add_argument("--backend", choices=["auto", "visqol", "visqol-py", "visqol-python"],
                        default="auto", help="ViSQOL backend to use")

    args = parser.parse_args()

    results_path = args.results_json
    aac_dir = args.aac_dir
    external_data_dir = args.external_data_dir

    with open(results_path, 'r') as f:
        data = json.load(f)

    matrix = data.get("matrix", {})
    total = len(matrix)
    num_cpus = os.cpu_count() or 1

    # Precompute AAC file list for performance
    try:
        aac_files = [f for f in os.listdir(aac_dir) if f.endswith(".aac")]
    except FileNotFoundError:
        aac_files = []

    # Filter to entries that don't already have a MOS score
    pending = {key: entry for key, entry in matrix.items() if entry.get("mos") is None}
    skipped = total - len(pending)
    if skipped > 0:
        print(f"Skipping {skipped} entries with existing MOS scores.")

    if not pending:
        print("No pending MOS computations.")
        return

    mos_results = {}

    # Sequential/Parallel fallback stack (Binary -> Legacy -> Modern)
    # We no longer use unconditional visqol-python batching to ensure binary priority.
    still_pending = pending
    if still_pending:
        if args.backend == "auto":
            mode_str = "Binary" if VISQOL_BIN else "visqol_py" if HAS_VISQOL_PY else "visqol-python" if HAS_VISQOL_PYTHON else "None"
            print(f"Computing MOS for {len(still_pending)} samples using prioritized stack (Primary: {mode_str}, {num_cpus} cores)...")
        else:
            print(f"Computing MOS for {len(still_pending)} samples using backend '{args.backend}' ({num_cpus} cores)...")

        with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
            futures = {
                executor.submit(compute_single_mos, key, entry, aac_dir, external_data_dir, results_path, args.backend, aac_files): key
                for key, entry in still_pending.items()
            }

            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                key, mos = future.result()
                if mos is not None:
                    mos_results[key] = mos
                mos_str = f"{mos:.2f}" if mos is not None else "N/A"
                print(f"  ({i+1}/{len(still_pending)}) {key}: {mos_str}")

    # Update data with results
    for key, mos in mos_results.items():
        if key in matrix:
            matrix[key]["mos"] = mos

    with open(results_path, 'w') as f:
        json.dump(data, f, indent=2)
    print("Phase 2 (MOS) complete.")

if __name__ == "__main__":
    main()
