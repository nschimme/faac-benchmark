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
            bad = os.path.join(td, "bad.m4a")
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


class TestScenarioMatrix(unittest.TestCase):
    """Invariants a new scenario or corpus must not break."""

    def setUp(self):
        from config import SCENARIOS, CORPORA, FAMILY_ORDER, METRIC_RATE
        self.SCENARIOS, self.CORPORA = SCENARIOS, CORPORA
        self.FAMILY_ORDER, self.METRIC_RATE = FAMILY_ORDER, METRIC_RATE

    def test_every_scenario_has_a_known_corpus(self):
        for name, cfg in self.SCENARIOS.items():
            self.assertIn(cfg["corpus"], self.CORPORA, f"{name} points at an unknown corpus")

    def test_corpus_directories_are_distinct(self):
        dirs = [c["dir"] for c in self.CORPORA.values()]
        self.assertEqual(len(dirs), len(set(dirs)), "two corpora share a directory")

    def test_scoring_rate_matches_metric_engine(self):
        # ViSQOL speech mode is 16 kHz mono and Zimtohrli is 48 kHz; any other
        # value is a rate neither engine accepts.
        for name, cfg in self.SCENARIOS.items():
            self.assertEqual(cfg["visqol_rate"], self.METRIC_RATE[cfg["mode"]],
                             f"{name} scores at a rate its engine does not accept")

    def test_speech_mode_is_mono(self):
        for name, cfg in self.SCENARIOS.items():
            if cfg["mode"] == "speech":
                self.assertEqual(cfg["channels"], 1, f"{name} scores stereo in speech mode")

    def test_every_family_is_ordered(self):
        for corpus_name, corpus in self.CORPORA.items():
            self.assertIn(corpus["family"], self.FAMILY_ORDER,
                          f"{corpus_name}'s family is missing from FAMILY_ORDER")


class TestScenarioSortKey(unittest.TestCase):
    def setUp(self):
        from utils import get_scenario_sort_key
        self.key = get_scenario_sort_key

    def test_decimal_rate_name_parses(self):
        # 44k1 means 44100 Hz, not 44000.
        self.assertEqual(self.key("44k1_stereo_128k")[1], 44100)

    def test_families_stay_contiguous(self):
        # Rate outranks bitrate: 32k_stereo_48k must not land beside
        # 48k_stereo_48k just because they share a bitrate.
        from config import SCENARIOS
        order = sorted(SCENARIOS, key=self.key)
        seen, last = set(), None
        for name in order:
            fam = self.key(name)[0]
            if fam != last:
                self.assertNotIn(fam, seen, f"family {fam} is split across the ordering")
                seen.add(fam)
                last = fam

    def test_retired_scenario_name_still_parses(self):
        # Archived result JSONs still name scenarios that no longer exist.
        self.assertEqual(self.key("16k_mono_40k")[1:4], (16000, 1, 40))


class TestSelectCorpusClips(unittest.TestCase):
    def setUp(self):
        from utils import select_corpus_clips
        self.select = select_corpus_clips
        self.voip = [f"C_{c:02d}_{deg}_{talker}.wav"
                     for c in range(1, 21)
                     for deg in ("CHOP", "CLIP", "ECHO", "NOISE", "COMPSPKR")
                     for talker in ("FA", "FG", "MK", "ML")]
        self.corpus = {"max_clips": 40, "strata": r"_([A-Z]+)_([A-Z]{2})\.wav$"}

    def test_under_cap_is_untouched(self):
        files = ["b.wav", "a.wav"]
        self.assertEqual(self.select(files, {"max_clips": 40}), ["a.wav", "b.wav"])

    def test_cap_is_honoured(self):
        self.assertEqual(len(self.select(self.voip, self.corpus)), 40)

    def test_every_stratum_survives(self):
        # A prefix slice would keep only CHOP/CLIP; round-robin must not drop
        # a whole degradation type or talker.
        picked = self.select(self.voip, self.corpus)
        for deg in ("CHOP", "CLIP", "ECHO", "NOISE", "COMPSPKR"):
            self.assertTrue(any(f"_{deg}_" in f for f in picked), f"{deg} dropped")
        for talker in ("FA", "FG", "MK", "ML"):
            self.assertTrue(any(f.endswith(f"_{talker}.wav") for f in picked), f"{talker} dropped")

    def test_selection_is_deterministic(self):
        self.assertEqual(self.select(self.voip, self.corpus),
                         self.select(list(reversed(self.voip)), self.corpus))

    def test_unmatched_names_fall_back_to_a_stride(self):
        files = [f"clip_{i}.wav" for i in range(100)]
        picked = self.select(files, self.corpus)
        self.assertEqual(len(picked), 40)


