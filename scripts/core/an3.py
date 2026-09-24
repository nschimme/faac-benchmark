"""Side-info estimate for all frames (long and short): section + scalefactor bits per channel."""
import sys, numpy as np
BL=[18,18,18,18,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,18,19,18,17,17,16,17,16,16,16,16,15,15,14,14,14,14,14,14,13,13,12,12,12,11,12,11,10,10,10,9,9,8,8,8,7,6,6,5,4,3,1,4,4,5,6,6,7,7,8,8,9,9,10,10,10,11,11,11,11,12,12,13,13,13,14,14,16,15,16,15,18,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19]
def load(fn):
    R=[]
    for l in open(fn):
        if not l.startswith('C '): continue
        h,b=l.split('|'); h=h.split()
        fr,ch,bits,ws,msfb,ng,gg=map(int,h[1:8])
        g=[[tuple(map(int,t.split(':'))) for t in x.split()] for x in b.split('/') if x.strip()]
        R.append(dict(fr=fr,ch=ch,bits=bits,ws=ws,g=g,gg=gg))
    return R
def side(r):
    short=r['ws']==2; lb,esc=(3,7) if short else (5,31)
    sec=0; sfb=0; last={'s':r['gg'],'i':0}; pfirst=True; n=dict(s=0,p=0,i=0,z=0,sw=0)
    for v in r['g']:
        cb=[x[0] for x in v]; i=0
        while i<len(cb):
            j=i
            while j<len(cb) and cb[j]==cb[i]: j+=1
            sec+=4+lb*(1+(j-i)//esc); i=j
        n['sw']+=sum(cb[k]!=cb[k-1] for k in range(1,len(cb)))
        for c,sf,nz,ms in v:
            k='s' if 1<=c<=11 else ('p' if c==13 else ('i' if c>=14 else None))
            n[k or 'z']+=1
            if not k: continue
            if k=='p' and pfirst: sfb+=9; pfirst=False; last['p']=sf; continue
            d=max(-60,min(60,sf-last[k])); sfb+=BL[d+60]; last[k]=sf
    return sec,sfb,n
for fn in sys.argv[1:]:
    R=load(fn); frames={}
    for r in R: frames.setdefault(r['fr'],{})[r['ch']]=r
    F=[f for f in frames if 0 in frames[f] and 1 in frames[f]]
    tot=np.mean([frames[f][0]['bits'] for f in F])
    print(f"{fn}: frames {len(F)} CPE bits/frame {tot:.0f}")
    for kind,sel in (('long',lambda r:r['ws']!=2),('short',lambda r:r['ws']==2)):
        Fk=[f for f in F if sel(frames[f][0])]
        if not Fk: continue
        tb=np.mean([frames[f][0]['bits'] for f in Fk]); parts=[]; sidesum=0
        for ch in (0,1):
            a=[side(frames[f][ch]) for f in Fk]
            sec=np.mean([x[0] for x in a]); sf=np.mean([x[1] for x in a]); sidesum+=sec+sf
            nm={k:np.mean([x[2][k] for x in a]) for k in ('s','p','i','z','sw')}
            ng=np.mean([len(frames[f][ch]['g']) for f in Fk])
            parts.append(f"ch{ch}: sect {sec:.0f} sf {sf:.0f} | spec {nm['s']:.1f} pns {nm['p']:.1f} is {nm['i']:.1f} zero {nm['z']:.1f} switches {nm['sw']:.1f} groups {ng:.1f}")
        print(f"  {kind:5s} {len(Fk)/len(F)*100:4.0f}% frames, CPE bits {tb:.0f}, side ~{sidesum:.0f} ({sidesum/tb*100:.0f}%)")
        for p in parts: print("     "+p)
