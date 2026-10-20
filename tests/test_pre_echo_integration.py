import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

import compare_codecs
import phase3_stereo
import transient


def metric(values=None):
    return {
        "pre_echo_db": list(values or []),
        "pre_echo_coverage": {"candidates": 3, "refined": 3, "eligible": 2, "scored": len(values or [])},
        "pre_echo_metric_version": transient.PRE_ECHO_METRIC_VERSION,
        "pre_echo_lag_samples": 0,
    }


class CompareResumeBackfillTests(unittest.TestCase):
    def test_resolver_never_guesses_tagged_prefix_and_rejects_ambiguous_exact_files(self):
        with tempfile.TemporaryDirectory() as td:
            row = {"row_key": "enc", "scenario": "scenario", "filename": "clip.wav"}
            wrong = os.path.join(td, "enc_scenario_clip.wav_tagged.m4a")
            open(wrong, "wb").close()
            self.assertIsNone(compare_codecs._resolve_existing_row_path(row, "aac_path", output_dir=td))
            for suffix in ("_base", "_cand"):
                candidate = os.path.join(td, f"enc_scenario_clip.wav{suffix}.m4a")
                open(candidate, "wb").close()
            with self.assertRaisesRegex(ValueError, "ambiguous exact AAC candidates"):
                compare_codecs._resolve_existing_row_path(row, "aac_path", output_dir=td)

    def test_missing_recorded_path_does_not_fall_back_and_relative_ambiguity_errors(self):
        with tempfile.TemporaryDirectory() as td:
            first = os.path.join(td, "first")
            second = os.path.join(td, "second")
            os.makedirs(first)
            os.makedirs(second)
            key = "enc_scenario_clip.wav"
            exact = os.path.join(first, key + ".m4a")
            open(exact, "wb").close()
            row = {"row_key": "enc", "scenario": "scenario", "filename": "clip.wav",
                   "aac_path": "missing-recorded.m4a"}
            self.assertIsNone(compare_codecs._resolve_existing_row_path(row, "aac_path", output_dir=first))
            row["aac_path"] = key + ".m4a"
            duplicate = os.path.join(second, key + ".m4a")
            open(duplicate, "wb").close()
            with self.assertRaisesRegex(ValueError, "ambiguous recorded audio path"):
                compare_codecs._resolve_existing_row_path(
                    row, "aac_path", saved_jsons=[os.path.join(first, "results.json"),
                                                   os.path.join(second, "results.json")])

    def test_backfill_only_updates_metric_and_skips_current_rows(self):
        with tempfile.TemporaryDirectory() as td:
            ref = os.path.join(td, "ref.wav")
            aac = os.path.join(td, "existing.m4a")
            sf.write(ref, np.zeros((32, 2), dtype=np.float32), 48000)
            open(aac, "wb").close()
            row = {"row_key": "enc", "scenario": "unit", "filename": "ref.wav",
                   "aac_path": aac, "ref_path": ref, "mos": 4.2, "ic_err": 0.13,
                   "attack_centroid_ms": [0.4], "encode_cmd": ["encode", "once"],
                   "pre_echo_metric_version": 0, "pre_echo_error": "previous decode failed"}
            current = {**row, **metric([1.25])}
            current.pop("pre_echo_error")
            def convert_audio(source, destination, **kwargs):
                sf.write(destination, np.zeros((32, 2), dtype=np.float32), 48000)
                return True
            with mock.patch.object(compare_codecs, "get_audio_info", return_value=(48000, 2)), \
                 mock.patch.object(compare_codecs, "wav_conv", side_effect=convert_audio) as convert, \
                 mock.patch.object(compare_codecs.transient, "pre_echo_metrics", return_value=metric([2.5])) as score, \
                 mock.patch.object(compare_codecs, "measure_peak_ram") as encode, \
                 mock.patch.object(compare_codecs.phase2_mos, "score_wav_pair") as mos:
                self.assertEqual(compare_codecs.refresh_pre_echo_results([row, current], td), 1)
            self.assertEqual(row["pre_echo_db"], [2.5])
            self.assertNotIn("pre_echo_error", row)
            self.assertEqual(current["pre_echo_db"], [1.25])
            for k in ("mos", "ic_err", "attack_centroid_ms", "encode_cmd"):
                self.assertEqual(row[k], {"mos": 4.2, "ic_err": 0.13,
                                          "attack_centroid_ms": [0.4],
                                          "encode_cmd": ["encode", "once"]}[k])
            self.assertEqual(convert.call_count, 2)
            score.assert_called_once()
            encode.assert_not_called()
            mos.assert_not_called()

    def test_missing_audio_is_error_not_empty_perfect_result_and_skip_is_noop(self):
        row = {"row_key": "enc", "scenario": "unit", "filename": "missing.wav"}
        self.assertEqual(compare_codecs.refresh_pre_echo_results([row], "/missing"), 1)
        self.assertIn("unavailable", row["pre_echo_error"])
        self.assertNotIn("pre_echo_db", row)
        self.assertFalse(transient.has_current_pre_echo(row))
        with mock.patch.object(compare_codecs, "wav_conv") as convert:
            self.assertEqual(compare_codecs.refresh_pre_echo_results([row], "/missing", skip_transient=True), 0)
        convert.assert_not_called()

    def test_failed_row_does_not_backfill_from_stale_inferred_aac(self):
        with tempfile.TemporaryDirectory() as td:
            stale_aac = os.path.join(td, "enc_unit_clip.wav.m4a")
            open(stale_aac, "wb").close()
            ref = os.path.join(td, "clip.wav")
            sf.write(ref, np.zeros((32, 2), dtype=np.float32), 48000)
            row = {"row_key": "enc", "scenario": "unit", "filename": "clip.wav",
                   "decode_valid": False, "aac_path": None, "ref_path": ref,
                   "mos": 4.1, "ic_err": 0.2, "attack_centroid_ms": [0.5]}
            with mock.patch.object(compare_codecs, "wav_conv") as convert, \
                 mock.patch.object(compare_codecs.transient, "pre_echo_metrics") as score:
                self.assertEqual(compare_codecs.refresh_pre_echo_results(
                    [row], td, output_dir=td), 1)
            self.assertIn("failed decode validation", row["pre_echo_error"])
            self.assertFalse(transient.has_current_pre_echo(row))
            self.assertEqual((row["mos"], row["ic_err"], row["attack_centroid_ms"]),
                             (4.1, 0.2, [0.5]))
            convert.assert_not_called()
            score.assert_not_called()

    def test_missing_recorded_reference_does_not_fall_back_to_same_basename(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = os.path.join(td, "corpus")
            os.makedirs(corpus)
            sf.write(os.path.join(corpus, "clip.wav"),
                     np.zeros((32, 2), dtype=np.float32), 48000)
            aac = os.path.join(td, "existing.m4a")
            open(aac, "wb").close()
            row = {"row_key": "enc", "scenario": "unit", "filename": "clip.wav",
                   "decode_valid": True, "aac_path": aac,
                   "ref_path": os.path.join(td, "old", "clip.wav")}
            with mock.patch.object(compare_codecs, "SCENARIOS", {"unit": {"corpus": "unit"}}), \
                 mock.patch.object(compare_codecs, "corpus_dir", return_value=corpus), \
                 mock.patch.object(compare_codecs, "wav_conv") as convert, \
                 mock.patch.object(compare_codecs.transient, "pre_echo_metrics") as score:
                self.assertEqual(compare_codecs.refresh_pre_echo_results([row], td), 1)
            self.assertEqual(row["pre_echo_error"], "existing reference WAV unavailable")
            convert.assert_not_called()
            score.assert_not_called()

    def test_ambiguous_relative_recorded_reference_becomes_error(self):
        with tempfile.TemporaryDirectory() as td:
            first = os.path.join(td, "first")
            second = os.path.join(td, "second")
            for root in (first, second):
                os.makedirs(os.path.join(root, "refs"))
                sf.write(os.path.join(root, "refs", "clip.wav"),
                         np.zeros((32, 2), dtype=np.float32), 48000)
            aac = os.path.join(td, "existing.m4a")
            open(aac, "wb").close()
            row = {"row_key": "enc", "scenario": "unit", "filename": "clip.wav",
                   "decode_valid": True, "aac_path": aac,
                   "ref_path": os.path.join("refs", "clip.wav")}
            saved_jsons = [os.path.join(first, "results.json"),
                           os.path.join(second, "results.json")]
            with mock.patch.object(compare_codecs, "wav_conv") as convert, \
                 mock.patch.object(compare_codecs.transient, "pre_echo_metrics") as score:
                self.assertEqual(compare_codecs.refresh_pre_echo_results(
                    [row], td, saved_jsons=saved_jsons), 1)
            self.assertIn("ambiguous recorded reference path", row["pre_echo_error"])
            convert.assert_not_called()
            score.assert_not_called()

    def test_changed_reference_signature_is_rejected_before_conversion(self):
        with tempfile.TemporaryDirectory() as td:
            ref = os.path.join(td, "clip.wav")
            sf.write(ref, np.zeros((32, 2), dtype=np.float32), 48000)
            aac = os.path.join(td, "existing.m4a")
            open(aac, "wb").close()
            row = {"row_key": "enc", "scenario": "unit", "filename": "clip.wav",
                   "decode_valid": True, "aac_path": aac, "ref_path": ref,
                   "input_sig": "stale-signature"}
            with mock.patch.object(compare_codecs, "wav_conv") as convert, \
                 mock.patch.object(compare_codecs.transient, "pre_echo_metrics") as score:
                self.assertEqual(compare_codecs.refresh_pre_echo_results([row], td), 1)
            self.assertIn("no longer matches input_sig", row["pre_echo_error"])
            convert.assert_not_called()
            score.assert_not_called()


class Phase3BackfillTests(unittest.TestCase):
    def test_cached_centroid_and_coherence_survive_preecho_only_backfill(self):
        with tempfile.TemporaryDirectory() as td:
            ref_dir = os.path.join(td, "corpus")
            aac_dir = os.path.join(td, "aac")
            os.makedirs(ref_dir)
            os.makedirs(aac_dir)
            ref = os.path.join(ref_dir, "stereo.wav")
            aac = os.path.join(aac_dir, "enc_case_stereo.m4a")
            sf.write(ref, np.zeros((512, 2), dtype=np.float32), 48000)
            open(aac, "wb").close()
            result_path = os.path.join(td, "results.json")
            original = {"scenario": "stereo_case", "filename": "stereo.wav",
                        "tool": "enc", "aac_path": aac, "ref_path": ref,
                        "ic_err": 0.125, "attack_centroid_ms": [0.25]}
            with open(result_path, "w") as f:
                json.dump({"matrix": {"enc_stereo_case_stereo.wav": original}}, f)

            class InlinePool:
                def __init__(self, **kwargs):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def submit(self, fn, *args):
                    class Done:
                        def result(self): return fn(*args)
                    return Done()

            with mock.patch.object(phase3_stereo, "SCENARIOS", {"stereo_case": {"channels": 2, "corpus": ref_dir}}), \
                 mock.patch.object(phase3_stereo, "scenario_channels", return_value=2), \
                 mock.patch.object(phase3_stereo, "corpus_dir", return_value=ref_dir), \
                 mock.patch.object(phase3_stereo, "resolve_scoring_decoder", return_value=("decoder", None)), \
                 mock.patch.object(phase3_stereo, "get_aac_path", return_value=aac), \
                 mock.patch.object(phase3_stereo, "decode_audio", side_effect=[ref, ref]), \
                 mock.patch.object(phase3_stereo, "WorkerPool", InlinePool), \
                 mock.patch.object(phase3_stereo.concurrent.futures, "as_completed", side_effect=lambda fs: list(fs)), \
                 mock.patch.object(phase3_stereo, "pre_echo_metrics", return_value=metric([3.0])) as pre, \
                 mock.patch.object(phase3_stereo, "attack_centroid_deltas") as centroid, \
                 mock.patch.object(phase3_stereo, "coherence_error") as coherence, \
                 mock.patch("sys.argv", ["phase3_stereo.py", result_path, aac_dir, td,
                                          "--skip-stereo"]):
                phase3_stereo.main()

            with open(result_path) as f:
                saved = json.load(f)["matrix"]["enc_stereo_case_stereo.wav"]
            self.assertEqual(saved["pre_echo_db"], [3.0])
            self.assertEqual(saved["pre_echo_coverage"]["scored"], 1)
            self.assertEqual(saved["ic_err"], 0.125)
            self.assertEqual(saved["attack_centroid_ms"], [0.25])
            pre.assert_called_once()
            centroid.assert_not_called()
            coherence.assert_not_called()


if __name__ == "__main__":
    unittest.main()
