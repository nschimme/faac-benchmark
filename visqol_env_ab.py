#!/usr/bin/env python3
"""Targeted ViSQOL (audio mode) paired A/B over encoder env configs.

Scores the CI's own metric on specific clips instead of the full gate, for
levers where zimtohrli disagrees with ViSQOL (e.g. block-switch promotion).
Encodes are deterministic; ViSQOL scoring is repeated --reps times and
averaged to tame backend nondeterminism.

Usage:
  .venv/bin/python visqol_env_ab.py --faac BIN --env-a "K=V[,K=V]" [--env-b "..."]
      --bitrates 12,16,24,32,48 [--reps 3] clip1.wav clip2.wav ...

Delta = A - B (B defaults to baseline env). Bitrates go straight to faac -b, as
they do in phase1_encode.py, so pass the CI scenario number verbatim:
48k_stereo_64k is -b 64.

Units, because they have been got wrong twice in both directions: -b is the
TOTAL rate. The frontend divides it by the channel count before the library
sees it (main.c), so -b 64 on stereo is 32 kbps/channel and measures ~67 kbps
total end to end. Scenario names are therefore accurate, not nominal. Anything
inside libfaac -- config->bitRate, ShortBlockTighten's anchors, CalcBandwidth --
is per channel and is half the number you passed here.

Do not halve the scenario number to "convert": that lands at -b 32, under the
SBR crossover, where the core frame count halves and PSY_TD_THRESH stops
mattering entirely -- a threshold sweep there returns byte-identical output at
every value, a clean and completely false "no difference".
"""
import argparse
import os
import statistics
import subprocess
import sys
import tempfile

from visqol import VisqolApi

_api = None

def api():
    global _api
    if _api is None:
        _api = VisqolApi()
        _api.create(mode="audio")
    return _api

def sh(cmd, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, env=env, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.decode()[-300:]}")

def to_48k_stereo(src, dst):
    sh(["ffmpeg", "-y", "-i", src, "-ac", "2", "-ar", "48000", dst])

def parse_env(spec):
    out = {}
    for kv in filter(None, (s.strip() for s in spec.split(","))):
        k, _, v = kv.partition("=")
        out[k] = v
    return out

def score(ref48, deg48, reps):
    vals = [float(api().measure(ref48, deg48).moslqo) for _ in range(reps)]
    return statistics.mean(vals)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--faac", required=True)
    p.add_argument("--env-a", required=True)
    p.add_argument("--env-b", default="")
    p.add_argument("--bitrates", default="12,16,24,32,48")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("clips", nargs="+")
    args = p.parse_args()

    env_a, env_b = parse_env(args.env_a), parse_env(args.env_b)
    bitrates = [int(b) for b in args.bitrates.split(",")]
    deltas = {b: [] for b in bitrates}

    with tempfile.TemporaryDirectory() as tmp:
        for clip in args.clips:
            name = os.path.basename(clip)
            ref48 = os.path.join(tmp, "ref.wav")
            to_48k_stereo(clip, ref48)
            print(f"{name}:", flush=True)
            for br in bitrates:
                mos = {}
                for tag, env in (("A", env_a), ("B", env_b)):
                    aac = os.path.join(tmp, f"{tag}.aac")
                    dec = os.path.join(tmp, f"{tag}.wav")
                    sh([args.faac, "-b", str(br), "-o", aac, clip], env)
                    to_48k_stereo(aac, dec)
                    mos[tag] = score(ref48, dec, args.reps)
                d = mos["A"] - mos["B"]
                deltas[br].append(d)
                print(f"  {br}k/ch: A={mos['A']:.4f} B={mos['B']:.4f} "
                      f"dMOS={d:+.4f}", flush=True)

    print(f"\n== aggregate (A={args.env_a!r} minus B={args.env_b!r}) ==")
    for br in bitrates:
        v = deltas[br]
        wins = sum(1 for x in v if x > 0.005)
        losses = sum(1 for x in v if x < -0.005)
        print(f"  {br}k/ch: mean dMOS={statistics.mean(v):+.4f} "
              f"min={min(v):+.4f} max={max(v):+.4f} "
              f"wins={wins} losses={losses} n={len(v)}")

if __name__ == "__main__":
    sys.exit(main())
