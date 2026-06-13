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
import subprocess
import argparse
import platform
import hashlib
import shutil
import copy

from utils import calculate_provenance_hash, get_git_tag

def calculate_docker_hash(script_dir):
    """Calculates a hash of the files used to build the ViSQOL Docker image."""
    files_to_hash = [
        "Dockerfile.visqol",
        "config.py",
        "phase2_mos.py"
    ]
    hasher = hashlib.sha256()
    for fname in sorted(files_to_hash):
        fpath = os.path.join(script_dir, fname)
        if os.path.exists(fpath):
            with open(fpath, "rb") as f:
                # Hash the filename and content
                hasher.update(fname.encode())
                hasher.update(f.read())
    return hasher.hexdigest()[:12]

def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite")
    parser.add_argument("faac_bin", help="Path to faac binary")
    parser.add_argument("lib_path", help="Path to libfaac.so")
    parser.add_argument("name", help="Name for this run")
    parser.add_argument("output", help="Output JSON path")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip perceptual quality (MOS) computation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo image (inter-channel coherence) computation")
    parser.add_argument("--visqol-image", help="Override the ViSQOL Docker image to use")
    parser.add_argument("--sha", help="Commit SHA to associate with these results")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--include-tests", help="Comma-separated list of test filename globs to include")
    parser.add_argument("--exclude-tests", help="Comma-separated list of test filename globs to exclude")
    parser.add_argument("--extra-args", nargs="*", help="Extra arguments to pass to faac encoder (e.g. '--tns')")
    parser.add_argument("--backend", choices=["auto", "docker", "visqol", "visqol-py", "visqol-python"],
                        default="auto", help="ViSQOL backend to use")
    parser.add_argument("--compare", nargs="+", help="A/B comparison mode: 'A:--args' 'B:--args'")
    parser.add_argument("--sweep", help="Parameter sweep mode: 'ENV_VAR=val1,val2,val3'")
    parser.add_argument("--faac-git-sha", help="Provenance: FAAC Git SHA")
    parser.add_argument("--faac-precision", help="Provenance: FAAC Build Precision")
    parser.add_argument("--diff", nargs=2, help="Standalone diff of two result JSONs")

    args, unknown = parser.parse_known_args()

    if args.diff:
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "compare_clips.py"), args.diff[0], args.diff[1]])
        return

    # Combine explicit extra args and any unknown args (which might be hyphenated flags)
    extra_args_list = []
    if args.extra_args:
        extra_args_list.extend(args.extra_args)
    if unknown:
        extra_args_list.extend(unknown)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    phase1_script = os.path.join(script_dir, "phase1_encode.py")
    phase2_script = os.path.join(script_dir, "phase2_mos.py")
    phase3_script = os.path.join(script_dir, "phase3_stereo.py")

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
        var, vals = args.sweep.split("=", 1)
        for val in vals.split(","):
            runs.append({"tag": val, "extra_args": extra_args_list, "output": args.output.replace(".json", f"_{val}.json"), "env": {var: val}})
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
        if args.faac_precision: run_env["FAAC_PRECISION"] = args.faac_precision

        # Phase 1: Encoding
        print(">>> Phase 1: Encoding and Basic Metrics")
        cmd_phase1 = [
            sys.executable, phase1_script,
            args.faac_bin, args.lib_path, run["tag"], run["output"],
            "--coverage", str(args.coverage)
        ]
        if args.sha:
            cmd_phase1.extend(["--sha", args.sha])
        if args.scenarios:
            cmd_phase1.extend(["--scenarios", args.scenarios])
        if args.include_tests:
            cmd_phase1.extend(["--include-tests", args.include_tests])
        if args.exclude_tests:
            cmd_phase1.extend(["--exclude-tests", args.exclude_tests])
        if run["extra_args"]:
            cmd_phase1.append(f"--extra-args={' '.join(run['extra_args'])}")

        subprocess.run(cmd_phase1, env=run_env, check=True)

        # Phase 2: MOS
        if args.skip_mos:
            print(">>> Skipping Phase 2 as requested.")
        else:
            print(">>> Phase 2: Perceptual Quality (MOS)")

            selected_backend = args.backend

            # Detection logic
            has_visqol_bin = False
            visqol_bin = os.environ.get("VISQOL_BIN") or shutil.which("visqol")
            if visqol_bin or os.path.exists("/app/visqol/bazel-bin/visqol"):
                has_visqol_bin = True

            has_visqol_python = False
            try:
                from visqol import VisqolApi
                has_visqol_python = True
            except ImportError:
                pass

            has_visqol_py = False
            try:
                import visqol_py
                has_visqol_py = True
            except ImportError:
                pass

            container_tool = None
            for tool in ["docker", "podman"]:
                try:
                    subprocess.run([tool, "--version"], check=True, capture_output=True)
                    container_tool = tool
                    break
                except (subprocess.CalledProcessError, FileNotFoundError):
                    continue

            # Auto-selection logic
            if selected_backend == "auto":
                if has_visqol_python:
                    selected_backend = "visqol-python"
                elif has_visqol_bin:
                    selected_backend = "visqol"
                elif container_tool:
                    selected_backend = "docker"
                elif has_visqol_py:
                    selected_backend = "visqol-py"
                else:
                    print(">>> ERROR: No ViSQOL backend found.")
                    sys.exit(1)

            if selected_backend != "docker":
                print(f"Using local ViSQOL backend: {selected_backend}")
                cmd_phase2 = [
                    sys.executable, phase2_script,
                    run["output"],
                    os.path.join(script_dir, "output"),
                    os.path.join(script_dir, "data", "external"),
                    "--backend", selected_backend,
                    "--faac-bin", args.faac_bin,
                    "--lib-path", args.lib_path
                ]
                if run["extra_args"]:
                    cmd_phase2.append(f"--extra-args={' '.join(run['extra_args'])}")
                subprocess.run(cmd_phase2, check=True)
            else:
                # Strategy 3: Container (Docker/Podman)
                print(f"Using container strategy with {container_tool}...")

                visqol_image = args.visqol_image or os.environ.get("VISQOL_IMAGE")
                image_name = "ghcr.io/nschimme/faac-benchmark-visqol"
                git_tag = get_git_tag()
                content_hash = calculate_docker_hash(script_dir)

                if not visqol_image:
                    preferred_tag = git_tag or content_hash
                    visqol_image = f"{image_name}:{preferred_tag}"

                try:
                    print(f"Checking for ViSQOL image: {visqol_image}")
                    pull_success = False
                    inspect_cmd = [container_tool, "inspect", "--type=image", visqol_image]
                    if subprocess.run(inspect_cmd, capture_output=True).returncode == 0:
                        print(f"Found image {visqol_image} locally.")
                        pull_success = True
                    else:
                        print(f"Image not found locally. Attempting to pull {visqol_image}...")
                        pull_cmd = [container_tool, "pull", "--platform", "linux/amd64", visqol_image]
                        if subprocess.run(pull_cmd).returncode == 0:
                            pull_success = True

                    if not pull_success:
                        print(f"Building {image_name} locally...")
                        build_tags = [f"{image_name}:{content_hash}", f"{image_name}:latest"]
                        if git_tag:
                            build_tags.append(f"{image_name}:{git_tag}")

                        build_cmd = [
                            container_tool, "build", "--platform", "linux/amd64",
                            "-f", os.path.join(script_dir, "Dockerfile.visqol")
                        ]
                        for tag in build_tags:
                            build_cmd.extend(["-t", tag])
                        build_cmd.append(script_dir)
                        subprocess.run(build_cmd, check=True)
                        if not args.visqol_image and not os.environ.get("VISQOL_IMAGE"):
                            visqol_image = f"{image_name}:{content_hash}"

                    print(f"Running MOS computation in {container_tool} (forcing amd64)...")
                    abs_output = os.path.abspath(run["output"])
                    abs_results_dir = os.path.dirname(abs_output)
                    results_file = os.path.basename(abs_output)
                    abs_output_dir = os.path.abspath(os.path.join(script_dir, "output"))
                    abs_data_dir = os.path.abspath(os.path.join(script_dir, "data", "external"))

                    cmd_container = [
                        container_tool, "run", "--rm", "--platform", "linux/amd64",
                        "-v", f"{abs_results_dir}:/results",
                        "-v", f"{abs_output_dir}:/output",
                        "-v", f"{abs_data_dir}:/data",
                        visqol_image, f"/results/{results_file}", "/output", "/data",
                        "--backend", "auto"
                    ]
                    subprocess.run(cmd_container, check=True)
                except subprocess.CalledProcessError as e:
                    print(f">>> ERROR: {container_tool} execution failed: {e}")
                    sys.exit(1)

        # Phase 3: Stereo image fidelity
        if not args.skip_stereo:
            print(">>> Phase 3: Stereo Image Fidelity (inter-channel coherence)")
            subprocess.run([
                sys.executable, phase3_script,
                run["output"],
                os.path.join(script_dir, "output"),
                os.path.join(script_dir, "data", "external"),
            ], check=True)

        print(f">>> Benchmark run {run['tag']} complete.")
        run_results.append(run)

        # Post-run comparison for Compare/Sweep
        if (args.compare or args.sweep) and len(run_results) >= 2:
            print("\n>>> Intermediate Comparison Results:")
            base_run = run_results[0]
            subprocess.run([sys.executable, os.path.join(script_dir, "compare_clips.py"), base_run["output"], run["output"]])

    print("\n>>> All benchmarks complete.")

if __name__ == "__main__":
    main()
