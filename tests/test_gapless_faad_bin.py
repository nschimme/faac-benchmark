"""The gapless check must decode with the FAAD3 the run scores with."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gapless import faad_bin_args

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FaadBinArgsTest(unittest.TestCase):
    def test_faad_scoring_decoder_is_passed_on(self):
        self.assertEqual(faad_bin_args("faad", "/ci/faad"), ["--faad-bin", "/ci/faad"])
        self.assertEqual(faad_bin_args("FAAD3", "/ci/faad"), ["--faad-bin", "/ci/faad"])

    def test_other_decoders_are_not(self):
        # The check needs FAAD3; another decoder's binary would be the wrong tool.
        self.assertEqual(faad_bin_args("ffmpeg", "/usr/bin/ffmpeg"), [])
        self.assertEqual(faad_bin_args("faad", None), [])

    def test_the_flag_reaches_the_check(self):
        with tempfile.TemporaryDirectory() as td:
            results = os.path.join(td, "res.json")
            with open(results, "w") as f:
                f.write('{"matrix": {}}')
            missing = os.path.join(td, "no-such-faad")
            proc = subprocess.run([sys.executable, os.path.join(REPO, "gapless.py"), results, td, td,
                                   "--faad-bin", missing], capture_output=True, text=True)
            # An empty matrix needs no decoder, so the run must not fall back to
            # (and fail on) the sibling build.
            self.assertNotIn("FAAD3 is unavailable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
