#!/usr/bin/env python3
"""Local A/B sweep tool: two-binary comparison, or single-binary env-var sweep.

Two-binary mode (bin_a vs bin_b, same env, zimtohrli MOS per clip/bitrate):
  python3 sweep_binary_ab.py BIN_A BIN_B [clip.wav ...] [--bitrates B1,B2,...]

Env-var sweep mode (one binary, each --values entry vs the first as baseline,
via score_transient.cmd_env_ab: paired A/B, bootstrap CI, byte delta, short%):
  python3 sweep_binary_ab.py BIN --env-var NAME --values V0,V1,... \\
      [clip.wav ...] [--bitrates B1,B2,...] [--metric nper|zimtohrli|both|attack_smear]

  An empty entry (or the literal "unset") in --values means "don't set NAME at
  all" (e.g. --values unset,0 tests NAME unset as baseline vs NAME=0).

Both modes force --object-type=lc so low-bitrate mono clips don't
auto-select HE-AAC v1 and skip the core TNS path, and bypass
score_transient's -Dtuning-build gate: two-binary mode never needs it; env-var
mode assumes a plain getenv() knob (FAAC_TD_THRESH, FAAC_TNS_DIR, ...) that
doesn't require a tuning build either.
"""
import argparse
import os
import sys
import tempfile

import score_transient as sp

DEFAULT_CLIPS = [
    'data/external/audio/cst.wav',
    'data/external/audio/35_SQAM_glockenspiel_cut.16b48k.wav',
    'data/external/audio/qrt.wav',
    'data/external/audio/fms.wav',
    'data/external/audio/12-German-male-speech.441.16b48k.wav',
    'data/external/audio/sandman.16b48k.wav',
]
DEFAULT_BITRATES = [20, 32, 48, 64, 96, 128]


def encode_lc(bin_path, wav_path, aac_path, bitrate):
    return sp.encode_aac(bin_path, wav_path, aac_path, bitrate,
                          extra_args=['--object-type=lc'])


def binary_ab(bin_a, bin_b, clips, bitrates):
    summary = []
    with tempfile.TemporaryDirectory() as tmp:
        for br in bitrates:
            deltas = []
            for clip in clips:
                ref, sr = sp.load_mono(clip)
                aac_a = os.path.join(tmp, 'a.aac')
                aac_b = os.path.join(tmp, 'b.aac')
                wav_a = os.path.join(tmp, 'a.wav')
                wav_b = os.path.join(tmp, 'b.wav')
                encode_lc(bin_a, clip, aac_a, br)
                encode_lc(bin_b, clip, aac_b, br)
                sp.decode_aac(aac_a, wav_a, sr=sr, channels=1)
                sp.decode_aac(aac_b, wav_b, sr=sr, channels=1)
                dec_a, _ = sp.load_mono(wav_a)
                dec_b, _ = sp.load_mono(wav_b)
                lag_a = sp.find_lag(ref, dec_a, sr)
                lag_b = sp.find_lag(ref, dec_b, sr)
                ra, da = sp.align_signals(ref, dec_a, lag_a)
                rb, db = sp.align_signals(ref, dec_b, lag_b)
                mos_a = sp.zimtohrli_mos(ra, da)
                mos_b = sp.zimtohrli_mos(rb, db)
                d = mos_b - mos_a
                deltas.append(d)
                print(f'  {br}k {os.path.basename(clip):45s} a={mos_a:.4f} b={mos_b:.4f} d={d:+.4f}')
            lo, hi = sp.bootstrap_ci(deltas)
            mean_d = sum(deltas) / len(deltas)
            summary.append((br, mean_d, lo, hi))
            print(f'{br}k mean d={mean_d:+.4f} CI=[{lo:+.4f},{hi:+.4f}]\n')

    if len(bitrates) > 1:
        print('=== summary ===')
        for br, mean_d, lo, hi in summary:
            crosses = 'crosses zero' if lo <= 0 <= hi else ('POSITIVE' if lo > 0 else 'NEGATIVE')
            print(f'{br:4d}k  mean={mean_d:+.4f}  CI=[{lo:+.4f},{hi:+.4f}]  {crosses}')


def _env_spec(env_var, value):
    return '' if value in ('', 'unset') else f'{env_var}={value}'


def env_var_ab(enc_bin, env_var, values, clips, bitrates, metric):
    orig_encode_aac = sp.encode_aac

    def _encode_aac_lc(faac_bin, wav_path, aac_path, bitrate, extra_args=None, env_extra=None):
        extra_args = [a for a in (extra_args or []) if a != '--tns'] + ['--object-type=lc']
        return orig_encode_aac(faac_bin, wav_path, aac_path, bitrate,
                                extra_args=extra_args, env_extra=env_extra)

    sp.encode_aac = _encode_aac_lc

    baseline_spec = _env_spec(env_var, values[0])
    for v in values[1:]:
        print(f'\n=== {env_var}={v or "unset"} vs baseline ({env_var}={values[0] or "unset"}) ===')
        sp.cmd_env_ab(enc_bin, clips, bitrates, _env_spec(env_var, v), baseline_spec,
                      metric=metric)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('bin_a', help='faac binary under test (both modes)')
    parser.add_argument('positional', nargs='*',
                        help='two-binary mode: BIN_B then clip.wav...; '
                             'env-var mode: clip.wav... only')
    parser.add_argument('--bitrates', help='Comma-separated bitrates (default varies by mode)')
    parser.add_argument('--env-var', dest='env_var', metavar='NAME',
                        help='Switch to env-var sweep mode; NAME is the env var to sweep')
    parser.add_argument('--values', help='Comma-separated values for --env-var (first is baseline)')
    parser.add_argument('--metric', choices=['nper', 'zimtohrli', 'both', 'attack_smear'],
                        default='zimtohrli',
                        help='Metric(s) for --env-var mode (default: zimtohrli)')
    args = parser.parse_args()

    sp.require_tuning_build = lambda *_a, **_k: None

    if args.env_var:
        if not args.values:
            parser.error('--env-var requires --values')
        values = args.values.split(',')
        clips = args.positional or DEFAULT_CLIPS
        bitrates = [int(b) for b in args.bitrates.split(',')] if args.bitrates else [20, 32, 48]
        env_var_ab(args.bin_a, args.env_var, values, clips, bitrates, args.metric)
    else:
        if not args.positional:
            parser.error('two-binary mode requires BIN_B')
        bin_b, clips = args.positional[0], args.positional[1:] or DEFAULT_CLIPS
        bitrates = [int(b) for b in args.bitrates.split(',')] if args.bitrates else DEFAULT_BITRATES
        binary_ab(args.bin_a, bin_b, clips, bitrates)


if __name__ == '__main__':
    main()
