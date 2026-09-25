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

import statistics

from utils import (get_binary_size, get_elf_section_sizes, get_ffmpeg_path,
                   get_faad_path, ffmpeg_probe, decode_validate, find_linked_lib,
                   resolve_wrapper_target, is_system_library, flatten_arg_list,
                   probe_version, make_unique_name_and_id, compute_snr, safe_run,
                   measure_delay_offset, measure_peak_ram, corrupt_adts_bitstream,
                   get_cached_ref_wav, scenario_channels, scenario_rate, wav_conv, corpus_dir)

# Robustness runaway threshold: a corrupted-bitstream decode whose output PCM
# exceeds this multiple of the intact decode's size did not fail cleanly --
# it kept synthesizing audio past where the real stream ended (typically a
# desynced frame-length field read as huge). Left unbounded this has filled
# the output directory with multi-gigabyte WAVs; see process_decoder_robustness_task.
ROBUSTNESS_RUNAWAY_FACTOR = 4

# A decoder's conformance SNR against the ffmpeg reference decode at or above
# this floor means the two decodes are perceptually identical, so the
# ffmpeg row's already-computed MOS is inherited instead of re-scoring
# (mos_source: "inherited" vs "scored"). Must match gate_check's
# CONFORMANCE_SNR_FLOOR_DB -- kept as a separate constant since gate_check
# uses it as a pass/fail gate and this uses it as a scoring-reuse threshold.
CONFORMANCE_SNR_FLOOR_DB = 60.0

if sys.platform == "darwin":
    os.environ["NUMBA_THREADING_LAYER"] = "workqueue"
else:
    os.environ.setdefault("NUMBA_THREADING_LAYER", "omp")

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


class FDKDecoder(Decoder):
    """Wraps scripts/fdkdec (Homebrew libfdk-aac): decodes MP4/M4A via
    TT_MP4_RAW + FAAC's mp4read.c for gapless trim, or raw ADTS via
    TT_MP4_ADTS. fdkdec writes the gapless-trimmed decode straight to
    output_path (plus an untrimmed "<stem>_raw.wav" alongside, for
    debugging), so no wrapper is needed."""
    def __init__(self, name, binary_path, tool_id="fdk_aac_dec"):
        super().__init__(name, binary_path, tool_id, lib_name_substr="libfdk-aac")

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


