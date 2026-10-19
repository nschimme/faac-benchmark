"""Find clips in the FLAC listening-test sets that duplicate PMLT2014/SoundExpert.

Names and cuts differ between sets (the corpus clips are 48 kHz, 10 s centre
windows; these are full-length 44.1 kHz FLACs), so duplicates are found by
content: a 3 s excerpt of the shorter file is cross-correlated against the
longer at 8 kHz mono (gain- and resampler-insensitive). On the current sets
true duplicates score 0.89-1.00 and every other pair stays below 0.25.

Candidates are the raw dataset sources, not data/external/audio, so the result
doesn't depend on what happens to be built locally. Writes
duplicate_clips.json at the repo root, which setup_datasets.py reads; an
existing entry's "action" is preserved so a hand-set "skip" survives re-runs.
Re-run it when bumping a dataset tag, then commit the file.

    python3 scripts/find_duplicate_clips.py
"""
import json
import os
import subprocess
import sys

import numpy as np
from scipy.signal import fftconvolve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import setup_datasets as sd  # noqa: E402

RATE = 8000
THRESHOLD = 0.8
FLAC_SETS = {"IgorCPBLT2020": "IgorCPBLT2020-IgorCPBLT2020",
             "Kamedo2PLT2012": "Kamedo2PLT2012-Kamedo2PLT2012"}


def load(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1",
                          "-ar", str(RATE), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def peak_ncc(a, b):
    """Peak normalized correlation of a 3 s excerpt of the shorter inside the longer."""
    if len(a) > len(b):
        a, b = b, a
    n = min(len(a), 3 * RATE)
    start = (len(a) - n) // 2
    t = a[start:start + n] - a[start:start + n].mean()
    tn = np.sqrt((t * t).sum())
    if tn < 1e-6 or len(b) < n:
        return 0.0
    num = fftconvolve(b, t[::-1], mode="valid")
    csum = np.concatenate([[0], np.cumsum(b)])
    csq = np.concatenate([[0], np.cumsum(b * b)])
    sm = csum[n:] - csum[:-n]
    sq = csq[n:] - csq[:-n]
    den = np.sqrt(np.maximum(sq - sm * sm / n, 1e-12)) * tn
    return float(np.max(num / den))


def main():
    for name in ["PMLT2014", "SoundExpert", *FLAC_SETS]:
        sd.download_and_extract(name, sd.DATASETS[name]["url"])

    # Output filename -> source, exactly as setup_datasets would name them.
    refs = {}
    for f in sd.pmlt_wav_files(os.path.join(sd.TEMP_DIR, "PMLT2014-PMLT2014")):
        refs[os.path.basename(f)] = f
    for f in sd.soundexpert_wav_files(os.path.join(sd.TEMP_DIR, "SoundExpert-SoundExpert")):
        refs[os.path.basename(f)] = f
    ref_sig = {n: load(p) for n, p in refs.items()}

    old = {}
    if os.path.exists(sd.DUPLICATES_FILE):
        old = sd.load_duplicates()

    found = {}
    seen = {}   # FLAC key -> signal, for duplicates between the new sets
    for zip_name, sub in FLAC_SETS.items():
        src = os.path.join(sd.TEMP_DIR, sub)
        for fn in sorted(f for f in os.listdir(src) if f.endswith(".flac")):
            key = f"{zip_name}/{fn}"
            sig = load(os.path.join(src, fn))
            best = max(((peak_ncc(sig, s), n) for n, s in ref_sig.items()))
            action = "replace"
            if best[0] < THRESHOLD:
                best = max(((peak_ncc(sig, s), k) for k, s in seen.items()),
                           default=(0.0, None))
                action = "skip"   # the earlier new-set copy is kept
            if best[0] >= THRESHOLD:
                action = old.get(key, {}).get("action", action)
                found[key] = {"matches": best[1], "score": round(best[0], 3),
                              "action": action}
                print(f"{key} == {best[1]} ({best[0]:.3f}, {action})")
            seen[key] = sig

    with open(sd.DUPLICATES_FILE, "w") as f:
        json.dump({"threshold": THRESHOLD, "duplicates": found}, f, indent=2,
                  sort_keys=True)
        f.write("\n")
    print(f"Wrote {len(found)} duplicate(s) to {sd.DUPLICATES_FILE}")


if __name__ == "__main__":
    main()
