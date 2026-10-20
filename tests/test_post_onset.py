"""Controls for the signed 12-50 ms high-band residual."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

import transient
from codec_bench.report import _post_onset_summary


def fixture():
    rng = np.random.default_rng(42)
    x = rng.normal(0, 2e-5, (48000 * 3, 2))
    for on in (24000, 48000, 72000, 96000, 120000):
        t = np.arange(4800)
        x[on:on + 4800] += rng.normal(size=(4800, 2)) * .15 * np.exp(-t[:, None] / 500)
    return x


class PostOnsetTests(unittest.TestCase):
    def test_identity_floor_and_determinism(self):
        r = fixture()
        m = transient.pre_echo_metrics(r, r, 48000)
        self.assertTrue(transient.has_current_post_onset(m))
        self.assertGreater(len(m['post_onset_residual_db']), 0)
        np.testing.assert_allclose(m['post_onset_residual_db'], -80, atol=.01)
        self.assertEqual(m, transient.pre_echo_metrics(r, r, 48000))
        self.assertAlmostEqual(_post_onset_summary([m]), -80)
        self.assertIsNone(_post_onset_summary([{'post_onset_residual_db': [-80]}]))

    def test_shift_stability(self):
        r = fixture()
        base = transient.pre_echo_metrics(r, r, 48000)
        for shift in (-1, 1):
            m = transient.pre_echo_metrics(r, np.roll(r, shift, axis=0), 48000)
            self.assertEqual(m['pre_echo_lag_samples'], -shift)
            np.testing.assert_allclose(m['post_onset_residual_db'],
                                       base['post_onset_residual_db'], atol=.01)

    def test_clean_pre_echo_control(self):
        r = fixture()
        base = transient.pre_echo_metrics(r, r, 48000)
        rng = np.random.default_rng(42)
        selected = []
        original = transient._scored_pre_echo_events
        def capture(*args):
            for event in original(*args):
                selected.append(event)
                yield event
        with patch.object(transient, '_scored_pre_echo_events', side_effect=capture):
            transient.pre_echo_metrics(r, r, 48000)
        for i, (ch, on, *_rest) in enumerate(selected):
            d = r.copy()
            noise = rng.normal(size=240)
            rms = np.sqrt(np.mean(r[on:on + 576, ch] ** 2))
            d[on - 288:on - 48, ch] += noise / np.sqrt(np.mean(noise ** 2)) * .1 * rms
            np.testing.assert_array_equal(d[on:], r[on:])
            m = transient.pre_echo_metrics(r, d, 48000)
            self.assertGreater(m['pre_echo_db'][i] - base['pre_echo_db'][i], 5)
            self.assertLess(abs(m['post_onset_residual_db'][i] -
                                base['post_onset_residual_db'][i]), .5)

    def test_monotone_decaying_post_noise(self):
        r = fixture()
        noise = np.zeros_like(r)
        rng = np.random.default_rng(7)
        for on in (24000, 48000, 72000, 96000, 120000):
            level = np.sqrt(np.mean(r[on:on + 576] ** 2, axis=0))
            noise[on:on + 9600] += (rng.normal(size=(9600, 2)) * level *
                                    np.exp(-np.arange(9600)[:, None] / 960))
        scores = [transient.pre_echo_metrics(r, r + noise * 10 ** (db / 20), 48000)
                  ['post_onset_residual_db'] for db in (-30, -20, -10)]
        self.assertTrue(np.all(np.diff(scores, axis=0) > 0))

    def test_nper_frozen_fixture(self):
        # Frozen with the pre-refactor a46f0f2 scorer, including native-rate resampling.
        expected = json.loads(Path(__file__).with_name('post_onset_nper_fixture.json').read_text())
        r = fixture()
        for case in expected:
            d = np.roll(r * case['gain'], case['shift'], axis=0)
            actual = transient.pre_echo_metrics(r, d, case['sr'])
            for key, value in case['expected'].items():
                if key == 'pre_echo_db':
                    self.assertEqual(len(actual[key]), len(value))
                    np.testing.assert_allclose(actual[key], value, rtol=1e-9, atol=1e-9)
                else:
                    self.assertEqual(actual[key], value)

    def test_missing_window_retains_shared_event_and_own_floor(self):
        r = fixture()
        m = transient.pre_echo_metrics(r[:24900], r[:24900], 48000)
        self.assertGreater(len(m['pre_echo_db']), 0)
        self.assertEqual(m['post_onset_residual_db'], [None] * len(m['pre_echo_db']))
        self.assertTrue(transient.has_current_post_onset(m))
        self.assertIsNone(_post_onset_summary([m]))
        # Constant gain has an analytic residual independent of onset energy.
        m = transient.pre_echo_metrics(r, r * .999, 48000)
        np.testing.assert_allclose(m['post_onset_residual_db'], -60, atol=1e-8)


if __name__ == '__main__':
    unittest.main()
