import os
import tempfile
import unittest
import wave
from unittest import mock

import utils


def _wav(path, seconds, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))


class TestDurationsMatch(unittest.TestCase):
    def test_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            ref, same, half = (os.path.join(td, n) for n in ("r.wav", "s.wav", "h.wav"))
            _wav(ref, 2.0)
            _wav(same, 2.0)
            _wav(half, 1.0)
            self.assertTrue(utils.durations_match(ref, same))
            self.assertFalse(utils.durations_match(ref, half))

    def test_unprobeable_counts_as_match(self):
        with mock.patch.object(utils, "ffmpeg_probe", return_value=None):
            self.assertTrue(utils.durations_match("a", "b"))


if __name__ == "__main__":
    unittest.main()
