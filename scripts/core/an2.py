import sys, numpy as np
BL=[18,18,18,18,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,18,19,18,17,17,16,17,16,16,16,16,15,15,14,14,14,14,14,14,13,13,12,12,12,11,12,11,10,10,10,9,9,8,8,8,7,6,6,5,4,3,1,4,4,5,6,6,7,7,8,8,9,9,10,10,10,11,11,11,11,12,12,13,13,13,14,14,16,15,16,15,18,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19,19]
def load(fn):
    R=[]
    for l in open(fn):
        if not l.startswith('C '): continue
        h,b=l.split('|'); h=h.split()
        fr,ch,bits,ws,msfb,ng,gg=map(int,h[1:8])
        g=[[tuple(map(int,t.split(':'))) for t in x.split()] for x in b.split('/') if x.strip()]
        R.append(dict(fr=fr,ch=ch,bits=bits,ws=ws,msfb=msfb,g=g,gg=gg))
    return R
def side(v,gg):
    cb=[x[0] for x in v]; sec=0; i=0
    while i<len(cb):
        j=i
        while j<len(cb) and cb[j]==cb[i]: j+=1
        sec+=4+5*(1+(j-i)//31); i=j
    sfb=0; last={'s':gg,'i':0}; pfirst=True
    for c,sf,nz,ms in v:
        k='s' if 1<=c<=11 else ('p' if c==13 else ('i' if c>=14 else None))
        if not k: continue
        if k=='p' and pfirst: sfb+=9; pfirst=False; last['p']=sf; continue
        d=max(-60,min(60,sf-last[k])); sfb+=BL[d+60]; last[k]=sf
    return sec,sfb
for fn in sys.argv[1:]:
    R=load(fn); fr0={r['fr']:r for r in R if r['ch']==0}; fr1={r['fr']:r for r in R if r['ch']==1}
    Lf=[f for f in fr0 if fr0[f]['ws']!=2 and f in fr1]
    out=[]
    for ch,FR in ((0,fr0),(1,fr1)):
        a=np.array([[*side(FR[f]['g'][0],FR[f]['gg']), sum(1<=x[0]<=11 for x in FR[f]['g'][0]), sum(x[0]==13 for x in FR[f]['g'][0]), sum(x[0]>=14 for x in FR[f]['g'][0]), sum(x[0]==0 for x in FR[f]['g'][0]), sum(x[2] for x in FR[f]['g'][0] if 1<=x[0]<=11)] for f in Lf])
        m=a.mean(0); out.append(f"ch{ch}: sect {m[0]:.0f}b sf {m[1]:.0f}b | spec {m[2]:.1f} pns {m[3]:.1f} is {m[4]:.1f} zero {m[5]:.1f} nnz {m[6]:.0f}")
    eb=np.mean([fr0[f]['bits'] for f in Lf]); short=np.mean([r['ws']==2 for r in fr0.values()])
    print(f"{fn}: long {len(Lf)} fr (short {short*100:.0f}%) CPE bits {eb:.0f}\n   "+"\n   ".join(out))
