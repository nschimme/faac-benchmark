#!/usr/bin/env python3
"""DRC probe: level and MOS of decoders on streams that carry dynamic_range_info.

Streams come from fdk's metadata encoder (drcenc.c: profile x prog_ref_level).
Every decoder reads the same bitstream, so bits-adjusting does not apply; MOS is
against the source PCM, raw and with the decoded level matched to the source.
"""
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path
import numpy as np, soundfile as sf
from scipy.signal import fftconvolve

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from phase2_mos import score_wav_pair  # noqa: E402


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def lufs(path):
    r = run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"])
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1])


def decode(name, aac, out, a):
    if name == "faad2":
        run([a.faad2, "-b", "4", "-o", str(out), str(aac)])
    elif name == "libfaad3":
        run([a.faad3, "-b", "32f", "-o", str(out), str(aac)])
    elif name == "fdk":
        run([a.fdkdec, str(aac), str(out)])
    elif name == "ffmpeg":
        run(["ffmpeg", "-v", "error", "-y", "-i", str(aac), "-c:a", "pcm_f32le", str(out)])


def lag_of(ref, deg):
    n = min(len(ref), len(deg), 48000 * 20)
    a, b = ref[:n].mean(1), deg[:n].mean(1)
    c = fftconvolve(b, a[::-1], mode="full")
    return int(np.argmax(c)) - (n - 1)


def to16(x, path):
    sf.write(path, np.clip(x, -1.0, 32767 / 32768), 48000, subtype="PCM_16")


def mos(ref_wav, deg):
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "d.wav"
        to16(deg, d)
        return score_wav_pair(str(ref_wav), str(d), "audio")[0]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--clips", nargs="+", required=True)
    p.add_argument("--drcenc", required=True)
    p.add_argument("--faad2", default=str(ROOT / "bin/faad"))
    p.add_argument("--faad3", required=True)
    p.add_argument("--fdkdec", default=str(ROOT / "bin/fdkdec"))
    p.add_argument("--work", required=True)
    p.add_argument("--aot", type=int, default=2)
    p.add_argument("--bitrate", type=int, default=128000)
    p.add_argument("--profiles", nargs="+", type=int, default=[0, 3])
    p.add_argument("--refs", nargs="+", default=["none", "-20", "-23", "-31"])
    p.add_argument("--json", required=True)
    a = p.parse_args()
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for clip in a.clips:
        cp = Path(clip)
        ref, sr = sf.read(cp, dtype="float32")
        assert sr == 48000 and ref.ndim == 2, "48 kHz stereo clips only"
        ref_i = lufs(cp)
        for prof in a.profiles:
            for r in a.refs:
                tag = f"{cp.stem}_a{a.aot}_p{prof}_r{r}"
                aac = work / f"{tag}.aac"
                run([a.drcenc, str(cp), str(aac), str(a.bitrate), str(prof), r, str(a.aot)])
                decs = {}
                for d in ("libfaad3", "faad2", "fdk", "ffmpeg"):
                    out = work / f"{tag}.{d}.wav"
                    decode(d, aac, out, a)
                    decs[d] = out
                x3, _ = sf.read(decs["libfaad3"], dtype="float32")
                lag = lag_of(ref, x3)
                n = len(ref) - max(lag, 0) - 4096
                refc = ref[max(-lag, 0) + 2048:][:n - 2048]
                ref_wav = work / f"{tag}.ref.wav"
                to16(refc, ref_wav)
                for d, path in decs.items():
                    x, _ = sf.read(path, dtype="float32")
                    x = x[max(lag, 0) + 2048:][:len(refc)] if lag >= 0 else x[2048:][:len(refc)]
                    il = lufs(path)
                    gain_db = ref_i - il
                    row = dict(clip=cp.stem, aot=a.aot, profile=prof, prog_ref=r, decoder=d,
                               lufs=il, src_lufs=ref_i, delta_vs_src_db=il - ref_i,
                               clipped=int((np.abs(x) >= 1.0).sum()), peak=float(np.abs(x).max()),
                               mos_raw=mos(ref_wav, x),
                               mos_matched=mos(ref_wav, x * 10 ** (gain_db / 20)))
                    rows.append(row)
                    print(json.dumps(row), flush=True)
                # identity check vs libfaad3 (known-answer control: 0 when no gain applied)
                for d in ("faad2", "fdk", "ffmpeg"):
                    x, _ = sf.read(decs[d], dtype="float32")
                    m = min(len(x), len(x3))
                    rows[-1]  # keep order
                    diff = float(np.abs(x[:m] - x3[:m]).max())
                    rows.append(dict(clip=cp.stem, aot=a.aot, profile=prof, prog_ref=r,
                                     decoder=d, maxabsdiff_vs_libfaad3=diff))
    Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
