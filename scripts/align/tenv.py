import sys, numpy as np, soundfile as sf
from scipy.signal import correlate, butter, sosfiltfilt
r,fs=sf.read(sys.argv[1]); r=r.mean(1)
sos=butter(8,12000,'hp',fs=fs,output='sos'); N=256
def env(x): h=sosfiltfilt(sos,x); return 10*np.log10(np.array([np.mean(h[i:i+N]**2) for i in range(0,len(h)-N,N)])+1e-10)
er=env(r)
for p in sys.argv[2:]:
    d,_=sf.read(p); d=d.mean(1); c=correlate(d[:fs*4],r[:fs*4],'full','fft'); lag=np.argmax(c)-(fs*4-1); d=d[lag:] if lag>=0 else np.concatenate([np.zeros(-lag),d])
    ed=env(d[:len(r)]); n=min(len(er),len(ed)); e=ed[:n]-er[:n]; m=er[:n]>np.max(er)-50
    # attacks: blocks where ref jumps > 8 dB
    att=np.where(np.diff(er[:n])>8)[0]+1
    pre=[e[a-2:a].mean() for a in att if a>2]; post=[e[a:a+3].mean() for a in att if a+3<n]
    print(p,'lag',lag,'mean err %.2f dB, abs %.2f, pre-attack %.2f, attack %.2f, n_att %d'%(e[m].mean(),np.abs(e[m]).mean(),np.mean(pre),np.mean(post),len(att)))
