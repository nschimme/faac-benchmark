# Local Usage

Run benchmarks and diagnostics on your own builds. The suite always compares
**a candidate build against a baseline build** — point it at the exact `faac`
binary and `libfaac.so` you want to test (not a system package), so results
reflect your code and the provenance hashing stays meaningful.

## 1. Install dependencies

```bash
# System (Ubuntu/Debian)
sudo apt-get update && sudo apt-get install -y meson ninja-build bc faad ffmpeg

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

A bare `<output.json>` (no directory component, e.g. `test.json`) is written
under `results/` (gitignored) instead of the repo root; pass a path with a
directory (e.g. `./results/test.json` or `./out/test.json`) to control it
explicitly.

Common options:

| Flag | Purpose |
| :--- | :--- |
| `--rate-control abr\|vbr` | Choose rate control mode (`abr` using `-b` bitrates, or `vbr` using `-q` quality targets) |
| `--scenarios 16k_mono_16k,48k_stereo_64k` | Restrict to specific scenarios (default: all) |
| `--coverage N` | Sample N% of each scenario's clips (deterministic stride) |
| `--gate` | Use the small fixed gate subset for ~30s iteration (see below) |
| `--include-tests` / `--exclude-tests` | Filename globs to include/exclude |
| `--extra-args "--tns"` | Pass extra flags through to the faac encoder |
| `--skip-mos` / `--skip-stereo` | Skip the perceptual MOS / stereo-image phases |
| `--sha $(git rev-parse HEAD)` | Stamp results with a commit SHA |

The script runs three phases:

1. **Phase 1** — encodes samples, measures throughput (via deterministic Cachegrind instruction counts when `valgrind` is installed, or wall-clock timing fallback), library size, and decode-validates each encode (supporting ABR `-b` or VBR `-q` modes).
2. **Phase 2** — perceptual quality (MOS) automatically evaluated via `visqol-python` (for speech/16kHz scenarios) or `Zimtohrli` (for audio scenarios).
3. **Phase 3** — stereo image fidelity (inter-channel coherence error), so joint
   stereo doesn't silently degrade the stereo image.

### Filtering tests and scenarios

```bash
python3 run_benchmark.py ... --scenarios 48k_stereo_64k,48k_stereo_128k
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

Bitrate is **not** sweepable — it defines a scenario's identity (`48k_stereo_64k` is
64 kbps), so sweeping `-b` would mislabel results. To study a bitrate range, add
a scenario at that rate in `config.py` (see [benchmarking.md](benchmarking.md)).

## Cross-Encoder Comparison (`compare_encoders.py`)

Benchmark `faac` against other available AAC encoders (FDK-AAC, FFmpeg internal, etc.) to generate a competitive leaderboard. Every encoder is compared at the same target bitrate; there is no VBR/quality-knob comparison mode here, since each encoder's own quality scale (FAAC's `-q`, FDK-AAC's `-vbr`, etc.) isn't calibrated against any other's.

```bash
python3 compare_encoders.py [options]
```

Options:
- `--gate`: Use the small fixed gate subset (recommended for quick checks).
- `--skip-mos`: Skip perceptual quality (MOS) calculation.
- `--faac-bin`, `--fdkaac-bin`, `--ffmpeg-bin`, `--aac-enc-bin`, `--falabaac-bin`, `--afconvert-bin`: Manual paths to encoder binaries.
- `--output <file.md>`: Path to write the Markdown leaderboard (default: `leaderboard.md`).

The leaderboard evaluates the **Golden Triangle**:
1. **Quality**: Average and Worst MOS across scenarios (higher is better).
2. **Fidelity**: Stereo image fidelity via inter-channel coherence fidelity (higher is better).
3. **Efficiency**: Average encoding speed as a multiple of real-time (higher is better).
4. **Footprint**: Combined executable and library size (lower is better).
5. **Accuracy**: Average bitrate error % relative to target (lower is better).

**Winner Highlighting**: The best-performing encoder in each category is **bolded** in the leaderboard tables.

## Diagnostic and ad hoc tools

`run_benchmark.py --compare`/`--sweep`/`--diff` cover the everyday A/B and
sweep workflows above. For per-band spectral diagnostics, pre-echo/TNS A/B
tooling, VBR-q calibration, and other local investigation scripts, see
[scripts.md](scripts.md) — everything under `scripts/` at the repo root.
