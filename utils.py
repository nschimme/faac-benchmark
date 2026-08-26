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
import re
import shutil
import tempfile
import math
from functools import lru_cache

import numpy as np

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

def get_elf_section_sizes(path):
    """.text/.rodata/.bss/.data, always four keys, zero when unreadable.

    Thin wrapper over get_section_sizes so there is exactly one section reader.
    The previous implementation was Linux-only and, whenever it could not read
    sections -- macOS, a missing tool, a non-ELF file -- reported the whole file
    size as "text". That is the number a section sum exists to avoid: it moves
    with symbol tables and padding, so a caller comparing it would see phantom
    changes and, worse, could not tell a real .text delta from one.

    Now it reports zeros instead, and callers that need a number when sections
    are unavailable fall back explicitly and visibly.
    """
    sizes = {"text": 0, "rodata": 0, "bss": 0, "data": 0}
    if not path:
        return sizes
    sizes.update(get_section_sizes(path))
    return sizes


# Whole-file size moves for reasons unrelated to code: symbol tables, .eh_frame,
# build IDs, section padding. Section sums do not, which is what makes them
# gateable. .rodata is in the sum because the next footprint regression is as
# likely to be a lookup table as a loop.
_SECTION_ALIASES = {
    "__text": "text", ".text": "text",
    "__const": "rodata", ".rodata": "rodata", "__cstring": "rodata",
    "__data": "data", ".data": "data",
    "__bss": "bss", ".bss": "bss", "__common": "bss",
}


def get_section_sizes(path):
    """Per-section byte counts, normalized across GNU and BSD size(1).

    Returns {} when the sizes cannot be read -- a caller that gates on these
    must treat an empty dict as "skip, loudly", never as "no change".
    """
    if not os.path.exists(path):
        return {}

    out = None
    # GNU binutils (Linux CI). -A prints "section size addr" per line.
    r = subprocess.run(["size", "-A", path], capture_output=True, text=True)
    if r.returncode == 0:
        out = [(f[0], f[1]) for f in
               (ln.split() for ln in r.stdout.splitlines())
               if len(f) >= 2 and f[1].isdigit()]
    else:
        # BSD size (macOS). -m prints "Section __text: 71800" for a linked
        # image but "Section (__TEXT, __text): 8024" for a relocatable object.
        r = subprocess.run(["size", "-m", path], capture_output=True, text=True)
        if r.returncode != 0:
            return {}
        out = re.findall(r"Section\s+(?:\([^,]+,\s*)?(__\w+)\)?:\s+(\d+)",
                         r.stdout)

    sizes = {}
    for name, size in out:
        key = _SECTION_ALIASES.get(name)
        if key:
            sizes[key] = sizes.get(key, 0) + int(size)

    if not sizes:
        # Berkeley format: a "text data bss dec hex" header then one row. No
        # .rodata column -- its bytes are folded into text -- so this is a
        # last resort for toolchains whose size(1) understands neither -A nor -m.
        r = subprocess.run(["size", path], capture_output=True, text=True)
        if r.returncode == 0:
            lines = r.stdout.splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 3 and all(p.lstrip("-").isdigit() for p in parts[:3]):
                    sizes = {"text": int(parts[0]), "data": int(parts[1]),
                             "bss": int(parts[2])}
    return sizes


def get_object_sizes(build_dir, target="libfaac"):
    """Per-object .text, so "the library grew" becomes "frame.c.o grew".

    Ungated context only. Finding the +13.9% regression of f94a81a8 took a
    manual bisection over nine commits; this is the one number that would have
    named the file directly.
    """
    if not build_dir or not os.path.isdir(build_dir):
        return {}

    sizes = {}
    for root, _, files in os.walk(build_dir):
        stem = os.path.basename(root)
        if target not in stem:
            continue
        for f in files:
            if not f.endswith(".o"):
                continue
            sec = get_section_sizes(os.path.join(root, f))
            if sec.get("text"):
                # Keyed by target too: the shared library is LTO'd, so its .p
                # holds a single fused lto.o and only the static archive's .p
                # attributes .text to a source file.
                sizes[f"{stem}/{f}"] = sec["text"]
    return sizes


