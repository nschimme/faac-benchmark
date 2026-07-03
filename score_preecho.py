"""
Pre-echo metric (NPER) for FAAC TNS short-window evaluation.

NPER = 10*log10(dec_pre_energy/onset_peak) - 10*log10(ref_pre_energy/onset_peak)

Positive NPER means the decoded audio has more pre-echo than the reference (bad).
A decrease in NPER with increasing bitrate validates that the metric is working.

Usage:
  score_preecho.py REF.wav DEC.wav [--verbose]
  score_preecho.py --validate FAAC_BIN [--bitrates 20,40,80] [--sr 48000]
  score_preecho.py --sweep   FAAC_BIN REF.wav [--bitrates 20,40,80]
"""

import argparse
import os
import subprocess
import sys
import tempfile

import numpy as np
import scipy.signal
import soundfile as sf


# ── algorithm constants ────────────────────────────────────────────────────────
HOP               = 512    # STFT hop length (~10 ms at 48 kHz)
WIN_LEN           = 2048   # STFT window (4× hop → good freq resolution)
MIN_ONSET_SPACING = 1024   # minimum samples between detected onsets (~21 ms)
NOISE_FLOOR_DB    = -80.0  # log floor relative to onset peak energy


# ── audio I/O ─────────────────────────────────────────────────────────────────

def load_mono(path):
    """Load audio, mix to mono, return (float32 array, sample_rate)."""
    audio, sr = sf.read(path, dtype='float32', always_2d=True)
    return audio.mean(axis=1), int(sr)


def decode_aac(aac_path, wav_path, sr=48000, channels=1):
    """Decode AAC to WAV via ffmpeg."""
    cmd = ['ffmpeg', '-y', '-i', aac_path,
           '-ar', str(sr), '-ac', str(channels),
           '-sample_fmt', 's16', wav_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg decode failed:\n{result.stderr}')


def encode_aac(faac_bin, wav_path, aac_path, bitrate, extra_args=None, env_extra=None):
    """Encode WAV to AAC via FAAC at the given total bitrate (kbps)."""
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
        extra = [] if tns else ['--no-tns']
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


# ── top-level scoring ─────────────────────────────────────────────────────────

def score_pair(ref_path, dec_path, verbose=False):
    """Score a reference/decoded WAV pair. Returns (mean_nper_db, std_nper_db, n)."""
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
    if not nper_list:
        return None, None, 0

    vals = np.array([v for _, v in nper_list])
    return float(vals.mean()), float(vals.std()), len(vals)


# ── validate: synthetic click test ────────────────────────────────────────────

def make_click_wav(path, sr=48000, silence_sec=0.5, amplitude=0.9):
    """Write a mono WAV: silence + single-sample pulse + silence."""
    n_silence = int(sr * silence_sec)
    sig = np.zeros(n_silence * 2 + 1, dtype=np.float32)
    sig[n_silence] = amplitude
    sf.write(path, sig, sr, subtype='PCM_16')


def cmd_validate(faac_bin, bitrates, sr=48000):
    print('=== NPER validation: synthetic click sweep ===')
    print('Expected: NPER decreases monotonically as bitrate increases\n')

    with tempfile.TemporaryDirectory() as tmp:
        ref_wav = os.path.join(tmp, 'click_ref.wav')
        make_click_wav(ref_wav, sr=sr)

        prev_nper = None
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

            mean, std, n = score_pair(ref_wav, dec_wav, verbose=True)
            if mean is None:
                print(f'  {br}k: NO ONSETS DETECTED — onset detection failed')
                ok = False
                continue

            marker = ''
            if prev_nper is not None and mean >= prev_nper - 0.5:
                marker = '  ← WARNING: not decreasing'
                ok = False
            print(f'  {br}k:  NPER = {mean:+.1f} ± {std:.1f} dB  ({n} onsets){marker}')
            prev_nper = mean

    print()
    if ok:
        print('PASS: NPER decreases with bitrate — metric is working')
    else:
        print('FAIL: unexpected NPER trend — check alignment and onset detection')
        sys.exit(1)


# ── sweep: score a real clip at multiple bitrates ─────────────────────────────

def cmd_sweep(faac_bin, ref_wav, bitrates, no_tns=False):
    print(f'=== NPER sweep: {os.path.basename(ref_wav)} ===')
    extra = ['--no-tns'] if no_tns else []

    ref, sr = load_mono(ref_wav)
    channels = 1  # We score on mono mix regardless of source

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

            mean, std, n = score_pair(ref_wav, dec_wav)
            if mean is None:
                print(f'  {br}k:  no onsets detected')
            else:
                print(f'  {br}k:  NPER = {mean:+.1f} ± {std:.1f} dB  ({n} onsets)')


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
    from math import comb
    nz = [d for d in deltas if abs(d) > 1e-9]
    n = len(nz)
    if n == 0:
        return 1.0, 0, 0
    neg = sum(1 for d in nz if d < 0)
    k = min(neg, n - neg)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail), neg, n


