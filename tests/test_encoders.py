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

    def test_channel_support_51(self):
        lame = enc.LameEncoder("LAME", "/usr/bin/lame")
        ok_2ch, _ = lame.supports_scenario(128, 2, 44100)
        self.assertTrue(ok_2ch)
        ok_6ch, reason = lame.supports_scenario(256, 6, 44100)
        self.assertFalse(ok_6ch)
        self.assertIn("max 2 channels", reason)

        fdk_hev2 = enc.FDKAACEncoder("fdkaac", "/usr/bin/fdkaac", profile="hev2")
        ok_hev2_2ch, _ = fdk_hev2.supports_scenario(16, 2, 44100)
        self.assertTrue(ok_hev2_2ch)
        ok_hev2_6ch, reason_hev2 = fdk_hev2.supports_scenario(256, 6, 44100)
        self.assertFalse(ok_hev2_6ch)
        self.assertIn("exactly 2 channels", reason_hev2)

    def test_encoder_commands(self):
        faac_enc = enc.FAACEncoder("FAAC 2.0", "/usr/bin/faac", tool_id="faac_2_0", profile="lc")
        cmd = faac_enc.get_encode_cmd("in.wav", "out.m4a", 128, 2, 44100)
        self.assertIn("faac", cmd[0])
        self.assertIn("-b", cmd)
        self.assertIn("128", cmd)

    def test_afconvert_rebranding(self):
        af_enc = enc.AFConvertEncoder("Apple AAC 15.3", "/usr/bin/afconvert", tool_id="afconvert", profile="lc")
        self.assertEqual(af_enc.name, "Apple AAC 15.3")

    def test_detect_encoders_optin_variations(self):
        import argparse
        # By default, variations like PNS-off and ADTS should be omitted.
        args_default = argparse.Namespace(mode="both", faac_bin=["/usr/bin/true"], faac_lib=None,
                                          faac_bin_version="1.0.8", fdkaac_bin=None, aac_enc_bin=None,
                                          falabaac_bin=None, ffmpeg_bin=None, afconvert_bin=None)
        encs = enc.detect_encoders(args_default)
        self.assertTrue(all(not getattr(e, "pns_free", False) for e in encs))
        self.assertTrue(all(not getattr(e, "adts", False) for e in encs))

        # When opt-in flags are provided, variations should be included.
        args_variations = argparse.Namespace(mode="both", faac_bin=["/usr/bin/true"], faac_lib=None,
                                             faac_bin_version="1.0.8", fdkaac_bin=None, aac_enc_bin=None,
                                             falabaac_bin=None, ffmpeg_bin=None, afconvert_bin=None,
                                             include_encoder_variations=True)
        encs_var = enc.detect_encoders(args_variations)
        has_pns_free = any(getattr(e, "pns_free", False) for e in encs_var)
        self.assertTrue(has_pns_free)

if __name__ == "__main__":
    unittest.main()
