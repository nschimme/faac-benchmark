#!/usr/bin/env python3
"""runs.py FAACBIN OUT.csv --rates 48 --extra "..." --arms "A=ENV=1 ..." ...
runc.py + stereo image: ic = benchmark phase-3 coherence_error; ic_hi = same on >1.5 kHz content."""
import argparse, subprocess, os, glob, tempfile, csv, statistics as st, concurrent.futures as cf, sys, shlex
FB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FB); sys.path.insert(0, FB + '/scripts')
import numpy as np, soundfile as sf, scipy.signal as ss
from phase3_stereo import coherence_error, coherence_vectorized, FRAME
ap = argparse.ArgumentParser(); ap.add_argument('faac'); ap.add_argument('out'); ap.add_argument('--rates', default='48')
ap.add_argument('--extra', default=''); ap.add_argument('--arms', nargs='+', required=True)
ap.add_argument('--clips', default=FB + '/data/external/audio/*.wav'); ap.add_argument('-j', type=int, default=8); ap.add_argument('--fdk', action='store_true')
a = ap.parse_args()
HP = ss.butter(4, 1500, 'hp', fs=48000, output='sos')

def score(ref, deg):
    r = subprocess.run([FB + '/.venv/bin/python', FB + '/scripts/align/sc.py', ref, deg], capture_output=True, text=True)
    try: return float(r.stdout.strip().splitlines()[-1])
    except Exception: return None

def ic_hi(ref, deg):
    R, _ = sf.read(ref); D, _ = sf.read(deg)
    if R.ndim < 2 or np.array_equal(R[:, 0], R[:, 1]): return None
    m = min(len(R), len(D)); R = ss.sosfilt(HP, R[:m], axis=0); D = ss.sosfilt(HP, D[:m], axis=0)
    rc = coherence_vectorized(R[:, 0], R[:, 1], FRAME); dc = coherence_vectorized(D[:, 0], D[:, 1], FRAME)
    e = np.sum(R**2, 1)[:len(rc) * FRAME].reshape(len(rc), FRAME).sum(1)
    keep = e > 1e-3 * e.max()          # skip near-silent frames, where coherence is noise
    return float(np.mean(np.abs(rc - dc)[keep]))

def job(arg):
    rate, arm, clip = arg; name, envs = arm.split('=', 1); env = dict(os.environ)
    for kv in envs.split():
        k, v = kv.split('=', 1); env[k] = v
    with tempfile.TemporaryDirectory() as t:
        o = t + '/o.m4a'
        binary = env.pop('BIN', a.faac)
        subprocess.run([binary, '-b', rate] + shlex.split(a.extra) + ['-o', o, clip], env=env, capture_output=True)
        res = dict(clip=os.path.basename(clip), rate=rate, arm=name, size=os.path.getsize(o) if os.path.exists(o) else 0)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', o, t + '/ff.wav'])
        res['ff'] = score(clip, t + '/ff.wav')
        if a.fdk:
            subprocess.run([FB + '/bin/fdkdec', o, t + '/fdk.wav'], capture_output=True)
            res['fdk'] = score(clip, t + '/fdk.wav')
        try: res['ic'] = coherence_error(clip, t + '/ff.wav')
        except Exception: res['ic'] = None
        try: res['ichi'] = ic_hi(clip, t + '/ff.wav')
        except Exception: res['ichi'] = None
        return res

jobs = [(r, arm, c) for r in a.rates.split(',') for arm in a.arms for c in sorted(glob.glob(a.clips))]
rows = list(cf.ThreadPoolExecutor(a.j).map(job, jobs))
keys = ['clip', 'rate', 'arm', 'size', 'ff', 'ic', 'ichi'] + (['fdk'] if a.fdk else [])
with open(a.out, 'w') as f: w = csv.DictWriter(f, keys); w.writeheader(); w.writerows(rows)
base = a.arms[0].split('=')[0]
for r in a.rates.split(','):
    B = {x['clip']: x for x in rows if x['rate'] == r and x['arm'] == base}
    for arm in a.arms:
        n = arm.split('=')[0]; C = {x['clip']: x for x in rows if x['rate'] == r and x['arm'] == n}
        cl = [k for k in C if C[k]['ff'] is not None and B.get(k, {}).get('ff') is not None]
        d = [C[k]['ff'] - B[k]['ff'] for k in cl]; bs = sum(C[k]['size'] for k in cl) / max(1, sum(B[k]['size'] for k in cl)) - 1
        out = f"{r}k {n:8s} MOS {st.mean(C[k]['ff'] for k in cl):.4f} d {st.mean(d):+.4f} W/L {sum(x>0.02 for x in d)}/{sum(x<-0.02 for x in d)} worst {min(d):+.3f} bytes {bs:+.2%}"
        if a.fdk:
            cf2 = [k for k in cl if C[k].get('fdk') is not None and B[k].get('fdk') is not None]
            df = [C[k]['fdk'] - B[k]['fdk'] for k in cf2]
            out += f" | fdkdec {st.mean(C[k]['fdk'] for k in cf2):.3f} d {st.mean(df):+.3f}"
        for ik in ('ic', 'ichi'):
            ci = [k for k in cl if C[k][ik] is not None and B[k][ik] is not None and B[k][ik] > 0]
            if not ci: continue
            rel = sum(C[k][ik] for k in ci) / sum(B[k][ik] for k in ci) - 1
            wk = max(ci, key=lambda k: C[k][ik] / B[k][ik])
            out += f" | {ik} {rel:+.1%} worst x{C[wk][ik]/B[wk][ik]:.2f} ({wk[:14]})"
        print(out)
