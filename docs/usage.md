# Local Usage

Run benchmarks and diagnostics on your own builds. The suite always compares
**a candidate build against a baseline build** — point it at the exact `faac`
binary and `libfaac.so` you want to test (not a system package), so results
reflect your code and the provenance hashing stays meaningful.

## Run a benchmark

```bash
python3 run_benchmark.py <faac> <libfaac.so> <name> <output.json> [options]
```

Common options:

| Flag | Purpose |
| :--- | :--- |
| `--scenarios voip,music_low` | Restrict to specific scenarios (default: all) |
| `--coverage N` | Sample N% of each scenario's clips (deterministic stride) |
| `--gate` | Use the small fixed gate subset for ~30s iteration (see below) |
| `--include-tests` / `--exclude-tests` | Filename globs to include/exclude |
| `--extra-args "--tns"` | Pass extra flags through to the faac encoder |
| `--skip-mos` / `--skip-stereo` | Skip the ViSQOL / stereo-image phases |
| `--backend auto\|visqol-python\|visqol\|docker` | Choose the ViSQOL backend |

### Fast gate subset (`--gate`)

For quick iteration, `--gate` runs a small, fixed, reproducible set of clips per
scenario (`config.GATE_CLIPS`) curated to span the strata that matter (percussive
vs tonal music; chop/noise/echo speech). Scenarios without a curated list fall
back to a deterministic even-spaced slice, so `--gate` always works. Use the full
run (or `--coverage 100`) only for the final check.

## A/B comparison (`--compare`)

Encode the same corpus two ways and get a ranked per-clip diff automatically:

```bash
python3 run_benchmark.py <faac> <lib> ab out.json \
    --gate --compare "lc:--object-type lc" "he:--object-type he-aac"
```

Each `TAG:--args` becomes its own tagged run; after the second run a
`compare_clips` table prints the per-scenario MOS delta, wins/losses and the
worst/best clips.

## Parameter sweeps (`--sweep`)

Sweep an **encoder parameter** over a list of values, one tagged run per value,
each auto-diffed against the first:

```bash
# faac CLI flag:
python3 run_benchmark.py <faac> <lib> sw out.json --gate --sweep "--pns=0,2,4"
# environment variable (for instrumented builds with tuning hooks):
python3 run_benchmark.py <faac> <lib> sw out.json --gate --sweep "FAAC_SBR_Q=0,6"
```

Bitrate is **not** sweepable — it defines a scenario's identity (`music_low` is
64 kbps), so sweeping `-b` would mislabel results. To study a bitrate range, add
a scenario at that rate in `config.py` (see [benchmarking.md](benchmarking.md)).

## Diagnostic tools

```bash
# Ranked per-clip diff of two result JSONs (also: run_benchmark.py --diff a b)
python3 compare_clips.py base.json cand.json

# ...with per-band log-spectral distortion for the worst regressors
python3 compare_clips.py base.json cand.json --bands [--bands-top N]

# One-shot perceptual score of a single encode vs its reference
python3 score_clip.py reference.wav encoded.aac --mode audio|speech

# Per-band distortion of one encode (locates *where* in the spectrum loss is)
python3 band_diag.py reference.wav encoded.aac --mode audio
```

`--bands` / `band_diag.py` report RMS log-spectral error in fixed bands
(0–4k, 4–8k, 8–12k, 12–18.4k, 18.4–24k). The 8–12k / 12–18.4k split corresponds
to an HE-AAC half-rate core's top octave vs the SBR band, which is how the
HE-AAC percussive loss was localized to the core rather than SBR.
