"""
 * FAAC Benchmark Suite
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""

# Regenerates config.py's per-scenario `vbr_q`: the faac -q value whose VBR
# output lands closest to that scenario's "bitrate" for representative
# content. A hand-picked linear guess (q ~= bitrate * 1.25) was tried before
# this script existed and undershot by 18-67%, worse at higher bitrates,
# because faac's -q-to-bitrate curve isn't linear -- see docs/metrics.md for
# the full writeup, including the 42-70 kbps dead zone this search cannot
# close (libfaac's HE-AAC/LC-AAC AUTO switch at quantqual=75 leaves a gap
# between HE-AAC's ceiling and LC-AAC's floor on 48kHz stereo content).
#
# Usage (from repo root): python3 scripts/calibrate_vbr_q.py [--scenarios NAME,...]
# Prints a table and a ready-to-paste vbr_q dict; does not edit config.py.

import argparse
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shutil
from config import SCENARIOS, GATE_CLIPS, GATE_FALLBACK_N
from utils import is_faac_legacy, corpus_dir, expand_scenario_list

DATA_DIR = "data/external"


def get_dur(path, cache={}):
    if path in cache:
        return cache[path]
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True).stdout.strip()
    d = float(out)
    cache[path] = d
    return d


def avg_kbps_for_q(q, samples, tmp):
    total_bits, total_dur = 0, 0
    faac_bin = shutil.which("faac") or "faac"
    cmd = [faac_bin]
    if is_faac_legacy(faac_bin):
        cmd.append("-w")
    cmd.extend(["-q", str(q), "-o", tmp, "-X", "--overwrite"])
    for s in samples:
        full_cmd = list(cmd)
        full_cmd.insert(-3, s)
        subprocess.run(full_cmd, capture_output=True)
        total_bits += os.path.getsize(tmp) * 8
        total_dur += get_dur(s)
    return (total_bits / 1000) / total_dur


def gate_samples(name, cfg):
    """The scenario's gate clips, or a deterministic slice when it has none.

    Corpora added with the rate rework (the clean-speech ones) have no curated
    gate list, so falling back to a slice keeps them calibratable.
    """
    data_dir = corpus_dir(cfg, DATA_DIR)
    clips = GATE_CLIPS.get(name, [])
    samples = [os.path.join(data_dir, c) for c in clips]
    samples = [s for s in samples if os.path.exists(s)]
    if samples or not os.path.isdir(data_dir):
        return samples
    avail = sorted(f for f in os.listdir(data_dir) if f.endswith(".wav"))
    n = min(GATE_FALLBACK_N, len(avail))
    if n <= 0:
        return []
    step = len(avail) / n
    return [os.path.join(data_dir, avail[int(i * step)]) for i in range(n)]


def calibrate(name, cfg, tmp):
    samples = gate_samples(name, cfg)
    target = cfg["bitrate"]

    # Coarse grid, then refine around the best candidate. A binary search
    # would mis-track faac's real q-to-bitrate curve, which jumps sharply
    # at the HE-AAC/LC-AAC AUTO threshold (quantqual=75) instead of rising
    # smoothly.
    best_q, best_err = None, float("inf")
    for q in range(10, 1001, 5):
        err = abs(avg_kbps_for_q(q, samples, tmp) - target)
        if err < best_err:
            best_err, best_q = err, q

    for q in range(max(10, best_q - 5), best_q + 6):
        err = abs(avg_kbps_for_q(q, samples, tmp) - target)
        if err < best_err:
            best_err, best_q = err, q

    return best_q, avg_kbps_for_q(best_q, samples, tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", help="Comma-separated scenario names (default: all)")
    args = parser.parse_args()

    names = expand_scenario_list(args.scenarios) if args.scenarios else list(SCENARIOS.keys())
    tmp = "/tmp/_calibrate_vbr_q.m4a"

    print(f"{'scenario':<20}{'target':>8}{'chosen_q':>10}{'actual':>10}{'err%':>8}")
    results = {}
    for name in names:
        if name not in SCENARIOS:
            print(f"Unknown scenario: {name}", file=sys.stderr)
            continue
        cfg = SCENARIOS[name]
        q, kbps = calibrate(name, cfg, tmp)
        results[name] = q
        target = cfg["bitrate"]
        print(f"{name:<20}{target:>8}{q:>10}{kbps:>10.1f}{100*(kbps-target)/target:>7.1f}%")

    print("\nvbr_q values:")
    for name, q in results.items():
        print(f'  "{name}": {q},')


if __name__ == "__main__":
    main()
