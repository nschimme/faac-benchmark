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
import hashlib

def calculate_docker_hash(script_dir):
    """Calculates a hash of the files used to build the ViSQOL Docker image."""
    files_to_hash = [
        "Dockerfile.visqol",
        "config.py",
        "phase2_mos.py",
        "requirements.txt",
        "requirements_visqol.txt"
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

def get_git_tag():
    """Returns the current git tag if exactly on a tag, else None."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite")
    parser.add_argument("faac_bin", help="Path to faac binary")
    parser.add_argument("lib_path", help="Path to libfaac.so")
    parser.add_argument("name", help="Name for this run")
    parser.add_argument("output", help="Output JSON path")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip perceptual quality (MOS) computation")
    parser.add_argument("--visqol-image", help="Override the ViSQOL Docker image to use")

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

        # Docker Image Logic:
        # 1. Use --visqol-image if provided.
        # 2. Use VISQOL_IMAGE env var if set.
        # 3. Otherwise, use ghcr.io/nschimme/faac-benchmark-visqol with a tag.
        #    - Tag priority: Git tag > Content Hash > latest.

        visqol_image = args.visqol_image or os.environ.get("VISQOL_IMAGE")

        image_name = "ghcr.io/nschimme/faac-benchmark-visqol"
        git_tag = get_git_tag()
        content_hash = calculate_docker_hash(script_dir)

        if not visqol_image:
            # Determine the primary tag we want to use/pull
            preferred_tag = git_tag or content_hash
            visqol_image = f"{image_name}:{preferred_tag}"

        try:
            # Try to see if we have it locally or can pull it
            print(f"Checking for ViSQOL image: {visqol_image}")
            pull_success = False

            # Check if it exists locally first
            inspect_cmd = [container_tool, "inspect", "--type=image", visqol_image]
            if subprocess.run(inspect_cmd, capture_output=True).returncode == 0:
                print(f"Found image {visqol_image} locally.")
                pull_success = True
            else:
                # Try to pull
                print(f"Image not found locally. Attempting to pull {visqol_image}...")
                pull_cmd = [container_tool, "pull", "--platform", "linux/amd64", visqol_image]
                if subprocess.run(pull_cmd).returncode == 0:
                    pull_success = True
                else:
                    print(f"Could not pull {visqol_image}.")

            if not pull_success:
                # Fallback: Build locally
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
                # If we were looking for a specific image and build succeeded,
                # we should use the one we just built (which will be one of the tags)
                if not args.visqol_image and not os.environ.get("VISQOL_IMAGE"):
                    visqol_image = f"{image_name}:{content_hash}"

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
