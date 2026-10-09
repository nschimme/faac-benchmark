"""
 * FAAC Benchmark Suite - Unit Tests for scripts/esp32/esp32_enc_bench.py (no hardware)
"""

import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESP32_DIR = os.path.join(SCRIPT_DIR, "scripts", "esp32")
for p in (SCRIPT_DIR, ESP32_DIR):
    if p not in sys.path:
        sys.path.append(p)

import esp32_enc_bench as eb


def response(status=0, frames=7, out_bytes=1000, best=1_000_000, crc=0xDEADBEEF):
    return struct.pack(eb.RSP_FMT, status, frames, out_bytes, 2, 1024, 48000, 32000, 14000,
                       best, best + 5, crc, 4096, 2048, 999, 77)


def adts_frame(n, fill=0):
    """A syntactically valid ADTS header (7 bytes) padded to n bytes."""
    hdr = bytearray(7)
    hdr[0], hdr[1] = 0xFF, 0xF1
    hdr[3] = (n >> 11) & 3
    hdr[4] = (n >> 3) & 0xFF
    hdr[5] = (n & 7) << 5
    return bytes(hdr) + bytes([fill]) * (n - 7)


class FakeSerial:
    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.written = bytearray()
        self.timeout = 2

    def read(self, n=1):
        out = bytes(self.data[:n])
        del self.data[:n]
        return out

    def write(self, b):
        self.written += b

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass


def make_device(data):
    dev = eb.Device.__new__(eb.Device)
    dev.ser = FakeSerial(data)
    return dev


class TestProtocol(unittest.TestCase):
    def test_sizes_match_firmware(self):
        self.assertEqual(struct.calcsize(eb.REQ_FMT), 28)
        self.assertEqual(4 + eb.RSP_LEN, 72)  # _Static_assert in enc_main.c

    def test_pack_request(self):
        raw = eb.pack_request(48000, 2, 32000, 5, 3, True, 1234)
        self.assertEqual(raw[:4], b"ENCB")
        self.assertEqual(struct.unpack(eb.REQ_FMT, raw[4:]), (48000, 2, 32000, 5, 3, 1, 1234))
        self.assertEqual(struct.unpack(eb.REQ_FMT, eb.pack_request(1, 1, 1, 2, 1, False, 2)[4:])[5], 0)

    def test_unpack_response(self):
        r = eb.unpack_response(response(best=123456))
        self.assertEqual(r["status_text"], "ok")
        self.assertEqual(r["best_cycles"], 123456)
        self.assertEqual(r["mean_cycles"], 123461)
        self.assertEqual(r["stream_crc32"], 0xDEADBEEF)
        self.assertEqual((r["frame_samples"], r["enc_rate"], r["bit_rate"], r["bandwidth"]), (1024, 48000, 32000, 14000))
        self.assertEqual((r["heap_min_free_delta"], r["stack_hwm"], r["init_heap_used"], r["open_cycles"]),
                         (4096, 2048, 999, 77))

    def test_unpack_rejects_short_header(self):
        with self.assertRaises(ValueError):
            eb.unpack_response(b"\x00" * 10)

    def test_unknown_status_text(self):
        self.assertEqual(eb.unpack_response(response(status=42))["status_text"], "42")

    def test_device_encode_skips_boot_noise_and_returns_stream(self):
        stream = b"\xff\xf1" * 5
        dev = make_device(b"boot log\r\n" + eb.ACK_MAGIC + eb.RSP_MAGIC + response(out_bytes=len(stream)) + stream)
        res, got = dev.encode(b"\x01\x00" * 9000, 48000, 2, 32000, 2, loops=2, want_stream=True)
        self.assertEqual(got, stream)
        self.assertEqual(res["out_bytes"], len(stream))
        sent = bytes(dev.ser.written)
        self.assertEqual(sent[:4], b"ENCB")
        self.assertEqual(len(sent), 4 + 28 + 18000)

    def test_device_encode_error_has_no_stream(self):
        dev = make_device(eb.RSP_MAGIC + response(status=3))
        res = dev.encode(b"\x00\x00", 16000, 1, 24000, 5, want_stream=True)
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status_text"], "encoder open failed")

    def test_early_rejection_sends_no_pcm(self):
        dev = make_device(eb.RSP_MAGIC + response(status=3))
        dev.encode(b"\x00\x00" * 5000, 16000, 1, 24000, 5)
        self.assertEqual(len(dev.ser.written), 4 + 28)

    def test_device_encode_without_magic_times_out(self):
        dev = make_device(b"garbage")
        with patch.object(eb.time, "monotonic", side_effect=[0, 0, 100]):
            with self.assertRaises(eb.DeviceError):
                dev.encode(b"\x00\x00", 16000, 1, 24000, 2)


