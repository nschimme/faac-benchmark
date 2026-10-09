"""
 * FAAC Benchmark Suite — Phase 5: Decoder Output Equality
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

 Exact PCM equality between baseline and candidate faad.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from codec_bench.wav_pcm import wav_pcm_info

import numpy as np


def read_pcm(path):
    # faad's default WAV output is signed 16-bit PCM. Headers are not compared.
    info = wav_pcm_info(path)
    tag, channels, rate, _, bits, _, _ = info['format']
    if tag != 1 or bits != 16:
        raise ValueError('expected uncompressed 16-bit PCM')
    with open(path, 'rb') as wav:
        wav.seek(info['data_offset'])
        pcm = np.frombuffer(wav.read(info['pcm_bytes']), dtype='<i2').astype(np.int64)
    if len(pcm) % channels:
        raise ValueError('incomplete PCM frame')
    return pcm, channels, rate


def compare_streams(base_bin, cand_bin, streams):
    for side, binary in (('baseline', base_bin), ('candidate', cand_bin)):
        if not binary or not shutil.which(str(binary)):
            return {'skipped': f'{side} faad binary missing: {binary or "not supplied"}'}
    if not streams:
        return {'skipped': 'no Phase 1 .m4a streams available'}
    block = {'streams': 0, 'unchanged': 0, 'changed': [], 'failed': []}
    # Sort and cap deterministically; never select stale files outside the matrix.
    streams = sorted(streams, key=lambda s: str(s['path']))[:200]
    with tempfile.TemporaryDirectory(prefix='decoder-diff-') as tmp:
        for i, stream in enumerate(streams):
            block['streams'] += 1
            decoded = {}
            for side, binary in (('base', base_bin), ('cand', cand_bin)):
                out = Path(tmp) / f'{i}-{side}.wav'  # does not exist before faad runs
                try:
                    proc = subprocess.run([str(binary), '-o', str(out), str(stream['path'])],
                                          capture_output=True, text=True, timeout=120)
                    if proc.returncode:
                        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f'exit {proc.returncode}')
                    decoded[side] = read_pcm(out)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    block['failed'].append({'file': stream['file'], 'side': side, 'error': str(exc)})
            if len(decoded) != 2:
                continue
            b, bc, br = decoded['base']
            c, cc, cr = decoded['cand']
            if bc == cc and br == cr and np.array_equal(b, c):
                block['unchanged'] += 1
                continue
            n = min(len(b), len(c))
            delta = c[:n] - b[:n]
            indices = np.flatnonzero(delta)
            first = int(indices[0]) if len(indices) else (n if len(b) != len(c) else None)
            # Zero-pad the shorter output for diagnostics; equality never uses a tolerance.
            padded_b = np.pad(b, (0, max(0, len(c) - len(b))))
            padded_c = np.pad(c, (0, max(0, len(b) - len(c))))
            error = padded_c - padded_b
            noise = float(np.dot(error.astype(float), error.astype(float)))
            signal = float(np.dot(padded_b.astype(float), padded_b.astype(float)))
            snr = 10 * math.log10(signal / noise) if signal and noise else None
            detail = {'file': stream['file'], 'scenario': stream.get('scenario'),
                      'profile': stream.get('profile'), 'base_frames': len(b) // bc,
                      'cand_frames': len(c) // cc, 'first_diff_sample': first,
                      'max_abs_diff': int(np.abs(error).max()) if len(error) else 0,
                      'snr_db_vs_base': snr}
            if bc != cc or br != cr:
                detail['format_change'] = f'{bc}ch/{br}Hz -> {cc}ch/{cr}Hz'
            block['changed'].append(detail)
    return block


def matrix_streams(data, directory):
    return [{'path': Path(directory) / clip['aac'], 'file': clip['aac'],
             'scenario': clip.get('scenario'), 'profile': clip.get('object_type')}
            for clip in data.get('matrix', {}).values()
            if clip.get('aac', '').endswith('.m4a')]


def store_block(path, block):
    with open(path) as f:
        data = json.load(f)
    data['decoder_diff'] = block
    tmp = str(path) + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def summary(block):
    if block is not None and 'error' in block:
        return f"decoder output: DID NOT RUN ({block['error']})"
    if not block or 'skipped' in block:
        return 'decoder output: skipped (' + (block or {}).get('skipped', 'no decoder_diff block') + ')'
    changed = len(block['changed'])
    failed = len({x['file'] for x in block['failed']})
    if changed or failed:
        return f"decoder output: CHANGED on {changed} of {block['streams']} streams" + (f', failed on {failed}' if failed else '') + ' (see cases)'
    return f"decoder output: unchanged on {block['unchanged']} streams"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results_json', help='Result JSON to update in place')
    parser.add_argument('streams_dir', help='Phase 1 output directory, or standalone stream directory')
    parser.add_argument('--decoder-ref-bin')
    parser.add_argument('--decoder-bin')
    parser.add_argument('--all-streams', action='store_true', help='Use directory .m4a files instead of the result matrix')
    args = parser.parse_args()
    with open(args.results_json) as f:
        data = json.load(f)
    if args.all_streams:
        streams = []
        for path in sorted(Path(args.streams_dir).rglob('*.m4a')):
            profile = None
            try:
                probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                        '-show_entries', 'stream=profile', '-of', 'json', str(path)],
                                       capture_output=True, text=True, check=True)
                profile = json.loads(probe.stdout)['streams'][0]['profile']
            except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError):
                pass
            streams.append({'path': path, 'file': str(path.relative_to(args.streams_dir)), 'profile': profile})
    else:
        streams = matrix_streams(data, args.streams_dir)
    block = compare_streams(args.decoder_ref_bin, args.decoder_bin, streams)
    store_block(args.results_json, block)
    print(summary(block))


if __name__ == '__main__':
    main()
