#!/usr/bin/env python3
"""runc.py FAACBIN OUT.csv --rates 40,48 --extra "--object-type he-aac-v1" --arms "A=ENV=1 ENV2=3" "B=..." [--clips glob] [--dec ff|fdk|both]"""
import argparse, subprocess, os, glob, tempfile, csv, statistics as st, concurrent.futures as cf, sys, shlex
sys.path.insert(0,'/Users/nschimme/gitprojects/faac-benchmark/scripts'); sys.path.insert(0,'/Users/nschimme/gitprojects/faac-benchmark')
FB='/Users/nschimme/gitprojects/faac-benchmark'
ap=argparse.ArgumentParser(); ap.add_argument('faac'); ap.add_argument('out'); ap.add_argument('--rates',default='48'); ap.add_argument('--extra',default='')
ap.add_argument('--arms',nargs='+',required=True); ap.add_argument('--clips',default=FB+'/data/external/audio/*.wav'); ap.add_argument('--dec',default='ff'); ap.add_argument('-j',type=int,default=8)
a=ap.parse_args()
def score(ref,deg):
    r=subprocess.run([FB+'/.venv/bin/python',os.path.dirname(os.path.abspath(__file__))+'/sc.py',ref,deg],capture_output=True,text=True)
    try: return float(r.stdout.strip().splitlines()[-1])
    except Exception: return None
def job(arg):
    rate,arm,clip=arg; name,envs=arm.split('=',1); env=dict(os.environ)
    for kv in envs.split():
        k,v=kv.split('=',1); env[k]=v
    with tempfile.TemporaryDirectory() as t:
        o=t+'/o.m4a'; subprocess.run([a.faac,'-b',rate]+shlex.split(a.extra)+['-o',o,clip],env=env,capture_output=True)
        sz=os.path.getsize(o) if os.path.exists(o) else 0; res={'clip':os.path.basename(clip),'rate':rate,'arm':name,'size':sz}
        if a.dec in('ff','both'):
            subprocess.run(['ffmpeg','-v','error','-y','-i',o,t+'/ff.wav']); res['ff']=score(clip,t+'/ff.wav')
        if a.dec in('fdk','both'):
            subprocess.run([FB+'/bin/fdkdec',o,t+'/fdk.wav'],capture_output=True); res['fdk']=score(clip,t+'/fdk.wav')
        return res
jobs=[(r,arm,c) for r in a.rates.split(',') for arm in a.arms for c in sorted(glob.glob(a.clips))]
rows=list(cf.ThreadPoolExecutor(a.j).map(job,jobs))
keys=['clip','rate','arm','size']+[k for k in ('ff','fdk') if k in rows[0]]
with open(a.out,'w') as f: w=csv.DictWriter(f,keys); w.writeheader(); w.writerows(rows)
base=a.arms[0].split('=')[0]
for r in a.rates.split(','):
    for arm in a.arms:
        n=arm.split('=')[0]
        for dk in keys[4:]:
            b={x['clip']:x for x in rows if x['rate']==r and x['arm']==base}; c={x['clip']:x for x in rows if x['rate']==r and x['arm']==n}
            cl=[k for k in c if c[k][dk] is not None and b.get(k,{}).get(dk) is not None]
            d=[c[k][dk]-b[k][dk] for k in cl]; bs=sum(c[k]['size'] for k in cl)/max(1,sum(b[k]['size'] for k in cl))-1
            print(f"{r}k {n:10s} {dk}: mean {st.mean(c[k][dk] for k in cl):.4f} d {st.mean(d):+.4f} W/L {sum(x>0.02 for x in d)}/{sum(x<-0.02 for x in d)} worst {min(d):+.3f} best {max(d):+.3f} bytes {bs:+.2%} n={len(cl)}")
