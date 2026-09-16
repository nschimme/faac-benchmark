"""
 * FAAC Benchmark Suite - Codec Comparison & Leaderboard (Primary Entrypoint)
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import sys
import argparse

import compare_encoders
import compare_decoders

def main():
    parser = argparse.ArgumentParser(description="FAAC Benchmark Suite - Audio Codec Comparison & Leaderboard")
    parser.add_argument("--mode", choices=["encoder", "decoder", "both"], default="both", help="Benchmarking mode: encoder, decoder, or both")
    parser.add_argument("--faac-bin", action="append", help="Path to faac binary")
    parser.add_argument("--faac-lib", action="append", help="Path to libfaac.so")
    parser.add_argument("--faac-bin-version", action="append", help="Explicit version label for --faac-bin")
    parser.add_argument("--fdkaac-bin", action="append", help="Path to fdkaac binary")
    parser.add_argument("--aac-enc-bin", action="append", help="Path to aac-enc binary")
    parser.add_argument("--falabaac-bin", action="append", help="Path to falabaac binary")
    parser.add_argument("--faad-bin", action="append", help="Path to faad binary")
    parser.add_argument("--faad-lib", action="append", help="Path to libfaad.so")
    parser.add_argument("--faad-bin-version", action="append", help="Explicit version label for --faad-bin")
    parser.add_argument("--ffmpeg-bin", action="append", help="Path to ffmpeg binary")
    parser.add_argument("--afconvert-bin", action="append", help="Path to afconvert binary")
    parser.add_argument("--opusenc-bin", action="append", help="Path to opusenc binary")
    parser.add_argument("--lame-bin", action="append", help="Path to lame binary")
    parser.add_argument("--include-other-codecs", action="store_true", help="Include non-AAC codecs (Opus, LAME)")
    parser.add_argument("--output", default="leaderboard.md", help="Output Markdown file")
    parser.add_argument("--results-json", default="comparison_results.json", help="Intermediate results JSON")
    parser.add_argument("--scenarios", help="Comma-separated list of scenarios to run")
    parser.add_argument("--gate", action="store_true", help="Use the fast fixed gate subset")
    parser.add_argument("--coverage", type=int, default=100, help="Coverage percentage (1-100)")
    parser.add_argument("--skip-mos", action="store_true", help="Skip MOS calculation")
    parser.add_argument("--skip-stereo", action="store_true", help="Skip stereo coherence calculation")
    parser.add_argument("--skip-transient", action="store_true", help="Skip transient fidelity calculation")
    parser.add_argument("--skip-graphs", action="store_true", help="Skip generating Mermaid graph blocks")
    parser.add_argument("--resume", action="store_true", help="Reload --results-json from previous run")

    args, unknown = parser.parse_known_args()

    if args.mode == "encoder":
        compare_encoders.main()
    elif args.mode == "decoder":
        compare_decoders.main()
    else:
        # mode == "both": Run compare_encoders in both mode
        compare_encoders.main()

if __name__ == "__main__":
    main()
