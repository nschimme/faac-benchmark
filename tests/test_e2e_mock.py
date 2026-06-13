import os
import subprocess
import json
import shutil
import unittest
import sys
import wave
import struct
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _write_wav(path, seconds=1, sr=48000, ch=2):
    with wave.open(path, "w") as f:
        f.setnchannels(ch)
        f.setsampwidth(2)
        f.setframerate(sr)
        for j in range(sr * seconds):
            for _ in range(ch):
                f.writeframes(struct.pack('<h', (j % 1000) - 500))


class TestEnhancements(unittest.TestCase):
    """Unit tests for the ergonomics/correctness enhancements (no ViSQOL needed)."""

    def test_decode_validate_clean_vs_corrupt(self):
        from utils import decode_validate
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "a.wav")
            _write_wav(wav)
            ok, err = decode_validate(wav)
            self.assertTrue(ok, f"clean wav should pass, got: {err}")
            # Corrupt: truncated/garbage file ffmpeg cannot decode cleanly.
            bad = os.path.join(td, "bad.aac")
            with open(bad, "wb") as f:
                f.write(b"\xff\xf1" + os.urandom(64))
            ok2, err2 = decode_validate(bad)
            self.assertFalse(ok2, "garbage should fail decode validation")
            self.assertTrue(err2)

    def test_provenance_hash_sensitivity(self):
        from utils import calculate_provenance_hash
        with tempfile.TemporaryDirectory() as td:
            binp = os.path.join(td, "faac")
            with open(binp, "wb") as fh:
                fh.write(b"BIN")
            lib = os.path.join(td, "lib")
            with open(lib, "wb") as fh:
                fh.write(b"LIB")
            inp = os.path.join(td, "in.wav"); _write_wav(inp)
            h0 = calculate_provenance_hash(binp, lib, "--pns 2", inp, env={})
            self.assertEqual(h0, calculate_provenance_hash(binp, lib, "--pns 2", inp, env={}))
            # Args change → hash changes
            self.assertNotEqual(h0, calculate_provenance_hash(binp, lib, "--pns 4", inp, env={}))
            # FAAC_* env change → hash changes; unrelated env does not
            self.assertNotEqual(h0, calculate_provenance_hash(binp, lib, "--pns 2", inp, env={"FAAC_SBR_Q": "6"}))
            self.assertEqual(h0, calculate_provenance_hash(binp, lib, "--pns 2", inp, env={"PATH": "/x"}))

    def test_gate_filter(self):
        from phase1_encode import gate_filter
        music = ["sandman.16b48k.wav", "velvet.16b48k.wav", "x.wav", "y.wav", "z.wav"]
        picked = gate_filter("music_low", music)
        self.assertIn("sandman.16b48k.wav", picked)
        self.assertIn("velvet.16b48k.wav", picked)
        # Unknown scenario → deterministic non-empty fallback subset
        fb = gate_filter("does_not_exist", music)
        self.assertTrue(0 < len(fb) <= len(music))
        self.assertEqual([], gate_filter("music_low", []))

    def test_sweep_rejects_bitrate(self):
        r = subprocess.run(
            [sys.executable, "run_benchmark.py", "f", "l", "n", "out.json", "--sweep", "-b=40,48"],
            cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("scenario", (r.stdout + r.stderr).lower())

    def test_compare_clips_ranking(self):
        from utils import save_results
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "a.json"); b = os.path.join(td, "b.json")
            save_results(a, {"matrix": {
                "r_c1.wav": {"mos": 3.5, "scenario": "music_low", "filename": "c1.wav", "bitrate": 64, "time": 1.0},
                "r_c2.wav": {"mos": 3.0, "scenario": "music_low", "filename": "c2.wav", "bitrate": 64, "time": 1.0}}})
            save_results(b, {"matrix": {
                "r_c1.wav": {"mos": 2.5, "scenario": "music_low", "filename": "c1.wav", "bitrate": 64, "time": 1.0},
                "r_c2.wav": {"mos": 3.6, "scenario": "music_low", "filename": "c2.wav", "bitrate": 64, "time": 1.0}}})
            r = subprocess.run([sys.executable, "compare_clips.py", a, b],
                               cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("music_low", r.stdout)
            self.assertIn("worst", r.stdout)  # c1 regressed -1.0


class TestE2EMock(unittest.TestCase):
    def setUp(self):
        self.test_dir = "test_e2e_tmp"
        os.makedirs(self.test_dir, exist_ok=True)
        self.data_dir = os.path.join(self.test_dir, "data", "external")
        os.makedirs(os.path.join(self.data_dir, "speech"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "audio"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "throughput"), exist_ok=True)

        # Create mock samples
        import wave
        import struct
        for d in ["speech", "audio"]:
            for i in range(3):
                path = os.path.join(self.data_dir, d, f"sample_{i}.wav")
                with wave.open(path, "w") as f:
                    f.setnchannels(2 if d == "audio" else 1)
                    f.setsampwidth(2)
                    f.setframerate(48000 if d == "audio" else 16000)
                    # Write 1 second of noise or silence
                    for j in range(48000 if d == "audio" else 16000):
                        f.writeframes(struct.pack('<h', j % 1000))
                        if d == "audio":
                            f.writeframes(struct.pack('<h', (j + 500) % 1000))

        with open(os.path.join(self.data_dir, "throughput", "sine.wav"), "w") as f:
            f.write("mock")

        # Create dummy faac
        self.faac_bin = os.path.abspath(os.path.join(self.test_dir, "dummy_faac"))
        with open(self.faac_bin, "w") as f:
            # Copy input to output so that it's a valid wav (renamed to aac)
            f.write("#!/bin/bash\noutput=\"\"\nwhile [[ $# -gt 0 ]]; do case $1 in -o) output=\"$2\"; shift 2;; -*) shift 2;; *) input=\"$1\"; shift;; esac; done\ncp \"$input\" \"$output\"")
        os.chmod(self.faac_bin, 0o755)

        self.lib_path = os.path.abspath(os.path.join(self.test_dir, "libfaac.so"))
        with open(self.lib_path, "w") as f:
            f.write("mock")

        self.results_dir = os.path.join(self.test_dir, "results")
        os.makedirs(self.results_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir)
        # test_full_workflow copies mock samples into the real data/external dir
        # (the scripts resolve corpus paths relative to SCRIPT_DIR). Remove them
        # so they can't pollute real benchmark runs or leak into git.
        real = os.path.join(REPO, "data", "external")
        for sub in ("audio", "speech"):
            for i in range(3):
                p = os.path.join(real, sub, f"sample_{i}.wav")
                if os.path.exists(p):
                    os.remove(p)
        sine = os.path.join(real, "throughput", "sine.wav")
        if os.path.exists(sine) and os.path.getsize(sine) < 100:  # mock stub only
            os.remove(sine)

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

        # 2. Run Cand Benchmark (VoIP + VSS + music_std, with filtering)
        cand_json = os.path.join(self.results_dir, "test_cand.json")
        cmd_cand = [
            sys.executable, "run_benchmark.py",
            self.faac_bin, self.lib_path, "test", cand_json,
            "--scenarios", "voip,vss,music_std", "--include-tests", "sample_0.wav,sample_1.wav",
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
            self.assertTrue(any("music_std_" in k for k in keys))

            # Verify Phase 3 (stereo) results are present for music_std
            music_keys = [k for k in keys if "music_std_" in k]
            for mk in music_keys:
                self.assertIn("ic_err", data_cand["matrix"][mk])

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
