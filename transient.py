"""
 * FAAC Benchmark Suite — transient metrics
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

 * This module provides two independent transient diagnostics:
 * attack-centroid shift measures decoded attack timing in a forward window;
 * reference-locked pre-echo measures decoded energy before qualifying
 * reference onsets. They use separate calculations and report their own
 * coverage. Both are diagnostics and should be interpreted alongside the
 * benchmark's other quality measures, not as standalone quality scores.
"""

import numpy as np
import scipy.ndimage
import scipy.signal
from math import gcd

from utils import loudest_window_start


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
    m = min(len(ref_mono), len(dec_mono))
    n = min(m, sr * search_seconds)
    s = loudest_window_start(ref_mono[:m], n)
    r = ref_mono[s:s + n] / (np.std(ref_mono[s:s + n]) + 1e-10)
    d = dec_mono[s:s + n] / (np.std(dec_mono[s:s + n]) + 1e-10)
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
    return np.sqrt(np.maximum(smoothed[::hop], 0.0))


# ── reference-locked pre-echo measurement ───────────────────────────────────

PRE_ECHO_METRIC_VERSION = 1
PRE_ECHO_RATE = 48000
PRE_ECHO_SEED_HOP = 48
PRE_ECHO_SEED_WIN = 512
PRE_ECHO_MIN_SPACING = 1024
PRE_ECHO_PRE_START = 288   # -6 ms
PRE_ECHO_PRE_END = 48      # -1 ms
PRE_ECHO_POST_END = 576    # +12 ms


def has_current_pre_echo(row):
    """Whether a result row contains a current, versioned pre-echo metric."""
    if (not isinstance(row, dict)
            or 'pre_echo_error' in row
            or row.get('pre_echo_metric_version') != PRE_ECHO_METRIC_VERSION):
        return False
    coverage = row.get('pre_echo_coverage')
    counts = ('candidates', 'refined', 'eligible', 'scored')
    values = row.get('pre_echo_db')
    if not (isinstance(values, list) and isinstance(coverage, dict)
            and all(isinstance(coverage.get(key), int) and coverage[key] >= 0 for key in counts)
            and isinstance(row.get('pre_echo_lag_samples'), int)):
        return False
    if not (coverage['candidates'] >= coverage['refined'] >= coverage['eligible'] >= coverage['scored']):
        return False
    if coverage['scored'] != len(values):
        return False
    return all(isinstance(value, (int, float, np.integer, np.floating))
               and np.isfinite(value) for value in values)


