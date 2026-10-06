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

 This phase encodes a small deterministic synthetic clip (generated here, no
 download) with fixed options and measures what the window decisions and the
 decoded spectrum do, with no perceptual model in the loop:

   short_frac  share of core frames in the span that use short windows
   kbd_frac    share of core frames whose ics_info window_shape is KBD
   haze_db     power of the decoded span between 1.2 and 19 kHz minus the
               source's, in dB (0 = no excess; the clip's own floor is the
               24-bit rounding of the source)

 Two controls pin the other directions. The treble control (the same bass plus
 quiet broadband treble) must see no change in what gates on bass dominance:
 KBD must not start firing and short windows must not appear. The attack
 control (kicks over silence) is the reverse risk: a rule that lets bass-heavy
 frames go long must not take real attacks with it, so its share of short
 windows must not fall.

 Like Phase 3 this is a regression guard for a property the other phases are
 blind to, not a perceptual ground truth. The gate (compare_results.check_haze)
 compares the candidate with the baseline run; it asserts no absolute level.
"""

import os
import sys
import json
import wave
import argparse
import subprocess
import tempfile

import numpy as np

from codec_bench.encoders import get_encoder_instance
from utils import get_ffmpeg_path

SR = 48000
LEAD_S = 1.0                    # digital silence before the bass
BASS_S = 6.0
SPAN_S = (2.5, 5.5)             # analysed span: inside the bass, past the onset
ATTACK_SPAN_S = (0.5, 6.0)      # the whole attack clip
BAND_HZ = (1200.0, 19000.0)     # the haze band
BASS_HZ = 58.0                  # period (827 samples) is longer than the 256-sample
                                # block-switching window, which the HE core's
                                # first-difference test trips on
TREBLE_FLOOR_DB = -50.0         # control clip: broadband treble, dB re full scale
TREBLE_CUTOFF_HZ = 1500.0

# (name, role, clip, profile, rate_control, value). "vbr" values are -q, "abr" -b (total kbps).
# Roles: "bass" must not get hazier or shorter-windowed; "control" must not change;
# "attack" must keep its short windows.
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


def adts_window_info(data):
    """(window_sequence, window_shape) of the first channel of every ADTS frame.

    Right after the element id and tag, a channel pair carries common_window;
    ics_info follows at once only when it is 1 (otherwise global_gain comes
    first), and a single-channel element always has global_gain first.
    """
    out = []
    i = 0
    while i + 7 <= len(data):
        if data[i] != 0xFF or (data[i + 1] & 0xF0) != 0xF0:
            break
        length = ((data[i + 3] & 3) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        header = 7 if data[i + 1] & 1 else 9
        payload = data[i + header:i + length]
        i += length
        if not payload:
            continue

        def bit(p):
            return (payload[p >> 3] >> (7 - (p & 7))) & 1

        element = payload[0] >> 5
        if element not in (0, 1):
            continue
        pos = 7
        if element == 1:
            common = bit(pos)
            pos += 1
            if not common:
                pos += 8
        else:
            pos += 8
        out.append(((bit(pos + 1) << 1) | bit(pos + 2), bit(pos + 3)))
    return out


def band_power_db(x, lo, hi, n=2048):
    """Mean Hann-windowed power of x between lo and hi Hz, in dB."""
    w = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    frames = 0
    for s in range(0, len(x) - n, n // 2):
        acc += np.abs(np.fft.rfft(x[s:s + n] * w)) ** 2
        frames += 1
    f = np.fft.rfftfreq(n, 1.0 / SR)
    power = acc[(f >= lo) & (f < hi)].sum() / max(frames, 1) / (w ** 2).sum()
    return 10 * np.log10(power + 1e-30)


def haze_db(source, decoded):
    """Decoded minus source power in the haze band over the span, left channel."""
    a, b = (int(s * SR) for s in SPAN_S)
    return float(band_power_db(decoded[a:b], *BAND_HZ) - band_power_db(source[a:b], *BAND_HZ))


def span_fractions(info, he, span=SPAN_S):
    """Share of short and of KBD frames in the span. Core frames are 1024 samples
    at the output rate for LC and at half of it for HE."""
    fps = (SR / 2 if he else SR) / 1024.0
    frames = info[int(span[0] * fps):int(span[1] * fps)]
    if not frames:
        return None, None
    return (sum(1 for ws, _ in frames if ws == 2) / len(frames),
            sum(1 for _, sh in frames if sh == 1) / len(frames))


def decode_left(path):
    cmd = [get_ffmpeg_path(), "-nostdin", "-v", "error", "-i", path,
           "-f", "f64le", "-ac", "2", "-ar", str(SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<f8").reshape(-1, 2)[:, 0]


def run_case(encoder_cache, tmp, clips, case, encoder_bin, encoder_lib):
    name, role, clip, profile, rc, value = case
    if profile not in encoder_cache:
        encoder_cache[profile] = get_encoder_instance(
            "faac", binary_path=encoder_bin, lib_override=encoder_lib, profile=profile)
    enc = encoder_cache[profile]
    out = os.path.join(tmp, f"{name}.aac")
    cmd = enc.get_encode_cmd(clips[clip][0], out, value, 2, SR,
                             rate_control=rc, vbr_q=value if rc == "vbr" else None)
    cmd.insert(len(cmd) - 1, "-a")          # ADTS: the window syntax is read from it
    subprocess.run(cmd, env=enc.get_run_env() or None, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(out, "rb") as f:
        data = f.read()
    span = ATTACK_SPAN_S if role == "attack" else SPAN_S
    short, kbd = span_fractions(adts_window_info(data), profile == "he", span)
    haze = None if role == "attack" else haze_db(clips[clip][1][:, 0], decode_left(out))
    return {"role": role, "bytes": len(data), "short_frac": short, "kbd_frac": kbd, "haze_db": haze}


def main():
    parser = argparse.ArgumentParser(description="Phase 4: bass haze regression guard")
    parser.add_argument("results_json", help="Path to results JSON file (the block is added in place)")
    parser.add_argument("--encoder-bin", help="Path to the faac binary")
    parser.add_argument("--encoder-lib", help="Path to a libfaac override")
    args = parser.parse_args()

    clips = {}
    cases = {}
    with tempfile.TemporaryDirectory() as tmp:
        for key, x in (("bass", bass_clip()), ("control", bass_clip(True)), ("attack", attack_clip())):
            path = os.path.join(tmp, f"{key}.wav")
            write_wav24(path, x)
            clips[key] = (path, x)
        cache = {}
        for case in CASES:
            try:
                cases[case[0]] = run_case(cache, tmp, clips, case, args.encoder_bin, args.encoder_lib)
            except Exception as e:      # one case failing must not hide the others
                print(f"  haze case {case[0]} failed: {e}")
                continue
            m = cases[case[0]]
            haze = "" if m["haze_db"] is None else f"  haze {m['haze_db']:+.1f} dB"
            print(f"  {case[0]:12s} bytes {m['bytes']:7d}  short {m['short_frac']:.2f}  "
                  f"kbd {m['kbd_frac']:.2f}{haze}")

    if not cases:
        print("  no haze case ran; results left untouched")
        return
    with open(args.results_json, "r") as f:
        data = json.load(f)
    data["haze"] = {"span_s": list(SPAN_S), "band_hz": list(BAND_HZ), "cases": cases}
    with open(args.results_json, "w") as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
