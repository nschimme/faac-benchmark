"""
 * FAAC Benchmark Suite — Phase 3: Stereo Image Fidelity
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

 ----------------------------------------------------------------------------

 Why this phase exists
 ---------------------
 ViSQOL "audio" mode (Phase 2) is effectively monaural: decoding the same AAC
 to stereo vs. mono yields a near-identical MOS. It scores per-frame spectral
 fidelity and is blind to the *stereo image*. This biases the benchmark toward
 stereo collapse — forced Intensity Stereo (--joint 2) discards the L/R
 relationship to bank bits for spectral fidelity that ViSQOL rewards, so it can
 out-score Mixed Mode (--joint 3) on Phase 2 while being perceptually worse for
 stereo material.

 This phase measures the property Phase 2 cannot: how faithfully the inter-
 channel relationship is reconstructed. It computes a windowed inter-channel
 coherence error (lower = truer stereo image) between the reference and the
 decoded output, after removing the codec delay by cross-correlation alignment.

 It is a regression *guard*, not a perceptual ground truth — the gold standard
 for stereo coding remains a subjective MUSHRA/ABX listening test. Use this to
 ensure stereo changes do not silently degrade the image, and to detect when a
 change trades real stereo fidelity (higher is better) for a higher (monaural) MOS.

 This phase also computes attack-centroid-shift (see transient.py), a
 transient-fidelity diagnostic unrelated to the stereo image but folded in
 here rather than given its own phase: both metrics need the same
 reference/decoded audio decoded once, and a fourth full decode pass over
 the matrix (after phase1 encode and phase2 MOS) would be pure added cost
 for no benefit. mono-mixed from the same decoded stereo WAVs this phase
 already produces, so it costs no extra decode.
"""

import os
import sys
import json
import argparse
import subprocess
import tempfile
import wave
import concurrent.futures
import hashlib

import numpy as np
from scipy.signal import fftconvolve

from config import SCENARIOS
from utils import get_aac_path, wav_conv, get_cached_ref_wav
from transient import attack_centroid_deltas

# 48 kHz, 50 ms analysis frames.
FRAME = 2400


def decode_stereo(path, tmpdir, tag, rate=48000):
    """Decode/transcode any audio file to 48 kHz 16-bit stereo wav."""
    out = os.path.join(tmpdir, f"{tag}.wav")
    if wav_conv(path, out, rate=rate, channels=2):
        return out
    return None


def read_stereo(path):
    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if ch >= 2:
        a = a.reshape(-1, ch)
        return a[:, 0], a[:, 1]
    return a, a  # mono source: both channels identical


def estimate_delay(ref, deg, win=50000, maxlag=4096):
    """Samples that `deg` lags `ref`, via cross-correlation of the left channel."""
    r = ref[:win]
    d = deg[:win + maxlag]
    if len(d) < len(r):
        return 0
    r = r - r.mean()
    d = d - d.mean()
    corr = fftconvolve(d, r[::-1], mode="valid")
    return int(np.argmax(corr))


def coherence_vectorized(L, R, frame_size):
    """Compute coherence for each frame of size `frame_size`."""
    num_frames = L.shape[0] // frame_size
    if num_frames == 0:
        return np.array([])

    L_f = L[:num_frames * frame_size].reshape(-1, frame_size)
    R_f = R[:num_frames * frame_size].reshape(-1, frame_size)

    # Sums across the frame dimension (axis=1)
    sum_L2 = np.sum(L_f * L_f, axis=1)
    sum_R2 = np.sum(R_f * R_f, axis=1)
    sum_LR = np.sum(L_f * R_f, axis=1)

    den = np.sqrt(sum_L2 * sum_R2) + 1e-9
    return sum_LR / den


def coherence_error(ref_path, deg_path):
    """Mean per-frame |coherence(ref) - coherence(deg)|, time-aligned.
    Note: Lower error is better. Reporting layers invert this to Fidelity (1.0 - error).

    Returns None if the reference is mono (no stereo image to measure)."""
    rL, rR = read_stereo(ref_path)
    dL, dR = read_stereo(deg_path)

    # Mono reference: nothing to measure.
    if np.array_equal(rL, rR):
        return None

    lag = estimate_delay(rL, dL)
    dL, dR = dL[lag:], dR[lag:]
    m = min(len(rL), len(dL))
    rL, rR, dL, dR = rL[:m], rR[:m], dL[:m], dR[:m]

    ref_coh = coherence_vectorized(rL, rR, FRAME)
    deg_coh = coherence_vectorized(dL, dR, FRAME)

    if ref_coh.size > 0 and deg_coh.size > 0:
        errs = np.abs(ref_coh - deg_coh)
    else:
        # Fallback for short clips: compute coherence over the whole available segment.
        def simple_coherence(L, R):
            den = np.sqrt(np.sum(L * L) * np.sum(R * R)) + 1e-9
            return np.sum(L * R) / den
        errs = np.array([abs(simple_coherence(rL, rR) - simple_coherence(dL, dR))])

    return float(np.mean(errs)) if errs.size > 0 else None


