"""
 * FAAC Benchmark Suite - Per-Band Distortion Diagnostic
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

Locates *where* in the spectrum a codec loses quality: it decodes the encoded
file and reports the RMS log-spectral error vs the reference in fixed frequency
bands. This is the analysis that pinned the HE-AAC percussive loss to the core's
8-12 kHz band (vs the SBR band, which was fine). Reuses utils.wav_conv for decode.
"""

import os
import sys
import argparse
import tempfile
import wave

import numpy as np

from utils import wav_conv

# Band edges in Hz. The 8-12k / 12-18.4k split is deliberate: at HE-AAC's
# half-rate core, 8-12k is the core's top octave and 12-18.4k is the SBR band.
DEFAULT_BANDS = [(0, 4000), (4000, 8000), (8000, 12000), (12000, 18400), (18400, 24000)]


def _read(path):
    w = wave.open(path, "rb")
    sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
    data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
    w.close()
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def band_errors(ref_wav, deg_path, mode="audio", bands=DEFAULT_BANDS, N=2048):
    """Return {(\"lo-hi\"): rms_log_spectral_error_dB} for deg vs ref.

    deg_path may be an .aac (decoded via ffmpeg) or a .wav. Both signals are
    resampled to a common rate and downmixed to mono for the comparison.
    """
    rate = 48000 if mode == "audio" else 16000
    with tempfile.TemporaryDirectory() as td:
        rw = os.path.join(td, "ref.wav")
        dw = os.path.join(td, "deg.wav")
        if not wav_conv(ref_wav, rw, rate, 1):
            return None
        if not wav_conv(deg_path, dw, rate, 1):
            return None
        ref, sr = _read(rw)
        deg, _ = _read(dw)

    n = min(len(ref), len(deg))
    if n < N:
        return None
    ref, deg = ref[:n], deg[:n]
    win = np.hanning(N)
    R = np.abs(np.fft.rfft(np.lib.stride_tricks.sliding_window_view(ref, N)[::N // 2] * win, axis=1))
    D = np.abs(np.fft.rfft(np.lib.stride_tricks.sliding_window_view(deg, N)[::N // 2] * win, axis=1))
    f = np.fft.rfftfreq(N, 1.0 / sr)
    out = {}
    for lo, hi in bands:
        m = (f >= lo) & (f < hi)
        if not m.any():
            continue
        e = 20.0 * np.log10((D[:, m] + 1e-6) / (R[:, m] + 1e-6))
        out[f"{lo // 1000}-{hi // 1000}k"] = float(np.sqrt((e ** 2).mean()))
    return out


def main():
    ap = argparse.ArgumentParser(description="Per-band log-spectral distortion of an encode vs its reference.")
    ap.add_argument("reference", help="Reference WAV")
    ap.add_argument("degraded", help="Encoded AAC (or decoded WAV)")
    ap.add_argument("--mode", choices=["audio", "speech"], default="audio")
    args = ap.parse_args()
    res = band_errors(args.reference, args.degraded, args.mode)
    if res is None:
        print("Error: could not compute band errors (decode failed or clip too short).")
        sys.exit(1)
    print(f"{'band':14} {'err(dB RMS)':>12}")
    for k, v in res.items():
        print(f"{k:14} {v:12.1f}")


if __name__ == "__main__":
    main()
