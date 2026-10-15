#!/usr/bin/env python3
"""libfaad3 DRC vs fdk DRC (black-box oracle) on ADTS streams: level and sample SNR."""
import subprocess, sys, itertools, tempfile
from pathlib import Path
import numpy as np, soundfile as sf

faad3, fdkdrc = sys.argv[1], sys.argv[2]
cfgs = [(127,127,96),(127,127,0),(64,64,96),(0,0,96),(127,0,0),(0,127,0)]
for aac in sys.argv[3:]:
    for cut, boost, tgt in cfgs:
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/"a.wav", Path(td)/"b.wav"
            subprocess.run([faad3, "-b", "16", "--drc-cut", str(cut), "--drc-boost", str(boost), "--drc-target", str(tgt), "-o", str(a), aac], capture_output=True)
            subprocess.run([fdkdrc, aac, str(b), str(cut), str(boost), str(tgt)], capture_output=True)
            x, _ = sf.read(a, dtype="float64"); y, _ = sf.read(b, dtype="float64")
            n = min(len(x), len(y), 48000*20)
            # align by xcorr of the first 20 s (decoder start offsets differ)
            from scipy.signal import fftconvolve
            c = fftconvolve(y[:n].mean(1), x[:n].mean(1)[::-1], mode="full"); lag = int(np.argmax(c)) - (n-1)
            if lag > 0: y = y[lag:]
            else: x = x[-lag:]
            m = min(len(x), len(y)) - 4096; x, y = x[2048:m], y[2048:m]
            rms = lambda s: 20*np.log10(np.sqrt((s**2).mean())+1e-12)
            err = x - y
            print(f"{Path(aac).name:32s} cut={cut:3d} boost={boost:3d} tgt={tgt:3d} lag={lag:5d} dLevel(f3-fdk)={rms(x)-rms(y):+6.2f} dB  SNR={rms(y)-rms(err):6.1f} dB")
