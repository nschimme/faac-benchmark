"""
 * FAAC Benchmark Suite - window_sequence histogram
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.

Counts the window_sequence of the first SCE/CPE in every ADTS frame, i.e. what
the bitstream actually carries rather than what the encoder's counters claim.
Handles the SCE global_gain and a CPE without common_window, which precede
ics_info. Verified on a pure sine (98% ONLY_LONG) and a click train (short
windows only at the clicks).

    python3 scripts/winseq.py out.aac [more.aac ...]
    python3 scripts/winseq.py --frames out.aac    # one line per frame
"""
import sys

NAMES = ["ONLY_LONG", "LONG_START", "EIGHT_SHORT", "LONG_STOP"]


def parse(path):
    d = open(path, 'rb').read()
    i = 0
    seq = []
    while i + 7 <= len(d):
        if d[i] != 0xFF or (d[i + 1] & 0xF6) != 0xF0:
            i += 1
            continue
        flen = ((d[i + 3] & 3) << 11) | (d[i + 4] << 3) | (d[i + 5] >> 5)
        hl = 7 if (d[i + 1] & 1) else 9
        bits = int.from_bytes(d[i + hl:i + hl + 8], 'big')
        pos = [0]

        def get(n):
            v = (bits >> (64 - pos[0] - n)) & ((1 << n) - 1)
            pos[0] += n
            return v

        eid = get(3)
        get(4)                           # element_instance_tag
        if eid == 0:                     # SCE: global_gain, then ics_info
            get(8)
            get(1)
            seq.append(get(2))
        elif eid == 1:                   # CPE
            if not get(1):               # no common_window: L's global_gain first
                get(8)
            get(1)
            seq.append(get(2))
        i += flen
    return seq


def main(argv):
    per_frame = "--frames" in argv
    for p in [a for a in argv if not a.startswith("--")]:
        s = parse(p)
        n = len(s) or 1
        if per_frame:
            for k, ws in enumerate(s):
                print(k, NAMES[ws])
        print(p.split('/')[-1], "frames", len(s),
              " ".join("%s %.1f%%" % (NAMES[k], 100 * s.count(k) / n) for k in range(4)))


if __name__ == "__main__":
    main(sys.argv[1:])
