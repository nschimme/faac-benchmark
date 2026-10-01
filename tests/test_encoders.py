"""
 * FAAC Benchmark Suite - Unit Tests for Encoder Classes & Auto-Detection
"""

import unittest
from unittest.mock import patch
import codec_bench.encoders as enc
import compare_codecs as cd
from tests.helpers import scenario_at

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

    def test_exhale_encoder_commands(self):
        exhale_enc = enc.ExhaleEncoder("xHE-AAC", "/usr/bin/exhale", tool_id="exhale")
        cmd_low = exhale_enc.get_encode_cmd("in.wav", "out.m4a", 24, 2, 44100)
        self.assertEqual(cmd_low, ["/usr/bin/exhale", "1", "in.wav", "out.m4a"])

        cmd_mid = exhale_enc.get_encode_cmd("in.wav", "out.m4a", 64, 2, 44100)
        self.assertEqual(cmd_mid, ["/usr/bin/exhale", "4", "in.wav", "out.m4a"])

        cmd_high = exhale_enc.get_encode_cmd("in.wav", "out.m4a", 192, 2, 44100)
        self.assertEqual(cmd_high, ["/usr/bin/exhale", "9", "in.wav", "out.m4a"])

        exhale_ff = enc.ExhaleEncoder("xHE-AAC (FFmpeg)", "/usr/bin/ffmpeg", is_ffmpeg=True)
        cmd_ff = exhale_ff.get_encode_cmd("in.wav", "out.m4a", 64, 2, 44100)
        self.assertEqual(cmd_ff, ["/usr/bin/ffmpeg", "-y", "-i", "in.wav", "-c:a", "libmpeghdec", "-b:a", "64k", "-ac", "2", "out.m4a"])

    def test_get_encoder_instance_exhale(self):
        e1 = enc.get_encoder_instance("exhale", binary_path="/usr/bin/exhale")
        self.assertIsInstance(e1, enc.ExhaleEncoder)
        self.assertEqual(e1.binary_path, "/usr/bin/exhale")

        e2 = enc.get_encoder_instance("xhe", binary_path="/usr/bin/exhale")
        self.assertIsInstance(e2, enc.ExhaleEncoder)

    def test_afconvert_rebranding(self):
        af_enc = enc.AFConvertEncoder("Apple AAC 15.3", "/usr/bin/afconvert", tool_id="afconvert", profile="lc")
        self.assertEqual(af_enc.name, "Apple AAC 15.3")

    def test_afconvert_supports_scenario(self):
        af_lc = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="lc")
        ok_lc_6ch, _ = af_lc.supports_scenario(256, 6, 44100)
        self.assertTrue(ok_lc_6ch)

        af_he = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="he")
        ok_he_2ch, _ = af_he.supports_scenario(48, 2, 44100)
        self.assertTrue(ok_he_2ch)
        ok_he_6ch, reason = af_he.supports_scenario(160, 6, 44100)
        self.assertFalse(ok_he_6ch)
        self.assertIn("max 2 channels", reason)

        ok_he_invalid, reason_he = af_he.supports_scenario(16, 2, 44100)
        self.assertFalse(ok_he_invalid)
        self.assertIn("12-32 kbps/ch", reason_he)

        af_hev2 = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="hev2")
        ok_v2_valid, _ = af_hev2.supports_scenario(32, 2, 44100)
        self.assertTrue(ok_v2_valid)
        ok_v2_invalid, reason_v2 = af_hev2.supports_scenario(64, 2, 44100)
        self.assertFalse(ok_v2_invalid)
        self.assertIn("16-48 kbps total", reason_v2)

    def test_apple_probe_uses_scenario_format(self):
        apple = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="hev2")
        low = scenario_at(32000, 2, 16)
        high = scenario_at(32000, 2, 64)
        names = [low, high]
        with patch.object(cd, "probe_encoder_capability", return_value=False) as probe:
            eligible = cd.supported_encoder_scenarios([apple], names)
        self.assertEqual(eligible[low], [])
        self.assertEqual(eligible[high], [])
        probe.assert_called_once_with(apple, 16, 2, 32000)

    def test_old_apple_failures_are_not_reused(self):
        apple = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="he")
        unsupported = scenario_at(32000, 2, 16)
        supported = scenario_at(32000, 2, 48)
        eligible = {unsupported: [], supported: [apple]}
        rows = [
            {"row_key": "afconvert_he", "scenario": unsupported, "decode_valid": False},
            {"row_key": "afconvert_he", "scenario": supported, "decode_valid": False},
            {"row_key": "afconvert_he", "scenario": supported, "decode_valid": True},
        ]
        kept = cd.reusable_encoder_results(rows, [apple], list(eligible), eligible)
        self.assertEqual(kept, [rows[2]])

    def test_resume_retries_missing_mos(self):
        apple = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="lc")
        speech = scenario_at(16000, 1, 20)
        eligible = {speech: [apple]}
        row = {"row_key": "afconvert_lc", "scenario": speech,
               "decode_valid": True, "mos": None}
        self.assertEqual(cd.reusable_encoder_results([row], [apple], list(eligible), eligible,
                                                     require_mos=True), [])

    def test_legacy_rows_need_unique_active_eligible_encoder(self):
        apple = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="he")
        unsupported = scenario_at(32000, 2, 16)
        supported = scenario_at(32000, 2, 48)
        eligible = {unsupported: [], supported: [apple]}
        rows = [
            {"tool": "Apple AAC", "profile": "he", "scenario": unsupported, "decode_valid": True},
            {"tool": "Apple AAC", "profile": "he", "scenario": supported, "decode_valid": True},
            {"tool": "Old Apple AAC", "profile": "he", "scenario": supported, "decode_valid": True},
            {"row_key": "old_apple_he", "scenario": supported, "decode_valid": True},
        ]
        kept = cd.reusable_encoder_results(rows, [apple], list(eligible), eligible)
        self.assertEqual(kept, [{**rows[1], "row_key": "afconvert_he"}])
        duplicate = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert",
                                         tool_id="other_afconvert", profile="he")
        self.assertEqual(cd.reusable_encoder_results([rows[1]], [apple, duplicate],
                                                      list(eligible), eligible), [])

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
