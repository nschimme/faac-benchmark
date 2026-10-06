"""
 * FAAC Benchmark Suite — Phase 4: Bass Haze
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

 Why this phase exists
 ---------------------
 A loud, stationary bass that plays alone (nothing above ~1 kHz within 53 dB of
 it) is coded through windows whose sidelobes spread the bass over the whole
 spectrum. On a clean source that spread is the loudest thing above the bass, so
 it is quantized away and returns as broadband "haze". The music corpora never
 contain such a passage, and the haze sits so far under the bass that MOS
 (Phase 2) barely moves: on a real clip with the defect the same haze costs 0.89
 MOS when the content above 1 kHz is scored alone and 0.10 when scored with the
 bass. So neither the corpus nor MOS can see it.

 This phase encodes small deterministic synthetic clips (generated here, no
 download) with fixed options, decodes them and measures the decoded audio, with
 no perceptual model and no knowledge of the stream's syntax:

   haze_db     power of the decoded span between 1.2 and 8 kHz minus the
               source's, in dB (0 = no excess; the clip's own floor is the
               24-bit rounding of the source)
   pre_db      for the attack clip: the error energy (decoded minus source,
               below 8 kHz) in the 20 ms before each kick, in dB

 The treble control (the same bass plus quiet broadband treble) is a known
 answer: it has no haze to remove, so it must read about 0 dB. The attack clip
 (kicks over silence) guards the opposite risk: a change that lets bass-heavy
 frames go long must not smear real attacks, so the error just before each kick
 must not grow.

 Like Phase 3 this is a regression guard for a property the other phases are
 blind to, not a perceptual ground truth. The gate (compare_results.check_haze)
 compares the candidate with the baseline run; it asserts no absolute level.
"""

import os
import sys
import json
import wave
import argparse
import tempfile

import numpy as np

from codec_bench.encoders import get_encoder_instance
from utils import get_ffmpeg_path, safe_run

SR = 48000
LEAD_S = 1.0                    # digital silence before the bass
BASS_S = 6.0
SPAN_S = (2.5, 5.5)             # analysed span: inside the bass, past the onset
ATTACK_ONSETS_S = (1.0, 2.0, 3.0, 4.0, 5.0)
PRE_ONSET_N = 960               # 20 ms before each kick
PRE_ONSET_LP_HZ = 8000.0        # the HE core's band; SBR is not waveform-matched above it
PRE_ONSET_CONTEXT = 4096
BAND_HZ = (1200.0, 8000.0)      # the haze band: the waveform-coded core, not SBR (HE codes
                                # the band above it parametrically, so its level is not leakage)
BASS_HZ = 58.0                  # period (827 samples) is longer than the 256-sample
                                # block-switching window, which the HE core's
                                # first-difference test trips on
TREBLE_FLOOR_DB = -50.0         # control clip: broadband treble, dB re full scale
TREBLE_CUTOFF_HZ = 1500.0

# (name, role, clip, profile, rate_control, value). "vbr" values are -q, "abr" -b (total kbps).
# Roles: "bass" must not get hazier; "control" has no haze and must read about 0 dB;
# "attack" must not smear its kicks.
CASES = [
    ("lc_q200",      "bass",    "bass",    "lc", "vbr", 200),
    ("he_q50",       "bass",    "bass",    "he", "vbr", 50),
    ("he_b32",       "bass",    "bass",    "he", "abr", 32),
    ("ctl_lc_q200",  "control", "control", "lc", "vbr", 200),
    ("ctl_he_q50",   "control", "control", "he", "vbr", 50),
    ("atk_he_q50",   "attack",  "attack",  "he", "vbr", 50),
]


