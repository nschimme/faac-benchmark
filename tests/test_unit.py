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
        picked = self.gate_filter("48k_stereo_64k", self.music)
        self.assertIn("sandman.16b48k.wav", picked)
        self.assertIn("velvet.16b48k.wav", picked)

    def test_unknown_scenario_returns_nonempty_subset(self):
        fb = self.gate_filter("does_not_exist", self.music)
        self.assertTrue(0 < len(fb) <= len(self.music))

    def test_empty_input_returns_empty(self):
        self.assertEqual([], self.gate_filter("48k_stereo_64k", []))


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
                "r_c1.wav": {"mos": 3.5, "scenario": "48k_stereo_64k", "filename": "c1.wav",
                             "bitrate": 64, "time": 1.0},
                "r_c2.wav": {"mos": 3.0, "scenario": "48k_stereo_64k", "filename": "c2.wav",
                             "bitrate": 64, "time": 1.0},
            }})
            save_results(b, {"matrix": {
                "r_c1.wav": {"mos": 2.5, "scenario": "48k_stereo_64k", "filename": "c1.wav",
                             "bitrate": 64, "time": 1.0},
                "r_c2.wav": {"mos": 3.6, "scenario": "48k_stereo_64k", "filename": "c2.wav",
                             "bitrate": 64, "time": 1.0},
            }})
            r = subprocess.run([sys.executable, "compare_clips.py", a, b],
                               cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("48k_stereo_64k", r.stdout)
            self.assertIn("worst", r.stdout)


from unittest.mock import patch, MagicMock

class TestElfSectionSizes(unittest.TestCase):
    def test_nonexistent_file(self):
        from utils import get_elf_section_sizes
        res = get_elf_section_sizes("/does/not/exist")
        self.assertEqual(res, {"text": 0, "rodata": 0, "bss": 0, "data": 0})

    @patch("utils.sys.platform", "linux")
    @patch("subprocess.run")
    def test_size_a_parsing(self, mock_run):
        from utils import get_elf_section_sizes
        # Mock size -A output
        mock_res = MagicMock()
        mock_res.stdout = """/path/to/binary  :
section               size    addr
.text                 12345   1000
.rodata                5678   2000
.data                   999   3000
.bss                    111   4000
Total                 19133
"""
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        with tempfile.NamedTemporaryFile() as tmp:
            res = get_elf_section_sizes(tmp.name)
            self.assertEqual(res, {"text": 12345, "rodata": 5678, "bss": 111, "data": 999})

    @patch("utils.sys.platform", "linux")
    @patch("subprocess.run")
    def test_berkeley_fallback(self, mock_run):
        from utils import get_elf_section_sizes
        # First call to size -A fails or returns empty, second call to size succeeds
        mock_res_a = MagicMock()
        mock_res_a.stdout = ""
        mock_res_a.returncode = 0

        mock_res_berkeley = MagicMock()
        mock_res_berkeley.stdout = """   text	   data	    bss	    dec	    hex	filename
  20000	   1500	    300	  21800	   5528	/path/to/binary
"""
        mock_res_berkeley.returncode = 0

        mock_run.side_effect = [mock_res_a, mock_res_berkeley]

        with tempfile.NamedTemporaryFile() as tmp:
            res = get_elf_section_sizes(tmp.name)
            self.assertEqual(res, {"text": 20000, "rodata": 0, "bss": 300, "data": 1500})
class TestFootprintGate(unittest.TestCase):
    """The footprint gate exists because lib_size was never wired to the
    verdict. These pin the verdict, not the byte counts."""

    def _run(self, base_code, cand_code, allow=0, base_fp=None, cand_fp=None):
        import compare_results as C
        C.FOOTPRINT_ALLOW = allow
        sr = {"gates": [], "has_regression": False}
        fp = {"cc": "gcc"}
        C.check_footprint(
            sr,
            {"lib_sections": {"text": base_code, "rodata": 0},
             "toolchain_fp": base_fp or fp},
            {"lib_sections": {"text": cand_code, "rodata": 0},
             "toolchain_fp": cand_fp or fp})
        C.FOOTPRINT_ALLOW = 0
        return sr

    def test_known_regression_fails(self):
        # f94a81a8: .text+.rodata 76676 -> 80180.
        sr = self._run(76676, 80180)
        self.assertEqual(sr["gates"][0]["status"], "fail")
        self.assertTrue(sr["has_regression"])

    def test_routine_growth_passes(self):
        # Largest routine commit in the 30-commit replay was +1556.
        sr = self._run(76676, 76676 + 1556)
        self.assertIn(sr["gates"][0]["status"], ("pass", "warn"))
        self.assertFalse(sr["has_regression"])

    def test_shrink_passes(self):
        sr = self._run(80180, 76676)
        self.assertEqual(sr["gates"][0]["status"], "pass")

    def test_acknowledged_growth_does_not_fail(self):
        sr = self._run(76676, 80180, allow=4000)
        self.assertEqual(sr["gates"][0]["status"], "warn")
        self.assertFalse(sr["has_regression"])

    def test_toolchain_mismatch_skips_visibly(self):
        sr = self._run(76676, 80180, base_fp={"cc": "gcc-13"},
                       cand_fp={"cc": "gcc-14"})
        self.assertEqual(sr["gates"][0]["status"], "skip")
        self.assertFalse(sr["has_regression"])
        self.assertIn("toolchain differs", sr["gates"][0]["detail"])


class TestThroughputGate(unittest.TestCase):
    def _run(self, base, cand, host_differs=False):
        import compare_results as C
        sr = {"gates": [], "has_regression": False}
        C.check_throughput(
            sr,
            {"throughput_samples": {"s": base}, "host_fp": {"cpu": "a"}},
            {"throughput_samples": {"s": cand},
             "host_fp": {"cpu": "b" if host_differs else "a"}})
        return sr["gates"][0]

    BASE = [1.00, 1.01, 1.02, 1.00, 1.03]

    def test_identical_passes(self):
        self.assertEqual(self._run(self.BASE, self.BASE)["status"], "pass")

    def test_large_slowdown_fails(self):
        # Pre-gate TNS cost +20.9%.
        slow = [x * 1.209 for x in self.BASE]
        self.assertEqual(self._run(self.BASE, slow)["status"], "fail")

    def test_deliberate_small_trade_warns(self):
        # Post-gate TNS cost +5.5%: visible, but not blocking.
        slow = [x * 1.055 for x in self.BASE]
        self.assertEqual(self._run(self.BASE, slow)["status"], "warn")

    def test_noisy_runner_does_not_fail_clean_change(self):
        # Same true minimum, heavy one-sided interference in both arms.
        noisy_b = [1.00, 1.5, 1.02, 2.0, 1.03]
        noisy_c = [1.00, 1.9, 1.02, 1.7, 1.03]
        self.assertEqual(self._run(noisy_b, noisy_c)["status"], "pass")

    def test_host_mismatch_skips_visibly(self):
        slow = [x * 1.5 for x in self.BASE]
        g = self._run(self.BASE, slow, host_differs=True)
        self.assertEqual(g["status"], "skip")
        # Assert on the cause and the remedy, not the sentence: a flag name is
        # a stable contract where prose is not.
        self.assertIn("different machine", g["detail"])
        self.assertIn("--throughput-only", g["detail"])


if __name__ == "__main__":
    unittest.main()
