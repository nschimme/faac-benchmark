"""
 * FAAC Benchmark Suite - Unit Tests for the bass-haze phase and gate
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

import compare_results
import phase4_haze as haze


def adts_frame(window_sequence, window_shape, common_window=1, rate_index=3):
    """One ADTS frame holding only the start of a channel pair element."""
    bits = "001" + "0000" + str(common_window)
    if common_window:
        bits += "0" + format(window_sequence, "02b") + str(window_shape)
    else:                                   # global_gain precedes ics_info
        bits += "00000000" + "0" + format(window_sequence, "02b") + str(window_shape)
    bits += "0" * (-len(bits) % 8)
    payload = int(bits, 2).to_bytes(len(bits) // 8, "big")
    length = 7 + len(payload)
    header = bytes([0xFF, 0xF1, 0x40 | (rate_index << 2), (length >> 11) & 3, (length >> 3) & 0xFF,
                    ((length & 7) << 5) | 0x1F, 0xFC])
    return header + payload


class TestBassClip(unittest.TestCase):
    def test_deterministic_and_on_the_24_bit_grid(self):
        a, b = haze.bass_clip(), haze.bass_clip()
        self.assertTrue(np.array_equal(a, b))
        self.assertTrue(np.array_equal(haze.bass_clip(True), haze.bass_clip(True)))
        ints = a * (2 ** 23)
        self.assertLess(np.abs(ints - np.round(ints)).max(), 1e-6)

    def test_silent_lead_and_bass_dominance(self):
        x = haze.bass_clip()
        lead = int(haze.LEAD_S * haze.SR)
        self.assertEqual(np.abs(x[:lead]).max(), 0.0)
        a, b = (int(s * haze.SR) for s in haze.SPAN_S)
        low = haze.band_power_db(x[a:b, 0], 20, 400)
        high = haze.band_power_db(x[a:b, 0], *haze.BAND_HZ)
        self.assertGreater(low - high, 53.0)        # what a bass-dominance rule keys on

    def test_control_keeps_the_treble_out_of_the_lead_and_adds_it_above_the_bass(self):
        x, c = haze.bass_clip(), haze.bass_clip(True)
        lead = int(haze.LEAD_S * haze.SR)
        self.assertEqual(np.abs(c[:lead]).max(), 0.0)
        a, b = (int(s * haze.SR) for s in haze.SPAN_S)
        self.assertGreater(haze.band_power_db(c[a:b, 0], *haze.BAND_HZ)
                           - haze.band_power_db(x[a:b, 0], *haze.BAND_HZ), 30.0)


class TestMetrics(unittest.TestCase):
    def test_haze_is_zero_for_identical_and_positive_for_added_noise(self):
        x = haze.bass_clip()[:, 0]
        self.assertAlmostEqual(haze.haze_db(x, x), 0.0, places=6)
        rng = np.random.default_rng(1)
        noisy = x + rng.standard_normal(len(x)) * 10 ** (-80 / 20)
        self.assertGreater(haze.haze_db(x, noisy), 10.0)

    def test_window_info_reads_both_common_window_cases(self):
        data = adts_frame(2, 1) + adts_frame(0, 0) + adts_frame(1, 1, common_window=0)
        self.assertEqual(haze.adts_window_info(data), [(2, 1), (0, 0), (1, 1)])

    def test_core_rate_comes_from_the_adts_header(self):
        self.assertEqual(haze.adts_core_rate(adts_frame(0, 0)), 48000)                 # LC at 48 kHz
        self.assertEqual(haze.adts_core_rate(adts_frame(0, 0, rate_index=6)), 24000)   # HE core
        with self.assertRaises(ValueError):
            haze.adts_core_rate(b"\x00" * 16)
        with self.assertRaises(ValueError):
            haze.adts_core_rate(adts_frame(0, 0, rate_index=15))

    def test_profile_must_match_what_the_encoder_wrote(self):
        haze.check_profile("lc", 48000)
        haze.check_profile("he", 24000)
        with self.assertRaises(ValueError):
            haze.check_profile("he", 48000)       # a binary that fell back to LC
        with self.assertRaises(ValueError):
            haze.check_profile("lc", 24000)

    def test_window_info_does_not_hang_on_a_zero_length_frame(self):
        zero = bytes([0xFF, 0xF1, 0x4C, 0x00, 0x00, 0x1F, 0xFC]) * 3
        self.assertEqual(haze.adts_window_info(adts_frame(2, 1) + zero), [(2, 1)])

    def test_window_info_stops_at_garbage(self):
        self.assertEqual(haze.adts_window_info(adts_frame(2, 0) + b"\x00" * 16), [(2, 0)])

    def test_span_fractions_use_the_core_frame_rate(self):
        # 100 short frames, then 300 long KBD ones. The span 2.5-5.5 s is frames
        # 117-257 at the LC rate (46.9/s) and 58-128 at the HE core rate (23.4/s).
        info = [(2, 0)] * 100 + [(0, 1)] * 300
        self.assertEqual(haze.span_fractions(info, he=False), (0.0, 1.0))
        short, kbd = haze.span_fractions(info, he=True)
        self.assertAlmostEqual(short, 42 / 70)          # frames 58-99 are short
        self.assertAlmostEqual(kbd, 28 / 70)            # frames 100-127 are KBD

    def test_span_fractions_of_an_empty_span(self):
        self.assertEqual(haze.span_fractions([], he=False), (None, None))


class TestStoreBlock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "results.json")
        with open(self.path, "w") as f:
            json.dump({"matrix": {"a": 1}}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_adds_the_block_and_keeps_the_rest(self):
        haze.store_block(self.path, {"cases": {"x": 1}})
        with open(self.path) as f:
            data = json.load(f)
        self.assertEqual(data["matrix"], {"a": 1})
        self.assertEqual(data["haze"], {"cases": {"x": 1}})
        self.assertEqual(os.listdir(self.dir), ["results.json"])

    def test_a_failed_write_leaves_the_original_intact(self):
        with self.assertRaises(TypeError):
            haze.store_block(self.path, {"cases": object()})        # not serialisable
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"matrix": {"a": 1}})
        self.assertEqual(os.listdir(self.dir), ["results.json"])


class TestHazeGate(unittest.TestCase):
    def suite(self):
        return {"gates": [], "has_regression": False}

    def case(self, role="bass", haze_db=0.0, short=0.0, kbd=0.0):
        return {"role": role, "haze_db": haze_db, "short_frac": short, "kbd_frac": kbd, "bytes": 1}

    def results(self, **cases):
        return {"haze": {"cases": cases}}

    def gate(self, base, cand):
        s = self.suite()
        compare_results.check_haze(s, base, cand)
        return s, s["gates"][-1]

    def test_improvement_passes(self):
        s, g = self.gate(self.results(lc=self.case(haze_db=38.4, short=1.0)),
                         self.results(lc=self.case(haze_db=16.3, short=0.0, kbd=1.0)))
        self.assertEqual(g["status"], "pass")
        self.assertFalse(s["has_regression"])

    def test_more_haze_fails(self):
        s, g = self.gate(self.results(lc=self.case(haze_db=16.3)), self.results(lc=self.case(haze_db=38.4)))
        self.assertEqual(g["status"], "fail")
        self.assertIn("lc (more haze)", g["detail"])
        self.assertTrue(s["has_regression"])

    def test_more_short_windows_fails(self):
        _, g = self.gate(self.results(he=self.case(short=0.0)), self.results(he=self.case(short=0.5)))
        self.assertEqual(g["status"], "fail")

    def test_small_noise_passes(self):
        _, g = self.gate(self.results(lc=self.case(haze_db=16.3)),
                         self.results(lc=self.case(haze_db=17.9, short=0.05)))
        self.assertEqual(g["status"], "pass")

    def test_control_fails_when_kbd_starts_firing(self):
        base = self.results(ctl=self.case("control", 0.3, 0.0, 0.0))
        cand = self.results(ctl=self.case("control", 0.3, 0.0, 0.4))   # haze unchanged: only kbd moved
        _, g = self.gate(base, cand)
        self.assertEqual(g["status"], "fail")
        self.assertIn("KBD fires where bass does not dominate", g["detail"])

    def test_kbd_on_a_bass_case_is_not_a_regression(self):
        _, g = self.gate(self.results(lc=self.case(haze_db=38.4, kbd=0.0)),
                         self.results(lc=self.case(haze_db=16.3, kbd=1.0)))
        self.assertEqual(g["status"], "pass")

    def test_attack_case_fails_when_short_windows_vanish(self):
        base = self.results(atk=self.case("attack", None, 0.21))
        _, g = self.gate(base, self.results(atk=self.case("attack", None, 0.02)))
        self.assertEqual(g["status"], "fail")
        self.assertIn("attacks lost short windows", g["detail"])

    def test_attack_case_tolerates_the_designed_drop(self):
        base = self.results(atk=self.case("attack", None, 0.21))
        _, g = self.gate(base, self.results(atk=self.case("attack", None, 0.13)))      # -8 points
        self.assertEqual(g["status"], "pass")

    def test_attack_case_needs_no_haze_value(self):
        base = self.results(atk=self.case("attack", None, 0.21))
        _, g = self.gate(base, self.results(atk=self.case("attack", None, 0.21)))
        self.assertEqual(g["status"], "pass")

    def test_missing_baseline_skips(self):
        s, g = self.gate({}, self.results(lc=self.case()))
        self.assertEqual(g["status"], "skip")
        self.assertFalse(s["has_regression"])

    def test_cases_without_measurements_skip(self):
        _, g = self.gate(self.results(lc=self.case(haze_db=None, short=None)),
                         self.results(lc=self.case(haze_db=None, short=None)))
        self.assertEqual(g["status"], "skip")

    def test_case_the_candidate_did_not_measure_fails(self):
        base = self.results(lc=self.case(haze_db=16.3), he=self.case(haze_db=21.5))
        cand = self.results(lc=self.case(haze_db=16.3))             # the HE encode crashed
        s, g = self.gate(base, cand)
        self.assertEqual(g["status"], "fail")
        self.assertIn("he (candidate produced no measurement)", g["detail"])
        self.assertTrue(s["has_regression"])

    def test_candidate_with_an_empty_measurement_fails(self):
        base = self.results(lc=self.case(haze_db=16.3))
        _, g = self.gate(base, self.results(lc=self.case(haze_db=None, short=None)))
        self.assertEqual(g["status"], "fail")

    def test_candidate_without_a_haze_block_fails_when_the_baseline_has_one(self):
        _, g = self.gate(self.results(lc=self.case()), {})
        self.assertEqual(g["status"], "fail")

    def test_control_with_an_empty_kbd_share_does_not_raise(self):
        base = self.results(ctl=self.case("control", 0.3, 0.0, None))
        cand = self.results(ctl=self.case("control", 0.3, 0.0, None))
        _, g = self.gate(base, cand)
        self.assertEqual(g["status"], "pass")

    def test_a_case_new_in_the_candidate_is_ignored(self):
        base = self.results(lc=self.case(haze_db=16.3))
        cand = self.results(lc=self.case(haze_db=16.3), extra=self.case(haze_db=99.0))
        _, g = self.gate(base, cand)
        self.assertEqual(g["status"], "pass")

    def test_case_without_role_counts_as_bass(self):
        legacy = {"haze_db": 16.3, "short_frac": 0.0, "bytes": 1}
        _, g = self.gate({"haze": {"cases": {"lc": legacy}}},
                         {"haze": {"cases": {"lc": {**legacy, "haze_db": 40.0}}}})
        self.assertEqual(g["status"], "fail")


class TestAttackClip(unittest.TestCase):
    def test_deterministic_with_five_onsets_over_silence(self):
        a, b = haze.attack_clip(), haze.attack_clip()
        self.assertTrue(np.array_equal(a, b))
        self.assertEqual(np.abs(a[:int(0.9 * haze.SR)]).max(), 0.0)
        for k in range(1, 6):
            self.assertGreater(np.abs(a[int(k * haze.SR):int(k * haze.SR) + 100]).max(), 0.05)


if __name__ == "__main__":
    unittest.main()
