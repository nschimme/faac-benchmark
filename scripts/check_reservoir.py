#!/usr/bin/env python3
"""Leaky-bucket check of an ADTS stream's declared bit reservoir, or the
bufferSizeDB of an MP4.  usage: check_reservoir.py FILE --bitrate KBPS_TOTAL
Simulates: fill += mean - payload per frame, mean = ch*br*1024/fs (integer),
capacity 6144*ch - mean, opening at half. Reports mismatches between declared
buffer_fullness (11 bits, /32) and the simulation, any frame exceeding
mean+fill (decoder underflow), and any above 6144*ch."""
import sys, struct, argparse
SR = [96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350]

def adts(path, br_total, start=0.5):
    d = open(path,'rb').read(); i = 0; n = 0; bad = 0; under = 0; over = 0; ovf = 0; sum_p = 0
    fill = None; first = True
    while i + 7 <= len(d):
        if d[i] != 0xFF or (d[i+1] & 0xF6) != 0xF0: sys.exit(f"lost sync at {i}")
        fs = SR[(d[i+2] >> 2) & 0xF]; ch = ((d[i+2] & 1) << 2) | (d[i+3] >> 6)
        flen = ((d[i+3] & 3) << 11) | (d[i+4] << 3) | (d[i+5] >> 5)
        full = ((d[i+5] & 0x1F) << 6) | (d[i+6] >> 2)
        payload = (flen - 7) * 8
        if first:
            mean = ch * (br_total * 1000 // ch) * 1024 // fs
            cap = max(0, 6144 * ch - mean); fill = int(cap * start); first = False
        avail = mean + fill
        if payload > avail: under += 1
        if payload > 6144 * ch: over += 1
        if fill + mean - payload > cap: ovf += 1
        fill = min(cap, max(0, fill + mean - payload))
        if full == 0x7FF or full != fill >> 5:
            bad += 1
            if bad <= 3: print(f"  frame {n}: declared {full} sim {fill>>5} (payload {payload}, mean {mean}, cap {cap})")
        i += flen; n += 1; sum_p += payload
    print(f"{path}: {n} frames fs={fs} ch={ch} mean={mean} cap={cap} | declared!=sim: {bad} | frame>mean+fill: {under} | frame>6144*ch: {over} | overflow(clamped): {ovf} | avg payload {sum_p/n:.0f}/{mean}")
    return bad == 0 and under == 0 and over == 0

def mp4(path):
    d = open(path,'rb').read(); i = d.find(b'esds')
    if i < 0: sys.exit("no esds")
    p = i + 4 + 4            # atom name + version/flags
    def desc(p):             # tag, size (expandable), payload start
        tag = d[p]; p += 1; sz = 0
        for _ in range(4):
            b = d[p]; p += 1; sz = (sz << 7) | (b & 0x7F)
            if not b & 0x80: break
        return tag, sz, p
    tag, sz, p = desc(p); assert tag == 3; p += 3        # ES_Descr: ES_ID(2) flags(1)
    tag, sz, p = desc(p); assert tag == 4                 # DecoderConfigDescr
    otype, stype = d[p], d[p+1]; bufsz = int.from_bytes(d[p+2:p+5], 'big')
    maxbr, avgbr = struct.unpack('>II', d[p+5:p+13])
    print(f"{path}: objectType=0x{otype:02x} bufferSizeDB={bufsz} bytes ({bufsz*8} bits) maxBitrate={maxbr} avgBitrate={avgbr}")
    return bufsz

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('file'); ap.add_argument('--bitrate', type=int, default=0)
    ap.add_argument('--start', type=float, default=0.5)
    a = ap.parse_args()
    if a.file.endswith(('.m4a', '.mp4')): mp4(a.file)
    else: sys.exit(0 if adts(a.file, a.bitrate, a.start) else 1)
