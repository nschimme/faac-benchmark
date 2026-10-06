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

    def test_decode_lag_finds_the_codec_delay(self):
        src = haze.attack_clip()[:, 0]
        delayed = np.concatenate([np.zeros(1234), src])
        self.assertEqual(haze.decode_lag(src, delayed), 1234)

    def delayed(self, src, lag=1105):
        return np.concatenate([np.zeros(lag), src])

    def test_pre_onset_error_is_tiny_for_a_clean_copy(self):
        src = haze.attack_clip()[:, 0]
        values = haze.pre_onset_db(src, self.delayed(src))
        self.assertEqual(len(values), len(haze.ATTACK_ONSETS_S))
        self.assertTrue(all(v < -80 for v in values))

    def test_pre_onset_error_sees_energy_smeared_before_one_kick(self):
        src = haze.attack_clip()[:, 0]
        dec = self.delayed(src)
        t = int(3 * haze.SR) + 1105
        rng = np.random.default_rng(5)
        dec[t - 900:t - 100] += rng.standard_normal(800) * 10 ** (-40 / 20)    # pre-echo before the third kick
        values = haze.pre_onset_db(src, dec)
        self.assertGreater(values[2], values[1] + 30)
        self.assertGreater(values[2], values[3] + 30)

    def test_pre_onset_error_ignores_content_above_the_core_band(self):
        src = haze.attack_clip()[:, 0]
        dec = self.delayed(src)
        t = int(2 * haze.SR) + 1105
        n = np.arange(900)
        burst = 0.1 * np.hanning(900) * np.sin(2 * np.pi * 12000 * n / haze.SR)    # tapered, so it does not leak down
        dec[t - 1000:t - 100] += burst                                              # SBR territory
        self.assertLess(haze.pre_onset_db(src, dec)[1], -80)


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
    PRE = [-13.9, -8.9, -10.7, -9.0, -9.8]

    def suite(self):
        return {"gates": [], "has_regression": False}

    def bass(self, haze_db, role="bass"):
        return {"role": role, "haze_db": haze_db, "pre_db": None, "bytes": 1}

    def attack(self, pre=None):
        return {"role": "attack", "haze_db": None, "pre_db": pre or list(self.PRE), "bytes": 1}

    def results(self, **cases):
        return {"haze": {"cases": cases}}

    def gate(self, base, cand):
        s = self.suite()
        compare_results.check_haze(s, base, cand)
        return s, s["gates"][-1]

    def test_less_haze_passes(self):
        s, g = self.gate(self.results(lc=self.bass(38.4)), self.results(lc=self.bass(16.3)))
        self.assertEqual(g["status"], "pass")
        self.assertFalse(s["has_regression"])

    def test_more_haze_fails(self):
        s, g = self.gate(self.results(lc=self.bass(16.3)), self.results(lc=self.bass(38.4)))
        self.assertEqual(g["status"], "fail")
        self.assertIn("lc (more haze)", g["detail"])
        self.assertTrue(s["has_regression"])

    def test_small_haze_noise_passes(self):
        _, g = self.gate(self.results(lc=self.bass(16.3)), self.results(lc=self.bass(17.9)))
        self.assertEqual(g["status"], "pass")

    def test_control_is_judged_on_haze(self):
        base = self.results(ctl=self.bass(0.3, "control"))
        _, g = self.gate(base, self.results(ctl=self.bass(8.0, "control")))
        self.assertEqual(g["status"], "fail")
        _, g = self.gate(base, self.results(ctl=self.bass(0.4, "control")))
        self.assertEqual(g["status"], "pass")

    def test_attack_error_rising_on_average_fails(self):
        rise = [v + 2.7 for v in self.PRE]            # an all-long encoder measured +2.7 on average
        _, g = self.gate(self.results(atk=self.attack()), self.results(atk=self.attack(rise)))
        self.assertEqual(g["status"], "fail")
        self.assertIn("attacks smeared (mean)", g["detail"])

    def test_one_kick_rising_a_lot_fails(self):
        worse = list(self.PRE)
        worse[3] += 6.5                                  # mean +1.3: only the single-kick rule trips
        _, g = self.gate(self.results(atk=self.attack()), self.results(atk=self.attack(worse)))
        self.assertEqual(g["status"], "fail")
        self.assertIn("attacks smeared (one kick)", g["detail"])

    def test_the_measured_change_passes(self):
        measured = [-13.9, -8.8, -11.1, -9.0, -9.3]     # mean +0.05, worst kick +0.5
        _, g = self.gate(self.results(atk=self.attack()), self.results(atk=self.attack(measured)))
        self.assertEqual(g["status"], "pass")

    def test_attack_error_falling_passes(self):
        better = [v - 3.0 for v in self.PRE]
        _, g = self.gate(self.results(atk=self.attack()), self.results(atk=self.attack(better)))
        self.assertEqual(g["status"], "pass")

    def test_missing_baseline_skips(self):
        s, g = self.gate({}, self.results(lc=self.bass(16.3)))
        self.assertEqual(g["status"], "skip")
        self.assertFalse(s["has_regression"])

    def test_baseline_without_measurements_skips(self):
        base = self.results(lc=self.bass(None))
        _, g = self.gate(base, self.results(lc=self.bass(None)))
        self.assertEqual(g["status"], "skip")

    def test_case_the_candidate_did_not_measure_fails(self):
        base = self.results(lc=self.bass(16.3), he=self.bass(21.5))
        s, g = self.gate(base, self.results(lc=self.bass(16.3)))          # the HE encode crashed
        self.assertEqual(g["status"], "fail")
        self.assertIn("he (candidate produced no measurement)", g["detail"])
        self.assertTrue(s["has_regression"])

    def test_candidate_with_an_empty_measurement_fails(self):
        _, g = self.gate(self.results(lc=self.bass(16.3)), self.results(lc=self.bass(None)))
        self.assertEqual(g["status"], "fail")

    def test_attack_with_a_partial_measurement_fails(self):
        partial = list(self.PRE)
        partial[2] = None
        _, g = self.gate(self.results(atk=self.attack()), self.results(atk=self.attack(partial)))
        self.assertEqual(g["status"], "fail")

    def test_candidate_without_a_haze_block_fails_when_the_baseline_has_one(self):
        _, g = self.gate(self.results(lc=self.bass(16.3)), {})
        self.assertEqual(g["status"], "fail")

    def test_a_case_new_in_the_candidate_is_ignored(self):
        base = self.results(lc=self.bass(16.3))
        _, g = self.gate(base, self.results(lc=self.bass(16.3), extra=self.bass(99.0)))
        self.assertEqual(g["status"], "pass")

    def test_case_without_role_counts_as_bass(self):
        legacy = {"haze_db": 16.3, "bytes": 1}
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
