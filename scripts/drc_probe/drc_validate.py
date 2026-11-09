#!/usr/bin/env python3
"""Compare FAAC's encoder DRC against the FDK line-mode oracle.

The oracle is black-box: fdk-aac encodes the same PCM with its metadata DRC
profile, and drc_meta.py reads the dynamic_range_info gains from both streams.
No FDK decoder/encoder source assumptions are used.

examples:
  drc_validate.py static --faac ../../faac/build/frontend/faac --json static.json
  drc_validate.py steps  --faac ../../faac/build/frontend/faac --json steps.json
  drc_validate.py decode --faac ... --json decode.json
"""
import argparse
import json
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import drc_meta  # noqa: E402

PROFILES = ["off", "film", "film-light", "music", "music-light", "speech"]
PROFILE_ID = {name: i for i, name in enumerate(PROFILES)}


def write_wav(path, samples, rate=48000):
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    pcm = np.clip(samples, -1.0, 32767.0 / 32768.0)
    pcm = np.round(pcm * 32768.0).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setparams((pcm.shape[1], 2, rate, pcm.shape[0], "NONE", ""))
        out.writeframes(pcm.tobytes())


def tone(rate, seconds, dbfs, freq=997.0, channels=2):
    n = int(round(rate * seconds))
    t = np.arange(n, dtype=np.float64) / rate
    x = 10.0 ** (dbfs / 20.0) * np.sin(2.0 * np.pi * freq * t)
    return np.repeat(x[:, None], channels, axis=1)


def staircase(rate, levels, seconds_per_level, gap=0.0):
    parts = []
    for level in levels:
        parts.append(tone(rate, seconds_per_level, level))
        if gap:
            parts.append(np.zeros((int(rate * gap), 2)))
    return np.concatenate(parts)


def run(cmd):
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (" ".join(map(str, cmd)), result.returncode,
                                                   result.stderr.strip()[-2000:]))
    return result


