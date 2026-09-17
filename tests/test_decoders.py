"""
 * FAAC Benchmark Suite - Unit Tests for Decoder Classes & Task Execution
"""

import os
import unittest
import tempfile
import codec_bench.decoders as dec
import utils

class TestDecoders(unittest.TestCase):
    def test_multi_version_faad_detection(self):
        class DummyArgs:
            faad_bin = ["/usr/bin/faad1", "/usr/bin/faad2"]
            faad_lib = None
            faad_bin_version = ["2.10.0", "2.11.1"]
            ffmpeg_bin = None
            afconvert_bin = None

        args = DummyArgs()
        decoders = dec.detect_decoders(args)
        faad_decs = [d for d in decoders if isinstance(d, dec.FAADDecoder)]
        self.assertEqual(len(faad_decs), 2)
        self.assertEqual(faad_decs[0].name, "FAAD2 2.10.0")
        self.assertEqual(faad_decs[1].name, "FAAD2 2.11.1")

    def test_corrupt_adts_bitstream(self):
        with tempfile.TemporaryDirectory() as td:
            in_aac = os.path.join(td, "in.aac")
            out_aac = os.path.join(td, "corrupt.aac")
            fake_adts = b"\xff\xf1\x50\x80\x01\x3f\xfc" + b"\x00" * 30
            with open(in_aac, "wb") as f:
                f.write(fake_adts * 10)

            ok = utils.corrupt_adts_bitstream(in_aac, out_aac, seed=42)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(out_aac))

    def test_process_decoder_task_channel_mismatch(self):
        from helpers import write_wav
        class DummyDecoder(dec.Decoder):
            def __init__(self, out_wav):
                super().__init__("DummyDecoder", "/usr/bin/true", "dummy_dec")
                self.out_wav = out_wav

            def get_decode_cmd(self, input_path, output_path):
                # Dummy command that copies the pre-created out_wav
                return ["cp", self.out_wav, output_path]

        with tempfile.TemporaryDirectory() as td:
            ref_wav = os.path.join(td, "ref.wav")
            dec_out = os.path.join(td, "dec_mono.wav")
            bitstream = os.path.join(td, "test.aac")

            write_wav(ref_wav, seconds=1, sr=48000, ch=2)
            write_wav(dec_out, seconds=1, sr=48000, ch=1)
            with open(bitstream, "wb") as f:
                f.write(b"dummy")

            res_item = {
                "row_key": "enc_row_1",
                "scenario": "48k_stereo_128k",
                "filename": "sample.wav",
                "aac_path": bitstream,
                "ref_path": ref_wav,
                "profile": "lc"
            }

            decoder = DummyDecoder(dec_out)
            res = dec.process_decoder_task(decoder, res_item, td)

            self.assertTrue(res["decode_valid"])
            self.assertIsNotNone(res["mos"])

if __name__ == "__main__":
    unittest.main()
