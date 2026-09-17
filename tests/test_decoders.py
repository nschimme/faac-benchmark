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

if __name__ == "__main__":
    unittest.main()
