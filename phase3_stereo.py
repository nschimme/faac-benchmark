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
from utils import get_aac_path, wav_conv, get_cached_ref_wav, scenario_channels, corpus_dir
from transient import attack_centroid_deltas
from codec_bench.decoders import get_decoder_instance

# 48 kHz, 50 ms analysis frames.
FRAME = 2400


def decode_audio(path, tmpdir, tag, rate=48000, channels=2, decoder=None):
    """Decode/transcode any audio file to 48 kHz 16-bit wav with specified channels."""
    out = os.path.join(tmpdir, f"{tag}.wav")
    if not decoder or decoder.tool_id == "ffmpeg_aac" or tag == "ref":
        if wav_conv(path, out, rate=rate, channels=channels):
            return out
        return None

    requires_adts = getattr(decoder, "requires_adts", False)
    bitstream_input = path
    if requires_adts and path.lower().endswith((".m4a", ".mp4")):
        temp_adts = os.path.join(tmpdir, f"{tag}_demux.aac")
        cmd_demux = ["ffmpeg", "-y", "-i", path, "-c:a", "copy", temp_adts]
        try:
            res_demux = subprocess.run(cmd_demux, capture_output=True, text=True)
            if res_demux.returncode == 0 and os.path.exists(temp_adts):
                bitstream_input = temp_adts
        except Exception:
            pass

    raw_dec_wav = os.path.join(tmpdir, f"{tag}_raw_dec.wav")
    cmd_dec = decoder.get_decode_cmd(bitstream_input, raw_dec_wav)
    try:
        res_dec = subprocess.run(cmd_dec, capture_output=True, text=True, env=decoder.get_run_env() or None)
        if res_dec.returncode == 0 and os.path.exists(raw_dec_wav):
            if wav_conv(raw_dec_wav, out, rate=rate, channels=channels):
                return out
    except Exception:
        pass

    if wav_conv(path, out, rate=rate, channels=channels):
        return out
    return None


def read_audio_channels(path):
    try:
        with wave.open(path, "rb") as w:
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
        if not raw:
            return np.array([]).reshape(0, 1)
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        if ch >= 1:
            return a.reshape(-1, ch)
        return a.reshape(-1, 1)
    except Exception:
        return np.array([]).reshape(0, 1)


def read_stereo(path):
    a = read_audio_channels(path)
    if a.shape[1] >= 2:
        return a[:, 0], a[:, 1]
    if a.shape[1] == 1:
        return a[:, 0], a[:, 0]
    return np.array([]), np.array([])


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
    """Mean per-frame |coherence(ref) - coherence(deg)| across channel pairs, time-aligned.
    Note: Lower error is better. Reporting layers invert this to Fidelity (1.0 - error).

    Returns None if the reference is mono (no inter-channel image to measure)."""
    r_data = read_audio_channels(ref_path)
    d_data = read_audio_channels(deg_path)

    if len(r_data) == 0 or len(d_data) == 0:
        return None

    ch = r_data.shape[1]
    if ch < 2:
        return None

    # Mono reference (all channels identical): nothing to measure.
    if all(np.array_equal(r_data[:, 0], r_data[:, i]) for i in range(1, ch)):
        return None

    lag = estimate_delay(r_data[:, 0], d_data[:, 0])
    if lag > 0:
        d_data = d_data[lag:, :]
    elif lag < 0:
        r_data = r_data[-lag:, :]

    m = min(len(r_data), len(d_data))
    if m == 0:
        return None
    r_data, d_data = r_data[:m, :], d_data[:m, :]

    # Determine channel pairs (e.g. Front L/R and Surround Ls/Rs for 5.1 surround)
    if ch >= 6:
        pairs = [(0, 1), (4, 5)]
    else:
        pairs = [(0, 1)]

    pair_errs = []
    for c1, c2 in pairs:
        if c1 < ch and c2 < ch:
            rL, rR = r_data[:, c1], r_data[:, c2]
            dL, dR = d_data[:, c1], d_data[:, c2]

            ref_coh = coherence_vectorized(rL, rR, FRAME)
            deg_coh = coherence_vectorized(dL, dR, FRAME)

            if ref_coh.size > 0 and deg_coh.size > 0:
                errs = np.abs(ref_coh - deg_coh)
            else:
                def simple_coherence(L, R):
                    den = np.sqrt(np.sum(L * L) * np.sum(R * R)) + 1e-9
                    return np.sum(L * R) / den
                errs = np.array([abs(simple_coherence(rL, rR) - simple_coherence(dL, dR))])

            if errs.size > 0:
                pair_errs.append(float(np.mean(errs)))

    return float(np.mean(pair_errs)) if pair_errs else None


