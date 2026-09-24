import sys, numpy as np, soundfile as sf
from scipy.signal import butter, sosfiltfilt, correlate
r,fs=sf.read('bursts.wav'); r=r[:,0]; d,_=sf.read(sys.argv[1]); d=d[:,0]
# align on full band
c=correlate(d[:fs*3],r[:fs*3],'full','fft'); lag=np.argmax(c)-(fs*3-1); d=d[lag:]
sos=butter(8,13000,'hp',fs=fs,output='sos'); hr=sosfiltfilt(sos,r); hd=sosfiltfilt(sos,d[:len(r)])
pos=np.load('bursts_pos.npy'); L=int(0.012*fs); W=2048
cm=[]; pre=[]
for p in pos:
    a=hr[p-W:p+L+W]**2; b=hd[p-W:p+L+W]**2; n=np.arange(len(a))
    cm.append((b*n).sum()/b.sum()-(a*n).sum()/a.sum())
    pre.append(10*np.log10(b[:W-64].sum()/b.sum()+1e-9))
print(sys.argv[1],'lag',lag,'HF centroid shift %.0f samples (sd %.0f), pre-burst energy share %.1f dB'%(np.median(cm),np.std(cm),np.median(pre)))
