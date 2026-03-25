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
import argparse
import concurrent.futures
import multiprocessing
import fnmatch

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


def process_sample(faac_bin_path, name, cfg, sample, data_dir, precision, env, extra_args=None):
    input_path = os.path.join(data_dir, sample)
    key = f"{name}_{sample}"
    output_path = os.path.join(OUTPUT_DIR, f"{key}_{precision}.aac")

    # Determine encoding parameters
    cmd = [faac_bin_path, "-o", output_path, input_path]
    cmd.extend(["-b", str(cfg["bitrate"])])
    if extra_args:
        cmd.extend(extra_args)

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
        run_perceptual=True,
        sha=None,
        scenarios=None,
        include_tests=None,
        exclude_tests=None,
        extra_args=None):
    env = os.environ.copy()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = {
        "sha": sha,
        "matrix": {},
        "throughput": {},
        "lib_size": get_binary_size(lib_path)
    }

    if run_perceptual:
        print(f"Starting Phase 1 (Encoding) for {precision}...")
        # Detect number of CPUs for parallelization
        num_cpus = os.cpu_count() or 1
        print(f"Parallelizing across {num_cpus} threads.")

        scenario_list = SCENARIOS.keys()
        if scenarios:
            scenario_list = [s.strip() for s in scenarios.split(",")]

        for name in scenario_list:
            if name not in SCENARIOS:
                print(f"  [Scenario: {name}] Warning: Scenario not found in config, skipping.")
                continue
            cfg = SCENARIOS[name]
            data_subdir = "speech" if cfg["mode"] == "speech" else "audio"
            data_dir = os.path.join(EXTERNAL_DATA_DIR, data_subdir)
            if not os.path.exists(data_dir):
                print(
                    f"  [Scenario: {name}] Data directory {data_dir} not found, skipping.")
                continue

            all_samples = sorted(
                [f for f in os.listdir(data_dir) if f.endswith(".wav")])

            # Apply include/exclude filters
            filtered_samples = []
            includes = [i.strip() for i in include_tests.split(",")] if include_tests else ["*"]
            excludes = [e.strip() for e in exclude_tests.split(",")] if exclude_tests else []

            for sample in all_samples:
                should_include = any(fnmatch.fnmatch(sample, i) for i in includes)
                should_exclude = any(fnmatch.fnmatch(sample, e) for e in excludes)
                if should_include and not should_exclude:
                    filtered_samples.append(sample)

            num_to_run = max(1, int(len(filtered_samples) * coverage / 100.0))
            step = len(filtered_samples) / num_to_run if num_to_run > 0 else 1
            samples = [filtered_samples[int(i * step)] for i in range(num_to_run)]

            print(f"  [Scenario: {name}] Processing {len(samples)} samples (coverage {coverage}%)...")

            # Pin each process to a unique CPU core (Linux only; macOS lacks sched_setaffinity)
            if hasattr(os, "sched_setaffinity"):
                manager = multiprocessing.Manager()
                cpu_id_queue = manager.Queue()
                for cpu_id in range(num_cpus):
                    cpu_id_queue.put(cpu_id)
                executor_kwargs = dict(initializer=worker_init, initargs=(cpu_id_queue,))
            else:
                manager = None
                executor_kwargs = {}

            with concurrent.futures.ProcessPoolExecutor(
                max_workers=num_cpus,
                **executor_kwargs
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
                        env,
                        extra_args): sample for sample in samples}
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
    parser = argparse.ArgumentParser(description="Phase 1: Encoding and Basic Metrics")
    parser.add_argument("faac_bin", help="Path to faac binary")
    parser.add_argument("lib_path", help="Path to libfaac.so")
    parser.add_argument("precision", help="Precision name")
    parser.add_argument("output", help="Output JSON path")
    parser.add_argument("--skip-mos", action="store_true", help="Skip perceptual quality (MOS) computation")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--sha", help="Commit SHA")
    parser.add_argument("--scenarios", help="Comma-separated scenarios")
    parser.add_argument("--include-tests", help="Comma-separated include globs")
    parser.add_argument("--exclude-tests", help="Comma-separated exclude globs")
    parser.add_argument("--extra-args", help="Extra arguments to pass to faac encoder (e.g. '--tns')")

    args = parser.parse_args()

    extra_args = args.extra_args.split() if args.extra_args else None
    data = run_benchmark(
        args.faac_bin,
        args.lib_path,
        args.precision,
        coverage=args.coverage,
        run_perceptual=not args.skip_mos,
        sha=args.sha,
        scenarios=args.scenarios,
        include_tests=args.include_tests,
        exclude_tests=args.exclude_tests,
        extra_args=extra_args)

    # Ensure results directory exists
    output_json = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_json), exist_ok=True)
    with open(output_json, "w") as f:
        json.dump(data, f, indent=2)
