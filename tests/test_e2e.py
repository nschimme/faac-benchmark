"""End-to-end integration test using a mock FAAC binary and dummy corpus."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import REPO, write_wav

sys.path.insert(0, REPO)

SAMPLE_COUNT = 3


class TestE2EMock(unittest.TestCase):
    # ------------------------------------------------------------------
    # Fixture setup / teardown
    # ------------------------------------------------------------------

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="faac_e2e_")
        self.data_dir = os.path.join(self.test_dir, "data", "external")
        for sub in ("speech", "audio", "throughput"):
            os.makedirs(os.path.join(self.data_dir, sub), exist_ok=True)

        self._create_corpus()
        self.faac_bin = self._create_dummy_faac()
        self.lib_path = os.path.join(self.test_dir, "libfaac.so")
        with open(self.lib_path, "w") as f:
            f.write("mock")
        self.results_dir = os.path.join(self.test_dir, "results")
        os.makedirs(self.results_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_corpus(self):
        for i in range(SAMPLE_COUNT):
            write_wav(os.path.join(self.data_dir, "audio", f"sample_{i}.wav"), sr=48000, ch=2)
            write_wav(os.path.join(self.data_dir, "speech", f"sample_{i}.wav"), sr=16000, ch=1)
        # Minimal throughput stub (phase1 only checks existence)
        with open(os.path.join(self.data_dir, "throughput", "sine.wav"), "w") as f:
            f.write("mock")

    def _create_dummy_faac(self) -> str:
        """Shell stub that copies its WAV input to the AAC output path."""
        path = os.path.join(self.test_dir, "dummy_faac")
        with open(path, "w") as f:
            f.write(
                "#!/bin/bash\n"
                "output=\"\"\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  case $1 in\n"
                "    -o) output=\"$2\"; shift 2;;\n"
                "    -*) shift 2;;\n"
                "    *) input=\"$1\"; shift;;\n"
                "  esac\n"
                "done\n"
                "cp \"$input\" \"$output\"\n"
            )
        os.chmod(path, 0o755)
        return path

    def _make_env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONPATH"] = REPO
        env["EXTERNAL_DATA_DIR"] = self.data_dir
        return env

    def _run(self, cmd: list, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=REPO, env=self._make_env(),
                              capture_output=True, text=True, **kwargs)

    # ------------------------------------------------------------------
    # Step helpers (called in sequence by test_full_workflow)
    # ------------------------------------------------------------------

    def _run_base_benchmark(self) -> dict:
        base_json = os.path.join(self.results_dir, "test_base.json")
        r = self._run([
            sys.executable, "run_benchmark.py",
            self.faac_bin, self.lib_path, "test", base_json,
            "--scenarios", "voip", "--sha", "base_123", "--skip-mos",
        ], check=True)
        with open(base_json) as f:
            return json.load(f), base_json

    def _assert_base_results(self, data: dict):
        self.assertEqual(data["sha"], "base_123")
        self.assertTrue(any(k.startswith("voip_") for k in data["matrix"]),
                        "expected voip keys in base results")
        self.assertFalse(any(k.startswith("vss_") for k in data["matrix"]),
                         "vss should not appear in base results")

    def _run_cand_benchmark(self) -> dict:
        cand_json = os.path.join(self.results_dir, "test_cand.json")
        self._run([
            sys.executable, "run_benchmark.py",
            self.faac_bin, self.lib_path, "test", cand_json,
            "--scenarios", "voip,vss,music_std",
            "--include-tests", "sample_0.wav,sample_1.wav",
            "--sha", "cand_456", "--skip-mos",
        ], check=True)
        with open(cand_json) as f:
            return json.load(f), cand_json

    def _assert_cand_results(self, data: dict):
        self.assertEqual(data["sha"], "cand_456")
        keys = list(data["matrix"])
        self.assertTrue(all("sample_0.wav" in k or "sample_1.wav" in k for k in keys),
                        "only sample_0 and sample_1 should appear (filter active)")
        self.assertTrue(any("voip_" in k for k in keys), "voip scenario missing")
        self.assertTrue(any("vss_" in k for k in keys), "vss scenario missing")
        self.assertTrue(any("music_std_" in k for k in keys), "music_std scenario missing")
        for mk in (k for k in keys if "music_std_" in k):
            self.assertIn("ic_err", data["matrix"][mk],
                          f"Phase 3 ic_err missing for stereo key {mk}")

    def _run_report(self) -> tuple[str, str]:
        report_md = os.path.join(self.test_dir, "report.md")
        summary_md = os.path.join(self.test_dir, "summary.md")
        # compare_results.py exits 1 when regressions exist — that's expected here.
        self._run([
            sys.executable, "compare_results.py",
            self.results_dir, "--output", report_md, "--summary-output", summary_md,
        ])
        return report_md, summary_md

    def _assert_report(self, report_md: str, summary_md: str):
        with open(report_md) as f:
            content = f.read()
        self.assertIn("base_123", content)
        self.assertIn("cand_456", content)
        self.assertIn("Scenario Performance", content)
        self.assertIn("Bit-Exact", content)
        self.assertIn("Speed Δ", content)

        with open(summary_md) as f:
            summary = f.read()
        self.assertIn("Regressions", summary)
        self.assertIn("Throughput", summary)

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    def test_full_workflow(self):
        with self.subTest(step="base benchmark"):
            base_data, _ = self._run_base_benchmark()
            self._assert_base_results(base_data)

        with self.subTest(step="candidate benchmark with filter"):
            cand_data, _ = self._run_cand_benchmark()
            self._assert_cand_results(cand_data)

        with self.subTest(step="report generation"):
            report_md, summary_md = self._run_report()
            self._assert_report(report_md, summary_md)


if __name__ == "__main__":
    unittest.main()
