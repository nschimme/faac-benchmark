"""
 * FAAC Benchmark Suite - Encoder Classes & Auto-Detection
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
import shutil
import tempfile
import wave
import json
import re

from utils import (get_binary_size, get_elf_section_sizes, get_ffmpeg_path,
                   safe_run, find_linked_lib, is_faac_legacy, resolve_wrapper_target,
                   guess_lib_version_from_path, is_system_library, flatten_arg_list,
                   probe_version, make_unique_name_and_id, hosted_codec_ver)

PROFILE_LABELS = {"lc": "LC", "he": "HE-v1", "hev2": "HE-v2", "standard": "Standard"}

def profile_label(profile):
    return PROFILE_LABELS.get(profile, profile.upper())

def encoder_row_key(encoder):
    """Stable identity key for an (encoder_tool, profile) combination."""
    return f"{encoder.tool_id}_{encoder.profile}"

row_key = encoder_row_key

def use_he_aac(bitrate_kbps, channels, sample_rate):
    """Heuristic for when HE-AAC v1 is selected for standard AAC encoders."""
    if sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / max(1, channels)
    return 8 <= bitrate_per_ch <= 48

def use_he_v2_aac(bitrate_kbps, channels, sample_rate):
    """Heuristic for when HE-AAC v2 (Parametric Stereo) is selected."""
    if channels < 2 or sample_rate < 32000:
        return False
    bitrate_per_ch = bitrate_kbps / max(1, channels)
    return 6 <= bitrate_per_ch <= 20


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


class FAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="faac", profile="lc", lib_override=None):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfaac", lib_override=lib_override)
        self.legacy = is_faac_legacy(binary_path, lib_override=lib_override)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        cmd = [self.binary_path, "-b", str(bitrate_kbps), "--overwrite", "-o", output_path]
        if not self.legacy:
            obj_type = "he-aac-v1" if self.profile == "he" else "lc"
            cmd.extend(["--object-type", obj_type])
        cmd.append(input_path)
        return cmd


class FFmpegEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="ffmpeg_aac", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=None)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        cmd = [self.binary_path, "-y", "-i", input_path, "-c:a"]
        if self.profile == "lc":
            cmd.extend(["aac", "-b:a", f"{bitrate_kbps}k"])
        elif self.profile == "he":
            cmd.extend(["libfdk_aac", "-profile:a", "aac_he", "-b:a", f"{bitrate_kbps}k"])
        elif self.profile == "hev2":
            cmd.extend(["libfdk_aac", "-profile:a", "aac_he_v2", "-b:a", f"{bitrate_kbps}k"])
        cmd.extend(["-ac", str(channels), output_path])
        return cmd


class FDKAACEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="fdkaac", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="libfdk-aac")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        m_val = "29" if self.profile == "hev2" else ("5" if self.profile == "he" else "2")
        bps = bitrate_kbps * 1000
        return [self.binary_path, "-m", m_val, "-b", str(bps), "-o", output_path, input_path]


class AACEncEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="aac_enc", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=None)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        t_val = "29" if self.profile == "hev2" else ("5" if self.profile == "he" else "2")
        bps = bitrate_kbps * 1000
        return [self.binary_path, "-t", t_val, "-r", str(bps), "-o", output_path, input_path]


class FalabaacEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="falabaac", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=None)

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        return [self.binary_path, "-b", str(bitrate_kbps), "-o", output_path, input_path]


class AFConvertEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="afconvert", profile="lc"):
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr="AudioToolbox")

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        d_val = "aacp" if self.profile == "hev2" else ("aach" if self.profile == "he" else "aac")
        bps = bitrate_kbps * 1000
        return [self.binary_path, "-f", "m4af", "-d", d_val, "-b", str(bps), "-c", str(channels), input_path, output_path]


class OpusEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="opusenc", profile="standard", is_ffmpeg=False):
        lib_substr = "libopus" if is_ffmpeg else "libopus"
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=lib_substr)
        self.is_ffmpeg = is_ffmpeg
        self.file_ext = ".opus"

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.is_ffmpeg:
            return [self.binary_path, "-y", "-i", input_path, "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path]
        return [self.binary_path, "--bitrate", str(bitrate_kbps), input_path, output_path]


class LameEncoder(Encoder):
    def __init__(self, name, binary_path, tool_id="lame", profile="standard", is_ffmpeg=False):
        lib_substr = "libmp3lame" if is_ffmpeg else "libmp3lame"
        super().__init__(name, binary_path, tool_id, profile, lib_name_substr=lib_substr)
        self.is_ffmpeg = is_ffmpeg
        self.file_ext = ".mp3"

    def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
        if self.is_ffmpeg:
            return [self.binary_path, "-y", "-i", input_path, "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), output_path]
        return [self.binary_path, "-b", str(bitrate_kbps), "-s", str(sample_rate / 1000.0), input_path, output_path]


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

    with tempfile.TemporaryDirectory() as td:
        dummy_wav = os.path.join(td, "test.wav")
        dummy_m4a = os.path.join(td, "test.m4a")
        try:
            with wave.open(dummy_wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(b"\x00\x00" * 44100)
            res = safe_run([faac_path, "-b", "64", "-o", dummy_m4a, dummy_wav], capture_output=True, check=False, env=env)
            stdout_stderr = (res.stdout or "") + "\n" + (res.stderr or "")
            m = re.search(r"FAAC\s+v?(\d+\.\d+(?:\.\d+)*[a-z0-9.]*(?:\s+\([^)]+\))?)", stdout_stderr, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    return None


def probe_encoder_capability(encoder, bitrate_kbps=None, channels=2, sample_rate=44100):
    if bitrate_kbps is None:
        bitrate_kbps = 16 if encoder.profile in ("he", "hev2") else 64
    supported, reason = encoder.supports_scenario(bitrate_kbps, channels, sample_rate)
    if not supported:
        return False
    with tempfile.TemporaryDirectory() as td:
        dummy_wav = os.path.join(td, "test.wav")
        out_file = os.path.join(td, f"test{encoder.file_ext}")
        try:
            with wave.open(dummy_wav, "wb") as w:
                w.setnchannels(channels)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(b"\x00\x00" * sample_rate)
            cmd = encoder.get_encode_cmd(dummy_wav, out_file, bitrate_kbps, channels, sample_rate)
            res = safe_run(cmd, env=encoder.get_run_env() or None, capture_output=True, check=False)
            return res.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0
        except Exception:
            return False


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

        enc_lc = FAACEncoder(name, f_bin, tool_id, "lc", lib_override=f_lib)
        if probe_encoder_capability(enc_lc):
            encoders.append(enc_lc)

        if not legacy:
            enc_he = FAACEncoder(name, f_bin, tool_id, "he", lib_override=f_lib)
            if probe_encoder_capability(enc_he):
                encoders.append(enc_he)

    fdkaac_bins = flatten_arg_list(getattr(args, "fdkaac_bin", None))
    if not fdkaac_bins:
        which_fdk = shutil.which("fdkaac")
        if which_fdk:
            fdkaac_bins = [which_fdk]

    for f_bin in fdkaac_bins:
        ver = probe_version(f_bin, ["-h", "--help"], [r"fdkaac\s+v?(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("fdkaac", ver, "fdkaac", existing_names, existing_ids)

        for p in ["lc", "he", "hev2"]:
            enc = FDKAACEncoder(name, f_bin, tool_id, p)
            if probe_encoder_capability(enc):
                encoders.append(enc)

    aac_enc_bins = flatten_arg_list(getattr(args, "aac_enc_bin", None))
    if not aac_enc_bins:
        which_aacenc = shutil.which("aac-enc")
        if which_aacenc:
            aac_enc_bins = [which_aacenc]

    if not fdkaac_bins:
        for a_bin in aac_enc_bins:
            ver = probe_version(a_bin, ["-h", "--help", "-v"], [r"aac-enc\s+v?(\d+\.\d+(?:\.\d+)*)"])
            name, tool_id = make_unique_name_and_id("aac-enc", ver, "aac_enc", existing_names, existing_ids)

            for p in ["lc", "he", "hev2"]:
                enc = AACEncEncoder(name, a_bin, tool_id, p)
                if probe_encoder_capability(enc):
                    encoders.append(enc)

    falabaac_bins = flatten_arg_list(getattr(args, "falabaac_bin", None))
    if not falabaac_bins:
        which_fala = shutil.which("falabaac")
        if which_fala:
            falabaac_bins = [which_fala]

    for f_bin in falabaac_bins:
        ver = probe_version(f_bin, ["-h", "--help", "-v"], [r"falabaac\s+v?(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("falabaac", ver, "falabaac", existing_names, existing_ids)
        enc = FalabaacEncoder(name, f_bin, tool_id, "lc")
        if probe_encoder_capability(enc):
            encoders.append(enc)

    ffmpeg_bin = getattr(args, "ffmpeg_bin", None) or get_ffmpeg_path()
    if ffmpeg_bin and os.path.exists(ffmpeg_bin):
        res = safe_run([ffmpeg_bin, "-version"], capture_output=True, check=False)
        m = re.search(r"ffmpeg version (\S+)", res.stdout)
        ffmpeg_ver = m.group(1) if m else None

        res_codecs = safe_run([ffmpeg_bin, "-codecs"], capture_output=True, check=False)
        has_native_aac = bool(re.search(r"^\s*[A-Z.]*E[A-Z.]*\s+aac\b", res_codecs.stdout, re.M)) or (" aac " in res_codecs.stdout)
        has_libfdk = "libfdk_aac" in res_codecs.stdout

        if has_native_aac:
            res_encoders = safe_run([ffmpeg_bin, "-h", "encoder=aac"], capture_output=True, check=False)
            has_nmr = bool(re.search(r"\bnmr\b", res_encoders.stdout, re.IGNORECASE))
            disp_name = f"FFmpeg AAC {ffmpeg_ver} (NMR experimental)" if has_nmr else f"FFmpeg AAC {ffmpeg_ver}" if ffmpeg_ver else "FFmpeg AAC"
            name, tool_id = make_unique_name_and_id(disp_name, None, "ffmpeg_aac", existing_names, existing_ids)
            enc_lc = FFmpegEncoder(name, ffmpeg_bin, tool_id, "lc")
            if probe_encoder_capability(enc_lc):
                encoders.append(enc_lc)

        if has_libfdk:
            ver_label = hosted_codec_ver(ffmpeg_bin, "libfdk-aac", ffmpeg_ver)
            base_name = f"FFmpeg libfdk-aac {ver_label}" if ver_label else "FFmpeg libfdk-aac"
            name, tool_id = make_unique_name_and_id(base_name, None, "ffmpeg_libfdk", existing_names, existing_ids)
            for p in ["lc", "he", "hev2"]:
                enc = FFmpegEncoder(name, ffmpeg_bin, tool_id, p)
                if probe_encoder_capability(enc):
                    encoders.append(enc)

    afconvert_bin = getattr(args, "afconvert_bin", None) or shutil.which("afconvert")
    if afconvert_bin and os.path.exists(afconvert_bin):
        ver = probe_version(afconvert_bin, ["-h"], [r"afconvert\s+version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("Apple AudioToolbox", ver, "afconvert", existing_names, existing_ids)
        for p in ["lc", "he", "hev2"]:
            enc = AFConvertEncoder(name, afconvert_bin, tool_id, p)
            if probe_encoder_capability(enc):
                encoders.append(enc)

    if getattr(args, "include_other_codecs", False):
        opusenc_bin = getattr(args, "opusenc_bin", None) or shutil.which("opusenc") or ffmpeg_bin
        if opusenc_bin and os.path.exists(opusenc_bin):
            ver = probe_version(opusenc_bin, ["-V", "--version", "-version"], [r"opusenc\s+[\w\s.-]+\s+v?(\d+\.\d+(?:\.\d+)*)"])
            if not ver and ffmpeg_ver:
                ver = hosted_codec_ver(opusenc_bin, "libopus", ffmpeg_ver)
            name, tool_id = make_unique_name_and_id("Opus", ver, "opus", existing_names, existing_ids)
            enc = OpusEncoder(name, opusenc_bin, tool_id, "standard")
            if probe_encoder_capability(enc, bitrate_kbps=64):
                encoders.append(enc)

        lame_bin = getattr(args, "lame_bin", None) or shutil.which("lame") or ffmpeg_bin
        if lame_bin and os.path.exists(lame_bin):
            ver = probe_version(lame_bin, ["--version", "-version"], [r"LAME\s+(?:64bits\s+)?version\s+(\d+\.\d+(?:\.\d+)*)"])
            if not ver and ffmpeg_ver:
                ver = hosted_codec_ver(lame_bin, "libmp3lame", ffmpeg_ver)
            name, tool_id = make_unique_name_and_id("LAME MP3", ver, "lame", existing_names, existing_ids)
            enc = LameEncoder(name, lame_bin, tool_id, "standard")
            if probe_encoder_capability(enc, bitrate_kbps=128):
                encoders.append(enc)

    return encoders
