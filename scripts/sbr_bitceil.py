#!/usr/bin/env python3
"""Offline SBR "bit ceiling" re-coder.

Parses the extended SBR_DUMP produced by the sbr-dump-work worktree's
libfaad/sbr.c (records B/H/T/D/E/Q -- see that file for the exact grammar)
and recomputes, from FAAC's own decoded integer envelope/noise-floor values,
how many bits FAAC's SBR payload would cost if it chose the cheaper of
frequency-delta / time-delta per envelope (and per noise envelope), and if
CPEs used stereo coupling (level+balance) when cheaper.

Record grammar emitted by the modified decoder:
  B id offset nsyms len0 len1 ... len(nsyms-1)      -- Huffman code lengths
  H frame nch amp_res start stop xover fscale ascale nbands lbands lgains \
    interp smooth reset kx M n_low n_high n_q extra1 extra2
  T frame n_low f_low[0..n_low] n_high f_high[0..n_high]
  D frame ch frame_class L_E L_Q n_low n_high amp_res coupling \
    grid_bits dtdf_bits invf_bits env_bits noise_bits addharm_bits ext_bits \
    total_bits nb0 n_q
    (grid/dtdf/invf/addharm/ext/total are only meaningful on the first
     channel of an element; they are 0 on the second.)
  E frame ch |l:freq_res:df_env,v0,v1,...  (one '|' block per envelope)
  Q frame ch |l:df_noise,v0,v1,...          (one '|' block per noise envelope)

Huffman book ids (matches HB_* enum order in libfaad/faad_internal.h):
  0 T_ENV_15  1 F_ENV_15  2 T_ENV_BAL_15  3 F_ENV_BAL_15
  4 T_ENV_30  5 F_ENV_30  6 T_ENV_BAL_30  7 F_ENV_BAL_30
  8 T_NOISE_30  9 T_NOISE_BAL_30
"""
import sys
import math
import argparse
import statistics
from collections import defaultdict

HB_T_ENV_15, HB_F_ENV_15, HB_T_ENV_BAL_15, HB_F_ENV_BAL_15 = 0, 1, 2, 3
HB_T_ENV_30, HB_F_ENV_30, HB_T_ENV_BAL_30, HB_F_ENV_BAL_30 = 4, 5, 6, 7
HB_T_NOISE_30, HB_T_NOISE_BAL_30 = 8, 9


class Book:
    def __init__(self, offset, lengths):
        self.offset = offset
        self.lengths = lengths  # index = value + offset

    def len_of(self, value):
        i = value + self.offset
        if 0 <= i < len(self.lengths):
            return self.lengths[i]
        return None  # out of table range: shouldn't happen for real streams


