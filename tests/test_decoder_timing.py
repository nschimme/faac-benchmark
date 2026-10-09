import os
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from codec_bench import decoders as dec
from codec_bench.report import _decode_seconds


class TestDecoderTiming(unittest.TestCase):
    def test_warmup_is_not_a_measured_sample_or_minimum(self):
        rows = [{"milliseconds": ms, "output_bytes": 100} for ms in (1, 30, 10, 20)]
        with patch.object(dec, "time_decode_once", side_effect=rows):
            stats = dec.measure_decode_speed(None, "input", ".", 3, 1, warmups=1)
        self.assertEqual(stats["samples_ms"], [30, 10, 20])
        self.assertEqual(stats["warmup_samples_ms"], [1])
        self.assertEqual(stats["best_ms"], 10)
        self.assertEqual(stats["median_ms"], 20)

    def test_one_measured_sample_after_warmup(self):
        with patch.object(dec, "time_decode_once", side_effect=[
                {"milliseconds": 1, "output_bytes": 100},
                {"milliseconds": 10, "output_bytes": 100}]):
            self.assertEqual(dec.measure_decode_speed(None, "in", ".", 1, 0, warmups=1)["samples_ms"], [10])

    def test_failure_does_not_publish_partial_timing(self):
        with patch.object(dec, "time_decode_once", side_effect=[RuntimeError("decode failed")]):
            self.assertIsNone(dec.measure_decode_speed(None, "in", ".", 3, 0, warmups=1))

    def test_timing_scales_summary_but_keeps_actual_samples(self):
        @contextmanager
        def prepared(*args):
            yield {"path": "loop", "scale": .1, "source_duration_seconds": 10,
                   "timed_duration_seconds": 100}
        stats = {"samples_ms": [100, 200], "best_ms": 100, "mean_ms": 150,
                 "median_ms": 150, "std_ms": 50}
        with patch.object(dec.os.path, "exists", return_value=True), \
             patch.object(dec, "prepared_decode_input", side_effect=prepared), \
             patch.object(dec, "measure_decode_speed", return_value=stats):
            result = dec.time_decoder_serial(None, {"aac_path": "in"}, ".", 2, return_stats=True)
        self.assertEqual(result["samples_ms"], [10, 20])
        self.assertEqual(result["unscaled_samples_ms"], [100, 200])
        self.assertEqual(result["median_ms"], 15)

    def test_report_prefers_median_and_reads_legacy_rows(self):
        self.assertEqual(_decode_seconds({"speed_median_ms": 20, "speed_best_ms": 10}), .02)
        self.assertEqual(_decode_seconds({"speed_best_ms": 10}), .01)

    def test_loop_failure_cleans_prepared_file(self):
        with tempfile.TemporaryDirectory() as td:
            def failed(cmd, **kwargs):
                with open(cmd[-1], "wb") as f:
                    f.write(b"bad")
                return SimpleNamespace(returncode=1)
            with patch.object(dec, "ffmpeg_probe", return_value=10), \
                 patch.object(dec, "safe_run", side_effect=failed):
                with self.assertRaisesRegex(RuntimeError, "stream-copy"):
                    with dec.prepared_decode_input(SimpleNamespace(requires_adts=False), "in.m4a", td, 60):
                        self.fail("failed preparation must not yield a workload")
            self.assertEqual(os.listdir(td), [])

    def test_macos_rpath_library_identity(self):
        with tempfile.TemporaryDirectory() as td:
            binary = os.path.join(td, "frontend", "faad")
            library = os.path.join(td, "libfaad", "libfaad.3.dylib")
            os.makedirs(os.path.dirname(binary))
            os.makedirs(os.path.dirname(library))
            with open(library, "wb") as f:
                f.write(b"library")
            outputs = [SimpleNamespace(stdout="faad:\n @rpath/libfaad.3.dylib (version)\n"),
                       SimpleNamespace(stdout="cmd LC_RPATH\n cmdsize 40\n path @loader_path/../libfaad (offset 12)\n")]
            decoder = SimpleNamespace(binary_path=binary, lib_override=None, lib_name_substr="libfaad")
            with patch.object(dec.sys, "platform", "darwin"), \
                 patch.object(dec, "resolve_wrapper_target", return_value=binary), \
                 patch.object(dec.subprocess, "run", side_effect=outputs), \
                 patch.dict(os.environ, {"DYLD_LIBRARY_PATH": ""}):
                self.assertEqual(dec.resolved_decoder_library(decoder), os.path.realpath(library))


if __name__ == "__main__":
    unittest.main()
