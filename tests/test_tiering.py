import unittest

import numpy as np

import config
import setup_datasets as sd
from utils import loudest_window_start


class TestTiering(unittest.TestCase):
    def test_visqol_tiers(self):
        self.assertEqual(sd.get_tier_params(3.0), (0, 5, True))
        self.assertEqual(sd.get_tier_params(7.0), (None, None, False))
        self.assertEqual(sd.get_tier_params(30.0), (10.0, 10, False))

    def test_only_speech_mode_corpora_are_visqol(self):
        self.assertTrue(config.corpus_is_visqol("speech_clean_16k"))
        self.assertTrue(config.corpus_is_visqol("speech_voip_16k"))
        for key in ("speech_clean_24k", "audio_48k", "audio_44k1", "audio_32k"):
            self.assertFalse(config.corpus_is_visqol(key), key)

    def test_zimtohrli_corpora_keep_the_full_clip(self):
        self.assertEqual(sd.tier_params_for("audio_48k", 30.0), (None, None, False))
        self.assertEqual(sd.tier_params_for("audio_48k", 3.0), (None, None, False))
        self.assertEqual(sd.tier_params_for("speech_clean_16k", 30.0), (10.0, 10, False))


class TestLoudestWindow(unittest.TestCase):
    def test_skips_leading_silence(self):
        x = np.zeros(1000)
        x[600:700] = 1.0
        s = loudest_window_start(x, 100)
        self.assertEqual(s, 600)

    def test_short_signal_starts_at_zero(self):
        self.assertEqual(loudest_window_start(np.ones(50), 100), 0)


if __name__ == "__main__":
    unittest.main()
