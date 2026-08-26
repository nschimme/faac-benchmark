"""
 * FAAC Benchmark Suite — Transient Fidelity (attack-centroid-shift)
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

 Why this module exists
 -----------------------
 NPER (compare_results.py's stereo/MOS neighbors don't cover this either)
 only looks *backward* from a detected onset -- energy leaking into the
 silence before a transient (pre-echo). It says nothing about whether the
 attack itself, once it starts, is preserved or blurred.

 attack-centroid-shift looks *forward*: within a fixed post-onset window
 [onset, onset+fwd_ms) (capped at the next onset), it computes each side's
 own energy-weighted temporal centroid (sum(t*e)/sum(e)) and reports
 dec_centroid - ref_centroid in milliseconds. Positive means decoded energy
 arrives later on average (smeared/delayed attack).

 This is the validated survivor of a two-design investigation done in
 scripts/score_transient.py (see that file's module docstring and
 docs/scripts.md for the full history, including a crossing-based
 "attack-smear" design that was killed by real-material testing: it
 required a quiet pre-onset gap that dense percussive material routinely
 doesn't have). Only the logic that passed validation is ported here, into
 a root-level module, because root CI-critical code (phase*.py) doesn't
 import from scripts/ -- that dependency direction is backwards; scripts/
 is explicitly non-CI-critical per docs/scripts.md. Keep this module's
 onset detection, alignment, and centroid math in sync with
 scripts/score_transient.py's copies by hand if either changes; they are
 deliberately duplicated, not shared, across that boundary.
"""

import numpy as np
import scipy.ndimage
import scipy.signal


# ── algorithm constants (mirrors scripts/score_transient.py) ──────────────────
ZIMT_RATE         = 48000  # onset detection / envelope math below is
                            # calibrated in samples at this rate.
HOP               = 512    # STFT hop length (~10 ms at 48 kHz) for onset detection
WIN_LEN           = 2048   # STFT window (4x hop -> good freq resolution)
MIN_ONSET_SPACING = 1024   # minimum samples between detected onsets (~21 ms)

# Envelope hop for the centroid measurement -- deliberately much finer than
# HOP above: HOP/WIN_LEN are sized for onset *detection* (~10ms resolution is
# plenty to find where a transient starts), but the centroid shift needs to
# resolve real attack rise times, which run ~1-3ms on transient material.
ENVELOPE_HOP = 24  # ~0.5 ms at 48 kHz
ENVELOPE_WIN = 24  # RMS window == hop: a wider window box-smooths the very
                    # shift being measured.
ATTACK_FWD_MS = 12.0  # post-onset window length; capped at the next onset.


# ── onset detection ────────────────────────────────────────────────────────

def detect_onsets(audio, sr, hop=HOP, win_len=WIN_LEN,
                   min_spacing=MIN_ONSET_SPACING):
    """Return list of onset sample positions using spectral flux (no librosa).

    Flux[i] = sum of positive spectral differences between STFT frames i and i+1.
    Onset at flux peak frame i+1 -> sample (i+1)*hop.
    """
    _, _, Zxx = scipy.signal.stft(
        audio, fs=sr, window='hann',
        nperseg=win_len, noverlap=win_len - hop,
        boundary='zeros')
    mag = np.abs(Zxx)
    flux = np.sum(np.maximum(mag[:, 1:] - mag[:, :-1], 0), axis=0)
    threshold = flux.mean() + 1.5 * flux.std()

    onsets = []
    last_onset = -min_spacing - 1
    for i in range(1, len(flux) - 1):
        if (flux[i] > threshold
                and flux[i] >= flux[i - 1]
                and flux[i] >= flux[i + 1]):
            sample = (i + 1) * hop
            if sample - last_onset >= min_spacing:
                onsets.append(sample)
                last_onset = sample

    return onsets


# ── alignment ───────────────────────────────────────────────────────────────

def find_lag(ref_mono, dec_mono, sr, search_seconds=3):
    """Integer lag of dec relative to ref via cross-correlation.

    scipy.signal.correlate(a, b) peaks at index k -> lag = k - (n-1).
    At lag d: a[i] correlates best with b[i-d].
    """
    n = min(len(ref_mono), len(dec_mono), sr * search_seconds)
    r = ref_mono[:n] / (np.std(ref_mono[:n]) + 1e-10)
    d = dec_mono[:n] / (np.std(dec_mono[:n]) + 1e-10)
    corr = scipy.signal.correlate(r, d, mode='full')
    lag = int(np.argmax(corr)) - (n - 1)
    return lag


def align_signals(ref, dec, lag):
    """Trim ref and dec to the same aligned content.

    Positive lag (dec ahead, typical for FAAC+ffmpeg delay-compensation):
      dec started lag samples earlier in audio time -> skip lag from start of ref.
    Negative lag (dec delayed):
      dec started |lag| samples later -> skip |lag| from start of dec.
    """
    if lag < 0:
        dec_out = dec[-lag:]
        ref_out = ref[:len(ref) + lag]
    elif lag > 0:
        ref_out = ref[lag:]
        dec_out = dec[:len(dec) - lag]
    else:
        ref_out, dec_out = ref.copy(), dec.copy()
    n = min(len(ref_out), len(dec_out))
    return ref_out[:n], dec_out[:n]