def tns_ab_one(encoder, enc_bin, ref_wav, bitrates, tmp, force_long=False,
               metric='nper'):
    """A/B one clip. Returns {bitrate: {'nper': [ΔNPER per onset], 'zim': Δzim_MOS}}.

    ΔNPER = NPER_on − NPER_off (negative ⇒ TNS reduced pre-echo).
    Δzim  = zimtohrli MOS_on − MOS_off (positive ⇒ TNS improved quality).
    """
    ref, sr = load_mono(ref_wav)
    onsets = detect_onsets(ref, sr)
    result = {}
    ext = 'aac' if encoder == 'faac' else 'm4a'
    for br in bitrates:
        entry = {'nper': [], 'zim': None}
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
    mode = f'encoder={encoder}, force_long={force_long}, metric={metric}'
    print(f'=== TNS A/B proof ({mode}) ===')
    if metric in ('nper', 'both'):
        print('    ΔNPER = NPER_on − NPER_off; negative ⇒ TNS reduced pre-echo (value).')
    if metric in ('zimtohrli', 'both'):
        print('    Δzim = zimtohrli MOS_on − MOS_off; positive ⇒ TNS improved quality.')
    print('    Both metrics are deterministic; no ViSQOL noise.\n')
    agg_nper = {br: [] for br in bitrates}
    agg_zim = {br: [] for br in bitrates}
    with tempfile.TemporaryDirectory() as tmp:
        for ref_wav in ref_wavs:
            per_br = tns_ab_one(encoder, enc_bin, ref_wav, bitrates, tmp,
                                force_long=force_long, metric=metric)
            print(f'{os.path.basename(ref_wav)}:')
            for br in bitrates:
                entry = per_br.get(br, {'nper': [], 'zim': None})
                parts = []
                d = entry['nper']
                if d:
                    agg_nper[br].extend(d)
                    arr = np.array(d)
                    frac = float(np.mean(np.abs(arr) > 0.5))
                    parts.append(f'ΔNPER = {arr.mean():+.2f} dB (n={len(arr)}, '
                                 f'{100*frac:.0f}% |Δ|>0.5dB)')
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
            verdict = ('TNS REDUCES pre-echo' if hi < 0 else
                       'TNS INCREASES pre-echo' if lo > 0 else 'inconclusive (CI spans 0)')
            lines.append(f'ΔNPER = {arr.mean():+.2f} dB  95%CI[{lo:+.2f},{hi:+.2f}]  '
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
        entry = {'nper': [], 'zim': None, 'bytes': None,
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
    env_a, env_b = parse_env_spec(env_a_spec), parse_env_spec(env_b_spec)
    print(f'=== env A/B (faac, TNS on both arms) ===')
    print(f'    A: {env_a or "(baseline)"}   B: {env_b or "(baseline)"}')
    print('    Δ = A − B.  ΔNPER negative ⇒ A has less pre-echo;'
          ' Δzim positive ⇒ A better quality.\n')
    agg_nper = {br: [] for br in bitrates}
    agg_zim = {br: [] for br in bitrates}
    agg_bytes = {br: [] for br in bitrates}
    agg_short = {br: [] for br in bitrates}
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
        description='Pre-echo metric (NPER) for FAAC TNS evaluation',
        epilog=(
            'Examples:\n'
            '  score_preecho.py ref.wav dec.wav\n'
            '  score_preecho.py --validate build/faac\n'
            '  score_preecho.py --sweep build/faac cst.wav\n'
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
    parser.add_argument('--metric', choices=['nper', 'zimtohrli', 'both'], default='nper',
                        help='Metric(s) for --tns-ab (default: nper)')
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
        mean, std, n = score_pair(ref_path, dec_path, verbose=args.verbose)
        if mean is None:
            print('NPER: no onsets detected')
        else:
            print(f'NPER: {mean:+.1f} ± {std:.1f} dB  ({n} onsets)')

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