def parse_dump(path):
    books = {}
    headers = {}   # frame_idx -> dict (most recent header effective AT this frame)
    tables = {}    # frame_idx (of reset) -> (f_low list, f_high list)
    data = defaultdict(dict)   # (frame, ch) -> D fields
    envs = defaultdict(dict)   # (frame, ch) -> list of (l, freq_res, df, values)
    noises = defaultdict(dict)  # (frame, ch) -> list of (l, df, values)

    cur_header = None
    cur_table = None
    with open(path) as fh:
        for line in fh:
            p = line.split()
            if not p:
                continue
            tag = p[0]
            if tag == "B":
                bid, off, nsyms = int(p[1]), int(p[2]), int(p[3])
                lens = [int(x) for x in p[4:4 + nsyms]]
                books[bid] = Book(off, lens)
            elif tag == "H":
                frame = int(p[1])
                fields = dict(zip(
                    ["frame", "nch", "amp_res", "start", "stop", "xover", "fscale",
                     "ascale", "nbands", "lbands", "lgains", "interp", "smooth",
                     "reset", "kx", "M", "n_low", "n_high", "n_q", "extra1", "extra2"],
                    [int(x) for x in p[1:22]]))
                cur_header = fields
                headers[frame] = fields
            elif tag == "T":
                frame = int(p[1])
                idx = 2
                n_low = int(p[idx]); idx += 1
                f_low = [int(x) for x in p[idx:idx + n_low + 1]]; idx += n_low + 1
                n_high = int(p[idx]); idx += 1
                f_high = [int(x) for x in p[idx:idx + n_high + 1]]; idx += n_high + 1
                cur_table = (f_low, f_high)
                tables[frame] = cur_table
            elif tag == "D":
                frame, ch = int(p[1]), int(p[2])
                fields = dict(zip(
                    ["frame_class", "L_E", "L_Q", "n_low", "n_high", "amp_res",
                     "coupling", "grid_bits", "dtdf_bits", "invf_bits", "env_bits",
                     "noise_bits", "addharm_bits", "ext_bits", "total_bits", "nb0", "n_q"],
                    [int(x) for x in p[3:20]]))
                fields["header"] = cur_header
                fields["table"] = cur_table
                data[(frame, ch)] = fields
            elif tag == "E":
                frame, ch = int(p[1]), int(p[2])
                blocks = []
                for chunk in p[3:]:
                    assert chunk.startswith("|")
                    head, *rest = chunk[1:].split(",")
                    l, fr, df = (int(x) for x in head.split(":"))
                    vals = [int(x) for x in rest]
                    blocks.append((l, fr, df, vals))
                envs[(frame, ch)] = blocks
            elif tag == "Q":
                frame, ch = int(p[1]), int(p[2])
                blocks = []
                for chunk in p[3:]:
                    head, *rest = chunk[1:].split(",")
                    l, df = (int(x) for x in head.split(":"))
                    vals = [int(x) for x in rest]
                    blocks.append((l, df, vals))
                noises[(frame, ch)] = blocks
    return books, headers, tables, data, envs, noises


def freq_res_map_index(k, r_prev, r_cur, header, table):
    """Mirrors sbr.c lines ~661-671: maps band k of the current resolution to
    an index into the previous envelope's array of the OTHER resolution."""
    f_low, f_high = table
    n_low = header["n_low"]
    n_high = header["n_high"]
    if r_cur:  # current is high-res, previous was low-res
        i = 0
        while i + 1 < n_low and f_high[k] >= f_low[i + 1]:
            i += 1
        return i
    else:  # current is low-res, previous was high-res
        i = 0
        while i < n_high and f_high[i] != f_low[k]:
            i += 1
        return i if i < n_high else n_high - 1


def env_df_cost(vals, book, start_bits):
    bits = start_bits
    for k in range(1, len(vals)):
        d = vals[k] - vals[k - 1]
        l = book.len_of(d)
        if l is None:
            return None
        bits += l
    return bits


def env_dt_cost(vals, ref, book):
    bits = 0
    for k in range(len(vals)):
        d = vals[k] - ref[k]
        l = book.len_of(d)
        if l is None:
            return None
        bits += l
    return bits


def choose_book_env(amp_res, balance, freq):
    if freq:
        return (HB_F_ENV_BAL_30 if balance else HB_F_ENV_30) if amp_res else \
               (HB_F_ENV_BAL_15 if balance else HB_F_ENV_15)
    else:
        return (HB_T_ENV_BAL_30 if balance else HB_T_ENV_30) if amp_res else \
               (HB_T_ENV_BAL_15 if balance else HB_T_ENV_15)


def start_bits_env(amp_res, balance):
    return (5 if balance else 6) if amp_res else (6 if balance else 7)


def pow2half(n):
    return math.pow(2.0, n / 2.0)


