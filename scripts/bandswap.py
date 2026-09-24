"""
 * FAAC Benchmark Suite - Band-Swap Attribution Tool
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

Attributes a quality gap between two encoders to the low band (AAC core) vs
the high band (SBR) by splitting each decoded signal with a linear-phase FIR
at a crossover frequency and cross-breeding low/high halves into hybrids:

    hybrid(A-low + B-high) = A's core, B's SBR
    hybrid(B-low + A-high) = B's core, A's SBR

Scoring all four (A, B, both hybrids) against the reference with the same MOS
engine isolates how much of score(B) - score(A) traces to each band:

    low_fix(A vs B)  = score(B-low + A-high) - score(A)   # A's core swapped for B's
    high_fix(A vs B) = score(A-low + B-high) - score(A)   # A's SBR swapped for B's

Generic over any two shell-command encoders; not FAAC/Apple-specific.
"""

import argparse
import csv
import os
import subprocess
import sys
import tempfile
import shlex
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import soundfile as sf
import scipy.signal

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from utils import wav_conv, select_corpus_clips
from phase2_mos import score_wav_pair

EXTERNAL_DATA_DIR = os.path.join(ROOT_DIR, "data", "external")


def encode(cmd_template, in_wav, out_path, kbps):
    """cmd_template is a list of tokens with {in}, {out}, {kbps} placeholders."""
    cmd = [t.format(**{"in": in_wav, "out": out_path, "kbps": kbps}) for t in cmd_template]
    subprocess.run(cmd, capture_output=True, check=True)


def decode_to_wav(m4a_path, wav_path, rate=48000, channels=2):
    if not wav_conv(m4a_path, wav_path, rate, channels):
        raise RuntimeError(f"decode failed: {m4a_path}")


def align_to_ref(ref, cand):
    """Cross-correlate mono downmixes, trim/pad cand to ref's length and lag.
    Returns (cand_aligned, lag_samples)."""
    r_mono = ref.mean(axis=1)
    c_mono = cand.mean(axis=1)
    n_search = min(len(r_mono), len(c_mono), 48000 * 3)
    r_norm = r_mono[:n_search] / (np.std(r_mono[:n_search]) + 1e-10)
    c_norm = c_mono[:n_search] / (np.std(c_mono[:n_search]) + 1e-10)
    corr = scipy.signal.correlate(r_norm, c_norm, mode="full")
    lag = int(np.argmax(corr)) - (n_search - 1)

    # correlate(ref, cand) peaks at -D when cand lags ref by D samples.
    if lag < 0:
        cand = cand[-lag:]
    elif lag > 0:
        pad = np.zeros((lag, cand.shape[1]), dtype=cand.dtype)
        cand = np.concatenate([pad, cand], axis=0)

    n = min(len(ref), len(cand))
    if len(cand) < len(ref):
        pad = np.zeros((len(ref) - len(cand), cand.shape[1]), dtype=cand.dtype)
        cand = np.concatenate([cand, pad], axis=0)
    return cand[:len(ref)], lag


def fir_split(signal, sr, cutoff_hz, numtaps=2047):
    """Zero-phase (filtfilt) low/high split via a linear-phase FIR lowpass.
    Returns (low, high) same shape as signal; low + high == signal (up to the
    FIR's transition-band leakage, which is why numtaps is large)."""
    taps = scipy.signal.firwin(numtaps, cutoff_hz, fs=sr)
    low = np.zeros_like(signal)
    for ch in range(signal.shape[1]):
        low[:, ch] = scipy.signal.filtfilt(taps, [1.0], signal[:, ch])
    high = signal - low
    return low, high


def write_wav(path, data, sr):
    sf.write(path, data, sr, subtype="PCM_16")


