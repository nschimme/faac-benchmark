import sys, numpy as np, soundfile as sf
from scipy.signal import correlate, butter, sosfiltfilt
r,fs=sf.read('ramp.wav'); r=r[:,0]; db=np.load('ramp_db.npy'); per=fs//2; rate=60/per
sos=butter(8,[13000,16000],'bp',fs=fs,output='sos'); hr=sosfiltfilt(sos,r)
for p in sys.argv[1:]:
    d,_=sf.read(p); d=d[:,0]; c=correlate(d[:fs*4],r[:fs*4],'full','fft'); lag=np.argmax(c)-(fs*4-1); d=d[lag:lag+len(r)]
    hd=sosfiltfilt(sos,d); N=256; k=len(hd)//N
    e=lambda h: 10*np.log10(np.array([np.mean(h[i*N:(i+1)*N]**2) for i in range(k)])+1e-20)
    diff=e(hd)-e(hr[:k*N]); mid=np.array([db[i*N+N//2] for i in range(k)])
    ph=np.array([((i*N+N//2)%(2*per))/per for i in range(k)])
    up=(ph>0.2)&(ph<0.9)&(mid>-45); dn=(ph>1.1)&(ph<1.8)&(mid>-45); dn&=np.arange(k)>fs*2//N; up&=np.arange(k)>fs*2//N
    bu=np.median(diff[up]); bd=np.median(diff[dn])
    print(p,'lag',lag,'bias up %.2f dB, down %.2f dB -> analysis early by %.0f samples'%(bu,bd,(bd-bu)/(2*rate)))
