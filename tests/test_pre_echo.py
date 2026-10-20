import unittest

import numpy as np
from scipy import signal

import transient


SR = 48000


def click_clip(length=SR * 2, onsets=(SR // 2, SR, SR + SR // 2), channels=1):
    rng = np.random.default_rng(9182)
    base = rng.normal(0.0, 1e-5, (length, channels))
    n = np.arange(720)
    burst = 0.25 * np.sin(2 * np.pi * 6000 * n / SR) * np.hanning(1440)[360:1080]
    for onset in onsets:
        for ch in range(channels):
            base[onset:onset + len(burst), ch] += burst
    return base


def offset_audio(x, offset):
    y = np.zeros_like(x)
    if offset > 0:
        y[offset:] = x[:-offset]
    elif offset < 0:
        y[:offset] = x[-offset:]
    else:
        y[:] = x
    return y


class PreEchoMetricTests(unittest.TestCase):
    def test_identity_sparse_clicks_is_finite_and_fully_scored(self):
        ref = click_clip()
        result = transient.pre_echo_metrics(ref, ref.copy(), SR)
        self.assertGreater(result['pre_echo_coverage']['candidates'], 0)
        self.assertGreater(result['pre_echo_coverage']['eligible'], 0)
        self.assertEqual(result['pre_echo_coverage']['eligible'], result['pre_echo_coverage']['scored'])
        self.assertTrue(np.all(np.isfinite(result['pre_echo_db'])))
        self.assertTrue(np.allclose(result['pre_echo_db'], 0.0, atol=1e-7))

    def test_recovers_positive_and_negative_global_offsets(self):
        ref = click_clip()
        for shift in (211, -173):
            with self.subTest(shift=shift):
                result = transient.pre_echo_metrics(ref, offset_audio(ref, shift), SR)
                self.assertLessEqual(abs(result['pre_echo_lag_samples'] + shift), 1)

    def test_stereo_correlations_do_not_cancel_anti_phase_channels(self):
        left = click_clip(channels=1)[:, 0]
        ref = np.column_stack((left, -left))
        result = transient.pre_echo_metrics(ref, offset_audio(ref, 211), SR)
        self.assertGreater(result['pre_echo_coverage']['eligible'], 0)
        self.assertLessEqual(abs(result['pre_echo_lag_samples'] + 211), 1)

    def test_advance_preecho_increases_nper_monotonically(self):
        ref = click_clip()
        quiet = transient.pre_echo_metrics(ref, ref.copy(), SR)
        dec = ref.copy()
        for onset in (SR // 2, SR, SR + SR // 2):
            # A short, lower-level copy before the source transient models
            # energy leakage while retaining the attack itself.
            dec[onset - 240:onset, 0] += 0.08 * ref[onset:onset + 240, 0]
        leaked = transient.pre_echo_metrics(ref, dec, SR)
        self.assertGreater(float(np.median(leaked['pre_echo_db'])),
                           float(np.median(quiet['pre_echo_db'])))

    def test_simple_post_blur_is_distinct_from_centroid_metric(self):
        ref = click_clip()
        blurred = signal.lfilter([0.5, 0.5], [1.0], ref, axis=0)
        pre = transient.pre_echo_metrics(ref, blurred, SR)
        centroids = transient.attack_centroid_deltas(ref[:, 0], blurred[:, 0], SR)
        self.assertEqual(pre['pre_echo_coverage']['scored'], len(pre['pre_echo_db']))
        self.assertTrue(centroids)
        self.assertTrue(np.allclose(pre['pre_echo_db'], 0.0, atol=0.05))
        self.assertNotAlmostEqual(float(np.mean(centroids)), 0.0, delta=1e-3)

    def test_refinement_is_stable_to_seed_shift(self):
        ref = click_clip()
        sos = signal.butter(4, 2000, btype='highpass', fs=SR, output='sos')
        hp = signal.sosfilt(sos, ref[:, 0])
        power = __import__('scipy').ndimage.uniform_filter1d(hp ** 2, size=12)
        seed = SR // 2
        onset_a = transient._refine_pre_echo_onset(hp, seed - 240, power)
        onset_b = transient._refine_pre_echo_onset(hp, seed + 240, power)
        slope = power[48:] - power[:-48]
        onset_fast = transient._refine_pre_echo_onset(hp, seed, power, slope)
        onset_reference = transient._refine_pre_echo_onset(hp, seed, power)
        self.assertIsNotNone(onset_a)
        self.assertIsNotNone(onset_b)
        self.assertLessEqual(abs(onset_a - onset_b), 48)
        self.assertEqual(onset_fast, onset_reference)

    def test_boundary_events_are_not_scored_and_muted_decode_is_not_perfect(self):
        ref = click_clip(length=SR, onsets=(SR // 10, SR // 2))
        boundary = transient.pre_echo_metrics(ref, ref.copy(), SR)
        self.assertGreaterEqual(boundary['pre_echo_coverage']['refined'], 1)
        muted = transient.pre_echo_metrics(ref, np.zeros_like(ref), SR)
        self.assertGreater(muted['pre_echo_coverage']['eligible'], 0)
        self.assertEqual(muted['pre_echo_coverage']['scored'], 0)
        self.assertEqual(muted['pre_echo_db'], [])
        missing_tail = transient.pre_echo_metrics(ref, np.zeros(SR // 4), SR)
        self.assertGreater(missing_tail['pre_echo_coverage']['eligible'], 0)
        self.assertEqual(missing_tail['pre_echo_coverage']['scored'], 0)

    def test_native_rate_resampling_and_validation(self):
        ref = click_clip()
        for native_sr in (44100, 96000):
            with self.subTest(native_sr=native_sr):
                native = signal.resample_poly(ref, native_sr, SR, axis=0)
                result = transient.pre_echo_metrics(native, native.copy(), native_sr)
                self.assertGreater(result['pre_echo_coverage']['scored'], 0)
        with self.assertRaises(ValueError):
            transient.pre_echo_metrics(ref, np.column_stack((ref[:, 0], ref[:, 0])), SR)
        with self.assertRaises(ValueError):
            transient.pre_echo_metrics(ref, ref, 0)
        with self.assertRaises(ValueError):
            bad = ref.copy()
            bad[0, 0] = np.nan
            transient.pre_echo_metrics(bad, ref, SR)

    def test_empty_reference_returns_empty_coverage_and_backfill_predicate(self):
        result = transient.pre_echo_metrics(np.zeros(SR), np.zeros(SR), SR)
        self.assertEqual(result['pre_echo_db'], [])
        self.assertEqual(result['pre_echo_coverage'],
                         {'candidates': 0, 'refined': 0, 'eligible': 0, 'scored': 0})
        self.assertTrue(transient.has_current_pre_echo(result))
        self.assertFalse(transient.has_current_pre_echo({'pre_echo_db': []}))
        invalid = dict(result, pre_echo_error='decode failed')
        self.assertFalse(transient.has_current_pre_echo(invalid))
        invalid = dict(result, pre_echo_db=[float('nan')])
        self.assertFalse(transient.has_current_pre_echo(invalid))
        invalid = dict(result, pre_echo_db=[0.0])
        self.assertFalse(transient.has_current_pre_echo(invalid))
        invalid = dict(result, pre_echo_coverage={
            'candidates': 1, 'refined': 0, 'eligible': 1, 'scored': 0})
        self.assertFalse(transient.has_current_pre_echo(invalid))

    def test_subwindow_inputs_return_no_events(self):
        short = np.ones(300, dtype=np.float64)
        result = transient.pre_echo_metrics(short, short, SR)
        self.assertEqual(result['pre_echo_db'], [])
        self.assertEqual(result['pre_echo_coverage'],
                         {'candidates': 0, 'refined': 0, 'eligible': 0, 'scored': 0})

    def test_nonquiet_reference_events_are_ineligible(self):
        rng = np.random.default_rng(2718)
        ref = rng.normal(0.0, 0.03, SR * 2)
        result = transient.pre_echo_metrics(ref, ref.copy(), SR)
        self.assertGreater(result['pre_echo_coverage']['candidates'], 0)
        self.assertEqual(result['pre_echo_coverage']['eligible'], 0)
        self.assertEqual(result['pre_echo_db'], [])


if __name__ == '__main__':
    unittest.main()
