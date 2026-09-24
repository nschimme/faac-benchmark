#!/usr/bin/env python3
"""Summarize libfaad3's SBR_DUMP diagnostic files (one per encoder/stream),
so FAAC, fdkaac and afconvert (or any other HE-AAC encoder) can be diffed on
what SBR decisions they actually encoded.

The dump format (see libfaad/sbr.c / libfaad/ps.c, gated on -Dstats and the
SBR_DUMP env var -- local probe-only tooling, never shipped):

  H frame_idx ch_cfg bs_amp_res bs_start_freq bs_stop_freq bs_xover_band \
    bs_freq_scale bs_alter_scale bs_noise_bands bs_limiter_bands \
    bs_limiter_gains bs_interpol_freq bs_smoothing_mode header_changed_flag \
    kx M N_low N_high NQ header_extra1 header_extra2
  F frame_idx ch frame_class num_env num_noise freq_res_csv amp_res_effective \
    invf_csv add_harmonic_flag n_add_harmonic coupling bits_channel \
    mean_env_dB mean_noise_dB
  P frame_idx enable_iid enable_icc num_env

Usage:
  python3 sbr_dump_summary.py [--fs 48000] [--csv out.csv] dump1.txt [dump2.txt ...]
"""
import argparse
import csv
import statistics
import sys
from collections import Counter

FRAME_CLASS_NAMES = {0: "FIXFIX", 1: "FIXVAR", 2: "VARFIX", 3: "VARVAR"}

H_FIELDS = [
    "frame_idx", "ch_cfg", "bs_amp_res", "bs_start_freq", "bs_stop_freq",
    "bs_xover_band", "bs_freq_scale", "bs_alter_scale", "bs_noise_bands",
    "bs_limiter_bands", "bs_limiter_gains", "bs_interpol_freq",
    "bs_smoothing_mode", "header_changed", "kx", "M", "N_low", "N_high",
    "NQ", "extra1", "extra2",
]
F_FIELDS = [
    "frame_idx", "ch", "frame_class", "num_env", "num_noise", "freq_res",
    "amp_res_eff", "invf", "add_harmonic_flag", "n_add_harmonic",
    "coupling", "bits", "mean_env_db", "mean_noise_db",
]


def parse_file(path):
    h_rows, f_rows, p_rows = [], [], []
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if not parts:
                continue
            tag = parts[0]
            rest = parts[1:]
            if tag == "H" and len(rest) == len(H_FIELDS):
                h_rows.append(dict(zip(H_FIELDS, rest)))
            elif tag == "F" and len(rest) == len(F_FIELDS):
                f_rows.append(dict(zip(F_FIELDS, rest)))
            elif tag == "P" and len(rest) == 4:
                p_rows.append(dict(zip(["frame_idx", "enable_iid", "enable_icc", "num_env"], rest)))
    return h_rows, f_rows, p_rows


def mode_and_dist(values):
    c = Counter(values)
    total = sum(c.values())
    if total == 0:
        return "n/a", {}
    mode_val, mode_n = c.most_common(1)[0]
    dist = {k: round(100.0 * n / total, 1) for k, n in sorted(c.items(), key=lambda kv: -kv[1])}
    return mode_val, dist


def kx_to_hz(kx, fs):
    """kx is in QMF bands of the SBR-rate (2x core) spectrum; each band is
    fs_sbr / 128 Hz wide (64 QMF bands span fs_sbr/2)."""
    if fs is None:
        return None
    fs_sbr = fs  # caller passes the SBR-side (output) sample rate
    return kx * fs_sbr / 128.0


