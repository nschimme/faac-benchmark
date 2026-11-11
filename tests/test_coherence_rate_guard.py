"""coherence_error compares frames by index, so a decode at another rate must be rejected."""

import os
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from phase3_stereo import coherence_error


def write_stereo(path, rate, seconds=2):
    rng = np.random.default_rng(1)
    left = rng.standard_normal(rate * seconds)
    pcm = np.stack([left, 0.5 * left + 0.5 * rng.standard_normal(left.size)], axis=1)
    pcm = np.clip(pcm * 3000, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


class CoherenceRateGuard(unittest.TestCase):
    def test_mismatched_rate_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref, deg = os.path.join(tmp, "ref.wav"), os.path.join(tmp, "deg.wav")
            write_stereo(ref, 48000)
            write_stereo(deg, 32000)
            with self.assertRaisesRegex(ValueError, "sample-rate mismatch: 48000 vs 32000"):
                coherence_error(ref, deg)

    def test_matching_rate_is_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref, deg = os.path.join(tmp, "ref.wav"), os.path.join(tmp, "deg.wav")
            write_stereo(ref, 48000)
            write_stereo(deg, 48000)
            self.assertAlmostEqual(coherence_error(ref, deg), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