def _audio_channels(audio, name):
    arr = np.asarray(audio, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    if arr.ndim != 2 or arr.shape[1] < 1:
        raise ValueError(f'{name} must be a 1D or 2D audio array')
    if not np.all(np.isfinite(arr)):
        raise ValueError(f'{name} must contain only finite samples')
    return arr


def _resample_audio(audio, sr):
    if sr == PRE_ECHO_RATE:
        return audio
    divisor = gcd(int(sr), PRE_ECHO_RATE)
    return scipy.signal.resample_poly(audio, PRE_ECHO_RATE // divisor,
                                      int(sr) // divisor, axis=0)


def _pre_echo_lag(ref, dec):
    """Bounded global lag from summed normalized channel correlations."""
    n = min(len(ref), len(dec))
    if n <= 1:
        return 0
    window = min(n, PRE_ECHO_RATE * 3)
    energy = np.sum(ref[:n] * ref[:n], axis=1)
    if n <= window:
        start = 0
    else:
        cs = np.concatenate(([0.0], np.cumsum(energy, dtype=np.float64)))
        start = int(np.argmax(cs[window:] - cs[:-window]))
    # Use the same loud reference segment in both recordings. Normalize each
    # channel independently so loud channels do not suppress quieter ones.
    length = min(window, len(ref) - start, len(dec) - start)
    if length <= 1:
        return 0
    total = None
    for ch in range(ref.shape[1]):
        r = ref[start:start + length, ch]
        d = dec[start:start + length, ch]
        r_scale = float(np.sqrt(np.mean(r * r)))
        d_scale = float(np.sqrt(np.mean(d * d)))
        if r_scale <= 1e-20 or d_scale <= 1e-20:
            continue
        corr = scipy.signal.correlate(r / r_scale, d / d_scale, mode='full', method='fft')
        total = corr if total is None else total + corr
    if total is None:
        return 0
    max_lag = int(round(PRE_ECHO_RATE * 0.250))
    center = length - 1
    lo, hi = max(0, center - max_lag), min(len(total), center + max_lag + 1)
    return int(np.argmax(total[lo:hi]) + lo - center)


def _refine_pre_echo_onset(filtered_ref, seed, power, slope=None):
    step = 24
    if slope is None:
        slope = power[2 * step:] - power[:-2 * step]
    lo, hi = max(seed - 576, 576), min(seed + 576, len(filtered_ref) - 576)
    if hi <= lo:
        return None
    rise = lo + int(np.argmax(slope[lo - step:hi - step]))
    base = float(np.median(power[rise - 288:rise - 96]))
    peak = float(np.max(power[rise:rise + 96]))
    if peak < 1e-12 or peak <= 4.0 * max(base, 1e-20):
        return None
    threshold = base + 0.1 * (peak - base)
    below = np.flatnonzero(power[rise - 288:rise + 1] <= threshold)
    if not len(below):
        return None
    return rise - 288 + int(below[-1]) + 1


def pre_echo_metrics(ref, dec, sr):
    """Measure reference-locked pre-echo NPER around refined transient onsets.

    Both inputs must contain the same number of channels and use the supplied
    native sample rate. The returned lag is in 48 kHz samples and follows
    ``align_signals`` semantics: positive means decoded audio starts ahead of
    the reference.
    """
    if not isinstance(sr, (int, np.integer)) or sr <= 0:
        raise ValueError('sr must be a positive integer')
    ref = _audio_channels(ref, 'ref')
    dec = _audio_channels(dec, 'dec')
    if ref.shape[1] != dec.shape[1]:
        raise ValueError('ref and dec must have equal channel counts')
    empty = {'pre_echo_db': [], 'pre_echo_coverage':
             {'candidates': 0, 'refined': 0, 'eligible': 0, 'scored': 0},
             'pre_echo_metric_version': PRE_ECHO_METRIC_VERSION,
             'pre_echo_lag_samples': 0}
    if len(ref) < 2 or len(dec) < 2 or not np.any(ref):
        return empty
    ref48 = _resample_audio(ref, int(sr))
    dec48 = _resample_audio(dec, int(sr))
    if len(ref48) < 2 or len(dec48) < 2:
        return empty
    if len(ref48) < PRE_ECHO_SEED_WIN:
        return empty
    lag = _pre_echo_lag(ref48, dec48)
    sos = scipy.signal.butter(4, 2000, btype='highpass', fs=PRE_ECHO_RATE, output='sos')
    ref_hp = scipy.signal.sosfilt(sos, ref48, axis=0)
    dec_hp = scipy.signal.sosfilt(sos, dec48, axis=0)
    ref_start = max(lag, 0)
    dec_start = max(-lag, 0)
    coverage = empty['pre_echo_coverage']
    values = []
    for ch in range(ref48.shape[1]):
        seeds = detect_onsets(ref48[:, ch], PRE_ECHO_RATE,
                              hop=PRE_ECHO_SEED_HOP, win_len=PRE_ECHO_SEED_WIN,
                              min_spacing=PRE_ECHO_MIN_SPACING)
        coverage['candidates'] += len(seeds)
        power = scipy.ndimage.uniform_filter1d(ref_hp[:, ch] ** 2, size=12)
        slope = power[48:] - power[:-48]
        refined = []
        for seed in seeds:
            onset = _refine_pre_echo_onset(ref_hp[:, ch], seed, power, slope)
            if onset is not None:
                refined.append(onset)
        refined.sort()
        unique = []
        for onset in refined:
            if not unique or onset - unique[-1] >= PRE_ECHO_MIN_SPACING:
                unique.append(onset)
        coverage['refined'] += len(unique)
        for onset in unique:
            pre_lo, pre_hi = onset - PRE_ECHO_PRE_START, onset - PRE_ECHO_PRE_END
            post_hi = onset + PRE_ECHO_POST_END
            if pre_lo < 0 or post_hi > len(ref_hp):
                continue
            ref_pre = float(np.mean(ref_hp[pre_lo:pre_hi, ch] ** 2))
            ref_post = float(np.mean(ref_hp[onset:post_hi, ch] ** 2))
            if ref_post < 1e-12 or 10.0 * np.log10(max(ref_pre, 1e-300) / ref_post) > -20.0:
                continue
            coverage['eligible'] += 1
            dec_lo = dec_start + pre_lo - ref_start
            dec_onset = dec_start + onset - ref_start
            dec_hi = dec_start + post_hi - ref_start
            if dec_lo < 0 or dec_hi > len(dec_hp):
                continue
            dec_post = float(np.mean(dec_hp[dec_onset:dec_hi, ch] ** 2))
            if dec_post < 1e-12:
                continue
            dec_pre = float(np.mean(dec_hp[dec_lo:dec_lo + (pre_hi - pre_lo), ch] ** 2))
            floor = ref_post * 1e-8
            values.append(float(10.0 * np.log10(max(dec_pre, floor) /
                                                   max(ref_pre, floor))))
            coverage['scored'] += 1
    return {'pre_echo_db': values, 'pre_echo_coverage': coverage,
            'pre_echo_metric_version': PRE_ECHO_METRIC_VERSION,
            'pre_echo_lag_samples': int(lag)}


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
    from scipy.stats import binomtest
    nz = [d for d in deltas if abs(d) > 1e-9]
    n = len(nz)
    if n == 0:
        return 1.0, 0, 0
    neg = sum(1 for d in nz if d < 0)
    p_val = float(binomtest(neg, n, 0.5).pvalue)
    return p_val, neg, n


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
