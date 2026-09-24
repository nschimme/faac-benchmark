"""Stereo coherence error of FAAC/fdk/Apple HE 48k (ffmpeg decode, xcorr aligned), 49 clips.
Requires FAAC_BIN=/path/to/faac (the encoder binary to score alongside fdkaac/afconvert)."""
import sys, os, glob, subprocess, tempfile, concurrent.futures as cf
FB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FB); sys.path.insert(0, FB + '/scripts')
import numpy as np, soundfile as sf, scipy.signal as ss
from phase3_stereo import coherence_vectorized, FRAME
from bandswap import align_to_ref
HP = ss.butter(4, 1500, 'hp', fs=48000, output='sos')
F = os.environ.get('FAAC_BIN')
if not F:
    sys.exit('error: set FAAC_BIN to a faac binary path')
ENC = {'faac': lambda i, o: [F, '-b', '48', '--object-type', 'he-aac-v1', '-o', o, i],
       'fdk': lambda i, o: ['fdkaac', '-p', '5', '-b', '48000', '-o', o, i],
       'apple': lambda i, o: ['afconvert', '-f', 'm4af', '-d', 'aach', '-b', '48000', i, o]}

def icerr(R, D, hp):
    if hp: R = ss.sosfilt(HP, R, axis=0); D = ss.sosfilt(HP, D, axis=0)
    rc = coherence_vectorized(R[:, 0], R[:, 1], FRAME); dc = coherence_vectorized(D[:, 0], D[:, 1], FRAME)
    e = np.sum(R**2, 1)[:len(rc) * FRAME].reshape(len(rc), FRAME).sum(1)
    k = e > 1e-3 * e.max()
    return float(np.mean(np.abs(rc - dc)[k]))

def job(arg):
    enc, clip = arg
    with tempfile.TemporaryDirectory() as t:
        o = t + '/o.m4a'; subprocess.run(ENC[enc](clip, o), capture_output=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', o, '-ar', '48000', t + '/d.wav'])
        R, _ = sf.read(clip); D, _ = sf.read(t + '/d.wav')
        if R.ndim < 2 or np.array_equal(R[:, 0], R[:, 1]): return enc, clip, None, None
        D, _ = align_to_ref(R, D)
        return enc, clip, icerr(R, D, False), icerr(R, D, True)

clips = sorted(glob.glob(FB + '/data/external/audio/*.wav'))
res = list(cf.ThreadPoolExecutor(8).map(job, [(e, c) for e in ENC for c in clips]))
for e in ENC:
    v = [(a, b) for x, c, a, b in res if x == e and a is not None]
    print(f"{e:6s} ic {np.mean([a for a, b in v]):.4f}  ic_hi {np.mean([b for a, b in v]):.4f}  n={len(v)}")
