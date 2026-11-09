#!/usr/bin/env python3
"""Find and cut multichannel (5.1 / 7.1) test sections from a media file.

One decode pass measures, per window, every channel's RMS level and how much
each surround/centre channel differs from the front channel(s) it would
otherwise copy. Windows are kept only where all channels carry real signal;
the best non-overlapping ones are cut to 16-bit WAV.

    scan_surround.py SOURCE OUTDIR [--window 20] [--count 3] [--track 0] [--cache DIR] [--dry-run]
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile

LAYOUTS = {
    6: ["FL", "FR", "FC", "LFE", "BL", "BR"],
    8: ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
}
# (name, expression): a channel minus the front channel it is compared with.
# A level close to the channel's own means it is not a copy of that front.
DIFFS = {
    6: [("BL-FL", "c4-c0"), ("BR-FR", "c5-c1"), ("FC-FLR", "c2-0.5*c0-0.5*c1")],
    8: [("BL-FL", "c4-c0"), ("BR-FR", "c5-c1"), ("SL-FL", "c6-c0"), ("SR-FR", "c7-c1"),
        ("FC-FLR", "c2-0.5*c0-0.5*c1")],
}
SILENT_DB = -45.0     # a non-LFE channel below this is treated as unused
LFE_MIN_DB = -55.0
FRONT_RANGE = (-50.0, -8.0)    # excludes near-silence and clipped, flattened masters
SPREAD = (0.9, 0.5, 0.1)       # front-level quantiles to sample: loud, typical, quiet
COPY_MARGIN_DB = -6.0  # difference more than 6 dB under the channel => mostly a copy


def probe(path, track):
    def ff(entries, scope):
        out = subprocess.run(
            ["ffprobe", "-v", "error"] + (["-select_streams", f"a:{track}"] if scope == "stream" else [])
            + ["-show_entries", f"{scope}={entries}", "-of", "default=nw=1", path],
            capture_output=True, text=True, check=True).stdout
        return dict(line.split("=", 1) for line in out.split())
    st = ff("channels,sample_rate", "stream")
    return int(st["channels"]), int(st["sample_rate"]), float(ff("duration", "format")["duration"])


def measure(path, track, n, rate, window):
    """Return {window_index: {name: rms_db}} from a single decode."""
    tmp = tempfile.mkdtemp()
    sinks = [("ch", None)] + [(name, expr) for name, expr in DIFFS[n]]
    parts = [f"[0:a:{track}]asplit={len(sinks)}" + "".join(f"[s{i}]" for i in range(len(sinks)))]
    files = {}
    samples = window * rate
    for i, (name, expr) in enumerate(sinks):
        f = os.path.join(tmp, f"{i}.txt")
        files[name] = f
        pan = "" if expr is None else f"pan=mono|c0={expr},"
        parts.append(f"[s{i}]{pan}asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
                     f"ametadata=mode=print:file={f}" + ("[o]" if i == 0 else ",anullsink"))
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-filter_complex", ";".join(parts),
           "-map", "[o]", "-f", "null", "-"]
    subprocess.run(cmd, check=True)
    stats = {}
    for name, f in files.items():
        idx = -1
        for line in open(f):
            if line.startswith("frame:"):
                idx += 1
                continue
            m = re.match(r"lavfi\.astats\.(\d+)\.RMS_level=(\S+)", line)
            if m and name == "ch":
                stats.setdefault(idx, {})[LAYOUTS[n][int(m.group(1)) - 1]] = to_db(m.group(2))
            m = re.match(r"lavfi\.astats\.Overall\.RMS_level=(\S+)", line)
            if m and name != "ch":
                stats.setdefault(idx, {})[name] = to_db(m.group(1))
    return stats


def to_db(v):
    return -120.0 if v in ("-inf", "nan") else float(v)


def score(w, n):
    """Return a score (higher is better) or None when the window is unsuitable."""
    names = [c for c in LAYOUTS[n] if c != "LFE"]
    if any(w.get(c, -120) < SILENT_DB for c in names):
        return None
    if w.get("LFE", -120) < LFE_MIN_DB:
        return None
    fl = max(w["FL"], w["FR"])
    if not FRONT_RANGE[0] <= fl <= FRONT_RANGE[1]:
        return None
    pairs = {"BL-FL": "BL", "BR-FR": "BR", "SL-FL": "SL", "SR-FR": "SR", "FC-FLR": "FC"}
    copy = min(w[d] - w[c] for d, c in pairs.items() if d in w and c in w)
    if copy < COPY_MARGIN_DB:
        return None
    return min(w[c] for c in names) + 0.5 * min(w["LFE"], -25.0)


def pick(stats, n, count, window, skip):
    """Choose windows spread over the front-level range, not just the loudest."""
    ok = sorted(((max(w["FL"], w["FR"]), i) for i, w in stats.items()
                 if i >= skip and score(w, n) is not None))
    if not ok:
        return []
    targets = sorted(SPREAD[:count], reverse=True) if count <= len(SPREAD) else [1 - k / (count - 1) for k in range(count)]
    chosen = []
    for q in targets:
        want = ok[min(len(ok) - 1, int(q * len(ok)))][0]
        for lvl, i in sorted(ok, key=lambda x: abs(x[0] - want)):
            if all(abs(i - j) * window >= 120 for _, j in chosen):
                chosen.append((lvl, i))
                break
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("outdir")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--cache", help="directory for cached per-window measurements")
    ap.add_argument("--track", type=int, default=0)
    ap.add_argument("--name", help="output file stem (default: source basename)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    n, rate, dur = probe(a.source, a.track)
    if n not in LAYOUTS:
        sys.exit(f"{n} channels: only 5.1 and 7.1 are supported")
    stats = None
    key = os.path.join(a.cache, re.sub(r"[^A-Za-z0-9]+", "_", a.source)[-80:] + f".{a.track}.{a.window}.json") if a.cache else None
    if key and os.path.exists(key):
        stats = {int(i): w for i, w in json.load(open(key)).items()}
    if stats is None:
        stats = measure(a.source, a.track, n, rate, a.window)
        if key:
            os.makedirs(a.cache, exist_ok=True)
            json.dump(stats, open(key, "w"))
    # Skip the first and last 5 minutes: studio logos, credits, music beds.
    skip = 300 // a.window
    last = int(dur // a.window) - skip
    stats = {i: w for i, w in stats.items() if i < last}
    chosen = pick(stats, n, a.count, a.window, skip)
    stem = a.name or re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(a.source))[0])[:40]
    os.makedirs(a.outdir, exist_ok=True)
    for k, (s, i) in enumerate(chosen, 1):
        t = i * a.window
        w = stats[i]
        print(f"{stem}_{k}.wav\t{t}s\tfront {s:.0f} dB\t" + " ".join(f"{c}{w[c]:.0f}" for c in LAYOUTS[n]))
        if not a.dry_run:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-ss", str(t), "-t", str(a.window), "-i", a.source,
                            "-map", f"0:a:{a.track}", "-af", "aresample=osf=s16:dither_method=triangular",
                            "-c:a", "pcm_s16le", os.path.join(a.outdir, f"{stem}_{k}.wav")], check=True)
    if not chosen:
        print(f"{stem}: no suitable window", file=sys.stderr)


if __name__ == "__main__":
    main()
