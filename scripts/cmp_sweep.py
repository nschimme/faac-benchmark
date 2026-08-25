#!/usr/bin/env python3
"""Compare sweep-output JSONs against a baseline value: per-clip and
per-scenario MOS delta, bitrate delta, and changed-md5 count.

Consolidates cmp_lever.py (MOS delta + changed-md5 count) and cmp_psy.py
(adds per-scenario bitrate delta) into one tool.

Usage (from repo root):
  python3 scripts/cmp_sweep.py PREFIX KEY V0,V1,V2,...

Loads `{PREFIX}_{KEY}{V}.json` for each V (produced by, e.g., repeated
`run_benchmark.py --sweep "KEY=V"` runs, or score_preecho.py --env-ab /
sweep_binary_ab.py --env-var dumping one JSON per value), and diffs every
non-baseline value V1... against the first value V0.
"""
import json
import sys
from collections import defaultdict


def load(prefix, key, v):
    return json.load(open(f'{prefix}_{key}{v}.json'))['matrix']


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    prefix, key, vals = sys.argv[1], sys.argv[2], sys.argv[3].split(',')

    base = load(prefix, key, vals[0])
    print(f'=== {key}: baseline={vals[0]} ===')
    for v in vals[1:]:
        cand = load(prefix, key, v)
        sc = defaultdict(list)
        changed = 0
        for k in base:
            b, c = base[k], cand[k]
            if b.get('mos') is None or c.get('mos') is None:
                continue
            if b['md5'] != c['md5']:
                changed += 1
            sc[b['scenario']].append((k, c['mos'] - b['mos'], b['mos'], c['mos'],
                                      b.get('bitrate', 0), c.get('bitrate', 0)))
        allrows = [r for a in sc.values() for r in a]
        net = sum(r[1] for r in allrows) / len(allrows)
        dbr = sum(r[5] - r[4] for r in allrows) / len(allrows)
        print(f'\n-- {v} vs {vals[0]}: netMOS={net:+.4f}  dBR={dbr:+.2f}kbps  '
              f'changed_md5={changed}/{len(allrows)}')
        for s in sorted(sc):
            arr = sc[s]
            avg = sum(x[1] for x in arr) / len(arr)
            br = sum(x[5] - x[4] for x in arr) / len(arr)
            worst = min(arr, key=lambda x: x[1])
            print(f"   {s:10s} avgMOS={avg:+.4f} dBR={br:+.2f}  "
                  f"worst {worst[0].replace(s + '_', ''):26s} "
                  f"{worst[2]:.3f}->{worst[3]:.3f} {worst[1]:+.4f}")


if __name__ == '__main__':
    main()
