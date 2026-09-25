"""
 * FAAC Benchmark Suite - Unit Tests for Leaderboard & Report Generation
"""

import os
import unittest
import tempfile
import codec_bench.report as rep
import codec_bench.decoders as dec
import codec_bench.encoders as enc

class TestReport(unittest.TestCase):
    def test_generate_decoder_leaderboard(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            d_obj = dec.FAADDecoder("FAAD", "/bin/true", "faad")
            results = [{
                "tool": "FAAD",
                "row_key": "faad",
                "scenario": "48k_stereo_64k",
                "filename": "clip1.wav",
                "duration": 0.01,
                "audio_duration": 5.0,
                "snr_db": 25.5,
                "alignment_delay_ms": 2.5,
                "mos": 4.2,
                "decode_valid": True,
                "decode_error": ""
            }]

            rep.generate_decoder_leaderboard([d_obj], results, out_md, ["48k_stereo_64k"], skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("# 🔊 AAC Decoder Leaderboard", text)
                self.assertIn("FAAD", text)
                self.assertIn("25.5 dB", text)
                self.assertIn("2.50 ms", text)
                self.assertIn("█", text)

    def test_decoder_quality_outliers_and_mono_downmix(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            d1 = dec.FAADDecoder("FAAD", "/bin/true", "faad")
            d2 = dec.HelixAACDecoder("Helix", "/bin/true", "helix")
            results = [
                {
                    "tool": "FAAD", "row_key": "faad", "scenario": "48k_stereo_16k",
                    "filename": "clip1.wav", "profile": "hev2", "duration": 0.01,
                    "audio_duration": 5.0, "snr_db": 30.0, "mos": 4.1,
                    "decode_valid": True, "decode_error": "", "mono_downmix": False
                },
                {
                    "tool": "Helix", "row_key": "helix", "scenario": "48k_stereo_16k",
                    "filename": "clip1.wav", "profile": "hev2", "duration": 0.01,
                    "audio_duration": 5.0, "snr_db": 15.0, "mos": 2.1,
                    "decode_valid": True, "decode_error": "", "mono_downmix": True
                }
            ]

            rep.generate_decoder_leaderboard([d1, d2], results, out_md, ["48k_stereo_16k"], skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("No HE-v2 PS (1ch)", text)
                self.assertIn("(1ch)", text)
                self.assertIn("Decoder Quality Outliers", text)

    def test_unknown_row_key_fallback_and_timeout_rendering(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            results = [
                {
                    "tool": "Apple AudioToolbox", "row_key": "apple_aac_2_0_he", "scenario": "48k_stereo_32k",
                    "filename": "clip1.wav", "profile": "he", "duration": 0.0,
                    "audio_duration": 5.0, "actual_bitrate": 32.0, "target_bitrate": 32,
                    "decode_valid": False, "decode_error": "Timeout expired", "timeout": True
                }
            ]

            rep.generate_leaderboard([], results, out_md, ["48k_stereo_32k"], skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))

            rep.generate_decoder_leaderboard([], results, out_md, ["48k_stereo_32k"], skip_graphs=True)
            with open(out_md) as f:
                text = f.read()
                self.assertIn("1x timeout", text)
                self.assertIn("Timeout expired", text)

    def test_methodology_and_failure_diagnostics_sections(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            e1 = enc.FAACEncoder("FAAC 2.1", "/bin/true", "faac", profile="lc")
            results = [
                {
                    "tool": "FAAC 2.1", "row_key": "faac_lc", "scenario": "48k_stereo_64k",
                    "filename": "clip1.wav", "profile": "lc", "duration": 0.01,
                    "audio_duration": 5.0, "actual_bitrate": 64.0, "target_bitrate": 64,
                    "mos": 4.5, "decode_valid": True, "decode_error": ""
                },
                {
                    "tool": "FAAC 2.1", "row_key": "faac_lc", "scenario": "48k_stereo_64k",
                    "filename": "clip2.wav", "profile": "lc", "duration": 0.0,
                    "audio_duration": 5.0, "actual_bitrate": None, "target_bitrate": 64,
                    "mos": None, "decode_valid": False, "decode_error": "Encoding failed: exit code 1"
                }
            ]

            rep.generate_leaderboard([e1], results, out_md, ["48k_stereo_64k"], skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("Methodology & Ranking Note", text)
                self.assertIn("Failure Analysis & Debugging Diagnostics", text)
                self.assertIn("Encoding failed: exit code 1", text)

if __name__ == "__main__":
    unittest.main()
