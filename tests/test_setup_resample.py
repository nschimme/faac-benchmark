import os
import tempfile
import unittest

import numpy as np
import soundfile as sf
from scipy.signal import welch

import setup_datasets as sd


def _image_level_db(path):
    """Mean level just above the source Nyquist vs the 1-10 kHz passband."""
    x, sr = sf.read(path)
    f, p = welch(x, sr, nperseg=8192)
    d = 10 * np.log10(p + 1e-30)
    return d[(f >= 22500) & (f < 23800)].mean() - d[(f >= 1000) & (f < 10000)].mean()


class TestMusicResampler(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.src = os.path.join(self._td.name, "noise44.wav")
        rng = np.random.default_rng(0)
        # Quiet enough not to clip when written back out as 16-bit.
        sf.write(self.src, 0.08 * rng.standard_normal(44100 * 6), 44100, subtype="FLOAT")

    def test_hq_suppresses_the_image_above_the_source_nyquist(self):
        default = os.path.join(self._td.name, "default.wav")
        hq = os.path.join(self._td.name, "hq.wav")
        sd.resample(self.src, default, 48000, 1)
        sd.resample(self.src, hq, 48000, 1, hq=True)
        self.assertGreater(_image_level_db(default), -30)   # ffmpeg defaults leak it
        self.assertLess(_image_level_db(hq), -60)

    def test_hq_output_is_16bit_at_the_requested_rate_and_channels(self):
        out = os.path.join(self._td.name, "hq.wav")
        sd.resample(self.src, out, 48000, 2, hq=True)
        info = sf.info(out)
        self.assertEqual((info.samplerate, info.channels, info.subtype), (48000, 2, "PCM_16"))

    def test_same_rate_audio_is_not_altered_by_hq(self):
        src = os.path.join(self._td.name, "n48.wav")
        rng = np.random.default_rng(1)
        sf.write(src, 0.08 * rng.standard_normal(48000 * 3), 48000, subtype="PCM_16")
        out = os.path.join(self._td.name, "hq.wav")
        sd.resample(src, out, 48000, 1, hq=True)
        a, _ = sf.read(src, dtype="int16")
        b, _ = sf.read(out, dtype="int16")
        self.assertTrue((a == b).all())


if __name__ == "__main__":
    unittest.main()
