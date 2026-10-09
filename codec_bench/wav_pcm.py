"""Inspect RIFF/RF64 PCM without fixed header offsets or whole-file reads."""
import hashlib
import os
import struct


def wav_pcm_info(path):
    with open(path, 'rb') as f:
        length = os.fstat(f.fileno()).st_size
        header = f.read(12)
        if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
            raise ValueError('expected RIFF/RF64 WAVE')
        rf64 = header[:4] == b'RF64'
        data64 = None
        overrides = []
        fmt = None
        while f.tell() + 8 <= length:
            kind, size = struct.unpack('<4sI', f.read(8))
            start = f.tell()
            if kind == b'ds64':
                if size < 28 or start + size > length:
                    raise ValueError('invalid ds64')
                _, data64, _, count = struct.unpack('<QQQI', f.read(28))
                if count > (size - 28) // 12 or count > 65536:
                    raise ValueError('invalid ds64 table')
                overrides = [struct.unpack('<4sQ', f.read(12)) for _ in range(count)]
            elif size == 0xffffffff and rf64:
                if kind == b'data' and data64 is not None:
                    size = data64
                else:
                    for i, (entry_kind, entry_size) in enumerate(overrides):
                        if entry_kind == kind:
                            size = entry_size
                            overrides.pop(i)
                            break
                    else:
                        raise ValueError('missing ds64 size')
            if start + size > length:
                raise ValueError('truncated WAV chunk')
            if kind == b'fmt ':
                if size < 16:
                    raise ValueError('short WAV format')
                raw = f.read(min(size, 40))
                tag, channels, rate, _, align, bits = struct.unpack('<HHIIHH', raw[:16])
                valid, mask = bits, 0
                if tag == 0xfffe:
                    if len(raw) < 40 or struct.unpack_from('<H', raw, 16)[0] < 22:
                        raise ValueError('short extensible WAV format')
                    valid, mask = struct.unpack_from('<HI', raw, 18)
                    if raw[28:40] != bytes.fromhex('00001000800000aa00389b71'):
                        raise ValueError('unsupported WAV subtype')
                    tag = struct.unpack_from('<I', raw, 24)[0]
                if tag not in (1, 3) or not channels or not rate or not bits or align != channels * ((bits + 7) // 8):
                    raise ValueError('unsupported PCM format')
                fmt = (tag, channels, rate, align, bits, valid, mask)
            elif kind == b'data':
                if fmt is None or (rf64 and data64 is None) or size % fmt[3]:
                    raise ValueError('invalid PCM data')
                return {'format': fmt, 'data_offset': start, 'pcm_bytes': size}
            f.seek(start + size + (size & 1))
        raise ValueError('missing PCM data')


def wav_pcm_identity(path):
    info = wav_pcm_info(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        f.seek(info['data_offset'])
        remaining = info['pcm_bytes']
        while remaining:
            block = f.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError('truncated PCM data')
            digest.update(block)
            remaining -= len(block)
    return {**info, 'pcm_sha256': digest.hexdigest()}
