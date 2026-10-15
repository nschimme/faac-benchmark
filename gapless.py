"""
 * FAAC Benchmark Suite — Gapless Playback check
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

 Does a gapless-aware decoder reproduce the source exactly?

 An MP4 carries the encoder delay and padding in an edit list (elst) and in an
 iTunSMPB tag. Without them a player cannot trim the priming samples, and every
 track starts late and ends long. This phase reads, per Phase 1 stream:

   tags         whether the file carries an elst and/or an iTunSMPB
   frame_error  decoded frames minus source frames, at the source rate
   lag          sample offset of the decode against the source (0 = aligned)

 with the pinned FAAD2, which trims by those tags. Builds that predate the tags
 write neither and decode with whatever offset their muxer happened to produce,
 so the gate (compare_results.check_gapless) never asks an untagged stream to be
 exact: it asks a tagged one to be, and a stream that lost its tags to stay lost.
"""

import argparse
import json
import math
import os
import struct
import subprocess
import tempfile

import numpy as np
import scipy.signal
import soundfile as sf

from codec_bench.decoders import pinned_faad2_path
from phase2_mos import get_sample_info
from utils import MAX_LAG_S, scenario_rate, wav_conv

MAX_STREAMS = 100       # deterministic stride over the matrix; each costs one decode
SEARCH_S = 3            # seconds of audio the lag is measured on