def get_decoder_instance(decoder_type="ffmpeg", binary_path=None, lib_override=None, version=None):
    """Instantiates a Decoder instance for a given decoder type or binary path."""
    decoder_type = (decoder_type or "ffmpeg").lower().strip()

    if decoder_type in ("ffmpeg", "ffmpeg_aac"):
        f_bin = binary_path or get_ffmpeg_path()
        ver = version
        if not ver and f_bin and os.path.exists(f_bin):
            res = safe_run([f_bin, "-version"], capture_output=True, check=False)
            stdout_str = res.stdout if isinstance(res.stdout, str) else str(res.stdout or "")
            m = re.search(r"ffmpeg version (\S+)", stdout_str)
            ver = m.group(1) if m else None
        display_name = f"FFmpeg AAC {ver}" if ver else "FFmpeg AAC"
        return FFmpegDecoder(display_name, f_bin, "ffmpeg_aac")

    elif decoder_type in ("faad", "faad2", "faad3"):
        f_bin = binary_path or get_faad_path()
        raw_text = ""
        if f_bin and os.path.exists(f_bin):
            for flag in ["-h", "--help", "-v"]:
                try:
                    res = subprocess.run([f_bin, flag], capture_output=True, text=True, timeout=5)
                    raw_text += (res.stdout or "") + (res.stderr or "")
                except Exception:
                    pass
        is_faad3 = bool(re.search(r"Freeware Advanced Audio Decoder|FAAD3", raw_text, re.IGNORECASE))
        base_name = "FAAD3" if is_faad3 else "FAAD2"
        base_id = "faad3" if is_faad3 else "faad2"
        ver = version
        if not ver and f_bin and os.path.exists(f_bin):
            ver = probe_version(f_bin, ["-h", "--help", "-v"],
                                [r"Freeware Advanced Audio Decoder\s*\(v?(\d+\.\d+(?:\.\d+)*)\)",
                                 r"FAAD2\s+v?(\d+\.\d+(?:\.\d+)*)",
                                 r"Decoder\s+V?(\d+\.\d+(?:\.\d+)*)",
                                 r"version\s+(\d+\.\d+(?:\.\d+)*)"])
        display_name = f"{base_name} {ver}" if ver else base_name
        return FAADDecoder(display_name, f_bin, base_id, lib_override=lib_override)

    elif decoder_type in ("helix", "helix_aac", "helix-aac-dec"):
        h_bin = binary_path
        if not h_bin:
            script_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            default_bin = os.path.join(script_root, "bin", "helix-aac-dec")
            if os.path.exists(default_bin):
                h_bin = default_bin
            else:
                build_script = os.path.join(script_root, "scripts", "build_helix_aac.sh")
                if os.path.exists(build_script):
                    try:
                        res = safe_run([build_script], capture_output=True, check=False)
                        if res.returncode == 0 and res.stdout.strip() and os.path.exists(res.stdout.strip()):
                            h_bin = res.stdout.strip()
                    except Exception:
                        pass
        ver = version
        if not ver and h_bin and os.path.exists(h_bin):
            ver = probe_version(h_bin, ["--version", "-v", "-h"], [r"Helix AAC Decoder v?(\d+\.\d+(?:\.\d+)*)"])
        display_name = f"Helix AAC {ver}" if ver else "Helix AAC"
        return HelixAACDecoder(display_name, h_bin, "helix_aac")

    elif decoder_type in ("fdkdec", "fdk", "fdk_aac", "fdk_aac_dec"):
        f_bin = binary_path
        if not f_bin:
            script_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            default_bin = os.path.join(script_root, "bin", "fdkdec")
            if os.path.exists(default_bin):
                f_bin = default_bin
            else:
                build_script = os.path.join(script_root, "scripts", "build_fdkdec.sh")
                if os.path.exists(build_script):
                    try:
                        res = safe_run([build_script], capture_output=True, check=False)
                        if res.returncode == 0 and res.stdout.strip() and os.path.exists(res.stdout.strip()):
                            f_bin = res.stdout.strip()
                    except Exception:
                        pass
        ver = version
        if not ver and f_bin and os.path.exists(f_bin):
            ver = probe_version(f_bin, ["-v", "--version"], [r"libfdk-aac\s+(\d+\.\d+(?:\.\d+)*)"])
        display_name = f"FDK AAC {ver}" if ver else "FDK AAC"
        return FDKDecoder(display_name, f_bin, "fdk_aac_dec")

    elif decoder_type in ("afconvert", "apple"):
        a_bin = binary_path or shutil.which("afconvert")
        ver = version
        if not ver and a_bin and os.path.exists(a_bin):
            ver = probe_version(a_bin, ["-h"], [r"afconvert\s+version\s+(\d+\.\d+(?:\.\d+)*)", r"version\s+(\d+\.\d+(?:\.\d+)*)"])
            if not ver and sys.platform == "darwin":
                try:
                    import platform
                    mac_v = platform.mac_ver()[0]
                    if mac_v:
                        ver = mac_v
                except Exception:
                    pass
        display_name = f"Apple AAC {ver}" if ver else "Apple AAC"
        return AFConvertDecoder(display_name, a_bin, "afconvert")

    else:
        # If binary_path is provided directly or decoder_type is a file path
        if os.path.exists(decoder_type):
            # Infer type from binary filename
            base = os.path.basename(decoder_type).lower()
            if "faad" in base:
                return get_decoder_instance("faad", binary_path=decoder_type, lib_override=lib_override, version=version)
            elif "helix" in base:
                return get_decoder_instance("helix", binary_path=decoder_type, version=version)
            elif "fdk" in base:
                return get_decoder_instance("fdkdec", binary_path=decoder_type, version=version)
            elif "afconvert" in base:
                return get_decoder_instance("afconvert", binary_path=decoder_type, version=version)
            else:
                return get_decoder_instance("ffmpeg", binary_path=decoder_type, version=version)
        raise ValueError(f"Unknown decoder type or invalid binary path: {decoder_type}")


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
        f_lib = faad_libs[idx] if idx < len(faad_libs) else None
        ver = faad_vers[idx] if idx < len(faad_vers) else None
        dec = get_decoder_instance("faad", binary_path=f_bin, lib_override=f_lib, version=ver)
        name, tool_id = make_unique_name_and_id(dec.name.rsplit(" ", 1)[0] if " " in dec.name else dec.name,
                                                dec.name.rsplit(" ", 1)[1] if " " in dec.name else None,
                                                dec.tool_id, existing_names, existing_ids)
        dec.name = name
        dec.tool_id = tool_id
        if probe_decoder_capability(dec):
            decoders.append(dec)

    ffmpeg_raw = getattr(args, "ffmpeg_bin", None)
    if isinstance(ffmpeg_raw, list):
        ffmpeg_bin = ffmpeg_raw[0] if ffmpeg_raw else None
    else:
        ffmpeg_bin = ffmpeg_raw or get_ffmpeg_path()

    if ffmpeg_bin and os.path.exists(ffmpeg_bin):
        dec = get_decoder_instance("ffmpeg", binary_path=ffmpeg_bin)
        name, tool_id = make_unique_name_and_id(dec.name.rsplit(" ", 1)[0] if " " in dec.name else dec.name,
                                                dec.name.rsplit(" ", 1)[1] if " " in dec.name else None,
                                                dec.tool_id, existing_names, existing_ids)
        dec.name = name
        dec.tool_id = tool_id
        if probe_decoder_capability(dec):
            decoders.append(dec)

    afconvert_bin = getattr(args, "afconvert_bin", None) or shutil.which("afconvert")
    if afconvert_bin and os.path.exists(afconvert_bin):
        dec = get_decoder_instance("afconvert", binary_path=afconvert_bin)
        name, tool_id = make_unique_name_and_id(dec.name.rsplit(" ", 1)[0] if " " in dec.name else dec.name,
                                                dec.name.rsplit(" ", 1)[1] if " " in dec.name else None,
                                                dec.tool_id, existing_names, existing_ids)
        dec.name = name
        dec.tool_id = tool_id
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
            dec = get_decoder_instance("helix", binary_path=h_bin)
            name, tool_id = make_unique_name_and_id(dec.name.rsplit(" ", 1)[0] if " " in dec.name else dec.name,
                                                    dec.name.rsplit(" ", 1)[1] if " " in dec.name else None,
                                                    dec.tool_id, existing_names, existing_ids)
            dec.name = name
            dec.tool_id = tool_id
            if probe_decoder_capability(dec):
                decoders.append(dec)

    fdkdec_bins = flatten_arg_list(getattr(args, "fdkdec_bin", None))
    if not fdkdec_bins:
        script_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_bin = os.path.join(script_root, "bin", "fdkdec")
        if os.path.exists(default_bin):
            fdkdec_bins = [default_bin]
        else:
            build_script = os.path.join(script_root, "scripts", "build_fdkdec.sh")
            if os.path.exists(build_script):
                try:
                    res = safe_run([build_script], capture_output=True, check=False)
                    if res.returncode == 0 and res.stdout.strip() and os.path.exists(res.stdout.strip()):
                        fdkdec_bins = [res.stdout.strip()]
                except Exception:
                    pass

    for f_bin in fdkdec_bins:
        if os.path.exists(f_bin):
            dec = get_decoder_instance("fdkdec", binary_path=f_bin)
            name, tool_id = make_unique_name_and_id(dec.name.rsplit(" ", 1)[0] if " " in dec.name else dec.name,
                                                    dec.name.rsplit(" ", 1)[1] if " " in dec.name else None,
                                                    dec.tool_id, existing_names, existing_ids)
            dec.name = name
            dec.tool_id = tool_id
            if probe_decoder_capability(dec):
                decoders.append(dec)

    return decoders


