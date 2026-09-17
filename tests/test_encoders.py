"""
 * FAAC Benchmark Suite - Unit Tests for Encoder Classes & Auto-Detection
"""

import unittest
import codec_bench.encoders as enc

class TestEncoders(unittest.TestCase):
    def test_profile_labels(self):
        self.assertEqual(enc.profile_label("lc"), "LC")
        self.assertEqual(enc.profile_label("he"), "HE-v1")
        self.assertEqual(enc.profile_label("hev2"), "HE-v2")
        self.assertEqual(enc.profile_label("standard"), "Standard")

    def test_heuristics(self):
        self.assertTrue(enc.use_he_aac(20, 2, 44100))
        self.assertFalse(enc.use_he_aac(128, 2, 44100))

        self.assertTrue(enc.use_he_v2_aac(16, 2, 44100))
        self.assertFalse(enc.use_he_v2_aac(16, 1, 44100))

    def test_encoder_commands(self):
        faac_enc = enc.FAACEncoder("FAAC 2.0", "/usr/bin/faac", tool_id="faac_2_0", profile="lc")
        cmd = faac_enc.get_encode_cmd("in.wav", "out.m4a", 128, 2, 44100)
        self.assertIn("faac", cmd[0])
        self.assertIn("-b", cmd)
        self.assertIn("128", cmd)

if __name__ == "__main__":
    unittest.main()
