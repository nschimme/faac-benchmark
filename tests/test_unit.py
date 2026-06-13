"""Unit tests — no ViSQOL needed, fast."""

import os
import subprocess
import sys
import tempfile
import unittest

from helpers import REPO, write_wav

sys.path.insert(0, REPO)


class TestDecodeValidate(unittest.TestCase):
    def test_clean_wav_passes(self):
        from utils import decode_validate
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "a.wav")
            write_wav(wav)
            ok, err = decode_validate(wav)
            self.assertTrue(ok, f"clean wav should pass, got: {err}")

    def test_corrupt_file_fails(self):
        from utils import decode_validate
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "bad.aac")
            with open(bad, "wb") as f:
                f.write(b"\xff\xf1" + os.urandom(64))
            ok, err = decode_validate(bad)
            self.assertFalse(ok, "garbage should fail decode validation")
            self.assertTrue(err)


class TestProvenanceHash(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self.binp = os.path.join(td, "faac")
        with open(self.binp, "wb") as f:
            f.write(b"BIN")
        self.lib = os.path.join(td, "lib")
        with open(self.lib, "wb") as f:
            f.write(b"LIB")
        self.inp = os.path.join(td, "in.wav")
        write_wav(self.inp)

    def tearDown(self):
        self._td.cleanup()

    def _hash(self, args="--pns 2", env=None):
        from utils import calculate_provenance_hash
        return calculate_provenance_hash(self.binp, self.lib, args, self.inp, env=env or {})

    def test_deterministic(self):
        self.assertEqual(self._hash(), self._hash())

    def test_args_change_hash(self):
        self.assertNotEqual(self._hash("--pns 2"), self._hash("--pns 4"))

    def test_faac_env_changes_hash(self):
        self.assertNotEqual(self._hash(), self._hash(env={"FAAC_SBR_Q": "6"}))

    def test_unrelated_env_ignored(self):
        self.assertEqual(self._hash(), self._hash(env={"PATH": "/x"}))


class TestGateFilter(unittest.TestCase):
    def setUp(self):
        from phase1_encode import gate_filter
        self.gate_filter = gate_filter
        self.music = ["sandman.16b48k.wav", "velvet.16b48k.wav", "x.wav", "y.wav", "z.wav"]

    def test_known_scenario_includes_fixtures(self):
        picked = self.gate_filter("music_low", self.music)
        self.assertIn("sandman.16b48k.wav", picked)
        self.assertIn("velvet.16b48k.wav", picked)

    def test_unknown_scenario_returns_nonempty_subset(self):
        fb = self.gate_filter("does_not_exist", self.music)
        self.assertTrue(0 < len(fb) <= len(self.music))

    def test_empty_input_returns_empty(self):
        self.assertEqual([], self.gate_filter("music_low", []))


class TestSweepRejectsBitrate(unittest.TestCase):
    def test_sweep_without_scenarios_fails(self):
        r = subprocess.run(
            [sys.executable, "run_benchmark.py", "f", "l", "n", "out.json", "--sweep", "-b=40,48"],
            cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("scenario", (r.stdout + r.stderr).lower())


class TestCompareClipsRanking(unittest.TestCase):
    def test_worst_regression_reported(self):
        from utils import save_results
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "a.json")
            b = os.path.join(td, "b.json")
            save_results(a, {"matrix": {
                "r_c1.wav": {"mos": 3.5, "scenario": "music_low", "filename": "c1.wav",
                             "bitrate": 64, "time": 1.0},
                "r_c2.wav": {"mos": 3.0, "scenario": "music_low", "filename": "c2.wav",
                             "bitrate": 64, "time": 1.0},
            }})
            save_results(b, {"matrix": {
                "r_c1.wav": {"mos": 2.5, "scenario": "music_low", "filename": "c1.wav",
                             "bitrate": 64, "time": 1.0},
                "r_c2.wav": {"mos": 3.6, "scenario": "music_low", "filename": "c2.wav",
                             "bitrate": 64, "time": 1.0},
            }})
            r = subprocess.run([sys.executable, "compare_clips.py", a, b],
                               cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("music_low", r.stdout)
            self.assertIn("worst", r.stdout)


if __name__ == "__main__":
    unittest.main()
