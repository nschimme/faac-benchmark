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

from utils import (decode_validate, calculate_provenance_hash, get_binary_size,
                   get_file_hash, get_elf_section_sizes, get_section_sizes,
                   get_object_sizes, get_toolchain_fp, get_host_fp)

# Ensure the current directory is in the path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import SCENARIOS, GATE_CLIPS, GATE_FALLBACK_N


def gate_filter(name, filtered_samples):
    """Restrict a scenario's samples to its fixed gate subset (config.GATE_CLIPS),
    intersected with what's on disk. Falls back to a deterministic even-spaced
    slice when no curated list exists, so --gate works for any scenario."""
    available = set(filtered_samples)
    picked = [c for c in GATE_CLIPS.get(name, []) if c in available]
    if picked:
        return picked
    n = min(GATE_FALLBACK_N, len(filtered_samples))
    if n <= 0:
        return []
    step = len(filtered_samples) / n
    return [filtered_samples[int(i * step)] for i in range(n)]

# Paths relative to script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXTERNAL_DATA_DIR = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(SCRIPT_DIR, "data", "external")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

# Timed repetitions per throughput signal. Five is the smallest N that gives the
# minimum a fair chance of landing on an uncontended run and still leaves enough
# samples to bootstrap a confidence interval.
TP_REPS = 5


def worker_init(cpu_id_queue):
    """Pin the worker process to a specific CPU core for consistent benchmarks."""
    cpu_id = cpu_id_queue.get()
    if hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, [cpu_id])
        except Exception as e:
            print(f" Failed to pin process {os.getpid()} to CPU {cpu_id}: {e}")