def compute_single(key, aac_path, ref_wav_path, external_data_dir, ref_path=None,
                    ref_cache_dir=None, want_ic=True, want_transient=True):
    with tempfile.TemporaryDirectory() as td:
        if ref_wav_path and os.path.exists(ref_wav_path):
            ref_wav = ref_wav_path
        elif ref_cache_dir and ref_path and os.path.exists(ref_path):
            ref_wav = get_cached_ref_wav(ref_cache_dir, ref_path, 48000, 2)
        else:
            if not ref_path or not os.path.exists(ref_path):
                return key, None, None
            ref_wav = decode_stereo(ref_path, td, "ref")

        deg_wav = decode_stereo(aac_path, td, "deg")
        if not ref_wav or not deg_wav:
            return key, None, None

        ic = None
        if want_ic:
            try:
                ic = coherence_error(ref_wav, deg_wav)
            except Exception as e:
                print(f"  coherence error for {key}: {e}")

        centroid_ms = None
        if want_transient:
            try:
                rL, rR = read_stereo(ref_wav)
                dL, dR = read_stereo(deg_wav)
                ref_mono = (rL + rR) / 2.0
                dec_mono = (dL + dR) / 2.0
                centroid_ms = attack_centroid_deltas(ref_mono, dec_mono, 48000)
            except Exception as e:
                print(f"  attack-centroid-shift error for {key}: {e}")

        return key, ic, centroid_ms


def main():
    parser = argparse.ArgumentParser(
        description="Stereo image fidelity (inter-channel coherence) and "
                     "transient fidelity (attack-centroid-shift) — Phase 3")
    parser.add_argument("results_json", help="Path to results JSON file")
    parser.add_argument("aac_dir", help="Path to directory containing AAC files")
    parser.add_argument("external_data_dir", help="Path to external data directory")
    parser.add_argument("--skip-stereo", action="store_true",
                        help="Skip inter-channel coherence (stereo image) scoring")
    parser.add_argument("--skip-transient", action="store_true",
                        help="Skip attack-centroid-shift (transient fidelity) scoring")
    args = parser.parse_args()

    want_ic = not args.skip_stereo
    want_transient = not args.skip_transient
    if not want_ic and not want_transient:
        print("Both --skip-stereo and --skip-transient given; nothing to do.")
        return

    with open(args.results_json) as f:
        data = json.load(f)
    matrix = data.get("matrix", {})

    try:
        aac_files = [f for f in os.listdir(args.aac_dir) if f.endswith((".m4a", ".mp4", ".aac", ".opus", ".mp3"))]
    except FileNotFoundError:
        aac_files = []

    # Only non-speech scenarios, and only entries missing a metric this
    # invocation was asked to compute.
    def is_pending(v):
        if SCENARIOS.get(v.get("scenario"), {}).get("mode") == "speech":
            return False
        if want_ic and v.get("ic_err") is None:
            return True
        if want_transient and v.get("attack_centroid_ms") is None:
            return True
        return False

    pending = {k: v for k, v in matrix.items() if is_pending(v)}
    if not pending:
        print("No pending stereo/transient computations.")
        return

    # Identify unique reference files for caching
    unique_refs = sorted(list(set(v.get("filename") for v in pending.values())))

    num_cpus = os.cpu_count() or 1
    print(f"Computing stereo/transient fidelity for {len(pending)} samples "
          f"({num_cpus} cores)...")

    # Pre-resolve AAC paths in the main process so workers don't touch the filesystem.
    resolved = {}
    for k, v in pending.items():
        p = get_aac_path(k, args.aac_dir, args.results_json, aac_files=aac_files, entry=v)
        if p:
            resolved[k] = (v, p)

    if not resolved:
        print("No resolvable AAC paths for pending samples.")
        return

    ic_results = {}
    centroid_results = {}
    with tempfile.TemporaryDirectory() as ref_cache_dir:
        ref_wav_map = {}
        if len(unique_refs) < len(resolved):
            print(f"Pre-decoding {len(unique_refs)} unique reference files (parallel)...")
            with concurrent.futures.ThreadPoolExecutor() as pool:
                ref_futs = {}
                for filename in unique_refs:
                    ref_path = os.path.join(args.external_data_dir, "audio", filename)
                    if os.path.exists(ref_path):
                        ref_futs[pool.submit(get_cached_ref_wav, ref_cache_dir, ref_path, 48000, 2)] = filename
                for fut in concurrent.futures.as_completed(ref_futs):
                    filename = ref_futs[fut]
                    wav_path = fut.result()
                    if wav_path:
                        ref_wav_map[filename] = wav_path

        with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
            futures = {
                executor.submit(
                    compute_single, k, aac_path,
                    ref_wav_map.get(entry.get("filename")),
                    args.external_data_dir,
                    os.path.join(args.external_data_dir, "audio", entry.get("filename", "")),
                    ref_cache_dir,
                    want_ic and entry.get("ic_err") is None,
                    want_transient and entry.get("attack_centroid_ms") is None,
                ): k
                for k, (entry, aac_path) in resolved.items()
            }
            total = len(futures)
            for i, fut in enumerate(concurrent.futures.as_completed(futures)):
                key, ic, centroid_ms = fut.result()
                if ic is not None:
                    ic_results[key] = ic
                if centroid_ms is not None:
                    centroid_results[key] = centroid_ms
                ic_str = f"{ic:.4f}" if ic is not None else "N/A"
                n_onsets = len(centroid_ms) if centroid_ms is not None else "N/A"
                print(f"  ({i+1}/{total}) {key}: ic_err={ic_str}  centroid_onsets={n_onsets}")

    for key, ic in ic_results.items():
        if key in matrix:
            matrix[key]["ic_err"] = ic
    for key, centroid_ms in centroid_results.items():
        if key in matrix:
            matrix[key]["attack_centroid_ms"] = centroid_ms

    with open(args.results_json, "w") as f:
        json.dump(data, f, indent=2)
    print("Phase 3 (stereo image / transient fidelity) complete.")


if __name__ == "__main__":
    main()
