"""
 * FAAC Benchmark Suite - Utilities
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import subprocess
import hashlib
import json
import sys

def safe_run(cmd, env=None, capture_output=True, check=True, shell=False):
    """Safe wrapper for subprocess.run."""
    try:
        return subprocess.run(
            cmd,
            env=env,
            capture_output=capture_output,
            check=check,
            text=True,
            shell=shell
        )
    except subprocess.CalledProcessError as e:
        if capture_output:
            print(f"Command failed: {' '.join(cmd)}")
            if e.stdout:
                print(f"STDOUT: {e.stdout}")
            if e.stderr:
                print(f"STDERR: {e.stderr}")
        raise e

def get_file_hash(path, algo="md5"):
    """Calculates the hash of a file."""
    if not os.path.exists(path):
        return ""

    if algo == "md5":
        hasher = hashlib.md5()
    elif algo == "sha256":
        hasher = hashlib.sha256()
    else:
        raise ValueError(f"Unsupported algorithm: {algo}")

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_binary_size(path):
    if os.path.exists(path):
        return os.path.getsize(path)
    return 0

def load_results(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_results(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

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

def get_ffmpeg_path():
    import shutil
    return shutil.which("ffmpeg")

def ffmpeg_probe(path):
    """Basic probe using ffprobe."""
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception:
        return None

def decode_validate(path):
    """Validates if an AAC file can be decoded by ffmpeg."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr
        return True, ""
    except Exception as e:
        return False, str(e)

def wav_conv(input_path, output_path, rate=None, channels=None):
    """Converts audio to WAV using ffmpeg."""
    cmd = ["ffmpeg", "-y", "-i", input_path]
    if rate:
        cmd.extend(["-ar", str(rate)])
    if channels:
        cmd.extend(["-ac", str(channels)])
    cmd.extend(["-sample_fmt", "s16", output_path])

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg conversion failed for {input_path}: {e.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"FFmpeg conversion failed for {input_path}: {e}", file=sys.stderr)
        return False

def calculate_provenance_hash(faac_bin, libfaac_so, extra_args, input_path):
    """Calculates a provenance hash for a specific encoding run."""
    hasher = hashlib.sha256()

    # Hash the binaries
    hasher.update(get_file_hash(faac_bin, "sha256").encode())
    hasher.update(get_file_hash(libfaac_so, "sha256").encode())

    # Hash the arguments
    if extra_args:
        if isinstance(extra_args, list):
            hasher.update(" ".join(extra_args).encode())
        else:
            hasher.update(extra_args.encode())

    # Hash the input file
    hasher.update(get_file_hash(input_path, "sha256").encode())

    return hasher.hexdigest()[:16]

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
