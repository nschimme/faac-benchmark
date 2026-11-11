"""coherence_error compares frames by index, so a decode at another rate is put on the reference's grid."""

import os
import sys
import tempfile
import unittest
import wave

import numpy as np
from scipy.signal import butter, resample_poly, sosfiltfilt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from phase3_stereo import coherence_error


def stereo(rate, seconds=3):
    # Band-limited below the lowest rate used, so resampling keeps the whole signal.
    rng = np.random.default_rng(1)
    sos = butter(6, 12000, btype="low", fs=rate, output="sos")
    left = sosfiltfilt(sos, rng.standard_normal(rate * seconds))
    right = 0.5 * left + 0.5 * sosfiltfilt(sos, rng.standard_normal(left.size))
    return np.stack([left, right], axis=1)


def write(path, pcm, rate):
    pcm = np.clip(pcm * 3000, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


class CoherenceRate(unittest.TestCase):
    def test_other_rate_is_resampled_to_the_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref, deg = os.path.join(tmp, "ref.wav"), os.path.join(tmp, "deg.wav")
            pcm = stereo(48000)
            write(ref, pcm, 48000)
            write(deg, resample_poly(pcm, 2, 3, axis=0), 32000)
            self.assertLess(coherence_error(ref, deg), 0.001)

    def test_matching_rate_is_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref, deg = os.path.join(tmp, "ref.wav"), os.path.join(tmp, "deg.wav")
            pcm = stereo(48000)
            write(ref, pcm, 48000)
            write(deg, pcm, 48000)
            self.assertAlmostEqual(coherence_error(ref, deg), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
