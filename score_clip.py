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
from utils import wav_conv, safe_run
from phase2_mos import get_process_visqol_python, MODEL_DIR, SPEECH_MODEL_NAME

def score_clip(ref, deg, mode="audio"):
    if not os.path.exists(ref):
        print(f"Error: Reference file {ref} not found.")
        return None
    if not os.path.exists(deg):
        print(f"Error: Degraded file {deg} not found.")
        return None

    rate = 48000 if mode == "audio" else 16000
    channels = 2 if mode == "audio" else 1

    with tempfile.TemporaryDirectory() as tmpdir:
        ref_wav = os.path.join(tmpdir, "ref.wav")
        deg_wav = os.path.join(tmpdir, "deg.wav")

        print(f"Converting to {rate}Hz {channels}ch WAV...")
        if not wav_conv(ref, ref_wav, rate, channels):
            return None
        if not wav_conv(deg, deg_wav, rate, channels):
            return None

        print(f"Computing MOS (mode: {mode})...")
        try:
            api = get_process_visqol_python(mode, MODEL_DIR)
            if api:
                result = api.measure(ref_wav, deg_wav)
                return float(result.moslqo)
            else:
                # Fallback to binary if python API fails or not available
                visqol_bin = os.environ.get("VISQOL_BIN") or "visqol"
                cmd = [visqol_bin, "--reference_file", ref_wav, "--degraded_file", deg_wav]
                if mode == "speech":
                    cmd.append("--use_speech_mode")
                    if MODEL_DIR:
                        cmd.extend(["--similarity_to_quality_model", os.path.join(MODEL_DIR, SPEECH_MODEL_NAME)])

                res = safe_run(cmd)
                for line in res.stdout.splitlines():
                    if "MOS-LQO:" in line:
                        return float(line.split()[-1])
        except Exception as e:
            print(f"Error computing MOS: {e}")
            return None
    return None

def main():
    parser = argparse.ArgumentParser(description="One-shot perceptual quality (MOS) scorer.")
    parser.add_argument("reference", help="Original WAV file")
    parser.add_argument("degraded", help="Encoded AAC file (or decoded WAV)")
    parser.add_argument("--mode", choices=["audio", "speech"], default="audio", help="ViSQOL mode")
    args = parser.parse_args()

    mos = score_clip(args.reference, args.degraded, args.mode)
    if mos is not None:
        print(f"MOS: {mos:.4f}")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
