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
 change trades real stereo fidelity for a higher (monaural) MOS.
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

try:
    import ffmpeg
except ImportError:
    ffmpeg = None

from config import SCENARIOS
from phase2_mos import get_aac_path

# 48 kHz, 50 ms analysis frames.
FRAME = 2400


def decode_stereo(path, tmpdir, tag, rate=48000):
    """Decode/transcode any audio file to 48 kHz 16-bit stereo wav."""
    out = os.path.join(tmpdir, f"{tag}.wav")
    try:
        if ffmpeg:
            try:
                ffmpeg.input(path).output(
                    out, ar=rate, ac=2, sample_fmt='s16').run(
                    quiet=True, overwrite_output=True)
            except ffmpeg.Error as e:
                print(f"ffmpeg-python failed for {path}:\n{e.stderr.decode() if e.stderr else str(e)}", file=sys.stderr)
                return None
        else:
            r = subprocess.run(["ffmpeg", "-y", "-i", path, "-ar", str(rate), "-ac", "2",
                               "-sample_fmt", "s16", out],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"ffmpeg failed for {path}:\n{r.stderr}", file=sys.stderr)
                return None
        return out
    except Exception as e:
        print(f"ffmpeg failed for {path}: {e}", file=sys.stderr)
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
    return int(np.argmax(np.correlate(d, r, mode="valid")))


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


def compute_single(key, entry, aac_dir, external_data_dir, results_path, aac_files=None, ref_wav_path=None):
    scenario_name = entry.get("scenario")
    cfg = SCENARIOS.get(scenario_name)
    if not cfg or cfg["mode"] == "speech":
        return key, None  # speech corpus is mono

    filename = entry.get("filename")
    ref_path = os.path.join(external_data_dir, "audio", filename)
    aac_path = get_aac_path(key, aac_dir, results_path, aac_files=aac_files)
    if not aac_path or not os.path.exists(ref_path):
        return key, None

    with tempfile.TemporaryDirectory() as td:
        if ref_wav_path and os.path.exists(ref_wav_path):
            ref_wav = ref_wav_path
        else:
            ref_wav = decode_stereo(ref_path, td, "ref")

        deg_wav = decode_stereo(aac_path, td, "deg")
        if not ref_wav or not deg_wav:
            return key, None
        try:
            return key, coherence_error(ref_wav, deg_wav)
        except Exception as e:
            print(f"  coherence error for {key}: {e}")
            return key, None


def main():
    parser = argparse.ArgumentParser(
        description="Stereo image fidelity — inter-channel coherence error (Phase 3)")
    parser.add_argument("results_json", help="Path to results JSON file")
    parser.add_argument("aac_dir", help="Path to directory containing AAC files")
    parser.add_argument("external_data_dir", help="Path to external data directory")
    args = parser.parse_args()

    with open(args.results_json) as f:
        data = json.load(f)
    matrix = data.get("matrix", {})

    try:
        aac_files = [f for f in os.listdir(args.aac_dir) if f.endswith(".aac")]
    except FileNotFoundError:
        aac_files = []

    # Only stereo scenarios, and only entries not already scored.
    pending = {
        k: v for k, v in matrix.items()
        if v.get("ic_err") is None
        and SCENARIOS.get(v.get("scenario"), {}).get("mode") != "speech"
    }
    if not pending:
        print("No pending stereo computations.")
        return

    # Identify unique reference files for caching
    unique_refs = sorted(list(set(v.get("filename") for v in pending.values())))

    num_cpus = os.cpu_count() or 1
    print(f"Computing inter-channel coherence error for {len(pending)} stereo samples "
          f"({num_cpus} cores)...")

    results = {}
    with tempfile.TemporaryDirectory() as ref_cache_dir:
        ref_wav_map = {}
        if len(unique_refs) < len(pending):
            print(f"Pre-decoding {len(unique_refs)} unique reference files...")
            for i, filename in enumerate(unique_refs):
                ref_path = os.path.join(args.external_data_dir, "audio", filename)
                if os.path.exists(ref_path):
                    # Use a hash or safe name for the cached wav
                    tag = hashlib.md5(filename.encode()).hexdigest()
                    wav_path = decode_stereo(ref_path, ref_cache_dir, tag)
                    if wav_path:
                        ref_wav_map[filename] = wav_path
                print(f"  ({i+1}/{len(unique_refs)}) {filename} decoded.", end="\r")
            print("")

        # Group pending by scenario for progress reporting
        scenarios_pending = {}
        for k, v in pending.items():
            s = v.get("scenario")
            if s not in scenarios_pending:
                scenarios_pending[s] = []
            scenarios_pending[s].append((k, v))

        with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
            for scenario_name, items in scenarios_pending.items():
                print(f"  [Scenario: {scenario_name}] Processing {len(items)} samples...")
                futures = {
                    executor.submit(compute_single, k, v, args.aac_dir,
                                    args.external_data_dir, args.results_json,
                                    aac_files, ref_wav_map.get(v.get("filename"))): k
                    for k, v in items
                }
                for i, fut in enumerate(concurrent.futures.as_completed(futures)):
                    key, ic = fut.result()
                    if ic is not None:
                        results[key] = ic
                    ic_str = f"{ic:.4f}" if ic is not None else "N/A"
                    print(f"    ({i+1}/{len(items)}) {key}: {ic_str}")

    for key, ic in results.items():
        if key in matrix:
            matrix[key]["ic_err"] = ic

    with open(args.results_json, "w") as f:
        json.dump(data, f, indent=2)
    print("Phase 3 (stereo image) complete.")


if __name__ == "__main__":
    main()
