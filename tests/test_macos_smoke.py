"""Small, corpus-free macOS checks for the leaderboard's native dependencies."""

import math
import os
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phase2_mos
import compare_codecs
from codec_bench.encoders import AFConvertEncoder, probe_encoder_capability
from tests.helpers import scenario_at


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("RUN_MACOS_NATIVE_SMOKE") == "1",
                     "requires unrestricted macOS audio services")
class TestMacOSLeaderboardSmoke(unittest.TestCase):
    def test_visqol_speech_scores_16k_wav(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "speech.wav")
            samples = bytearray()
            for i in range(16000 * 10):
                t = i / 16000
                level = 0.0 if t < 0.5 or t >= 9.5 else 0.3
                value = int(32767 * level * math.sin(2 * math.pi * 220 * t))
                samples.extend(value.to_bytes(2, "little", signed=True))
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(samples)
            mos, backend = phase2_mos.score_wav_pair(path, path, mode_str="speech")
            self.assertEqual(backend, "visqol-python")
            self.assertIsNotNone(mos)
            self.assertGreater(mos, 1.0)

    def test_apple_scenario_probe(self):
        speech_encoder = AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="lc")
        self.assertTrue(probe_encoder_capability(speech_encoder, 20, 1, 16000))
        encoder = AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="hev2")
        self.assertTrue(probe_encoder_capability(encoder, 24, 2, 44100))
        supported = probe_encoder_capability(encoder, 16, 2, 32000)
        scenario = scenario_at(32000, 2, 16)
        eligible = compare_codecs.supported_encoder_scenarios([encoder], [scenario])
        self.assertEqual(encoder in eligible[scenario], supported)


if __name__ == "__main__":
    unittest.main()