def get_conformance_ref_wav(aac_path, cache_dir):
    """FFmpeg's own decode of aac_path, cached by content hash so every
    decoder under test in a run reuses the same one conformance reference
    instead of re-decoding it once per decoder."""
    if not cache_dir or not aac_path or not os.path.exists(aac_path):
        return None
    os.makedirs(cache_dir, exist_ok=True)
    import hashlib
    key = hashlib.sha1(os.path.abspath(aac_path).encode()).hexdigest()[:16]
    cached = os.path.join(cache_dir, f"conformance_ref_{key}.wav")
    if os.path.exists(cached):
        return cached
    # Must end in .wav: ffmpeg infers the muxer from the output extension,
    # and a bare ".tmp<pid>" suffix makes it refuse to write anything.
    tmp = os.path.join(cache_dir, f"conformance_ref_{key}.tmp{os.getpid()}.wav")
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    try:
        res = safe_run([ffmpeg_bin, "-y", "-i", aac_path, "-sample_fmt", "s16", tmp], capture_output=True, check=False)
        if res.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, cached)
            return cached
    except Exception:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None


def get_conformance_ref_offset(ref_path, ffmpeg_ref_wav, cache_dir):
    """Sample offset of the cached ffmpeg reference decode vs the original
    source WAV, disk-cached by (ref_path, ffmpeg_ref_wav) like
    get_conformance_ref_wav so it is computed once per bitstream and every
    decoder under test in a run reuses it, instead of each one
    cross-correlating its own output against the original from scratch.
    Returns (lag_samples, lag_ms), either possibly None on failure."""
    if not cache_dir or not ref_path or not ffmpeg_ref_wav or not os.path.exists(ref_path) or not os.path.exists(ffmpeg_ref_wav):
        return None, None
    os.makedirs(cache_dir, exist_ok=True)
    import hashlib
    import json
    key = hashlib.sha1(f"{os.path.abspath(ref_path)}|{os.path.abspath(ffmpeg_ref_wav)}".encode()).hexdigest()[:16]
    cached = os.path.join(cache_dir, f"conformance_offset_{key}.json")
    if os.path.exists(cached):
        try:
            with open(cached) as f:
                data = json.load(f)
            return data.get("lag_samples"), data.get("lag_ms")
        except Exception:
            pass

    lag_samples, lag_ms = measure_delay_offset(ref_path, ffmpeg_ref_wav)
    tmp = cached + f".tmp{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump({"lag_samples": lag_samples, "lag_ms": lag_ms}, f)
        os.replace(tmp, cached)
    except Exception:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return lag_samples, lag_ms