def _boot_session():
    """Something that changes when the machine or its boot changes.

    Best effort: a degraded value costs a skipped gate, which is the safe
    direction. Never let this raise -- it runs on every benchmark.

    Linux and macOS get a real boot identifier. Anywhere else this degrades to
    the hostname, which still separates two machines -- the case that actually
    corrupted comparisons here -- but not two boots of one machine. Good enough
    while CI is Linux; revisit if a Windows runner ever gates throughput.
    """
    import platform
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["sysctl", "-n", "kern.boottime"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return f"{platform.node()}:{r.stdout.strip()}"
    except Exception:
        pass
    return platform.node() or "unknown"


def get_host_fp():
    """Identity of the machine *instance* that produced the timings.

    CPU model is not enough. A hosted CI fleet runs many identical VMs, and the
    baseline JSON -- timing samples included -- is restored from cache, so a
    candidate measured now can be compared against a baseline measured days ago
    on a different physical machine that merely shared a model name. That
    comparison produced 3% deltas on encodes verified bit-identical.

    So the fingerprint carries a boot session, which is unique per machine boot.
    Two runs that did not happen on the same booted host will not match, and the
    throughput gate skips rather than reporting the difference between two
    machines as a code change.
    """
    import platform
    fp = {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpus": str(os.cpu_count() or 0),
        "session": _boot_session(),
    }
    try:
        if platform.system() == "Darwin":
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                fp["cpu"] = r.stdout.strip()
        else:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        fp["cpu"] = line.split(":", 1)[1].strip()
                        break
    except Exception:
        pass
    return fp


def get_toolchain_fp(build_dir=None):
    """Identity of the toolchain that produced the binaries.

    A compiler or runner-image bump changes both size and timing with zero
    source change. Without this in the cache key, a stale baseline gets reused
    across that bump and the diff is attributed to the PR.
    """
    fp = {}

    cc = os.environ.get("CC") or "cc"
    r = subprocess.run([cc, "--version"], capture_output=True, text=True)
    if r.returncode == 0:
        fp["cc"] = r.stdout.splitlines()[0].strip()
    r = subprocess.run([cc, "-dumpmachine"], capture_output=True, text=True)
    if r.returncode == 0:
        fp["triple"] = r.stdout.strip()

    if build_dir:
        r = subprocess.run(["meson", "configure", build_dir],
                           capture_output=True, text=True)
        if r.returncode == 0:
            for key in ("buildtype", "b_lto", "default_library", "optimization",
                        "tuning"):
                m = re.search(rf"^\s*{key}\s+(\S+)", r.stdout, re.M)
                if m:
                    fp[key] = m.group(1)

    return fp

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
    return shutil.which("ffmpeg")

def get_faad_path():
    """Returns the path to faad binary if available, else None."""
    env_faad = os.environ.get("FAAD_BIN")
    if env_faad and os.path.exists(env_faad):
        return env_faad
    return shutil.which("faad")

def ffmpeg_probe(path):
    """Basic probe using ffprobe."""
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception:
        return None

def decode_validate(path):
    """Validates that an AAC file decodes cleanly with ffmpeg.

    ffmpeg exits 0 even when the decoder logs hard errors (e.g. a corrupt SBR
    payload prints "env_facs_q 255 is invalid" but the process still returns 0).
    So a returncode check alone silently passes broken bitstreams. With
    "-v error" ffmpeg only writes to stderr when something actually went wrong,
    so we treat ANY non-empty stderr as a failure too.
    """
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        err = (result.stderr or "").strip()
        if result.returncode != 0 or err:
            return False, err or f"ffmpeg exited {result.returncode}"
        return True, ""
    except Exception as e:
        return False, str(e)

def _wav_conv_faad(input_path, output_path, rate=None, channels=None):
    faad_bin = get_faad_path()
    if not faad_bin:
        return False

    with tempfile.TemporaryDirectory() as td:
        tmp_wav = os.path.join(td, "faad_out.wav")
        cmd = [faad_bin, "-q", "-o", tmp_wav, input_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(tmp_wav):
                return False

            try:
                import soundfile as sf
                import scipy.signal

                data, sr = sf.read(tmp_wav, dtype='float32', always_2d=True)
                needs_proc = False

                if channels is not None and data.shape[1] != channels:
                    needs_proc = True
                    if channels == 1:
                        data = data.mean(axis=1, keepdims=True)
                    elif channels == 2 and data.shape[1] == 1:
                        data = np.repeat(data, 2, axis=1)
                    elif channels == 2 and data.shape[1] > 2:
                        data = data[:, :2]

                if rate is not None and sr != rate:
                    needs_proc = True
                    g = math.gcd(rate, sr)
                    data = scipy.signal.resample_poly(data, rate // g, sr // g, axis=0)
                    sr = rate

                if needs_proc:
                    sf.write(output_path, data, sr, subtype='PCM_16')
                else:
                    shutil.move(tmp_wav, output_path)
                return True
            except Exception:
                if rate is None and channels is None:
                    shutil.move(tmp_wav, output_path)
                    return True
                return False
        except Exception:
            return False

def _wav_conv_ffmpeg(input_path, output_path, rate=None, channels=None):
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

def wav_conv(input_path, output_path, rate=None, channels=None):
    """Converts audio to WAV using FFmpeg (with FAAD2 fallback for AAC/M4A/MP4 files)."""
    if _wav_conv_ffmpeg(input_path, output_path, rate, channels):
        return True

    is_aac = input_path.lower().endswith((".aac", ".m4a", ".mp4"))
    if is_aac:
        if _wav_conv_faad(input_path, output_path, rate, channels):
            return True

    return False

def get_cached_ref_wav(cache_dir, ref_input_path, v_rate, v_channels):
    """Converts ref_input_path to a (v_rate, v_channels) WAV under cache_dir,
    reusing a prior conversion for the same (path, rate, channels) key.
    Safe under concurrent workers: conversion is written to a private temp
    file and atomically renamed into place."""
    key = hashlib.sha1(f"{ref_input_path}|{v_rate}|{v_channels}".encode()).hexdigest()
    cached_path = os.path.join(cache_dir, f"{key}.wav")
    if os.path.exists(cached_path):
        return cached_path

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=cache_dir)
    os.close(fd)
    if not wav_conv(ref_input_path, tmp_path, v_rate, v_channels):
        os.unlink(tmp_path)
        return None
    os.replace(tmp_path, cached_path)
    return cached_path

def encoder_env_signature(env=None):
    """Stable signature of the FAAC_*-prefixed env vars that can change the
    encode (used by --sweep ENV= runs). Both phase1 (encode) and phase2 (cache
    check) must compute this from the same environment or every swept clip
    would look stale; run_benchmark passes the run env to both phases."""
    if env is None:
        env = os.environ
    items = sorted((k, v) for k, v in env.items() if k.startswith("FAAC_"))
    return ";".join(f"{k}={v}" for k, v in items)

def calculate_provenance_hash(faac_bin, libfaac_so, extra_args, input_path, env=None):
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

    # Hash the FAAC_* env (env-var sweeps)
    hasher.update(encoder_env_signature(env).encode())

    # Hash the input file
    hasher.update(get_file_hash(input_path, "sha256").encode())

    return hasher.hexdigest()[:16]

@lru_cache(maxsize=128)
def get_scenario_sort_key(name):
    """Returns a sortable key for a scenario: (dataset_rank, bitrate, rate, name).
    Dataset rank: 0 for mono/speech, 1 for stereo/audio, 2 for others.
    Bitrate and rate are numeric.
    """
    try:
        from config import SCENARIOS
    except ImportError:
        SCENARIOS = {}

    dataset_rank = 2
    bitrate = 0
    rate = 0

    if name in SCENARIOS:
        cfg = SCENARIOS[name]
        mode = cfg.get("mode", "")
        if mode == "speech":
            dataset_rank = 0
        elif mode == "audio":
            dataset_rank = 1

        bitrate = cfg.get("bitrate", 0)
        rate = cfg.get("rate", 0)
    else:
        # Attempt to parse from name: e.g. "48k_stereo_128k"
        # Try full pattern
        m = re.match(r"^(\d+)k_(mono|stereo|speech|audio)_(\d+)k$", name)
        if m:
            rate = int(m.group(1)) * 1000
            mode_str = m.group(2)
            bitrate = int(m.group(3))
            if mode_str in ["mono", "speech"]:
                dataset_rank = 0
            elif mode_str in ["stereo", "audio"]:
                dataset_rank = 1
        else:
            # Heuristics
            if "mono" in name or "speech" in name:
                dataset_rank = 0
            elif "stereo" in name or "audio" in name:
                dataset_rank = 1

            # Extract bitrate (usually the last number before a 'k').
            # Use findall and take the last match to honor the "last number" heuristic.
            m_br_list = re.findall(r"(\d+)k(?:$|_)", name)
            if m_br_list:
                bitrate = int(m_br_list[-1])

            # Extract rate (usually the first number)
            m_rate = re.search(r"^(\d+)k", name)
            if m_rate:
                rate = int(m_rate.group(1)) * 1000

    return (dataset_rank, bitrate, rate, name)


def get_aac_path(key, aac_dir, results_path, aac_files=None, entry=None):
    # Preferred: the matrix entry records the exact .m4a/.mp4/.aac file phase1 produced for
    # this run/tag (phase1 writes "{key}_{precision}.m4a", precision == the run
    # tag). This is unambiguous and is essential for --compare/--sweep, where
    # every tag shares aac_dir: a bare key-prefix match below would return the
    # first matching file regardless of tag, silently scoring another variant's
    # bitstream (e.g. an HE run getting the LC encode's MOS).
    if entry:
        recorded = entry.get("aac")
        if recorded:
            cand = os.path.join(aac_dir, recorded)
            if os.path.exists(cand):
                return cand

    results_filename = os.path.basename(results_path)
    precision_suffix = ""
    if "_base.json" in results_filename:
        precision_suffix = "_base"
    elif "_cand.json" in results_filename:
        precision_suffix = "_cand"

    # Try exact match with preferred extensions (.m4a, .mp4, .aac)
    for ext in [".m4a", ".mp4", ".aac"]:
        target_filename = f"{key}{precision_suffix}{ext}"
        aac_path = os.path.join(aac_dir, target_filename)
        if os.path.exists(aac_path):
            return aac_path

    # Fallback to prefix matching (legacy results lacking a recorded "aac").
    # Sort for determinism so repeated runs at least resolve identically.
    if aac_files is None:
        try:
            aac_files = [f for f in os.listdir(aac_dir) if f.endswith((".m4a", ".mp4", ".aac"))]
        except FileNotFoundError:
            return None

    matching = sorted(f for f in aac_files if f.startswith(key))
    if not matching:
        return None
    return os.path.join(aac_dir, matching[0])
