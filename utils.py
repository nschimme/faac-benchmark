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

def find_linked_lib(binary_path, name_substr):
    """Resolve the on-disk path of a shared library linked into binary_path."""
    try:
        if sys.platform == "darwin":
            res = subprocess.run(["otool", "-L", binary_path], capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                line = line.strip()
                if name_substr in line:
                    lib_path = line.split(" ")[0]
                    if os.path.exists(lib_path):
                        return lib_path
        else:
            res = subprocess.run(["ldd", binary_path], capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                if name_substr in line and "=>" in line:
                    lib_path = line.split("=>")[1].strip().split(" ")[0]
                    if lib_path and os.path.exists(lib_path):
                        return lib_path
    except Exception:
        pass
    return None

def is_faac_legacy(faac_path, lib_override=None):
    """Check if faac binary is legacy 1.XX (lacks --object-type support)."""
    if not faac_path:
        return False
    env = None
    if lib_override:
        env = dict(os.environ)
        abs_lib = os.path.abspath(lib_override)
        lib_dir = os.path.dirname(abs_lib)
        if sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_INSERT_LIBRARIES"] = abs_lib
        else:
            env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            env["LD_PRELOAD"] = (abs_lib + " " + env.get("LD_PRELOAD", "")).strip()

    for help_flag in ["-H", "--help-advanced", "--help", "-h"]:
        try:
            res = safe_run([faac_path, help_flag], capture_output=True, check=False, env=env)
            stdout_stderr = (res.stdout or "") + (res.stderr or "")
            if "--object-type" in stdout_stderr:
                return False
        except Exception:
            pass

    lib_path = lib_override or find_linked_lib(faac_path, "libfaac")
    if lib_path and "libfaac.so.0" in lib_path:
        return True

    return True

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

def get_audio_es_bytes(path):
    """Calculates pure audio elementary stream payload size in bytes, excluding container metadata & padding.

    MP4/M4A containers add 1.5KB-2KB of ftyp/moov atom overhead. On short benchmark clips,
    container overhead distorts bitrate metrics. This uses ffprobe packet inspection
    to sum the exact audio payload bytes.
    """
    if not path or not os.path.exists(path):
        return 0

    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "packet=size", "-of", "json", path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        packets = data.get("packets", [])
        if packets:
            es_bytes = sum(int(p["size"]) for p in packets if "size" in p)
            if es_bytes > 0:
                return es_bytes
    except Exception:
        pass

    # Fallback to whole-file size if ffprobe packet inspection fails
    return os.path.getsize(path)

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

def _scenario_cfg(name_or_cfg):
    """Accepts a scenario name or an already-resolved cfg dict.

    A name absent from SCENARIOS (a retired scenario in an archived result
    JSON, say) is reconstructed from the name itself, so reports of old runs
    still show the right rate and channel count instead of the defaults.
    """
    if isinstance(name_or_cfg, dict):
        return name_or_cfg
    from config import SCENARIOS
    cfg = SCENARIOS.get(name_or_cfg)
    if cfg:
        return cfg
    rate, channels, bitrate = _parse_scenario_name(name_or_cfg or "")
    if not rate:
        return {}
    return {"rate": rate, "channels": channels, "bitrate": bitrate}


def get_corpus(cfg):
    """The CORPORA entry a scenario reads its content from."""
    from config import CORPORA
    cfg = _scenario_cfg(cfg)
    return CORPORA.get(cfg.get("corpus")) or {}


def corpus_dir(cfg, external_data_dir=None):
    """Absolute path of a scenario's reference-WAV directory.

    This replaces the `"speech" if cfg["mode"] == "speech" else "audio"`
    ternary that used to be repeated in every phase: sample rate and channel
    count are properties of the corpus, and `mode` now only selects the metric
    engine.
    """
    if external_data_dir is None:
        external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "external")
    subdir = get_corpus(cfg).get("dir", "audio")
    return os.path.join(external_data_dir, subdir)


def scenario_channels(cfg):
    cfg = _scenario_cfg(cfg)
    return cfg.get("channels") or get_corpus(cfg).get("channels", 2)


def scenario_rate(cfg):
    cfg = _scenario_cfg(cfg)
    return cfg.get("rate") or get_corpus(cfg).get("rate", 48000)


def scenario_family(name_or_cfg):
    """The rate/channel family a scenario belongs to (e.g. "44k1_stereo").

    Reports chart MOS against bitrate, which is only meaningful within one
    family -- two scenarios at 48 kbps and different sample rates are not two
    points on the same curve.
    """
    cfg = _scenario_cfg(name_or_cfg)
    fam = get_corpus(cfg).get("family")
    if fam:
        return fam
    # Unknown scenario (e.g. a stale result JSON): derive from the name.
    rate, channels, _ = _parse_scenario_name(
        name_or_cfg if isinstance(name_or_cfg, str) else "")
    if not rate:
        return "other"
    label = f"{rate // 1000}k" if rate % 1000 == 0 else f"{rate // 1000}k{(rate % 1000) // 100}"
    return f"{label}_{'mono' if channels == 1 else 'stereo'}"


def family_label(family):
    """Human label for a family, taken from whichever corpus declares it."""
    from config import CORPORA
    for c in CORPORA.values():
        if c.get("family") == family:
            # Strip the parenthetical qualifier so "16 kHz Mono Speech
            # (VoIP-degraded)" and the clean corpus share one section heading.
            return c.get("label", family).split(" (")[0]
    return family


def format_scenario_rate(cfg):
    """Compact "48k/2ch" tag for report tables."""
    rate = scenario_rate(cfg)
    khz = rate / 1000.0
    khz_s = f"{khz:g}k"
    return f"{khz_s}/{scenario_channels(cfg)}ch"


def select_corpus_clips(files, corpus):
    """Deterministically cap a corpus to `max_clips`, balanced across strata.

    The 400-clip TCD-VoIP set is 5 degradation types x ~20 conditions x 4
    talkers describing ONE configuration; taking a prefix would silently drop
    whole degradation types. Round-robin across the strata matched by the
    corpus's `strata` regex (falling back to an even-spaced stride when a
    filename doesn't match) keeps every stratum represented and the selection
    reproducible.
    """
    files = sorted(files)
    max_clips = (corpus or {}).get("max_clips")
    if not max_clips or len(files) <= max_clips:
        return files

    pattern = (corpus or {}).get("strata")
    if not pattern:
        step = len(files) / max_clips
        return [files[int(i * step)] for i in range(max_clips)]

    groups = {}
    rx = re.compile(pattern)
    for f in files:
        m = rx.search(f)
        groups.setdefault(m.groups() if m else ("_",), []).append(f)

    selected = []
    keys = sorted(groups, key=lambda k: tuple("" if p is None else p for p in k))
    idx = 0
    while len(selected) < max_clips:
        progressed = False
        for k in keys:
            if idx < len(groups[k]):
                selected.append(groups[k][idx])
                progressed = True
                if len(selected) == max_clips:
                    break
        if not progressed:
            break
        idx += 1
    return sorted(selected)


def expand_scenario_list(spec):
    """Parse a --scenarios value, expanding family names.

    With five rate families in the matrix, "run just the 44.1 kHz family" is
    the common iteration command, so a family name ("44k1_stereo") expands to
    every scenario in it. Anything else is passed through untouched so an
    unknown name still reaches the caller's own "not found" warning.
    """
    from config import SCENARIOS

    out = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token in SCENARIOS:
            out.append(token)
            continue
        members = [n for n in SCENARIOS if scenario_family(n) == token]
        if members:
            out.extend(sorted(members, key=get_scenario_sort_key))
        else:
            out.append(token)
    # Preserve order, drop duplicates (a family plus one of its members).
    seen = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def scenario_families(names):
    """The families present in `names`, in report order."""
    from config import FAMILY_ORDER
    present = {scenario_family(n) for n in names}
    ordered = [f for f in FAMILY_ORDER if f in present]
    return ordered + sorted(present - set(ordered))


def _parse_scenario_name(name):
    """(rate, channels, bitrate) parsed from a scenario name.

    Handles the decimal-rate form too: "44k1_stereo_128k" -> 44100 Hz. Used
    only as a fallback for names absent from SCENARIOS.
    """
    rate = 0
    channels = 0
    bitrate = 0

    m = re.match(r"^(\d+)k(\d)?_(mono|stereo|speech|audio)(?:_[a-z0-9]+)?_(\d+)k$", name)
    if m:
        rate = int(m.group(1)) * 1000 + (int(m.group(2)) * 100 if m.group(2) else 0)
        channels = 1 if m.group(3) in ("mono", "speech") else 2
        bitrate = int(m.group(4))
        return rate, channels, bitrate

    if "mono" in name or "speech" in name:
        channels = 1
    elif "stereo" in name or "audio" in name:
        channels = 2

    # Bitrate: the last number before a 'k'.
    m_br_list = re.findall(r"(\d+)k(?:$|_)", name)
    if m_br_list:
        bitrate = int(m_br_list[-1])

    m_rate = re.match(r"^(\d+)k(\d)?", name)
    if m_rate:
        rate = int(m_rate.group(1)) * 1000 + (int(m_rate.group(2)) * 100 if m_rate.group(2) else 0)

    return rate, channels, bitrate


@lru_cache(maxsize=128)
def get_scenario_sort_key(name):
    """Sortable key for a scenario: (family_rank, rate, channels, bitrate, name).

    Rate outranks bitrate deliberately. The old key sorted by bitrate first,
    which interleaved rate families -- 32k_stereo_48k would land next to
    48k_stereo_48k -- and made the scenario tables unscannable once more than
    one sample rate existed. Families now stay contiguous and ascend by rate.
    """
    try:
        from config import SCENARIOS, FAMILY_ORDER
    except ImportError:
        SCENARIOS = {}
        FAMILY_ORDER = []

    if name in SCENARIOS:
        cfg = SCENARIOS[name]
        rate = scenario_rate(cfg)
        channels = scenario_channels(cfg)
        bitrate = cfg.get("bitrate", 0)
        family = scenario_family(cfg)
    else:
        rate, channels, bitrate = _parse_scenario_name(name)
        family = scenario_family(name)

    family_rank = FAMILY_ORDER.index(family) if family in FAMILY_ORDER else len(FAMILY_ORDER)
    return (family_rank, rate, channels, bitrate, name)


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

    # Try exact match with preferred extensions (.m4a, .mp4, .aac, .opus, .mp3)
    for ext in [".m4a", ".mp4", ".aac", ".opus", ".mp3"]:
        target_filename = f"{key}{precision_suffix}{ext}"
        aac_path = os.path.join(aac_dir, target_filename)
        if os.path.exists(aac_path):
            return aac_path

    # Fallback to prefix matching (legacy results lacking a recorded "aac").
    # Sort for determinism so repeated runs at least resolve identically.
    if aac_files is None:
        try:
            aac_files = [f for f in os.listdir(aac_dir) if f.endswith((".m4a", ".mp4", ".aac", ".opus", ".mp3"))]
        except FileNotFoundError:
            return None

    matching = sorted(f for f in aac_files if f.startswith(key))
    if not matching:
        return None
    return os.path.join(aac_dir, matching[0])
