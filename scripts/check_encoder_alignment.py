#!/usr/bin/env python3
"""Check scorer alignment with a deterministic stereo AAC click control."""
import argparse
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

import numpy as np
from scipy.io import wavfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codec_bench.encoders import get_encoder_instance
from codec_bench.decoders import FFmpegDecoder
from transient import _pre_echo_lag

POSITIONS = [40037, 72501, 104219, 136777, 168093]
RATE = 48000


def make_control(path):
    """Match the original clicks.wav recipe, including integer truncation."""
    rng = random.Random(1)
    samples = [int(rng.gauss(0, 3)) for _ in range(RATE * 4)]
    for pos in POSITIONS:
        for j in range(64):
            samples[pos + j] += int(20000 * 0.92**j * (1 if j % 2 == 0 else -0.6))
    mono = np.clip(samples, -32768, 32767).astype(np.int16)
    stereo = np.column_stack((mono, mono))
    wavfile.write(path, RATE, stereo)
    return stereo.astype(np.float64) / 32768


def run(cmd, env):
    result = subprocess.run(list(map(str, cmd)), capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError(f'{cmd}:\n{result.stderr.decode(errors="replace")}')
    return result


def check(encoder, directory, args, env):
    encoded = directory / ('control' + encoder.file_ext)
    decoded = directory / 'decoded.wav'
    cmd = encoder.get_encode_cmd(str(directory / 'control.wav'), str(encoded), 128, 2, RATE)
    print(f'{encoder.tool_id}: encode {json.dumps(cmd)}', flush=True)
    run(cmd, dict(env, **encoder.get_run_env()))
    mux = encoder.get_mux_cmd(str(encoded))
    if mux:
        run(mux, env)
    run(FFmpegDecoder('ffmpeg', args.decode_ffmpeg).get_decode_cmd(str(encoded), str(decoded)), env)
    rate, audio = wavfile.read(decoded)
    if rate != RATE or audio.ndim != 2 or audio.shape[1] != 2:
        raise RuntimeError(f'unexpected decode format: {rate}, {audio.shape}')
    lag = _pre_echo_lag(args.reference, audio.astype(np.float64) / 32768)
    probe = json.loads(run([args.ffprobe, '-v', 'error', '-show_packets', '-show_entries',
        'stream=index,codec_type,start_pts,time_base:packet=stream_index,pts,duration',
        '-of', 'json', encoded], env).stdout)
    stream = next(s for s in probe['streams'] if s['codec_type'] in ('audio', 'video'))
    if stream['time_base'] != '1/48000':
        raise RuntimeError(f'unexpected time base: {stream}')
    start = int(stream['start_pts'])
    print(f'{encoder.tool_id}: lag={lag} samples; stream start_pts={start}; time_base=1/48000')
    valid = lag == 0
    if not args.trace_decoder:
        print(f'{encoder.tool_id}: SKIP (c): stock decoder window trace unavailable (use --trace-decoder)')
        return valid
    adts = directory / 'stream.aac'
    adts.write_bytes(run([args.decode_ffmpeg, '-v', 'error', '-i', encoded,
                         '-c:a', 'copy', '-f', 'adts', 'pipe:1'], env).stdout)
    result = run([args.trace_decoder, '--overwrite', '-o', directory / 'traced.wav', adts],
                 dict(env, FAAD_GROUP_TRACE='1'))
    frames = [json.loads(line[len('TOOL_TRACE '):]) for line in result.stderr.decode().splitlines()
              if line.startswith('TOOL_TRACE ')]
    if not frames:
        print(f'{encoder.tool_id}: SKIP (c): decoder emits no TOOL_TRACE records')
        return valid
    packets = [p for p in probe['packets'] if p['stream_index'] == stream['index']]
    if len(frames) != len(packets):
        raise RuntimeError('trace/packet count mismatch')
    for click in POSITIONS:
        # Short windows sit in [448, 1600) of a packet's 2048-sample span, so a
        # click can be covered by a packet next to the one that contains it.
        covering = [j for j, p in enumerate(packets)
                    if int(p['pts']) - start + 448 <= click < int(p['pts']) - start + 1600
                    and all(c['window_sequence'] == 2 for c in frames[j]['channels'])]
        short = bool(covering)
        print(f'{encoder.tool_id}: click={click} short_window_packets={covering} short={short}')
        valid = valid and short
    return valid


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Only explicitly supplied encoder rows run, at 128 kbps LC stereo. '
        'Packet source time is raw PTS minus stream start_pts. '
        'A nonzero lag or failed short-block assertion exits nonzero.'))
    for name in ('faac', 'fdkaac', 'afconvert', 'ffmpeg'):
        parser.add_argument(f'--{name}-bin', action='append', default=[], metavar='PATH',
                            help=f'test this {name} encoder (repeatable)')
    parser.add_argument('--encoder', action='append', default=[], metavar='TYPE=PATH',
                        help='additional codec_bench encoder type and binary (repeatable)')
    parser.add_argument('--decode-ffmpeg', default='ffmpeg', help='scorer decoder binary')
    parser.add_argument('--ffprobe', default='ffprobe', help='packet/stream probe binary')
    parser.add_argument('--trace-decoder', help='FAAD decoder emitting TOOL_TRACE with FAAD_GROUP_TRACE=1')
    args = parser.parse_args()
    rows = [(name, path) for name in ('faac', 'fdkaac', 'afconvert', 'ffmpeg')
            for path in getattr(args, name + '_bin')]
    for row in args.encoder:
        if '=' not in row:
            parser.error('--encoder requires TYPE=PATH')
        rows.append(tuple(row.split('=', 1)))
    if not rows:
        parser.error('supply at least one encoder binary')
    env = dict(os.environ, CCACHE_DISABLE='1', PYTHONDONTWRITEBYTECODE='1')
    os.environ.update(CCACHE_DISABLE='1', PYTHONDONTWRITEBYTECODE='1')
    failed = False
    for kind, binary in rows:
        try:
            with tempfile.TemporaryDirectory(prefix='encoder-alignment-') as tmp:
                directory = Path(tmp)
                args.reference = make_control(directory / 'control.wav')
                encoder = get_encoder_instance(kind, binary_path=binary, profile='lc')
                if encoder is None:
                    raise RuntimeError(f'unsupported encoder: {kind}')
                failed = not check(encoder, directory, args, env) or failed
        except (RuntimeError, OSError, ValueError, StopIteration) as exc:
            print(f'{kind}: FAIL: {exc}', file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