class TestMetrics(unittest.TestCase):
    def test_bit_rate_is_per_channel(self):
        self.assertEqual(eb.bit_rate_per_channel(64, 2), 32000)
        self.assertEqual(eb.bit_rate_per_channel(24, 1), 24000)

    def test_xrt_and_mhz(self):
        # 10 s of audio in 40M cycles at 160 MHz: 0.25 s of CPU, 40x real time, 4 MHz needed.
        self.assertAlmostEqual(eb.xrt(40_000_000, 10.0, 160), 40.0)
        self.assertAlmostEqual(eb.mhz_for_realtime(40_000_000, 10.0), 4.0)
        self.assertEqual(eb.xrt(0, 10.0, 160), 0.0)

    def test_actual_kbps(self):
        self.assertAlmostEqual(eb.actual_kbps(80_000, 10.0), 64.0)

    def test_stream_seconds_counts_all_frames(self):
        self.assertAlmostEqual(eb.stream_seconds(26, 1024, 32000), 0.832)
        self.assertAlmostEqual(eb.stream_seconds(15, 2048, 32000), 0.96)
        self.assertEqual(eb.stream_seconds(5, 1024, 0), 0.0)

    def test_audio_seconds(self):
        self.assertAlmostEqual(eb.audio_seconds(48000 * 4 * 3, 48000, 2), 3.0)

    def test_clip_seconds_trims_to_target(self):
        self.assertEqual(eb.clip_seconds("esp32s3", 16000, 1, 10), 10)
        small = eb.clip_seconds("esp32c6", 48000, 2, 10)
        self.assertLess(small, 10)
        self.assertLessEqual(small * 48000 * 4, eb.MAX_PCM_BYTES["esp32c6"])

    def test_cut_command(self):
        cmd = eb.cut_command("a.wav", 2.5, 44100, 2)
        self.assertEqual(cmd[:1], ["ffmpeg"])
        self.assertEqual(cmd[cmd.index("-t") + 1], "2.5")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "44100")
        self.assertEqual(cmd[-3:], ["-f", "s16le", "-"])
        self.assertEqual(cmd[cmd.index("-ss") + 1], "0")
        late = eb.cut_command("a.wav", 2, 16000, 1, start=7.5)
        self.assertEqual(late[late.index("-ss") + 1], "7.5")


class TestAdts(unittest.TestCase):
    def test_frame_lengths(self):
        data = adts_frame(100) + adts_frame(7) + adts_frame(2000, 5)
        self.assertEqual(eb.adts_frame_lengths(data), [100, 7, 2000])

    def test_broken_framing(self):
        self.assertIsNone(eb.adts_frame_lengths(adts_frame(100)[:-1]))
        self.assertIsNone(eb.adts_frame_lengths(b"\x00" * 20))
        self.assertEqual(eb.adts_frame_lengths(b""), [])

    def test_check_stream(self):
        stream = adts_frame(50) + adts_frame(60)
        res = eb.unpack_response(response(frames=2, out_bytes=len(stream), crc=zlib.crc32(stream)))
        self.assertEqual(eb.check_stream(stream, res), {"crc_ok": True, "size_ok": True, "adts_frames": 2, "frames_ok": True})
        bad = eb.check_stream(stream[:-1] + b"\x01", res)
        self.assertFalse(bad["crc_ok"])
        self.assertIsNone(eb.check_stream(stream[:-3], res)["adts_frames"])


