"""
 * FAAC Benchmark Suite - Unit Tests for compare_decoders.py
"""

import os
import sys
import unittest
import tempfile
import json
import shutil

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

import utils
import compare_decoders as cd

class TestCompareDecoders(unittest.TestCase):
    def test_compute_snr_identical(self):
        with tempfile.TemporaryDirectory() as td:
            wav1 = os.path.join(td, "w1.wav")
            wav2 = os.path.join(td, "w2.wav")

            # Create dummy 1s mono 48kHz WAV
            import soundfile as sf
            import numpy as np
            data = np.random.uniform(-0.5, 0.5, 48000).astype(np.float32)
            sf.write(wav1, data, 48000)
            sf.write(wav2, data, 48000)

            snr = utils.compute_snr(wav1, wav2)
            self.assertEqual(snr, float("inf"))

    def test_decoder_detection(self):
        class DummyArgs:
            faad_bin = [shutil.which("faad")] if shutil.which("faad") else None
            faad_lib = None
            faad_bin_version = None
            ffmpeg_bin = [utils.get_ffmpeg_path()] if utils.get_ffmpeg_path() else None
            afconvert_bin = None

        args = DummyArgs()
        decoders = cd.detect_decoders(args)
        self.assertGreater(len(decoders), 0)

    def test_generate_decoder_leaderboard(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            dec = cd.FAADDecoder("FAAD2", "/bin/true", "faad")
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

            cd.generate_decoder_leaderboard([dec], results, out_md, ["48k_stereo_64k"], skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("# AAC Leaderboard", text)
                self.assertIn("FAAD2", text)
                self.assertIn("25.5 dB", text)

if __name__ == "__main__":
    unittest.main()
