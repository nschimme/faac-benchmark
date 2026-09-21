"""
 * FAAC Benchmark Suite - Container Muxer Benchmark (FAAM vs FFmpeg vs MP4Box)
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

import os
import shutil
import statistics

from utils import safe_run, measure_peak_ram


def _timed(cmd_builder, iterations, timeout=30):
    """Runs cmd_builder() `iterations` times, discarding nothing (the
    builder is expected to overwrite its own output each call), returning
    (mean_ms, std_ms) or None if any run fails."""
    times_ms = []
    for _ in range(iterations):
        cmd = cmd_builder()
        res, duration, _rss = measure_peak_ram(cmd, timeout=timeout)
        if res.returncode != 0:
            return None
        times_ms.append(duration * 1000.0)
    mean_ms = statistics.mean(times_ms)
    std_ms = statistics.pstdev(times_ms) if len(times_ms) > 1 else 0.0
    return mean_ms, std_ms


def run_muxer_bench(faam_bin, ffmpeg_bin, largest_adts, output_dir, iterations=10):
    """Times faam vs ffmpeg (-c:a copy) vs MP4Box (if present) muxing the
    largest ADTS elementary stream from this run into M4A, demuxing it back
    out, and (faam only) injecting iTunes tags. Returns a list of row dicts;
    skips a tool cleanly (a "note", no fabricated numbers) when it's missing
    or a run fails."""
    rows = []
    if not largest_adts or not os.path.exists(largest_adts):
        return rows

    mp4box_bin = shutil.which("MP4Box")
    os.makedirs(output_dir, exist_ok=True)
    m4a_out = os.path.join(output_dir, "muxer_bench.m4a")

    mux_tools = [
        ("faam", faam_bin, lambda: [faam_bin, "mux", largest_adts, "-o", m4a_out]),
        ("ffmpeg", ffmpeg_bin, lambda: [ffmpeg_bin, "-y", "-v", "error", "-i", largest_adts, "-c:a", "copy", m4a_out]),
        ("MP4Box", mp4box_bin, lambda: [mp4box_bin, "-quiet", "-add", largest_adts, "-new", m4a_out]),
    ]
    for tool, bin_path, builder in mux_tools:
        if not bin_path or not os.path.exists(bin_path):
            rows.append({"tool": tool, "op": "Mux AAC->M4A", "mean_ms": None, "std_ms": None, "note": "not found, skipped"})
            continue
        res = _timed(builder, iterations)
        rows.append({"tool": tool, "op": "Mux AAC->M4A", "mean_ms": res[0] if res else None,
                      "std_ms": res[1] if res else None, "note": None if res else "run failed, skipped"})

    if not os.path.exists(m4a_out):
        return rows

    demux_out = os.path.join(output_dir, "muxer_bench_demux.aac")
    demux_tools = [
        ("faam", faam_bin, lambda: [faam_bin, "demux", m4a_out, "-o", demux_out]),
        ("MP4Box", mp4box_bin, lambda: [mp4box_bin, "-quiet", "-raw", "1", m4a_out, "-out", demux_out]),
    ]
    for tool, bin_path, builder in demux_tools:
        if not bin_path or not os.path.exists(bin_path):
            rows.append({"tool": tool, "op": "Demux M4A->AAC", "mean_ms": None, "std_ms": None, "note": "not found, skipped"})
            continue
        res = _timed(builder, iterations)
        rows.append({"tool": tool, "op": "Demux M4A->AAC", "mean_ms": res[0] if res else None,
                      "std_ms": res[1] if res else None, "note": None if res else "run failed, skipped"})

    if faam_bin and os.path.exists(faam_bin):
        res = _timed(lambda: [faam_bin, "tag", m4a_out, "--title", "Bench Title", "--artist", "Bench Artist", "--album", "Bench Album"], iterations)
        rows.append({"tool": "faam", "op": "Inject iTunes Tags", "mean_ms": res[0] if res else None,
                      "std_ms": res[1] if res else None, "note": None if res else "run failed / unsupported flags, skipped"})
    else:
        rows.append({"tool": "faam", "op": "Inject iTunes Tags", "mean_ms": None, "std_ms": None, "note": "not found, skipped"})

    for f in (m4a_out, demux_out):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    return rows
