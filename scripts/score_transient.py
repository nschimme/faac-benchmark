"""
Transient-fidelity metrics (NPER, attack-smear) for FAAC TNS/block-switch
short-window evaluation.

NPER = 10*log10(dec_pre_energy/onset_peak) - 10*log10(ref_pre_energy/onset_peak)

Positive NPER means the decoded audio has more pre-echo than the reference (bad).
A decrease in NPER with increasing bitrate validates that the metric is working.
NPER looks *backward* from a detected onset (silence leaking pre-echo).

Attack-smear looks *forward* from the same onset: a log10-seconds delta in
how long the envelope takes to rise from 10% to 90% of its local peak (in the
spirit of the MPEG-7 "Log Attack Time" descriptor), decoded vs. reference.
Positive means the decoded attack is measurably slower to reach full energy
than the reference (smeared). See the module comment above
compute_attack_smear() for why this must be checked against NPER for
redundancy before it's trusted as a distinct signal.

Usage:
  score_transient.py REF.wav DEC.wav [--verbose]
  score_transient.py --validate FAAC_BIN [--bitrates 20,40,80]
  score_transient.py --sweep   FAAC_BIN REF.wav [--bitrates 20,40,80]
"""

import argparse
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
import scipy.ndimage
import scipy.signal
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import wav_conv


# ── algorithm constants ────────────────────────────────────────────────────────
ZIMT_RATE         = 48000  # Zimtohrli hard-assumes this rate and does not
                            # resample internally; HOP/WIN_LEN/MIN_ONSET_SPACING
                            # below are also calibrated in samples at this rate.
HOP               = 512    # STFT hop length (~10 ms at 48 kHz)
WIN_LEN           = 2048   # STFT window (4× hop → good freq resolution)
MIN_ONSET_SPACING = 1024   # minimum samples between detected onsets (~21 ms)
NOISE_FLOOR_DB    = -80.0  # log floor relative to onset peak energy

# Attack-smear (rise-time) constants. Deliberately a separate, much finer hop
# than HOP above: HOP/WIN_LEN were sized for onset *detection* (~10ms
# resolution is plenty to find where a transient starts), but measuring how
# fast the attack rises needs to resolve real attack rise times, which run
# ~1-3ms on transient material (castanets, glockenspiel) -- a hop anywhere
# near HOP would just measure its own quantization.
ENVELOPE_HOP      = 24     # ~0.5 ms at 48 kHz
ENVELOPE_WIN       = 24     # RMS window == hop (not 2x hop): a wider window
                            # box-smooths the very rise time being measured,
                            # imposing an artificial floor on sub-1ms rises.
                            # Crossing times are still sub-frame-interpolated
                            # (see _crossing_frames), so hop==win doesn't cost
                            # resolution the way naive frame-quantized
                            # crossings would.
ATTACK_BACK_MS    = 6.0    # look-back before onset when searching for the
                            # rise-time start crossing. Needs headroom beyond
                            # just "the onset sample": a slow attack's 10%
                            # crossing can itself sit several ms before the
                            # detected onset, and on top of that a residual
                            # post-find_lag alignment error shifts it further.
                            # 2.0ms (one true onset's worth) was found to clip
                            # the search window during alignment-jitter
                            # self-testing (cmd_validate_alignment) on a 2ms
                            # rise with a 2ms residual shift -- the combined
                            # ~2.2ms offset fell just outside a 2.0ms back
                            # window and produced a spurious crossing.
ATTACK_FWD_MS     = 12.0   # look-forward after onset for the local peak and
                            # end crossing. Real transient material
                            # (castanets, glockenspiel) rises in ~1-3ms; this
                            # leaves headroom without exceeding
                            # MIN_ONSET_SPACING (~21ms), since the window is
                            # also capped at the next detected onset (see
                            # compute_attack_smear) to avoid latching onto a
                            # neighboring transient's peak on dense material.
ATTACK_START_FRAC = 0.1    # rise-time start: envelope crosses this fraction
                            # of the local peak (10%, matching MPEG-7 LAT)
ATTACK_END_FRAC   = 0.9    # rise-time end: 90% of the local peak


# ── audio I/O ─────────────────────────────────────────────────────────────────

def load_mono(path):
    """Load audio, mix to mono, resample to 48kHz, return (float32 array, sample_rate).

    Every caller (onset detection, NPER windowing, zimtohrli_mos) assumes
    48kHz -- resampling once here, at the one place audio enters the
    pipeline, keeps that assumption true regardless of the source file's
    native rate instead of silently mis-scaling non-48kHz input downstream.
    """
    audio, sr = sf.read(path, dtype='float32', always_2d=True)
    mono = audio.mean(axis=1)
    if sr != ZIMT_RATE:
        g = math.gcd(ZIMT_RATE, sr)
        mono = scipy.signal.resample_poly(mono, ZIMT_RATE // g, sr // g)
        sr = ZIMT_RATE
    return mono.astype(np.float32), sr


def decode_aac(aac_path, wav_path, sr=48000, channels=1):
    """Decode AAC to WAV via FAAD2 (with ffmpeg fallback)."""
    if not wav_conv(aac_path, wav_path, rate=sr, channels=channels):
        raise RuntimeError(f'AAC decode failed for {aac_path}')


def encode_aac(faac_bin, wav_path, aac_path, bitrate, extra_args=None, env_extra=None):
    """Encode WAV to AAC/M4A via FAAC at the given total bitrate (kbps)."""
    cmd = [faac_bin, '-b', str(bitrate), '-o', aac_path, wav_path]
    if extra_args:
        cmd.extend(extra_args)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(cmd, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f'faac encode failed:\n{result.stderr.decode(errors="replace")}')
    return result.stderr.decode(errors='replace')


_TUNING_CHECKED = {}

def require_tuning_build(faac_bin):
    """Abort unless faac_bin was built with -Dtuning=true.

    A release build silently ignores every FAAC_* knob, so a sweep against one
    runs two identical arms and reports a confident "no difference". Probe once
    per binary by encoding a fraction of a second and looking for the banner
    libfaac prints from FaacTuningInit.
    """
    if faac_bin in _TUNING_CHECKED:
        return
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, 'probe.wav')
        subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=1000:duration=0.2', '-ar', '48000',
                        '-ac', '1', wav], capture_output=True, check=True)
        out = subprocess.run([faac_bin, '-b', '64', '-o', os.path.join(tmp, 'probe.m4a'), wav],
                             capture_output=True)
        banner = 'FAAC_TUNING build' in out.stderr.decode(errors='replace')
    if not banner:
        sys.exit(f'{faac_bin} is not a tuning build; FAAC_* knobs would be ignored.\n'
                 f'Rebuild with:  meson setup build-tune -Dtuning=true && ninja -C build-tune')
    _TUNING_CHECKED[faac_bin] = True