def measure_decode_speed(decoder, bitstream_input, output_dir, iterations, audio_duration):
    """Repeats a decode `iterations` times with the output discarded after
    each run, returning mean/std latency, x-realtime, and PCM throughput.
    None when iterations <= 1 (the single timed decode already covers that
    case) or when any repeat fails."""
    if iterations <= 1:
        return None
    scratch = os.path.join(output_dir, f"speed_scratch_{os.getpid()}.wav")
    latencies_ms = []
    pcm_bytes = None
    try:
        for _ in range(iterations):
            res, dur, _ram = measure_peak_ram(decoder.get_decode_cmd(bitstream_input, scratch), env=decoder.get_run_env() or None)
            if res.returncode != 0 or not os.path.exists(scratch):
                return None
            latencies_ms.append(dur * 1000.0)
            if pcm_bytes is None:
                pcm_bytes = max(0, os.path.getsize(scratch) - 44)
    finally:
        if os.path.exists(scratch):
            try:
                os.remove(scratch)
            except OSError:
                pass

    mean_ms = statistics.mean(latencies_ms)
    std_ms = statistics.pstdev(latencies_ms) if len(latencies_ms) > 1 else 0.0
    xrt = (audio_duration * 1000.0 / mean_ms) if (audio_duration and mean_ms > 0) else None
    mbps = (pcm_bytes / (mean_ms / 1000.0) / (1024 * 1024)) if (pcm_bytes and mean_ms > 0) else None
    return {"mean_ms": mean_ms, "std_ms": std_ms, "xrt": xrt, "mbps": mbps, "iterations": iterations}


