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
            d_obj = dec.FAADDecoder("FAAD2", "/bin/true", "faad")
            results = [{
                "tool": "FAAD2",
                "row_key": "faad",
                "scenario": "48k_stereo_64k",
                "filename": "clip1.wav",
                "duration": 0.01,
                "audio_duration": 5.0,
                "snr_db": 25.5,
                "mos": 4.2,
                "decode_valid": True,
                "decode_error": ""
            }]

            rep.generate_decoder_leaderboard([d_obj], results, out_md, ["48k_stereo_64k"], skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("# 🔊 AAC Decoder Leaderboard", text)
                self.assertIn("FAAD2", text)
                self.assertIn("25.5 dB", text)

if __name__ == "__main__":
    unittest.main()
