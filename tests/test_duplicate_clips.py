import json
import os
import unittest

import numpy as np

import setup_datasets as sd
from scripts.find_duplicate_clips import peak_ncc


class DuplicateReferenceTest(unittest.TestCase):
    def test_entries_are_well_formed(self):
        dupes = sd.load_duplicates()
        self.assertTrue(dupes)
        for key, e in dupes.items():
            self.assertIn(key.split("/")[0], sd.DATASETS)
            self.assertIn(e["action"], ("replace", "skip"))
            self.assertTrue(e["matches"])

    def test_matches_are_unique_replacements(self):
        names = [e["matches"] for e in sd.load_duplicates().values()
                 if e["action"] == "replace"]
        self.assertEqual(len(names), len(set(names)))

    def _fake_zips(self, td):
        for d, files in {"PMLT2014-PMLT2014": ["velvet.16b48k.wav", "sandman.16b48k.wav"],
                         "SoundExpert-SoundExpert": ["cst.wav"],
                         "IgorCPBLT2020-IgorCPBLT2020": ["08 velvet.flac", "03 eig.flac", "01 castanets.flac"],
                         "Kamedo2PLT2012-Kamedo2PLT2012": []}.items():
            os.makedirs(os.path.join(td, d))
            for f in files:
                open(os.path.join(td, d, f), "w").close()

    def test_music_sources_superseded_clip_is_replaced_by_its_new_name(self):
        import tempfile
        from unittest import mock
        dupes = {"IgorCPBLT2020/08 velvet.flac": {"matches": "velvet.16b48k.wav", "action": "replace"},
                 "IgorCPBLT2020/01 castanets.flac": {"matches": "cst.wav", "action": "skip"}}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(sd, "TEMP_DIR", td), mock.patch.object(sd, "load_duplicates", return_value=dupes):
            self._fake_zips(td)
            names = sorted(n for n, _ in sd.music_sources())
        # velvet appears once, under the Igor name; the skipped castanets FLAC is
        # left out and the older cst.wav kept
        self.assertEqual(names, ["03 eig.wav", "08 velvet.wav", "cst.wav", "sandman.16b48k.wav"])

    def test_rebuild_removes_superseded_clips_but_not_skipped_ones(self):
        import tempfile
        from unittest import mock
        dupes = {"IgorCPBLT2020/08 velvet.flac": {"matches": "velvet.16b48k.wav", "action": "replace"},
                 "IgorCPBLT2020/01 castanets.flac": {"matches": "cst.wav", "action": "skip"}}
        with tempfile.TemporaryDirectory() as td, mock.patch.object(sd, "load_duplicates", return_value=dupes):
            for n in ("velvet.16b48k.wav", "cst.wav", "keep.wav"):
                open(os.path.join(td, n), "w").close()
            sd._remove_superseded(td)
            self.assertEqual(sorted(os.listdir(td)), ["cst.wav", "keep.wav"])

    def test_derived_corpora_resample_the_original_once(self):
        # native 44.1 kHz and 48 kHz sources go straight to the target rate
        self.assertTrue(sd.derives_from_original(44100, 44100))
        self.assertTrue(sd.derives_from_original(48000, 44100))
        self.assertTrue(sd.derives_from_original(44100, 32000))
        # a source below the target would have to be upsampled: fall back to
        # the 48 kHz clip rather than inventing a new upsample
        self.assertFalse(sd.derives_from_original(32000, 44100))
        self.assertFalse(sd.derives_from_original(0, 44100))

    def test_peak_ncc_finds_excerpt_and_rejects_noise(self):
        rng = np.random.default_rng(0)
        a = rng.standard_normal(8000 * 10)
        self.assertGreater(peak_ncc(a[16000:56000] * 0.5, a), 0.99)
        self.assertLess(peak_ncc(rng.standard_normal(8000 * 5), a), 0.3)


if __name__ == "__main__":
    unittest.main()
