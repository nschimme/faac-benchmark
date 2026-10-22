"""Phase 3 must score each row against the reference from its own corpus.

The same clip name exists in the 48 kHz and 44.1 kHz corpora with different
content. The reference map used to be keyed by (filename, channels), so rows
from both corpora in one run shared whichever reference finished decoding last.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SCENARIOS
from utils import corpus_dir
from phase3_stereo import reference_key

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "same_name.wav"
ROW_48K = "48k_stereo_96k"
ROW_44K1 = "44k1_stereo_128k"
# A second rung per corpus: references are only pre-decoded into the shared map
# when several rows share one, as they do in any real ladder.
EXTRA = {ROW_48K: "48k_stereo_64k", ROW_44K1: "44k1_stereo_64k"}


def write_stereo(path, left, right, rate=48000):
    pcm = np.clip(np.stack([left, right], axis=1) * 20000, -32768, 32767).astype("<i2")
    with wave.open(path, "w") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm.tobytes())


class ReferenceKeyTest(unittest.TestCase):
    def test_key_separates_corpora(self):
        a = reference_key({"scenario": ROW_48K, "filename": NAME}, "/x")
        b = reference_key({"scenario": ROW_44K1, "filename": NAME}, "/x")
        self.assertNotEqual(a, b)
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[2], b[2])


class Phase3CorpusTest(unittest.TestCase):
    def test_each_row_uses_its_own_corpus_reference(self):
        rng = np.random.default_rng(7)
        n = 48000 * 3
        mono = rng.standard_normal(n) * 0.2
        with tempfile.TemporaryDirectory() as td:
            ext = os.path.join(td, "ext")
            aac_dir = os.path.join(td, "aac")
            os.makedirs(aac_dir)
            rows = {}
            # 48k corpus: strongly correlated channels. 44.1k corpus: independent
            # channels. (Identical channels would count as mono and score None.)
            contents = {ROW_48K: (mono, 0.9 * mono + 0.1 * rng.standard_normal(n) * 0.2),
                        ROW_44K1: (mono, rng.standard_normal(n) * 0.2)}
            for scen, (l, r) in contents.items():
                d = corpus_dir(SCENARIOS[scen], ext)
                os.makedirs(d, exist_ok=True)
                write_stereo(os.path.join(d, NAME), l, r)
                # The "stream" is the clip itself, so its own reference gives
                # zero coherence error and any other corpus's does not.
                for rung in (scen, EXTRA[scen]):
                    stream = f"{rung}.m4a"
                    write_stereo(os.path.join(aac_dir, stream), l, r)
                    rows[f"{rung}_{NAME}"] = {"scenario": rung, "filename": NAME, "aac": stream}
            results = os.path.join(td, "res.json")
            with open(results, "w") as f:
                json.dump({"matrix": rows}, f)
            subprocess.run([sys.executable, os.path.join(REPO, "phase3_stereo.py"),
                            results, aac_dir, ext, "--decoder", "ffmpeg", "--skip-transient"],
                           check=True, capture_output=True)
            with open(results) as f:
                out = json.load(f)["matrix"]
            for key, entry in out.items():
                self.assertIsNotNone(entry.get("ic_err"), key)
                self.assertLess(entry["ic_err"], 1e-3, f"{key} was scored against another corpus's reference")


if __name__ == "__main__":
    unittest.main()