class TestSnr(unittest.TestCase):
    def test_lag_snr_finds_delay(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy missing")
        rng = np.random.default_rng(1)
        ref = (rng.standard_normal(40000) * 3000).astype("<i2")
        dec = np.concatenate([np.zeros(1100, dtype="<i2"), ref])[:40000]
        self.assertGreater(eb.lag_snr(ref.tobytes(), dec.tobytes(), 1), 60)

    def test_too_short_is_none(self):
        self.assertIsNone(eb.lag_snr(b"\x00\x01" * 100, b"\x00\x01" * 100, 1))


class TestResults(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        patcher = patch.object(eb, "RESULTS_DIR", self.td.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_results_path_and_roundtrip(self):
        self.assertTrue(eb.results_path("esp32c6").endswith("esp32_enc_esp32c6.json"))
        self.assertEqual(eb.load_results("esp32c6"), {"target": "esp32c6", "runs": [], "footprint": None})
        eb.save_results("esp32c6", {"target": "esp32c6", "runs": [1], "footprint": {"flash_total": 5}})
        self.assertEqual(eb.load_results("esp32c6")["runs"], [1])

    def row(self, **kw):
        r = {"profile": "lc", "scenario": "48k_stereo_64k", "filename": "a.wav", "seconds": 3.0,
             "audio_seconds": 3.0, "status": 0, "status_text": "ok", "object_type": 2, "target_kbps": 64,
             "actual_kbps": 60.0, "xrt": 2.0, "mhz_for_1x": 80.0, "init_heap_used": 100_000,
             "heap_min_free_delta": 110_000, "stack_hwm": 20_000, "decodes": True, "crc_ok": True,
             "frames_ok": True, "snr_db": 18.2}
        r.update(kw)
        return r

    def test_row_key_includes_seconds_and_start(self):
        self.assertNotEqual(eb.row_key(self.row()), eb.row_key(self.row(seconds=5.0)))
        self.assertNotEqual(eb.row_key(self.row()), eb.row_key(self.row(start=4.0)))
        self.assertEqual(eb.row_key(self.row()), eb.row_key(self.row(start=0.0)))

    def test_report(self):
        data = {"target": "esp32c6", "runs": [self.row(), self.row(profile="he", scenario="x", status=3, status_text="encoder open failed")],
                "footprint": {"flash_total": 51486}}
        eb.save_results("esp32c6", data)
        out = StringIO()
        with redirect_stdout(out):
            eb.cmd_report(Namespace(target="esp32c6", output=None))
        text = out.getvalue()
        self.assertIn("| lc | 1 | 2.00 | 2.00 | 80 / 80 | 0.94x |", text)
        self.assertIn("1/1", text)
        self.assertIn("Failed: he/x (encoder open failed)", text)
        self.assertIn("flash_total", text)


class TestSelect(unittest.TestCase):
    def test_gate_selection_skips_surround_and_limits_clips(self):
        clips = eb.select_clips(["48k_stereo_64k", "16k_mono_24k"], gate=True, per_scenario=2)
        by = {}
        for c in clips:
            by.setdefault(c["scenario"], []).append(c)
        for name, cs in by.items():
            self.assertLessEqual(len(cs), 2)
        if clips:
            c = next(c for c in clips if c["scenario"] == "48k_stereo_64k")
            self.assertEqual((c["rate"], c["channels"], c["target_kbps"]), (48000, 2, 64))

    def test_unknown_scenario_gives_nothing(self):
        self.assertEqual(eb.select_clips(["nope"]), [])


class ProfileApplicabilityTests(unittest.TestCase):
    def test_he_needs_32khz(self):
        self.assertFalse(eb.profile_applies("he", 16000))
        self.assertFalse(eb.profile_applies("he", 24000))
        self.assertTrue(eb.profile_applies("he", 32000))
        self.assertTrue(eb.profile_applies("lc", 16000))

    def test_clamp_detection(self):
        self.assertTrue(eb.clamped_by_library({"target_kbps": 320, "channels": 2, "bit_rate": 144000}))
        self.assertFalse(eb.clamped_by_library({"target_kbps": 64, "channels": 2, "bit_rate": 32000}))
        self.assertFalse(eb.clamped_by_library({"target_kbps": 64, "channels": 2}))


if __name__ == "__main__":
    unittest.main()
