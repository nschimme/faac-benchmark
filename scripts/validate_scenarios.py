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

# Checks that every scenario asks for a bitrate its content can actually
# carry. A target is only legitimate if ABR lands near it: the retired
# 16k_mono_40k scenario asked 16 kHz mono for 40 kbps and got 32.2 -- a
# permanent, uncloseable bitrate-accuracy deficit reported in every run that no
# code change could ever fix. Run this before adding or retuning a scenario.
#
# Usage (from repo root):
#   python3 scripts/validate_scenarios.py [--scenarios NAME|FAMILY,...]
#                                         [--rate-control abr|vbr] [--tolerance 15]
# Exits non-zero if any scenario is outside tolerance, so it can gate CI.

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SCENARIOS, GATE_CLIPS, GATE_FALLBACK_N
from utils import (is_faac_legacy, corpus_dir, expand_scenario_list,
                   get_scenario_sort_key, format_scenario_rate)

DATA_DIR = "data/external"

# The one documented, expected exception: for 48 kHz stereo, libfaac's AUTO
# object-type resolution in VBR mode picks HE-AAC at quantqual <= 75 and forces
# LC-AAC above it, leaving no -q value in the 42-70 kbps gap. See config.py.
VBR_DEAD_ZONE = {"48k_stereo_48k", "48k_stereo_56k"}


def get_dur(path, cache={}):
    if path not in cache:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True).stdout.strip()
        cache[path] = float(out) if out else 0.0
    return cache[path]


def gate_samples(name, cfg):
    """The scenario's gate clips, or a deterministic slice when it has none."""
    data_dir = corpus_dir(cfg, DATA_DIR)
    samples = [os.path.join(data_dir, c) for c in GATE_CLIPS.get(name, [])]
    samples = [s for s in samples if os.path.exists(s)]
    if samples or not os.path.isdir(data_dir):
        return samples
    avail = sorted(f for f in os.listdir(data_dir) if f.endswith(".wav"))
    n = min(GATE_FALLBACK_N, len(avail))
    if n <= 0:
        return []
    step = len(avail) / n
    return [os.path.join(data_dir, avail[int(i * step)]) for i in range(n)]


def measure(faac_bin, cfg, samples, rate_control, tmp):
    """Average achieved kbps over the sample set, weighted by duration."""
    total_bits, total_dur = 0, 0.0
    for src in samples:
        cmd = [faac_bin]
        if is_faac_legacy(faac_bin):
            cmd.append("-w")
        if rate_control == "vbr":
            cmd.extend(["-q", str(cfg["vbr_q"])])
        else:
            cmd.extend(["-b", str(cfg["bitrate"])])
        cmd.extend([src, "-o", tmp, "--overwrite"])
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode != 0 or not os.path.exists(tmp):
            continue
        dur = get_dur(src)
        if dur <= 0:
            continue
        total_bits += os.path.getsize(tmp) * 8
        total_dur += dur
    if total_dur <= 0:
        return None
    return (total_bits / 1000) / total_dur


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", help="Comma-separated scenario or family names")
    parser.add_argument("--rate-control", choices=["abr", "vbr"], default="abr")
    parser.add_argument("--tolerance", type=float, default=15.0,
                        help="Max allowed deviation from target, in percent (default 15)")
    parser.add_argument("--faac-bin", default=shutil.which("faac") or "faac")
    args = parser.parse_args()

    names = expand_scenario_list(args.scenarios) if args.scenarios else list(SCENARIOS)
    names = sorted((n for n in names if n in SCENARIOS), key=get_scenario_sort_key)
    tmp = "/tmp/_validate_scenarios.m4a"

    # The banner only appears on an actual encode, not on --help.
    ver = subprocess.run([args.faac_bin, "-b", "128", os.devnull, "-o", "/dev/null"],
                         capture_output=True, text=True)
    ver_line = next((l for l in (ver.stdout + ver.stderr).splitlines()
                     if l.strip().startswith("FAAC ")), "")
    print(f"faac binary: {args.faac_bin}  {ver_line.strip()}")
    if args.rate_control == "abr":
        # A build without libfaac's AUTO object-type resolution stays on LC at
        # every rate, so the HE-AAC-targeted stereo scenarios (24-56 kbps) will
        # read as large overshoots. That is a property of the binary, not of
        # the scenario -- check the build before retargeting anything there.
        print("Note: low-rate stereo targets assume HE-AAC auto-selection; a stock "
              "LC-only faac will overshoot them.\n")
    print(f"{'scenario':<20}{'rate':>10}{'target':>8}{'actual':>9}{'err%':>8}  status")
    failures = []
    for name in names:
        cfg = SCENARIOS[name]
        samples = gate_samples(name, cfg)
        if not samples:
            print(f"{name:<20}{format_scenario_rate(cfg):>10}"
                  f"{cfg['bitrate']:>8}{'--':>9}{'--':>8}  NO CLIPS (corpus not built?)")
            failures.append(name)
            continue

        kbps = measure(args.faac_bin, cfg, samples, args.rate_control, tmp)
        if kbps is None:
            print(f"{name:<20}{format_scenario_rate(cfg):>10}"
                  f"{cfg['bitrate']:>8}{'--':>9}{'--':>8}  ENCODE FAILED")
            failures.append(name)
            continue

        target = cfg["bitrate"]
        err = 100.0 * (kbps - target) / target
        exempt = args.rate_control == "vbr" and name in VBR_DEAD_ZONE
        ok = abs(err) <= args.tolerance or exempt
        status = "ok" if abs(err) <= args.tolerance else (
            "EXPECTED (documented dead zone)" if exempt else "OUT OF RANGE")
        if not ok:
            failures.append(name)
        print(f"{name:<20}{format_scenario_rate(cfg):>10}"
              f"{target:>8}{kbps:>9.1f}{err:>7.1f}%  {status}")

    if os.path.exists(tmp):
        os.remove(tmp)

    if failures:
        print(f"\n{len(failures)} scenario(s) outside ±{args.tolerance:g}%: "
              f"{', '.join(failures)}")
        print("Either retarget the scenario's bitrate to something the format can "
              "carry, or drop it -- a target the content cannot reach shows up as a "
              "permanent accuracy deficit in every report.")
        return 1
    print(f"\nAll {len(names)} scenario(s) within ±{args.tolerance:g}% of target.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