def analyze(path, label):
    books, headers, tables, data, envs, noises = parse_dump(path)

    frames = sorted(set(f for (f, c) in data.keys()))
    # per-channel running state for time-delta reference and reset tracking
    chan_state = defaultdict(lambda: {"E_prev": None, "fr_prev": None,
                                       "Q_prev": None, "has_prev_env": False,
                                       "has_prev_noise": False})

    total_actual_env = 0
    total_actual_noise = 0
    total_dumped_env = 0
    total_dumped_noise = 0
    match_count = 0
    total_count = 0

    total_df_only_bits = 0    # == actual (sanity)
    total_dt_opt_bits = 0     # min(df,dt) per envelope/noise-env, no coupling
    n_env_dt_used = 0
    n_env_total = 0
    n_noise_dt_used = 0
    n_noise_total = 0

    # coupling stats
    total_coupled_or_uncoupled_bits = 0  # (c) alone: per-frame min(uncoupled_df, coupled_df)
    total_both_bits = 0                  # (b)+(c): per-frame min(uncoupled_dtopt, coupled_dtopt)
    cpe_frames = 0
    grid_match_frames = 0
    coupled_chosen_frames = 0
    roundtrip_total = 0
    roundtrip_ok = 0

    payload_frames = 0
    payload_bits_actual = 0  # from dumped 'total_bits' (grid+dtdf+invf+addharm+ext+env+noise), per element

    actual_env_dt = 0
    actual_env_n = 0
    actual_noise_dt = 0
    actual_noise_n = 0
    actual_coupled_elems = 0
    actual_elems = 0
    sum_L_E = 0

    for frame in frames:
        chs_here = sorted(c for (f, c) in data.keys() if f == frame)
        if not chs_here:
            continue
        # element = pair (0,1) if both exist and share the same header, else singles
        # our corpus is always CPE, so expect chs_here == [0,1]
        elem_total = 0
        have_total = False
        per_ch_uncoupled_dtopt = {}
        per_ch_uncoupled_df = {}
        ch_info = {}
        for ch in chs_here:
            d = data[(frame, ch)]
            header = d["header"]
            table = d["table"]
            amp_res = d["amp_res"]
            eb = envs.get((frame, ch), [])
            nb_ = noises.get((frame, ch), [])
            st = chan_state[ch]

            df_book_id = choose_book_env(amp_res, False, True)
            tf_book_id = choose_book_env(amp_res, False, False)
            f_book = books.get(df_book_id)
            t_book = books.get(tf_book_id)
            sbits = start_bits_env(amp_res, False)

            df_total = 0
            dtopt_total = 0
            env_used_dt = []
            cur_E_by_l = {}
            for (l, fr, df_flag, vals) in eb:
                n_env_total += 1
                df_cost = env_df_cost(vals, f_book, sbits)
                # reference for dt
                if l == 0:
                    ref = st["E_prev"]
                    r_prev = st["fr_prev"]
                    has_prev = st["has_prev_env"]
                else:
                    ref = cur_E_by_l[l - 1][0]
                    r_prev = cur_E_by_l[l - 1][1]
                    has_prev = True
                dt_cost = None
                if has_prev and ref is not None:
                    if r_prev == fr:
                        mapped_ref = ref
                    else:
                        mapped_ref = []
                        for k in range(len(vals)):
                            idx = freq_res_map_index(k, r_prev, fr, header, table)
                            mapped_ref.append(ref[idx])
                    dt_cost = env_dt_cost(vals, mapped_ref, t_book)
                best = df_cost if (dt_cost is None or df_cost <= dt_cost) else dt_cost
                if dt_cost is not None and dt_cost < df_cost:
                    env_used_dt.append(1)
                    n_env_dt_used += 1
                df_total += df_cost if df_cost is not None else 0
                dtopt_total += best if best is not None else (df_cost or 0)
                cur_E_by_l[l] = (vals, fr)
            if eb:
                last_l = max(x[0] for x in eb)
                st["E_prev"] = cur_E_by_l[last_l][0]
                st["fr_prev"] = cur_E_by_l[last_l][1]
                st["has_prev_env"] = True

            # noise
            tn_book = books.get(HB_T_NOISE_30)
            fn_book = books.get(HB_F_ENV_30)  # noise freq shares the 3dB envelope freq table
            ndf_total = 0
            ndtopt_total = 0
            cur_Q_by_l = {}
            for (l, df_flag, vals) in nb_:
                n_noise_total += 1
                ndf_cost = env_df_cost(vals, fn_book, 5)
                if l == 0:
                    ref = st["Q_prev"]
                    has_prev = st["has_prev_noise"]
                else:
                    ref = cur_Q_by_l[l - 1]
                    has_prev = True
                ndt_cost = env_dt_cost(vals, ref, tn_book) if (has_prev and ref is not None) else None
                best = ndf_cost if (ndt_cost is None or ndf_cost <= ndt_cost) else ndt_cost
                if ndt_cost is not None and ndt_cost < ndf_cost:
                    n_noise_dt_used += 1
                ndf_total += ndf_cost if ndf_cost is not None else 0
                ndtopt_total += best if best is not None else (ndf_cost or 0)
                cur_Q_by_l[l] = vals
            if nb_:
                last_l = max(x[0] for x in nb_)
                st["Q_prev"] = cur_Q_by_l[last_l]
                st["has_prev_noise"] = True

            total_actual_env += d["env_bits"]
            total_actual_noise += d["noise_bits"]
            total_dumped_env += df_total
            total_dumped_noise += ndf_total
            total_count += 1
            if df_total == d["env_bits"] and ndf_total == d["noise_bits"]:
                match_count += 1

            total_df_only_bits += df_total + ndf_total
            total_dt_opt_bits += dtopt_total + ndtopt_total

            per_ch_uncoupled_df[ch] = df_total + ndf_total
            per_ch_uncoupled_dtopt[ch] = dtopt_total + ndtopt_total
            ch_info[ch] = (d, eb, nb_, amp_res)

            for (l, fr, df_flag, vals) in eb:
                actual_env_n += 1
                actual_env_dt += df_flag
            for (l, df_flag, vals) in nb_:
                actual_noise_n += 1
                actual_noise_dt += df_flag

            if ch == chs_here[0]:
                payload_bits_actual += d["total_bits"]
                elem_total = True
                actual_elems += 1
                actual_coupled_elems += d["coupling"]
                sum_L_E += d["L_E"]

        payload_frames += 1

        # ---- coupling (only meaningful for a true CPE: exactly 2 channels) ----
        if len(chs_here) == 2:
            cpe_frames += 1
            ch0, ch1 = chs_here
            d0, eb0, nb0_, amp0 = ch_info[ch0]
            d1, eb1, nb1_, amp1 = ch_info[ch1]
            grid_match = (d0["frame_class"] == d1["frame_class"] and d0["L_E"] == d1["L_E"] and
                          amp0 == amp1 and [e[1] for e in eb0] == [e[1] for e in eb1] and
                          d0["L_Q"] == d1["L_Q"])
            uncoupled_df = per_ch_uncoupled_df[ch0] + per_ch_uncoupled_df[ch1]
            uncoupled_dtopt = per_ch_uncoupled_dtopt[ch0] + per_ch_uncoupled_dtopt[ch1]
            coupled_df = None
            if grid_match:
                grid_match_frames += 1
                step = 2 if amp0 else 1
                pan = 12 if amp0 else 24
                lvl_book = books.get(choose_book_env(amp0, False, True))
                lvl_sbits = start_bits_env(amp0, False)
                bal_book = books.get(choose_book_env(amp0, True, True))
                bal_sbits = start_bits_env(amp0, True)
                coupled_df = 0
                for (l, fr, df0, v0), (_, _, df1, v1) in zip(eb0, eb1):
                    lvl_vals = []
                    bal_vals = []
                    for k in range(len(v0)):
                        e0 = 64.0 * pow2half(step * v0[k])
                        e1 = 64.0 * pow2half(step * v1[k])
                        e_avg = (e0 + e1) / 2.0
                        b_ratio = e1 / e0 if e0 > 0 else 1e9
                        elev = (2.0 / step) * math.log2(e_avg / 64.0)
                        ebal = pan - (2.0 / step) * math.log2(b_ratio)
                        elev_i = round(elev)
                        ebal_i = 2 * round(ebal / 2.0)
                        lvl_vals.append(elev_i)
                        bal_vals.append(ebal_i)
                        # round-trip check
                        roundtrip_total += 1
                        e_lvl = 64.0 * pow2half(step * elev_i)
                        b_lvl = pow2half(step * (pan - ebal_i))
                        e0r = 2.0 * e_lvl / (1.0 + b_lvl)
                        e1r = 2.0 * e_lvl / (1.0 + 1.0 / b_lvl)
                        v0r = round((2.0 / step) * math.log2(e0r / 64.0)) if e0r > 0 else None
                        v1r = round((2.0 / step) * math.log2(e1r / 64.0)) if e1r > 0 else None
                        if v0r == v0[k] and v1r == v1[k]:
                            roundtrip_ok += 1
                    c_lvl = env_df_cost(lvl_vals, lvl_book, lvl_sbits)
                    c_bal = env_df_cost([v // 2 for v in bal_vals], bal_book, bal_sbits) if bal_book else None
                    if c_lvl is not None and c_bal is not None:
                        coupled_df += c_lvl + c_bal
                    else:
                        coupled_df = None
                        break
                # noise coupling (simplified: freq-delta only, same transform, pan=12/step effectively fixed 3dB)
                if coupled_df is not None:
                    nlvl_book = books.get(HB_F_ENV_30)
                    nbal_book = books.get(HB_F_ENV_BAL_30)
                    for (l, dfq0, qv0), (_, dfq1, qv1) in zip(nb0_, nb1_):
                        nlvl_vals = []
                        nbal_vals = []
                        ok = True
                        for k in range(len(qv0)):
                            q0 = pow2half(2 * (6 - qv0[k]))
                            q1 = pow2half(2 * (6 - qv1[k]))
                            q_avg = (q0 + q1) / 2.0
                            b_ratio = q1 / q0 if q0 > 0 else 1e9
                            qlev = 6 - 0.5 * math.log2(q_avg)
                            qbal = 12 - math.log2(b_ratio)
                            qlev_i = round(qlev)
                            qbal_i = 2 * round(qbal / 2.0)
                            nlvl_vals.append(qlev_i)
                            nbal_vals.append(qbal_i)
                        c_nlvl = env_df_cost(nlvl_vals, nlvl_book, 5)
                        c_nbal = env_df_cost([v // 2 for v in nbal_vals], nbal_book, 5) if nbal_book else None
                        if c_nlvl is not None and c_nbal is not None:
                            coupled_df += c_nlvl + c_nbal
                        else:
                            ok = False
                    if not ok:
                        coupled_df = None
            if coupled_df is not None and coupled_df < uncoupled_df:
                coupled_chosen_frames += 1
                total_coupled_or_uncoupled_bits += coupled_df
            else:
                total_coupled_or_uncoupled_bits += uncoupled_df
            # combined (b)+(c): approximate coupled-dt savings as coupled_df scaled by the
            # same per-channel dt/df ratio observed uncoupled (documented approximation)
            if coupled_df is not None:
                ratio = (uncoupled_dtopt / uncoupled_df) if uncoupled_df else 1.0
                combined_coupled = coupled_df * ratio
                total_both_bits += min(uncoupled_dtopt, combined_coupled)
            else:
                total_both_bits += uncoupled_dtopt
        else:
            total_coupled_or_uncoupled_bits += sum(per_ch_uncoupled_df.values())
            total_both_bits += sum(per_ch_uncoupled_dtopt.values())

    n_frames = len(frames)
    return {
        "label": label,
        "n_frames": n_frames,
        "payload_bits_actual": payload_bits_actual,
        "payload_frames": payload_frames,
        "unit_check_match": match_count,
        "unit_check_total": total_count,
        "actual_env_noise_bits": total_actual_env + total_actual_noise,
        "recomputed_df_bits": total_df_only_bits,
        "dt_opt_bits": total_dt_opt_bits,
        "coupling_opt_bits": total_coupled_or_uncoupled_bits,
        "both_opt_bits": total_both_bits,
        "n_env_total": n_env_total,
        "n_env_dt_used": n_env_dt_used,
        "n_noise_total": n_noise_total,
        "n_noise_dt_used": n_noise_dt_used,
        "cpe_frames": cpe_frames,
        "grid_match_frames": grid_match_frames,
        "coupled_chosen_frames": coupled_chosen_frames,
        "roundtrip_total": roundtrip_total,
        "roundtrip_ok": roundtrip_ok,
        "actual_env_dt": actual_env_dt,
        "actual_env_n": actual_env_n,
        "actual_noise_dt": actual_noise_dt,
        "actual_noise_n": actual_noise_n,
        "actual_coupled_elems": actual_coupled_elems,
        "actual_elems": actual_elems,
        "sum_L_E": sum_L_E,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", nargs="+")
    ap.add_argument("--fps", type=float, required=True, help="SBR frames per second (core_rate/1024)")
    args = ap.parse_args()

    agg = None
    for path in args.dump:
        r = analyze(path, path)
        if agg is None:
            agg = {k: (0 if isinstance(v, (int, float)) else v) for k, v in r.items()}
        for k, v in r.items():
            if isinstance(v, (int, float)):
                agg[k] += v

    fps = args.fps
    def kbps(bits_total, n_frames):
        return bits_total * fps / n_frames / 1000.0

    payload_kbps = kbps(agg["payload_bits_actual"], agg["payload_frames"])
    dt_kbps = kbps(agg["dt_opt_bits"] + (agg["payload_bits_actual"] - agg["actual_env_noise_bits"]), agg["payload_frames"])
    coupling_kbps = kbps(agg["coupling_opt_bits"] + (agg["payload_bits_actual"] - agg["actual_env_noise_bits"]), agg["payload_frames"])
    both_kbps = kbps(agg["both_opt_bits"] + (agg["payload_bits_actual"] - agg["actual_env_noise_bits"]), agg["payload_frames"])

    print(f"frames                       : {agg['payload_frames']}")
    print(f"unit check (env+noise exact) : {agg['unit_check_match']}/{agg['unit_check_total']} "
          f"({100.0*agg['unit_check_match']/max(1,agg['unit_check_total']):.2f}%)")
    print(f"actual SBR payload           : {payload_kbps:.4f} kbps")
    print(f"dt-optimal SBR payload       : {dt_kbps:.4f} kbps  (saved {payload_kbps-dt_kbps:.4f} kbps)")
    print(f"coupling-optimal SBR payload : {coupling_kbps:.4f} kbps  (saved {payload_kbps-coupling_kbps:.4f} kbps)")
    print(f"dt+coupling SBR payload      : {both_kbps:.4f} kbps  (saved {payload_kbps-both_kbps:.4f} kbps)")
    print(f"envelopes using dt if allowed: {agg['n_env_dt_used']}/{agg['n_env_total']} "
          f"({100.0*agg['n_env_dt_used']/max(1,agg['n_env_total']):.1f}%)")
    print(f"noise-envs using dt if allowed: {agg['n_noise_dt_used']}/{agg['n_noise_total']} "
          f"({100.0*agg['n_noise_dt_used']/max(1,agg['n_noise_total']):.1f}%)")
    print(f"CPE frames                   : {agg['cpe_frames']}")
    print(f"  grid-matching (couplable)  : {agg['grid_match_frames']} "
          f"({100.0*agg['grid_match_frames']/max(1,agg['cpe_frames']):.1f}%)")
    print(f"  coupling chosen (cheaper)  : {agg['coupled_chosen_frames']}")
    print(f"  coupling exact round-trip  : {agg['roundtrip_ok']}/{agg['roundtrip_total']} "
          f"({100.0*agg['roundtrip_ok']/max(1,agg['roundtrip_total']):.1f}%)")
    print(f"--- actual stream stats ---")
    print(f"actual env dt fraction        : {100.0*agg['actual_env_dt']/max(1,agg['actual_env_n']):.1f}% "
          f"({agg['actual_env_dt']}/{agg['actual_env_n']})")
    print(f"actual noise dt fraction      : {100.0*agg['actual_noise_dt']/max(1,agg['actual_noise_n']):.1f}% "
          f"({agg['actual_noise_dt']}/{agg['actual_noise_n']})")
    print(f"actual coupled-element fraction: {100.0*agg['actual_coupled_elems']/max(1,agg['actual_elems']):.1f}% "
          f"({agg['actual_coupled_elems']}/{agg['actual_elems']})")
    print(f"mean numEnvelopes (L_E)       : {agg['sum_L_E']/max(1,agg['actual_elems']):.2f}")


if __name__ == "__main__":
    main()
