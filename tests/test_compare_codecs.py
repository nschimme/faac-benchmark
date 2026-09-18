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
import compare_codecs as cd

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

    def test_multi_version_faad_detection(self):
        class DummyArgs:
            faad_bin = ["/usr/bin/faad1", "/usr/bin/faad2"]
            faad_lib = None
            faad_bin_version = ["2.10.0", "2.11.1"]
            ffmpeg_bin = None
            afconvert_bin = None

        args = DummyArgs()
        decoders = cd.detect_decoders(args)
        faad_decs = [d for d in decoders if isinstance(d, cd.FAADDecoder)]
        self.assertEqual(len(faad_decs), 2)
        self.assertEqual(faad_decs[0].name, "FAAD 2.10.0")
        self.assertEqual(faad_decs[1].name, "FAAD 2.11.1")

    def test_generate_decoder_leaderboard(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard.md")
            dec = cd.FAADDecoder("FAAD", "/bin/true", "faad")
            results = [{
                "tool": "FAAD",
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
                self.assertIn("# 🔊 AAC Decoder Leaderboard", text)
                self.assertIn("FAAD", text)
                self.assertIn("25.5 dB", text)

    def test_corrupt_adts_bitstream(self):
        with tempfile.TemporaryDirectory() as td:
            in_aac = os.path.join(td, "in.aac")
            out_aac = os.path.join(td, "corrupt.aac")
            # Create synthetic ADTS header + payload
            fake_adts = b"\xff\xf1\x50\x80\x01\x3f\xfc" + b"\x00" * 30
            with open(in_aac, "wb") as f:
                f.write(fake_adts * 10)

            ok = utils.corrupt_adts_bitstream(in_aac, out_aac, seed=42)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(out_aac))

    def test_measure_delay_offset(self):
        with tempfile.TemporaryDirectory() as td:
            wav1 = os.path.join(td, "w1.wav")
            wav2 = os.path.join(td, "w2.wav")
            import soundfile as sf
            import numpy as np
            data = np.random.uniform(-0.5, 0.5, 48000).astype(np.float32)
            sf.write(wav1, data, 48000)
            sf.write(wav2, data, 48000)

            lag, ms = utils.measure_delay_offset(wav1, wav2)
            self.assertEqual(lag, 0)
            self.assertEqual(ms, 0.0)

    def test_compare_codecs_import(self):
        import compare_codecs
        self.assertTrue(hasattr(compare_codecs, "main"))

    def test_51_surround_leaderboard_rendering(self):
        from compare_codecs import generate_leaderboard, Encoder

        class DummyEncoder(Encoder):
            def __init__(self, name, profile):
                super().__init__(name, None, name.lower(), profile)
            def get_encode_cmd(self, i, o, b, c, s):
                return ["echo"]

        encoders = [DummyEncoder("Tool51", "lc")]
        results = [{
            "tool": "Tool51",
            "profile": "lc",
            "row_key": "tool51_lc",
            "scenario": "44k1_51_256k",
            "filename": "6_Channel_ID.wav",
            "duration": 1.0,
            "audio_duration": 10.0,
            "size": 1000,
            "actual_bitrate": 256.0,
            "target_bitrate": 256,
            "decode_valid": True,
            "decode_error": "",
            "mos": 4.1,
            "ic_err": 0.05,
            "attack_centroid_ms": [1.0]
        }]

        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "leaderboard_51.md")
            scenario_list = ["44k1_51_256k"]
            generate_leaderboard(encoders, results, out_md, scenario_list, skip_graphs=True)
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("44.1 kHz 5.1 Surround", text)
                self.assertIn("Tool51", text)

    def test_mode_both_leaderboard_rendering(self):
        with tempfile.TemporaryDirectory() as td:
            out_md = os.path.join(td, "combined.md")
            dec = cd.FAADDecoder("FAAD", "/bin/true", "faad")
            enc = cd.FFmpegDecoder("FFmpeg AAC", "/bin/true", "ffmpeg_aac")
            results = [{
                "tool": "FAAD",
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

            cd.generate_decoder_leaderboard([dec], results, out_md, ["48k_stereo_64k"], skip_graphs=True, encoders=[enc], encoder_results=[])
            self.assertTrue(os.path.exists(out_md))
            with open(out_md) as f:
                text = f.read()
                self.assertIn("## 🔊 Decoder Leaderboard", text)
                self.assertIn("FAAD", text)

if __name__ == "__main__":
    unittest.main()
