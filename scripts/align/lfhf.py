import sys, numpy as np, soundfile as sf
from scipy.signal import butter, sosfiltfilt
# Per-band burst timing vs the reference, WITHOUT re-aligning (decoders trim priming): lf/hf centroid shift in samples.
r,fs=sf.read('bursts.wav'); r=r[:,0]; pos=np.load('bursts_pos.npy'); L=int(0.012*fs); W=2048
bands={'lf':('bp',[300,3000]),'mid':('bp',[5000,9000]),'hf':('hp',13000)}
for p in sys.argv[1:]:
    d,_=sf.read(p); d=d[:,0] if d.ndim>1 else d; d=np.pad(d,(0,max(0,len(r)-len(d))))[:len(r)]
    out=[]
    for nm,(t,f) in bands.items():
        sos=butter(6,f,t,fs=fs,output='sos'); hr=sosfiltfilt(sos,r); hd=sosfiltfilt(sos,d)
        cm=[];pre=[]
        for q in pos:
            a=hr[q-W:q+L+W]**2; b=hd[q-W:q+L+W]**2; n=np.arange(len(a))
            cm.append((b*n).sum()/b.sum()-(a*n).sum()/a.sum()); pre.append(10*np.log10(b[:W-64].sum()/b.sum()+1e-9))
        out.append('%s %+5.0f (pre %.1f dB)'%(nm,np.median(cm),np.median(pre)))
    print(p.split('/')[-1],' | '.join(out))
