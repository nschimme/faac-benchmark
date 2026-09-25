"""
 * FAAC Benchmark Suite
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
import subprocess
import argparse
import platform
import hashlib
import shutil
import copy

from utils import calculate_provenance_hash, get_git_tag
from codec_bench.decoders import get_decoder_instance

def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite")
    parser.add_argument("name", help="Name for this run")
    parser.add_argument("output", help="Output JSON path")
    parser.add_argument("--encoder", default="faac", help="Encoder type: faac, ffmpeg, fdkaac, aac_enc, falabaac, afconvert, opus, lame")
    parser.add_argument("--encoder-bin", "--faac-bin", dest="encoder_bin", help="Path to encoder binary")
    parser.add_argument("--encoder-lib", "--lib-path", dest="encoder_lib", help="Path to encoder shared library override")
    parser.add_argument("--decoder", default="ffmpeg", help="Decoder type: ffmpeg, faad, fdkdec, helix, afconvert")
    parser.add_argument("--decoder-bin", help="Path to decoder binary")
    parser.add_argument("--decoder-lib", help="Path to decoder shared library override")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip perceptual quality (MOS) computation")
    parser.add_argument("--skip-encode", action="store_true",
                        help="Skip the encode matrix entirely (footprint-only runs)")
    parser.add_argument("--throughput-only", action="store_true",
                        help="Measure only throughput and merge into an existing output JSON")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo image (inter-channel coherence) computation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip transient fidelity (attack-centroid-shift) computation")
    parser.add_argument("--sha", help="Commit SHA to associate with these results")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--include-tests", help="Comma-separated list of test filename globs to include")
    parser.add_argument("--exclude-tests", help="Comma-separated list of test filename globs to exclude")
    parser.add_argument("--extra-args", nargs="*", help="Extra arguments to pass to faac encoder (e.g. '--tns')")
    parser.add_argument("--compare", nargs="+", help="A/B comparison mode: 'A:--args' 'B:--args'")
    parser.add_argument("--sweep", help="Parameter sweep mode: 'KEY=v1,v2,...' where KEY is a faac flag (e.g. --pns) or an env var")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset (config.GATE_CLIPS)")
    parser.add_argument("--rate-control", choices=["abr", "vbr", "cbr"], default="abr",
                        help="Rate control mode: abr (-b), vbr (-q), or cbr (-b --cbr: bit reservoir, exact rate)")
    parser.add_argument("--build-dir", help="Meson build directory, for per-object sizes and toolchain identity")
    parser.add_argument("--faac-git-sha", help="Provenance: FAAC Git SHA")
    parser.add_argument("--diff", nargs=2, help="Standalone diff of two result JSONs")

    args, unknown = parser.parse_known_args()

    decoder_inst = get_decoder_instance(args.decoder, binary_path=args.decoder_bin, lib_override=args.decoder_lib)

    # A bare filename (no directory component) is an ad hoc/manual run --
    # default it under results/ instead of littering the repo root. Callers
    # that already pass a path with a directory (CI's action.yml always does,
    # e.g. "./results/base.json") are untouched.
    if not os.path.dirname(args.output):
        os.makedirs("results", exist_ok=True)
        args.output = os.path.join("results", args.output)

    if args.diff:
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "scripts", "compare_clips.py"), args.diff[0], args.diff[1]])
        return

    # Combine explicit extra args and any unknown args (which might be hyphenated flags)
    extra_args_list = []
    if args.extra_args:
        extra_args_list.extend(args.extra_args)
    if unknown:
        extra_args_list.extend(unknown)

    # A throughput refresh reuses the cached matrix, so scoring it again would
    # spend the ViSQOL time the cache exists to avoid.
    if args.throughput_only:
        args.skip_mos = True
        args.skip_encode = True

    script_dir = os.path.dirname(os.path.abspath(__file__))
    phase1_script = os.path.join(script_dir, "phase1_encode.py")
    phase2_script = os.path.join(script_dir, "phase2_mos.py")
    phase3_script = os.path.join(script_dir, "phase3_stereo.py")
    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(script_dir, "data", "external")

    # Logic for A/B or Sweep
    runs = []
    if args.compare:
        for item in args.compare:
            if ":" in item:
                tag, r_args = item.split(":", 1)
                runs.append({"tag": tag, "extra_args": r_args.split(), "output": args.output.replace(".json", f"_{tag}.json"), "env": {}})
            else:
                runs.append({"tag": item, "extra_args": extra_args_list, "output": args.output.replace(".json", f"_{item}.json"), "env": {}})
    elif args.sweep:
        if "=" not in args.sweep:
            print("Error: --sweep must be 'KEY=v1,v2,...' (KEY is an env var, or a faac flag like --pns).")
            sys.exit(1)
        key, vals = args.sweep.split("=", 1)
        # Bitrate is a scenario's identity (e.g. music_low == 64k); sweeping it
        # would mislabel results. Study bitrate ranges with dedicated scenarios.
        if key in ("-b", "--bitrate", "-q"):
            print(f"Error: cannot sweep '{key}' — bitrate/quality define a scenario. "
                  "Add a scenario at the target bitrate in config.py instead.")
            sys.exit(1)
        is_flag = key.startswith("-")
        for val in vals.split(","):
            tag = f"{key.lstrip('-')}{val}"
            run = {"tag": tag,
                   "extra_args": extra_args_list + ([key, val] if is_flag else []),
                   "output": args.output.replace(".json", f"_{tag}.json"),
                   "env": {} if is_flag else {key: val}}
            runs.append(run)
    else:
        runs.append({"tag": args.name, "extra_args": extra_args_list, "output": args.output, "env": {}})

    if not runs:
        print("Error: No runs defined.")
        sys.exit(1)

    run_results = []

    for run in runs:
        print(f"\n>>> Starting run: {run['tag']}")
        run_env = os.environ.copy()
        run_env.update(run["env"])
        if args.faac_git_sha: run_env["FAAC_GIT_SHA"] = args.faac_git_sha

        # Phase 1: Encoding
        print(">>> Phase 1: Encoding and Basic Metrics")
        cmd_phase1 = [
            sys.executable, phase1_script,
            run["tag"], run["output"],
            "--encoder", args.encoder,
            "--coverage", str(args.coverage),
            "--rate-control", args.rate_control
        ]
        if args.encoder_bin:
            cmd_phase1.extend(["--encoder-bin", args.encoder_bin])
        if args.encoder_lib:
            cmd_phase1.extend(["--encoder-lib", args.encoder_lib])
        if args.sha:
            cmd_phase1.extend(["--sha", args.sha])
        if args.scenarios:
            cmd_phase1.extend(["--scenarios", args.scenarios])
        if args.include_tests:
            cmd_phase1.extend(["--include-tests", args.include_tests])
        if args.exclude_tests:
            cmd_phase1.extend(["--exclude-tests", args.exclude_tests])
        if args.gate:
            cmd_phase1.append("--gate")
        # --skip-mos means "do not score", not "do not encode": the e2e run and
        # any bitstream-only check need a matrix without paying for ViSQOL. Only
        # --skip-encode suppresses the corpus, which is what a footprint- or
        # throughput-only run wants.
        if args.skip_encode:
            cmd_phase1.append("--skip-encode")
        # Refreshes a cached baseline's timings on the machine that is about to
        # measure the candidate. Implies --skip-mos: the matrix is already on
        # disk and re-encoding it would defeat the point of the cache.
        if args.throughput_only:
            cmd_phase1.append("--throughput-only")
        if args.build_dir:
            cmd_phase1.extend(["--build-dir", args.build_dir])
        if run["extra_args"]:
            cmd_phase1.append(f"--extra-args={' '.join(run['extra_args'])}")

        subprocess.run(cmd_phase1, env=run_env, check=True)

        # Phase 2: MOS
        if args.skip_mos:
            print(">>> Skipping Phase 2 as requested.")
        else:
            print(f">>> Phase 2: Perceptual Quality (MOS) using decoder: {decoder_inst.name}")
            cmd_phase2 = [
                sys.executable, phase2_script,
                run["output"],
                os.path.join(script_dir, "output"),
                external_data_dir,
                "--decoder", args.decoder
            ]
            if args.encoder_bin and args.encoder_lib:
                cmd_phase2.extend(["--faac-bin", args.encoder_bin, "--lib-path", args.encoder_lib])
            if args.decoder_bin:
                cmd_phase2.extend(["--decoder-bin", args.decoder_bin])
            if args.decoder_lib:
                cmd_phase2.extend(["--decoder-lib", args.decoder_lib])
            if run["extra_args"]:
                cmd_phase2.append(f"--extra-args={' '.join(run['extra_args'])}")
            subprocess.run(cmd_phase2, env=run_env, check=True)

        # Phase 3: Stereo image fidelity + transient fidelity.
        if not args.skip_encode and not (args.skip_stereo and args.skip_transient):
            print(f">>> Phase 3: Stereo Image Fidelity + Transient Fidelity using decoder: {decoder_inst.name}")
            cmd_phase3 = [
                sys.executable, phase3_script,
                run["output"],
                os.path.join(script_dir, "output"),
                external_data_dir,
                "--decoder", args.decoder
            ]
            if args.decoder_bin:
                cmd_phase3.extend(["--decoder-bin", args.decoder_bin])
            if args.decoder_lib:
                cmd_phase3.extend(["--decoder-lib", args.decoder_lib])
            if args.skip_stereo:
                cmd_phase3.append("--skip-stereo")
            if args.skip_transient:
                cmd_phase3.append("--skip-transient")
            subprocess.run(cmd_phase3, check=True)

        # Update JSON with decoder metadata
        if os.path.exists(run["output"]):
            try:
                with open(run["output"], "r") as f:
                    res_data = json.load(f)
                res_data["decoder_name"] = decoder_inst.name
                res_data["decoder_version"] = getattr(decoder_inst, "tool_id", "unknown")
                with open(run["output"], "w") as f:
                    json.dump(res_data, f, indent=2)
            except Exception as e:
                print(f"Warning: Failed to update decoder metadata in {run['output']}: {e}")


        print(f">>> Benchmark run {run['tag']} complete.")
        run_results.append(run)

        # Post-run comparison for Compare/Sweep
        if (args.compare or args.sweep) and len(run_results) >= 2:
            print("\n>>> Intermediate Comparison Results:")
            base_run = run_results[0]
            subprocess.run([sys.executable, os.path.join(script_dir, "scripts", "compare_clips.py"), base_run["output"], run["output"]])

    print("\n>>> All benchmarks complete.")

if __name__ == "__main__":
    main()
