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
import subprocess
import argparse
import platform

def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite")
    parser.add_argument("faac_bin", help="Path to faac binary")
    parser.add_argument("lib_path", help="Path to libfaac.so")
    parser.add_argument("name", help="Name for this run")
    parser.add_argument("output", help="Output JSON path")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip perceptual quality (MOS) computation")

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    phase1_script = os.path.join(script_dir, "phase1_encode.py")
    phase2_script = os.path.join(script_dir, "phase2_mos.py")

    # Phase 1: Encoding
    print(">>> Phase 1: Encoding and Basic Metrics")
    cmd_phase1 = [
        sys.executable, phase1_script,
        args.faac_bin, args.lib_path, args.name, args.output,
        "--coverage", str(args.coverage)
    ]
    subprocess.run(cmd_phase1, check=True)

    if args.skip_mos:
        print(">>> Skipping Phase 2 as requested.")
        return

    # Phase 2: MOS
    print(">>> Phase 2: Perceptual Quality (MOS)")

    # Strategy 1: Local Python (check if requirements_visqol are met)
    try:
        import visqol_py
        print("Using local ViSQOL installation...")
        cmd_phase2 = [
            sys.executable, phase2_script,
            args.output,
            os.path.join(script_dir, "output"),
            os.path.join(script_dir, "data", "external")
        ]
        subprocess.run(cmd_phase2, check=True)
    except ImportError:
        # Strategy 2: Container (Docker/Podman)
        print("Local ViSQOL not found. Attempting container strategy...")

        container_tool = None
        for tool in ["docker", "podman"]:
            try:
                subprocess.run([tool, "--version"], check=True, capture_output=True)
                container_tool = tool
                break
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue

        if not container_tool:
            print(">>> ERROR: No container tool (docker/podman) found.")
            print("Please either:")
            print("  1. Install ViSQOL dependencies: pip install -r requirements_visqol.txt")
            print("  2. Install Docker or Podman and ensure the daemon/service is running.")
            print("  3. Run with --skip-mos if you only need encoding metrics.")
            sys.exit(1)

        # Use central image if it exists, otherwise fall back to local build
        visqol_image = os.environ.get("VISQOL_IMAGE", "faac-visqol")

        try:
            if visqol_image == "faac-visqol":
                # Build if needed
                print(f"Building faac-visqol image using {container_tool} (forcing amd64)...")
                subprocess.run([
                    container_tool, "build", "--platform", "linux/amd64", "-t", "faac-visqol", "-f",
                    os.path.join(script_dir, "Dockerfile.visqol"), script_dir
                ], check=True)
            else:
                print(f"Using central image: {visqol_image}")
                subprocess.run([container_tool, "pull", "--platform", "linux/amd64", visqol_image], check=True)

            # Run
            print(f"Running MOS computation in {container_tool} (forcing amd64)...")
            # We need absolute paths for volume mounting
            abs_output = os.path.abspath(args.output)
            abs_results_dir = os.path.dirname(abs_output)
            results_file = os.path.basename(abs_output)
            abs_output_dir = os.path.abspath(os.path.join(script_dir, "output"))
            abs_data_dir = os.path.abspath(os.path.join(script_dir, "data", "external"))

            cmd_container = [
                container_tool, "run", "--rm", "--platform", "linux/amd64",
                "-v", f"{abs_results_dir}:/results",
                "-v", f"{abs_output_dir}:/output",
                "-v", f"{abs_data_dir}:/data",
                visqol_image, f"/results/{results_file}", "/output", "/data"
            ]
            subprocess.run(cmd_container, check=True)

        except subprocess.CalledProcessError as e:
            print(f">>> ERROR: {container_tool} execution failed: {e}")
            sys.exit(1)

    print(">>> Benchmark complete.")

if __name__ == "__main__":
    main()