def process_clip(args):
    (ref_path, clip_name, enc_a_cmd, enc_b_cmd, name_a, name_b, kbps, splits, workdir) = args
    result_rows = []
    with tempfile.TemporaryDirectory(dir=workdir) as td:
        m4a_a = os.path.join(td, "a.m4a")
        m4a_b = os.path.join(td, "b.m4a")
        wav_a = os.path.join(td, "a.wav")
        wav_b = os.path.join(td, "b.wav")
        try:
            encode(enc_a_cmd, ref_path, m4a_a, kbps)
            encode(enc_b_cmd, ref_path, m4a_b, kbps)
            decode_to_wav(m4a_a, wav_a)
            decode_to_wav(m4a_b, wav_b)
        except Exception as e:
            print(f"  ERROR encoding/decoding {clip_name}: {e}", file=sys.stderr)
            return result_rows

        ref_data, ref_sr = sf.read(ref_path, dtype="float32", always_2d=True)
        if ref_sr != 48000:
            g = np.gcd(48000, ref_sr)
            ref_data = scipy.signal.resample_poly(ref_data, 48000 // g, ref_sr // g, axis=0)
            ref_sr = 48000
        a_data, _ = sf.read(wav_a, dtype="float32", always_2d=True)
        b_data, _ = sf.read(wav_b, dtype="float32", always_2d=True)

        a_aligned, lag_a = align_to_ref(ref_data, a_data)
        b_aligned, lag_b = align_to_ref(ref_data, b_data)

        ref_wav_tmp = os.path.join(td, "ref.wav")
        write_wav(ref_wav_tmp, ref_data, ref_sr)

        a_full = os.path.join(td, "a_full.wav")
        b_full = os.path.join(td, "b_full.wav")
        write_wav(a_full, a_aligned, ref_sr)
        write_wav(b_full, b_aligned, ref_sr)

        mos_a, _ = score_wav_pair(ref_wav_tmp, a_full, "audio", sample_rate=48000)
        mos_b, _ = score_wav_pair(ref_wav_tmp, b_full, "audio", sample_rate=48000)

        for split_hz in splits:
            a_lo, a_hi = fir_split(a_aligned, ref_sr, split_hz)
            b_lo, b_hi = fir_split(b_aligned, ref_sr, split_hz)

            hyb_afix_lo = os.path.join(td, f"hyb_blo_ahi_{split_hz}.wav")  # B-low + A-high: "fix A's low band"
            hyb_afix_hi = os.path.join(td, f"hyb_alo_bhi_{split_hz}.wav")  # A-low + B-high: "fix A's high band"
            write_wav(hyb_afix_lo, b_lo + a_hi, ref_sr)
            write_wav(hyb_afix_hi, a_lo + b_hi, ref_sr)

            mos_lofix, _ = score_wav_pair(ref_wav_tmp, hyb_afix_lo, "audio", sample_rate=48000)
            mos_hifix, _ = score_wav_pair(ref_wav_tmp, hyb_afix_hi, "audio", sample_rate=48000)

            result_rows.append({
                "clip": clip_name,
                "kbps": kbps,
                "split_hz": split_hz,
                f"mos_{name_a}": mos_a,
                f"mos_{name_b}": mos_b,
                "mos_lowfix": mos_lofix,   # name_b's low band + name_a's high band
                "mos_highfix": mos_hifix,  # name_a's low band + name_b's high band
                "lag_a": lag_a,
                "lag_b": lag_b,
            })
    return result_rows


def main():
    ap = argparse.ArgumentParser(description="Band-swap attribution between two encoders.")
    ap.add_argument("--enc-a", required=True, help="Shell command template for encoder A, "
                     "tokens separated by spaces, use {in}/{out}/{kbps} placeholders")
    ap.add_argument("--enc-b", required=True, help="Shell command template for encoder B")
    ap.add_argument("--name-a", default="a")
    ap.add_argument("--name-b", default="b")
    ap.add_argument("--kbps", default="48,64", help="Comma-separated bitrate list (total stream kbps)")
    ap.add_argument("--split", default="9000,11625", help="Comma-separated crossover Hz list")
    ap.add_argument("--corpus-dir", default=os.path.join(EXTERNAL_DATA_DIR, "audio"))
    ap.add_argument("--max-clips", type=int, default=49)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out-csv", default=os.path.join(ROOT_DIR, "results", "bandswap_apple_he.csv"))
    ap.add_argument("--limit", type=int, default=None, help="Debug: only process first N clips")
    args = ap.parse_args()

    enc_a_cmd = shlex.split(args.enc_a)
    enc_b_cmd = shlex.split(args.enc_b)
    kbps_list = [int(x) for x in args.kbps.split(",")]
    split_list = [int(x) for x in args.split.split(",")]

    files = sorted(f for f in os.listdir(args.corpus_dir) if f.lower().endswith(".wav"))
    files = select_corpus_clips(files, {"max_clips": args.max_clips})
    if args.limit:
        files = files[:args.limit]
    print(f"{len(files)} clips, kbps={kbps_list}, splits={split_list}")

    tasks = []
    workdir = tempfile.mkdtemp(prefix="bandswap_")
    for clip in files:
        ref_path = os.path.join(args.corpus_dir, clip)
        for kbps in kbps_list:
            tasks.append((ref_path, clip, enc_a_cmd, enc_b_cmd, args.name_a, args.name_b,
                          kbps, split_list, workdir))

    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_clip, t): t for t in tasks}
        done = 0
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                r = fut.result()
                rows.extend(r)
            except Exception as e:
                print(f"  ERROR task {t[1]} @ {t[6]}kbps: {e}", file=sys.stderr)
            done += 1
            print(f"  [{done}/{len(tasks)}] {t[1]} @ {t[6]}kbps done", flush=True)

    if not rows:
        print("No results.")
        return

    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)
    fieldnames = ["clip", "kbps", "split_hz", f"mos_{args.name_a}", f"mos_{args.name_b}",
                  "mos_lowfix", "mos_highfix", "lag_a", "lag_b"]
    rows.sort(key=lambda r: (r["kbps"], r["split_hz"], r["clip"]))
    with open(args.out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out_csv}")

    # Summary
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[(r["kbps"], r["split_hz"])].append(r)
    print(f"\n{'kbps':>5} {'split':>7} {'n':>4} {args.name_a:>8} {args.name_b:>8} {'lowfix':>8} {'highfix':>9}")
    for (kbps, split), grp in sorted(groups.items()):
        grp = [r for r in grp if r[f"mos_{args.name_a}"] is not None and r[f"mos_{args.name_b}"] is not None
               and r["mos_lowfix"] is not None and r["mos_highfix"] is not None]
        if not grp:
            continue
        ma = np.mean([r[f"mos_{args.name_a}"] for r in grp])
        mb = np.mean([r[f"mos_{args.name_b}"] for r in grp])
        mlo = np.mean([r["mos_lowfix"] for r in grp])
        mhi = np.mean([r["mos_highfix"] for r in grp])
        print(f"{kbps:>5} {split:>7} {len(grp):>4} {ma:8.3f} {mb:8.3f} {mlo - ma:8.3f} {mhi - ma:9.3f}")


if __name__ == "__main__":
    main()
