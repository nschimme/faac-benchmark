"""
 * FAAC Benchmark Suite - Unit Tests for Encoder Classes & Auto-Detection
"""

import unittest
from unittest.mock import patch
import codec_bench.encoders as enc
import compare_codecs as cd
from tests.helpers import scenario_at

import contextlib
from unittest import mock as _mock


@contextlib.contextmanager
def unittest_mock_ffmpeg(path):
    with _mock.patch.object(enc, "get_ffmpeg_path", return_value=path):
        yield


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

    def test_faac_default_rate_control_follows_cbr_support(self):
        e = enc.FAACEncoder("FAAC", "/nonexistent/faac")
        e.legacy, e.supports_cbr = False, True
        self.assertNotIn("--cbr", e.get_encode_cmd("in.wav", "o.m4a", 64, 2, 48000))
        e.default_rate_control = "cbr"   # what compare_codecs sets when supports_cbr
        cmd = e.get_encode_cmd("in.wav", "o.m4a", 64, 2, 48000)
        self.assertIn("--cbr", cmd)
        self.assertEqual(cmd[cmd.index("-b") + 1], "64")
        # an explicit caller (phase1, phase4_haze) still wins over the default
        self.assertNotIn("--cbr", e.get_encode_cmd("in.wav", "o.m4a", 64, 2, 48000, rate_control="abr"))

    def test_faac_without_cbr_in_help_is_not_cbr_capable(self):
        from unittest import mock
        enc._FAAC_CBR_CACHE.clear()
        out = mock.Mock(stdout="usage: faac --object-type", stderr="")
        with mock.patch.object(enc, "safe_run", return_value=out):
            self.assertFalse(enc.faac_has_cbr("/x/faac"))
        enc._FAAC_CBR_CACHE.clear()
        out = mock.Mock(stdout="-b ABR  --cbr CBR", stderr="")
        with mock.patch.object(enc, "safe_run", return_value=out):
            self.assertTrue(enc.faac_has_cbr("/y/faac"))
        enc._FAAC_CBR_CACHE.clear()

    def test_afconvert_is_forced_to_cbr(self):
        af = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="lc")
        with unittest_mock_ffmpeg(None):
            cmd = af.get_encode_cmd("in.wav", "o.m4a", 64, 2, 44100)
        self.assertEqual(cmd[cmd.index("-s") + 1], "0")

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

    def test_generate_leaderboard_with_exhale_encoder(self):
        import tempfile
        import os
        exhale_enc = enc.ExhaleEncoder("xHE-AAC (exhale)", "/usr/bin/exhale", tool_id="exhale", profile="standard")
        encoders = [exhale_enc]
        results = [
            {
                "tool": "xHE-AAC (exhale)", "profile": "standard", "row_key": "exhale_standard",
                "scenario": "48k_stereo_64k", "filename": "s1.wav", "duration": 0.5,
                "audio_duration": 10.0, "size": 800, "actual_bitrate": 64.0,
                "target_bitrate": 64, "decode_valid": True, "decode_error": "",
                "mos": 4.12, "ic_err": 0.03, "attack_centroid_ms": [0.8]
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            scenario_list = ["48k_stereo_64k"]
            cd.generate_leaderboard(encoders, results, out_md, scenario_list, skip_graphs=False)

            with open(out_md) as f:
                content = f.read()

            self.assertIn("xHE-AAC (exhale)", content)
            self.assertIn("4.120", content)

    def test_afconvert_rebranding(self):
        af_enc = enc.AFConvertEncoder("Apple AAC 15.3", "/usr/bin/afconvert", tool_id="afconvert", profile="lc")
        self.assertEqual(af_enc.name, "Apple AAC 15.3")

    def test_afconvert_encodes_7_1_with_the_front_side_back_layout(self):
        af = enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="lc")
        self.assertTrue(af.supports_scenario(448, 8, 48000)[0])
        self.assertFalse(af.supports_scenario(448, 7, 48000)[0])
        self.assertFalse(enc.AFConvertEncoder("Apple AAC", "/usr/bin/afconvert", profile="he")
                         .supports_scenario(448, 8, 48000)[0])
        cmd = af.get_encode_cmd("in.wav", "out.m4a", 448, 8, 48000)
        # AAC_7_1_B carries the back pair; MPEG_7_1_B/AAC_7_1 is the wide 7.1 and drops it.
        self.assertEqual(cmd[cmd.index("-l") + 1], "AAC_7_1_B")
        self.assertEqual(cmd[0], "/usr/bin/afconvert")

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