def bass_clip(treble=False):
    """Stereo float clip in [-1, 1], rounded to the 24-bit grid. Deterministic."""
    t = np.arange(int(BASS_S * SR)) / SR
    env = 1.0 + 0.15 * np.sin(2 * np.pi * 0.4 * t)
    f = BASS_HZ
    b = env * (np.sin(2 * np.pi * f * t)
               + 0.08 * np.sin(2 * np.pi * 2 * f * t + 1)
               + 0.04 * np.sin(2 * np.pi * 3 * f * t + 2))
    b *= 0.12 / np.abs(b).max()
    lead = np.zeros(int(LEAD_S * SR))
    x = np.stack([np.concatenate([lead, b]), 0.98 * np.concatenate([lead, b])], axis=1)

    if treble:
        rng = np.random.default_rng(9)
        n = len(x)
        spec = np.fft.rfft(rng.standard_normal(n))
        spec[np.fft.rfftfreq(n, 1.0 / SR) < TREBLE_CUTOFF_HZ] = 0
        h = np.fft.irfft(spec, n)
        h *= 10 ** (TREBLE_FLOOR_DB / 20) / np.sqrt((h ** 2).mean())
        x[:, 0] += h
        x[:, 1] += np.roll(h, 777)
        x[:len(lead)] = 0.0

    return np.round(np.clip(x, -1.0, 1.0) * (2 ** 23 - 1)) / (2 ** 23)


def attack_clip():
    """Five kicks over digital silence, one a second: a pitch-dropping 50-170 Hz
    tone with a short click, so each onset is an attack the encoder codes short."""
    x = np.zeros((int(6.5 * SR), 2))
    t = np.arange(int(0.35 * SR)) / SR
    for k in range(1, 6):
        phase = 2 * np.pi * np.cumsum(50 + 120 * np.exp(-t * 30)) / SR
        kick = 0.5 * np.exp(-t * 9) * np.sin(phase)
        kick[:40] += 0.2 * np.random.default_rng(k).standard_normal(40) * np.exp(-np.arange(40) / 10)
        n = int(k * SR)
        x[n:n + len(kick)] += kick[:, None]
    return np.round(np.clip(x, -1.0, 1.0) * (2 ** 23 - 1)) / (2 ** 23)


def write_wav24(path, x):
    ints = np.round(x * (2 ** 23 - 1)).astype(np.int32)
    raw = ints.astype("<i4").tobytes()
    packed = b"".join(raw[i:i + 3] for i in range(0, len(raw), 4))
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(3)
        w.setframerate(SR)
        w.writeframes(packed)


