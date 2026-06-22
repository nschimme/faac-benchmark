# Local Usage

Run benchmarks and diagnostics on your own builds. The suite always compares
**a candidate build against a baseline build** — point it at the exact `faac`
binary and `libfaac.so` you want to test (not a system package), so results
reflect your code and the provenance hashing stays meaningful.

## 1. Install dependencies

```bash
# System (Ubuntu/Debian)
sudo apt-get update && sudo apt-get install -y meson ninja-build bc ffmpeg

# Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Prepare datasets

Downloads samples and generates 10-minute synthetic throughput signals (Sine,
Sweep, Noise, Silence).

```bash
python3 setup_datasets.py
```

## 3. Run a benchmark

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
| `--sha $(git rev-parse HEAD)` | Stamp results with a commit SHA |

The script runs three phases:

1. **Phase 1** — encodes samples, measures throughput, library size, and
   decode-validates each encode.
2. **Phase 2** — perceptual quality (MOS) via ViSQOL.
3. **Phase 3** — stereo image fidelity (inter-channel coherence error), so joint
   stereo doesn't silently degrade the stereo image.

### Selecting the ViSQOL backend

In `auto` mode (default) the suite tries, in order: `visqol-python` (preferred),
the `visqol` binary (PATH or `VISQOL_BIN`), Docker/Podman container, then the
legacy `visqol_py`. Force one explicitly:

```bash
python3 run_benchmark.py ... --backend docker   # containerized
python3 run_benchmark.py ... --backend visqol    # local binary
```

### Docker image discovery

When using the container backend, the image
`ghcr.io/nschimme/faac-benchmark-visqol` is resolved deterministically:
1. **Search** locally for the tag matching the current git tag, else a short
   hash of the build files (`Dockerfile.visqol`, etc.).
2. **Pull** that tag from GHCR if not present locally.
3. **Build** locally as a last resort.

Override with `--visqol-image <image>`.

### Filtering tests and scenarios

```bash
python3 run_benchmark.py ... --scenarios music_low,music_std
python3 run_benchmark.py ... --include-tests "TCD_*"
python3 run_benchmark.py ... --exclude-tests "white_noise.wav"
```

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

## Cross-Encoder Comparison (`compare_encoders.py`)

Benchmark `faac` against other available AAC encoders (FDK-AAC, FFmpeg internal, etc.) to generate a competitive leaderboard.

```bash
python3 compare_encoders.py [options]
```

Options:
- `--gate`: Use the small fixed gate subset (recommended for quick checks).
- `--skip-mos`: Skip perceptual quality (MOS) calculation.
- `--faac-bin`, `--fdkaac-bin`, `--ffmpeg-bin`: Manual paths to encoder binaries.
- `--output <file.md>`: Path to write the Markdown leaderboard (default: `leaderboard.md`).

The leaderboard evaluates the **Golden Triangle**:
1. **Quality**: Average and Worst MOS across scenarios.
2. **Efficiency**: Average encoding speed (xRT).
3. **Footprint**: Executable/Library size.
4. **Accuracy**: Average bitrate error %.

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
