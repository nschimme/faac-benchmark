import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = Path(os.environ.get("FAAC_BENCHMARK_ROOT", ROOT)).resolve()
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))
spec = importlib.util.spec_from_file_location(
    "storage_ab", ROOT / "scripts" / "storage_ab.py")
storage_ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage_ab)


class FakeDecoder:
    name = "fake"
    tool_id = "fake"

    def get_decode_cmd(self, input_path, output_path):
        return ["fake-faad", "-o", output_path, input_path]

    def get_run_env(self):
        return {}


class TestStorageAB(unittest.TestCase):
    def test_seconds_and_dest_args_reject_invalid_values(self):
        for value in ("0", "-1", "nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(Exception):
                storage_ab.positive_finite_seconds(value)
        self.assertTrue(math.isfinite(storage_ab.positive_finite_seconds("300")))
        self.assertEqual(storage_ab.parse_destination("nas=/mnt/data"),
                         ("nas", Path("/mnt/data")))
        with self.assertRaises(Exception):
            storage_ab.parse_destination("no-separator")
        with self.assertRaises(Exception):
            storage_ab.parse_destination("dev_null=/tmp")

    def test_cpu_and_wall_timing_boundaries_use_direct_child_delta(self):
        usage = [SimpleNamespace(ru_utime=1.0, ru_stime=0.25),
                 SimpleNamespace(ru_utime=1.1, ru_stime=0.30)]

        class FakeResource:
            RUSAGE_CHILDREN = object()

            @staticmethod
            def getrusage(_):
                return usage.pop(0)

        ticks = iter((10.0, 10.05))
        proc = SimpleNamespace(returncode=0, stderr=b"")
        called = []

        def runner(command, **kwargs):
            called.append((command, kwargs))
            return proc

        result, wall_ms, cpu_ms = storage_ab.run_measured_command(
            ["fake-faad"], runner=runner, clock=lambda: next(ticks),
            resource_module=FakeResource)
        self.assertIs(result, proc)
        self.assertAlmostEqual(wall_ms, 50.0)
        self.assertAlmostEqual(cpu_ms, 150.0)
        self.assertEqual(called[0][0], ["fake-faad"])

    def test_local_copy_is_used_even_when_source_exceeds_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_dir, work = root / "source", root / "local"
            source_dir.mkdir(); work.mkdir()
            source = source_dir / "long.m4a"
            source.write_bytes(b"aac bytes")
            observed = {}

            @contextmanager
            def prepared(_decoder, source_arg, output_dir, seconds):
                observed.update(source=Path(source_arg).resolve(),
                                output_dir=Path(output_dir).resolve(), seconds=seconds)
                # Simulate prepared_decode_input returning its source unchanged
                # for an input that already exceeds the requested duration.
                yield {"path": source_arg, "source_duration_seconds": 900.0,
                       "timed_duration_seconds": 900.0, "looped": False}

            with patch.object(storage_ab, "prepared_decode_input", side_effect=prepared):
                with storage_ab.prepare_local_workload(FakeDecoder(), source, work, 300.0,
                                                       source_tag="case-0") as row:
                    copied, prepared_path, metadata = row
                    self.assertEqual(copied.read_bytes(), source.read_bytes())
                    self.assertEqual(prepared_path, copied.resolve())
                    self.assertFalse(metadata["looped"])
            self.assertNotEqual(observed["source"], source.resolve())
            self.assertEqual(observed["source"].parent, work.resolve())
            self.assertEqual(observed["seconds"], 300.0)

    def _args(self, root, source, report, roots):
        argv = ["--faad", "fake-faad", "--inputs", str(source), "--json", str(report),
                "--iterations", "1"]
        for label, directory in roots:
            argv.extend(("--dest", f"{label}={directory}"))
        return storage_ab.build_parser().parse_args(argv)

    def _patch_run_dependencies(self, fake_attempt):
        @contextmanager
        def fake_prepare(decoder, source, work_dir, audio_seconds, source_tag="input"):
            copied = storage_ab.copy_source_to_local(source, work_dir, source_tag)
            yield copied, copied.resolve(), {"path": str(copied),
                                             "source_duration_seconds": 1.0,
                                             "timed_duration_seconds": 1.0,
                                             "looped": False}

        return (
            patch.object(storage_ab, "make_decoder", return_value=FakeDecoder()),
            patch.object(storage_ab, "decoder_identity", return_value={"binary": {"sha256": "fake"}}),
            patch.object(storage_ab, "prepare_local_workload", side_effect=fake_prepare),
            patch.object(storage_ab, "measure_attempt", side_effect=fake_attempt),
        )

    def test_failed_attempt_keeps_report_and_cleans_owned_destination_scratch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "input.m4a"; source.write_bytes(b"input")
            destination = root / "destination"; destination.mkdir()
            report_path = root / "failure.json"
            args = self._args(root, source, report_path, [("local", destination)])

            def fail(*_args, **_kwargs):
                raise RuntimeError("synthetic timed decode failure")

            patches = self._patch_run_dependencies(fail)
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaisesRegex(RuntimeError, "synthetic timed decode failure"):
                    storage_ab.run(args)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "failed")
            self.assertIn("synthetic timed decode failure", report["error"]["message"])
            self.assertEqual(list(destination.iterdir()), [])

    def test_pcm_mismatch_keeps_attempts_and_failure_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "input.m4a"; source.write_bytes(b"input")
            first, second = root / "first", root / "second"
            first.mkdir(); second.mkdir()
            report_path = root / "mismatch.json"
            args = self._args(root, source, report_path,
                              [("first", first), ("second", second)])

            def attempt(_decoder, _input, scratch, is_null=False):
                digest = "a" if Path(scratch).parent.resolve() == first.resolve() else "b"
                return {"mode": "wav_file", "wall_ms": 10.0, "cpu_ms": 5.0,
                        "peak_rss_kb": None,
                        "pcm_identity": {"format": (1, 2, 48000, 4, 16, 16, 0),
                                         "pcm_bytes": 16, "pcm_sha256": digest}}

            patches = self._patch_run_dependencies(attempt)
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaisesRegex(RuntimeError, "PCM mismatch"):
                    storage_ab.run(args)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "failed")
            warmup = report["inputs"][0]["warmup"]
            self.assertEqual(set(warmup["results"]), {"first", "second"})
            self.assertFalse(warmup["pcm_matches"]["second"])
            self.assertEqual(list(first.iterdir()), [])
            self.assertEqual(list(second.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