class TestGateClipsSurviveCorpusCap(unittest.TestCase):
    """A corpus cap must never eat a scenario's curated gate clips.

    It did: the cap ran first and 16k_mono_voip_24k's 4 gate clips came out of
    a 48-clip stratified subset that happened to contain only one of them, so
    --gate quietly ran a single clip for that scenario.
    """

    # The real TCD-VoIP listing shape: 20 conditions x 5 degradations x 4 talkers.
    VOIP_FILES = sorted(
        f"C_{c:02d}_{deg}_{talker}.wav"
        for c in range(1, 21)
        for deg in ("CHOP", "CLIP", "COMPSPKR", "ECHO", "NOISE")
        for talker in ("FA", "FG", "MK", "ML"))

    def test_capping_first_would_lose_gate_clips(self):
        # Guards the premise: if this ever stops being true the bug cannot
        # recur and the skip below is free to be simplified away.
        from config import CORPORA, GATE_CLIPS
        from utils import select_corpus_clips
        clips = GATE_CLIPS["16k_mono_voip_24k"]
        capped = select_corpus_clips(self.VOIP_FILES, CORPORA["speech_voip_16k"])
        self.assertTrue(set(clips) - set(capped),
                        "the cap now happens to keep every gate clip; premise stale")

    def test_gate_mode_keeps_every_gate_clip(self):
        from config import GATE_CLIPS
        from phase1_encode import gate_filter
        clips = GATE_CLIPS["16k_mono_voip_24k"]
        # Gate mode passes the uncapped listing through, so gate_filter sees
        # all four curated clips.
        picked = gate_filter("16k_mono_voip_24k", self.VOIP_FILES)
        self.assertEqual(sorted(picked), sorted(clips))

    def test_phase1_skips_the_cap_in_gate_mode(self):
        import inspect
        import phase1_encode
        src = inspect.getsource(phase1_encode.run_benchmark)
        self.assertIn("if gate else select_corpus_clips", src)


