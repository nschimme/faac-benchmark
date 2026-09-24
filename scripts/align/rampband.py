import sys, numpy as np, soundfile as sf
from scipy.signal import correlate, butter, sosfiltfilt
r,fs=sf.read('ramp.wav'); r=r[:,0]
for p in sys.argv[1:]:
    d,_=sf.read(p); d=d[:,0]; c=correlate(d[:fs*4],r[:fs*4],'full','fft'); lag=np.argmax(c)-(fs*4-1); d=d[lag:lag+len(r)]
    out=[]
    for lo,hi in [(12000,13500),(13500,15000),(15000,16500),(16500,18000)]:
        sos=butter(8,[lo,hi],'bp',fs=fs,output='sos'); a=sosfiltfilt(sos,r)[fs*2:]; b=sosfiltfilt(sos,d)[fs*2:]
        out.append('%.1f'%(10*np.log10(np.sum(b**2)/np.sum(a**2))))
    print(p,'level vs ref by band 12-13.5/13.5-15/15-16.5/16.5-18 kHz:',' '.join(out))
