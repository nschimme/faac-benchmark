"""
 * FAAC Benchmark Suite - One-Shot Clip Scorer
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import sys
import argparse
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import wav_conv
from phase2_mos import score_wav_pair

def score_clip(ref, deg, mode="audio"):
    if not os.path.exists(ref):
        print(f"Error: Reference file {ref} not found.")
        return None, None
    if not os.path.exists(deg):
        print(f"Error: Degraded file {deg} not found.")
        return None, None

    rate = 48000 if mode == "audio" else 16000
    channels = 2 if mode == "audio" else 1

    with tempfile.TemporaryDirectory() as tmpdir:
        ref_wav = os.path.join(tmpdir, "ref.wav")
        deg_wav = os.path.join(tmpdir, "deg.wav")

        print(f"Converting to {rate}Hz {channels}ch WAV...")
        if not wav_conv(ref, ref_wav, rate, channels):
            return None, None
        if not wav_conv(deg, deg_wav, rate, channels):
            return None, None

        print(f"Computing MOS (mode: {mode})...")
        return score_wav_pair(ref_wav, deg_wav, mode_str=mode, sample_rate=rate)

def main():
    parser = argparse.ArgumentParser(description="One-shot perceptual quality (MOS) scorer.")
    parser.add_argument("reference", help="Original WAV file")
    parser.add_argument("degraded", help="Encoded AAC file (or decoded WAV)")
    parser.add_argument("--mode", choices=["audio", "speech"], default="audio", help="Scoring mode")
    args = parser.parse_args()

    mos, backend_used = score_clip(args.reference, args.degraded, args.mode)
    if mos is not None:
        print(f"MOS: {mos:.4f} (backend: {backend_used})")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
