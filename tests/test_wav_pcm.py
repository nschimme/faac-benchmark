import os
import struct
import tempfile
import unittest
from pathlib import Path
from codec_bench.wav_pcm import wav_pcm_info, wav_pcm_identity


def fixture(pcm=b'\x01\x02\x03\x04', rf64=False, junk=False, size=None):
    size = len(pcm) if size is None else size
    fmt = struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, 48000, 96000, 2, 16)
    extra = b'JUNK' + struct.pack('<I', 3) + b'abc\0' if junk else b''
    ds64 = b'ds64' + struct.pack('<IQQQI', 28, 72 + size, size, size // 2, 0) if rf64 else b''
    body = b'WAVE' + ds64 + extra + fmt + b'data' + struct.pack('<I', 0xffffffff if rf64 else size) + pcm
    return (b'RF64' if rf64 else b'RIFF') + struct.pack('<I', 0xffffffff if rf64 else len(body)) + body


class TestWavPCM(unittest.TestCase):
    def test_pcm_identity_ignores_container_and_padded_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            identities = []
            for i, (rf64, junk) in enumerate(((False, False), (False, True), (True, False))):
                path = Path(td) / str(i)
                path.write_bytes(fixture(rf64=rf64, junk=junk))
                identities.append(wav_pcm_identity(path))
            self.assertEqual(len({x['pcm_sha256'] for x in identities}), 1)
            self.assertEqual(len({x['format'] for x in identities}), 1)
            self.assertEqual([x['pcm_bytes'] for x in identities], [4, 4, 4])

    def test_large_rf64_inspection_does_not_read_pcm(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'large.wav'
            size = (1 << 32) + 4
            with path.open('wb') as f:
                f.write(fixture(pcm=b'', rf64=True, size=size))
                f.truncate(80 + size)
            info = wav_pcm_info(path)
            self.assertEqual(info['pcm_bytes'], size)
            self.assertEqual(info['data_offset'], 80)

    def test_truncation_and_missing_ds64_fail(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'bad.wav'
            for data in (fixture()[:-1], fixture().replace(b'RIFF', b'RF64', 1)):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    wav_pcm_info(path)
