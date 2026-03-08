import os
import subprocess
import json
import shutil
import unittest
import sys

class TestE2EMock(unittest.TestCase):
    def setUp(self):
        self.test_dir = "test_e2e_tmp"
        os.makedirs(self.test_dir, exist_ok=True)
        self.data_dir = os.path.join(self.test_dir, "data", "external")
        os.makedirs(os.path.join(self.data_dir, "speech"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "audio"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "throughput"), exist_ok=True)

        # Create mock samples
        for d in ["speech", "audio"]:
            for i in range(3):
                with open(os.path.join(self.data_dir, d, f"sample_{i}.wav"), "w") as f:
                    f.write("mock")

        with open(os.path.join(self.data_dir, "throughput", "sine.wav"), "w") as f:
            f.write("mock")

        # Create dummy faac
        self.faac_bin = os.path.abspath(os.path.join(self.test_dir, "dummy_faac"))
        with open(self.faac_bin, "w") as f:
            # We must use absolute path for output_path because the benchmark script might run from different dirs
            f.write("#!/bin/bash\nwhile [[ $# -gt 0 ]]; do case $1 in -o) touch \"$2\"; shift 2;; *) shift;; esac; done")
        os.chmod(self.faac_bin, 0o755)

        self.lib_path = os.path.abspath(os.path.join(self.test_dir, "libfaac.so"))
        with open(self.lib_path, "w") as f:
            f.write("mock")

        self.results_dir = os.path.join(self.test_dir, "results")
        os.makedirs(self.results_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_full_workflow(self):
        # 1. Run Base Benchmark (VoIP only)
        base_json = os.path.join(self.results_dir, "test_base.json")
        cmd_base = [
            sys.executable, "run_benchmark.py",
            self.faac_bin, self.lib_path, "test", base_json,
            "--scenarios", "voip", "--sha", "base_123", "--skip-mos"
        ]
        # Set environment to point to our mock data
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        # We need to trick the scripts into using our mock data dir.
        # Since they use SCRIPT_DIR/data/external, we'll symlink it if possible or just run from here.
        # But phase1_encode.py uses SCRIPT_DIR.
        # Let's temporarily override EXTERNAL_DATA_DIR in phase1_encode if we can,
        # or just put the data in the real data/external if it's safe.

        # Better: run the scripts with a modified EXTERNAL_DATA_DIR by patching them or
        # just creating the data/external structure here.
        real_data_dir = os.path.join(os.getcwd(), "data", "external")
        os.makedirs(real_data_dir, exist_ok=True)
        # Backup if exists? No, this is a sandbox.
        shutil.copytree(self.data_dir, real_data_dir, dirs_exist_ok=True)

        subprocess.run(cmd_base, env=env, check=True)

        with open(base_json, "r") as f:
            data_base = json.load(f)
            self.assertEqual(data_base["sha"], "base_123")
            self.assertTrue(any(k.startswith("voip_") for k in data_base["matrix"]))
            self.assertFalse(any(k.startswith("vss_") for k in data_base["matrix"]))

        # 2. Run Cand Benchmark (VoIP + VSS, with filtering)
        cand_json = os.path.join(self.results_dir, "test_cand.json")
        cmd_cand = [
            sys.executable, "run_benchmark.py",
            self.faac_bin, self.lib_path, "test", cand_json,
            "--scenarios", "voip,vss", "--include-tests", "sample_0.wav,sample_1.wav",
            "--sha", "cand_456", "--skip-mos"
        ]
        subprocess.run(cmd_cand, env=env, check=True)

        with open(cand_json, "r") as f:
            data_cand = json.load(f)
            self.assertEqual(data_cand["sha"], "cand_456")
            # Should have voip and vss, but only samples 0 and 1
            keys = data_cand["matrix"].keys()
            self.assertTrue(all("sample_0.wav" in k or "sample_1.wav" in k for k in keys))
            self.assertTrue(any("voip_" in k for k in keys))
            self.assertTrue(any("vss_" in k for k in keys))

        # 3. Generate Report
        report_md = os.path.join(self.test_dir, "report.md")
        summary_md = os.path.join(self.test_dir, "summary.md")
        cmd_report = [
            sys.executable, "compare_results.py",
            self.results_dir, "--output", report_md, "--summary-output", summary_md
        ]
        # compare_results.py returns 1 if regressions/missing data are found, which is expected here
        subprocess.run(cmd_report, env=env)

        with open(report_md, "r") as f:
            content = f.read()
            self.assertIn("base_123", content)
            self.assertIn("cand_456", content)
            self.assertIn("Scenario Performance", content)
            self.assertIn("Bit-Exact", content)
            self.assertIn("Speed Δ", content)

        with open(summary_md, "r") as f:
            summary = f.read()
            self.assertIn("Regressions", summary)
            self.assertIn("Throughput", summary)

if __name__ == "__main__":
    unittest.main()