def _boxes(buf, start, end):
    """(type, payload start, box end) of the ISO-BMFF boxes in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
        header = 8
        if size == 1 and pos + 16 <= end:
            size, header = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def mp4_gapless_tags(path):
    """{"elst": bool, "itunsmpb": bool} for an MP4 file.

    The edit list is found by walking moov/trak/edts; iTunSMPB is a free-form
    iTunes tag, searched for inside moov only so media data cannot match it."""
    with open(path, "rb") as f:
        buf = f.read()
    tags = {"elst": False, "itunsmpb": False}
    for kind, start, end in _boxes(buf, 0, len(buf)):
        if kind != b"moov":
            continue
        tags["itunsmpb"] = b"iTunSMPB" in buf[start:end]
        for kind2, s2, e2 in _boxes(buf, start, end):
            if kind2 != b"trak":
                continue
            for kind3, s3, e3 in _boxes(buf, s2, e2):
                if kind3 == b"edts":
                    tags["elst"] |= any(k == b"elst" for k, _, _ in _boxes(buf, s3, e3))
    return tags


def phat_lag(ref, dec, rate):
    """Sample offset of dec against ref (positive: dec starts early) within the codec-delay window.

    A plain cross-correlation is dominated by whatever carries the most energy, and a
    low-rate decode keeps the bass and little else: its peak lands on a bass cycle and
    wanders by thousands of samples with the window. Whitening the spectrum first
    (phase transform) makes every bin vote for the delay alike, so the answer is the
    alignment itself and does not depend on which part of the clip is measured."""
    n = len(ref)
    size = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(ref, size) * np.conj(np.fft.rfft(dec, size))
    spec /= np.abs(spec) + 1e-12 * np.abs(spec).max()
    corr = np.fft.irfft(spec, size)
    reach = min(n - 1, int(MAX_LAG_S * rate))
    lags = np.concatenate([np.arange(reach + 1), np.arange(-reach, 0)])
    return int(lags[np.argmax(np.concatenate([corr[:reach + 1], corr[size - reach:]]))])


def measure(aac_path, ref_input, rate, channels, faad):
    """(frame_error, lag, decoded rate) of one stream against its source."""
    with tempfile.TemporaryDirectory() as td:
        ref_wav, dec_wav = os.path.join(td, "ref.wav"), os.path.join(td, "dec.wav")
        if not wav_conv(ref_input, ref_wav, rate, channels):
            raise RuntimeError("could not convert the source")
        proc = subprocess.run([faad, "-q", "-o", dec_wav, aac_path], capture_output=True, text=True, timeout=120)
        if proc.returncode or not os.path.exists(dec_wav):
            raise RuntimeError((proc.stderr or proc.stdout).strip() or f"faad exit {proc.returncode}")
        ref, ref_rate = sf.read(ref_wav, dtype="float32", always_2d=True)
        dec, dec_rate = sf.read(dec_wav, dtype="float32", always_2d=True)

    # FAAD2 may emit a different rate than the source (it doubles low-rate LC on
    # an implicit-SBR guess); compare at the source rate.
    if dec_rate != ref_rate:
        g = math.gcd(ref_rate, dec_rate)
        dec = scipy.signal.resample_poly(dec, ref_rate // g, dec_rate // g, axis=0).astype("float32")
    r, d = ref.mean(axis=1), dec.mean(axis=1)
    n = min(len(r), len(d), ref_rate * SEARCH_S)
    if n == 0:
        raise RuntimeError("empty decode")
    return len(dec) - len(ref), phat_lag(r[:n], d[:n], ref_rate), dec_rate


def measure_matrix(data, aac_dir, external_data_dir, results_path, faad):
    keys = sorted(data.get("matrix", {}))
    if len(keys) > MAX_STREAMS:
        keys = keys[::math.ceil(len(keys) / MAX_STREAMS)][:MAX_STREAMS]
    block = {"streams": 0, "cases": {}, "failed": []}
    for key in keys:
        entry = data["matrix"][key]
        if not str(entry.get("aac", "")).endswith((".m4a", ".mp4")):
            continue
        info = get_sample_info(key, entry, aac_dir, external_data_dir, results_path)
        if not info or not info["aac_path"] or not os.path.exists(info["aac_path"]):
            continue
        cfg = info["cfg"]
        block["streams"] += 1
        case = {"file": entry.get("aac"), "scenario": entry.get("scenario"), "profile": entry.get("object_type")}
        try:
            case.update(mp4_gapless_tags(info["aac_path"]))
            case["frame_error"], case["lag"], _ = measure(
                info["aac_path"], info["ref_input_path"], scenario_rate(cfg), info["v_channels"], faad)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            block["failed"].append({"file": entry.get("aac"), "error": str(exc)})
            continue
        block["cases"][key] = case
    return block


def store_block(path, block):
    with open(path) as f:
        data = json.load(f)
    data["gapless"] = block
    tmp = str(path) + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def summary(block):
    if block is not None and "error" in block:
        return f"gapless: DID NOT RUN ({block['error']})"
    if not block or "skipped" in block:
        return "gapless: skipped (" + (block or {}).get("skipped", "no gapless block") + ")"
    cases = block["cases"].values()
    tagged = sum(1 for c in cases if c["elst"] or c["itunsmpb"])
    return f"gapless: {tagged} of {len(block['cases'])} streams carry gapless tags"


def main():
    parser = argparse.ArgumentParser(description="Gapless playback check")
    parser.add_argument("results_json", help="Result JSON to update in place")
    parser.add_argument("aac_dir", help="Phase 1 output directory")
    parser.add_argument("external_data_dir", help="Reference corpus directory")
    parser.add_argument("--faad-bin", help="Gapless-aware faad (default: the pinned FAAD2)")
    parser.add_argument("--snapshot", help="Read the matrix from this copy of the result JSON (it may be rewritten while this runs)")
    parser.add_argument("--out", help="Write the block to this file instead of into the result JSON")
    args = parser.parse_args()
    with open(args.snapshot or args.results_json) as f:
        data = json.load(f)
    faad = args.faad_bin or pinned_faad2_path()
    block = measure_matrix(data, args.aac_dir, args.external_data_dir, args.results_json, faad)
    if not block["cases"]:
        block = {"skipped": "no MP4 streams could be measured" + (f" ({block['failed'][0]['error']})" if block["failed"] else "")}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(block, f, allow_nan=False)
    else:
        store_block(args.results_json, block)
    print(summary(block))


if __name__ == "__main__":
    main()