def encode_faac(faac, wav, aac, profile, ref, bitrate=128000, extra=()):
    cmd = [faac, "--overwrite", "-a", "-b", "%dk" % (bitrate // 1000), "-o", aac, wav]
    if profile != "off":
        cmd[1:1] = ["--drc", profile, "--drc-ref-level", "%.2f" % ref]
    cmd.extend(extra)
    run(cmd)


def encode_fdk(drcenc, wav, aac, profile, ref, bitrate=128000, aot=2):
    run([drcenc, wav, aac, str(bitrate), str(PROFILE_ID[profile]),
         "none" if ref is None else "%.2f" % ref, str(aot)])


def gains(path, ref=None):
    rows = drc_meta.parse(path, ref)
    result = []
    for row in rows:
        drc = row["drc"]
        result.append(None if drc is None else drc["gains"][0]["db_025"])
    return result


def settled(values, frame_dt, skip_seconds=None, take_seconds=2.0):
    if skip_seconds is None:
        skip_seconds = 0.0
    if not values:
        return None
    start = int(round(skip_seconds / frame_dt))
    stop = int(round((skip_seconds + take_seconds) / frame_dt))
    window = [v for v in values[start:stop] if v is not None]
    return None if not window else float(np.median(window))


def frame_seconds(aot):
    return 2048.0 / 48000.0 if aot == 5 else 1024.0 / 48000.0


def static(args):
    rate = 48000
    levels = [-60, -50, -40, -30, -24, -20, -15, -10, -6]
    refs = [float(r) for r in args.refs]
    profiles = args.profiles
    rows = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for profile in profiles:
            for ref in refs:
                for level in levels:
                    wav = td / "tone.wav"
                    write_wav(wav, tone(rate, args.seconds, level))
                    f_aac = td / "faac.aac"
                    d_aac = td / "fdk.aac"
                    encode_faac(args.faac, wav, f_aac, profile, ref, args.bitrate)
                    encode_fdk(args.drcenc, wav, d_aac, profile, ref, args.bitrate)
                    fg = gains(f_aac, ref)
                    dg = gains(d_aac, ref)
                    f = settled(fg, 1024.0 / rate, args.seconds - args.settle, args.settle)
                    d = settled(dg, 1024.0 / rate, args.seconds - args.settle, args.settle)
                    if f is None or d is None:
                        rows.append({"profile": profile, "ref": ref, "level": level,
                                     "error": None, "note": "no DRC payload"})
                        continue
                    rows.append({"profile": profile, "ref": ref, "level": level,
                                 "faac_db": f, "fdk_db": d, "error": f - d})
                    print("%-11s ref=%6.2f level=%4d faac=%7.2f fdk=%7.2f err=%+6.2f" %
                          (profile, ref, level, f, d, f - d), flush=True)
    valid = [r for r in rows if r.get("error") is not None]
    if valid:
        err = np.array([r["error"] for r in valid])
        print("\nsteady error: mean %+0.3f dB, max |%0.3f| dB, over %d points" %
              (err.mean(), np.abs(err).max(), len(err)))
        print("over tolerance: %d/%d" % (int((np.abs(err) > 0.5).sum()), len(err)))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
    return 0 if not valid or np.abs(err).max() <= 0.5 else 1


def steps(args):
    rate = 48000
    levels = [args.quiet, args.loud, args.quiet, args.loud]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wav = td / "steps.wav"
        write_wav(wav, staircase(rate, levels, args.segment))
        rows = []
        for profile in args.profiles:
            for ref in [float(r) for r in args.refs]:
                f_aac = td / "faac.aac"
                d_aac = td / "fdk.aac"
                encode_faac(args.faac, wav, f_aac, profile, ref, args.bitrate)
                encode_fdk(args.drcenc, wav, d_aac, profile, ref, args.bitrate)
                fg = gains(f_aac, ref)
                dg = gains(d_aac, ref)
                n = min(len(fg), len(dg))
                for i in range(n):
                    if fg[i] is None or dg[i] is None:
                        continue
                    rows.append({"profile": profile, "ref": ref, "frame": i,
                                 "time": i * 1024.0 / rate, "faac_db": fg[i],
                                 "fdk_db": dg[i], "error": fg[i] - dg[i]})
                subset = [r for r in rows if r["profile"] == profile and r["ref"] == ref]
                if subset:
                    f = np.array([r["faac_db"] for r in subset])
                    d = np.array([r["fdk_db"] for r in subset])
                    t = np.array([r["time"] for r in subset])
                    best = None
                    for shift in range(-8, 9):
                        if shift >= 0:
                            a, b = f[shift:], d[:len(d) - shift]
                        else:
                            a, b = f[:len(f) + shift], d[-shift:]
                        err = np.abs(a - b)
                        if best is None or err.mean() < best[1]:
                            best = (shift, err.mean(), err.max())
                    mask = np.ones(len(t), dtype=bool)
                    for edge in range(0, int(t[-1]) + 1, int(args.segment)):
                        mask &= np.abs(t - edge) > 0.06
                    err = np.abs(f - d)[mask]
                    print("%-11s ref=%6.2f frames=%5d raw mean=%+0.3f max=%0.3f | "
                          "shift=%+d mean=%0.3f max=%0.3f over1dB=%d (edges excluded)" %
                          (profile, ref, len(subset), (f - d).mean(), np.abs(f - d).max(),
                           best[0], err.mean(), err.max(), int((err > 1.0).sum())), flush=True)
        if args.json:
            Path(args.json).write_text(json.dumps(rows, indent=1))
    return 0


def read_f32(path, channels):
    return np.fromfile(path, dtype=np.float32).reshape(-1, channels)


def read_wav(path):
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        data = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    return data.reshape(-1, channels).astype(np.float64) / 32768.0


def align(reference, samples, rate=48000):
    n = min(len(reference), len(samples), rate * 10)
    if n <= 0:
        return reference, samples
    a = reference[:n].mean(axis=1)
    b = samples[:n].mean(axis=1)
    corr = fftconvolve(b, a[::-1], mode=full)
    lag = int(np.argmax(corr)) - (n - 1)
    if lag > 0:
        samples = samples[lag:]
    elif lag < 0:
        reference = reference[-lag:]
    return reference, samples


def segment_rms(samples, rate, levels, segment):
    edges = np.arange(len(levels) + 1) * int(rate * segment)
    result = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        lo += int(0.5 * rate)
        hi -= int(0.5 * rate)
        block = samples[lo:hi]
        if len(block) == 0:
            result.append(float("nan"))
        else:
            result.append(20.0 * math.log10(float(np.sqrt(np.mean(block ** 2))) + 1e-12))
    return result


def decode(args):
    rate = 48000
    levels = [args.quiet, args.loud, args.quiet, args.loud]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wav = td / "steps.wav"
        write_wav(wav, staircase(rate, levels, args.segment))
        aac = td / "faac.aac"
        encode_faac(args.faac, wav, aac, args.profile, args.ref, args.bitrate,
                    (["--object-type", args.aot] if args.aot else ()))
        base = td / "base.wav"
        run([args.faad, "--overwrite", "-o", base, aac])
        rows = []
        base_pcm = read_wav(base)
        cases = [
            ("faad_full", [args.faad, "--overwrite", "--drc-cut", "127", "--drc-boost", "127",
                           "-o", td / "faad_full.wav", aac], td / "faad_full.wav"),
            ("fdk_full", [args.fdkdrc, aac, td / "fdk.wav", "127", "127", "0"], td / "fdk.wav"),
            ("apple_full", [args.atdrc, aac, td / "apple.f32", "2", "0"], td / "apple.f32"),
        ]
        for name, cmd, produced in cases:
            try:
                run(cmd)
            except Exception as exc:  # keep the other rows
                print("%s: %s" % (name, exc), file=sys.stderr)
                continue
            pcm = read_wav(produced) if produced.suffix == ".wav" else read_f32(produced, 2)
            n = min(len(pcm), len(base_pcm))
            pcm = pcm[:n]
            ref = base_pcm[:n]
            base_levels = segment_rms(ref, rate, levels, args.segment)
            got_levels = segment_rms(pcm, rate, levels, args.segment)
            row = {"decoder": name, "base": base_levels,
                   "drc": got_levels,
                   "delta": [g - b for g, b in zip(got_levels, base_levels)]}
            rows.append(row)
            print(name, "delta", ["%+.2f" % d for d in row["delta"]], flush=True)
        if args.json:
            Path(args.json).write_text(json.dumps(rows, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["static", "steps", "decode"])
    ap.add_argument("--faac", default=str(Path(__file__).resolve().parents[3] / "faac" / "build" / "frontend" / "faac"))
    ap.add_argument("--drcenc", default=str(Path(__file__).resolve().parents[2] / "bin" / "drcenc"))
    ap.add_argument("--fdkdrc", default=str(Path(__file__).resolve().parents[2] / "bin" / "fdkdrc"))
    ap.add_argument("--atdrc", default=str(Path(__file__).resolve().parents[2] / "bin" / "atdrc"))
    ap.add_argument("--faad", default=str(Path(__file__).resolve().parents[3] / "faac" / "build" / "frontend" / "faad"))
    ap.add_argument("--profiles", nargs="+", default=["film", "film-light", "music", "music-light", "speech"])
    ap.add_argument("--refs", nargs="+", default=["-31", "-23", "-20"])
    ap.add_argument("--bitrate", type=int, default=128000)
    ap.add_argument("--seconds", type=float, default=16.0)
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds at the end of each tone used for the steady value")
    ap.add_argument("--segment", type=float, default=6.0)
    ap.add_argument("--quiet", type=float, default=-40.0)
    ap.add_argument("--loud", type=float, default=-6.0)
    ap.add_argument("--profile", default="film")
    ap.add_argument("--ref", type=float, default=-23.0)
    ap.add_argument("--aot", choices=["lc", "he-aac-v1", "he-aac-v2"], default="lc")
    ap.add_argument("--json")
    args = ap.parse_args()
    return {"static": static, "steps": steps, "decode": decode}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