def compute_single(key, aac_path, ref_wav_path, external_data_dir, ref_path=None,
                    ref_cache_dir=None, want_ic=True, want_transient=True, channels=2, decoder=None):
    with tempfile.TemporaryDirectory() as td:
        if ref_wav_path and os.path.exists(ref_wav_path):
            ref_wav = ref_wav_path
        elif ref_cache_dir and ref_path and os.path.exists(ref_path):
            ref_wav = get_cached_ref_wav(ref_cache_dir, ref_path, 48000, channels)
        else:
            if not ref_path or not os.path.exists(ref_path):
                return key, None, None
            ref_wav = decode_audio(ref_path, td, "ref", channels=channels)

        deg_wav = decode_audio(aac_path, td, "deg", channels=channels, decoder=decoder)
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
                r_data = read_audio_channels(ref_wav)
                d_data = read_audio_channels(deg_wav)
                ref_mono = r_data.mean(axis=1) if len(r_data) > 0 else np.array([])
                dec_mono = d_data.mean(axis=1) if len(d_data) > 0 else np.array([])
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
    parser.add_argument("--decoder", default="ffmpeg", help="Decoder type: ffmpeg, faad, fdkdec, helix, afconvert")
    parser.add_argument("--decoder-bin", help="Path to decoder binary")
    parser.add_argument("--decoder-lib", help="Path to decoder shared library override")
    args = parser.parse_args()

    decoder_inst = get_decoder_instance(args.decoder, binary_path=args.decoder_bin, lib_override=args.decoder_lib)

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

    # Only stereo scenarios, and only entries missing a metric this
    # invocation was asked to compute.
    def is_pending(v):
        # Stereo image fidelity is undefined for mono content. Key off the
        # corpus channel count, not the metric mode -- 24k_mono_* is mono
        # content scored in audio mode.
        if scenario_channels(SCENARIOS.get(v.get("scenario"), {})) < 2:
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

    # Identify unique (filename, channels, corpus_dir) tuples for reference caching
    unique_ref_tuples = sorted(list(set(
        (v.get("filename"),
         scenario_channels(SCENARIOS.get(v.get("scenario"), {})),
         corpus_dir(SCENARIOS.get(v.get("scenario"), {}), args.external_data_dir))
        for v in pending.values()
        if v.get("filename")
    )))

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
        if len(unique_ref_tuples) < len(resolved):
            print(f"Pre-decoding {len(unique_ref_tuples)} unique reference files (parallel)...")
            with concurrent.futures.ThreadPoolExecutor() as pool:
                ref_futs = {}
                for filename, chans, c_dir in unique_ref_tuples:
                    ref_path = os.path.join(c_dir, filename)
                    if os.path.exists(ref_path):
                        ref_futs[pool.submit(get_cached_ref_wav, ref_cache_dir, ref_path, 48000, chans)] = (filename, chans)
                for fut in concurrent.futures.as_completed(ref_futs):
                    key_tuple = ref_futs[fut]
                    wav_path = fut.result()
                    if wav_path:
                        ref_wav_map[key_tuple] = wav_path

        with concurrent.futures.ProcessPoolExecutor(max_workers=num_cpus) as executor:
            futures = {
                executor.submit(
                    compute_single, k, aac_path,
                    ref_wav_map.get((entry.get("filename"), scenario_channels(SCENARIOS.get(entry.get("scenario"), {})))),
                    args.external_data_dir,
                    os.path.join(corpus_dir(SCENARIOS.get(entry.get("scenario"), {}), args.external_data_dir), entry.get("filename", "")),
                    ref_cache_dir,
                    want_ic and entry.get("ic_err") is None,
                    want_transient and entry.get("attack_centroid_ms") is None,
                    scenario_channels(SCENARIOS.get(entry.get("scenario"), {})),
                    decoder_inst,
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
