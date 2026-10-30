#!/usr/bin/env python3
"""Independent MPEG-4 dynamic_range_info parser for ADTS and MP4/M4A.

Emits one JSON object per access unit with the DRC payload found in it. This is
deliberately independent of libfdk-aac and libfaad so it can validate both
encoders. It scans for fill-element candidates rather than decoding Huffman
spectral data; a candidate is accepted only when dynamic_range_info parses
cleanly inside the declared fill-element payload.

usage:
  drc_meta.py in.aac [--json out.jsonl]
  drc_meta.py in.m4a [--json out.jsonl]     # demuxed with ffmpeg
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

EXT_DYNAMIC_RANGE = 0xB
ID_FIL = 6


class BitReader:
    def __init__(self, data, bit=0, end=None):
        self.data = data
        self.bit = bit
        self.end = len(data) * 8 if end is None else end

    def left(self):
        return self.end - self.bit

    def get(self, n):
        if n < 0 or self.bit + n > self.end:
            raise EOFError
        value = 0
        for _ in range(n):
            value = (value << 1) | ((self.data[self.bit >> 3] >> (7 - (self.bit & 7))) & 1)
            self.bit += 1
        return value

    def skip(self, n):
        if n < 0 or self.bit + n > self.end:
            raise EOFError
        self.bit += n


def parse_dynamic_range(reader):
    """Parse dynamic_range_info() starting after extension_type."""
    pce_present = reader.get(1)
    pce_tag = reader.get(4) if pce_present else None
    if pce_present:
        reader.skip(4)
    excluded = []
    if reader.get(1):
        channel = 0
        while True:
            for _ in range(7):
                if reader.get(1):
                    excluded.append(channel)
                channel += 1
            if not reader.get(1):
                break
    bands = 1
    tops = []
    if reader.get(1):
        bands += reader.get(4)
        reader.skip(4)
        for _ in range(bands):
            tops.append(reader.get(8))
    ref = None
    if reader.get(1):
        ref = reader.get(7)
        reader.skip(1)
    gains = []
    for _ in range(bands):
        sign = reader.get(1)
        control = reader.get(7)
        gains.append((sign, control))
    return {
        "pce_tag": pce_tag,
        "excluded": excluded,
        "bands": bands,
        "tops": tops if tops else [255],
        "prog_ref_code": ref,
        "prog_ref_db": None if ref is None else -0.25 * ref,
        "gains": [
            {
                "sgn": sgn,
                "ctl": ctl,
                "db_025": (-1.0 if sgn else 1.0) * 0.25 * ctl,
                "db_exact": (-1.0 if sgn else 1.0) * (20.0 * 0.3010299956639812) * ctl / 24.0,
            }
            for sgn, ctl in gains
        ],
    }


def scan_frame(data, header_bytes):
    """Return all plausible FIL/dynamic_range_info candidates in one ADTS body."""
    bits = BitReader(data, header_bytes * 8)
    candidates = []
    total = len(data) * 8
    for start in range(header_bytes * 8, total - 3):
        reader = BitReader(data, start, total)
        try:
            if reader.get(3) != ID_FIL:
                continue
            count = reader.get(4)
            if count == 15:
                count += reader.get(8) - 1
            if count <= 0 or count > 270:
                continue
            payload_start = reader.bit
            payload_end = payload_start + count * 8
            if payload_end > total:
                continue
            if reader.get(4) != EXT_DYNAMIC_RANGE:
                continue
            payload = parse_dynamic_range(reader)
            # Accept only a parse that ends inside the payload and leaves at
            # most 7 alignment bits. This rejects random Huffman-data hits.
            used = reader.bit
            if used > payload_end or payload_end - used > 7:
                continue
            payload["fill_count"] = count
            payload["bit_offset"] = start - header_bytes * 8
            payload["fill_end"] = payload_end - header_bytes * 8
            payload["pad_bits"] = payload_end - used
            candidates.append(payload)
        except EOFError:
            continue
    return candidates


def adts_frame_len(data, pos):
    if pos + 7 > len(data) or data[pos] != 0xFF or (data[pos + 1] & 0xF6) != 0xF0:
        raise ValueError("not an ADTS sync word at %d" % pos)
    protection_absent = data[pos + 1] & 1
    header = 7 if protection_absent else 9
    length = ((data[pos + 3] & 0x03) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
    if length < header or pos + length > len(data):
        raise ValueError("bad ADTS frame length at %d: %d" % (pos, length))
    return length, header


def read_adts(path):
    data = Path(path).read_bytes()
    frames = []
    pos = 0
    while pos < len(data):
        length, header = adts_frame_len(data, pos)
        frames.append((data[pos:pos + length], header))
        pos += length
    return frames


def read_mp4(path):
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "demux.aac"
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:a:0",
             "-c", "copy", "-f", "adts", str(out)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit("ffmpeg demux failed: " + result.stderr.strip())
        return read_adts(out)


def pick_candidate(candidates, body_bits, expected_ref=None):
    """Choose the encoder's real FIL from scan hits.

    FAAC writes the FIL as the first syntax element; FDK writes it as the last
    element before ID_END. Real payloads at either edge win over random
    Huffman-data matches in the middle.
    """
    if not candidates:
        return None
    # FAAC and FDK both write a programme reference with their line-mode
    # payload. Requiring it rejects PCE and Huffman-data lookalikes on
    # 8-channel streams, where the real FIL is not at an edge of the block.
    # No line-mode profile cuts more than 24 dB or boosts more than 15 dB, so
    # the metadata grid bounds reject the remaining partial-fill lookalikes.
    def plausible(c):
        gain = c["gains"][0]
        return gain["ctl"] <= (96 if gain["sgn"] else 60)

    def ref_ok(c):
        return (expected_ref is None or
                (c["prog_ref_code"] is not None and
                 abs(c["prog_ref_db"] - expected_ref) <= 0.13))

    expected = [c for c in candidates if ref_ok(c) and plausible(c)]
    with_ref = [c for c in candidates if c["prog_ref_code"] is not None and plausible(c)]
    pool = expected or with_ref or [c for c in candidates if plausible(c)] or candidates
    if expected:
        # Both encoders put the real payload at the last matching fill element;
        # a false hit can precede it (FDK's core DRC is followed by SBR fills).
        return max(expected, key=lambda c: c["bit_offset"])
    at_start = [c for c in pool if c["bit_offset"] == 0]
    if at_start:
        return at_start[0]
    near_end = [c for c in pool if body_bits - c["fill_end"] <= 10]
    if near_end:
        return max(near_end, key=lambda c: c["fill_end"])
    return max(pool, key=lambda c: c["bit_offset"])


def parse(path, expected_ref=None):
    path = Path(path)
    if path.suffix.lower() in (".m4a", ".mp4", ".m4b"):
        frames = read_mp4(path)
    else:
        frames = read_adts(path)
    rows = []
    for index, (frame, header) in enumerate(frames):
        candidates = scan_frame(frame, header)
        body_bits = (len(frame) - header) * 8
        rows.append({
            "frame": index,
            "adts_bytes": len(frame),
            "candidates": candidates,
            "drc": pick_candidate(candidates, body_bits, expected_ref),
            "candidate_count": len(candidates),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--json")
    ap.add_argument("--ref", type=float, default=None)
    args = ap.parse_args()
    rows = parse(args.input, args.ref)
    if args.json:
        Path(args.json).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    else:
        for row in rows:
            print(json.dumps(row))
    drc = [r["drc"] for r in rows if r["drc"]]
    multi = [r for r in rows if r["candidate_count"] > 1]
    print(f"frames={len(rows)} with_drc={len(drc)} multi_candidate={len(multi)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