def band_power_db(x, lo, hi, n=2048):
    """Mean Hann-windowed power of x between lo and hi Hz, in dB."""
    w = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    frames = 0
    for start in range(0, len(x) - n, n // 2):
        acc += np.abs(np.fft.rfft(x[start:start + n] * w)) ** 2
        frames += 1
    f = np.fft.rfftfreq(n, 1.0 / SR)
    power = acc[(f >= lo) & (f < hi)].sum() / max(frames, 1) / (w ** 2).sum()
    return 10 * np.log10(power + 1e-30)


def haze_db(source, decoded):
    """Decoded minus source power in the haze band over the span, left channel."""
    a, b = (int(t * SR) for t in SPAN_S)
    return float(band_power_db(decoded[a:b], *BAND_HZ) - band_power_db(source[a:b], *BAND_HZ))


def decode_lag(source, decoded, search=8192):
    """Samples by which the decoded stream trails the source (codec delay)."""
    n = min(len(source), len(decoded) - search)
    size = 1 << int(np.ceil(np.log2(n + search)))
    xc = np.fft.irfft(np.fft.rfft(decoded[:n + search], size)
                      * np.conj(np.fft.rfft(source[:n], size)), size)[:search]
    return int(np.argmax(xc))


def pre_onset_db(source, decoded):
    """Error energy (decoded minus source, below PRE_ONSET_LP_HZ) in the 20 ms before
    each kick, in dB, one value per kick. A transform codec that spreads an attack
    backwards leaves energy there: pre-echo."""
    lag = decode_lag(source, decoded)
    out = []
    for onset in ATTACK_ONSETS_S:
        t = int(onset * SR)
        lo = t - PRE_ONSET_CONTEXT
        # Only samples before the kick go into the filter: a zero-phase low-pass
        # spreads energy both ways, and the coding error of the kick itself would
        # otherwise bleed backwards into the window being measured.
        err = decoded[lo + lag:t + lag] - source[lo:t]
        spec = np.fft.rfft(err)
        spec[np.fft.rfftfreq(len(err), 1.0 / SR) > PRE_ONSET_LP_HZ] = 0
        err = np.fft.irfft(spec, len(err))[-PRE_ONSET_N:]
        out.append(float(10 * np.log10((err ** 2).sum() + 1e-12)))
    return out


def decode_left(path):
    """Left channel of the decoded stream as floats. ffmpeg writes raw samples to a
    file rather than a pipe, because safe_run decodes its output as text."""
    raw = path + ".f64"
    try:
        safe_run([get_ffmpeg_path(), "-nostdin", "-v", "error", "-y", "-i", path,
                  "-f", "f64le", "-ac", "2", "-ar", str(SR), raw])
        return np.fromfile(raw, "<f8").reshape(-1, 2)[:, 0]
    finally:
        if os.path.exists(raw):
            os.remove(raw)


def measure_case(role, source, encoded_path):
    """Metrics of one encoded stream against its source clip (left channel)."""
    decoded = decode_left(encoded_path)
    result = {"role": role, "bytes": os.path.getsize(encoded_path),
              "haze_db": None, "pre_db": None}
    if role == "attack":
        result["pre_db"] = pre_onset_db(source[:, 0], decoded)
    else:
        result["haze_db"] = haze_db(source[:, 0], decoded)
    return result


def run_case(encoder_cache, tmp, clips, case, encoder_bin, encoder_lib):
    name, role, clip, profile, rc, value = case
    if profile not in encoder_cache:
        encoder_cache[profile] = get_encoder_instance(
            "faac", binary_path=encoder_bin, lib_override=encoder_lib, profile=profile)
    enc = encoder_cache[profile]
    if profile != "lc" and getattr(enc, "legacy", False):
        return None                     # a legacy faac has no --object-type: it would encode LC
    out = os.path.join(tmp, f"{name}{enc.file_ext}")
    cmd = enc.get_encode_cmd(clips[clip][0], out, value, 2, SR,
                             rate_control=rc, vbr_q=value if rc == "vbr" else None)
    safe_run(cmd, env=enc.get_run_env() or None)
    return measure_case(role, clips[clip][1], out)


def store_block(path, block):
    """Add the haze block to the results JSON. The file is replaced only once the new
    content is fully written, so an interrupted run cannot leave a truncated file."""
    with open(path, "r") as f:
        data = json.load(f)
    data["haze"] = block
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def describe(m):
    if m["pre_db"] is not None:
        return "pre-onset " + " ".join(f"{v:+.1f}" for v in m["pre_db"]) + " dB"
    return f"haze {m['haze_db']:+.1f} dB"


def main():
    parser = argparse.ArgumentParser(description="Phase 4: bass haze regression guard")
    parser.add_argument("results_json", help="Path to results JSON file (the block is added in place)")
    parser.add_argument("--encoder-bin", help="Path to the faac binary")
    parser.add_argument("--encoder-lib", help="Path to a libfaac override")
    args = parser.parse_args()

    if not get_ffmpeg_path():
        print("  Phase 4 needs ffmpeg to decode the test streams and found none")
        sys.exit(1)

    cases = {}
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        clips = {}
        for key, x in (("bass", bass_clip()), ("control", bass_clip(True)), ("attack", attack_clip())):
            path = os.path.join(tmp, f"{key}.wav")
            write_wav24(path, x)
            clips[key] = (path, x)
        cache = {}
        for case in CASES:
            try:
                m = run_case(cache, tmp, clips, case, args.encoder_bin, args.encoder_lib)
            except Exception as e:      # one case failing must not hide the others
                print(f"  haze case {case[0]} failed: {e}")
                failed += 1
                continue
            if m is None:
                print(f"  haze case {case[0]} skipped: the encoder cannot produce this profile")
                continue
            cases[case[0]] = m
            print(f"  {case[0]:12s} bytes {m['bytes']:7d}  {describe(m)}")

    if not cases:
        print("  no haze case ran; results left untouched")
        sys.exit(1)
    store_block(args.results_json, {"span_s": list(SPAN_S), "band_hz": list(BAND_HZ),
                                    "onsets_s": list(ATTACK_ONSETS_S), "cases": cases})
    if failed:
        sys.exit(1)                     # the partial block is kept; the runner warns


if __name__ == "__main__":
    main()
