#!/usr/bin/env python3
"""VBR consistency report: does FAAC's VBR follow content, and does its quality
stay even across a clip, compared with Apple and FDK-AAC at matched rate?

Two shapes are reported separately because they can move independently:
  rate shape      corr of FAAC's per-segment bits with each reference's bits
  quality shape   per-segment Zimtohrli MOS spread (sd, p10, min) and its
                  correlation with spectral flatness, FAAC vs reference

FAAC is re-encoded per reference at the -q that matches that reference's mean
rate on that clip (bisection), so quality shape is compared at equal bits. The
references run at their native settings (afconvert -s 3 vbrq, fdkaac -m).
The Apple-vs-FDK agreement is printed as the noise floor for rate shape.

Reads clips only; writes encodes to a temp dir. Needs afconvert (macOS), fdkaac,
ffmpeg/ffprobe, and the benchmark venv (numpy, scipy, soundfile, zimtohrli).
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from multiprocessing import Pool

import numpy as np
import scipy.signal
import soundfile as sf
import zimtohrli

SEG_S, HOP_S, EDGE_S, ZR, SR = 0.75, 0.375, 0.5, 48000, 44100
APPLE_Q = (70, 100)
FDK_M = (4, 5)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def packet_kbps(path):
    out = sh(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
              "packet=size", "-of", "csv=p=0", path]).stdout.split()
    return np.array([int(x.split(",")[0]) * 8 * SR / 1024 / 1000 for x in out if x.strip()])


def priming(path):
    t = sh(["ffprobe", "-v", "error", "-show_entries", "format_tags=iTunSMPB",
            "-of", "csv=p=0", path]).stdout.split()
    return int(t[1], 16)


def stream_rate(path):
    return int(sh(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                   "stream=sample_rate", "-of", "csv=p=0", path]).stdout.split()[0])


def mean_kbps(path, dur):
    """Audio payload rate: container overhead differs between encoders and is
    1-2% of a short clip, comparable to the effects measured here."""
    out = sh(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
              "packet=size", "-of", "csv=p=0", path]).stdout.split()
    return sum(int(x.split(",")[0]) for x in out if x.strip()) * 8 / dur / 1000


def encode_faac_abr(faac, opts, wav, out, kbps):
    if os.path.exists(out):
        os.remove(out)
    sh([faac, "-b", str(int(round(kbps)))] + opts + ["-o", out, wav])


def encode_faac(faac, opts, wav, out, q):
    if os.path.exists(out):
        os.remove(out)
    sh([faac, "-q", str(q)] + opts + ["-o", out, wav])


def match_faac(faac, opts, wav, out, target, dur):
    lo, hi = 5, 3000
    for _ in range(13):
        mid = (lo + hi) // 2
        encode_faac(faac, opts, wav, out, mid)
        if mean_kbps(out, dur) < target:
            lo = mid + 1
        else:
            hi = mid
    encode_faac(faac, opts, wav, out, hi)
    return hi


def decode(path, tmp):
    wav = os.path.join(tmp, "dec.wav")
    sh(["ffmpeg", "-v", "error", "-y", "-i", path, "-f", "wav", "-acodec", "pcm_f32le", wav])
    d, sr = sf.read(wav, dtype="float32", always_2d=True)
    assert sr == SR, "decoded rate %d" % sr
    return d


def to48(x):
    g = math.gcd(ZR, SR)
    return scipy.signal.resample_poly(x, ZR // g, SR // g, axis=0).astype(np.float32)


def seg_mos(z, ref48, dec48, starts, seg):
    out = []
    for s in starts:
        a, b = int(s * ZR / SR), int((s + seg) * ZR / SR)
        d = [z.distance(np.ascontiguousarray(ref48[a:b, c]), np.ascontiguousarray(dec48[a:b, c]))
             for c in range(2)]
        out.append(float(zimtohrli.mos_from_zimtohrli(math.sqrt(sum(v * v for v in d)))))
    return np.array(out)


def seg_bits(k, p, starts, seg):
    return np.array([float(np.mean(k[max(0, (s + p) // 1024):min(len(k), (s + seg + p) // 1024 + 1)]))
                     for s in starts])


def seg_flatness(mono, starts, seg):
    w = np.hanning(2048)
    out = []
    for s in starts:
        x = mono[s:s + seg]
        P = np.mean([np.abs(np.fft.rfft(x[i:i + 2048] * w)) ** 2
                     for i in range(0, len(x) - 2048, 1024)], axis=0) + 1e-12
        out.append(10 * np.log10(np.exp(np.mean(np.log(P[5:]))) / np.mean(P[5:])))
    return np.array(out)


def corr(a, b):
    if np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def stats(m):
    return dict(mean=float(m.mean()), sd=float(m.std()), p10=float(np.quantile(m, .1)), min=float(m.min()))


def pkt_cv(path):
    k = packet_kbps(path)[12:-4]
    return float(np.std(k) / np.mean(k))


def score(z, ref48, path, tmp, n, starts, seg, flat):
    m = seg_mos(z, ref48, to48(decode(path, tmp)[:n]), starts, seg)
    b = seg_bits(packet_kbps(path), priming(path), starts, seg)
    return m, b, dict(**stats(m), mos_flat=corr(m, flat), cv=pkt_cv(path))


def run_clip(args):
    try:
        return _run_clip(args)
    except Exception as e:  # one bad clip must not hide the rest
        return dict(clip=os.path.basename(args[0]), skip="error: %r" % (e,))


def _run_clip(args):
    clip, faac, opts, fixed_q = args
    name = os.path.basename(clip)
    info = sf.info(clip)
    if info.samplerate != SR or info.channels != 2:
        return dict(clip=name, skip="not 44.1k stereo")
    dur = info.frames / SR
    ref, _ = sf.read(clip, dtype="float32", always_2d=True)
    n = len(ref)
    seg, hop, edge = int(SEG_S * SR), int(HOP_S * SR), int(EDGE_S * SR)
    starts = list(range(edge, n - seg - edge, hop))
    if len(starts) < 8:
        return dict(clip=name, skip="too short")
    ref48 = to48(ref)
    z = zimtohrli.Pyohrli()
    flat = seg_flatness(ref.mean(axis=1), starts, seg)
    res = dict(clip=name, segs=len(starts), refs={})
    with tempfile.TemporaryDirectory() as tmp:
        encs = []
        for v in APPLE_Q:
            f = os.path.join(tmp, "ap%d.m4a" % v)
            sh(["afconvert", "-f", "m4af", "-d", "aac", "-s", "3", "-u", "vbrq", str(v), clip, f])
            encs.append(("apple_vbrq%d" % v, f))
        for m in FDK_M:
            f = os.path.join(tmp, "fdk%d.m4a" % m)
            sh(["fdkaac", "-m", str(m), "-o", f, clip])
            encs.append(("fdk_m%d" % m, f))
        for rname, f in encs:
            if not os.path.exists(f) or stream_rate(f) != SR:
                res["refs"][rname] = dict(skip="reference not at 44.1 kHz")
                continue
            kb = mean_kbps(f, dur)
            fo = os.path.join(tmp, "faac.m4a")
            q = match_faac(faac, opts, clip, fo, kb, dur)
            r_mos, r_bits, r_st = score(z, ref48, f, tmp, n, starts, seg, flat)
            f_mos, f_bits, f_st = score(z, ref48, fo, tmp, n, starts, seg, flat)
            ent = dict(ref_kbps=kb, faac_kbps=mean_kbps(fo, dur), faac_q=q,
                       bits_corr=corr(f_bits, r_bits), faac=f_st, ref=r_st, bits_ref=r_bits.tolist())
            # ABR control at the same rate: separates a VBR shortfall from a core-coder one
            fa = os.path.join(tmp, "faac_abr.m4a")
            encode_faac_abr(faac, opts, clip, fa, kb)
            if os.path.exists(fa):
                _, _, st = score(z, ref48, fa, tmp, n, starts, seg, flat)
                ent["faac_abr"] = dict(st, kbps=mean_kbps(fa, dur))
            ra = os.path.join(tmp, "ref_abr.m4a")
            if os.path.exists(ra):
                os.remove(ra)
            if rname.startswith("apple"):
                sh(["afconvert", "-f", "m4af", "-d", "aac", "-s", "1", "-b", str(int(kb * 1000)), clip, ra])
            else:
                sh(["fdkaac", "-b", str(int(round(kb))), "-o", ra, clip])
            if os.path.exists(ra) and stream_rate(ra) == SR:
                _, _, st = score(z, ref48, ra, tmp, n, starts, seg, flat)
                ent["ref_abr"] = dict(st, kbps=mean_kbps(ra, dur))
            res["refs"][rname] = ent
        # one setting for every clip, no rate matching: does a setting mean the same quality?
        ff = os.path.join(tmp, "faac_fixed.m4a")
        encode_faac(faac, opts, clip, ff, fixed_q)
        _, _, st = score(z, ref48, ff, tmp, n, starts, seg, flat)
        res["fixed"] = dict(faac=dict(st, kbps=mean_kbps(ff, dur)))
        for rname in ("apple_vbrq70", "fdk_m4"):
            r = res["refs"].get(rname)
            if r and "skip" not in r:
                res["fixed"][rname] = dict(r["ref"], kbps=r["ref_kbps"])
        # reference agreement: noise floor for rate shape, at both operating points
        res["ref_agree"] = {}
        for a_, f_ in (("apple_vbrq70", "fdk_m4"), ("apple_vbrq100", "fdk_m5")):
            ab = res["refs"].get(a_, {}).get("bits_ref")
            fb = res["refs"].get(f_, {}).get("bits_ref")
            res["ref_agree"][a_ + "~" + f_] = corr(np.array(ab), np.array(fb)) if ab and fb else float("nan")
        for r in res["refs"].values():
            r.pop("bits_ref", None)
    return res


def fisher(vals, ns):
    z = [(np.arctanh(max(min(v, .999), -.999)), n - 3) for v, n in zip(vals, ns) if not math.isnan(v) and n > 3]
    if not z:
        return float("nan")
    return float(np.tanh(sum(a * w for a, w in z) / sum(w for _, w in z)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--faac-bin", required=True)
    ap.add_argument("--faac-opts", default="", help="extra faac options, e.g. '--joint 1'")
    ap.add_argument("--clips", nargs="+", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--fixed-q", type=int, default=230, help="single -q for the cross-clip consistency test")
    ap.add_argument("--json", help="write raw results here")
    a = ap.parse_args()
    opts = a.faac_opts.split()
    with Pool(a.jobs) as p:
        results = p.map(run_clip, [(c, a.faac_bin, opts, a.fixed_q) for c in a.clips], chunksize=1)
    for r in results:
        if "skip" in r:
            print("skipped %s: %s" % (r["clip"], r["skip"]), file=sys.stderr)
    results = [r for r in results if "skip" not in r]
    print("%d clips scored\n" % len(results))
    for rname in ["apple_vbrq70", "apple_vbrq100", "fdk_m4", "fdk_m5"]:
        rows = [(r["clip"], r["segs"], r["refs"][rname]) for r in results
                if rname in r["refs"] and "skip" not in r["refs"][rname]]
        if not rows:
            continue
        print("== vs %s (%d clips)" % (rname, len(rows)))
        print("%-44s %5s %5s %4s | %6s | %-17s | %-17s | %-12s" %
              ("clip", "ref", "faac", "q", "bits~", "MOS sd faac/ref", "MOS p10 faac/ref", "M~flat f/r"))
        for c, n, r in rows:
            print("%-44s %5.0f %5.0f %4d | %+6.2f | %.3f / %.3f     | %.3f / %.3f     | %+.2f/%+.2f" %
                  (c[:44], r["ref_kbps"], r["faac_kbps"], r["faac_q"], r["bits_corr"],
                   r["faac"]["sd"], r["ref"]["sd"], r["faac"]["p10"], r["ref"]["p10"],
                   r["faac"]["mos_flat"], r["ref"]["mos_flat"]))
        d_sd = np.array([r["faac"]["sd"] - r["ref"]["sd"] for _, _, r in rows])
        d_p10 = np.array([r["faac"]["p10"] - r["ref"]["p10"] for _, _, r in rows])
        d_mean = np.array([r["faac"]["mean"] - r["ref"]["mean"] for _, _, r in rows])
        d_min = np.array([r["faac"]["min"] - r["ref"]["min"] for _, _, r in rows])
        pooled = fisher([r["bits_corr"] for _, _, r in rows], [n for _, n, _ in rows])
        print("  rate shape   pooled bits corr (Fisher)  %+.2f   (clips >0: %d/%d)" %
              (pooled, sum(1 for _, _, r in rows if r["bits_corr"] > 0), len(rows)))
        print("  quality      FAAC-ref mean MOS median %+.3f | p10 %+.3f (FAAC lower in %d/%d) | min %+.3f | sd %+.3f (FAAC wider in %d/%d)" %
              (np.median(d_mean), np.median(d_p10), int((d_p10 < 0).sum()), len(rows),
               np.median(d_min), np.median(d_sd), int((d_sd > 0).sum()), len(rows)))
        print()
    print("== VBR gain over ABR/CBR control at the same rate (median over clips; + means VBR is better)")
    print("%-14s %-22s %9s %9s %9s %9s %9s" % ("reference", "encoder", "d mean", "d p10", "d sd", "d min", "ABR/VBR kbps"))
    for rname in ["apple_vbrq70", "apple_vbrq100", "fdk_m4", "fdk_m5"]:
        rows = [r["refs"][rname] for r in results if rname in r["refs"] and "skip" not in r["refs"][rname]]
        for who, vk, ak in (("FAAC", "faac", "faac_abr"), ("reference", "ref", "ref_abr")):
            pr = [(x[vk], x[ak]) for x in rows if ak in x]
            if not pr:
                continue
            d = lambda key: float(np.median([v[key] - b[key] for v, b in pr]))
            ratio = float(np.median([x[ak]["kbps"] / (x["faac_kbps"] if vk == "faac" else x["ref_kbps"])
                                     for x in rows if ak in x]))
            print("%-14s %-22s %+9.4f %+9.4f %+9.4f %+9.4f %9.3f   (n=%d)" %
                  (rname, who + (" ABR" if who == "FAAC" else (" ABR" if rname.startswith("apple") else " CBR")),
                   d("mean"), d("p10"), -d("sd"), d("min"), ratio, len(pr)))
    print("   (d sd is reported as ABR sd minus VBR sd so that + again means VBR is more even)\n")
    print("== Pooled MOS~flatness (Fisher; FAAC positive and reference negative = FAAC under-serves tonal passages)")
    for rname in ["apple_vbrq70", "apple_vbrq100", "fdk_m4", "fdk_m5"]:
        rows = [(r["segs"], r["refs"][rname]) for r in results if rname in r["refs"] and "skip" not in r["refs"][rname]]
        if not rows:
            continue
        ns = [n for n, _ in rows]
        line = "%-14s FAAC VBR %+.2f" % (rname, fisher([x["faac"]["mos_flat"] for _, x in rows], ns))
        line += " | ref VBR %+.2f" % fisher([x["ref"]["mos_flat"] for _, x in rows], ns)
        fa = [(n, x["faac_abr"]["mos_flat"]) for n, x in rows if "faac_abr" in x]
        if fa:
            line += " | FAAC ABR %+.2f" % fisher([v for _, v in fa], [n for n, _ in fa])
        print(line)
    print()
    print("== Spread of packet bitrate within a clip (CV, median over clips)")
    for rname in ["apple_vbrq70", "fdk_m4", "apple_vbrq100", "fdk_m5"]:
        rows = [r["refs"][rname] for r in results if rname in r["refs"] and "skip" not in r["refs"][rname]]
        if rows:
            print("%-14s FAAC %.3f | reference %.3f" % (rname, np.median([x["faac"]["cv"] for x in rows]),
                                                       np.median([x["ref"]["cv"] for x in rows])))
    print()
    print("== One setting on every clip (no rate matching): consistency across clips")
    for label, key in (("FAAC -q fixed", "faac"), ("Apple vbrq70", "apple_vbrq70"), ("fdk -m 4", "fdk_m4")):
        rows = [r["fixed"][key] for r in results if key in r.get("fixed", {})]
        if rows:
            km = np.array([x["kbps"] for x in rows]); mm = np.array([x["mean"] for x in rows])
            p10 = np.array([x["p10"] for x in rows])
            print("%-14s kbps mean %5.0f sd %4.0f (CV %.2f) | clip mean MOS %.3f sd %.3f min %.3f | clip p10 MOS min %.3f  (n=%d)" %
                  (label, km.mean(), km.std(), km.std() / km.mean(), mm.mean(), mm.std(), mm.min(), p10.min(), len(rows)))
    print()
    for key in ("apple_vbrq70~fdk_m4", "apple_vbrq100~fdk_m5"):
        ag = [(r["segs"], r["ref_agree"][key]) for r in results if key in r.get("ref_agree", {})
              and not math.isnan(r["ref_agree"][key])]
        if ag:
            print("noise floor %s per-segment bits corr, pooled %+.2f (median %+.2f)" %
                  (key, fisher([v for _, v in ag], [n for n, _ in ag]), float(np.median([v for _, v in ag]))))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f)


if __name__ == "__main__":
    main()