class TestExpandScenarioList(unittest.TestCase):
    def setUp(self):
        from utils import expand_scenario_list
        self.expand = expand_scenario_list

    def test_family_expands_to_its_members(self):
        self.assertEqual(self.expand("44k1_stereo"),
                         ["44k1_stereo_64k", "44k1_stereo_128k", "44k1_stereo_192k"])

    def test_scenario_name_passes_through(self):
        self.assertEqual(self.expand("48k_stereo_128k"), ["48k_stereo_128k"])

    def test_unknown_name_is_preserved_for_the_callers_warning(self):
        self.assertEqual(self.expand("nope"), ["nope"])

    def test_duplicates_are_dropped(self):
        self.assertEqual(self.expand("44k1_stereo,44k1_stereo_64k").count("44k1_stereo_64k"), 1)


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
            r = subprocess.run([sys.executable, os.path.join("scripts", "compare_clips.py"), a, b],
                               cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("48k_stereo_64k", r.stdout)
            self.assertIn("worst", r.stdout)


from unittest.mock import patch, MagicMock

class TestIsFaacLegacy(unittest.TestCase):
    def test_none_faac_path_returns_false(self):
        from utils import is_faac_legacy
        self.assertFalse(is_faac_legacy(None))
        self.assertFalse(is_faac_legacy(""))

    @patch("utils.safe_run")
    def test_modern_faac_advanced_help_returns_false(self, mock_safe_run):
        from utils import is_faac_legacy
        def side_effect(cmd, capture_output=True, check=False, env=None):
            res = MagicMock()
            res.returncode = 0
            if "-H" in cmd or "--help-advanced" in cmd:
                res.stdout = "Usage: faac [options] ...\n  --object-type X   Set AAC object type\n"
            else:
                res.stdout = "Usage: faac [options] ...\nBasic options: -q, -b, -o\n"
            res.stderr = ""
            return res

        mock_safe_run.side_effect = side_effect
        self.assertFalse(is_faac_legacy("/usr/bin/faac"))

    @patch("utils.find_linked_lib", return_value=None)
    @patch("utils.safe_run")
    def test_legacy_faac_returns_true(self, mock_safe_run, mock_find_lib):
        from utils import is_faac_legacy
        res = MagicMock()
        res.returncode = 0
        res.stdout = "Usage: faac [options] ...\nOptions: -q, -b, -w, -o\n"
        res.stderr = ""
        mock_safe_run.return_value = res

        self.assertTrue(is_faac_legacy("/usr/bin/faac"))

    @patch("utils.find_linked_lib", return_value="/usr/lib/libfaac.so.0")
    @patch("utils.safe_run")
    def test_libfaac_so_0_override_returns_true(self, mock_safe_run, mock_find_lib):
        from utils import is_faac_legacy
        res = MagicMock()
        res.returncode = 0
        res.stdout = "Usage: faac [options]\n"
        res.stderr = ""
        mock_safe_run.return_value = res

        self.assertTrue(is_faac_legacy("/usr/bin/faac"))


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
        # A 50% slower candidate would fail loudly on a matched host. Across a
        # host boundary the comparison is meaningless, so it must skip -- and
        # say why, since a silent skip reads like a pass.
        slow = [x * 1.5 for x in self.BASE]
        g = self._run(self.BASE, slow, host_differs=True)
        self.assertEqual(g["status"], "skip")
        # Assert on the cause and the remedy, not the sentence: a flag name is
        # a stable contract where prose is not.
        self.assertIn("different machine", g["detail"])
        self.assertIn("--throughput-only", g["detail"])

    def test_cachegrind_metric_label(self):
        import compare_results as C
        sr = {"gates": [], "has_regression": False}
        C.check_throughput(
            sr,
            {"throughput_samples": {"s": [1000]}, "host_fp": {"cpu": "a"}, "throughput_metric": "cachegrind"},
            {"throughput_samples": {"s": [1000]}, "host_fp": {"cpu": "a"}, "throughput_metric": "cachegrind"})
        self.assertIn("instruction count", sr["gates"][0]["detail"])


class TestAutoBackendSelection(unittest.TestCase):
    def test_run_benchmark_calls_phase2_mos(self):
        import run_benchmark
        with patch.object(sys, "argv", ["run_benchmark.py", "bin", "lib", "name", "out.json", "--skip-encode", "--skip-stereo"]):
            with patch("subprocess.run") as mock_subproc:
                try:
                    run_benchmark.main()
                except SystemExit:
                    pass
                called_phase2 = False
                for call_args in mock_subproc.call_args_list:
                    cmd = call_args[0][0]
                    if "phase2_mos.py" in str(cmd):
                        called_phase2 = True
                        self.assertNotIn("--backend", cmd)
                self.assertTrue(called_phase2)

    def test_phase2_mos_backend_routing(self):
        import phase2_mos
        with tempfile.TemporaryDirectory() as td:
            ref = os.path.join(td, "ref.wav")
            deg = os.path.join(td, "deg.wav")
            write_wav(ref, seconds=1, sr=16000, ch=1)
            write_wav(deg, seconds=1, sr=16000, ch=1)

            # 16kHz should route to visqol-python
            with patch.object(phase2_mos, "get_process_visqol_python") as mock_vpython:
                mock_api = MagicMock()
                mock_api.measure.return_value = MagicMock(moslqo=4.2)
                mock_vpython.return_value = mock_api
                mos, backend = phase2_mos.score_wav_pair(ref, deg, mode_str="speech", sample_rate=16000)
                self.assertEqual(backend, "visqol-python")
                self.assertAlmostEqual(mos, 4.2)

    def test_phase2_mos_has_zimtohrli_flag(self):
        import phase2_mos
        self.assertTrue(hasattr(phase2_mos, "HAS_ZIMTOHRLI"))


class TestFaadWavConv(unittest.TestCase):
    def test_get_faad_path(self):
        from utils import get_faad_path
        p = get_faad_path()
        if p is None:
            with patch("shutil.which", return_value="/usr/bin/faad"):
                p = get_faad_path()
                self.assertEqual(p, "/usr/bin/faad")
        else:
            self.assertIsNotNone(p)
            self.assertTrue(os.path.exists(p))

    def test_wav_conv_faad_decoding(self):
        from utils import wav_conv
        import soundfile as sf
        with tempfile.TemporaryDirectory() as td:
            ref_wav = os.path.join(td, "ref.wav")
            write_wav(ref_wav, seconds=1, sr=48000, ch=2)
            m4a_path = os.path.join(td, "test.m4a")
            dec_wav = os.path.join(td, "dec.wav")

            r = subprocess.run(["ffmpeg", "-y", "-i", ref_wav, "-c:a", "aac", m4a_path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            ok = wav_conv(m4a_path, dec_wav, rate=48000, channels=2)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(dec_wav))

            data, sr = sf.read(dec_wav)
            self.assertEqual(sr, 48000)
            self.assertEqual(data.shape[1], 2)
            self.assertGreater(len(data), 0)

    def test_wav_conv_faad_resampling_mono(self):
        from utils import wav_conv
        import soundfile as sf
        with tempfile.TemporaryDirectory() as td:
            ref_wav = os.path.join(td, "ref.wav")
            write_wav(ref_wav, seconds=1, sr=48000, ch=2)
            m4a_path = os.path.join(td, "test.m4a")
            dec_wav = os.path.join(td, "dec_16k_mono.wav")

            r = subprocess.run(["ffmpeg", "-y", "-i", ref_wav, "-c:a", "aac", m4a_path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            ok = wav_conv(m4a_path, dec_wav, rate=16000, channels=1)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(dec_wav))

            data, sr = sf.read(dec_wav)
            self.assertEqual(sr, 16000)
            self.assertEqual(data.ndim, 1)

    @patch("utils._wav_conv_ffmpeg", return_value=False)
    @patch("utils._wav_conv_faad")
    def test_wav_conv_ffmpeg_fallback_to_faad(self, mock_faad, mock_ffmpeg):
        from utils import wav_conv
        mock_faad.return_value = True
        with tempfile.TemporaryDirectory() as td:
            m4a_path = os.path.join(td, "test.m4a")
            dec_wav = os.path.join(td, "dec.wav")
            with open(m4a_path, "w") as f:
                f.write("dummy")

            ok = wav_conv(m4a_path, dec_wav, rate=48000, channels=2)
            self.assertTrue(ok)
            mock_ffmpeg.assert_called_once()
            mock_faad.assert_called_once()


class TestRefWavCache(unittest.TestCase):
    """The same reference clip is scored against many scenarios that often
    share identical (rate, channels) conversion params (e.g. every
    48k_stereo_* scenario decodes its clips at 48kHz/2ch) -- ref conversion
    should be deduped across those calls, not repeated per scenario."""

    def test_same_params_reuses_conversion(self):
        from utils import get_cached_ref_wav, wav_conv
        with tempfile.TemporaryDirectory() as td:
            ref = os.path.join(td, "ref.wav")
            write_wav(ref, seconds=1, sr=48000, ch=2)
            cache_dir = os.path.join(td, "cache")
            os.makedirs(cache_dir)

            calls = []
            import utils
            orig_wav_conv = utils.wav_conv

            def counting_wav_conv(*args, **kwargs):
                calls.append(args)
                return orig_wav_conv(*args, **kwargs)

            with patch.object(utils, "wav_conv", side_effect=counting_wav_conv):
                p1 = get_cached_ref_wav(cache_dir, ref, 48000, 2)
                p2 = get_cached_ref_wav(cache_dir, ref, 48000, 2)

            self.assertEqual(len(calls), 1, "second call should hit the cache, not re-run ffmpeg")
            self.assertEqual(p1, p2)
            self.assertTrue(os.path.exists(p1))

    def test_different_params_convert_separately(self):
        from utils import get_cached_ref_wav
        with tempfile.TemporaryDirectory() as td:
            ref = os.path.join(td, "ref.wav")
            write_wav(ref, seconds=1, sr=48000, ch=2)
            cache_dir = os.path.join(td, "cache")
            os.makedirs(cache_dir)

            p_stereo = get_cached_ref_wav(cache_dir, ref, 48000, 2)
            p_mono16k = get_cached_ref_wav(cache_dir, ref, 16000, 1)

            self.assertNotEqual(p_stereo, p_mono16k)
            self.assertTrue(os.path.exists(p_stereo))
            self.assertTrue(os.path.exists(p_mono16k))


class TestZimtohrliScoring(unittest.TestCase):
    """score_wav_pair's Zimtohrli branch must resample non-48kHz input and
    score stereo channels independently rather than diluting them via a
    mono downmix (Zimtohrli has no native multi-channel mode)."""

    def setUp(self):
        import phase2_mos
        self.phase2_mos = phase2_mos
        if not phase2_mos.HAS_ZIMTOHRLI:
            self.skipTest("zimtohrli not installed")

    def test_32khz_input_is_resampled_before_scoring(self):
        # A non-48kHz audio WAV fed straight into Zimtohrli (which hard-assumes 48kHz)
        # is effectively time/frequency-scaled -- identical ref/deg should
        # still score near-perfect, but the regression this guards against is
        # scipy.signal.resample_poly never being called at all.
        with tempfile.TemporaryDirectory() as td, \
             patch.object(self.phase2_mos.scipy.signal, "resample_poly",
                                wraps=self.phase2_mos.scipy.signal.resample_poly) as m:
            ref = os.path.join(td, "ref.wav")
            deg = os.path.join(td, "deg.wav")
            write_wav(ref, seconds=1, sr=32000, ch=1)
            write_wav(deg, seconds=1, sr=32000, ch=1)

            mos, backend = self.phase2_mos.score_wav_pair(ref, deg, mode_str="audio", sample_rate=32000)

            self.assertEqual(backend, "zimtohrli")
            self.assertIsNotNone(mos)
            m.assert_called()
            for call in m.call_args_list:
                up, down = call.args[1], call.args[2]
                self.assertEqual(up, 3)
                self.assertEqual(down, 2)

    def test_48khz_input_is_not_resampled(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(self.phase2_mos.scipy.signal, "resample_poly",
                                wraps=self.phase2_mos.scipy.signal.resample_poly) as m:
            ref = os.path.join(td, "ref.wav")
            deg = os.path.join(td, "deg.wav")
            write_wav(ref, seconds=1, sr=48000, ch=1)
            write_wav(deg, seconds=1, sr=48000, ch=1)

            mos, backend = self.phase2_mos.score_wav_pair(ref, deg, mode_str="audio", sample_rate=48000)

            self.assertEqual(backend, "zimtohrli")
            self.assertIsNotNone(mos)
            m.assert_not_called()

    def test_stereo_channel_only_difference_is_not_diluted(self):
        # Build a ref/deg pair that are identical except one channel of deg
        # has extra noise. A mono-downmix-then-single-distance approach
        # averages that noise away far more than scoring the channels
        # independently and combining via L2 would -- assert distance()
        # (mocked to isolate the combining logic) is invoked once per
        # channel, not once on an averaged mono signal.
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as td:
            ref = os.path.join(td, "ref.wav")
            deg = os.path.join(td, "deg.wav")
            write_wav(ref, seconds=1, sr=48000, ch=2)
            write_wav(deg, seconds=1, sr=48000, ch=2)

            data, sr = sf.read(deg, dtype='float32', always_2d=True)
            data[:, 1] += (np.random.RandomState(0).rand(len(data)).astype('float32') - 0.5) * 0.5
            sf.write(deg, np.clip(data, -1, 1), sr)

            real_engine = self.phase2_mos.get_process_zimtohrli()
            original_distance = real_engine.distance
            call_lengths = []

            def counting_distance(a, b):
                call_lengths.append(len(a))
                return original_distance(a, b)

            with patch.object(real_engine, "distance", side_effect=counting_distance):
                mos, backend = self.phase2_mos.score_wav_pair(ref, deg, mode_str="audio", sample_rate=48000)

            self.assertEqual(backend, "zimtohrli")
            self.assertIsNotNone(mos)
            self.assertEqual(len(call_lengths), 2, "expected one distance() call per channel")


class TestCompareResultsRendering(unittest.TestCase):
    def test_summary_table_mos_delta_rendering(self):
        import compare_results as C
        metrics_with_churn = {
            "total_mos_count": 20,
            "total_mos_delta": 0.020,
            "total_clip_wins": 15,
            "total_clip_losses": 3,
            "total_regressions": 0,
            "total_reg_critical": 0,
            "total_reg_significant": 0,
            "total_reg_minor": 0,
            "worst_mos_drop": (0, "N/A"),
            "worst_bitrate_err": (0, "N/A"),
            "total_new_wins": 0,
            "total_significant_wins": 0,
            "bit_exact_percent": 85.0,
            "avg_tp_reduction": 0.0,
            "tp_details_source": [],
            "worst_tp_delta": 0.0,
            "avg_lib_chg": 0.0,
            "avg_bitrate_chg": 0.0,
            "total_bitrate_acc_count": 0,
            "total_ic_count": 0,
            "total_decode_errors": 0,
            "total_missing_mos": 0,
        }
        lines = C.render_summary_table(metrics_with_churn, "MOS", "ABR")
        avg_line = next((l for l in lines if "Avg MOS Δ" in l), None)
        self.assertIsNotNone(avg_line, "Expected Avg MOS Δ row to be present when clip movement exists")
        self.assertIn("+0.001", avg_line)
        self.assertIn("15 clips improved, 3 degraded", avg_line)

    def test_summary_table_mos_delta_omitted_for_identical(self):
        import compare_results as C
        metrics_identical = {
            "total_mos_count": 20,
            "total_mos_delta": 0.0,
            "total_clip_wins": 0,
            "total_clip_losses": 0,
            "total_regressions": 0,
            "total_reg_critical": 0,
            "total_reg_significant": 0,
            "total_reg_minor": 0,
            "worst_mos_drop": (0, "N/A"),
            "worst_bitrate_err": (0, "N/A"),
            "total_new_wins": 0,
            "total_significant_wins": 0,
            "bit_exact_percent": 100.0,
            "avg_tp_reduction": 0.0,
            "tp_details_source": [],
            "worst_tp_delta": 0.0,
            "avg_lib_chg": 0.0,
            "avg_bitrate_chg": 0.0,
            "total_bitrate_acc_count": 0,
            "total_ic_count": 0,
            "total_decode_errors": 0,
            "total_missing_mos": 0,
        }
        lines = C.render_summary_table(metrics_identical, "MOS", "ABR")
        avg_line = next((l for l in lines if "Avg MOS Δ" in l), None)
        self.assertIsNone(avg_line, "Avg MOS Δ row should be omitted for identical runs to avoid noise")

    def test_vbr_metrics_rendering_and_anomaly_detection(self):
        import compare_results as C
        vbr_metrics = {
            "total_mos_count": 10,
            "total_mos_delta": 0.0,
            "total_clip_wins": 0,
            "total_clip_losses": 0,
            "total_regressions": 0,
            "total_reg_critical": 0,
            "total_reg_significant": 0,
            "total_reg_minor": 0,
            "worst_mos_drop": (0, "N/A"),
            "worst_bitrate_err": (0, "N/A"),
            "total_new_wins": 0,
            "total_significant_wins": 0,
            "bit_exact_percent": 100.0,
            "avg_tp_reduction": 0.0,
            "tp_details_source": [],
            "worst_tp_delta": 0.0,
            "avg_lib_chg": 0.0,
            "avg_bitrate_chg": 0.0,
            "total_bitrate_acc_count": 0,
            "avg_vbr_bitrate_chg": 2.5,
            "total_vbr_bitrate_chg_count": 10,
            "vbr_anomalies": [("48k_stereo_64k: C_19_NOISE_FA.wav", 25.0)],
            "total_ic_count": 0,
            "total_decode_errors": 0,
            "total_missing_mos": 0,
        }
        lines = C.render_summary_table(vbr_metrics, "MOS", "VBR")
        content = "\n".join(lines)
        self.assertIn("VBR Bitrate Δ (vs Base)", content)
        self.assertIn("+2.50%", content)
        self.assertIn("VBR Bitrate Anomalies", content)
        self.assertIn("1 clip(s) drifted ≥15% vs Base", content)
        self.assertNotIn("Allocation Accuracy", content)
        self.assertNotIn("Allocation Bias", content)

    def test_analyze_pair_vbr_bitrate_anomaly(self):
        import compare_results as C
        from utils import save_results
        with tempfile.TemporaryDirectory() as td:
            base_json = os.path.join(td, "base.json")
            cand_json = os.path.join(td, "cand.json")
            save_results(base_json, {"matrix": {
                "s1": {"mos": 3.5, "scenario": "48k_stereo_64k", "filename": "c1.wav",
                       "bitrate": 60.0, "time": 1.0, "rate_control_mode": "vbr"}
            }})
            save_results(cand_json, {"matrix": {
                "s1": {"mos": 3.5, "scenario": "48k_stereo_64k", "filename": "c1.wav",
                       "bitrate": 75.0, "time": 1.0, "rate_control_mode": "vbr"}
            }})
            res = C.analyze_pair(base_json, cand_json)
            self.assertIsNotNone(res)
            self.assertEqual(res["rate_control_mode"], "vbr")
            self.assertEqual(len(res["vbr_anomalies"]), 1)
            self.assertIn("48k_stereo_64k: c1.wav", res["vbr_anomalies"][0][0])
            self.assertAlmostEqual(res["vbr_anomalies"][0][1], 25.0)


class TestAttackCentroidShift(unittest.TestCase):
    """Ground-truth checks for transient.py's attack-centroid-shift metric,
    ported from scripts/score_transient.py's cmd_validate_centroid (ref-vs-ref
    exactness, near-total yield) plus a directional sanity check. Uses a
    synthetic click train rather than the external audio corpus so it stays
    hermetic and fast."""

    @staticmethod
    def _click_train(sr=48000, seconds=2.0, click_times_ms=(200, 500, 800, 1100, 1400, 1700)):
        import numpy as np
        n = int(sr * seconds)
        rng = np.random.default_rng(0)
        audio = rng.normal(0, 1e-4, n)
        decay = np.exp(-np.arange(500) / 50.0) * 0.9
        for t_ms in click_times_ms:
            idx = int(t_ms * 1e-3 * sr)
            audio[idx:idx + len(decay)] += decay
        return audio.astype(np.float64), sr, click_times_ms

    def test_ref_vs_ref_exact_and_full_yield(self):
        import transient
        ref, sr, click_times_ms = self._click_train()
        onsets = transient.detect_onsets(ref, sr)
        self.assertEqual(len(onsets), len(click_times_ms), "should detect every synthetic click")

        result = transient.compute_attack_centroid_shift(ref, ref, onsets, sr)
        self.assertEqual(len(result), len(onsets), "ref-vs-ref should score every onset (full yield)")
        for _, delta_ms in result:
            self.assertEqual(delta_ms, 0.0, "ref-vs-ref must read exactly 0.0 by construction")

    def test_delayed_energy_reads_positive(self):
        """A decoded copy whose post-onset energy arrives later than the
        reference should read a positive (smeared) delta at every onset."""
        import numpy as np
        import transient
        ref, sr, click_times_ms = self._click_train()
        shift = int(0.002 * sr)  # 2ms
        dec = ref.copy()
        decay = np.exp(-np.arange(500) / 50.0) * 0.9
        for t_ms in click_times_ms:
            idx = int(t_ms * 1e-3 * sr)
            dec[idx:idx + len(decay)] -= decay  # remove the on-time energy
            dec[idx + shift:idx + shift + len(decay)] += decay  # re-add it shifted later

        onsets = transient.detect_onsets(ref, sr)
        result = transient.compute_attack_centroid_shift(ref, dec, onsets, sr)
        self.assertEqual(len(result), len(onsets))
        for _, delta_ms in result:
            self.assertGreater(delta_ms, 0.0, "delayed post-onset energy should read positive (smeared)")

    def test_end_to_end_wrapper_matches_direct_call(self):
        """attack_centroid_deltas() (find_lag + align + detect + compute) on
        an unshifted ref-vs-ref pair should agree with the direct call."""
        import transient
        ref, sr, _ = self._click_train()
        deltas = transient.attack_centroid_deltas(ref, ref, sr)
        self.assertGreater(len(deltas), 0)
        for d in deltas:
            self.assertEqual(d, 0.0)


class TestCiSigntestVerdict(unittest.TestCase):
    def test_consistent_negative_deltas_verdict_decrease(self):
        import transient
        deltas = [-0.5] * 40
        lo, hi = transient.bootstrap_ci(deltas)
        p, _neg, _n = transient.sign_test_p(deltas)
        verdict = transient.ci_signtest_verdict(lo, hi, p, "decreased", "increased")
        self.assertEqual(verdict, "decreased")

    def test_sign_test_p_large_n_no_overflow(self):
        import transient
        deltas = [-0.5] * 1000
        p, neg, n = transient.sign_test_p(deltas)
        self.assertEqual(n, 1000)
        self.assertEqual(neg, 1000)
        self.assertLess(p, 1e-12)

    def test_noisy_mixed_deltas_are_inconclusive(self):
        """A CI that barely excludes zero but with no consistent per-onset
        direction must not print a confident verdict -- this is the exact
        failure mode found during Stage 1 (an ffmpeg TNS A/B run with CI
        [+0.005, +0.243] but sign-test p=0.458)."""
        import transient
        rng_deltas = ([0.6, -0.5] * 20) + [0.01]  # near 50/50 sign split
        p, _neg, _n = transient.sign_test_p(rng_deltas)
        lo, hi = 0.005, 0.243  # CI that barely excludes zero
        verdict = transient.ci_signtest_verdict(lo, hi, p, "decreased", "increased")
        self.assertIn("inconclusive", verdict)


class TestCompareEncodersLeaderboard(unittest.TestCase):
    def test_leaderboard_refactored_sections_and_worst_mos(self):
        from compare_encoders import generate_leaderboard, Encoder

        class DummyEncoder(Encoder):
            def __init__(self, name, profile):
                self.name = name
                self.profile = profile
                self.tool_id = name.lower()
                self.text_size = 1000
                self.rodata_size = 500

            def get_encode_cmd(self, input_path, output_path, bitrate_kbps, channels, sample_rate):
                return ["echo"]

        encoders = [
            DummyEncoder("ToolA", "lc"),
            DummyEncoder("ToolA", "he"),
            DummyEncoder("ToolA", "hev2"),
        ]

        results = [
            {
                "tool": "ToolA", "profile": "lc", "row_key": "toola_lc",
                "scenario": "48k_stereo_128k", "filename": "s1.wav", "duration": 1.0,
                "audio_duration": 10.0, "size": 1000, "actual_bitrate": 128.0,
                "target_bitrate": 128, "decode_valid": True, "decode_error": "",
                "mos": 4.2, "ic_err": 0.05, "attack_centroid_ms": [1.0]
            },
            {
                "tool": "ToolA", "profile": "he", "row_key": "toola_he",
                "scenario": "48k_stereo_32k", "filename": "s1.wav", "duration": 1.0,
                "audio_duration": 10.0, "size": 300, "actual_bitrate": 32.0,
                "target_bitrate": 32, "decode_valid": True, "decode_error": "",
                "mos": 3.8, "ic_err": 0.1, "attack_centroid_ms": [2.0]
            },
            {
                "tool": "ToolA", "profile": "hev2", "row_key": "toola_hev2",
                "scenario": "48k_stereo_16k", "filename": "s1.wav", "duration": 1.0,
                "audio_duration": 10.0, "size": 150, "actual_bitrate": 16.0,
                "target_bitrate": 16, "decode_valid": True, "decode_error": "",
                "mos": 3.1, "ic_err": 0.15, "attack_centroid_ms": [3.0]
            }
        ]

        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            scenario_list = ["48k_stereo_128k", "48k_stereo_32k", "48k_stereo_16k"]
            # patch.dict, not assignment: this used to overwrite the real
            # SCENARIOS entries for the rest of the process, so whichever test
            # ran next saw a synthetic matrix.
            import config
            fake = {n: {"mode": "audio", "corpus": "audio_48k", "bitrate": b,
                        "rate": 48000, "channels": 2, "visqol_rate": 48000}
                    for n, b in (("48k_stereo_128k", 128), ("48k_stereo_32k", 32),
                                 ("48k_stereo_16k", 16))}
            with patch.dict(config.SCENARIOS, fake):
                generate_leaderboard(encoders, results, out_md, scenario_list, skip_graphs=False)

            with open(out_md) as f:
                content = f.read()

            self.assertIn("# AAC Encoder Leaderboard", content)
            self.assertIn("## Overall Rankings", content)
            self.assertIn("## Per-Scenario Breakdown & Visualizations", content)
            self.assertIn("xychart-beta", content)
            self.assertIn("#### Per-Scenario Average MOS", content)
            self.assertIn("#### Per-Scenario Worst MOS", content)
            self.assertIn("#### LC Profile", content)
            self.assertIn("#### HE-v1 Profile", content)
            self.assertIn("#### HE-v2 Profile", content)

    def test_leaderboard_title_and_standard_profile_with_non_aac(self):
        from compare_encoders import generate_leaderboard, Encoder, OpusEncoder, LameEncoder

        class DummyAAC(Encoder):
            def __init__(self):
                super().__init__("FAAC", "faac", "faac", "lc")
            def get_encode_cmd(self, i, o, b, c, s):
                return ["echo"]

        aac_enc = DummyAAC()
        opus_enc = OpusEncoder("Opus", "opusenc", tool_id="opusenc")
        lame_enc = LameEncoder("LAME", "lame", tool_id="lame")

        encoders = [aac_enc, opus_enc, lame_enc]
        results = [
            {
                "tool": "FAAC", "profile": "lc", "row_key": "faac_lc",
                "scenario": "48k_stereo_128k", "filename": "s1.wav", "duration": 1.0,
                "audio_duration": 10.0, "size": 1000, "actual_bitrate": 128.0,
                "target_bitrate": 128, "decode_valid": True, "decode_error": "",
                "mos": 4.1, "ic_err": 0.05, "attack_centroid_ms": [1.0]
            },
            {
                "tool": "Opus", "profile": "standard", "row_key": "opusenc_standard",
                "scenario": "48k_stereo_128k", "filename": "s1.wav", "duration": 0.5,
                "audio_duration": 10.0, "size": 900, "actual_bitrate": 128.0,
                "target_bitrate": 128, "decode_valid": True, "decode_error": "",
                "mos": 4.5, "ic_err": 0.02, "attack_centroid_ms": [0.5]
            },
            {
                "tool": "LAME", "profile": "standard", "row_key": "lame_standard",
                "scenario": "48k_stereo_128k", "filename": "s1.wav", "duration": 0.8,
                "audio_duration": 10.0, "size": 1100, "actual_bitrate": 128.0,
                "target_bitrate": 128, "decode_valid": True, "decode_error": "",
                "mos": 3.9, "ic_err": 0.08, "attack_centroid_ms": [1.2]
            }
        ]

        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            scenario_list = ["48k_stereo_128k"]
            generate_leaderboard(encoders, results, out_md, scenario_list, skip_graphs=False)

            with open(out_md) as f:
                content = f.read()

            self.assertIn("# Audio Encoder Leaderboard", content)
            self.assertIn("#### Standard Profile", content)
            self.assertIn("Opus", content)
            self.assertIn("LAME", content)


class TestOpusLameEncoderCommands(unittest.TestCase):
    def test_opusenc_command_and_ffmpeg_fallback(self):
        from compare_encoders import OpusEncoder
        op1 = OpusEncoder("Opus", "/usr/bin/opusenc", is_ffmpeg=False)
        cmd1 = op1.get_encode_cmd("in.wav", "out.opus", 128, 2, 48000)
        self.assertEqual(cmd1, ["/usr/bin/opusenc", "--bitrate", "128", "in.wav", "out.opus"])

        op2 = OpusEncoder("Opus (FFmpeg)", "/usr/bin/ffmpeg", is_ffmpeg=True)
        cmd2 = op2.get_encode_cmd("in.wav", "out.opus", 128, 2, 48000)
        self.assertEqual(cmd2, ["/usr/bin/ffmpeg", "-y", "-i", "in.wav", "-c:a", "libopus", "-b:a", "128k", "-ac", "2", "out.opus"])

    def test_lame_command_and_ffmpeg_fallback(self):
        from compare_encoders import LameEncoder
        lame1 = LameEncoder("LAME", "/usr/bin/lame", is_ffmpeg=False)
        cmd1 = lame1.get_encode_cmd("in.wav", "out.mp3", 128, 2, 48000)
        self.assertEqual(cmd1, ["/usr/bin/lame", "-b", "128", "-s", "48.0", "in.wav", "out.mp3"])

        lame2 = LameEncoder("LAME (FFmpeg)", "/usr/bin/ffmpeg", is_ffmpeg=True)
        cmd2 = lame2.get_encode_cmd("in.wav", "out.mp3", 128, 2, 48000)
        self.assertEqual(cmd2, ["/usr/bin/ffmpeg", "-y", "-i", "in.wav", "-c:a", "libmp3lame", "-b:a", "128k", "-ac", "2", "out.mp3"])


if __name__ == "__main__":
    unittest.main()
