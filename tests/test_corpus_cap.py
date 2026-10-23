"""A capped music corpus must keep the clips the metrics depend on."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CORPORA, MUSIC_KEEP
from utils import select_corpus_clips


class CorpusCapTest(unittest.TestCase):
    FILES = [f"clip_{i:02d}.wav" for i in range(60)] + MUSIC_KEEP

    def test_keep_list_survives_the_cap(self):
        corpus = {"max_clips": 40, "keep": MUSIC_KEEP[:10]}
        picked = select_corpus_clips(self.FILES, corpus)
        self.assertEqual(len(picked), 40)
        self.assertTrue(set(MUSIC_KEEP[:10]) <= set(picked))

    def test_selection_is_deterministic_and_sorted(self):
        corpus = {"max_clips": 40, "keep": MUSIC_KEEP[:10]}
        a = select_corpus_clips(self.FILES, corpus)
        b = select_corpus_clips(list(reversed(self.FILES)), corpus)
        self.assertEqual(a, b)
        self.assertEqual(a, sorted(a))

    def test_keep_names_missing_from_the_corpus_are_ignored(self):
        picked = select_corpus_clips([f"c{i}.wav" for i in range(30)], {"max_clips": 10, "keep": ["absent.wav"]})
        self.assertEqual(len(picked), 10)

    def test_a_corpus_without_keep_behaves_as_before(self):
        files = [f"c{i:02d}.wav" for i in range(20)]
        picked = select_corpus_clips(files, {"max_clips": 5})
        step = 20 / 5
        self.assertEqual(picked, [files[int(i * step)] for i in range(5)])

    def test_capped_music_corpora_keep_the_transient_clips(self):
        for name in ("audio_32k", "audio_44k1"):
            self.assertEqual(CORPORA[name]["max_clips"], 48)
            self.assertIs(CORPORA[name]["keep"], MUSIC_KEEP)
        self.assertLess(len(MUSIC_KEEP), 48)


if __name__ == "__main__":
    unittest.main()