def process_decoder_task(decoder, res_item, output_dir, skip_mos=False, ref_cache_dir=None, iterations=1, keep_decodes=False):
    aac_path = res_item.get("aac_path")
    ref_path = res_item.get("ref_path")
    scenario_name = res_item["scenario"]
    sample = res_item["filename"]
    container = "ADTS" if aac_path and aac_path.lower().endswith((".aac", ".adts")) else "M4A"

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
            "mos_source": None,
            "conformance_snr_db": None,
            "alignment_delay_ms": None,
            "gapless_offset_samples": None,
            "gapless_length_delta": None,
            "container": container,
            "peak_ram_kb": None,
            "decoded_wav": None
        }

    output_filename = f"dec_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}.wav".replace(" ", "_")
    output_path = os.path.join(output_dir, output_filename)

    requires_adts = getattr(decoder, "requires_adts", False)
    temp_adts = None
    bitstream_input = aac_path

    if requires_adts and aac_path.lower().endswith((".m4a", ".mp4")):
        temp_adts = os.path.join(output_dir, f"demux_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}.aac".replace(" ", "_"))
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
                "mos_source": None,
                "conformance_snr_db": None,
                "alignment_delay_ms": None,
                "gapless_offset_samples": None,
                "gapless_length_delta": None,
                "container": container,
                "peak_ram_kb": None,
                "decoded_wav": None
            }

        valid, decode_err = decode_validate(output_path)
        snr_db = None
        conformance_snr_db = None
        alignment_delay_ms = None
        gapless_offset_samples = None
        gapless_length_delta = None
        mos_val = None
        mos_source = None
        dec_channels = None
        mono_downmix = False
        speed_stats = None

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
                # Codec-quality SNR vs the uncompressed source (includes the
                # encoder's own lossy error, not just decoder bugs).
                snr_db = compute_snr(ref_path, output_path)
                try:
                    with wave.open(ref_path, "rb") as rw, wave.open(output_path, "rb") as dw:
                        gapless_length_delta = dw.getnframes() - rw.getnframes()
                except Exception:
                    gapless_length_delta = None

                # FFmpeg decoding itself IS the conformance reference; every
                # other decoder is measured against it, the same way as the
                # codec-quality snr_db above but against the reference
                # *decode* rather than the source WAV -- this isolates
                # decoder bugs from encoder lossy-compression error. That
                # reference decode is produced/cached once per bitstream (by
                # the encoder phase, or here on first use) and reused by
                # every decoder under test in this run rather than
                # re-decoded per decoder.
                ffmpeg_ref_wav = get_conformance_ref_wav(aac_path, ref_cache_dir)
                base_lag, base_lag_ms = (None, None)
                if ffmpeg_ref_wav:
                    # The reference decode's own offset vs the original is
                    # also computed once per bitstream and cached; every
                    # decoder's alignment vs the original is then this base
                    # offset plus its own (much cheaper, already-aligned)
                    # offset vs the reference decode, instead of each decoder
                    # re-correlating a full window against the original.
                    base_lag, base_lag_ms = get_conformance_ref_offset(ref_path, ffmpeg_ref_wav, ref_cache_dir)

                if decoder.tool_id == "ffmpeg_aac":
                    conformance_snr_db = "ref"
                    gapless_offset_samples, alignment_delay_ms = base_lag, base_lag_ms
                    if not skip_mos and res_item.get("mos") is not None:
                        # This *is* the ffmpeg decode the encoder phase already
                        # scored -- reuse that MOS instead of re-scoring it.
                        mos_val, mos_source = res_item.get("mos"), "inherited"
                else:
                    rel_lag, rel_lag_ms = (None, None)
                    if ffmpeg_ref_wav:
                        conformance_snr_db = compute_snr(ffmpeg_ref_wav, output_path)
                        rel_lag, rel_lag_ms = measure_delay_offset(ffmpeg_ref_wav, output_path)

                    if base_lag is not None and rel_lag is not None:
                        gapless_offset_samples = base_lag + rel_lag
                        alignment_delay_ms = (base_lag_ms or 0.0) + (rel_lag_ms or 0.0)

                    if not skip_mos:
                        inherit_ok = (isinstance(conformance_snr_db, (int, float))
                                      and conformance_snr_db >= CONFORMANCE_SNR_FLOOR_DB
                                      and res_item.get("mos") is not None)
                        if inherit_ok:
                            # Perceptually identical to the ffmpeg decode
                            # already scored in the encoder phase -- reuse
                            # its MOS instead of launching a fresh scorer.
                            mos_val, mos_source = res_item.get("mos"), "inherited"
                        else:
                            mos_source = "scored"
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

                                    import phase2_mos
                                    mos_val, _backend = phase2_mos.score_wav_pair(ref_wav, dec_wav, mode_str=mode_str)
                            except Exception as e:
                                print(f"Decoder MOS calculation failed for {scenario_name}/{sample}: {e}")

        audio_duration = ffmpeg_probe(ref_path) if ref_path else None

        if valid and iterations > 1:
            speed_stats = measure_decode_speed(decoder, bitstream_input, output_dir, iterations, audio_duration)

        result = {
            "tool": decoder.name,
            "row_key": decoder_row_key(decoder),
            "encoder_row_key": res_item["row_key"],
            "scenario": scenario_name,
            "filename": sample,
            "profile": res_item.get("profile", "lc"),
            "container": container,
            "duration": duration,
            "audio_duration": audio_duration,
            "decode_valid": valid,
            "decode_error": decode_err,
            "mos": mos_val,
            "mos_source": mos_source,
            "snr_db": snr_db,
            "conformance_snr_db": conformance_snr_db,
            "alignment_delay_ms": alignment_delay_ms,
            "gapless_offset_samples": gapless_offset_samples,
            "gapless_length_delta": gapless_length_delta,
            "peak_ram_kb": peak_ram_kb,
            "decoded_wav": output_path if keep_decodes else None,
            "dec_channels": dec_channels,
            "mono_downmix": mono_downmix,
            "speed_mean_ms": speed_stats["mean_ms"] if speed_stats else None,
            "speed_std_ms": speed_stats["std_ms"] if speed_stats else None,
            "speed_xrt": speed_stats["xrt"] if speed_stats else None,
            "speed_mbps": speed_stats["mbps"] if speed_stats else None,
        }

        if not keep_decodes and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

        return result
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
            "mos_source": None,
            "conformance_snr_db": None,
            "alignment_delay_ms": None,
            "gapless_offset_samples": None,
            "gapless_length_delta": None,
            "container": container,
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
        temp_adts = os.path.join(output_dir, f"demux_rob_{decoder_row_key(decoder)}_{res_item['row_key']}_{scenario_name}_{sample}.aac".replace(" ", "_"))
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
    runaway = False
    is_crash = False
    is_timeout = False
    is_error_exit = False
    passed = False

    try:
        res, duration, _peak_ram = measure_peak_ram(cmd, env=decoder.get_run_env() or None)
        stderr_low = (res.stderr or "").lower()
        is_timeout = (res.returncode == -124) or ("timed out" in stderr_low) or ("timeout" in stderr_low)
        is_crash = (res.returncode < 0) or ("segmentation fault" in stderr_low) or ("aborted" in stderr_low) or ("bus error" in stderr_low)

        if not is_timeout and not is_crash:
            if res.returncode == 0:
                passed = True
                if os.path.exists(dec_corrupt_wav):
                    cfg = SCENARIOS.get(scenario_name, {})
                    ref_path = res_item.get("ref_path")
                    duration_s = ffmpeg_probe(ref_path) if ref_path else None
                    if duration_s and cfg:
                        expected_bytes = duration_s * scenario_rate(cfg) * scenario_channels(cfg) * 2 + 44
                        if os.path.getsize(dec_corrupt_wav) > ROBUSTNESS_RUNAWAY_FACTOR * expected_bytes:
                            runaway = True
                            passed = False
            else:
                is_error_exit = True
    except BaseException as e:
        e_str = str(e).lower()
        if "timed out" in e_str or "timeout" in e_str:
            is_timeout = True
        else:
            is_crash = True
    finally:
        if temp_adts and os.path.exists(temp_adts):
            try:
                os.remove(temp_adts)
            except OSError:
                pass
        for scratch in (corrupt_path, dec_corrupt_wav):
            if os.path.exists(scratch):
                try:
                    os.remove(scratch)
                except OSError:
                    pass

    crash_free = (passed or is_error_exit) and not is_timeout and not runaway and not is_crash

    return {
        "tool": decoder.name,
        "row_key": decoder_row_key(decoder),
        "encoder_row_key": res_item["row_key"],
        "scenario": scenario_name,
        "filename": sample,
        "passed": passed,
        "error_exit": is_error_exit,
        "crash": is_crash,
        "timeout": is_timeout,
        "runaway": runaway,
        "crash_free": crash_free
    }
