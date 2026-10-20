"""Rendering for the versioned reference-locked NPER metric."""

import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import codec_bench.report as report
from transient import PRE_ECHO_METRIC_VERSION


class DummyEncoder:
    def __init__(self, name, tool_id):
        self.name = name
        self.tool_id = tool_id
        self.profile = "lc"
        self.text_size = 0
        self.rodata_size = 0
        self.data_size = 0


def current_row(values, coverage, **extra):
    row = {
        "pre_echo_db": values,
        "pre_echo_coverage": coverage,
        "pre_echo_metric_version": PRE_ECHO_METRIC_VERSION,
        "pre_echo_lag_samples": 0,
    }
    row.update(extra)
    return row


class TestPreEchoReport(unittest.TestCase):
    def test_positive_excess_mean(self):
        rows = [current_row(
            [-4.0, 0.0, 3.0, 7.0],
            {"candidates": 8, "refined": 6, "eligible": 5, "scored": 4},
        )]
        summary = report._pre_echo_summary(rows)
        self.assertEqual(summary, 2.5)

    def test_empty_and_legacy_rows_are_not_perfect_scores(self):
        empty = current_row(
            [], {"candidates": 0, "refined": 0, "eligible": 0, "scored": 0},
        )
        legacy = {"pre_echo_db": [0.0], "pre_echo_coverage": {
            "candidates": 1, "refined": 1, "eligible": 1, "scored": 1,
        }}
        wrong_version = current_row(
            [0.0], {"candidates": 1, "refined": 1, "eligible": 1, "scored": 1},
            pre_echo_metric_version=PRE_ECHO_METRIC_VERSION - 1,
        )
        summary = report._pre_echo_summary([empty, legacy, wrong_version])
        self.assertIsNone(summary)

    def test_leaderboard_ranking_stays_mos_based_and_zero_is_displayed(self):
        encoders = [DummyEncoder("Higher MOS", "high"), DummyEncoder("Lower MOS", "low")]
        coverage = {"candidates": 3, "refined": 2, "eligible": 1, "scored": 1}
        shared = {
            "scenario": "48k_stereo_128k", "filename": "clip.wav",
            "duration": 1.0, "audio_duration": 1.0,
            "actual_bitrate": 128.0, "target_bitrate": 128.0,
            "decode_valid": True, "decode_error": "", "ic_err": 0.0,
            "attack_centroid_ms": [0.0],
        }
        results = [
            {**shared, "tool": "Higher MOS", "profile": "lc", "row_key": "high_lc",
             "mos": 4.8, **current_row([10.0], coverage)},
            {**shared, "tool": "Lower MOS", "profile": "lc", "row_key": "low_lc",
             "mos": 4.0, **current_row([0.0], coverage)},
        ]
        fake_scenario = {"48k_stereo_128k": {
            "mode": "audio", "corpus": "audio_48k", "bitrate": 128,
            "rate": 48000, "channels": 2, "visqol_rate": 48000,
        }}
        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "leaderboard.md")
            with patch.dict(config.SCENARIOS, fake_scenario):
                report.generate_leaderboard(encoders, results, output,
                                            ["48k_stereo_128k"], skip_graphs=True)
            text = pathlib.Path(output).read_text(encoding="utf-8")
        self.assertIn("| 🥇 1 | Higher MOS |", text)
        self.assertIn("| 🥈 2 | Lower MOS |", text)
        self.assertIn("| Rank | Encoder | Status | 1st Percentile MOS | Overall MOS | Scenarios | Stereo Fidelity | Speed (xRT) |", text)
        self.assertNotIn("| Attack Fidelity |", text)
        self.assertNotIn("| Pre-echo (dB ↓) |", text)
        self.assertIn("<details><summary><b>Pre-echo diagnostic (", text)
        self.assertIn("| 48k_stereo_128k | Higher MOS | 10.000 |", text)
        self.assertIn("| 48k_stereo_128k | Lower MOS | 0.000 |", text)
        self.assertNotIn("(n=", text)
        self.assertNotIn("Candidates / refined", text)

    def test_legacy_report_renders_n_a(self):
        encoder = DummyEncoder("Legacy", "legacy")
        row = {
            "tool": "Legacy", "profile": "lc", "row_key": "legacy_lc",
            "scenario": "48k_stereo_128k", "filename": "old.wav",
            "duration": 1.0, "audio_duration": 1.0,
            "actual_bitrate": 128.0, "target_bitrate": 128.0,
            "decode_valid": True, "decode_error": "", "mos": 4.0,
            "ic_err": 0.0, "attack_centroid_ms": [1.0],
            "pre_echo_db": [0.0],
        }
        fake_scenario = {"48k_stereo_128k": {
            "mode": "audio", "corpus": "audio_48k", "bitrate": 128,
            "rate": 48000, "channels": 2, "visqol_rate": 48000,
        }}
        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "legacy.md")
            with patch.dict(config.SCENARIOS, fake_scenario):
                report.generate_leaderboard([encoder], [row], output,
                                            ["48k_stereo_128k"], skip_graphs=True)
            text = pathlib.Path(output).read_text(encoding="utf-8")
        self.assertIn("| 🥇 1 | Legacy |", text)
        self.assertIn("| 48k_stereo_128k | Legacy | N/A |", text)
        self.assertIn("N/A means no current scored value", text)
        self.assertNotIn("(n=", text)


if __name__ == "__main__":
    unittest.main()
