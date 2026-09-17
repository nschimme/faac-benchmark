"""
 * FAAC Benchmark Suite - Decoder Classes & Auto-Detection
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
import re

from utils import (get_binary_size, get_elf_section_sizes, get_ffmpeg_path,
                   get_faad_path, ffmpeg_probe, decode_validate, find_linked_lib,
                   resolve_wrapper_target, is_system_library, flatten_arg_list,
                   probe_version, make_unique_name_and_id, compute_snr,
                   measure_delay_offset, measure_peak_ram, corrupt_adts_bitstream)

def decoder_row_key(decoder):
    """Stable identity key for a decoder tool."""
    return f"{decoder.tool_id}"


class Decoder:
    def __init__(self, name, binary_path, tool_id, lib_name_substr=None, lib_override=None):
        self.name = name
        self.binary_path = binary_path
        self.tool_id = tool_id
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


def detect_decoders(args):
    decoders = []
    existing_names = set()
    existing_ids = set()

    faad_bins = flatten_arg_list(getattr(args, "faad_bin", None))
    faad_libs = flatten_arg_list(getattr(args, "faad_lib", None))
    faad_vers = flatten_arg_list(getattr(args, "faad_bin_version", None))
    if not faad_bins:
        faad_path = get_faad_path()
        if faad_path:
            faad_bins = [faad_path]

    for idx, f_bin in enumerate(faad_bins):
        f_lib = faad_libs[idx] if idx < len(faad_libs) else (faad_libs[0] if faad_libs else None)
        ver = faad_vers[idx] if idx < len(faad_vers) else None
        if not ver:
            ver = probe_version(f_bin, ["-h", "--help", "-v"],
                                [r"FAAD2\s+v?(\d+\.\d+(?:\.\d+)*)",
                                 r"Decoder\s+V?(\d+\.\d+(?:\.\d+)*)",
                                 r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("FAAD2", ver, "faad2", existing_names, existing_ids)
        decoders.append(FAADDecoder(name, f_bin, tool_id, lib_override=f_lib))

    ffmpeg_raw = getattr(args, "ffmpeg_bin", None)
    if isinstance(ffmpeg_raw, list):
        ffmpeg_bin = ffmpeg_raw[0] if ffmpeg_raw else None
    else:
        ffmpeg_bin = ffmpeg_raw or get_ffmpeg_path()

    if ffmpeg_bin and os.path.exists(ffmpeg_bin):
        res = subprocess.run([ffmpeg_bin, "-version"], capture_output=True, text=True)
        m = re.search(r"ffmpeg version (\S+)", res.stdout)
        ffmpeg_ver = m.group(1) if m else None
        name, tool_id = make_unique_name_and_id("FFmpeg AAC", ffmpeg_ver, "ffmpeg_aac", existing_names, existing_ids)
        decoders.append(FFmpegDecoder(name, ffmpeg_bin, tool_id))

    afconvert_bin = getattr(args, "afconvert_bin", None) or shutil.which("afconvert")
    if afconvert_bin and os.path.exists(afconvert_bin):
        ver = probe_version(afconvert_bin, ["-h"], [r"afconvert\s+version\s+(\d+\.\d+(?:\.\d+)*)"])
        name, tool_id = make_unique_name_and_id("Apple AudioToolbox", ver, "afconvert", existing_names, existing_ids)
        decoders.append(AFConvertDecoder(name, afconvert_bin, tool_id))

    return decoders


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
            "decode_error": f"Decode failed: {detail}",
            "snr_db": None,
            "alignment_delay_ms": None,
            "peak_ram_kb": None,
            "decoded_wav": None
        }


def process_decoder_robustness_task(decoder, res_item, output_dir):
    aac_path = res_item.get("aac_path")
    if not aac_path or not os.path.exists(aac_path):
        return None

    scenario_name = res_item["scenario"]
    sample = res_item["filename"]

    corrupt_filename = f"corrupt_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}"
    corrupt_path = os.path.join(output_dir, corrupt_filename)
    dec_corrupt_wav = os.path.join(output_dir, f"{corrupt_filename}.wav")

    if not corrupt_adts_bitstream(aac_path, corrupt_path, seed=42):
        return None

    cmd = decoder.get_decode_cmd(corrupt_path, dec_corrupt_wav)
    try:
        res, duration, _peak_ram = measure_peak_ram(cmd, env=decoder.get_run_env() or None)
        passed = (res.returncode == 0)
    except Exception:
        passed = False

    return {
        "tool": decoder.name,
        "row_key": decoder_row_key(decoder),
        "encoder_row_key": res_item["row_key"],
        "scenario": scenario_name,
        "filename": sample,
        "passed": passed
    }
