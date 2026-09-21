"""
 * FAAC Benchmark Suite - Decoder Gate PASS/FAIL Evaluation
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

CONFORMANCE_SNR_FLOOR_DB = 60.0


def _is_faad3(row):
    return str(row.get("row_key", "")).startswith("faad3")


def evaluate_gate(decoder_results, decoder_robustness_results):
    """Pass/fail summary for --gate, per compare_codecs' gate contract:

    The gate judges FAAD3 (the decoder under test); the reference decoders'
    own limitations (e.g. FAAD2 has no PS) are leaderboard data, not gate
    conditions. FAIL if FAAD3 failed/timed out on an intact stream or ran
    away on a corrupted one, if its conformance SNR (vs the FFmpeg decode of
    the same bitstream) drops below CONFORMANCE_SNR_FLOOR_DB on any measured
    stream, or if its gapless offset on an M4A stream exceeds 2 samples
    (fdkaac HE-AAC files excluded, see below).

    PNS noise is non-normative, so the conformance bar applies only to
    PNS-free streams: the faac "(PNS off)" encoder variant and fdkaac's HE
    profiles. Other rows are reported, not gated.

    Returns (passed: bool, lines: list[str]).
    """
    lines = []
    ok = True

    faad3_all = [r for r in decoder_results if _is_faad3(r)]
    intact_fail = [r for r in faad3_all if not r.get("decode_valid")]
    if intact_fail:
        ok = False
        for r in intact_fail[:15]:
            reason = "timeout" if r.get("timeout") else (r.get("decode_error") or "decode failed")
            lines.append(f"FAIL decode: {r['tool']} {r['scenario']}/{r['filename']} ({r.get('profile', 'lc')}): {reason}")
    else:
        lines.append(f"PASS: all {len(faad3_all)} FAAD3 intact-stream decodes OK")
    other_fail = [r for r in decoder_results if not _is_faad3(r) and not r.get("decode_valid")]
    if other_fail:
        lines.append(f"NOTE: {len(other_fail)} reference-decoder decodes failed (not gated): " +
                     ", ".join(sorted({r['tool'] for r in other_fail})))

    faad3_rob = [r for r in decoder_robustness_results if _is_faad3(r)]
    runaway = [r for r in faad3_rob if r.get("runaway") or r.get("timeout")]
    if runaway:
        ok = False
        for r in runaway[:15]:
            what = "timed out" if r.get("timeout") else "produced oversized output"
            lines.append(f"FAIL robustness: {r['tool']} {r['scenario']}/{r['filename']} {what} on a corrupted stream")
    else:
        lines.append(f"PASS: no FAAD3 timeouts or runaways across {len(faad3_rob)} robustness cases")

    # Only PNS-free streams can be held to a sample-level conformance bar:
    # faac's "(PNS off)" variant and fdkaac's HE profiles (which do not use PNS).
    def _pns_free(r):
        key = str(r.get("encoder_row_key", ""))
        return "_nopns" in key or (key.startswith("fdkaac") and r.get("profile") in ("he", "hev2"))
    faad3_rows = [r for r in decoder_results if _is_faad3(r) and r.get("decode_valid") and _pns_free(r)]
    snr_bad = [r for r in faad3_rows
               if isinstance(r.get("conformance_snr_db"), (int, float)) and r["conformance_snr_db"] < CONFORMANCE_SNR_FLOOR_DB]
    if snr_bad:
        ok = False
        for r in snr_bad[:15]:
            lines.append(f"FAIL conformance SNR: faad3 {r['scenario']}/{r['filename']}: "
                         f"{r['conformance_snr_db']:.1f} dB < {CONFORMANCE_SNR_FLOOR_DB:.0f} dB")
    else:
        measured = [r for r in faad3_rows if isinstance(r.get("conformance_snr_db"), (int, float))]
        lines.append(f"PASS: faad3 conformance SNR >= {CONFORMANCE_SNR_FLOOR_DB:.0f} dB on all {len(measured)} measured streams")

    # fdkaac's HE-AAC priming assumes libfdk's decoder, which removes the SBR
    # delay internally; spec-delay decoders (FAAD2 included) land 961 samples
    # late on those files, so they carry no gapless verdict. Cross-correlation
    # alignment is ambiguous by a sample or two on very tonal clips.
    def _fdk_he(r):
        key = str(r.get("encoder_row_key", ""))
        return key.startswith("fdkaac") and r.get("profile") in ("he", "hev2")
    m4a_rows = [r for r in decoder_results if _is_faad3(r) and r.get("decode_valid") and r.get("container") == "M4A"
                and r.get("gapless_offset_samples") is not None and not _fdk_he(r)]
    offset_bad = [r for r in m4a_rows if abs(r["gapless_offset_samples"]) > 2]
    if offset_bad:
        ok = False
        for r in offset_bad[:15]:
            lines.append(f"FAIL gapless offset: faad3 {r['scenario']}/{r['filename']}: "
                         f"{r['gapless_offset_samples']} samples (expected 0)")
    elif m4a_rows:
        lines.append(f"PASS: faad3 gapless offset == 0 samples on all {len(m4a_rows)} M4A streams")
    else:
        lines.append("WARN: no M4A streams measured for gapless offset")

    return ok, lines
