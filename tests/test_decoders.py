"""
 * FAAC Benchmark Suite - Unit Tests for Decoder Classes & Task Execution
"""

import os
import unittest
import tempfile
import codec_bench.decoders as dec
import utils

class TestDecoders(unittest.TestCase):
    def test_faad3_strict_mode(self):
        faad3 = dec.FAADDecoder("FAAD3 3.0", "/usr/bin/faad3", tool_id="faad3", is_faad3=True)
        cmd = faad3.get_decode_cmd("in.aac", "out.wav")
        self.assertIn("--strict", cmd)

        faad2 = dec.FAADDecoder("FAAD2 2.11", "/usr/bin/faad", tool_id="faad2", is_faad3=False)
        cmd2 = faad2.get_decode_cmd("in.aac", "out.wav")
        self.assertNotIn("--strict", cmd2)

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
        self.assertEqual(faad_decs[0].name, "FAAD 2.10.0")
        self.assertEqual(faad_decs[1].name, "FAAD 2.11.1")

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

    def test_decoder_robustness_task_error_exit_is_crash_free(self):
        class ErrorExitDecoder(dec.Decoder):
            def __init__(self):
                super().__init__("ErrExitDecoder", "/bin/sh", "err_exit_dec")

            def get_decode_cmd(self, input_path, output_path):
                return ["sh", "-c", "exit 1"]

        with tempfile.TemporaryDirectory() as td:
            in_aac = os.path.join(td, "in.aac")
            fake_adts = b"\xff\xf1\x50\x80\x01\x3f\xfc" + b"\x00" * 30
            with open(in_aac, "wb") as f:
                f.write(fake_adts * 10)

            res_item = {
                "row_key": "enc_row_1",
                "scenario": "48k_stereo_128k",
                "filename": "sample.wav",
                "aac_path": in_aac,
                "profile": "lc"
            }

            decoder = ErrorExitDecoder()
            res = dec.process_decoder_robustness_task(decoder, res_item, td)
            self.assertIsNotNone(res)
            self.assertFalse(res["passed"])
            self.assertTrue(res["error_exit"])
            self.assertFalse(res["crash"])
            self.assertTrue(res["crash_free"])

    def _robustness_result(self, shell_cmd):
        class ShellDecoder(dec.Decoder):
            def __init__(self):
                super().__init__("ShellDecoder", "/bin/sh", "shell_dec")

            def get_decode_cmd(self, input_path, output_path):
                return ["sh", "-c", shell_cmd]

        with tempfile.TemporaryDirectory() as td:
            in_aac = os.path.join(td, "in.aac")
            with open(in_aac, "wb") as f:
                f.write((b"\xff\xf1\x50\x80\x01\x3f\xfc" + b"\x00" * 30) * 10)
            res_item = {"row_key": "enc_row_1", "scenario": "48k_stereo_128k",
                        "filename": "sample.wav", "aac_path": in_aac, "profile": "lc"}
            return dec.process_decoder_robustness_task(ShellDecoder(), res_item, td)

    def test_decoder_robustness_task_rejection_message_is_not_a_crash(self):
        for cmd in ("echo 'invalid frame' >&2; exit 1", "echo 'invalid frame' >&2; exit 0"):
            res = self._robustness_result(cmd)
            self.assertFalse(res["crash"], cmd)
            self.assertTrue(res["crash_free"], cmd)

    def test_decoder_robustness_task_signal_is_a_crash(self):
        res = self._robustness_result("kill -SEGV $$")
        self.assertTrue(res["crash"])
        self.assertFalse(res["crash_free"])

if __name__ == "__main__":
    unittest.main()
