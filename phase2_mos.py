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
import multiprocessing

try:
    import ffmpeg
    import visqol_py
    from visqol_py import ViSQOLMode
    HAS_VISQOL = True
except ImportError:
    HAS_VISQOL = False

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import SCENARIOS

# Process-local storage for ViSQOL instances
_process_visqol_instances = {}

def get_process_visqol(mode_str):
    if not HAS_VISQOL:
        return None
    if mode_str not in _process_visqol_instances:
        try:
            mode = ViSQOLMode.SPEECH if mode_str == "speech" else ViSQOLMode.AUDIO
            _process_visqol_instances[mode_str] = visqol_py.ViSQOL(mode=mode)
        except Exception as e:
            print(f" Failed to initialize ViSQOL: {e}")
            _process_visqol_instances[mode_str] = None
    return _process_visqol_instances[mode_str]

def compute_single_mos(key, entry, aac_dir, external_data_dir):
    scenario_name = entry.get("scenario")
    filename = entry.get("filename")
    cfg = SCENARIOS.get(scenario_name)

    if not cfg:
        return key, None

    data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
    ref_input_path = os.path.join(external_data_dir, data_subdir, filename)

    # Results JSON filename usually follows the pattern: {arch}_{precision}_{stage}.json
    # We can derive the precision suffix from the results filename
    results_filename = os.path.basename(results_path)
    precision_suffix = ""
    if "_base.json" in results_filename:
        precision_suffix = results_filename.replace("_base.json", "_base.aac")
    elif "_cand.json" in results_filename:
        precision_suffix = results_filename.replace("_cand.json", "_cand.aac")

    # Target file is f"{key}_{arch}_{precision}_{stage}.aac"
    target_filename = f"{key}_{precision_suffix}"
    aac_path = os.path.join(aac_dir, target_filename)

    if not os.path.exists(aac_path):
        # Fallback to startswith if precision suffix derivation fails or file not found
        aac_files = [f for f in os.listdir(aac_dir) if f.startswith(key) and f.endswith(".aac")]
        if not aac_files:
            return key, None
        aac_path = os.path.join(aac_dir, aac_files[0])


    with tempfile.TemporaryDirectory() as tmpdir:
        v_ref = os.path.join(tmpdir, "vref.wav")
        v_deg = os.path.join(tmpdir, "vdeg.wav")
        v_rate = cfg["visqol_rate"]
        v_channels = 1 if cfg["mode"] == "speech" else 2

        try:
            if not HAS_VISQOL:
                return key, None

            ffmpeg.input(ref_input_path).output(
                v_ref, ar=v_rate, ac=v_channels, sample_fmt='s16').run(
                quiet=True, overwrite_output=True)
            ffmpeg.input(aac_path).output(
                v_deg, ar=v_rate, ac=v_channels, sample_fmt='s16').run(
                quiet=True, overwrite_output=True)

            if os.path.exists(v_ref) and os.path.exists(v_deg):
                visqol = get_process_visqol(cfg["mode"])
                if visqol:
                    result = visqol.measure(v_ref, v_deg)
                    return key, float(result.moslqo)
        except Exception as e:
            print(f"  Error computing MOS for {key}: {e}")

    return key, None

def main():
    if len(sys.argv) < 4:
        print("Usage: python3 phase2_mos.py <results_json> <aac_dir> <external_data_dir>")
        sys.exit(1)

    global results_path
    results_path = sys.argv[1]
    aac_dir = sys.argv[2]
    external_data_dir = sys.argv[3]

    with open(results_path, 'r') as f:
        data = json.load(f)

    matrix = data.get("matrix", {})
    total = len(matrix)
    num_cpus = os.cpu_count() or 1
    print(f"Computing MOS for {total} samples using {num_cpus} cores...")

    with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
        futures = {
            executor.submit(compute_single_mos, key, entry, aac_dir, external_data_dir): key
            for key, entry in matrix.items()
        }

        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            key, mos = future.result()
            if mos is not None:
                matrix[key]["mos"] = mos
            mos_str = f"{mos:.2f}" if mos is not None else "N/A"
            print(f"  ({i+1}/{total}) {key}: {mos_str}")

    with open(results_path, 'w') as f:
        json.dump(data, f, indent=2)
    print("Phase 2 (MOS) complete.")

if __name__ == "__main__":
    main()
