#!/usr/bin/env python3
"""core24.py OUT.csv --kbps 44 --arms NAME=CMD ...   CMD tokens: {in} {out} {bps} {kbps}; env via ENV:K=V prefix tokens.
Encodes 24 kHz stereo (resampled) clips as LC, decodes with ffmpeg, scores vs the 24 kHz reference (both upsampled to 48k)."""
import sys, os
FB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FB + '/scripts'); sys.path.insert(0, FB)
import numpy as np, soundfile as sf, scipy.signal as ss
from bandswap import align_to_ref
import argparse, subprocess, glob, csv, statistics as st, concurrent.futures as cf, shlex, hashlib
PY=FB+'/.venv/bin/python'; SC=FB+'/scripts/align/sc.py'
CACHE=os.path.dirname(os.path.abspath(__file__))+'/ref'; os.makedirs(CACHE,exist_ok=True)
ap=argparse.ArgumentParser(); ap.add_argument('out'); ap.add_argument('--kbps',default='44'); ap.add_argument('--arms',nargs='+',required=True)
ap.add_argument('--clips',default=FB+'/data/external/audio/*.wav'); ap.add_argument('-j',type=int,default=8); ap.add_argument('--base',default=None); ap.add_argument('--lp',type=int,default=0)
a=ap.parse_args()
def ff(*x): subprocess.run(['ffmpeg','-v','error','-y',*x],check=True)
def refs(clip):
    b=CACHE+'/'+os.path.basename(clip)[:-4]
    if not os.path.exists(b+'_48.wav'):
        ff('-i',clip,'-af','aresample=24000:filter_size=128:phase_shift=10:cutoff=0.97','-ac','2','-c:a','pcm_s16le',b+'_24.wav')
        ff('-i',b+'_24.wav','-af','aresample=48000:filter_size=128:phase_shift=10:cutoff=0.97','-c:a','pcm_s16le',b+'_48.wav')
    if a.lp:
        lp=f'{b}_48lp{a.lp}.wav'
        if not os.path.exists(lp):
            subprocess.run([PY,'-c',f'''
import soundfile as sf, scipy.signal as s, numpy as np
x,fs=sf.read("{b}_48.wav"); h=s.firwin(1023,{a.lp},fs=fs)
y=np.stack([s.oaconvolve(x[:,c],h,mode="same") for c in range(x.shape[1])],1)
sf.write("{lp}",y,fs,subtype="PCM_16")'''],check=True)
        return b+'_24.wav',lp
    return b+'_24.wav',b+'_48.wav'
def score(ref,deg):
    r=subprocess.run([PY,SC,ref,deg],capture_output=True,text=True)
    try: return float(r.stdout.strip().splitlines()[-1])
    except Exception: return None
def job(arg):
    kbps,arm,clip=arg; name,cmd=arm.split('=',1); r24,r48=refs(clip); orig=clip
    toks=shlex.split(cmd); env=dict(os.environ)
    while toks and toks[0].startswith('ENV:'):
        k,v=toks.pop(0)[4:].split('=',1); env[k]=v
    t=f'/tmp/c24_{os.getpid()}_{hashlib.md5((name+kbps+clip).encode()).hexdigest()}'; os.makedirs(t,exist_ok=True)
    o=t+'/o.m4a'
    if os.path.exists(o): os.remove(o)
    subprocess.run([x.format(**{'orig':orig,'in':r24,'out':o,'bps':str(int(kbps)*1000),'kbps':kbps}) for x in toks],env=env,capture_output=True)
    res={'clip':os.path.basename(clip),'kbps':kbps,'arm':name,'size':os.path.getsize(o) if os.path.exists(o) else 0,'mos':None,'lag':None}
    if res['size']:
        ff('-i',o,'-af','aresample=48000:filter_size=128:phase_shift=10:cutoff=0.97','-ac','2','-c:a','pcm_s16le',t+'/d.wav')
        R,_=sf.read(r48); D,_=sf.read(t+'/d.wav'); D,lag=align_to_ref(R,D); res['lag']=lag
        if a.lp:
            h=ss.firwin(1023,a.lp,fs=48000); D=np.stack([ss.oaconvolve(D[:,c],h,mode='same') for c in range(2)],1)
        sf.write(t+'/d.wav',np.clip(D,-1,1),48000,subtype='PCM_16')
        res['mos']=score(r48,t+'/d.wav')
    subprocess.run(['rm','-rf',t]); return res
clips=sorted(glob.glob(a.clips)); [refs(c) for c in clips]
jobs=[(k,arm,c) for k in a.kbps.split(',') for arm in a.arms for c in clips]
rows=list(cf.ThreadPoolExecutor(a.j).map(job,jobs))
with open(a.out,'w') as f: w=csv.DictWriter(f,['clip','kbps','arm','size','mos','lag']); w.writeheader(); w.writerows(rows)
names=[x.split('=')[0] for x in a.arms]; base=a.base or names[0]
for k in a.kbps.split(','):
    B={x['clip']:x for x in rows if x['kbps']==k and x['arm']==base}
    for n in names:
        C={x['clip']:x for x in rows if x['kbps']==k and x['arm']==n}
        cl=[c for c in C if C[c]['mos'] is not None and B.get(c,{}).get('mos') is not None]
        d=[C[c]['mos']-B[c]['mos'] for c in cl]; bs=sum(C[c]['size'] for c in cl)/max(1,sum(B[c]['size'] for c in cl))-1
        print(f"{k}k {n:12s} mean {st.mean(C[c]['mos'] for c in cl):.4f} d {st.mean(d):+.4f} W/L {sum(x>0.02 for x in d)}/{sum(x<-0.02 for x in d)} worst {min(d):+.3f} best {max(d):+.3f} bytes {bs:+.2%} n={len(cl)}")
