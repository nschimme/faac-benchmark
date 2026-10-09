import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.decoder_ab as decoder_ab


class DummyDecoder:
    def __init__(self, tool_id):
        self.tool_id = tool_id
        self.name = tool_id

    def get_decode_cmd(self, input_path, output_path):
        return [self.tool_id, "-o", output_path, input_path]

    def get_run_env(self):
        return {}


class TestDecoderAB(unittest.TestCase):
    def test_rotating_order_balances_each_position(self):
        names = ["baseline", "candidate", "control"]
        orders = [decoder_ab.balanced_order(names, i) for i in range(6)]
        self.assertEqual(orders[0], ["baseline", "candidate", "control"])
        for name in names:
            self.assertEqual(sum(order.index(name) == 0 for order in orders), 2)
            self.assertEqual(sum(order.index(name) == 1 for order in orders), 2)
            self.assertEqual(sum(order.index(name) == 2 for order in orders), 2)

    def test_interleaved_trials_keep_order_and_continue_after_failure(self):
        decoders = [DummyDecoder(name) for name in ("baseline", "candidate", "control")]
        prepared = {d.tool_id: {"path": d.tool_id + ".m4a"} for d in decoders}
        calls = []
        counts = {d.tool_id: 0 for d in decoders}

        def measure(decoder, path, output_dir):
            calls.append(decoder.tool_id)
            counts[decoder.tool_id] += 1
            if decoder.tool_id == "candidate" and counts[decoder.tool_id] == 2:
                raise RuntimeError("synthetic decode failure")
            return {"milliseconds": 100.0 - 10.0 * (decoder.tool_id == "candidate"),
                    "peak_rss_kb": 1, "command": [decoder.tool_id], "output_bytes": 4,
                    "output_info": {"rate": 48000, "channels": 2, "frames": 1, "subtype": "PCM_16"}}

        result = decoder_ab.run_interleaved_trials(decoders, prepared, ".", 3, measure_once=measure)
        self.assertEqual(calls, [
            "baseline", "candidate", "control",  # warmups
            "baseline", "candidate", "control",
            "candidate", "control", "baseline",
            "control", "baseline", "candidate",
        ])
        self.assertFalse(result["rounds"][0]["attempts"]["candidate"]["ok"])
        self.assertEqual(result["paired"]["candidate_vs_baseline"]["paired_rounds"], 2)
        self.assertEqual(len(result["summary"]["baseline"]["samples_ms"]), 3)

    def test_pcm_preflight_accepts_equal_output_and_records_commands(self):
        decoders = [DummyDecoder("baseline"), DummyDecoder("candidate")]
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source.m4a"
            source.write_bytes(b"source")

            def fake_run(command, env=None, capture_output=True, check=False):
                Path(command[command.index("-o") + 1]).write_bytes(b"identical wav bytes")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(decoder_ab, "safe_run", side_effect=fake_run):
                rows = decoder_ab.preflight_pcm(decoders, [source], Path(td) / "out", ["16"])

            self.assertTrue(rows[0]["byte_identical"])
            self.assertIn("-b", rows[0]["outputs"]["baseline"]["command"])
            self.assertEqual(rows[0]["outputs"]["baseline"]["sha256"],
                             rows[0]["outputs"]["candidate"]["sha256"])

    def test_pcm_preflight_rejects_mismatch(self):
        decoders = [DummyDecoder("baseline"), DummyDecoder("candidate")]
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source.m4a"
            source.write_bytes(b"source")

            def fake_run(command, env=None, capture_output=True, check=False):
                payload = b"base" if command[0] == "baseline" else b"candidate"
                Path(command[command.index("-o") + 1]).write_bytes(payload)
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(decoder_ab, "safe_run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "PCM mismatch"):
                    decoder_ab.preflight_pcm(decoders, [source], Path(td) / "out", ["32f"])


if __name__ == "__main__":
    unittest.main()
