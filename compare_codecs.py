"""
 * FAAC Benchmark Suite - Codec Comparison & Leaderboard
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
import time
import argparse
import subprocess
import shutil
import tempfile
import wave
import concurrent.futures
import re
from collections import defaultdict

from utils import (get_binary_size, get_elf_section_sizes, decode_validate, get_ffmpeg_path,
                   get_faad_path, ffmpeg_probe, get_scenario_sort_key, safe_run, find_linked_lib,
                   is_faac_legacy, resolve_wrapper_target, guess_lib_version_from_path,
                   corpus_dir, select_corpus_clips, scenario_channels, scenario_rate,
                   scenario_family, family_label, scenario_families, expand_scenario_list,
                   get_audio_es_bytes, is_system_library, flatten_arg_list, probe_version,
                   make_unique_name_and_id, format_size, make_progress_bar, zoomed_y_range,
                   compute_snr, wav_conv, hosted_codec_ver, measure_delay_offset,
                   measure_peak_ram, corrupt_adts_bitstream)
from config import SCENARIOS, CORPORA, FAMILY_ORDER, GATE_CLIPS, GATE_FALLBACK_N

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)
scripts_dir = os.path.join(SCRIPT_DIR, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

PROFILE_LABELS = {"lc": "LC", "he": "HE-v1", "hev2": "HE-v2", "standard": "Standard"}

def profile_label(profile):
    return PROFILE_LABELS[profile]

def encoder_row_key(encoder):
    """Stable identity key for an (encoder_tool, profile) combination."""
    return f"{encoder.tool_id}_{encoder.profile}"

def decoder_row_key(decoder):
    """Stable identity key for a decoder tool."""
    return f"{decoder.tool_id}"

row_key = encoder_row_key

CLIP_PEER_BUG_GAP = 0.75

def cell_peer_gap(clip_mos, rk, s_name, filename):
    if not filename:
        return None
    clip_scores = clip_mos.get((s_name, filename), {})
    this_mos = clip_scores.get(rk)
    peers = {other_rk: m for other_rk, m in clip_scores.items() if other_rk != rk}
    if this_mos is None or not peers:
        return None
    peer_avg = sum(peers.values()) / len(peers)
    if peer_avg - this_mos > CLIP_PEER_BUG_GAP:
        return (this_mos, peer_avg)
    return None

# -----------------------------------------------------------------------------
# ENCODER ABSTRACTIONS
# -----------------------------------------------------------------------------

class Encoder:
    def __init__(self, name, binary_path, tool_id, profile, lib_name_substr=None, lib_override=None):
        self.name = name
        self.binary_path = binary_path
        self.tool_id = tool_id
        self.profile = profile
        self.lib_override = lib_override

        measure_bin = resolve_wrapper_target(binary_path) if binary_path else binary_path
        lib_path = lib_override or (find_linked_lib(measure_bin, lib_name_substr) if lib_name_substr else None)

        measured_path = None
        if lib_path and not lib_override and is_system_library(lib_path):
            self.size = 0
        elif lib_path:
            self.size = get_binary_size(lib_path)
            measured_path = lib_path
        else:
            if is_system_library(measure_bin):
                self.size = 0
            else:
                self.size = get_binary_size(measure_bin) if measure_bin else 0
                measured_path = measure_bin if measure_bin and not is_system_library(measure_bin) else None

        sec_sizes = get_elf_section_sizes(measured_path) if measured_path else {"text": 0, "rodata": 0, "bss": 0, "data": 0}
        self.file_ext = ".m4a"
        self.text_size = sec_sizes.get("text", 0)
        self.rodata_size = sec_sizes.get("rodata", 0)
        self.bss_size = sec_sizes.get("bss", 0)
        self.data_size = sec_sizes.get("data", 0)

    def supports_scenario(self, bitrate_kbps, channels, sample_rate):
        if self.profile in ("he", "hev2") and sample_rate < 32000:
            return False, f"sample rate {sample_rate} Hz < 32 kHz required for SBR ({profile_label(self.profile)})"
        if self.profile == "hev2" and channels < 2:
            return False, f"HE-v2 (Parametric Stereo) requires >= 2 channels (got {channels})"
        return True, ""

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        raise NotImplementedError

    def get_run_env(self):
        if not self.lib_override:
            return {}
        env = dict(os.environ)
        abs_lib = os.path.abspath(self.lib_override)
        lib_dir = os.path.dirname(abs_lib)
        if sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_INSERT_LIBRARIES"] = abs_lib
        else:
            env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            env["LD_PRELOAD"] = (abs_lib + " " + env.get("LD_PRELOAD", "")).strip()
        return env

def use_he_aac(bitrate_kbps, channels, sample_rate):
    if sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / channels
    return 8 <= bitrate_per_ch <= 48

def use_he_v2_aac(bitrate_kbps, channels, sample_rate):
    if channels < 2 or sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / channels
    return 6 <= bitrate_per_ch <= 20

class FAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id, profile="lc", lib_override=None, legacy=False):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfaac", lib_override=lib_override)
        self.legacy = legacy

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.legacy:
            return [self.binary_path, "-w", "-b", str(bitrate_kbps), "--overwrite", "-o", output_path, input_path]
        object_type = "he-aac-v1" if self.profile == "he" else "lc"
        return [self.binary_path, "-b", str(bitrate_kbps), "--overwrite", "--object-type", object_type, "-o", output_path, input_path]

class FFmpegEncoder(Encoder):
    def __init__(self, name, binary_path, codec_name, tool_id=None, coder=None, profile="lc"):
        lib_name_substr = {
            "libfdk_aac": "libfdk-aac",
            "vo_aacenc": "vo-aacenc",
        }.get(codec_name)
        tid = tool_id or f"ffmpeg_{codec_name}"
        super().__init__(name, binary_path, tid, profile, lib_name_substr=lib_name_substr)
        self.codec_name = codec_name
        self.coder = coder

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        cmd = [self.binary_path, "-y", "-i", input_path, "-c:a", self.codec_name]
        if self.codec_name == "libfdk_aac":
            if self.profile == "he":
                cmd.extend(["-profile:a", "aac_he"])
            elif self.profile == "hev2":
                cmd.extend(["-profile:a", "aac_he_v2"])
        if self.codec_name == "aac" and self.coder:
            cmd.extend(["-aac_coder", self.coder])

        cmd.extend(["-b:a", f"{bitrate_kbps}k"])
        cmd.extend(["-ac", str(channels), output_path])
        return cmd

class FDKAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="fdkaac", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.profile == "hev2":
            profile = "29"
        elif self.profile == "he":
            profile = "5"
        else:
            profile = "2"
        cmd = [self.binary_path, "-p", profile, "-b", str(bitrate_kbps * 1000), "-m", "0"]
        cmd.extend(["-o", output_path, input_path])
        return cmd

class AACEncEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="aac_enc", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.profile == "hev2":
            aot = "29"
        elif self.profile == "he":
            aot = "5"
        else:
            aot = "2"
        return [self.binary_path, "-r", str(bitrate_kbps * 1000), "-t", aot, input_path, output_path]

class FalabaacEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="falabaac", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        return [self.binary_path, "-i", input_path, "-o", output_path, "-b", str(bitrate_kbps)]

class AFConvertEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="afconvert", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="AudioToolbox")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.profile == "hev2":
            codec = "aacp"
        elif self.profile == "he":
            codec = "aach"
        else:
            codec = "aac "
        return [self.binary_path, "-f", "m4af", "-d", codec, "-b", str(bitrate_kbps * 1000), "-q", "127", "-c", str(channels), input_path, output_path]

class OpusEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="opusenc", profile="standard", is_ffmpeg=False):
        lib_substr = None if not is_ffmpeg else "libopus"
        if not is_ffmpeg and not lib_substr:
            lib_substr = "libopus"
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=lib_substr)
        self.is_ffmpeg = is_ffmpeg
        self.file_ext = ".opus"

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.is_ffmpeg:
            return [self.binary_path, "-y", "-i", input_path, "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path]
        return [self.binary_path, "--bitrate", str(bitrate_kbps), input_path, output_path]

class LameEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="lame", profile="standard", is_ffmpeg=False):
        lib_substr = None if not is_ffmpeg else "libmp3lame"
        if not is_ffmpeg and not lib_substr:
            lib_substr = "libmp3lame"
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=lib_substr)
        self.is_ffmpeg = is_ffmpeg
        self.file_ext = ".mp3"

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.is_ffmpeg:
            return [self.binary_path, "-y", "-i", input_path, "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path]
        return [self.binary_path, "-b", str(bitrate_kbps), "-s", str(sample_rate / 1000.0), input_path, output_path]

# -----------------------------------------------------------------------------
# DECODER ABSTRACTIONS
# -----------------------------------------------------------------------------

class Decoder:
    def __init__(self, name, binary_path, tool_id, lib_name_substr=None, lib_override=None):
        self.name = name
        self.binary_path = binary_path
        self.tool_id = tool_id
        self.lib_override = lib_override

        measure_bin = resolve_wrapper_target(binary_path) if binary_path else binary_path
        lib_path = lib_override or (find_linked_lib(measure_bin, lib_name_substr) if lib_name_substr and measure_bin else None)

        measured_path = None
        if lib_path and not lib_override and is_system_library(lib_path):
            self.size = 0
        elif lib_path:
            self.size = get_binary_size(lib_path)
            measured_path = lib_path
        else:
            if is_system_library(measure_bin):
                self.size = 0
            else:
                self.size = get_binary_size(measure_bin) if measure_bin else 0
                measured_path = measure_bin if measure_bin and not is_system_library(measure_bin) else None

        sec_sizes = get_elf_section_sizes(measured_path) if measured_path else {"text": 0, "rodata": 0, "bss": 0, "data": 0}
        self.text_size = sec_sizes.get("text", 0)
        self.rodata_size = sec_sizes.get("rodata", 0)
        self.bss_size = sec_sizes.get("bss", 0)
        self.data_size = sec_sizes.get("data", 0)

    def get_decode_cmd(self, input_path, output_path):
        raise NotImplementedError

    def get_run_env(self):
        if not self.lib_override:
            return {}
        env = dict(os.environ)
        abs_lib = os.path.abspath(self.lib_override)
        lib_dir = os.path.dirname(abs_lib)
        if sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_INSERT_LIBRARIES"] = abs_lib
        else:
            env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            env["LD_PRELOAD"] = (abs_lib + " " + env.get("LD_PRELOAD", "")).strip()
        return env

class FAADDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="faad", lib_override=None):
        super().__init__(name, binary_path, tool_id, lib_name_substr="libfaad", lib_override=lib_override)

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, "-q", "-o", output_path, input_path]

class FFmpegDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="ffmpeg_aac"):
        super().__init__(name, binary_path, tool_id, lib_name_substr=None)

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, "-y", "-i", input_path, "-sample_fmt", "s16", output_path]

class AFConvertDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="afconvert"):
        super().__init__(name, binary_path, tool_id, lib_name_substr="AudioToolbox")

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, "-f", "WAVE", "-d", "LEI16", input_path, output_path]

# -----------------------------------------------------------------------------
# DETECTION ROUTINES
# -----------------------------------------------------------------------------

def probe_faac_version(faac_path, lib_override=None):
    if not faac_path or not os.path.exists(faac_path):
        return None
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

    tmp_dir = tempfile.mkdtemp(prefix="faac_ver_")
    try:
        wav_path = os.path.join(tmp_dir, "silence.wav")
        out_path = os.path.join(tmp_dir, "silence.m4a")
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        res = subprocess.run([faac_path, "-o", out_path, "--overwrite", wav_path],
                              capture_output=True, text=True, timeout=10, env=env)
        text = (res.stdout or "") + "\n" + (res.stderr or "")
        m = re.search(r"FAAC\s+v?(\d+\.\d+(?:\.\d+)*[a-z0-9.]*(?:\s+\([^)]+\))?)", text, re.IGNORECASE)
        return m.group(1).strip() if m else None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def probe_encoder_capability(encoder, bitrate_kbps=None, channels=2, sample_rate=44100):
    if bitrate_kbps is None:
        per_channel = {"he": 32, "hev2": 16}.get(encoder.profile, 64)
        bitrate_kbps = per_channel * channels

    tmp_dir = tempfile.mkdtemp(prefix="cap_probe_")
    try:
        wav_path = os.path.join(tmp_dir, "silence.wav")
        out_path = os.path.join(tmp_dir, "silence" + getattr(encoder, "file_ext", ".m4a"))
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(b"\x00\x00" * channels * sample_rate)

        cmd = encoder.get_encode_cmd(wav_path, out_path, bitrate_kbps, channels, sample_rate)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=encoder.get_run_env() or None)
        if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True, ""

        text = ((res.stderr or "") + "\n" + (res.stdout or "")).strip()
        reason = next((l for l in reversed(text.splitlines()) if l.strip()), f"exit code {res.returncode}")
        return False, reason
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def detect_encoders(args):
    encoders = []
    existing_names = set()
    existing_ids = set()

    faac_bins = flatten_arg_list(getattr(args, "faac_bin", None))
    faac_libs = flatten_arg_list(getattr(args, "faac_lib", None))
    faac_vers = flatten_arg_list(getattr(args, "faac_bin_version", None))
    if not faac_bins:
        which_faac = shutil.which("faac")
        if which_faac:
            faac_bins = [which_faac]

    for idx, f_bin in enumerate(faac_bins):
        f_lib = faac_libs[idx] if idx < len(faac_libs) else (faac_libs[0] if faac_libs else None)
        legacy = is_faac_legacy(f_bin, lib_override=f_lib)
        ver = faac_vers[idx] if idx < len(faac_vers) else None
        if not ver:
            ver = probe_version(f_bin, ["-H", "--help-advanced", "--help", "-h", "-v"],
                                [r"FAAC\s+v?(\d+\.\d+(?:\.\d+)*[a-z0-9.]*(?:\s+\([^)]+\))?)",
                                 r"version\s+(\d+\.\d+(?:\.\d+)*[a-z0-9.]*)"])
        if not ver:
            ver = probe_faac_version(f_bin, lib_override=f_lib)
        if not ver and legacy:
            ver = "1.x"
        name, tool_id = make_unique_name_and_id("FAAC", ver, "faac", existing_names, existing_ids)
        encoders.append(FAACEncoder(name, f_bin, tool_id, profile="lc", lib_override=f_lib, legacy=legacy))
        if not legacy:
            candidate = FAACEncoder(name, f_bin, tool_id, profile="he", lib_override=f_lib, legacy=legacy)
            ok, reason = probe_encoder_capability(candidate)
            if ok:
                encoders.append(candidate)

    ffmpeg_bins = flatten_arg_list(getattr(args, "ffmpeg_bin", None))
    if not ffmpeg_bins:
        which_ff = get_ffmpeg_path()
        if which_ff:
            ffmpeg_bins = [which_ff]

    for ff_bin in ffmpeg_bins:
        ver = probe_version(ff_bin, ["-version"], [r"ffmpeg\s+version\s+([^\s,]+)"])
        supports_nmr = False
        try:
            res = subprocess.run([ff_bin, "-h", "encoder=aac"], capture_output=True, text=True)
            supports_nmr = bool(re.search(r"\bnmr\b", res.stdout or ""))
        except Exception:
            pass

        name_aac, id_aac = make_unique_name_and_id("FFmpeg AAC", ver, "ffmpeg_aac", existing_names, existing_ids)
        encoders.append(FFmpegEncoder(name_aac, ff_bin, "aac", tool_id=id_aac, coder="twoloop"))

        if supports_nmr:
            name_nmr, id_nmr = make_unique_name_and_id("FFmpeg NMR", ver, "ffmpeg_aac_nmr", existing_names, existing_ids)
            encoders.append(FFmpegEncoder(name_nmr, ff_bin, "aac", tool_id=id_nmr, coder="nmr"))

        try:
            res = subprocess.run([ff_bin, "-encoders"], capture_output=True, text=True)
            stdout = res.stdout or ""
            if "libfdk_aac" in stdout:
                name_fdk, id_fdk = make_unique_name_and_id("FFmpeg FDK-AAC", hosted_codec_ver(ff_bin, "libfdk-aac", ver), "ffmpeg_libfdk_aac", existing_names, existing_ids)
                encoders.append(FFmpegEncoder(name_fdk, ff_bin, "libfdk_aac", tool_id=id_fdk, profile="lc"))
                for profile in ("he", "hev2"):
                    candidate = FFmpegEncoder(name_fdk, ff_bin, "libfdk_aac", tool_id=id_fdk, profile=profile)
                    ok, reason = probe_encoder_capability(candidate)
                    if ok:
                        encoders.append(candidate)
            if "vo_aacenc" in stdout:
                name_vo, id_vo = make_unique_name_and_id("VO-AAC", hosted_codec_ver(ff_bin, "vo-aacenc", ver), "ffmpeg_vo_aacenc", existing_names, existing_ids)
                encoders.append(FFmpegEncoder(name_vo, ff_bin, "vo_aacenc", tool_id=id_vo))
        except Exception:
            pass

    fdkaac_bins = flatten_arg_list(getattr(args, "fdkaac_bin", None))
    if not fdkaac_bins:
        which_fdk = shutil.which("fdkaac")
        if which_fdk:
            fdkaac_bins = [which_fdk]

    for fdk_bin in fdkaac_bins:
        ver = probe_version(fdk_bin, ["--version", "-v", "-h", "--help"],
                            [r"fdkaac\s+v?(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("fdkaac", ver, "fdkaac", existing_names, existing_ids)
        encoders.append(FDKAACEncoder(name, fdk_bin, tool_id=tool_id, profile="lc"))
        for profile in ("he", "hev2"):
            candidate = FDKAACEncoder(name, fdk_bin, tool_id=tool_id, profile=profile)
            ok, reason = probe_encoder_capability(candidate)
            if ok:
                encoders.append(candidate)

    aacenc_bins = flatten_arg_list(getattr(args, "aac_enc_bin", None))
    explicit_aacenc = bool(getattr(args, "aac_enc_bin", None))
    if not aacenc_bins and not fdkaac_bins and not explicit_aacenc:
        which_aacenc = shutil.which("aac-enc")
        if which_aacenc:
            aacenc_bins = [which_aacenc]

    for aacenc_bin in aacenc_bins:
        ver = probe_version(aacenc_bin, ["-h", "--help", "-v", "--version"],
                            [r"aac-enc\s+v?(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("aac-enc", ver, "aac_enc", existing_names, existing_ids)
        encoders.append(AACEncEncoder(name, aacenc_bin, tool_id=tool_id, profile="lc"))
        for profile in ("he", "hev2"):
            candidate = AACEncEncoder(name, aacenc_bin, tool_id=tool_id, profile=profile)
            ok, reason = probe_encoder_capability(candidate)
            if ok:
                encoders.append(candidate)

    falabaac_bins = flatten_arg_list(getattr(args, "falabaac_bin", None))
    if not falabaac_bins:
        which_falab = shutil.which("falabaac")
        if which_falab:
            falabaac_bins = [which_falab]

    for falab_bin in falabaac_bins:
        ver = probe_version(falab_bin, ["-h", "--help", "-v", "--version"],
                            [r"falabaac\s+v?(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("falabaac", ver, "falabaac", existing_names, existing_ids)
        encoders.append(FalabaacEncoder(name, falab_bin, tool_id=tool_id))

    afconvert_bins = flatten_arg_list(getattr(args, "afconvert_bin", None))
    if not afconvert_bins:
        which_afc = shutil.which("afconvert")
        if which_afc:
            afconvert_bins = [which_afc]

    for afc_bin in afconvert_bins:
        ver = probe_version(afc_bin, ["-h", "--help"], [r"afconvert\s+version\s+([^\s,]+)", r"version:?\s+([0-9.]+)"])
        name, tool_id = make_unique_name_and_id("Apple AAC", ver, "afconvert", existing_names, existing_ids)
        encoders.append(AFConvertEncoder(name, afc_bin, tool_id=tool_id, profile="lc"))
        for profile in ("he", "hev2"):
            candidate = AFConvertEncoder(name, afc_bin, tool_id=tool_id, profile=profile)
            ok, reason = probe_encoder_capability(candidate)
            if ok:
                encoders.append(candidate)

    if getattr(args, 'include_other_codecs', False):
        opus_bins = flatten_arg_list(getattr(args, "opusenc_bin", None))
        if not opus_bins:
            which_opus = shutil.which("opusenc")
            if which_opus:
                opus_bins = [which_opus]

        if opus_bins:
            for op_bin in opus_bins:
                ver = probe_version(op_bin, ["--version", "-V", "-h"], [r"opusenc\s+([^\s\n]+)", r"opus-tools\s+([^\s\n]+)"])
                name, tool_id = make_unique_name_and_id("Opus", ver, "opusenc", existing_names, existing_ids)
                encoders.append(OpusEncoder(name, op_bin, tool_id=tool_id, is_ffmpeg=False))
        elif ffmpeg_bins:
            ff_bin = ffmpeg_bins[0]
            try:
                res = subprocess.run([ff_bin, "-encoders"], capture_output=True, text=True)
                if "libopus" in (res.stdout or "") or "opus" in (res.stdout or ""):
                    ffmpeg_ver = probe_version(ff_bin, ["-version"], [r"ffmpeg\s+version\s+([^\s,]+)"])
                    name, tool_id = make_unique_name_and_id("Opus", hosted_codec_ver(ff_bin, "libopus", ffmpeg_ver), "ffmpeg_opus", existing_names, existing_ids)
                    encoders.append(OpusEncoder(name, ff_bin, tool_id=tool_id, is_ffmpeg=True))
            except Exception:
                pass

        lame_bins = flatten_arg_list(getattr(args, "lame_bin", None))
        if not lame_bins:
            which_lame = shutil.which("lame")
            if which_lame:
                lame_bins = [which_lame]

        if lame_bins:
            for lame_bin in lame_bins:
                ver = probe_version(lame_bin, ["--version", "-v"],
                                    [r"LAME\s+64bits?\s+version\s+([^\s]+)", r"LAME\s+32bits?\s+version\s+([^\s]+)", r"LAME\s+version\s+([0-9.]+)"])
                name, tool_id = make_unique_name_and_id("LAME", ver, "lame", existing_names, existing_ids)
                encoders.append(LameEncoder(name, lame_bin, tool_id=tool_id, is_ffmpeg=False))
        elif ffmpeg_bins:
            ff_bin = ffmpeg_bins[0]
            try:
                res = subprocess.run([ff_bin, "-encoders"], capture_output=True, text=True)
                if "libmp3lame" in (res.stdout or ""):
                    ffmpeg_ver = probe_version(ff_bin, ["-version"], [r"ffmpeg\s+version\s+([^\s,]+)"])
                    name, tool_id = make_unique_name_and_id("LAME", hosted_codec_ver(ff_bin, "libmp3lame", ffmpeg_ver), "ffmpeg_lame", existing_names, existing_ids)
                    encoders.append(LameEncoder(name, ff_bin, tool_id=tool_id, is_ffmpeg=True))
            except Exception:
                pass

    return encoders

def detect_decoders(args):
    decoders = []
    existing_names = set()
    existing_ids = set()

    faad_bins = flatten_arg_list(getattr(args, "faad_bin", None))
    faad_libs = flatten_arg_list(getattr(args, "faad_lib", None))
    faad_vers = flatten_arg_list(getattr(args, "faad_bin_version", None))

    if not faad_bins:
        which_faad = get_faad_path()
        if which_faad:
            faad_bins = [which_faad]

    for idx, f_bin in enumerate(faad_bins):
        f_lib = faad_libs[idx] if idx < len(faad_libs) else (faad_libs[0] if faad_libs else None)
        ver = faad_vers[idx] if idx < len(faad_vers) else None
        if not ver:
            ver = probe_version(f_bin, ["-h", "--help", "-v", "--version"],
                                [r"FAAD2\s+v?(\d+\.\d+(?:\.\d+)*)", r"Decoder\s+V?(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("FAAD2", ver, "faad", existing_names, existing_ids)
        decoders.append(FAADDecoder(name, f_bin, tool_id=tool_id, lib_override=f_lib))

    ffmpeg_bins = flatten_arg_list(getattr(args, "ffmpeg_bin", None))
    if not ffmpeg_bins:
        which_ff = get_ffmpeg_path()
        if which_ff:
            ffmpeg_bins = [which_ff]

    for ff_bin in ffmpeg_bins:
        ver = probe_version(ff_bin, ["-version"], [r"ffmpeg\s+version\s+([^\s,]+)"])
        name, tool_id = make_unique_name_and_id("FFmpeg AAC", ver, "ffmpeg_aac", existing_names, existing_ids)
        decoders.append(FFmpegDecoder(name, ff_bin, tool_id=tool_id))

        try:
            res = subprocess.run([ff_bin, "-decoders"], capture_output=True, text=True)
            stdout = res.stdout or ""
            if "libfdk_aac" in stdout:
                name_fdk, id_fdk = make_unique_name_and_id("FFmpeg FDK-AAC", hosted_codec_ver(ff_bin, "libfdk-aac", ver), "ffmpeg_libfdk_aac", existing_names, existing_ids)
                decoders.append(FFmpegDecoder(name_fdk, ff_bin, tool_id=id_fdk))
        except Exception:
            pass

    afconvert_bins = flatten_arg_list(getattr(args, "afconvert_bin", None))
    if not afconvert_bins:
        which_afc = shutil.which("afconvert")
        if which_afc:
            afconvert_bins = [which_afc]

    for afc_bin in afconvert_bins:
        ver = probe_version(afc_bin, ["-h", "--help"], [r"afconvert\s+version\s+([^\s,]+)", r"version:?\s+([0-9.]+)"])
        name, tool_id = make_unique_name_and_id("Apple AudioToolbox", ver, "afconvert", existing_names, existing_ids)
        decoders.append(AFConvertDecoder(name, afc_bin, tool_id=tool_id))

    return decoders

# -----------------------------------------------------------------------------
# REPORT GENERATION FUNCTIONS
# -----------------------------------------------------------------------------

def generate_leaderboard(encoders, results, output_path, scenario_list, skip_graphs=False, has_decoders=False):
    # Aggregation keyed by row_key (tool, profile). Every encoder is compared
    # at the same target bitrate (there is no cross-encoder VBR/quality mode
    # here -- each encoder's own quality knob isn't comparable to any other's,
    # see compare_codecs.py's Encoder.get_encode_cmd), so there is nothing
    # left to key on beyond the (tool, profile) combination itself.
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0, "mos_min_file": None,
        "ic_sum": 0, "ic_count": 0,
        "centroid_sum": 0, "centroid_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "br_err_sum": 0, "br_err_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    error_counts = defaultdict(int)

    # Every row_key's MOS on the same (scenario, filename), so a worst-case
    # score can be judged against how other encoders did on that exact clip
    # instead of in isolation -- see cell_peer_gap().
    clip_mos = defaultdict(dict)
    # (tool, scenario, filename, this_mos, peer_avg), one per (row_key,
    # scenario) cell flagged by cell_peer_gap, collected while rendering the
    # per-scenario Worst MOS tables and rolled up into their own section.
    bug_flags = []

    for res in results:
        e = res["row_key"]
        s = res["scenario"]

        if not res.get("decode_valid"):
            err_msg = res.get("decode_error") or "Unknown error"
            short_err = err_msg.split("\n")[0].split(":")[0].strip()
            error_counts[(e, short_err)] += 1

        if res.get("mos") is not None:
            stats[e][s]["mos_sum"] += res["mos"]
            stats[e][s]["mos_count"] += 1
            if res["mos"] < stats[e][s]["mos_min"]:
                stats[e][s]["mos_min"] = res["mos"]
                stats[e][s]["mos_min_file"] = res["filename"]
            clip_mos[(s, res["filename"])][e] = res["mos"]

        if res.get("ic_err") is not None:
            stats[e][s]["ic_sum"] += res["ic_err"]
            stats[e][s]["ic_count"] += 1

        # Transient fidelity: mean |attack-centroid-shift| in ms across this
        # clip's onsets (lower is better), unlike Stereo Fidelity above which
        # is reported as 1 - error (higher is better).
        if res.get("attack_centroid_ms"):
            for d in res["attack_centroid_ms"]:
                stats[e][s]["centroid_sum"] += abs(d)
                stats[e][s]["centroid_count"] += 1

        if res["duration"] > 0 and res["audio_duration"]:
            speed = res["audio_duration"] / res["duration"]
            stats[e][s]["speed_sum"] += speed
            stats[e][s]["speed_count"] += 1

        if res["actual_bitrate"] and res["target_bitrate"]:
            err = abs(res["actual_bitrate"] - res["target_bitrate"]) / res["target_bitrate"] * 100
            stats[e][s]["br_err_sum"] += err
            stats[e][s]["br_err_count"] += 1

        if res.get("peak_ram_kb") is not None and res["peak_ram_kb"] > 0:
            stats[e][s]["ram_sum"] = stats[e][s].get("ram_sum", 0) + res["peak_ram_kb"]
            stats[e][s]["ram_count"] = stats[e][s].get("ram_count", 0) + 1

        stats[e][s]["total_count"] += 1
        if res.get("decode_valid"):
            stats[e][s]["valid_count"] += 1

    encoder_info = {row_key(e): e for e in encoders}
    all_row_keys = sorted(stats.keys())

    overall = {}
    for e_name in all_row_keys:
        e_mos, e_speed, e_br_err, e_ic, e_centroid = [], [], [], [], []
        e_mos_min = 6.0
        e_total = e_valid = 0

        has_data = False
        scenario_count = 0
        for s_name in scenario_list:
            s_stats = stats[e_name][s_name]
            e_total += s_stats["total_count"]
            e_valid += s_stats["valid_count"]
            s_has_data = False
            if s_stats["mos_count"] > 0:
                e_mos.append(s_stats["mos_sum"] / s_stats["mos_count"])
                e_mos_min = min(e_mos_min, s_stats["mos_min"])
                s_has_data = True
            if s_stats["speed_count"] > 0:
                e_speed.append(s_stats["speed_sum"] / s_stats["speed_count"])
                s_has_data = True
            if s_stats["br_err_count"] > 0:
                e_br_err.append(s_stats["br_err_sum"] / s_stats["br_err_count"])
                s_has_data = True
            if s_stats["ic_count"] > 0:
                e_ic.append(s_stats["ic_sum"] / s_stats["ic_count"])
                s_has_data = True
            if s_stats["centroid_count"] > 0:
                e_centroid.append(s_stats["centroid_sum"] / s_stats["centroid_count"])
                s_has_data = True
            if s_has_data:
                has_data = True
                scenario_count += 1

        if not has_data:
            continue

        enc_obj = encoder_info.get(e_name)
        overall[e_name] = {
            "tool": enc_obj.name if enc_obj else e_name,
            "profile": enc_obj.profile if enc_obj else "lc",
            "overall_mos": sum(e_mos) / len(e_mos) if e_mos else 0,
            "worst_mos": e_mos_min if e_mos else 0,
            "avg_ic": sum(e_ic) / len(e_ic) if e_ic else 0,
            "avg_centroid_ms": sum(e_centroid) / len(e_centroid) if e_centroid else 0,
            "avg_speed": sum(e_speed) / len(e_speed) if e_speed else 0,
            "avg_br_err": sum(e_br_err) / len(e_br_err) if e_br_err else 0,
            "text_size": enc_obj.text_size if enc_obj else 0,
            "rodata_size": enc_obj.rodata_size if enc_obj else 0,
            "valid_rate": (e_valid / e_total * 100) if e_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    # Overall Rankings roll up to one row per tool: for each scenario, use
    # whichever profile that tool actually produced the best (or only) result
    # for. This is deliberate -- a tool that supports HE-AAC gets its
    # low-bitrate efficiency counted as part of what it can achieve, the same
    # way a real deployment would pick the better profile automatically. Tools
    # without HE-AAC support are simply scored on the one profile they have.
    # The LC/HE split is still fully visible in the per-scenario tables below.
    tool_row_keys = defaultdict(list)
    for e in encoders:
        tool_row_keys[e.name].append(row_key(e))

    def scenario_best_row_key(candidates, s_name):
        available = [rk for rk in candidates if stats[rk][s_name]["total_count"] > 0]
        if not available:
            return None
        def sort_key(rk):
            st = stats[rk][s_name]
            mos_avg = st["mos_sum"] / st["mos_count"] if st["mos_count"] > 0 else None
            valid_rate = st["valid_count"] / st["total_count"] if st["total_count"] > 0 else 0
            return (mos_avg is not None, mos_avg if mos_avg is not None else 0, valid_rate)
        return max(available, key=sort_key)

    tool_overall = {}
    for tool_name, candidates in tool_row_keys.items():
        e_mos, e_speed, e_br_err, e_ic, e_centroid, e_ram = [], [], [], [], [], []
        e_mos_min = 6.0
        has_data = False
        scenario_count = 0
        tool_total = tool_valid = 0
        for rk in candidates:
            for s_name in scenario_list:
                tool_total += stats[rk][s_name]["total_count"]
                tool_valid += stats[rk][s_name]["valid_count"]

        for s_name in scenario_list:
            rk = scenario_best_row_key(candidates, s_name)
            if rk is None:
                continue
            s_stats = stats[rk][s_name]
            s_has_data = False
            if s_stats["mos_count"] > 0:
                e_mos.append(s_stats["mos_sum"] / s_stats["mos_count"])
                e_mos_min = min(e_mos_min, s_stats["mos_min"])
                s_has_data = True
            if s_stats["speed_count"] > 0:
                e_speed.append(s_stats["speed_sum"] / s_stats["speed_count"])
                s_has_data = True
            if s_stats["br_err_count"] > 0:
                e_br_err.append(s_stats["br_err_sum"] / s_stats["br_err_count"])
                s_has_data = True
            if s_stats["ic_count"] > 0:
                e_ic.append(s_stats["ic_sum"] / s_stats["ic_count"])
                s_has_data = True
            if s_stats["centroid_count"] > 0:
                e_centroid.append(s_stats["centroid_sum"] / s_stats["centroid_count"])
                s_has_data = True
            if s_stats.get("ram_count", 0) > 0:
                e_ram.append(s_stats["ram_sum"] / s_stats["ram_count"])
                s_has_data = True
            if s_has_data:
                has_data = True
                scenario_count += 1

        if not has_data:
            continue

        enc_obj = next((e for e in encoders if e.name == tool_name), None)
        tool_overall[tool_name] = {
            "tool": tool_name,
            "overall_mos": sum(e_mos) / len(e_mos) if e_mos else 0,
            "worst_mos": e_mos_min if e_mos else 0,
            "avg_ic": sum(e_ic) / len(e_ic) if e_ic else 0,
            "avg_centroid_ms": sum(e_centroid) / len(e_centroid) if e_centroid else 0,
            "avg_speed": sum(e_speed) / len(e_speed) if e_speed else 0,
            "avg_br_err": sum(e_br_err) / len(e_br_err) if e_br_err else 0,
            "avg_ram_kb": sum(e_ram) / len(e_ram) if e_ram else 0,
            "text_size": enc_obj.text_size if enc_obj else 0,
            "rodata_size": enc_obj.rodata_size if enc_obj else 0,
            "valid_rate": (tool_valid / tool_total * 100) if tool_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    # Rank by worst-case MOS first, overall (mean) MOS as tiebreaker -- a strong
    # mean can hide a bad worst-case scenario, and worst-case robustness is what
    # matters most in practice (see Metric Legend below).
    has_mos = any(o["overall_mos"] > 0 for o in tool_overall.values())
    sorted_tools = sorted(tool_overall.keys(), key=lambda x: (tool_overall[x]["worst_mos"], tool_overall[x]["overall_mos"]), reverse=True) if has_mos else sorted(tool_overall.keys())

    has_non_aac = any(e.profile == "standard" for e in encoders)
    title_str = "# Audio Encoder Leaderboard\n\n" if has_non_aac else "# AAC Encoder Leaderboard\n\n"

    with open(output_path, "w") as f:
        f.write(title_str)
        if has_decoders:
            nav_links = ["[🎧 Encoder Rankings](#overall-rankings)", "[🔊 Decoder Rankings](#decoder-leaderboard)"]
            f.write(" | ".join(nav_links) + "\n\n---\n\n")
        f.write("Quality scores are objective proxy estimates (Zimtohrli/ViSQOL), not blind ABX listening test results.\n\n")
        f.write("## Overall Rankings\n\n")
        # Overall MOS is a mean of per-scenario means, so the scenario set is
        # part of the number: adding or removing a family shifts every
        # encoder's absolute score even though nothing about the encoders
        # changed. Rankings stay valid (all encoders see the same set).
        f.write("> **Note**: Overall MOS averages the scenario set listed below, so absolute "
                "values are only comparable between leaderboards built from the same set of "
                "scenarios. Relative ranking is unaffected.\n\n")
        f.write("| Rank | Encoder | Status | Worst MOS | Overall MOS | Scenarios | Stereo Fidelity | Transient Fidelity | Speed (xRT) | Bitrate Error | Peak RAM | ROM (Flash) |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_mos = max(o['overall_mos'] for o in tool_overall.values()) if tool_overall else 0
        best_worst_mos = max(o['worst_mos'] for o in tool_overall.values()) if tool_overall else 0
        best_speed = max(o['avg_speed'] for o in tool_overall.values()) if tool_overall else 0
        has_ic = any(o['avg_ic'] > 0 for o in tool_overall.values())
        best_ic = max(1.0 - o['avg_ic'] for o in tool_overall.values() if o['avg_ic'] > 0) if has_ic else None
        has_centroid = any(o['avg_centroid_ms'] > 0 for o in tool_overall.values())
        best_centroid_fid = max(1.0 / (1.0 + o['avg_centroid_ms']) for o in tool_overall.values() if o['avg_centroid_ms'] > 0) if has_centroid else None
        valid_br = [o['avg_br_err'] for o in tool_overall.values()]
        best_br = min(valid_br) if valid_br else 0

        for i, tool_name in enumerate(sorted_tools):
            o = tool_overall[tool_name]
            rank_str = f"🏆 {i+1}" if i == 0 and o['worst_mos'] > 0 else f"{i+1}"

            if o['valid_rate'] == 100:
                status_str = "OK"
            else:
                candidates = tool_row_keys[tool_name]
                relevant_errors = {err: v for (rk, err), v in error_counts.items() if rk in candidates}
                top_err = max(relevant_errors, key=relevant_errors.get) if relevant_errors else "Err"
                status_str = f"❌ {100-o['valid_rate']:.1f}% ({top_err})"

            w_str = f"**{o['worst_mos']:.3f}**" if o['worst_mos'] == best_worst_mos and best_worst_mos > 0 else f"{o['worst_mos']:.3f}"
            m_str = f"**{o['overall_mos']:.3f}**" if o['overall_mos'] == best_mos and best_mos > 0 else f"{o['overall_mos']:.3f}"
            scenarios_str = f"{o['scenario_count']}/{o['scenario_total']}"

            ic_val = o['avg_ic']
            if ic_val > 0:
                fid = 1.0 - ic_val
                ic_str = f"**{fid:.4f}**" if fid == best_ic else f"{fid:.4f}"
            else:
                ic_str = "N/A"

            centroid_val = o['avg_centroid_ms']
            if centroid_val > 0:
                centroid_fid = 1.0 / (1.0 + centroid_val)
                centroid_str = f"**{centroid_fid:.4f}**" if centroid_fid == best_centroid_fid else f"{centroid_fid:.4f}"
            else:
                centroid_str = "N/A"

            s_str = f"**{o['avg_speed']:.1f}x**" if o['avg_speed'] == best_speed and best_speed > 0 else f"{o['avg_speed']:.1f}x"
            br_str = f"**{o['avg_br_err']:.1f}%**" if o['avg_br_err'] == best_br else f"{o['avg_br_err']:.1f}%"
            ram_str = format_size(int(o["avg_ram_kb"] * 1024)) if o["avg_ram_kb"] > 0 else "N/A"
            rom_str = format_size(o['text_size'] + o['rodata_size'])

            f.write(f"| {rank_str} | {tool_name} | {status_str} | {w_str} | {m_str} | {scenarios_str} | {ic_str} | {centroid_str} | {s_str} | {br_str} | {ram_str} | {rom_str} |\n")

        scenarios = sorted(scenario_list, key=get_scenario_sort_key)
        all_em_keys_sorted = sorted(overall.keys())

        # Group encoder keys by profile ("lc", "he", "hev2", "standard")
        profile_order = ["lc", "he", "hev2", "standard"]
        profile_keys = {p: [rk for rk in all_em_keys_sorted if overall[rk]["profile"] == p] for p in profile_order}

        tool_row_keys = defaultdict(list)
        for e in encoders:
            tool_row_keys[e.name].append(row_key(e))

        def scenario_best_row_key(candidates, s_name):
            available = [rk for rk in candidates if stats[rk][s_name]["total_count"] > 0]
            if not available:
                return None
            def sort_key(rk):
                st = stats[rk][s_name]
                mos_avg = st["mos_sum"] / st["mos_count"] if st["mos_count"] > 0 else None
                valid_rate = st["valid_count"] / st["total_count"] if st["total_count"] > 0 else 0
                return (mos_avg is not None, mos_avg if mos_avg is not None else 0, valid_rate)
            return max(available, key=sort_key)

        # Charts rank by overall (mean) MOS, not sorted_tools' worst-case-first
        # order: the Overall Rankings table above is deliberately pessimistic
        # (surfacing a bad worst-case is the point of that table), but a chart
        # is meant to show off the best performers -- one unlucky clip
        # shouldn't bump a consistently high-quality encoder out of every
        # graph. Bar and line charts share this same ranking so the "cast" of
        # encoders is consistent across the whole Efficiency & Footprint
        # section, not a different set of tools per metric.
        mos_ranked_tools = sorted(tool_overall.keys(), key=lambda x: tool_overall[x]["overall_mos"], reverse=True) if has_mos else sorted(tool_overall.keys())
        chart_tools = mos_ranked_tools[:10]  # Top 10 for clean bar display
        top_tools = chart_tools[:5]          # Top 5 for clean line display

        avg_mos = (lambda rk, sc: stats[rk][sc]["mos_sum"] / stats[rk][sc]["mos_count"]
                   if stats[rk][sc]["mos_count"] > 0 else None)
        worst_mos = (lambda rk, sc: stats[rk][sc]["mos_min"]
                     if stats[rk][sc]["mos_count"] > 0 else None)
        stereo_fid = (lambda rk, sc: 1.0 - (stats[rk][sc]["ic_sum"] / stats[rk][sc]["ic_count"])
                      if stats[rk][sc]["ic_count"] > 0 else None)
        transient_fid = (lambda rk, sc: 1.0 / (1.0 + (stats[rk][sc]["centroid_sum"] / stats[rk][sc]["centroid_count"]))
                         if stats[rk][sc]["centroid_count"] > 0 else None)
        bitrate_err = (lambda rk, sc: stats[rk][sc]["br_err_sum"] / stats[rk][sc]["br_err_count"]
                      if stats[rk][sc]["br_err_count"] > 0 else None)

        def is_suboptimal(rk, s_name):
            if rk not in overall:
                return False
            enc_obj = encoder_info.get(rk)
            tool_name = enc_obj.name if enc_obj else overall[rk]["tool"]

            # Generic quality comparison: a profile is suboptimal if another profile
            # of the same encoder tool achieves higher perceptual MOS for this scenario.
            candidates = tool_row_keys[tool_name]
            mos_this = avg_mos(rk, s_name)
            if mos_this is not None:
                for other_rk in candidates:
                    if other_rk == rk:
                        continue
                    other_mos = avg_mos(other_rk, s_name)
                    if other_mos is not None and other_mos > mos_this + 0.005:
                        return True

            return False

        def make_progress_bar(val, max_val=1.0, width=8, lower_is_better=False):
            if val is None or max_val <= 0:
                return ""
            ratio = max(0.0, min(1.0, val / max_val))
            if lower_is_better:
                ratio = 1.0 - ratio
            filled = int(round(ratio * width))
            return " " + "█" * filled + "░" * (width - filled)

        def render_metric_tables(extract_val_fn, fmt_fn, lower_is_better=False, filter_scenarios=None, max_scale=None, annotate_fn=None):
            scen_list = filter_scenarios if filter_scenarios is not None else scenarios
            table_used_strikethrough = False
            for p in profile_order:
                # Filter out encoder keys that have no valid data for this scenario subset
                keys = [rk for rk in profile_keys[p] if any(extract_val_fn(rk, s) is not None for s in scen_list)]
                if not keys:
                    continue

                headers = [overall[rk]["tool"] for rk in keys]

                # Determine max_scale automatically if not explicitly provided
                table_max_scale = max_scale
                if table_max_scale is None:
                    vals = [extract_val_fn(rk, s) for rk in keys for s in scen_list if extract_val_fn(rk, s) is not None]
                    table_max_scale = max(vals) if vals else 1.0

                f.write(f"#### {profile_label(p)} Profile\n\n")
                f.write("| Scenario | " + " | ".join(headers) + " |\n")
                f.write("| :--- | " + " | ".join([":---:"] * len(headers)) + " |\n")

                for s in scen_list:
                    row_vals = [extract_val_fn(rk, s) for rk in keys]

                    # Highest / best value overall in this scenario within this profile table
                    all_non_none = [v for v in row_vals if v is not None]
                    best_val = (min(all_non_none) if lower_is_better else max(all_non_none)) if all_non_none else None

                    line = f"| {s} |"
                    for rk, val in zip(keys, row_vals):
                        if val is None:
                            line += " N/A |"
                        else:
                            formatted = fmt_fn(val)
                            subopt = is_suboptimal(rk, s)
                            is_best = (val == best_val)

                            if subopt and is_best:
                                formatted = f"_**{formatted}**_*"
                                table_used_strikethrough = True
                            elif subopt:
                                formatted = f"_{formatted}_*"
                                table_used_strikethrough = True
                            elif is_best:
                                formatted = f"**{formatted}**"

                            bar_str = make_progress_bar(val, table_max_scale, lower_is_better=lower_is_better)
                            note = annotate_fn(rk, s) if annotate_fn else ""
                            line += f" {formatted}{bar_str}{note} |"
                    f.write(line + "\n")

                f.write("\n")

            if table_used_strikethrough:
                f.write("_\\* Italicized scores indicate sub-optimal profile performance superseded by another profile from the same encoder at this bitrate._\n\n")

        f.write("\n<details><summary><b>📊 View Per-Scenario Breakdowns & Visualizations</b></summary>\n\n")
        f.write("## Per-Scenario Breakdown & Visualizations\n\n")

        # 1-3. Quality per rate family.
        #
        # These sections used to be a hard-coded "Stereo Audio" / "Mono Speech"
        # pair, which worked only while the suite had exactly one sample rate
        # per channel count. The charts plot MOS against BITRATE, so mixing
        # rates into one chart draws a line through unrelated configurations --
        # 32k_stereo_48k and 48k_stereo_48k would both land on the "48k" tick.
        # One section per family keeps every curve meaningful.
        def zoomed_y_range(vals, y_range):
            """Zoom to where the data actually falls instead of the metric's
            full theoretical range, which otherwise buries real differences in
            dead space when every value clusters far from the floor/ceiling
            (e.g. MOS 3.5-4.9 on a 1.0-5.0 axis). Clamped within that range."""
            y_floor, y_ceiling = (float(x) for x in y_range.split("-->"))
            lo, hi = min(vals), max(vals)
            pad = max((hi - lo) * 0.1, (y_ceiling - y_floor) * 0.02)
            axis_lo, axis_hi = max(y_floor, lo - pad), min(y_ceiling, hi + pad)
            if axis_hi - axis_lo < 1e-6:
                axis_lo, axis_hi = y_floor, y_ceiling
            return axis_lo, axis_hi

        def family_chart(fam_scenarios, title, y_label, y_range, value_fn):
            """Emit one MOS-style xychart for a single family.

            Skipped below three points: a two-point line chart says less than
            the table underneath it.
            """
            if skip_graphs or not top_tools or len(fam_scenarios) < 3:
                return
            # The x-axis is bitrate, so two scenarios at the same bitrate (the
            # clean and VoIP-degraded 16 kHz corpora, say) would collide on one
            # tick and read as a single curve through unrelated content.
            bitrates = [SCENARIOS.get(sc, {}).get("bitrate") for sc in fam_scenarios]
            if len(set(bitrates)) != len(bitrates):
                return
            scen_labels = [f'"{SCENARIOS.get(sc, {}).get("bitrate", sc)}k"' for sc in fam_scenarios]

            series = {}
            all_vals = []
            for t in top_tools:
                candidates = tool_row_keys[t]
                vals = []
                for sc in fam_scenarios:
                    rk = scenario_best_row_key(candidates, sc)
                    vals.append(value_fn(rk, sc) if rk else None)
                series[t] = vals
                all_vals.extend(v for v in vals if v is not None)
            if not all_vals:
                return

            axis_lo, axis_hi = zoomed_y_range(all_vals, y_range)

            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write(f'    title "{title}"\n')
            f.write(f"    x-axis [{', '.join(scen_labels)}]\n")
            f.write(f'    y-axis "{y_label}" {axis_lo:.4g} --> {axis_hi:.4g}\n')
            for t in top_tools:
                vals_str = ", ".join(f"{v:.4f}" if v is not None else "0.0" for v in series[t])
                f.write(f'    line "{t}" [{vals_str}]\n')
            f.write("```\n\n")

        for fam in scenario_families(scenarios):
            fam_scenarios = sorted(
                [sc for sc in scenarios if scenario_family(sc) == fam],
                key=get_scenario_sort_key)
            if not fam_scenarios:
                continue
            label = family_label(fam)

            f.write(f"### {label} Quality Across Bitrates\n\n")
            family_chart(fam_scenarios,
                         f"{label} Quality across Bitrates (Average MOS)",
                         "MOS Score", "1.0 --> 5.0", avg_mos)

            f.write(f"<details><summary><b>View Detailed {label} Average & Worst MOS Tables</b></summary>\n\n")
            f.write(f"#### Per-Scenario Average MOS ({label})\n\n")
            render_metric_tables(avg_mos, lambda v: f"{v:.3f}",
                                 filter_scenarios=fam_scenarios, max_scale=5.0)
            f.write(f"#### Per-Scenario Worst MOS (Min Clip MOS - {label})\n\n")
            f.write("> **Note**: Minimum perceptual MOS score observed across any clip in the scenario. Highlights edge-case clip degradation. "
                    f"A 🐛 names the clip when every other encoder scored ≥{CLIP_PEER_BUG_GAP} MOS higher on that exact clip -- "
                    "likely a defect specific to this encoder; see Quality Outliers under Issues Worth Investigating below.\n\n")

            def worst_mos_annotate(rk, s):
                filename = stats[rk][s].get("mos_min_file")
                gap = cell_peer_gap(clip_mos, rk, s, filename)
                if not gap:
                    return ""
                this_mos, peer_avg = gap
                bug_flags.append((overall[rk]["tool"], s, filename, this_mos, peer_avg))
                return f" 🐛 _{filename}_"

            render_metric_tables(worst_mos, lambda v: f"{v:.3f}",
                                 filter_scenarios=fam_scenarios, max_scale=5.0, annotate_fn=worst_mos_annotate)
            f.write("</details>\n\n")

            # Stereo image fidelity is undefined for mono families.
            if scenario_channels(SCENARIOS.get(fam_scenarios[0], {})) >= 2:
                f.write(f"### Stereo Image Fidelity ({label})\n\n")
                f.write("> **Note**: Measured as 1.0 - |Coherence(Ref) - Coherence(Deg)|. **Higher is truer** (closer to reference stereo image).\n\n")
                family_chart(fam_scenarios,
                             f"Stereo Image Fidelity across Bitrates - {label} (Higher is Better)",
                             "Stereo Fidelity", "0.0 --> 1.0", stereo_fid)
                f.write(f"<details><summary><b>View Detailed Stereo Fidelity Table ({label})</b></summary>\n\n")
                render_metric_tables(stereo_fid, lambda v: f"{v:.4f}",
                                     filter_scenarios=fam_scenarios, max_scale=1.0)
                f.write("</details>\n\n")

            f.write(f"### Transient Fidelity ({label})\n\n")
            f.write("> **Note**: Measured as 1 / (1 + mean |attack-centroid-shift| ms) across onsets. **Higher is truer** (attack timing closer to reference).\n\n")
            family_chart(fam_scenarios,
                         f"Transient Fidelity across Bitrates - {label} (Higher is Better)",
                         "Transient Fidelity", "0.0 --> 1.0", transient_fid)
            f.write(f"<details><summary><b>View Detailed Transient Fidelity Table ({label})</b></summary>\n\n")
            render_metric_tables(transient_fid, lambda v: f"{v:.4f}",
                                 filter_scenarios=fam_scenarios, max_scale=1.0)
            f.write("</details>\n\n")

            f.write(f"### Bitrate Accuracy ({label})\n\n")
            f.write("> **Note**: Deviation from target bitrate calculated from pure elementary stream audio bytes. **Lower is Better**.\n\n")
            family_chart(fam_scenarios,
                         f"Bitrate Accuracy across Bitrates - {label} (Lower is Better)",
                         "Bitrate Error (%)", "0 --> 25", bitrate_err)
            f.write(f"<details><summary><b>View Detailed Bitrate Accuracy Table ({label})</b></summary>\n\n")
            render_metric_tables(bitrate_err, lambda v: f"{v:.1f}%",
                                 filter_scenarios=fam_scenarios, lower_is_better=True)
            f.write("</details>\n\n")

            # Isolates the cost of VoIP-style degradation (chop/clip/echo/noise
            # in the reference) at a fixed bitrate -- exactly the pair the
            # bitrate-progression chart above refuses to plot together, since
            # it would draw them as one misleading curve at the same x tick.
            if "16k_mono_24k" in fam_scenarios and "16k_mono_voip_24k" in fam_scenarios and not skip_graphs and top_tools:
                f.write(f"### {label}: Clean vs VoIP-Degraded (Same Bitrate)\n\n")
                f.write("> **Note**: Both scenarios target 24 kbps, isolating how much VoIP-style degradation costs each encoder independent of bitrate.\n\n")
                tool_labels_fam = [f'"{t}"' for t in top_tools]
                clean_raw, voip_raw = [], []
                for t in top_tools:
                    candidates = tool_row_keys[t]
                    rk_clean = scenario_best_row_key(candidates, "16k_mono_24k")
                    rk_voip = scenario_best_row_key(candidates, "16k_mono_voip_24k")
                    clean_raw.append(avg_mos(rk_clean, "16k_mono_24k") if rk_clean else None)
                    voip_raw.append(avg_mos(rk_voip, "16k_mono_voip_24k") if rk_voip else None)
                all_vals = [v for v in clean_raw + voip_raw if v is not None]
                axis_lo, axis_hi = zoomed_y_range(all_vals, "1.0 --> 5.0") if all_vals else (1.0, 5.0)
                clean_vals = [f"{v:.3f}" if v is not None else "0.0" for v in clean_raw]
                voip_vals = [f"{v:.3f}" if v is not None else "0.0" for v in voip_raw]
                f.write("```mermaid\n")
                f.write("xychart-beta\n")
                f.write(f'    title "{label}: Clean vs VoIP-Degraded MOS at 24 kbps"\n')
                f.write(f"    x-axis [{', '.join(tool_labels_fam)}]\n")
                f.write(f'    y-axis "MOS Score" {axis_lo:.4g} --> {axis_hi:.4g}\n')
                f.write(f'    bar "Clean" [{", ".join(clean_vals)}]\n')
                f.write(f'    bar "VoIP-Degraded" [{", ".join(voip_vals)}]\n')
                f.write("```\n\n")

        # BD-Rate Analysis vs Baseline Encoder (FAAC if present, else first encoder)
        try:
            import bd_rate as bdr

            # Find reference baseline encoder key: the highest-version FAAC
            # LC build present, so every candidate is measured against
            # current FAAC rather than whichever version happened to sort
            # first alphabetically (previously the *lowest* version, since
            # "faac_1_31_1" < "faac_2_1_0" as strings -- and "faac" in rk
            # matched any profile, not just LC despite the old comment).
            # A git-identified dev build (name has a "(...)" suffix) counts
            # as ahead of the bare tag it shares a version number with.
            def faac_version_key(rk):
                name = encoder_info[rk].name
                m = re.search(r"(\d+)\.(\d+)\.(\d+)", name)
                ver = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
                return (ver, 1 if "(" in name else 0)

            faac_lc_keys = [rk for rk in all_row_keys
                             if isinstance(encoder_info.get(rk), FAACEncoder) and encoder_info[rk].profile == "lc"]
            base_key = max(faac_lc_keys, key=faac_version_key) if faac_lc_keys else (all_row_keys[0] if all_row_keys else None)
            if base_key:
                base_tool_name = encoder_info[base_key].name if base_key in encoder_info else base_key
                f.write(f"### BD-Rate Relative Efficiency (vs {base_tool_name})\n\n")
                f.write("> **Note**: Bjontegaard-delta rate (BD-rate) measures the average percentage difference in bitrate for equal perceptual quality (MOS). "
                        "**Negative % = candidate is more efficient** (uses fewer bits for same quality). "
                        "BD-rate holds quality fixed by construction, avoiding bitrate-bias traps of raw fixed-rate MOS deltas.\n\n")

                base_recs = [r for r in results if r["row_key"] == base_key and r.get("decode_valid") and r.get("mos") is not None and r.get("actual_bitrate")]
                base_mat = {f"{r['scenario']}_{r['filename']}": {
                    "scenario": r["scenario"], "filename": r["filename"],
                    "mos": r["mos"], "bitrate": r["actual_bitrate"],
                    "bitrate_target": r["target_bitrate"],
                    "object_type": r.get("profile", "lc")
                } for r in base_recs}

                bd_rows = []
                all_notes = set()
                for cand_key in all_row_keys:
                    if cand_key == base_key:
                        continue
                    cand_recs = [r for r in results if r["row_key"] == cand_key and r.get("decode_valid") and r.get("mos") is not None and r.get("actual_bitrate")]
                    cand_mat = {f"{r['scenario']}_{r['filename']}": {
                        "scenario": r["scenario"], "filename": r["filename"],
                        "mos": r["mos"], "bitrate": r["actual_bitrate"],
                        "bitrate_target": r["target_bitrate"],
                        "object_type": r.get("profile", "lc")
                    } for r in cand_recs}

                    analysis = bdr.analyze(base_mat, cand_mat)
                    scored = [seg for seg in analysis.get("segments", []) if seg.get("stats")]
                    all_notes.update(analysis.get("notes", []))
                    if scored:
                        avg_bd = sum(seg["stats"]["mean"] for seg in scored) / len(scored)
                        cand_obj = encoder_info.get(cand_key)
                        c_label = f"{cand_obj.name} ({profile_label(cand_obj.profile)})" if cand_obj else cand_key
                        corpora = sorted(set(seg["corpus"] for seg in scored))
                        bd_rows.append((c_label, avg_bd, corpora))

                if bd_rows:
                    f.write(f"| Candidate Encoder | Mean BD-Rate vs {base_tool_name} | Valid Ladders |\n")
                    f.write("| :--- | :---: | :--- |\n")
                    for c_label, avg_bd, corpora in sorted(bd_rows, key=lambda x: x[1]):
                        icon = "🚀" if avg_bd < -0.5 else "📉" if avg_bd > 0.5 else "🎯"
                        f.write(f"| {c_label} | **{avg_bd:+.2f}%** {icon} | {len(corpora)} ({', '.join(corpora)}) |\n")
                    f.write("\n")
                    if all_notes:
                        # bd_rate.py emits one note per excluded scenario/group,
                        # repeated across every candidate -- dumping them all
                        # verbatim is a wall of near-duplicate text. Summarize
                        # by reason instead of listing every scenario.
                        ot_mismatches = [n for n in all_notes if "excluded from every ladder" in n]
                        rung_shortfalls = sorted(set(n.split(":", 1)[0] for n in all_notes if "-- skipped" in n))
                        parts = []
                        if ot_mismatches:
                            parts.append(f"{len(ot_mismatches)} scenario/candidate pair(s) excluded because the candidate "
                                         "ran a different profile than the baseline for that scenario")
                        if rung_shortfalls:
                            parts.append(f"skipped for too few bitrate rungs (need ≥{bdr.MIN_RUNGS}): {', '.join(rung_shortfalls)}")
                        if parts:
                            f.write("> **Note**: " + "; ".join(parts) + ".\n\n")
                else:
                    f.write("_No valid BD-rate ladders found between baseline and candidate encoders._\n\n")
        except Exception as e:
            f.write(f"### BD-Rate Relative Efficiency\n\n_BD-rate evaluation skipped due to error: {e}_\n\n")

        # 6. Efficiency & Footprint
        f.write("### Encoder Efficiency & Footprint\n\n")
        if not skip_graphs and chart_tools:
            tool_labels = [f'"{t}"' for t in chart_tools]
            speed_vals = [f"{tool_overall[t]['avg_speed']:.1f}" for t in chart_tools]
            max_speed = max([tool_overall[t]['avg_speed'] for t in chart_tools] + [1.0])
            rom_vals = [f"{(tool_overall[t]['text_size'] + tool_overall[t]['rodata_size']) / 1024.0:.1f}" for t in chart_tools]
            max_rom = max([(tool_overall[t]['text_size'] + tool_overall[t]['rodata_size']) / 1024.0 for t in chart_tools] + [10.0])

            f.write("#### Encoding Speed (xRT)\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Average Encoding Speed (xRealtime, Higher is Better)"\n')
            f.write(f"    x-axis [{', '.join(tool_labels)}]\n")
            f.write(f'    y-axis "Speed (xRT)" 0 --> {int(max_speed * 1.25) + 1}\n')
            f.write(f"    bar [{', '.join(speed_vals)}]\n")
            f.write("```\n\n")

            f.write("#### Codec ROM (Flash) Size\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Codec Code + Read-Only Data Size (KB, Lower is Better)"\n')
            f.write(f"    x-axis [{', '.join(tool_labels)}]\n")
            f.write(f'    y-axis "ROM Size (KB)" 0 --> {int(max_rom * 1.25) + 1}\n')
            f.write(f"    bar [{', '.join(rom_vals)}]\n")
            f.write("```\n\n")

        f.write("<details><summary><b>View Detailed Per-Scenario Efficiency Table</b></summary>\n\n")
        render_metric_tables(
            lambda rk, s: stats[rk][s]["speed_sum"] / stats[rk][s]["speed_count"] if stats[rk][s]["speed_count"] > 0 else None,
            lambda v: f"{v:.1f}x"
        )
        f.write("</details>\n\n")
        f.write("</details>\n\n")

        if error_counts or bug_flags:
            f.write("\n<details><summary><b>❌ View Issues Worth Investigating</b></summary>\n\n")
            f.write("## Issues Worth Investigating\n\n")

            if error_counts:
                f.write("### Encoding Failures\n\n")
                f.write("| Encoder: Error Type | Occurrences |\n")
                f.write("| :--- | :---: |\n")
                for row_k, err in sorted(error_counts.keys(), key=lambda k: error_counts[k], reverse=True):
                    enc_obj = encoder_info.get(row_k)
                    label = f"{enc_obj.name} {profile_label(enc_obj.profile)}" if enc_obj else row_k
                    f.write(f"| {label}: {err} | {error_counts[(row_k, err)]} |\n")
                f.write("\n")

            if bug_flags:
                f.write("### Quality Outliers\n\n")
                f.write(f"> **Note**: Every other encoder scored ≥{CLIP_PEER_BUG_GAP} MOS higher than this one on "
                        "the exact same clip -- likely a defect specific to this encoder, not just a hard clip "
                        "every encoder shares. Sorted by gap size.\n\n")
                f.write("| Encoder | Scenario | Clip | This Score | Others Avg | Gap |\n")
                f.write("| :--- | :--- | :--- | :---: | :---: | :---: |\n")
                for tool_name, s_name, filename, this_mos, peer_avg in sorted(bug_flags, key=lambda x: x[4] - x[3], reverse=True):
                    f.write(f"| {tool_name} | {s_name} | {filename} | {this_mos:.3f} | {peer_avg:.3f} | {peer_avg - this_mos:+.2f} |\n")
                f.write("\n")

            f.write("</details>\n\n")

        f.write("\n---\n")
        f.write("**Metric Legend**:\n")
        f.write("- **Ranking**: by Worst MOS, then Overall MOS as tiebreaker.\n")
        f.write("- **Quality (MOS)**: Perceptual audio quality (1-5, **Higher is Better**)\n")
        f.write("- **Stereo Fidelity**: Faithfulness of stereo image (0-1, **Higher is Better**)\n")
        f.write("- **Transient Fidelity**: How little attacks are smeared/delayed (0-1, **Higher is Better**)\n")
        f.write("- **Speed**: Encoding throughput (**Higher is Better**)\n")
        f.write("- **Bitrate Error**: Deviation from target bitrate (**Lower is Better**)\n")
        f.write("- **ROM (Flash)**: Codec code + read-only data size (**Lower is Better**)\n")

    print(f"\nLeaderboard generated at: {output_path}")


def generate_decoder_leaderboard(decoders, results, output_path, scenario_list, skip_graphs=False, encoders=None, encoder_results=None, robustness_results=None, run_encoders_leaderboard=False):
    stats = defaultdict(lambda: defaultdict(lambda: {
        "mos_sum": 0, "mos_count": 0, "mos_min": 6.0,
        "snr_sum": 0, "snr_count": 0,
        "delay_sum": 0, "delay_count": 0,
        "ram_sum": 0, "ram_count": 0,
        "speed_sum": 0, "speed_count": 0,
        "valid_count": 0, "total_count": 0
    }))

    for res in results:
        rk = res["row_key"]
        s = res["scenario"]
        stats[rk][s]["total_count"] += 1
        if res.get("decode_valid"):
            stats[rk][s]["valid_count"] += 1
            if res.get("mos") is not None:
                stats[rk][s]["mos_sum"] += res["mos"]
                stats[rk][s]["mos_count"] += 1
                stats[rk][s]["mos_min"] = min(stats[rk][s]["mos_min"], res["mos"])
            if res.get("snr_db") is not None and res["snr_db"] != float("inf"):
                stats[rk][s]["snr_sum"] += res["snr_db"]
                stats[rk][s]["snr_count"] += 1
            if res.get("alignment_delay_ms") is not None:
                stats[rk][s]["delay_sum"] += abs(res["alignment_delay_ms"])
                stats[rk][s]["delay_count"] += 1
            if res.get("peak_ram_kb") is not None and res["peak_ram_kb"] > 0:
                stats[rk][s]["ram_sum"] += res["peak_ram_kb"]
                stats[rk][s]["ram_count"] += 1
            if res.get("duration", 0) > 0 and res.get("audio_duration"):
                stats[rk][s]["speed_sum"] += res["audio_duration"] / res["duration"]
                stats[rk][s]["speed_count"] += 1

    rob_stats = defaultdict(lambda: {"passed": 0, "total": 0})
    if robustness_results:
        for r_res in robustness_results:
            rk = r_res["row_key"]
            rob_stats[rk]["total"] += 1
            if r_res.get("passed"):
                rob_stats[rk]["passed"] += 1

    decoder_info = {decoder_row_key(d): d for d in decoders}
    overall = {}
    for rk, dec_obj in decoder_info.items():
        d_mos, d_speed, d_snr, d_delay, d_ram = [], [], [], [], []
        d_worst_mos = 6.0
        d_total = d_valid = 0
        scenario_count = 0

        for s_name in scenario_list:
            st = stats[rk][s_name]
            d_total += st["total_count"]
            d_valid += st["valid_count"]
            if st["mos_count"] > 0:
                d_mos.append(st["mos_sum"] / st["mos_count"])
                d_worst_mos = min(d_worst_mos, st["mos_min"])
                scenario_count += 1
            if st["snr_count"] > 0:
                d_snr.append(st["snr_sum"] / st["snr_count"])
            if st["delay_count"] > 0:
                d_delay.append(st["delay_sum"] / st["delay_count"])
            if st["ram_count"] > 0:
                d_ram.append(st["ram_sum"] / st["ram_count"])
            if st["speed_count"] > 0:
                d_speed.append(st["speed_sum"] / st["speed_count"])

        rob_info = rob_stats[rk]
        robustness_pct = (rob_info["passed"] / rob_info["total"] * 100.0) if rob_info["total"] > 0 else 100.0

        overall[rk] = {
            "tool": dec_obj.name,
            "worst_mos": d_worst_mos if d_mos else 0,
            "overall_mos": sum(d_mos) / len(d_mos) if d_mos else 0,
            "avg_snr_db": sum(d_snr) / len(d_snr) if d_snr else None,
            "avg_delay_ms": sum(d_delay) / len(d_delay) if d_delay else 0.0,
            "avg_ram_kb": sum(d_ram) / len(d_ram) if d_ram else 0,
            "avg_speed": sum(d_speed) / len(d_speed) if d_speed else 0,
            "robustness_pct": robustness_pct,
            "text_size": dec_obj.text_size,
            "rodata_size": dec_obj.rodata_size,
            "valid_rate": (d_valid / d_total * 100) if d_total > 0 else 0,
            "scenario_count": scenario_count,
            "scenario_total": len(scenario_list)
        }

    sorted_rk = sorted(overall.keys(), key=lambda rk: (overall[rk]["worst_mos"], overall[rk]["overall_mos"]), reverse=True)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        if not run_encoders_leaderboard:
            f.write("# AAC Leaderboard\n\n")
            nav_links = ["[📊 Decoder Rankings](#decoder-leaderboard)", "[📋 Decoder Scenarios](#per-scenario-decoder-breakdown)", "[⚙️ Decoder Efficiency](#decoder-efficiency--footprint)"]
            f.write(" | ".join(nav_links) + "\n\n---\n\n")

        f.write("## 🔊 Decoder Leaderboard\n\n")
        f.write("Objective evaluation of AAC decoders on Spec Conformance (SNR), Decoded Quality (MOS), Timing Alignment Error, Robustness, Speed, and Footprint.\n\n")

        f.write("### Overall Decoder Rankings\n\n")
        f.write("| Rank | Decoder | Status | Worst MOS | Overall MOS | Mean SNR | Timing Error | Robustness | Speed (xRT) | Peak RAM | ROM (Flash) |\n")
        f.write("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n")

        best_worst_mos = max(o["worst_mos"] for o in overall.values()) if overall else 0
        best_mos = max(o["overall_mos"] for o in overall.values()) if overall else 0
        best_speed = max(o["avg_speed"] for o in overall.values()) if overall else 0
        best_robustness = max(o["robustness_pct"] for o in overall.values()) if overall else 100.0

        for i, rk in enumerate(sorted_rk):
            o = overall[rk]
            rank_str = f"🏆 {i+1}" if i == 0 and o["worst_mos"] > 0 else f"{i+1}"
            status_str = "OK" if o["valid_rate"] == 100 else f"❌ {100-o['valid_rate']:.1f}%"
            w_str = f"**{o['worst_mos']:.3f}**" if o["worst_mos"] == best_worst_mos and best_worst_mos > 0 else f"{o['worst_mos']:.3f}"
            m_str = f"**{o['overall_mos']:.3f}**" if o["overall_mos"] == best_mos and best_mos > 0 else f"{o['overall_mos']:.3f}"
            snr_str = f"{o['avg_snr_db']:.1f} dB" if o["avg_snr_db"] is not None else "Bit-exact"
            delay_str = f"{o['avg_delay_ms']:.2f} ms"
            rob_str = f"**{o['robustness_pct']:.1f}%**" if o["robustness_pct"] == best_robustness else f"{o['robustness_pct']:.1f}%"
            s_str = f"**{o['avg_speed']:.1f}x**" if o["avg_speed"] == best_speed and best_speed > 0 else f"{o['avg_speed']:.1f}x"
            ram_str = format_size(int(o["avg_ram_kb"] * 1024)) if o["avg_ram_kb"] > 0 else "N/A"
            rom_str = format_size(o["text_size"] + o["rodata_size"])

            f.write(f"| {rank_str} | {o['tool']} | {status_str} | {w_str} | {m_str} | {snr_str} | {delay_str} | {rob_str} | {s_str} | {ram_str} | {rom_str} |\n")

        f.write("\n<details><summary><b>📊 View Per-Scenario Decoder Breakdowns</b></summary>\n\n")
        f.write("### Per-Scenario Decoder Breakdown\n\n")
        f.write("| Scenario | " + " | ".join(overall[rk]["tool"] for rk in sorted_rk) + " |\n")
        f.write("| :--- | " + " | ".join([":---:"] * len(sorted_rk)) + " |\n")

        for s_name in sorted(scenario_list, key=get_scenario_sort_key):
            row_str = f"| {s_name} |"
            for rk in sorted_rk:
                st = stats[rk][s_name]
                if st["mos_count"] > 0:
                    avg_m = st["mos_sum"] / st["mos_count"]
                    row_str += f" {avg_m:.3f} |"
                else:
                    row_str += " N/A |"
            f.write(row_str + "\n")

        if not skip_graphs and sorted_rk:
            f.write("\n### Decoder Efficiency & Footprint\n\n")
            labels = [f'"{overall[rk]["tool"]}"' for rk in sorted_rk]
            speeds = [f"{overall[rk]['avg_speed']:.1f}" for rk in sorted_rk]
            max_s = max([overall[rk]['avg_speed'] for rk in sorted_rk] + [1.0])

            f.write("#### Decoding Speed (xRT)\n\n")
            f.write("```mermaid\n")
            f.write("xychart-beta\n")
            f.write('    title "Average Decoding Throughput (xRealtime, Higher is Better)"\n')
            f.write(f"    x-axis [{', '.join(labels)}]\n")
            f.write(f'    y-axis "Speed (xRT)" 0 --> {int(max_s * 1.25) + 1}\n')
            f.write(f"    bar [{', '.join(speeds)}]\n")
            f.write("```\n\n")

        f.write("\n</details>\n\n")

    print(f"\nDecoder leaderboard generated at: {output_path}")

# -----------------------------------------------------------------------------
# MAIN CLI & BENCHMARK ORCHESTRATION
# -----------------------------------------------------------------------------


def gate_filter(name, filtered_samples):
    available = set(filtered_samples)
    picked = [c for c in GATE_CLIPS.get(name, []) if c in available]
    if picked:
        return picked
    n = min(GATE_FALLBACK_N, len(filtered_samples))
    if n <= 0:
        return []
    step = len(filtered_samples) / n
    return [filtered_samples[int(i * step)] for i in range(n)]

def get_audio_info(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,sample_rate", "-of", "json", path]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        s = data["streams"][0]
        return int(s["channels"]), int(s["sample_rate"])
    except Exception:
        return None, None

def process_encoder_task(encoder, scenario_name, cfg, sample, data_dir, output_dir):
    input_path = os.path.join(data_dir, sample)
    ext = getattr(encoder, "file_ext", ".m4a")
    output_filename = f"{encoder_row_key(encoder)}_{scenario_name}_{sample}{ext}".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    channels = scenario_channels(cfg)
    sample_rate = scenario_rate(cfg)
    cmd = encoder.get_encode_cmd(input_path, output_path, cfg["bitrate"], channels, sample_rate)

    try:
        res, duration, peak_ram_kb = measure_peak_ram(cmd, env=encoder.get_run_env() or None)

        if res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)

        file_size = os.path.getsize(output_path)
        es_bytes = get_audio_es_bytes(output_path)

        actual_bitrate = None
        audio_duration = ffmpeg_probe(input_path)
        if audio_duration and audio_duration > 0:
            actual_bitrate = (es_bytes * 8) / (audio_duration * 1000)

        valid, decode_err = decode_validate(output_path)
        out_channels, out_rate = get_audio_info(output_path)
        exp_channels = scenario_channels(cfg)
        if valid and out_channels is not None and out_channels != exp_channels:
            valid = False
            decode_err = f"Channels mismatch: {out_channels} vs {exp_channels}"

        return {
            "tool": encoder.name,
            "profile": encoder.profile,
            "row_key": encoder_row_key(encoder),
            "scenario": scenario_name,
            "filename": sample,
            "duration": duration,
            "audio_duration": audio_duration,
            "size": file_size,
            "actual_bitrate": actual_bitrate,
            "target_bitrate": cfg["bitrate"],
            "decode_valid": valid,
            "decode_error": decode_err,
            "peak_ram_kb": peak_ram_kb,
            "aac_path": output_path,
            "ref_path": input_path
        }
    except Exception as e:
        detail = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            stderr_text = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            stderr_tail = next((l for l in reversed(stderr_text.splitlines()) if l.strip()), "")
            if stderr_tail:
                detail = f"exit code {e.returncode}: {stderr_tail}"
        print(f"Error encoding {sample} with {encoder.name} ({profile_label(encoder.profile)}): {detail}")
        return {
            "tool": encoder.name,
            "profile": encoder.profile,
            "row_key": encoder_row_key(encoder),
            "scenario": scenario_name,
            "filename": sample,
            "duration": 0,
            "audio_duration": None,
            "size": 0,
            "actual_bitrate": None,
            "target_bitrate": cfg["bitrate"],
            "decode_valid": False,
            "decode_error": f"Encoding failed: {detail}",
            "aac_path": None,
            "ref_path": input_path
        }

def process_decoder_task(decoder, res_item, output_dir):
    aac_path = res_item.get("aac_path")
    ref_path = res_item.get("ref_path")
    scenario_name = res_item["scenario"]
    sample = res_item["filename"]

    if not aac_path or not os.path.exists(aac_path):
        return {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "duration": 0,
            "audio_duration": None,
            "decode_valid": False,
            "decode_error": "Input bitstream missing",
            "snr_db": None,
            "alignment_delay_ms": None,
            "peak_ram_kb": None,
            "decoded_wav": None
        }

    output_filename = f"dec_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}.wav".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    cmd = decoder.get_decode_cmd(aac_path, output_path)

    try:
        res, duration, peak_ram_kb = measure_peak_ram(cmd, env=decoder.get_run_env() or None)

        if res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)

        valid, decode_err = decode_validate(output_path)
        snr_db = None
        alignment_delay_ms = None
        if valid and ref_path and os.path.exists(ref_path):
            snr_db = compute_snr(ref_path, output_path)
            _lag_samples, alignment_delay_ms = measure_delay_offset(ref_path, output_path)

        audio_duration = ffmpeg_probe(ref_path) if ref_path else None

        return {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "duration": duration,
            "audio_duration": audio_duration,
            "decode_valid": valid,
            "decode_error": decode_err,
            "snr_db": snr_db,
            "alignment_delay_ms": alignment_delay_ms,
            "peak_ram_kb": peak_ram_kb,
            "decoded_wav": output_path
        }
    except Exception as e:
        detail = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            stderr_text = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            stderr_tail = next((l for l in reversed(stderr_text.splitlines()) if l.strip()), "")
            if stderr_tail:
                detail = f"exit code {e.returncode}: {stderr_tail}"
        return {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "duration": 0,
            "audio_duration": None,
            "decode_valid": False,
            "decode_error": f"Decoding failed: {detail}",
            "snr_db": None,
            "alignment_delay_ms": None,
            "peak_ram_kb": None,
            "decoded_wav": None
        }

def process_decoder_robustness_task(decoder, res_item, output_dir):
    """Executes a dedicated robustness test on a deterministically corrupted ADTS bitstream."""
    aac_path = res_item.get("aac_path")
    scenario_name = res_item["scenario"]
    sample = res_item["filename"]

    if not aac_path or not os.path.exists(aac_path):
        return {"tool": decoder.name, "row_key": decoder_row_key(decoder), "scenario": scenario_name, "filename": sample, "passed": False}

    corrupt_filename = f"corrupt_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}.aac".replace(" ", "_")
    corrupt_path = os.path.join(output_dir, corrupt_filename)
    out_wav_filename = f"corrupt_out_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}.wav".replace(" ", "_")
    out_wav_path = os.path.join(output_dir, out_wav_filename)

    if not corrupt_adts_bitstream(aac_path, corrupt_path, seed=42):
        return {"tool": decoder.name, "row_key": decoder_row_key(decoder), "scenario": scenario_name, "filename": sample, "passed": False}

    cmd = decoder.get_decode_cmd(corrupt_path, out_wav_path)
    try:
        res = subprocess.run(cmd, capture_output=True, check=False, timeout=10, env=decoder.get_run_env() or None)
        # Crash-free robustness pass: decoder did not crash (signal/SEGFAULT) or hang
        passed = (res.returncode >= 0)
        return {"tool": decoder.name, "row_key": decoder_row_key(decoder), "scenario": scenario_name, "filename": sample, "passed": passed}
    except Exception:
        return {"tool": decoder.name, "row_key": decoder_row_key(decoder), "scenario": scenario_name, "filename": sample, "passed": False}


def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite - Codec Comparison & Leaderboard")
    parser.add_argument("--mode", choices=["encoder", "decoder", "both"], default="both", help="Benchmarking mode: encoder, decoder, or both")
    parser.add_argument("--faac-bin", action="append", help="Path to faac binary")
    parser.add_argument("--faac-lib", action="append", help="Path to libfaac.so")
    parser.add_argument("--faac-bin-version", action="append", help="Explicit version label for --faac-bin")
    parser.add_argument("--fdkaac-bin", action="append", help="Path to fdkaac binary")
    parser.add_argument("--aac-enc-bin", action="append", help="Path to aac-enc binary")
    parser.add_argument("--falabaac-bin", action="append", help="Path to falabaac binary")
    parser.add_argument("--faad-bin", action="append", help="Path to faad binary")
    parser.add_argument("--faad-lib", action="append", help="Path to libfaad.so")
    parser.add_argument("--faad-bin-version", action="append", help="Explicit version label for --faad-bin")
    parser.add_argument("--ffmpeg-bin", action="append", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", action="append", help="Path to afconvert binary")
    parser.add_argument("--opusenc-bin", action="append", help="Path to opusenc binary")
    parser.add_argument("--lame-bin", action="append", help="Path to lame binary")
    parser.add_argument("--include-other-codecs", action="store_true", help="Include non-AAC codecs (Opus, LAME)")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="comparison_results.json", help="Intermediate results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip transient fidelity calculation")
    parser.add_argument("--skip-graphs", action="store_true", help="Skip generating Mermaid graph blocks")
    parser.add_argument("--resume", action="store_true", help="Reload --results-json from previous run")

    args = parser.parse_args()

    external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(SCRIPT_DIR, "data", "external")
    output_dir = os.path.join(SCRIPT_DIR, "output", "comparison")
    os.makedirs(output_dir, exist_ok=True)

    num_cpus = os.cpu_count() or 1

    scenario_list = list(SCENARIOS.keys())
    if args.scenarios:
        scenario_list = expand_scenario_list(args.scenarios)

    run_encoders = args.mode in ("encoder", "both")
    run_decoders = args.mode in ("decoder", "both")

    encoders = []
    encoder_results = []

    if run_encoders or (run_decoders and not os.path.exists(args.results_json)):
        encoders = detect_encoders(args)
        if not encoders and run_encoders:
            print("No encoders detected!")
            sys.exit(1)

    if encoders:
        print(f"Detected encoders: {', '.join(f'{e.name} ({profile_label(e.profile)})' for e in encoders)}")

    if (args.resume or not run_encoders) and os.path.exists(args.results_json):
        print(f"==> Loading bitstreams from {args.results_json}")
        with open(args.results_json) as f:
            encoder_results = json.load(f)
    elif encoders:
        for scenario_name in scenario_list:
            if scenario_name not in SCENARIOS:
                print(f"Scenario {scenario_name} not found in config, skipping.")
                continue
            cfg = SCENARIOS[scenario_name]
            print(f"\n>>> Running Encoder Scenario: {scenario_name} ({cfg['bitrate']} kbps)")
            data_dir = corpus_dir(cfg, external_data_dir)
            if not os.path.exists(data_dir):
                print(f"Data directory {data_dir} not found, skipping.")
                continue

            wavs = [f for f in os.listdir(data_dir) if f.endswith(".wav")]
            all_samples = sorted(wavs) if args.gate else select_corpus_clips(
                wavs, CORPORA.get(cfg["corpus"], {}))
            if args.gate:
                samples = gate_filter(scenario_name, all_samples)
            else:
                num_to_run = max(1, int(len(all_samples) * args.coverage / 100.0))
                step = len(all_samples) / num_to_run if num_to_run > 0 else 1
                samples = [all_samples[int(i * step)] for i in range(num_to_run)]

            print(f"Processing {len(samples)} samples...")
            channels = scenario_channels(cfg)
            sample_rate = scenario_rate(cfg)

            for encoder in encoders:
                supported, reason = encoder.supports_scenario(cfg["bitrate"], channels, sample_rate)
                if not supported:
                    print(f"  Skipping {encoder.name} ({profile_label(encoder.profile)}) for {scenario_name}: {reason}.")
                    continue

                if encoder.profile in ("he", "hev2"):
                    ok, cap_reason = probe_encoder_capability(encoder, bitrate_kbps=cfg["bitrate"], channels=channels, sample_rate=sample_rate)
                    if not ok:
                        print(f"  Skipping {encoder.name} ({profile_label(encoder.profile)}) for {scenario_name}: unsupported at {cfg['bitrate']} kbps ({cap_reason}).")
                        continue

                print(f"  Encoding with {encoder.name} ({profile_label(encoder.profile)})...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                    futures = [executor.submit(process_encoder_task, encoder, scenario_name, cfg, sample, data_dir, output_dir) for sample in samples]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            encoder_results.append(res)

        with open(args.results_json, "w") as f:
            json.dump(encoder_results, f, indent=2)

    # Phase 2 & 3 for Encoders if run_encoders
    if run_encoders and encoder_results:
        bridge_json = "bridge_results.json"
        bridge_data = {"matrix": {}}
        valid_count = 0
        for i, res in enumerate(encoder_results):
            if not res.get("aac_path") or not os.path.exists(res["aac_path"]):
                continue
            ext = os.path.splitext(res["aac_path"])[1] or ".m4a"
            key = f"res_{res['row_key']}_{i}"
            bridge_data["matrix"][key] = {
                "scenario": res["scenario"],
                "filename": res["filename"],
                "aac": f"{key}{ext}",
                "mos": None
            }
            dst_file = os.path.join(output_dir, f"{key}{ext}")
            if not os.path.exists(dst_file):
                shutil.copy(res["aac_path"], dst_file)
            valid_count += 1

        if valid_count > 0:
            with open(bridge_json, "w") as f:
                json.dump(bridge_data, f, indent=2)

            if not args.skip_mos:
                print("\n>>> Phase 2: Perceptual Quality (MOS) for Encoders")
                phase2_script = os.path.join(SCRIPT_DIR, "phase2_mos.py")
                cmd_phase2 = [sys.executable, phase2_script, bridge_json, output_dir, external_data_dir]
                subprocess.run(cmd_phase2, check=False)

                if os.path.exists(bridge_json):
                    with open(bridge_json) as f:
                        updated_bridge = json.load(f)
                    for i, res in enumerate(encoder_results):
                        key = f"res_{res['row_key']}_{i}"
                        if key in updated_bridge.get("matrix", {}):
                            res["mos"] = updated_bridge["matrix"][key].get("mos")

            if not args.skip_stereo:
                print("\n>>> Phase 3: Stereo Coherence for Encoders")
                if os.path.exists(bridge_json):
                    phase3_script = os.path.join(SCRIPT_DIR, "phase3_stereo.py")
                    cmd_phase3 = [sys.executable, phase3_script, bridge_json, output_dir, external_data_dir]
                    subprocess.run(cmd_phase3, check=False)

                    with open(bridge_json) as f:
                        updated_bridge = json.load(f)
                    for i, res in enumerate(encoder_results):
                        key = f"res_{res['row_key']}_{i}"
                        if key in updated_bridge.get("matrix", {}):
                            res["ic_err"] = updated_bridge["matrix"][key].get("ic_err")

            if not args.skip_transient:
                print("\n>>> Phase 3: Transient Fidelity for Encoders")
                if os.path.exists(bridge_json):
                    score_transient_script = os.path.join(SCRIPT_DIR, "scripts", "score_transient.py")
                    cmd_transient = [sys.executable, score_transient_script, bridge_json, output_dir, external_data_dir]
                    subprocess.run(cmd_transient, check=False)

                    with open(bridge_json) as f:
                        updated_bridge = json.load(f)
                    for i, res in enumerate(encoder_results):
                        key = f"res_{res['row_key']}_{i}"
                        if key in updated_bridge.get("matrix", {}):
                            res["attack_centroid_ms"] = updated_bridge["matrix"][key].get("attack_centroid_ms")

    # Decoder Benchmarking Phase
    decoder_results = []
    decoder_robustness_results = []
    decoders = []
    if run_decoders:
        decoders = detect_decoders(args)
        if not decoders:
            print("No decoders detected!")
            if not run_encoders:
                sys.exit(1)
        else:
            print(f"Detected decoders: {', '.join(d.name for d in decoders)}")

        valid_encoder_bitstreams = [r for r in encoder_results if r.get("decode_valid") and r.get("aac_path") and os.path.exists(r["aac_path"])]
        if args.gate and valid_encoder_bitstreams:
            gate_filenames = set()
            for s_name in scenario_list:
                gate_filenames.update(GATE_CLIPS.get(s_name, []))
            if gate_filenames:
                valid_encoder_bitstreams = [r for r in valid_encoder_bitstreams if r.get("filename") in gate_filenames]
        if valid_encoder_bitstreams and decoders:
            print(f"\n>>> Running Decoder Benchmarks across {len(valid_encoder_bitstreams)} bitstreams x {len(decoders)} decoders...")
            for decoder in decoders:
                print(f"  Decoding with {decoder.name}...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                    futures = [executor.submit(process_decoder_task, decoder, item, output_dir) for item in valid_encoder_bitstreams]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            decoder_results.append(res)

            print(f"\n>>> Running Decoder Robustness Pass (Corrupted Bitstreams)...")
            for decoder in decoders:
                print(f"  Testing robustness for {decoder.name}...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_cpus) as executor:
                    futures = [executor.submit(process_decoder_robustness_task, decoder, item, output_dir) for item in valid_encoder_bitstreams]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            decoder_robustness_results.append(res)

            # Phase 2 MOS calculation for decoded WAVs if MOS is enabled
            if not args.skip_mos and decoder_results:
                print("\n>>> Phase 2: Perceptual Quality (MOS) for Decoders")
                dec_bridge_data = {"matrix": {}}
                valid_dec_count = 0
                for i, res in enumerate(decoder_results):
                    if not res.get("decoded_wav") or not os.path.exists(res["decoded_wav"]):
                        continue
                    key = f"dec_res_{res['row_key']}_{i}"
                    dec_bridge_data["matrix"][key] = {
                        "scenario": res["scenario"],
                        "filename": res["filename"],
                        "aac": os.path.basename(res["decoded_wav"]),
                        "mos": None
                    }
                    valid_dec_count += 1

                if valid_dec_count > 0:
                    dec_bridge_json = "dec_bridge_results.json"
                    with open(dec_bridge_json, "w") as f:
                        json.dump(dec_bridge_data, f, indent=2)

                    phase2_script = os.path.join(SCRIPT_DIR, "phase2_mos.py")
                    cmd_phase2 = [sys.executable, phase2_script, dec_bridge_json, output_dir, external_data_dir]
                    subprocess.run(cmd_phase2, check=False)

                    if os.path.exists(dec_bridge_json):
                        with open(dec_bridge_json) as f:
                            updated_bridge = json.load(f)
                        for i, res in enumerate(decoder_results):
                            key = f"dec_res_{res['row_key']}_{i}"
                            if key in updated_bridge.get("matrix", {}):
                                res["mos"] = updated_bridge["matrix"][key].get("mos")

    # Final Leaderboard Generation
    out_file = args.output
    if run_encoders and run_decoders and decoders:
        generate_leaderboard(encoders, encoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, has_decoders=True)
        dec_file = out_file.replace(".md", "_decoders.md") if out_file.endswith(".md") else out_file + "_decoders"
        generate_decoder_leaderboard(decoders, decoder_results, dec_file, scenario_list, skip_graphs=args.skip_graphs, encoders=encoders, encoder_results=encoder_results, robustness_results=decoder_robustness_results, run_encoders_leaderboard=True)

        if os.path.exists(dec_file) and dec_file != out_file:
            with open(dec_file) as f_dec:
                dec_md = f_dec.read()
            with open(out_file, "a") as f_main:
                f_main.write("\n\n---\n\n" + dec_md)
            try:
                os.remove(dec_file)
            except OSError:
                pass
    elif run_encoders:
        generate_leaderboard(encoders, encoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs)
    elif run_decoders and decoders:
        generate_decoder_leaderboard(decoders, decoder_results, out_file, scenario_list, skip_graphs=args.skip_graphs, robustness_results=decoder_robustness_results)

if __name__ == "__main__":
    main()
