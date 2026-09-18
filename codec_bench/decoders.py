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
                   probe_version, make_unique_name_and_id, compute_snr, safe_run,
                   measure_delay_offset, measure_peak_ram, corrupt_adts_bitstream,
                   get_cached_ref_wav, scenario_channels, wav_conv, corpus_dir)

if sys.platform == "darwin":
    os.environ["NUMBA_THREADING_LAYER"] = "workqueue"
else:
    os.environ.setdefault("NUMBA_THREADING_LAYER", "omp")

import phase2_mos
from config import SCENARIOS

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


class HelixAACDecoder(Decoder):
    def __init__(self, name, binary_path, tool_id="helix_aac"):
        super().__init__(name, binary_path, tool_id, lib_name_substr=None)
        self.requires_adts = True

    def get_decode_cmd(self, input_path, output_path):
        return [self.binary_path, input_path, output_path]


import tempfile
import wave

def probe_decoder_capability(decoder):
    """Probes whether a decoder binary exists and is capable of decoding."""
    if not decoder.binary_path:
        return False
    if not os.path.exists(decoder.binary_path):
        return True

    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        return True

    with tempfile.TemporaryDirectory() as td:
        dummy_wav = os.path.join(td, "test_ref.wav")
        dummy_aac = os.path.join(td, "test.aac")
        dummy_out = os.path.join(td, "test_out.wav")

        try:
            with wave.open(dummy_wav, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(b"\x00\x00" * 44100)

            cmd_enc = [ffmpeg_bin, "-y", "-i", dummy_wav, "-c:a", "aac", "-b:a", "64k", "-ac", "2", dummy_aac]
            res_enc = safe_run(cmd_enc, capture_output=True, check=False)
            if res_enc.returncode != 0 or not os.path.exists(dummy_aac):
                return True

            bitstream_input = dummy_aac
            if getattr(decoder, "requires_adts", False):
                demux_aac = os.path.join(td, "test_demux.aac")
                cmd_demux = [ffmpeg_bin, "-y", "-i", dummy_aac, "-c:a", "copy", demux_aac]
                res_demux = safe_run(cmd_demux, capture_output=True, check=False)
                if res_demux.returncode == 0 and os.path.exists(demux_aac):
                    bitstream_input = demux_aac

            cmd_dec = decoder.get_decode_cmd(bitstream_input, dummy_out)
            res_dec, _duration, _ram = measure_peak_ram(cmd_dec, env=decoder.get_run_env() or None)

            return res_dec.returncode == 0 and os.path.exists(dummy_out) and os.path.getsize(dummy_out) > 0
        except Exception:
            return True


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
        name, tool_id = make_unique_name_and_id("FAAD", ver, "faad2", existing_names, existing_ids)
        dec = FAADDecoder(name, f_bin, tool_id, lib_override=f_lib)
        if probe_decoder_capability(dec):
            decoders.append(dec)

    ffmpeg_raw = getattr(args, "ffmpeg_bin", None)
    if isinstance(ffmpeg_raw, list):
        ffmpeg_bin = ffmpeg_raw[0] if ffmpeg_raw else None
    else:
        ffmpeg_bin = ffmpeg_raw or get_ffmpeg_path()

    if ffmpeg_bin and os.path.exists(ffmpeg_bin):
        res = safe_run([ffmpeg_bin, "-version"], capture_output=True, check=False)
        m = re.search(r"ffmpeg version (\S+)", res.stdout)
        ffmpeg_ver = m.group(1) if m else None
        name, tool_id = make_unique_name_and_id("FFmpeg AAC", ffmpeg_ver, "ffmpeg_aac", existing_names, existing_ids)
        dec = FFmpegDecoder(name, ffmpeg_bin, tool_id)
        if probe_decoder_capability(dec):
            decoders.append(dec)

    afconvert_bin = getattr(args, "afconvert_bin", None) or shutil.which("afconvert")
    if afconvert_bin and os.path.exists(afconvert_bin):
        ver = probe_version(afconvert_bin, ["-h"], [r"afconvert\s+version\s+(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        if not ver and sys.platform == "darwin":
            try:
                import platform
                mac_v = platform.mac_ver()[0]
                if mac_v:
                    ver = mac_v
            except Exception:
                pass
        name, tool_id = make_unique_name_and_id("Apple AAC", ver, "afconvert", existing_names, existing_ids)
        dec = AFConvertDecoder(name, afconvert_bin, tool_id)
        if probe_decoder_capability(dec):
            decoders.append(dec)

    helix_bins = flatten_arg_list(getattr(args, "helix_bin", None))
    if not helix_bins:
        script_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_bin = os.path.join(script_root, "bin", "helix-aac-dec")
        if os.path.exists(default_bin):
            helix_bins = [default_bin]
        else:
            build_script = os.path.join(script_root, "scripts", "build_helix_aac.sh")
            if os.path.exists(build_script):
                try:
                    res = safe_run([build_script], capture_output=True, check=False)
                    if res.returncode == 0 and res.stdout.strip() and os.path.exists(res.stdout.strip()):
                        helix_bins = [res.stdout.strip()]
                except Exception:
                    pass

    for h_bin in helix_bins:
        if os.path.exists(h_bin):
            ver = probe_version(h_bin, ["--version", "-v", "-h"], [r"Helix AAC Decoder v?(\d+\.\d+(?:\.\d+)*)"])
            name, tool_id = make_unique_name_and_id("Helix AAC", ver or "1.0", "helix_aac", existing_names, existing_ids)
            dec = HelixAACDecoder(name, h_bin, tool_id)
            if probe_decoder_capability(dec):
                decoders.append(dec)

    return decoders


def process_decoder_task(decoder, res_item, output_dir, skip_mos=False, ref_cache_dir=None):
    aac_path = res_item.get("aac_path")
    ref_path = res_item.get("ref_path")
    scenario_name = res_item["scenario"]
    sample = res_item["filename"]

    if not ref_path or not os.path.exists(ref_path):
        cfg = SCENARIOS.get(scenario_name, {})
        if cfg:
            external_data_dir = os.environ.get("EXTERNAL_DATA_DIR") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "external")
            fallback_ref = os.path.join(corpus_dir(cfg, external_data_dir), sample)
            if os.path.exists(fallback_ref):
                ref_path = fallback_ref

    if not aac_path or not os.path.exists(aac_path):
        return {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "profile": res_item.get("profile", "lc"),
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

    requires_adts = getattr(decoder, "requires_adts", False)
    temp_adts = None
    bitstream_input = aac_path

    if requires_adts and aac_path.lower().endswith((".m4a", ".mp4")):
        temp_adts = os.path.join(output_dir, f"demux_{decoder_row_key(decoder)}_{scenario_name}_{sample}.aac".replace(" ", "_"))
        cmd_demux = [get_ffmpeg_path() or "ffmpeg", "-y", "-i", aac_path, "-c:a", "copy", temp_adts]
        try:
            res_demux = safe_run(cmd_demux, capture_output=True, check=False)
            if res_demux.returncode == 0 and os.path.exists(temp_adts) and os.path.getsize(temp_adts) > 0:
                bitstream_input = temp_adts
        except Exception:
            pass

    cmd = decoder.get_decode_cmd(bitstream_input, output_path)

    try:
        res, duration, peak_ram_kb = measure_peak_ram(cmd, env=decoder.get_run_env() or None)

        if res.returncode != 0:
            stderr_text = res.stderr.decode(errors="replace") if isinstance(res.stderr, bytes) else (res.stderr or "")
            stderr_clean = stderr_text.strip()
            is_timeout = (res.returncode == -124) or ("timed out" in stderr_clean.lower()) or ("timeout" in stderr_clean.lower())
            if is_timeout:
                err_detail = "Timeout expired"
            elif stderr_clean:
                stderr_tail = next((l for l in reversed(stderr_clean.splitlines()) if l.strip()), "")
                err_detail = f"exit code {res.returncode}: {stderr_tail}"
            elif res.returncode < 0:
                err_detail = f"Process terminated by signal {-res.returncode}"
            else:
                err_detail = f"exit code {res.returncode}"

            return {
                "tool": decoder.name,
                "row_key": decoder_row_key(decoder),
                "encoder_row_key": res_item["row_key"],
                "scenario": scenario_name,
                "filename": sample,
                "profile": res_item.get("profile", "lc"),
                "duration": 0,
                "audio_duration": None,
                "decode_valid": False,
                "decode_error": err_detail if is_timeout else f"Decode failed: {err_detail}",
                "timeout": is_timeout,
                "snr_db": None,
                "alignment_delay_ms": None,
                "peak_ram_kb": None,
                "decoded_wav": None
            }

        valid, decode_err = decode_validate(output_path)
        snr_db = None
        alignment_delay_ms = None
        mos_val = None
        dec_channels = None
        mono_downmix = False

        if valid:
            try:
                with wave.open(output_path, "rb") as w:
                    dec_channels = w.getnchannels()
            except Exception:
                dec_channels = None

            cfg = SCENARIOS.get(scenario_name, {})
            v_rate = cfg.get("visqol_rate") or cfg.get("rate") or 48000
            v_channels = scenario_channels(cfg) if cfg else 2
            mode_str = cfg.get("mode", "audio")

            if v_channels >= 2 and dec_channels == 1:
                mono_downmix = True

            if ref_path and os.path.exists(ref_path):
                snr_db = compute_snr(ref_path, output_path)
                _lag_samples, alignment_delay_ms = measure_delay_offset(ref_path, output_path)

                if not skip_mos:
                    try:
                        with tempfile.TemporaryDirectory() as td:
                            ref_wav = get_cached_ref_wav(ref_cache_dir or td, ref_path, v_rate, v_channels) if ref_cache_dir else None
                            if not ref_wav:
                                ref_wav = os.path.join(td, "ref_conv.wav")
                                if not wav_conv(ref_path, ref_wav, rate=v_rate, channels=v_channels):
                                    ref_wav = ref_path

                            dec_wav = os.path.join(td, "dec_conv.wav")
                            if not wav_conv(output_path, dec_wav, rate=v_rate, channels=v_channels):
                                dec_wav = output_path

                            mos_val, _backend = phase2_mos.score_wav_pair(ref_wav, dec_wav, mode_str=mode_str)
                    except Exception as e:
                        print(f"Decoder MOS calculation failed for {scenario_name}/{sample}: {e}")

        audio_duration = ffmpeg_probe(ref_path) if ref_path else None

        return {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "profile": res_item.get("profile", "lc"),
            "duration": duration,
            "audio_duration": audio_duration,
            "decode_valid": valid,
            "decode_error": decode_err,
            "mos": mos_val,
            "snr_db": snr_db,
            "alignment_delay_ms": alignment_delay_ms,
            "peak_ram_kb": peak_ram_kb,
            "decoded_wav": output_path,
            "dec_channels": dec_channels,
            "mono_downmix": mono_downmix
        }
    except BaseException as e:
        detail = str(e)
        is_timeout = False
        if isinstance(e, subprocess.CalledProcessError):
            stderr_text = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            if e.returncode == -124 or "timed out" in stderr_text.lower() or "timeout" in stderr_text.lower():
                is_timeout = True
            stderr_tail = next((l for l in reversed(stderr_text.splitlines()) if l.strip()), "")
            if stderr_tail:
                detail = f"exit code {e.returncode}: {stderr_tail}"
        elif "timed out" in detail.lower() or "timeout" in detail.lower():
            is_timeout = True

        err_msg = "Timeout expired" if is_timeout else f"Decode failed: {detail}"
        return {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "profile": res_item.get("profile", "lc"),
            "duration": 0,
            "audio_duration": None,
            "decode_valid": False,
            "decode_error": err_msg,
            "timeout": is_timeout,
            "snr_db": None,
            "alignment_delay_ms": None,
            "peak_ram_kb": None,
            "decoded_wav": None
        }
    finally:
        if temp_adts and os.path.exists(temp_adts):
            try:
                os.remove(temp_adts)
            except OSError:
                pass


def process_decoder_robustness_task(decoder, res_item, output_dir):
    aac_path = res_item.get("aac_path")
    if not aac_path or not os.path.exists(aac_path):
        return None

    scenario_name = res_item["scenario"]
    sample = res_item["filename"]

    requires_adts = getattr(decoder, "requires_adts", False)
    temp_adts = None
    bitstream_input = aac_path

    if requires_adts and aac_path.lower().endswith((".m4a", ".mp4")):
        temp_adts = os.path.join(output_dir, f"demux_rob_{decoder_row_key(decoder)}_{scenario_name}_{sample}.aac".replace(" ", "_"))
        cmd_demux = [get_ffmpeg_path() or "ffmpeg", "-y", "-i", aac_path, "-c:a", "copy", temp_adts]
        try:
            res_demux = safe_run(cmd_demux, capture_output=True, check=False)
            if res_demux.returncode == 0 and os.path.exists(temp_adts) and os.path.getsize(temp_adts) > 0:
                bitstream_input = temp_adts
        except Exception:
            pass

    corrupt_filename = f"corrupt_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}"
    corrupt_path = os.path.join(output_dir, corrupt_filename)
    dec_corrupt_wav = os.path.join(output_dir, f"{corrupt_filename}.wav")

    if not corrupt_adts_bitstream(bitstream_input, corrupt_path, seed=42):
        if temp_adts and os.path.exists(temp_adts):
            os.remove(temp_adts)
        return None

    cmd = decoder.get_decode_cmd(corrupt_path, dec_corrupt_wav)
    try:
        res, duration, _peak_ram = measure_peak_ram(cmd, env=decoder.get_run_env() or None)
        passed = (res.returncode == 0)
        is_timeout = (res.returncode == -124) or ("timed out" in (res.stderr or "").lower())
    except BaseException as e:
        passed = False
        is_timeout = "timed out" in str(e).lower() or "timeout" in str(e).lower()
    finally:
        if temp_adts and os.path.exists(temp_adts):
            try:
                os.remove(temp_adts)
            except OSError:
                pass

    return {
        "tool": decoder.name,
        "row_key": decoder_row_key(decoder),
        "encoder_row_key": res_item["row_key"],
        "scenario": scenario_name,
        "filename": sample,
        "passed": passed,
        "timeout": is_timeout
    }