def process_sample(faac_bin_path, lib_path, name, cfg, sample, data_dir, precision, env, extra_args=None, rate_control="abr"):
    input_path = os.path.join(data_dir, sample)
    key = f"{name}_{sample}"
    output_path = os.path.join(OUTPUT_DIR, f"{key}_{precision}.aac")

    # Determine encoding parameters
    cmd = [faac_bin_path, "-o", output_path, input_path]
    if rate_control == "vbr":
        cmd.extend(["-q", str(cfg.get("vbr_q", 100))])
    else:
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

        # Decode validation
        valid, decode_err = decode_validate(output_path)
        if not valid:
            print(f"    [DECODE ERROR] {sample}: {decode_err}")

        # Rate control bias & accuracy calculations per SOP
        rc_mode = "abr" if rate_control == "abr" else "vbr"
        expected_rate = cfg.get("bitrate")
        bias_ratio = None
        bias_percent = None
        accuracy_score = None
        bias_status = "Optimal"

        if actual_bitrate is not None and expected_rate and expected_rate > 0:
            bias_ratio = actual_bitrate / expected_rate
            bias_percent = (bias_ratio - 1.0) * 100
            accuracy_score = max(0.0, 1.0 - abs((actual_bitrate - expected_rate) / expected_rate))

            if rc_mode == "abr":
                if bias_ratio > 1.05:
                    bias_status = "Overshoot"
                elif bias_ratio < 0.95:
                    bias_status = "Undershoot"
            else:
                if bias_ratio > 1.15:
                    bias_status = "Overshoot"
                elif bias_ratio < 0.85:
                    bias_status = "Undershoot"

        # Provenance hash
        prov_hash = calculate_provenance_hash(faac_bin_path, lib_path, extra_args, input_path)

        return key, {
            "mos": mos,
            "size": aac_size,
            "bitrate": actual_bitrate,
            "bitrate_target": cfg.get("bitrate"),
            "expected_bitrate": expected_rate,
            "rate_control_mode": rc_mode,
            "bias_ratio": bias_ratio,
            "bias_percent": bias_percent,
            "accuracy_score": accuracy_score,
            "bias_status": bias_status,
            "time": t_duration,
            "md5": get_file_hash(output_path),
            "thresh": cfg["thresh"],
            "scenario": name,
            "filename": sample,
            "aac": os.path.basename(output_path),
            "decode_error": decode_err if not valid else None,
            "prov_hash": prov_hash
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
        extra_args=None,
        gate=False,
        build_dir=None,
        throughput_only=False,
        rate_control="abr"):
    env = os.environ.copy()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sec_sizes = get_elf_section_sizes(lib_path)
    exact_rom_size = sec_sizes.get("text", 0) + sec_sizes.get("rodata", 0) + sec_sizes.get("data", 0)
    if exact_rom_size == 0:
        exact_rom_size = get_binary_size(lib_path)

    results = {
        "sha": sha,
        "faac_git_sha": os.environ.get("FAAC_GIT_SHA"),
        "faac_precision": os.environ.get("FAAC_PRECISION"),
        "faac_args": " ".join(extra_args) if extra_args else "",
        "matrix": {},
        "throughput": {},
        "throughput_samples": {},
        "lib_size": exact_rom_size,
        "lib_text_size": sec_sizes.get("text", 0),
        "lib_rodata_size": sec_sizes.get("rodata", 0),
        "lib_bss_size": sec_sizes.get("bss", 0),
        "lib_data_size": sec_sizes.get("data", 0),
        # Gate inputs. get_section_sizes is the portable reader: on macOS
        # get_elf_section_sizes falls back to whole-file size as "text", which
        # is the very number a section sum exists to avoid.
        "lib_sections": get_section_sizes(lib_path),
        "frontend_size": get_binary_size(faac_bin_path),
        "object_text": get_object_sizes(build_dir) if build_dir else {},
        "toolchain_fp": get_toolchain_fp(build_dir),
        "host_fp": get_host_fp(),
    }

    if run_perceptual and not throughput_only:
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

            if len(filtered_samples) == 0:
                print(f"  [Scenario: {name}] No samples found.")
                continue

            if gate:
                samples = gate_filter(name, filtered_samples)
                print(f"  [Scenario: {name}] Gate subset: {len(samples)} clip(s).")
            else:
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
                        lib_path,
                        name,
                        cfg,
                        sample,
                        data_dir,
                        precision,
                        env,
                        extra_args,
                        rate_control): sample for sample in samples}
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

                    # Interference is one-sided: a run can be slowed by another
                    # process but never sped up, so the minimum is the
                    # low-variance estimator of the true cost and the mean just
                    # imports whatever else the runner was doing.
                    durations = []
                    for _ in range(TP_REPS):
                        start_time = time.perf_counter()
                        subprocess.run([faac_bin_path,
                                        "-o",
                                        output_path,
                                        input_path],
                                       env=env,
                                       check=True,
                                       capture_output=True)
                        durations.append(time.perf_counter() - start_time)

                    best = min(durations)
                    results["throughput"][sample] = best
                    # Keep every sample. Without them there is no dispersion to
                    # test, and a gate cannot be built on a bare mean.
                    results["throughput_samples"][sample] = durations
                    overall_durations.append(best)
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
    # Phase 1 does not compute MOS -- phase 2 does -- so what this actually
    # controls is whether the corpus is encoded at all. --skip-encode says that;
    # --skip-mos is kept as an alias because callers already pass it.
    parser.add_argument("--skip-encode", "--skip-mos", dest="skip_encode",
                        action="store_true",
                        help="Skip the encode matrix (footprint- or throughput-only runs)")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--sha", help="Commit SHA")
    parser.add_argument("--scenarios", help="Comma-separated scenarios")
    parser.add_argument("--include-tests", help="Comma-separated include globs")
    parser.add_argument("--exclude-tests", help="Comma-separated exclude globs")
    parser.add_argument("--extra-args", nargs="*", help="Extra arguments to pass to faac encoder (e.g. '--tns')")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset (config.GATE_CLIPS)")
    parser.add_argument("--rate-control", choices=["abr", "vbr"], default="abr", help="Rate control mode (abr or vbr)")
    parser.add_argument("--build-dir", help="Meson build directory, for per-object sizes and toolchain identity")
    parser.add_argument("--throughput-only", action="store_true",
                        help="Measure only throughput and merge it into an existing output JSON. "
                             "For refreshing a cached baseline's timings on the machine that will "
                             "measure the candidate, so the two are comparable.")

    args, unknown = parser.parse_known_args()

    # Combine explicit extra args and any unknown args
    extra_args_list = []
    if args.extra_args:
        # If passed via --extra-args="--flag1 --flag2", it might be a single string in the list
        for arg in args.extra_args:
            extra_args_list.extend(arg.split())
    if unknown:
        for arg in unknown:
            extra_args_list.extend(arg.split())

    extra_args = extra_args_list if extra_args_list else None
    data = run_benchmark(
        args.faac_bin,
        args.lib_path,
        args.precision,
        coverage=args.coverage,
        run_perceptual=not args.skip_encode,
        sha=args.sha,
        scenarios=args.scenarios,
        include_tests=args.include_tests,
        exclude_tests=args.exclude_tests,
        extra_args=extra_args,
        gate=args.gate,
        build_dir=args.build_dir,
        throughput_only=args.throughput_only,
        rate_control=args.rate_control)

    # Ensure results directory exists
    output_json = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_json), exist_ok=True)

    # Refreshing timings must not discard the MOS matrix the cached run paid
    # for. Rather than list the keys to refresh -- a list that goes stale the
    # moment a field is added, silently leaving it cached -- keep the matrix and
    # let everything measured this run overwrite its cached counterpart.
    # Falsy values do not overwrite, so a field this run did not produce (an
    # unset --sha, say) keeps what the cached run recorded.
    if args.throughput_only and os.path.exists(output_json):
        with open(output_json) as f:
            merged = json.load(f)
        for key, value in data.items():
            if key == "matrix" or not value:
                continue
            merged[key] = value
        data = merged

    with open(output_json, "w") as f:
        json.dump(data, f, indent=2)
