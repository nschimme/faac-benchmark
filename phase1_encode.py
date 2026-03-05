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
import subprocess
import time
import sys
import json
import hashlib
import concurrent.futures
import multiprocessing

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import SCENARIOS

# Paths relative to script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXTERNAL_DATA_DIR = os.path.join(SCRIPT_DIR, "data", "external")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")


def get_binary_size(path):
    if os.path.exists(path):
        return os.path.getsize(path)
    return 0


def get_md5(path):
    if not os.path.exists(path):
        return ""
    hash_md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def worker_init(cpu_id_queue):
    """Pin the worker process to a specific CPU core for consistent benchmarks."""
    cpu_id = cpu_id_queue.get()
    if hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, [cpu_id])
        except Exception as e:
            print(f" Failed to pin process {os.getpid()} to CPU {cpu_id}: {e}")


def process_sample(faac_bin_path, name, cfg, sample, data_dir, precision, env):
    input_path = os.path.join(data_dir, sample)
    key = f"{name}_{sample}"
    output_path = os.path.join(OUTPUT_DIR, f"{key}_{precision}.aac")

    # Determine encoding parameters
    cmd = [faac_bin_path, "-o", output_path, input_path]
    cmd.extend(["-b", str(cfg["bitrate"])])

    try:
        t_start = time.time()
        subprocess.run(cmd, env=env, check=True, capture_output=True)
        t_duration = time.time() - t_start

        mos = None
        aac_size = os.path.getsize(output_path)
        actual_bitrate = None

        try:
            import ffmpeg
            try:
                probe = ffmpeg.probe(input_path)
                duration = float(probe['format']['duration'])
                if duration > 0:
                    # kbps = (bytes * 8) / (seconds * 1000)
                    actual_bitrate = (aac_size * 8) / (duration * 1000)
            except Exception as e:
                print(f" Failed to probe duration for {sample}: {e}")
        except ImportError:
            pass

        return key, {
            "mos": mos,
            "size": aac_size,
            "bitrate": actual_bitrate,
            "bitrate_target": cfg.get("bitrate"),
            "time": t_duration,
            "md5": get_md5(output_path),
            "thresh": cfg["thresh"],
            "scenario": name,
            "filename": sample
        }
    except Exception as e:
        print(f" failed: {e}")
        return None


def run_benchmark(
        faac_bin_path,
        lib_path,
        precision,
        coverage=100,
        run_perceptual=True):
    env = os.environ.copy()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = {
        "matrix": {},
        "throughput": {},
        "lib_size": get_binary_size(lib_path)
    }

    if run_perceptual:
        print(f"Starting Phase 1 (Encoding) for {precision}...")
        # Detect number of CPUs for parallelization
        num_cpus = os.cpu_count() or 1
        print(f"Parallelizing across {num_cpus} threads.")

        for name, cfg in SCENARIOS.items():
            data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
            data_dir = os.path.join(EXTERNAL_DATA_DIR, data_subdir)
            if not os.path.exists(data_dir):
                print(
                    f"  [Scenario: {name}] Data directory {data_dir} not found, skipping.")
                continue

            all_samples = sorted(
                [f for f in os.listdir(data_dir) if f.endswith(".wav")])
            num_to_run = max(1, int(len(all_samples) * coverage / 100.0))
            step = len(all_samples) / num_to_run if num_to_run > 0 else 1
            samples = [all_samples[int(i * step)] for i in range(num_to_run)]

            print(f"  [Scenario: {name}] Processing {len(samples)} samples (coverage {coverage}%)...")

            # Pin each process to a unique CPU core
            manager = multiprocessing.Manager()
            cpu_id_queue = manager.Queue()
            for cpu_id in range(num_cpus):
                cpu_id_queue.put(cpu_id)

            with concurrent.futures.ProcessPoolExecutor(
                max_workers=num_cpus,
                initializer=worker_init,
                initargs=(cpu_id_queue,)
            ) as executor:
                futures = {
                    executor.submit(
                        process_sample,
                        faac_bin_path,
                        name,
                        cfg,
                        sample,
                        data_dir,
                        precision,
                        env): sample for sample in samples}
                for i, future in enumerate(
                        concurrent.futures.as_completed(futures)):
                    result = future.result()
                    if result:
                        key, data = result
                        results["matrix"][key] = data
                        print(
                            f"    ({i + 1}/{len(samples)}) {data['filename']} done.")

    print(f"Measuring throughput for {precision}...")
    # Pin current process to a single core for accurate throughput measurement
    if hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, [0])
        except BaseException:
            pass

    tp_dir = os.path.join(EXTERNAL_DATA_DIR, "throughput")
    if os.path.exists(tp_dir):
        tp_samples = sorted(
            [f for f in os.listdir(tp_dir) if f.endswith(".wav")])
        if tp_samples:
            overall_durations = []
            for sample in tp_samples:
                input_path = os.path.join(tp_dir, sample)
                output_path = os.path.join(
                    OUTPUT_DIR, f"tp_{sample}_{precision}.aac")

                print(f"  Benchmarking throughput with {sample}...")
                try:
                    # Warmup
                    subprocess.run([faac_bin_path,
                                    "-o",
                                    output_path,
                                    input_path],
                                   env=env,
                                   check=True,
                                   capture_output=True)

                    # Multiple runs to average noise
                    durations = []
                    for _ in range(3):
                        start_time = time.perf_counter()
                        subprocess.run([faac_bin_path,
                                        "-o",
                                        output_path,
                                        input_path],
                                       env=env,
                                       check=True,
                                       capture_output=True)
                        durations.append(time.perf_counter() - start_time)

                    avg_dur = sum(durations) / len(durations)
                    results["throughput"][sample] = avg_dur
                    overall_durations.append(avg_dur)
                except BaseException as e:
                    print(f"    Throughput benchmark failed for {sample}: {e}")
                    pass

            if overall_durations:
                results["throughput"]["overall"] = sum(
                    overall_durations) / len(overall_durations)

    return results


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(
            "Usage: python3 phase1_encode.py <faac_bin_path> <lib_path> <precision_name> <output_json> [--skip-mos] [--coverage 100]")
        sys.exit(1)

    do_perc = "--skip-mos" not in sys.argv
    coverage = 100
    if "--coverage" in sys.argv:
        idx = sys.argv.index("--coverage")
        coverage = int(sys.argv[idx + 1])

    data = run_benchmark(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        coverage=coverage,
        run_perceptual=do_perc)

    # Ensure results directory exists
    output_json = os.path.abspath(sys.argv[4])
    os.makedirs(os.path.dirname(output_json), exist_ok=True)
    with open(output_json, "w") as f:
        json.dump(data, f, indent=2)