# ── attack-centroid-shift measurement ────────────────────────────────────────

def envelope(audio, sr, hop=ENVELOPE_HOP, win=ENVELOPE_WIN):
    """Short-time RMS envelope, subsampled at `hop`. Frame i <-> sample i*hop."""
    power = audio.astype(np.float64) ** 2
    smoothed = scipy.ndimage.uniform_filter1d(power, size=win, mode='nearest')
    return np.sqrt(smoothed[::hop])


def compute_attack_centroid_shift(ref, dec, onsets, sr, hop=ENVELOPE_HOP, win=ENVELOPE_WIN,
                                   fwd_ms=ATTACK_FWD_MS):
    """Post-onset energy-centroid shift (milliseconds) at each onset.

    For each onset, in the window [onset, onset+fwd_ms) (capped at the next
    onset): compute each side's own energy-weighted centroid time
    (sum(t*e)/sum(e), e = envelope^2), then delta = dec_centroid - ref_centroid.
    Positive => decoded energy arrives later on average (smeared/delayed
    attack). Both ref and dec use the SAME window position and formula, so
    ref-vs-ref is 0.0 by construction.

    Returns list of (onset_sample, delta_ms). Skipped only when neither side
    has any measurable energy in the window (a true silence/decode failure).
    """
    ref_env = envelope(ref, sr, hop, win)
    dec_env = envelope(dec, sr, hop, win)
    fwd_frames = max(1, int(fwd_ms * 1e-3 * sr / hop))
    frame_dt_ms = hop / sr * 1000.0

    sorted_onsets = sorted(onsets)
    results = []
    for idx, n_o in enumerate(sorted_onsets):
        onset_frame = n_o // hop
        next_onset_frame = (sorted_onsets[idx + 1] // hop
                             if idx + 1 < len(sorted_onsets) else None)
        hi = onset_frame + fwd_frames
        if next_onset_frame is not None:
            hi = min(hi, next_onset_frame)
        hi = min(hi, len(ref_env), len(dec_env))
        if hi <= onset_frame:
            continue

        ref_e = ref_env[onset_frame:hi].astype(np.float64) ** 2
        dec_e = dec_env[onset_frame:hi].astype(np.float64) ** 2
        ref_sum = float(ref_e.sum())
        dec_sum = float(dec_e.sum())
        if ref_sum < 1e-12 or dec_sum < 1e-12:
            continue

        t_ms = np.arange(len(ref_e)) * frame_dt_ms
        ref_centroid = float((t_ms * ref_e).sum() / ref_sum)
        dec_centroid = float((t_ms * dec_e).sum() / dec_sum)
        results.append((n_o, dec_centroid - ref_centroid))

    return results


def attack_centroid_deltas(ref_mono, dec_mono, sr):
    """End-to-end: align dec to ref, detect onsets on the aligned ref, and
    return the per-onset attack-centroid-shift delta list (ms).

    This is the entry point phase4_transient.py uses per (ref, aac) pair.
    """
    lag = find_lag(ref_mono, dec_mono, sr)
    ref_a, dec_a = align_signals(ref_mono, dec_mono, lag)
    onsets = detect_onsets(ref_a, sr)
    if not onsets:
        return []
    return [d for _, d in compute_attack_centroid_shift(ref_a, dec_a, onsets, sr)]


# ── statistics (paired A/B: bootstrap CI + sign test) ──────────────────────

def bootstrap_ci(values, n_boot=5000, alpha=0.05, seed=0):
    """Bootstrap (percentile) CI for the mean of a paired-delta array."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n == 0:
        return (float('nan'), float('nan'))
    idx = rng.integers(0, n, size=(n_boot, n))
    means = arr[idx].mean(axis=1)
    lo = float(np.percentile(means, 100 * alpha / 2))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return lo, hi


def sign_test_p(deltas):
    """Two-sided sign test: P(observing this many negatives | p=0.5), exact binomial."""
    from math import comb
    nz = [d for d in deltas if abs(d) > 1e-9]
    n = len(nz)
    if n == 0:
        return 1.0, 0, 0
    neg = sum(1 for d in nz if d < 0)
    k = min(neg, n - neg)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail), neg, n


def ci_signtest_verdict(lo, hi, p, label_decrease, label_increase, alpha=0.05):
    """A/B verdict that requires the bootstrap CI AND the sign test to agree.

    Found necessary during attack-centroid-shift's Stage 1 validation
    (scripts/score_transient.py): a CI-only rule printed a confident
    directional verdict on pure noise (CI barely excluded zero while the
    sign test showed no consistent per-onset direction). Falling back to
    "inconclusive" whenever the two disagree is deliberately conservative.
    """
    if hi < 0 and p < alpha:
        return label_decrease
    if lo > 0 and p < alpha:
        return label_increase
    return 'inconclusive (CI/sign-test disagree or CI spans 0)'