def encode_any(encoder, enc_bin, wav_path, out_path, bitrate, tns, force_long,
               env_extra=None):
    """Encode with faac or ffmpeg-native-AAC, TNS on/off, optionally forcing long blocks.

    force_long requires an encoder built with the debug knob:
      faac   → FAAC_FORCE_LONG=1 env (blockswitch.c)
      ffmpeg → FF_FORCE_LONG=1 env (patched psy_lame_window in aacpsy.c)
    """
    if encoder == 'faac':
        # Match the ffmpeg path: encode a mono 48k downmix so per-channel
        # bitrate is comparable (faac itself can't downmix).
        mono_wav = out_path + '.mono.wav'
        result = subprocess.run(['ffmpeg', '-y', '-i', wav_path, '-ac', '1',
                                 '-ar', '48000', mono_wav], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f'ffmpeg downmix failed:\n{result.stderr.decode(errors="replace")}')
        # Ask for TNS explicitly rather than relying on the library default,
        # which is currently off: an implicit on-arm makes both arms identical
        # and every A/B read exactly 0.00.
        #
        # Also force --object-type=lc: low-bitrate mono content otherwise
        # auto-selects HE-AAC v1 (per AGENTS.md), whose half-rate core skips
        # the TNS path entirely -- found via a byte-identical on/off encode
        # pair on sandman.16b48k.wav at 20k during Stage 1 validation, which
        # silently voided that clip's arm of a --tns-ab run. sweep_binary_ab.py
        # already forces this for the same reason.
        extra = ['--tns', '--object-type=lc'] if tns else ['--no-tns', '--object-type=lc']
        env = {'FAAC_FORCE_LONG': '1'} if force_long else {}
        if env_extra:
            env.update(env_extra)
        stderr = encode_aac(enc_bin, mono_wav, out_path, bitrate,
                            extra_args=extra, env_extra=env or None)
        os.unlink(mono_wav)
        return stderr
    else:  # ffmpeg
        env = dict(os.environ)
        if force_long:
            env['FF_FORCE_LONG'] = '1'
        if env_extra:
            env.update(env_extra)
        cmd = [enc_bin, '-y', '-i', wav_path, '-c:a', 'aac',
               '-aac_tns', '1' if tns else '0',
               '-b:a', f'{bitrate}k', '-ac', '1', '-ar', '48000', out_path]
        result = subprocess.run(cmd, capture_output=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(f'ffmpeg encode failed:\n{result.stderr.decode(errors="replace")}')


# ── zimtohrli metric (deterministic psychoacoustic MOS; sees temporal artifacts) ──

_ZIMT = None

def zimtohrli_mos(ref_aligned, dec_aligned):
    """Zimtohrli MOS of dec vs ref (48 kHz mono float, already aligned)."""
    global _ZIMT
    import zimtohrli
    if _ZIMT is None:
        _ZIMT = zimtohrli.Pyohrli()
    dist = _ZIMT.distance(np.ascontiguousarray(ref_aligned, dtype=np.float32),
                          np.ascontiguousarray(dec_aligned, dtype=np.float32))
    return float(zimtohrli.mos_from_zimtohrli(dist))


# ── alignment ─────────────────────────────────────────────────────────────────

def find_lag(ref_mono, dec_mono, sr, search_seconds=3):
    """Integer lag of dec relative to ref via cross-correlation.

    scipy.signal.correlate(a, b) peaks at index k → lag = k - (n-1).
    At lag d:  a[i] correlates best with b[i-d].

    FAAC + ffmpeg: ffmpeg strips the encoder priming samples (delay compensation),
    so the decoded audio starts ~2048 samples AHEAD of the reference content.
    This produces lag = +D (positive) where D ≈ encoder_delay_samples.
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
      dec started lag samples earlier in audio time → skip lag from start of ref.
        ref_aligned = ref[lag :]
        dec_aligned = dec[: N-lag]

    Negative lag (dec delayed):
      dec started |lag| samples later → skip |lag| from start of dec.
        ref_aligned = ref[: N+lag]
        dec_aligned = dec[-lag :]
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


# ── onset detection ───────────────────────────────────────────────────────────

def detect_onsets(audio, sr, hop=HOP, win_len=WIN_LEN,
                  min_spacing=MIN_ONSET_SPACING):
    """Return list of onset sample positions using spectral flux (no librosa).

    Flux[i] = sum of positive spectral differences between STFT frames i and i+1.
    Onset at flux peak frame i+1 → sample (i+1)*hop.
    """
    _, _, Zxx = scipy.signal.stft(
        audio, fs=sr, window='hann',
        nperseg=win_len, noverlap=win_len - hop,
        boundary='zeros')
    mag = np.abs(Zxx)  # (n_freq, n_frames)
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


# ── NPER measurement ──────────────────────────────────────────────────────────

def compute_nper(ref, dec, onsets, hop=HOP):
    """Compute NPER (dB) at each onset. Returns list of (sample, nper_db).

    For each onset at sample n_o:
      pre_window  = [n_o - hop, n_o)       — silence before transient
      onset_peak  = mean energy [n_o, n_o + hop)  — normalization anchor

    NPER = 10*log10(dec_pre/peak) − 10*log10(ref_pre/peak)
         = 10*log10(dec_pre_energy / ref_pre_energy)

    Both terms are floored at NOISE_FLOOR_DB dB below the onset peak to
    avoid log(0) when the pre-window is true silence.
    """
    floor_linear = 10 ** (NOISE_FLOOR_DB / 10.0)
    results = []
    n_sig = min(len(ref), len(dec))

    for n_o in onsets:
        if n_o < hop or n_o + hop > n_sig:
            continue

        ref_pre = ref[n_o - hop: n_o]
        dec_pre = dec[n_o - hop: n_o]
        onset_seg = ref[n_o: n_o + hop]

        onset_peak = float(np.mean(onset_seg ** 2))
        if onset_peak < 1e-12:
            continue  # degenerate: no real transient

        ref_pre_energy = float(np.mean(ref_pre ** 2))
        dec_pre_energy = float(np.mean(dec_pre ** 2))

        ref_norm = max(ref_pre_energy / onset_peak, floor_linear)
        dec_norm = max(dec_pre_energy / onset_peak, floor_linear)

        nper_db = 10.0 * np.log10(dec_norm) - 10.0 * np.log10(ref_norm)
        results.append((n_o, nper_db))

    return results


# ── attack-smear (rise-time) measurement ────────────────────────────────────────
#
# NPER (above) only looks *backward* from an onset -- energy leaking into the
# silence before a transient. It says nothing about whether the attack itself,
# once it starts, is preserved or blurred. This measures the forward side: how
# long the envelope takes to rise from ATTACK_START_FRAC to ATTACK_END_FRAC of
# its local peak, in the spirit of the MPEG-7 / ISO-IEC 15938-4 "Log Attack
# Time" descriptor (LAT is standardized for isolated instrument notes as a
# timbre descriptor, not per-onset inside continuous music, so this is
# deliberately described as "in the spirit of" rather than a claim of strict
# LAT compliance).
#
# IMPORTANT: this is only useful if it isn't just NPER restated in log form --
# a raised pre-onset floor (pre-echo) moves the 10% crossing earlier and
# inflates rise time, and an encoder that gates/mutes quiet content moves it
# the other way. Before trusting this metric, correlate its per-onset deltas
# against ΔNPER on the same onsets (see --corr-check) and confirm they are NOT
# just restating each other.

def envelope(audio, sr, hop=ENVELOPE_HOP, win=ENVELOPE_WIN):
    """Short-time RMS envelope, subsampled at `hop`. Frame i ↔ sample i*hop.

    Vectorized via a box filter on the squared signal rather than a Python
    loop over windows -- same result, but fast enough to run per onset
    without becoming the bottleneck of a bitrate sweep.
    """
    power = audio.astype(np.float64) ** 2
    smoothed = scipy.ndimage.uniform_filter1d(power, size=win, mode='nearest')
    return np.sqrt(smoothed[::hop])


def _interp_crossing(seg, start_idx, threshold):
    """First index >= start_idx where seg crosses `threshold`, linearly
    interpolated between the two bracketing samples (fractional frame index).

    Whole-frame crossing indices quantize rise time to multiples of
    ENVELOPE_HOP (0.5ms) -- on a real attack that's often only 2-4 frames
    wide, that quantization dominates over genuine signal, especially at low
    smoothing/smear amounts. Interpolating the crossing keeps sub-frame
    differences visible instead of rounding them away.
    """
    idxs = np.nonzero(seg[start_idx:] >= threshold)[0]
    if idxs.size == 0:
        return None
    i = start_idx + int(idxs[0])
    if i == 0:
        return None  # see _crossing_frames: a threshold already exceeded at
                     # the window's first sample is not a found crossing
    v0, v1 = seg[i - 1], seg[i]
    frac = 0.0 if v1 == v0 else min(max((threshold - v0) / (v1 - v0), 0.0), 1.0)
    return (i - 1) + frac


def _crossing_frames(env, lo, hi, thresh_lo, thresh_hi):
    """Sub-frame-interpolated crossing of thresh_lo, then thresh_hi after it,
    within [lo, hi) of `env`.

    Returns (frac_i_lo, frac_i_hi) as absolute fractional frame indices, or
    None if either threshold is never reached within the window, OR if
    thresh_lo is already exceeded at the window's very first sample.

    That last case matters on real material and was missed by every
    synthetic self-test: make_rise_wav's isolated transients sit on a -60dB
    bed, so thresh_lo is never pre-exceeded at window start -- but on
    continuous music, sustained or decaying content from a preceding note
    routinely IS already above thresh_lo at the window's back edge. Treating
    that as "crossing found at frame 0" silently measures the tail end of a
    rise that started before the window, which is a biased partial
    measurement, not the true rise time -- confirmed as the root cause of a
    real reference-vs-itself self-comparison reading nonzero (-0.53 log10s
    on velvet.16b48k.wav) during Stage 1 validation on real material.
    Rejecting the onset entirely here is deliberate: a truncated crossing is
    wrong, not merely approximate.
    """
    seg = env[lo:hi]
    if seg.size == 0:
        return None
    i_lo = _interp_crossing(seg, 0, thresh_lo)  # returns None if seg[0]
                                                # already >= thresh_lo
    if i_lo is None:
        return None
    i_hi = _interp_crossing(seg, int(np.ceil(i_lo)), thresh_hi)
    if i_hi is None:
        return None
    return lo + i_lo, lo + i_hi


def compute_attack_smear(ref, dec, onsets, sr, hop=ENVELOPE_HOP, win=ENVELOPE_WIN,
                         back_ms=ATTACK_BACK_MS, fwd_ms=ATTACK_FWD_MS,
                         start_frac=ATTACK_START_FRAC, end_frac=ATTACK_END_FRAC):
    """Rise-time delta (log10-seconds) at each onset. Returns list of (sample, delta).

    `ref`/`dec` are expected to already be aligned (the usual case: the
    caller's global find_lag/align_signals correction, via
    score_pair_detailed). An earlier version of this function tried to work
    around residual find_lag drift by locating dec's local peak independently
    of ref's, using a differently-shaped search procedure for each side. That
    was found, via a reference-vs-itself self-comparison on real music
    (velvet.16b48k.wav), to be a bug, not a robustness improvement: on
    identical input the two asymmetric procedures returned a nonzero
    "delta" (-0.53 log10s) purely from disagreeing with each other, not from
    any real signal. ref and dec now use the exact SAME window
    ([onset-back_ms, onset+fwd_ms), capped at the next onset), so identical
    input is guaranteed to produce delta == 0.0 by construction.

    For each onset:
      - local peak and both crossing thresholds are measured on the
        REFERENCE envelope only, in [onset, onset+fwd_ms) (capped at the
        next onset -- see below). That same absolute threshold is applied to
        dec -- so a decoded-side level change shows up as a *failure to
        cross* end_frac, not as a same-shape-different-scale rise time.
      - the search window is capped at the next detected onset, so a wide
        back/forward margin can't latch onto a neighboring transient's
        content on dense material (ATTACK_BACK_MS + ATTACK_FWD_MS can
        otherwise exceed MIN_ONSET_SPACING).
      - a threshold already exceeded at the window's very first sample is
        NOT treated as a found crossing (see _crossing_frames) -- on
        continuous music, sustained or decaying content from a preceding
        note routinely sits above thresh_lo at the window's back edge, and
        measuring from there would silently score a truncated partial rise
        rather than the true one. The onset is excluded instead.
      - rise time = t(end_frac crossing) − t(start_frac crossing), using
        sub-frame-interpolated crossing times (see _crossing_frames) so rise
        times shorter than one envelope hop are still distinguishable rather
        than all quantizing to the same floor value; floored at a small
        fraction of one frame to avoid log(0).
      - delta = log10(dec_rise) − log10(ref_rise); positive ⇒ decoded attack
        takes measurably longer to reach full energy (smeared).
      - onset is skipped (not included) if the reference peak is degenerate,
        or if the reference or decoded envelope never reaches end_frac of
        the reference peak within the window -- the latter is a real "does
        the transient survive at all" edge case, not something this
        rise-time metric is designed to score.
    """
    ref_env = envelope(ref, sr, hop, win)
    dec_env = envelope(dec, sr, hop, win)
    back_frames = max(1, int(back_ms * 1e-3 * sr / hop))
    fwd_frames = max(1, int(fwd_ms * 1e-3 * sr / hop))
    frame_dt = hop / sr
    rise_floor = frame_dt * 0.1  # avoid log(0); small relative to frame_dt
                                 # so interpolated sub-frame rises stay
                                 # distinguishable

    sorted_onsets = sorted(onsets)
    results = []
    for idx, n_o in enumerate(sorted_onsets):
        onset_frame = n_o // hop
        next_onset_frame = (sorted_onsets[idx + 1] // hop
                            if idx + 1 < len(sorted_onsets) else None)

        lo = max(0, onset_frame - back_frames)
        hi = onset_frame + fwd_frames
        if next_onset_frame is not None:
            hi = min(hi, next_onset_frame)
        hi = min(hi, len(ref_env), len(dec_env))
        if hi <= onset_frame:
            continue

        peak_window = ref_env[onset_frame:hi]
        if peak_window.size == 0:
            continue
        peak = float(peak_window.max())
        if peak < 1e-6:
            continue  # degenerate: no real transient

        thresh_lo = start_frac * peak
        thresh_hi = end_frac * peak

        ref_cross = _crossing_frames(ref_env, lo, hi, thresh_lo, thresh_hi)
        dec_cross = _crossing_frames(dec_env, lo, hi, thresh_lo, thresh_hi)
        if ref_cross is None or dec_cross is None:
            continue

        ref_rise = max((ref_cross[1] - ref_cross[0]) * frame_dt, rise_floor)
        dec_rise = max((dec_cross[1] - dec_cross[0]) * frame_dt, rise_floor)
        delta = float(np.log10(dec_rise) - np.log10(ref_rise))
        results.append((n_o, delta))

    return results


# ── attack-centroid-shift (alternative to the crossing-based estimator above) ──
#
# compute_attack_smear (above) requires locating an absolute threshold
# CROSSING, which needs a quiet gap before the onset to find a clean 10%
# start point. That precondition fails on dense percussive material: measured
# on sandman.16b48k.wav, real yield was 0/28-29 onsets at every bitrate, and
# widening/narrowing the back-window didn't help -- confirmed via a
# ref-vs-ref diagnostic showing the envelope routinely sits 3-10x above the
# 10%-of-peak threshold a full millisecond before the "onset", because the
# previous note's sustain/decay hasn't quieted down. That's a design-level
# mismatch, not a tunable parameter (see AGENTS.md-adjacent history in this
# file's git log for the full investigation).
#
# This estimator avoids the precondition entirely: instead of locating a
# landmark in each signal, it compares the two envelope CURVES directly over
# a fixed post-onset window via the shift in temporal energy centroid.
# A smeared attack puts more of its energy later in the window, so the
# centroid shifts later (positive delta_ms). Defined at every onset with any
# energy in the window at all -- no thresholds, no crossings, so yield should
# be near-total instead of near-zero.
#
# Known limitation, accepted as a deliberate trade-off for yield: because the
# centroid formula normalizes by each side's own energy sum, it is NOT
# sensitive to overall attenuation the way the ref-anchored-threshold design
# was -- a severely quietened but still similarly-*shaped* decoded attack
# will show a small delta even though it's audibly destroyed. This estimator
# answers "did the attack's energy arrive later," not "did the attack
# survive at all"; the latter question is better served by decode-error
# checks or the existing MOS metric, not duplicated here.
#
# STATUS: unvalidated. The NPER-redundancy check must be re-run from scratch
# for this estimator -- a post-onset energy-divergence measure could
# plausibly correlate with pre-echo more than the crossing version did, and
# nothing about compute_attack_smear's correlation result transfers.

def compute_attack_centroid_shift(ref, dec, onsets, sr, hop=ENVELOPE_HOP, win=ENVELOPE_WIN,
                                  fwd_ms=ATTACK_FWD_MS):
    """Post-onset energy-centroid shift (milliseconds) at each onset.

    For each onset, in the window [onset, onset+fwd_ms) (capped at the next
    onset, same reasoning as compute_attack_smear): compute each side's own
    energy-weighted centroid time (sum(t*e)/sum(e), e = envelope^2), then
    delta = dec_centroid - ref_centroid. Positive ⇒ decoded energy arrives
    later on average (smeared/delayed attack).

    Both ref and dec use the SAME window position and the same formula --
    unlike the earlier asymmetric-window bug in compute_attack_smear, there
    is no separate "locate the other side's peak" step here, so ref-vs-ref
    is 0.0 by construction with no special-casing required.

    Returns list of (onset_sample, delta_ms). Skipped only when neither side
    has any measurable energy in the window at all (a true silence/decode
    failure), not when a threshold isn't crossed -- this is the change that
    should fix the yield collapse.
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
            continue  # degenerate: no real energy in the window on one side

        t_ms = np.arange(len(ref_e)) * frame_dt_ms
        ref_centroid = float((t_ms * ref_e).sum() / ref_sum)
        dec_centroid = float((t_ms * dec_e).sum() / dec_sum)
        results.append((n_o, dec_centroid - ref_centroid))

    return results


# ── top-level scoring ─────────────────────────────────────────────────────────

def score_pair_detailed(ref_path, dec_path, verbose=False):
    """Score a ref/decoded WAV pair once, computing NPER, attack-smear, and
    attack-centroid-shift on the exact same onsets/alignment. Returns
    (onsets, nper_list, smear_list, centroid_list, ref_aligned, dec_aligned, sr).

    A single shared pass (rather than independent score_pair-style calls per
    metric) matters here: comparing ΔNPER against a transient metric's own
    delta per onset only means something if both were computed from the
    identical alignment and onset set, not two runs that might drift apart
    on edge cases.
    """
    ref, sr = load_mono(ref_path)
    dec, dsr = load_mono(dec_path)

    if sr != dsr:
        print(f'WARNING: sample-rate mismatch: ref={sr} dec={dsr}', file=sys.stderr)

    lag = find_lag(ref, dec, sr)
    ref_a, dec_a = align_signals(ref, dec, lag)

    if verbose:
        ms = lag / sr * 1000.0
        print(f'  lag = {lag} samples  ({ms:.1f} ms)', file=sys.stderr)
        # ffmpeg compensates encoder delay → decoded audio arrives early → positive lag
        expected = (64 <= abs(lag) <= 5000)
        if not expected:
            print(f'  WARNING: unusual lag={lag}; expected |lag| ≈ 1024–4096 for FAAC/ffmpeg',
                  file=sys.stderr)

    onsets = detect_onsets(ref_a, sr)
    if verbose:
        print(f'  detected {len(onsets)} onsets', file=sys.stderr)

    nper_list = compute_nper(ref_a, dec_a, onsets)
    smear_list = compute_attack_smear(ref_a, dec_a, onsets, sr)
    centroid_list = compute_attack_centroid_shift(ref_a, dec_a, onsets, sr)
    return onsets, nper_list, smear_list, centroid_list, ref_a, dec_a, sr


def score_pair(ref_path, dec_path, verbose=False):
    """Score a reference/decoded WAV pair. Returns
    (mean_nper_db, std_nper_db, nper_n, mean_smear, std_smear, smear_n,
     mean_centroid_ms, std_centroid_ms, centroid_n).

    Smear values are log10-seconds (dimensionless); centroid values are
    milliseconds. Each mean/std/n is (None, None, 0) if that metric found no
    usable onsets.
    """
    _, nper_list, smear_list, centroid_list, _, _, _ = score_pair_detailed(
        ref_path, dec_path, verbose=verbose)

    if nper_list:
        nvals = np.array([v for _, v in nper_list])
        nper_stats = (float(nvals.mean()), float(nvals.std()), len(nvals))
    else:
        nper_stats = (None, None, 0)

    if centroid_list:
        cvals = np.array([v for _, v in centroid_list])
        centroid_stats = (float(cvals.mean()), float(cvals.std()), len(cvals))
    else:
        centroid_stats = (None, None, 0)

    if smear_list:
        svals = np.array([v for _, v in smear_list])
        smear_stats = (float(svals.mean()), float(svals.std()), len(svals))
    else:
        smear_stats = (None, None, 0)

    return nper_stats + smear_stats + centroid_stats


# ── validate: synthetic click test ────────────────────────────────────────────

def make_click_wav(path, sr=48000, gap_sec=0.5, amplitude=0.9, n_clicks=4,
                   bed_db=-60.0, seed=0):
    """Write a mono WAV: impulses on a low-level noise bed.

    The bed is not cosmetic. NPER normalizes the decoded pre-onset energy by the
    reference's own pre-onset energy, and compute_nper floors both at
    NOISE_FLOOR_DB below the onset peak. Against a reference of exact digital
    silence the reference term is pinned to that floor forever, so as soon as the
    encoder gets good enough to put decoded pre-echo under the floor too, NPER
    reads exactly 0.00 by construction -- the metric saturates instead of
    resolving, and the monotonicity check fails on a working encoder.

    A bed ~60 dB down gives the reference term real energy (~30 dB of headroom
    over the floor here) so the ratio stays live across the useful bitrate range.
    """
    rng = np.random.default_rng(seed)
    n_gap = int(sr * gap_sec)
    n = n_gap * (n_clicks + 1)
    sig = rng.normal(0.0, 10 ** (bed_db / 20.0), n).astype(np.float32)
    for i in range(1, n_clicks + 1):
        sig[i * n_gap] = amplitude
    sf.write(path, sig, sr, subtype='PCM_16')


def make_rise_wav(path, sr, rise_ms, amplitude=0.9, bed_db=-60.0, hold_ms=10.0, seed=0):
    """Write a mono WAV: a noise bed with one transient whose envelope rises
    LINEARLY from 0 to `amplitude` over `rise_ms`, then holds at amplitude
    for `hold_ms` before returning to the bed.

    Deliberately NOT built by low-pass-filtering a single-sample impulse: a
    box/Gaussian filter on a true delta divides its peak by the filter width
    (a W-sample box filter turns a height-A impulse into a height-A/W
    plateau), which conflates "slower rise" with "much quieter attack" --
    the decoded copy can end up so attenuated it never crosses
    ATTACK_END_FRAC of the reference peak at all, and the onset gets silently
    skipped rather than scored. Constructing the rise directly gives an exact,
    peak-preserving ground truth: this signal's true rise time IS rise_ms by
    construction, nothing else about it changes.
    """
    rng = np.random.default_rng(seed)
    n = int(sr * 1.0)
    sig = rng.normal(0.0, 10 ** (bed_db / 20.0), n).astype(np.float64)
    onset = n // 2
    rise_samples = max(1, int(round(rise_ms * 1e-3 * sr)))
    hold_samples = max(1, int(round(hold_ms * 1e-3 * sr)))
    ramp = np.linspace(0.0, amplitude, rise_samples, endpoint=True)
    end = min(onset + rise_samples, n)
    sig[onset:end] += ramp[:end - onset]
    hold_end = min(end + hold_samples, n)
    if hold_end > end:
        sig[end:hold_end] += amplitude
    sf.write(path, sig.astype(np.float32), sr, subtype='PCM_16')


def cmd_validate_smear(sr=48000):
    """Ground-truth self-test for attack-smear: a sharp-attack reference vs.
    signals with a directly-parametrized, known rise time, with no encoder in
    the loop at all.

    This isolates the measurement itself from codec behavior -- unlike the
    NPER click-encode-sweep above, there's no faac/ffmpeg round-trip here, so
    a failure here means the rise-time math is wrong, not that some encoder
    setting confounded it. This is the check that has to pass before the
    metric is trusted on anything else.
    """
    print('\n=== attack-smear validation: synthetic known-rise-time sweep ===')
    print('Expected: Δ ~0 at the reference\'s own rise time, rising with slower rise times\n')

    # Reference rise time: one envelope hop -- as sharp an attack as this
    # measurement can meaningfully resolve at all (a rise faster than one hop
    # is indistinguishable from an instantaneous one at this sampling of the
    # envelope).
    ref_rise_ms = round(1000.0 * ENVELOPE_HOP / sr, 3)
    rise_levels_ms = [ref_rise_ms, 1.0, 2.0, 5.0, 10.0]
    deltas = {}
    with tempfile.TemporaryDirectory() as tmp:
        ref_wav = os.path.join(tmp, 'smear_ref.wav')
        make_rise_wav(ref_wav, sr, ref_rise_ms)
        ref_sig, _ = load_mono(ref_wav)
        # detect_onsets on ref alone -- these are all constructed at the same
        # sample position by design (same seed, same `onset` offset in
        # make_rise_wav), so no lag correction is needed. Deliberately NOT
        # routed through score_pair()/find_lag here: cross-correlating a
        # sharp reference spike against a slow multi-ms ramp is a shape
        # mismatch, not a timing mismatch, and find_lag's xcorr can (and, as
        # found while building this, does) lock onto a spurious non-zero lag
        # trying to best-fit the two different shapes -- exactly the kind of
        # confound this ground-truth check exists to keep out of the
        # rise-time math itself. Real alignment behavior is covered
        # separately by cmd_validate_alignment().
        onsets = detect_onsets(ref_sig, sr)

        for ms in rise_levels_ms:
            dec_wav = os.path.join(tmp, f'smear_{ms}ms.wav')
            make_rise_wav(dec_wav, sr, ms)
            dec_sig, _ = load_mono(dec_wav)
            smear_list = compute_attack_smear(ref_sig, dec_sig, onsets, sr)
            if not smear_list:
                print(f'  rise={ms:5.2f}ms: NO ONSETS DETECTED')
                continue
            svals = np.array([v for _, v in smear_list])
            smean, sstd, sn = float(svals.mean()), float(svals.std()), len(svals)
            print(f'  rise={ms:5.2f}ms:  Δ = {smean:+.3f} ± {sstd:.3f} log10s  ({sn} onsets)')
            deltas[ms] = smean

    print()
    ok = True
    if ref_rise_ms not in deltas:
        print('FAIL: reference-rise-time baseline produced no onsets')
        return False

    base = deltas[ref_rise_ms]
    # ~0.3 log10s ≈ a 2x rise-time difference on what should be a bit-identical
    # comparison (dec built with the same rise_ms as ref) -- anything near
    # that on the baseline case means the measurement itself is unstable,
    # not that it found real smear.
    if abs(base) > 0.3:
        print(f'FAIL: identical-rise-time baseline reads Δ={base:+.3f} (expected ~0) — '
              'measurement is not stable')
        ok = False

    ordered = sorted(deltas)
    vals = [deltas[m] for m in ordered]
    if not all(b >= a - 0.05 for a, b in zip(vals, vals[1:])):
        print('FAIL: Δ does not rise (roughly) monotonically with rise time — '
              'measurement is not tracking rise-time differences')
        ok = False
    elif vals[-1] - vals[0] < 0.3:
        print(f'FAIL: Δ barely moves ({vals[0]:+.3f} → {vals[-1]:+.3f}) across rise-time '
              'levels — insufficient sensitivity')
        ok = False

    if ok:
        print(f'PASS: Δ {vals[0]:+.3f} (sharpest) → {vals[-1]:+.3f} (slowest rise) — '
              'measurement is working')
    return ok


def cmd_validate_alignment(sr=48000):
    """Alignment-sensitivity self-test: ref and dec built with the IDENTICAL
    rise (true Δ = 0), then dec alone is shifted by a small fractional
    amount and scored directly against ref's onsets -- bypassing find_lag
    entirely, the same way cmd_validate_smear bypasses it.

    This is deliberately NOT routed through score_pair()/find_lag: an
    earlier version of this test shifted a single signal into both ref and
    dec together and diffed the results through score_pair, which produced
    Δ=0.000 at every shift -- a null result, not evidence of stability,
    because find_lag's integer-sample correction (plus the fact that both
    sides carried the identical residual) canceled the very thing being
    tested. That version also surfaced a separate, real finding worth
    recording here: find_lag's cross-correlation tracks an attack's shape
    centroid, not strictly its onset, so on real material (where a slower
    decoded attack has a different shape than the reference, not just a time
    offset) find_lag's estimated lag will itself shift by an amount
    correlated with the smear being measured. That risk isn't something a
    synthetic self-test can rule out; --sweep's per-bitrate correlation
    check is the closest available real-material signal, but this is a
    known residual risk to call out explicitly in Stage 1b's writeup.

    What THIS test actually isolates: given perfect (bypassed) alignment
    except for a small sub-frame residual on the decoded side only, does the
    crossing/rise-time math stay stable? NPER tolerates ~1ms of residual
    alignment error because it averages energy over a 512-sample (~10.7ms)
    window; this metric measures crossings at ENVELOPE_HOP (~0.5ms)
    resolution, so it needs to be checked separately.
    """
    print('\n=== attack-smear validation: alignment-jitter sensitivity ===')
    print('Expected: Δ stays near 0 across small (sub-2ms) shifts of dec alone\n')

    same_rise_ms = 2.0  # well above the ENVELOPE_HOP resolution floor
                        # (see cmd_validate_smear's ref_rise_ms note), so any
                        # movement here is jitter, not floor artifacts
    shifts_ms = [-2.0, -1.0, -0.3, 0.0, 0.3, 1.0, 2.0]
    deltas = {}
    with tempfile.TemporaryDirectory() as tmp:
        ref_wav = os.path.join(tmp, 'align_ref.wav')
        make_rise_wav(ref_wav, sr, same_rise_ms)
        ref_sig, _ = load_mono(ref_wav)
        onsets = detect_onsets(ref_sig, sr)

        dec_wav = os.path.join(tmp, 'align_dec.wav')
        make_rise_wav(dec_wav, sr, same_rise_ms)
        dec_sig, _ = load_mono(dec_wav)

        for ms in shifts_ms:
            shift_samples = ms * 1e-3 * sr
            shifted = scipy.ndimage.shift(
                dec_sig.astype(np.float64), shift_samples, order=1,
                mode='nearest').astype(np.float32)
            smear_list = compute_attack_smear(ref_sig, shifted, onsets, sr)
            if not smear_list:
                print(f'  shift={ms:+.1f}ms: NO ONSETS DETECTED')
                continue
            svals = np.array([v for _, v in smear_list])
            smean, sstd, sn = float(svals.mean()), float(svals.std()), len(svals)
            print(f'  shift={ms:+.1f}ms:  Δ = {smean:+.3f} ± {sstd:.3f} log10s  ({sn} onsets)')
            deltas[ms] = smean

    print()
    if len(deltas) < 2:
        print('FAIL: not enough shifted variants produced onsets to judge stability')
        return False

    vals = np.array(list(deltas.values()))
    spread = float(vals.max() - vals.min())
    print(f'  spread across shifts: {spread:.3f} log10s')
    # 0.3 log10s ≈ a 2x rise-time swing from sub-2ms jitter alone -- if the
    # metric moves that much just from jitter, real encoder-induced deltas
    # will be unreadable against this noise floor.
    if spread > 0.3:
        print('FAIL: Δ swings substantially with alignment jitter alone — '
              'per-onset local lag refinement is needed before trusting this metric')
        return False
    print('PASS: Δ is stable across realistic alignment jitter')
    return True


def cmd_validate_production(sr=48000):
    """End-to-end self-test: the same known-rise-time levels as
    cmd_validate_smear, but routed through the real score_pair() path
    (find_lag + align_signals included) instead of calling
    compute_attack_smear directly.

    cmd_validate_smear proves the rise-time math is right in isolation;
    this proves the full pipeline doesn't corrupt that answer. The two are
    not redundant: an earlier version of compute_attack_smear passed the
    isolated test but returned a non-monotonic, sign-flipped result through
    this exact path (production find_lag drifted up to ~9.5ms toward the
    most-smeared decoded attack's shape centroid). That's the failure mode
    this check exists to catch before it reaches --sweep/--tns-ab/--env-ab,
    which all go through this same path.
    """
    print('\n=== attack-smear validation: production-path (find_lag) known-rise-time sweep ===')
    print('Expected: same monotonic Δ pattern as the isolated synthetic test above\n')

    ref_rise_ms = round(1000.0 * ENVELOPE_HOP / sr, 3)
    rise_levels_ms = [ref_rise_ms, 1.0, 2.0, 5.0, 10.0]
    deltas = {}
    with tempfile.TemporaryDirectory() as tmp:
        ref_wav = os.path.join(tmp, 'prod_ref.wav')
        make_rise_wav(ref_wav, sr, ref_rise_ms)

        for ms in rise_levels_ms:
            dec_wav = os.path.join(tmp, f'prod_{ms}ms.wav')
            make_rise_wav(dec_wav, sr, ms)
            _, _, _, smean, sstd, sn, _, _, _ = score_pair(ref_wav, dec_wav, verbose=True)
            if smean is None:
                print(f'  rise={ms:5.2f}ms: NO ONSETS DETECTED')
                continue
            print(f'  rise={ms:5.2f}ms:  Δ = {smean:+.3f} ± {sstd:.3f} log10s  ({sn} onsets)')
            deltas[ms] = smean

    print()
    ok = True
    if ref_rise_ms not in deltas:
        print('FAIL: reference-rise-time baseline produced no onsets')
        return False

    base = deltas[ref_rise_ms]
    if abs(base) > 0.3:
        print(f'FAIL: identical-rise-time baseline reads Δ={base:+.3f} (expected ~0) — '
              'production path is not stable')
        ok = False

    ordered = sorted(deltas)
    vals = [deltas[m] for m in ordered]
    if not all(b >= a - 0.05 for a, b in zip(vals, vals[1:])):
        print('FAIL: Δ does not rise (roughly) monotonically through the production path — '
              'find_lag is corrupting the measurement (see compute_attack_smear docstring)')
        ok = False
    elif vals[-1] - vals[0] < 0.3:
        print(f'FAIL: Δ barely moves ({vals[0]:+.3f} → {vals[-1]:+.3f}) through the '
              'production path — insufficient sensitivity')
        ok = False

    if ok:
        print(f'PASS: Δ {vals[0]:+.3f} (sharpest) → {vals[-1]:+.3f} (slowest rise) through '
              'the production path — end-to-end pipeline is working')
    return ok


def cmd_validate_centroid(sr=48000):
    """Ground-truth self-tests for attack-centroid-shift: no encoder, no
    synthetic signal -- a real transient-heavy corpus clip against itself.

    Unlike compute_attack_smear's self-tests, this needs no faac encode/decode
    round-trip at all: both properties below are about the estimator's own
    math, not about codec behavior, so they're checked directly on real
    material where the crossing-based estimator was found to fail:

    1. Ref-vs-ref must read exactly 0.0 at every onset (translation of the
       exact check that caught compute_attack_smear's asymmetric-window bug
       -- if this ever drifts from 0.0 again on real content, it's the same
       class of bug).
    2. Yield must be near-total (not just non-zero) -- the crossing
       estimator's fatal flaw on this exact clip was correct math with 0-2/18
       onsets surviving; a correct-but-unusable estimator would pass check 1
       and silently fail this one.
    """
    print('\n=== attack-centroid-shift validation: real-clip ref-vs-ref ===')
    print('Expected: delta == 0.0 at every onset, near-total yield\n')

    clip = 'data/external/audio/35_SQAM_glockenspiel_cut.16b48k.wav'
    if not os.path.exists(clip):
        print(f'SKIP: {clip} not found (run from repo root)')
        return True  # not a measurement failure -- don't block --validate
                     # on a missing corpus file

    ref, csr = load_mono(clip)
    onsets = detect_onsets(ref, csr)
    centroid_list = compute_attack_centroid_shift(ref, ref, onsets, csr)

    n_detected = len(onsets)
    n_scored = len(centroid_list)
    yield_pct = 100.0 * n_scored / n_detected if n_detected else 0.0
    max_abs = max((abs(v) for _, v in centroid_list), default=float('nan'))
    print(f'  {os.path.basename(clip)}: {n_scored}/{n_detected} onsets scored '
          f'({yield_pct:.0f}% yield), max|Δ| = {max_abs:.6f} ms')

    ok = True
    if max_abs != 0.0:
        print(f'FAIL: ref-vs-ref reads nonzero (max|Δ|={max_abs}) — the estimator is '
              'not symmetric between ref and dec (see compute_attack_centroid_shift docstring)')
        ok = False
    # 80%, not 100%: real corpus content can legitimately have a handful of
    # degenerate onsets (near-silent windows); this checks for the crossing
    # estimator's *class* of failure (near-total dropout), not zero tolerance.
    if yield_pct < 80.0:
        print(f'FAIL: yield only {yield_pct:.0f}% — this is the failure mode that killed '
              'the crossing-based estimator; investigate before trusting this metric')
        ok = False

    if ok:
        print(f'PASS: ref-vs-ref exact, {yield_pct:.0f}% yield on real transient-heavy material')
    return ok


def cmd_validate(faac_bin, bitrates, sr=48000):
    print('=== NPER validation: synthetic click sweep ===')
    print('Expected: large positive NPER at the lowest bitrate, falling to ~0\n')

    with tempfile.TemporaryDirectory() as tmp:
        ref_wav = os.path.join(tmp, 'click_ref.wav')
        make_click_wav(ref_wav, sr=sr)

        nper_by_br = {}
        ok = True
        for br in bitrates:
            aac_path = os.path.join(tmp, f'click_{br}k.aac')
            dec_wav = os.path.join(tmp, f'click_{br}k_dec.wav')
            try:
                # Encode without TNS so pre-echo is maximally visible
                encode_aac(faac_bin, ref_wav, aac_path, br, extra_args=['--no-tns'])
                decode_aac(aac_path, dec_wav, sr=sr, channels=1)
            except RuntimeError as e:
                print(f'  {br}k: ENCODE/DECODE FAILED — {e}')
                ok = False
                continue

            mean, std, n, smean, sstd, sn, cmean, cstd, cn = score_pair(
                ref_wav, dec_wav, verbose=True)
            if mean is None:
                print(f'  {br}k: NO ONSETS DETECTED — onset detection failed')
                ok = False
                continue

            smear_str = (f'  smear = {smean:+.3f} ± {sstd:.3f} log10s ({sn} onsets)'
                        if smean is not None else '  smear = n/a')
            centroid_str = (f'  centroid = {cmean:+.3f} ± {cstd:.3f} ms ({cn} onsets)'
                           if cmean is not None else '  centroid = n/a')
            print(f'  {br}k:  NPER = {mean:+.1f} ± {std:.1f} dB  ({n} onsets){smear_str}{centroid_str}')
            nper_by_br[br] = mean

    print()
    # Deliberately not a monotonicity check. NPER is an energy ratio against the
    # reference's pre-onset window, so an encoder that discards quiet content
    # scores as having *less* pre-echo: mid-bitrate points routinely dip below
    # high-bitrate ones (here, and on real material -- castanets read -1.6 dB at
    # 20k vs +0.1 at 80k). That confound is intrinsic to the metric, not a fault
    # in the chain, and cancels only in the paired same-bitrate A/B the harness
    # actually reports. What a working chain must show is gross sensitivity:
    # heavy pre-echo at the lowest rate, gone by the highest.
    ordered = sorted(nper_by_br)
    if len(ordered) >= 2:
        lo, hi = nper_by_br[ordered[0]], nper_by_br[ordered[-1]]
        if lo < 6.0:
            print(f'FAIL: only {lo:+.1f} dB NPER at {ordered[0]}k — pre-echo not '
                  'detected where it must be; check alignment and onset detection')
            ok = False
        elif lo - hi < 6.0:
            print(f'FAIL: NPER barely moves ({lo:+.1f} → {hi:+.1f} dB) across '
                  f'{ordered[0]}k→{ordered[-1]}k — metric is not tracking pre-echo')
            ok = False
    else:
        print('FAIL: need at least two bitrates to validate')
        ok = False

    if ok:
        print(f'PASS: NPER {nper_by_br[ordered[0]]:+.1f} dB at {ordered[0]}k → '
              f'{nper_by_br[ordered[-1]]:+.1f} dB at {ordered[-1]}k — metric is working')

    smear_ok = cmd_validate_smear(sr=sr)
    align_ok = cmd_validate_alignment(sr=sr)
    prod_ok = cmd_validate_production(sr=sr)
    centroid_ok = cmd_validate_centroid(sr=sr)

    if not (ok and smear_ok and align_ok and prod_ok and centroid_ok):
        sys.exit(1)


# ── sweep: score a real clip at multiple bitrates ─────────────────────────────

def cmd_sweep(faac_bin, ref_wav, bitrates, no_tns=False):
    print(f'=== NPER + attack-smear sweep: {os.path.basename(ref_wav)} ===')
    extra = ['--no-tns'] if no_tns else ['--tns']

    ref, sr = load_mono(ref_wav)
    channels = 1  # We score on mono mix regardless of source

    # Pooled per-onset values across the whole sweep, to check whether
    # attack-smear is just restating NPER (see the correlation warning
    # below) -- the actual go/no-go question for trusting this metric.
    # Tracked per-bitrate too: pooling across bitrates conflates real
    # within-bitrate covariation with the across-bitrate trend both metrics
    # share for free (both worsen at low bitrate, improve at high bitrate),
    # which inflates pooled |r| even when the metrics aren't actually
    # redundant at a fixed bitrate -- the comparison that matters for CI,
    # where deltas are always computed within one scenario/bitrate.
    # Each transient metric gets its OWN pooled-NPER list, built in lockstep
    # with that metric's pooled values from the same onset intersection --
    # smear and centroid don't survive the same onsets (smear's yield is much
    # lower), so a single shared pooled_nper list would silently pair NPER
    # values from one onset set against a different metric's values from a
    # different onset set the moment their surviving counts happened to
    # match in length.
    pooled = {
        'attack-smear': {'nper': [], 'metric': [], 'per_br_nper': {}, 'per_br_metric': {}},
        'attack-centroid-shift': {'nper': [], 'metric': [], 'per_br_nper': {}, 'per_br_metric': {}},
    }

    with tempfile.TemporaryDirectory() as tmp:
        for br in bitrates:
            aac_path = os.path.join(tmp, f'enc_{br}k.aac')
            dec_wav = os.path.join(tmp, f'dec_{br}k.wav')
            try:
                encode_aac(faac_bin, ref_wav, aac_path, br, extra_args=extra)
                decode_aac(aac_path, dec_wav, sr=sr, channels=channels)
            except RuntimeError as e:
                print(f'  {br}k: FAILED — {e}')
                continue

            onsets, nper_list, smear_list, centroid_list, _, _, _ = score_pair_detailed(
                ref_wav, dec_wav)
            if not nper_list and not smear_list and not centroid_list:
                print(f'  {br}k:  no onsets detected')
                continue

            # Survival rate: compute_attack_smear silently skips onsets on
            # several conditions (window bounds, degenerate peak, no
            # threshold crossing on either side). If this fraction differs
            # between arms of a paired A/B, the comparison is scoring
            # different onset sets on each side, which breaks the pairing
            # --tns-ab/--env-ab and the CI report's pooling both rest on --
            # surfaced here so it's visible on every sweep, not just when
            # something already looks wrong. (compute_attack_centroid_shift
            # is designed for near-total yield, so a low fraction there is a
            # much stronger signal than the same fraction for smear.)
            n_detected = len(onsets)
            if n_detected and len(smear_list) < n_detected:
                print(f'  {br}k:  ({len(smear_list)}/{n_detected} onsets produced a smear value)')
            if n_detected and len(centroid_list) < n_detected:
                print(f'  {br}k:  ({len(centroid_list)}/{n_detected} onsets produced a centroid value)')

            nper_by_onset = dict(nper_list)
            for name, values_by_onset in (('attack-smear', dict(smear_list)),
                                          ('attack-centroid-shift', dict(centroid_list))):
                p = pooled[name]
                br_n, br_m = [], []
                for o in sorted(set(nper_by_onset) & set(values_by_onset)):
                    p['nper'].append(nper_by_onset[o])
                    p['metric'].append(values_by_onset[o])
                    br_n.append(nper_by_onset[o])
                    br_m.append(values_by_onset[o])
                p['per_br_nper'][br] = br_n
                p['per_br_metric'][br] = br_m

            nper_str = ('n/a' if not nper_list else
                       f'{np.mean([v for _, v in nper_list]):+.1f} ± '
                       f'{np.std([v for _, v in nper_list]):.1f} dB ({len(nper_list)} onsets)')
            smear_str = ('n/a' if not smear_list else
                        f'{np.mean([v for _, v in smear_list]):+.3f} ± '
                        f'{np.std([v for _, v in smear_list]):.3f} log10s ({len(smear_list)} onsets)')
            centroid_str = ('n/a' if not centroid_list else
                           f'{np.mean([v for _, v in centroid_list]):+.3f} ± '
                           f'{np.std([v for _, v in centroid_list]):.3f} ms ({len(centroid_list)} onsets)')
            print(f'  {br}k:  NPER = {nper_str}   smear = {smear_str}   centroid = {centroid_str}')

    for name in ('attack-smear', 'attack-centroid-shift'):
        p = pooled[name]
        print()
        per_br_r = {}
        for br in bitrates:
            n_vals, m_vals = p['per_br_nper'].get(br, []), p['per_br_metric'].get(br, [])
            if len(n_vals) >= 3 and np.std(n_vals) > 0 and np.std(m_vals) > 0:
                per_br_r[br] = float(np.corrcoef(n_vals, m_vals)[0, 1])

        if per_br_r:
            r_str = '  '.join(f'{br}k: r={r:+.2f}(n={len(p["per_br_nper"][br])})'
                              for br, r in per_br_r.items())
            print(f'  ΔNPER vs {name} correlation, per bitrate: {r_str}')
        else:
            print(f'  (too few onsets per bitrate to compute a per-bitrate '
                  f'ΔNPER-vs-{name} correlation)')

        if len(p['nper']) >= 3:
            r = float(np.corrcoef(p['nper'], p['metric'])[0, 1])
            print(f'  ΔNPER vs {name} correlation, pooled across {len(p["nper"])} '
                  f'onsets (all bitrates): r = {r:+.3f}')
            if abs(r) > 0.7 and (not per_br_r or max(abs(v) for v in per_br_r.values()) > 0.5):
                print(f'  WARNING: high correlation with NPER -- {name} may not be adding '
                      'information beyond pre-echo; do not wire this into CI as-is')
            elif abs(r) > 0.7:
                print('  NOTE: pooled r is high but per-bitrate r is not -- this is likely the '
                      'shared across-bitrate trend (both metrics worsen at low bitrate), not '
                      'redundancy at a fixed bitrate. Judge by the per-bitrate numbers above.')
        else:
            print(f'  (only {len(p["nper"])} shared onsets — too few to judge the NPER '
                  f'correlation for {name} from this sweep alone)')


# ── paired TNS on/off A/B (deterministic proof of TNS value) ──────────────────

def nper_at_onsets(ref_raw, dec_raw, sr, onsets_raw):
    """NPER per raw-onset for one decode. Returns dict {onset_raw_sample: nper_db}.

    Aligns dec to ref, translates the reference-frame onset list into the aligned
    coordinate system by the measured lag, and scores at those positions. Keying by
    the raw onset sample lets us pair on/off decodes onset-for-onset even if their
    encoder-delay lags differ slightly.
    """
    lag = find_lag(ref_raw, dec_raw, sr)
    ref_a, dec_a = align_signals(ref_raw, dec_raw, lag)
    # aligned index of a raw-ref onset: positive lag trims lag from front of ref
    shift = lag if lag > 0 else 0
    aligned_onsets = [o - shift for o in onsets_raw]
    scored = compute_nper(ref_a, dec_a, aligned_onsets)
    # map aligned-onset sample back to the raw onset it came from
    out = {}
    for (aligned_sample, nper_db) in scored:
        out[aligned_sample + shift] = nper_db
    return out


def attack_smear_at_onsets(ref_raw, dec_raw, sr, onsets_raw):
    """Attack-smear per raw-onset for one decode. Mirrors nper_at_onsets above
    (same alignment/shift bookkeeping), returns {onset_raw_sample: delta}."""
    lag = find_lag(ref_raw, dec_raw, sr)
    ref_a, dec_a = align_signals(ref_raw, dec_raw, lag)
    shift = lag if lag > 0 else 0
    aligned_onsets = [o - shift for o in onsets_raw]
    scored = compute_attack_smear(ref_a, dec_a, aligned_onsets, sr)
    out = {}
    for (aligned_sample, delta) in scored:
        out[aligned_sample + shift] = delta
    return out


def attack_centroid_at_onsets(ref_raw, dec_raw, sr, onsets_raw):
    """Attack-centroid-shift per raw-onset for one decode. Mirrors
    nper_at_onsets/attack_smear_at_onsets above, returns
    {onset_raw_sample: delta_ms}."""
    lag = find_lag(ref_raw, dec_raw, sr)
    ref_a, dec_a = align_signals(ref_raw, dec_raw, lag)
    shift = lag if lag > 0 else 0
    aligned_onsets = [o - shift for o in onsets_raw]
    scored = compute_attack_centroid_shift(ref_a, dec_a, aligned_onsets, sr)
    out = {}
    for (aligned_sample, delta) in scored:
        out[aligned_sample + shift] = delta
    return out


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
    """Two-sided sign test: P(observing this many negatives | p=0.5), exact binomial.

    Negative ΔNPER = TNS reduced pre-echo (good). Zeros (short-block onsets) excluded.
    """
    from scipy.stats import binomtest
    nz = [d for d in deltas if abs(d) > 1e-9]
    n = len(nz)
    if n == 0:
        return 1.0, 0, 0
    neg = sum(1 for d in nz if d < 0)
    p_val = float(binomtest(neg, n, 0.5).pvalue)
    return p_val, neg, n


def ci_signtest_verdict(lo, hi, p, label_decrease, label_increase, alpha=0.05):
    """A/B verdict that requires the bootstrap CI AND the sign test to agree,
    not just the CI alone.

    Found necessary during attack-centroid-shift's Stage 1 validation: a
    ffmpeg TNS on/off run on sandman.16b48k.wav at 20k gave 95% CI
    [+0.005, +0.243] (barely excludes zero) alongside sign-test p=0.458 (not
    remotely significant) -- a mean dragged by a few onsets with no
    consistent per-onset direction. A CI-only rule would have printed a
    confident directional verdict on that noise. Falling back to
    "inconclusive" whenever the two disagree is deliberately conservative:
    it's the same discipline that keeps NPER's own click-sweep validation
    from over-reading a monotonicity blip (see cmd_validate's comment on
    "gross sensitivity, not strict monotonicity").
    """
    if hi < 0 and p < alpha:
        return label_decrease
    if lo > 0 and p < alpha:
        return label_increase
    return 'inconclusive (CI/sign-test disagree or CI spans 0)'


def tns_ab_one(encoder, enc_bin, ref_wav, bitrates, tmp, force_long=False,
               metric='nper'):
    """A/B one clip. Returns {bitrate: {'nper': [ΔNPER per onset], 'zim': Δzim_MOS}}.

    ΔNPER = NPER_on − NPER_off (negative ⇒ TNS reduced pre-echo).
    Δzim  = zimtohrli MOS_on − MOS_off (positive ⇒ TNS improved quality).
    """
    ref, sr = load_mono(ref_wav)
    onsets = detect_onsets(ref, sr)
    result = {}
    ext = 'm4a'
    for br in bitrates:
        entry = {'nper': [], 'zim': None, 'smear': [], 'centroid': []}
        try:
            on_enc = os.path.join(tmp, f'on_{br}.{ext}');  on_wav = os.path.join(tmp, f'on_{br}.wav')
            off_enc = os.path.join(tmp, f'off_{br}.{ext}'); off_wav = os.path.join(tmp, f'off_{br}.wav')
            encode_any(encoder, enc_bin, ref_wav, on_enc, br, tns=True, force_long=force_long)
            encode_any(encoder, enc_bin, ref_wav, off_enc, br, tns=False, force_long=force_long)
            decode_aac(on_enc, on_wav, sr=sr, channels=1)
            decode_aac(off_enc, off_wav, sr=sr, channels=1)
        except RuntimeError as e:
            print(f'  {br}k: FAILED — {e}')
            result[br] = entry
            continue
        on_dec, _ = load_mono(on_wav)
        off_dec, _ = load_mono(off_wav)
        if metric in ('nper', 'both'):
            nper_on = nper_at_onsets(ref, on_dec, sr, onsets)
            nper_off = nper_at_onsets(ref, off_dec, sr, onsets)
            entry['nper'] = [nper_on[o] - nper_off[o] for o in nper_on if o in nper_off]
        if metric == 'attack_smear':
            smear_on = attack_smear_at_onsets(ref, on_dec, sr, onsets)
            smear_off = attack_smear_at_onsets(ref, off_dec, sr, onsets)
            entry['smear'] = [smear_on[o] - smear_off[o] for o in smear_on if o in smear_off]
            # If TNS on/off produce different onset SURVIVAL sets (not just
            # different values), the paired comparison above is silently
            # comparing different onsets on each side of the A/B -- worth
            # flagging explicitly since it would otherwise look like a
            # smaller-than-expected n with no visible cause.
            if len(onsets) and (len(smear_on) != len(smear_off) or
                                len(entry['smear']) < min(len(smear_on), len(smear_off))):
                print(f'    ({len(onsets)} onsets: on={len(smear_on)} off={len(smear_off)} '
                      f'paired={len(entry["smear"])} survived)')
        if metric == 'attack_centroid':
            centroid_on = attack_centroid_at_onsets(ref, on_dec, sr, onsets)
            centroid_off = attack_centroid_at_onsets(ref, off_dec, sr, onsets)
            entry['centroid'] = [centroid_on[o] - centroid_off[o] for o in centroid_on
                                 if o in centroid_off]
        if metric in ('zimtohrli', 'both'):
            zs = []
            for dec in (on_dec, off_dec):
                lag = find_lag(ref, dec, sr)
                ra, da = align_signals(ref, dec, lag)
                zs.append(zimtohrli_mos(ra, da))
            entry['zim'] = zs[0] - zs[1]
        result[br] = entry
    return result


def cmd_tns_ab(enc_bin, ref_wavs, bitrates, encoder='faac', force_long=False,
               metric='nper'):
    if encoder == 'faac' and force_long:
        require_tuning_build(enc_bin)
    mode = f'encoder={encoder}, force_long={force_long}, metric={metric}'
    print(f'=== TNS A/B proof ({mode}) ===')
    if metric in ('nper', 'both'):
        print('    ΔNPER = NPER_on − NPER_off; negative ⇒ TNS reduced pre-echo (value).')
    if metric == 'attack_smear':
        print('    Δsmear = smear_on − smear_off (log10s); negative ⇒ TNS reduced attack smear.')
    if metric == 'attack_centroid':
        print('    Δcentroid = centroid_on − centroid_off (ms); negative ⇒ TNS reduced centroid shift.')
    if metric in ('zimtohrli', 'both'):
        print('    Δzim = zimtohrli MOS_on − MOS_off; positive ⇒ TNS improved quality.')
    print('    All metrics are deterministic; no ViSQOL noise.\n')
    agg_nper = {br: [] for br in bitrates}
    agg_zim = {br: [] for br in bitrates}
    agg_smear = {br: [] for br in bitrates}
    agg_centroid = {br: [] for br in bitrates}
    with tempfile.TemporaryDirectory() as tmp:
        for ref_wav in ref_wavs:
            per_br = tns_ab_one(encoder, enc_bin, ref_wav, bitrates, tmp,
                                force_long=force_long, metric=metric)
            print(f'{os.path.basename(ref_wav)}:')
            for br in bitrates:
                entry = per_br.get(br, {'nper': [], 'zim': None, 'smear': [], 'centroid': []})
                parts = []
                d = entry['nper']
                if d:
                    agg_nper[br].extend(d)
                    arr = np.array(d)
                    frac = float(np.mean(np.abs(arr) > 0.5))
                    parts.append(f'ΔNPER = {arr.mean():+.2f} dB (n={len(arr)}, '
                                 f'{100*frac:.0f}% |Δ|>0.5dB)')
                sd = entry.get('smear', [])
                if sd:
                    agg_smear[br].extend(sd)
                    sarr = np.array(sd)
                    parts.append(f'Δsmear = {sarr.mean():+.3f} log10s (n={len(sarr)})')
                cd = entry.get('centroid', [])
                if cd:
                    agg_centroid[br].extend(cd)
                    carr = np.array(cd)
                    parts.append(f'Δcentroid = {carr.mean():+.3f} ms (n={len(carr)})')
                if entry['zim'] is not None:
                    agg_zim[br].append(entry['zim'])
                    parts.append(f'Δzim = {entry["zim"]:+.4f}')
                print(f'  {br}k:  ' + ('  '.join(parts) if parts else 'no data'))
    print('\n── aggregate across all clips ──')
    for br in bitrates:
        lines = []
        arr = np.array(agg_nper[br])
        if len(arr):
            lo, hi = bootstrap_ci(arr)
            p, neg, nz = sign_test_p(arr)
            verdict = ci_signtest_verdict(lo, hi, p, 'TNS REDUCES pre-echo', 'TNS INCREASES pre-echo')
            lines.append(f'ΔNPER = {arr.mean():+.2f} dB  95%CI[{lo:+.2f},{hi:+.2f}]  '
                         f'sign-test p={p:.3g} ({neg}/{nz} onsets improved)  → {verdict}')
        smarr = np.array(agg_smear[br])
        if len(smarr):
            lo, hi = bootstrap_ci(smarr)
            p, neg, nz = sign_test_p(smarr)
            verdict = ci_signtest_verdict(lo, hi, p, 'TNS REDUCES attack smear', 'TNS INCREASES attack smear')
            lines.append(f'Δsmear = {smarr.mean():+.3f} log10s  95%CI[{lo:+.3f},{hi:+.3f}]  '
                         f'sign-test p={p:.3g} ({neg}/{nz} onsets improved)  → {verdict}')
        carr = np.array(agg_centroid[br])
        if len(carr):
            lo, hi = bootstrap_ci(carr)
            p, neg, nz = sign_test_p(carr)
            verdict = ci_signtest_verdict(lo, hi, p, 'TNS REDUCES centroid shift', 'TNS INCREASES centroid shift')
            lines.append(f'Δcentroid = {carr.mean():+.3f} ms  95%CI[{lo:+.3f},{hi:+.3f}]  '
                         f'sign-test p={p:.3g} ({neg}/{nz} onsets improved)  → {verdict}')
        zarr = np.array(agg_zim[br])
        if len(zarr):
            lo, hi = bootstrap_ci(zarr)
            pos = int((zarr > 1e-4).sum())
            verdict = ('TNS IMPROVES quality' if lo > 0 else
                       'TNS DEGRADES quality' if hi < 0 else 'inconclusive (CI spans 0)')
            lines.append(f'Δzim  = {zarr.mean():+.4f}  95%CI[{lo:+.4f},{hi:+.4f}]  '
                         f'({pos}/{len(zarr)} clips improved)  → {verdict}')
        if not lines:
            print(f'  {br}k:  no data')
        for i, ln in enumerate(lines):
            print(f'  {br}k:  {ln}' if i == 0 else f'        {ln}')


# ── paired env-var A/B (threshold/tuning sweeps at fixed TNS state) ───────────

def parse_env_spec(spec):
    """'K=V,K=V' → dict. Empty/None → {}."""
    out = {}
    if spec:
        for item in spec.split(','):
            k, _, v = item.partition('=')
            if not k or not v:
                raise ValueError(f'bad env spec item: {item!r}')
            out[k.strip()] = v.strip()
    return out


def last_bs_stats(stderr_text):
    """Parse the last 'BS_STATS frames=N short=M pct=X.X' line → (short%, frames) or None."""
    stats = None
    for line in (stderr_text or '').splitlines():
        if line.startswith('BS_STATS'):
            fields = dict(f.split('=') for f in line.split()[1:])
            stats = (float(fields['pct']), int(fields['frames']))
    return stats


def env_ab_one(enc_bin, ref_wav, bitrates, tmp, env_a, env_b, metric='both'):
    """A/B one clip with faac, TNS on in both arms, differing only in env vars.

    Returns {bitrate: {'nper': [Δ per onset], 'zim': Δ, 'bytes': Δ,
                       'short_a': pct|None, 'short_b': pct|None}}, Δ = A − B.
    """
    ref, sr = load_mono(ref_wav)
    onsets = detect_onsets(ref, sr)
    stats_env = {'FAAC_BS_STATS': '1'}
    result = {}
    for br in bitrates:
        entry = {'nper': [], 'zim': None, 'bytes': None, 'smear': [],
                 'short_a': None, 'short_b': None}
        try:
            a_enc = os.path.join(tmp, f'a_{br}.aac'); a_wav = os.path.join(tmp, f'a_{br}.wav')
            b_enc = os.path.join(tmp, f'b_{br}.aac'); b_wav = os.path.join(tmp, f'b_{br}.wav')
            err_a = encode_any('faac', enc_bin, ref_wav, a_enc, br, tns=True,
                               force_long=False, env_extra={**stats_env, **env_a})
            err_b = encode_any('faac', enc_bin, ref_wav, b_enc, br, tns=True,
                               force_long=False, env_extra={**stats_env, **env_b})
            decode_aac(a_enc, a_wav, sr=sr, channels=1)
            decode_aac(b_enc, b_wav, sr=sr, channels=1)
        except RuntimeError as e:
            print(f'  {br}k: FAILED — {e}')
            result[br] = entry
            continue
        entry['bytes'] = os.path.getsize(a_enc) - os.path.getsize(b_enc)
        sa, sb = last_bs_stats(err_a), last_bs_stats(err_b)
        entry['short_a'] = sa[0] if sa else None
        entry['short_b'] = sb[0] if sb else None
        a_dec, _ = load_mono(a_wav)
        b_dec, _ = load_mono(b_wav)
        if metric in ('nper', 'both'):
            nper_a = nper_at_onsets(ref, a_dec, sr, onsets)
            nper_b = nper_at_onsets(ref, b_dec, sr, onsets)
            entry['nper'] = [nper_a[o] - nper_b[o] for o in nper_a if o in nper_b]
        if metric == 'attack_smear':
            smear_a = attack_smear_at_onsets(ref, a_dec, sr, onsets)
            smear_b = attack_smear_at_onsets(ref, b_dec, sr, onsets)
            entry['smear'] = [smear_a[o] - smear_b[o] for o in smear_a if o in smear_b]
        if metric in ('zimtohrli', 'both'):
            zs = []
            for dec in (a_dec, b_dec):
                lag = find_lag(ref, dec, sr)
                ra, da = align_signals(ref, dec, lag)
                zs.append(zimtohrli_mos(ra, da))
            entry['zim'] = zs[0] - zs[1]
        result[br] = entry
    return result


def cmd_env_ab(enc_bin, ref_wavs, bitrates, env_a_spec, env_b_spec, metric='both'):
    require_tuning_build(enc_bin)
    env_a, env_b = parse_env_spec(env_a_spec), parse_env_spec(env_b_spec)
    print(f'=== env A/B (faac, TNS on both arms) ===')
    print(f'    A: {env_a or "(baseline)"}   B: {env_b or "(baseline)"}')
    print('    Δ = A − B.  ΔNPER negative ⇒ A has less pre-echo;'
          ' Δzim positive ⇒ A better quality.\n')
    agg_nper = {br: [] for br in bitrates}
    agg_zim = {br: [] for br in bitrates}
    agg_bytes = {br: [] for br in bitrates}
    agg_short = {br: [] for br in bitrates}
    agg_smear = {br: [] for br in bitrates}
    with tempfile.TemporaryDirectory() as tmp:
        for ref_wav in ref_wavs:
            per_br = env_ab_one(enc_bin, ref_wav, bitrates, tmp, env_a, env_b,
                                metric=metric)
            print(f'{os.path.basename(ref_wav)}:')
            for br in bitrates:
                entry = per_br.get(br)
                if entry is None:
                    continue
                parts = []
                d = entry['nper']
                if d:
                    agg_nper[br].extend(d)
                    arr = np.array(d)
                    parts.append(f'ΔNPER = {arr.mean():+.2f} dB (n={len(arr)})')
                sd = entry.get('smear', [])
                if sd:
                    agg_smear[br].extend(sd)
                    sarr = np.array(sd)
                    parts.append(f'Δsmear = {sarr.mean():+.3f} log10s (n={len(sarr)})')
                if entry['zim'] is not None:
                    agg_zim[br].append(entry['zim'])
                    parts.append(f'Δzim = {entry["zim"]:+.4f}')
                if entry['bytes'] is not None:
                    agg_bytes[br].append(entry['bytes'])
                    parts.append(f'Δbytes = {entry["bytes"]:+d}')
                if entry['short_a'] is not None and entry['short_b'] is not None:
                    agg_short[br].append((entry['short_a'], entry['short_b']))
                    parts.append(f'short% A={entry["short_a"]:.1f} B={entry["short_b"]:.1f}')
                print(f'  {br}k:  ' + ('  '.join(parts) if parts else 'no data'))
    print('\n── aggregate across all clips (Δ = A − B) ──')
    for br in bitrates:
        lines = []
        arr = np.array(agg_nper[br])
        if len(arr):
            lo, hi = bootstrap_ci(arr)
            p, neg, nz = sign_test_p(arr)
            verdict = ('A REDUCES pre-echo' if hi < 0 else
                       'A INCREASES pre-echo' if lo > 0 else 'inconclusive (CI spans 0)')
            lines.append(f'ΔNPER = {arr.mean():+.2f} dB  95%CI[{lo:+.2f},{hi:+.2f}]  '
                         f'sign-test p={p:.3g} ({neg}/{nz} onsets improved)  → {verdict}')
        smarr = np.array(agg_smear[br])
        if len(smarr):
            lo, hi = bootstrap_ci(smarr)
            p, neg, nz = sign_test_p(smarr)
            verdict = ('A REDUCES attack smear' if hi < 0 else
                       'A INCREASES attack smear' if lo > 0 else 'inconclusive (CI spans 0)')
            lines.append(f'Δsmear = {smarr.mean():+.3f} log10s  95%CI[{lo:+.3f},{hi:+.3f}]  '
                         f'sign-test p={p:.3g} ({neg}/{nz} onsets improved)  → {verdict}')
        zarr = np.array(agg_zim[br])
        if len(zarr):
            lo, hi = bootstrap_ci(zarr)
            pos = int((zarr > 1e-4).sum())
            verdict = ('A IMPROVES quality' if lo > 0 else
                       'A DEGRADES quality' if hi < 0 else 'inconclusive (CI spans 0)')
            lines.append(f'Δzim  = {zarr.mean():+.4f}  95%CI[{lo:+.4f},{hi:+.4f}]  '
                         f'({pos}/{len(zarr)} clips improved)  → {verdict}')
        if agg_bytes[br]:
            lines.append(f'Δbytes = {np.mean(agg_bytes[br]):+.0f} avg/clip')
        if agg_short[br]:
            sa = np.mean([s[0] for s in agg_short[br]])
            sb = np.mean([s[1] for s in agg_short[br]])
            lines.append(f'short% A={sa:.1f} B={sb:.1f}')
        if not lines:
            print(f'  {br}k:  no data')
        for i, ln in enumerate(lines):
            print(f'  {br}k:  {ln}' if i == 0 else f'        {ln}')


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Transient-fidelity metrics (NPER, attack-smear) for FAAC TNS evaluation',
        epilog=(
            'Examples:\n'
            '  score_transient.py ref.wav dec.wav\n'
            '  score_transient.py --validate build/faac\n'
            '  score_transient.py --sweep build/faac cst.wav\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('positional', nargs='*',
                        help='REF.wav DEC.wav  (score a pair)')
    parser.add_argument('--validate', metavar='FAAC',
                        help='Run synthetic click validation')
    parser.add_argument('--sweep', metavar='FAAC',
                        help='Encode REF.wav at multiple bitrates and score')
    parser.add_argument('--tns-ab', metavar='ENC_BIN', dest='tns_ab',
                        help='Paired TNS-on vs TNS-off proof over REF.wav [REF2.wav ...]')
    parser.add_argument('--env-ab', metavar='ENC_BIN', dest='env_ab',
                        help='Paired A/B over env-var configs (faac, TNS on both '
                             'arms) over REF.wav [REF2.wav ...]')
    parser.add_argument('--env-a', default='', dest='env_a',
                        help='Env vars for arm A, e.g. "FAAC_TD_THRESH=1.5" '
                             '(comma-separated K=V)')
    parser.add_argument('--env-b', default='', dest='env_b',
                        help='Env vars for arm B (default: baseline, none)')
    parser.add_argument('--encoder', choices=['faac', 'ffmpeg'], default='faac',
                        help='Encoder driven by --tns-ab (default: faac)')
    parser.add_argument('--force-long', action='store_true', dest='force_long',
                        help='Disable block switching to expose pre-echo (needs '
                             'FAAC_FORCE_LONG/FF_FORCE_LONG-enabled encoder build)')
    parser.add_argument('--metric',
                        choices=['nper', 'zimtohrli', 'both', 'attack_smear', 'attack_centroid'],
                        default='nper',
                        help='Metric(s) for --tns-ab/--env-ab (default: nper). '
                             '"attack_smear" and "attack_centroid" are each exclusive of the '
                             'others. attack_smear is a documented NO-GO (near-zero yield on '
                             'dense transient material -- see compute_attack_smear docstring); '
                             'attack_centroid is its validated replacement (see '
                             'compute_attack_centroid_shift docstring) but is still only '
                             'validated for --tns-ab, not wired into env_ab_one yet.')
    parser.add_argument('--bitrates', default='20,40,80',
                        help='Comma-separated bitrates for --validate/--sweep (default: 20,40,80)')
    parser.add_argument('--no-tns', action='store_true',
                        help='Pass --no-tns to FAAC when encoding (for --sweep)')
    parser.add_argument('-v', '--verbose', action='store_true')

    args = parser.parse_args()
    bitrates = [int(x) for x in args.bitrates.split(',')]

    if args.validate:
        cmd_validate(args.validate, bitrates)

    elif args.env_ab:
        if not args.positional:
            parser.error('--env-ab requires at least one REF.wav argument')
        cmd_env_ab(args.env_ab, args.positional, bitrates,
                   args.env_a, args.env_b, metric=args.metric)

    elif args.tns_ab:
        if not args.positional:
            parser.error('--tns-ab requires at least one REF.wav argument')
        cmd_tns_ab(args.tns_ab, args.positional, bitrates,
                   encoder=args.encoder, force_long=args.force_long,
                   metric=args.metric)

    elif args.sweep:
        if not args.positional:
            parser.error('--sweep requires a REF.wav argument')
        cmd_sweep(args.sweep, args.positional[0], bitrates,
                  no_tns=args.no_tns)

    elif len(args.positional) == 2:
        ref_path, dec_path = args.positional
        mean, std, n, smean, sstd, sn, cmean, cstd, cn = score_pair(
            ref_path, dec_path, verbose=args.verbose)
        if mean is None:
            print('NPER: no onsets detected')
        else:
            print(f'NPER: {mean:+.1f} ± {std:.1f} dB  ({n} onsets)')
        if smean is None:
            print('attack-smear: no onsets detected')
        else:
            print(f'attack-smear: {smean:+.3f} ± {sstd:.3f} log10s  ({sn} onsets)')
        if cmean is None:
            print('attack-centroid-shift: no onsets detected')
        else:
            print(f'attack-centroid-shift: {cmean:+.3f} ± {cstd:.3f} ms  ({cn} onsets)')

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