def summarize(path, fs):
    h_rows, f_rows, p_rows = parse_file(path)
    out = {"file": path, "n_header": len(h_rows), "n_frames_sbr": len(f_rows), "n_ps": len(p_rows)}

    print(f"\n=== {path} ===")
    if not h_rows:
        print("  no H (SBR header) records found -- not an HE-AAC stream, or SBR never engaged")
    else:
        print(f"  SBR headers seen: {len(h_rows)} (of which changed: {sum(1 for r in h_rows if r['header_changed'] == '1')})")
        for field, label in [
            ("bs_amp_res", "bs_amp_res"), ("bs_start_freq", "bs_start_freq"),
            ("bs_stop_freq", "bs_stop_freq"), ("bs_xover_band", "bs_xover_band"),
            ("bs_freq_scale", "bs_freq_scale"), ("bs_alter_scale", "bs_alter_scale"),
            ("bs_noise_bands", "bs_noise_bands"), ("bs_limiter_bands", "bs_limiter_bands"),
            ("bs_limiter_gains", "bs_limiter_gains"), ("bs_interpol_freq", "bs_interpol_freq"),
            ("bs_smoothing_mode", "bs_smoothing_mode"),
        ]:
            vals = [r[field] for r in h_rows]
            mode_val, dist = mode_and_dist(vals)
            print(f"    {label:20s}: mode={mode_val:>4s}  dist={dist}")
            out[f"h_{field}_mode"] = mode_val

        kx_vals = [int(r["kx"]) for r in h_rows]
        m_vals = [int(r["M"]) for r in h_rows]
        nlow_vals = [int(r["N_low"]) for r in h_rows]
        nhigh_vals = [int(r["N_high"]) for r in h_rows]
        nq_vals = [int(r["NQ"]) for r in h_rows]
        mode_kx, _ = mode_and_dist([str(v) for v in kx_vals])
        kx_mode = int(mode_kx)
        crossover_hz = kx_to_hz(kx_mode, fs) if fs else None
        print(f"    kx (crossover band): mode={kx_mode}"
              + (f"  -> {crossover_hz:.0f} Hz (fs_sbr={fs})" if crossover_hz else "  (pass --fs to get Hz)"))
        print(f"    M (hi-res bands)    : mean={statistics.mean(m_vals):.1f}")
        print(f"    N_low / N_high / NQ : {statistics.mean(nlow_vals):.1f} / {statistics.mean(nhigh_vals):.1f} / {statistics.mean(nq_vals):.1f}")
        extra1_pct = 100.0 * sum(1 for r in h_rows if r["extra1"] == "1") / len(h_rows)
        extra2_pct = 100.0 * sum(1 for r in h_rows if r["extra2"] == "1") / len(h_rows)
        print(f"    header_extra1/2 sent: {extra1_pct:.0f}% / {extra2_pct:.0f}% of headers")
        out["kx_mode"] = kx_mode
        out["crossover_hz"] = crossover_hz
        out["M_mean"] = statistics.mean(m_vals)
        out["N_low_mean"] = statistics.mean(nlow_vals)
        out["N_high_mean"] = statistics.mean(nhigh_vals)
        out["NQ_mean"] = statistics.mean(nq_vals)

    if not f_rows:
        print("  no F (per-channel SBR frame) records found")
    else:
        fc_vals = [FRAME_CLASS_NAMES.get(int(r["frame_class"]), r["frame_class"]) for r in f_rows]
        _, fc_dist = mode_and_dist(fc_vals)
        print(f"  frame_class share   : {fc_dist}")
        out["frame_class_dist"] = fc_dist

        env_vals = [int(r["num_env"]) for r in f_rows]
        noise_vals = [int(r["num_noise"]) for r in f_rows]
        print(f"  envelopes/frame     : mean={statistics.mean(env_vals):.2f}  (noise floors/frame mean={statistics.mean(noise_vals):.2f})")
        out["mean_env_per_frame"] = statistics.mean(env_vals)

        fr_vals = []
        for r in f_rows:
            fr_vals.extend(r["freq_res"].split(","))
        fr_vals = [v for v in fr_vals if v]
        _, fr_dist = mode_and_dist(fr_vals)
        print(f"  freq_res share (0=low/1=high): {fr_dist}")
        out["freq_res_dist"] = fr_dist

        invf_vals = []
        for r in f_rows:
            invf_vals.extend(r["invf"].split(","))
        invf_vals = [v for v in invf_vals if v]
        _, invf_dist = mode_and_dist(invf_vals)
        print(f"  invf mode histogram (0=OFF,1=LOW,2=MID,3=HIGH): {invf_dist}")
        out["invf_dist"] = invf_dist

        harm_pct = 100.0 * sum(1 for r in f_rows if r["add_harmonic_flag"] == "1") / len(f_rows)
        print(f"  add_harmonic share  : {harm_pct:.1f}% of channel-frames")
        out["add_harmonic_pct"] = harm_pct

        coupling_pct = 100.0 * sum(1 for r in f_rows if r["coupling"] == "1") / len(f_rows)
        print(f"  coupling share      : {coupling_pct:.1f}% of channel-frames")
        out["coupling_pct"] = coupling_pct

        amp_res_vals = [r["amp_res_eff"] for r in f_rows]
        _, amp_dist = mode_and_dist(amp_res_vals)
        print(f"  amp_res_effective   : {amp_dist} (0=1.5dB steps possible, 1=header's bs_amp_res)")

        mean_env_db = statistics.mean(float(r["mean_env_db"]) for r in f_rows)
        mean_noise_db = statistics.mean(float(r["mean_noise_db"]) for r in f_rows)
        print(f"  mean envelope energy: {mean_env_db:.2f} dB   mean noise floor: {mean_noise_db:.2f} dB")
        out["mean_env_db"] = mean_env_db
        out["mean_noise_db"] = mean_noise_db

    if p_rows:
        iid_pct = 100.0 * sum(1 for r in p_rows if r["enable_iid"] == "1") / len(p_rows)
        icc_pct = 100.0 * sum(1 for r in p_rows if r["enable_icc"] == "1") / len(p_rows)
        print(f"  PS present: {len(p_rows)} frames, enable_iid={iid_pct:.0f}% enable_icc={icc_pct:.0f}%")
        out["ps_frames"] = len(p_rows)
    else:
        out["ps_frames"] = 0

    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dumps", nargs="+", help="SBR_DUMP output file(s)")
    ap.add_argument("--fs", type=int, default=None, help="SBR-side (post-SBR/output) sample rate, for kx->Hz")
    ap.add_argument("--csv", default=None, help="write one summary row per file to this CSV")
    args = ap.parse_args()

    rows = [summarize(path, args.fs) for path in args.dumps]

    if args.csv:
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\nwrote {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
