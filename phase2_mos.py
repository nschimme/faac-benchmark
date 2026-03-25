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

try:
    import ffmpeg
except ImportError:
    ffmpeg = None

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

def get_process_visqol(mode_str):
    if not HAS_VISQOL_PY:
        return None
    if mode_str not in _process_visqol_instances:
        try:
            mode = ViSQOLMode.SPEECH if mode_str == "speech" else ViSQOLMode.AUDIO
            _process_visqol_instances[mode_str] = visqol_py.ViSQOL(mode=mode)
        except Exception as e:
            print(f" Failed to initialize ViSQOL Python: {e}")
            _process_visqol_instances[mode_str] = None
    return _process_visqol_instances[mode_str]

def compute_single_mos(key, entry, aac_dir, external_data_dir, results_path):
    scenario_name = entry.get("scenario")
    filename = entry.get("filename")
    cfg = SCENARIOS.get(scenario_name)

    if not cfg:
        return key, None

    data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
    ref_input_path = os.path.join(external_data_dir, data_subdir, filename)

    results_filename = os.path.basename(results_path)
    precision_suffix = ""
    if "_base.json" in results_filename:
        precision_suffix = results_filename.replace("_base.json", "_base.aac")
    elif "_cand.json" in results_filename:
        precision_suffix = results_filename.replace("_cand.json", "_cand.aac")

    target_filename = f"{key}_{precision_suffix}"
    aac_path = os.path.join(aac_dir, target_filename)

    if not os.path.exists(aac_path):
        aac_files = [f for f in os.listdir(aac_dir) if f.startswith(key) and f.endswith(".aac")]
        if not aac_files:
            return key, None
        aac_path = os.path.join(aac_dir, aac_files[0])

    # FFmpeg read gate: verify the AAC file decodes without error
    try:
        ffmpeg.input(aac_path).output('pipe:', format='s16le').run(
            quiet=True, capture_stdout=True, capture_stderr=True)
    except Exception as e:
        print(f"  FFmpeg decode gate failed for {key}: {e}")
        return key, 1.0

    with tempfile.TemporaryDirectory() as tmpdir:
        v_ref = os.path.join(tmpdir, "vref.wav")
        v_deg = os.path.join(tmpdir, "vdeg.wav")
        v_rate = cfg["visqol_rate"]
        v_channels = 1 if cfg["mode"] == "speech" else 2

        try:
            # 1. Try Python Mode (visqol_py)
            if HAS_VISQOL_PY:
                if ffmpeg:
                    ffmpeg.input(ref_input_path).output(
                        v_ref, ar=v_rate, ac=v_channels, sample_fmt='s16').run(
                        quiet=True, overwrite_output=True)
                    ffmpeg.input(aac_path).output(
                        v_deg, ar=v_rate, ac=v_channels, sample_fmt='s16').run(
                        quiet=True, overwrite_output=True)
                else:
                    subprocess.run(['ffmpeg', '-i', ref_input_path, '-ar', str(v_rate), '-ac', str(v_channels), '-sample_fmt', 's16', v_ref],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(['ffmpeg', '-i', aac_path, '-ar', str(v_rate), '-ac', str(v_channels), '-sample_fmt', 's16', v_deg],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                visqol = get_process_visqol(cfg["mode"])
                if visqol:
                    result = visqol.measure(v_ref, v_deg)
                    return key, float(result.moslqo)

            # 2. Try Binary Mode
            if VISQOL_BIN and os.path.exists(VISQOL_BIN):
                # Ensure WAVs are generated if not already (though they should be from above if ffmpeg worked)
                if not os.path.exists(v_ref):
                    subprocess.run(['ffmpeg', '-i', ref_input_path, '-ar', str(v_rate), '-ac', str(v_channels), '-sample_fmt', 's16', v_ref],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(['ffmpeg', '-i', aac_path, '-ar', str(v_rate), '-ac', str(v_channels), '-sample_fmt', 's16', v_deg],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

        except Exception as e:
            print(f"  Error computing MOS for {key}: {e}")

    return key, None

def main():
    if len(sys.argv) < 4:
        print("Usage: python3 phase2_mos.py <results_json> <aac_dir> <external_data_dir>")
        sys.exit(1)

    results_path = sys.argv[1]
    aac_dir = sys.argv[2]
    external_data_dir = sys.argv[3]

    with open(results_path, 'r') as f:
        data = json.load(f)

    matrix = data.get("matrix", {})
    total = len(matrix)
    num_cpus = os.cpu_count() or 1

    # Filter to entries that don't already have a MOS score
    pending = {key: entry for key, entry in matrix.items() if entry.get("mos") is None}
    skipped = total - len(pending)
    if skipped > 0:
        print(f"Skipping {skipped} entries with existing MOS scores.")

    mode_str = "Python" if HAS_VISQOL_PY else "Binary" if VISQOL_BIN else "Unknown"
    print(f"Computing MOS for {len(pending)} samples using local ViSQOL ({mode_str}, {num_cpus} cores)...")

    with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
        futures = {
            executor.submit(compute_single_mos, key, entry, aac_dir, external_data_dir, results_path): key
            for key, entry in pending.items()
        }

        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            key, mos = future.result()
            if mos is not None:
                matrix[key]["mos"] = mos
            mos_str = f"{mos:.2f}" if mos is not None else "N/A"
            print(f"  ({i+1}/{len(pending)}) {key}: {mos_str}")

    with open(results_path, 'w') as f:
        json.dump(data, f, indent=2)
    print("Phase 2 (MOS) complete.")

if __name__ == "__main__":
    main()
