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
| `--rate-control abr\|vbr\|cbr` | Choose rate control mode (`abr` using `-b` bitrates, `vbr` using `-q` quality targets, or `cbr` using `-b --cbr`: the bit reservoir holds the rate exactly, so `bias_percent` is a hard target there) |
| `--scenarios 16k_mono_20k,48k_stereo_64k` | Restrict to specific scenarios, or to a whole rate family (`--scenarios 44k1_stereo`); default: all |
| `--coverage N` | Sample N% of each scenario's clips (deterministic stride) |
| `--gate` | Use the small fixed gate subset for ~30s iteration (see below) |
| `--include-tests` / `--exclude-tests` | Filename globs to include/exclude |
| `--extra-args "--tns"` | Pass extra flags through to the faac encoder |
| `--skip-mos` / `--skip-stereo` | Skip the perceptual MOS / stereo-image phases |
| `--sha $(git rev-parse HEAD)` | Stamp results with a commit SHA |

The script runs three phases:

1. **Phase 1** — encodes samples, measures throughput (via deterministic Cachegrind instruction counts when `valgrind` is installed, or wall-clock timing fallback), library size, and decode-validates each encode (supporting ABR `-b` or VBR `-q` modes).
2. **Phase 2** — perceptual quality (MOS) automatically evaluated via `visqol-python` (for `speech`-mode scenarios) or `Zimtohrli` (for `audio`-mode ones); the engine follows the scenario's mode, not its sample rate.
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
back to a deterministic even-spaced slice, so `--gate` always works. `--gate`
also bypasses a corpus's `max_clips` cap, so a curated clip is never dropped by
the cap before the gate list is applied. Use the full run (or `--coverage 100`)
only for the final check.

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

## Cross-Codec Comparison (`compare_codecs.py`)

Benchmark `faac` and `faad` against other available AAC encoders (FDK-AAC, FFmpeg internal, Apple AudioToolbox, etc.) and decoders (FAAD2, FFmpeg, Apple AudioToolbox) to generate a competitive leaderboard. Encoders and decoders can be evaluated independently or together via `--mode encoder|decoder|both`.

```bash
python3 compare_codecs.py [options]
```

Options:
- `--mode encoder|decoder|both`: Benchmarking mode (default: `both`).
- `--gate`: Use the small fixed gate subset (recommended for quick checks).
- `--skip-mos`: Skip perceptual quality (MOS) calculation.
- `--faac-bin`, `--fdkaac-bin`, `--ffmpeg-bin`, `--faad-bin`, `--afconvert-bin`: Manual paths to encoder/decoder binaries.
- `--output <file.md>`: Path to write the Markdown leaderboard (default: `leaderboard.md`).

The leaderboard evaluates key dimensions:
1. **Quality**: Average and Worst MOS across scenarios (higher is better).
2. **Spec Conformance (Decoders)**: Signal-to-Noise Ratio (SNR in dB) against reference decodes (higher/bit-exact is better).
3. **Timing Alignment (Decoders)**: Sample timing alignment error (in ms) relative to reference audio.
4. **Robustness (Decoders)**: Crash-free decoding success rate (%) on deterministically corrupted ADTS bitstreams.
5. **Fidelity**: Stereo image fidelity via inter-channel coherence fidelity (higher is better).
6. **Efficiency**: Average throughput as a multiple of real-time (higher is better).
7. **RAM & Footprint**: Peak dynamic RAM (Max RSS in KB/MB) and compiled code section size (`.text` + `.rodata` in KB, lower is better).
8. **Accuracy**: Average bitrate error % relative to target (lower is better).

**Winner Highlighting**: The best-performing encoder or decoder in each category is **bolded** in the leaderboard tables.

### Decoder phase: an extension of the encoder phase, not a repeat

The encoder phase already decodes every bitstream once with ffmpeg (to
decode-validate it and score its MOS). The decoder phase reuses that same
cached ffmpeg decode as its **conformance reference** instead of decoding
each bitstream again per tool under test:

- **Conformance SNR** (`conformance_snr_db`) is each decoder's output
  compared against the cached ffmpeg decode of the same bitstream, in-process
  with numpy/scipy -- not a fresh `ffmpeg`/decoder re-run per metric. This
  isolates decoder-implementation bugs from the encoder's own lossy error
  (which `snr_db`, measured against the original uncompressed WAV, still
  captures).
- **MOS inheritance**: a decoder output whose conformance SNR against the
  ffmpeg decode is **>= 60 dB** is treated as perceptually identical to it,
  so it inherits the MOS the encoder phase already computed for that
  bitstream (`mos_source: "inherited"`) instead of paying for a fresh
  Zimtohrli/ViSQOL scoring pass. Only a decoder whose output diverges from
  the ffmpeg decode below that floor (or one with no cached reference) is
  actually rescored (`mos_source: "scored"`).
- **Alignment** (`alignment_delay_ms`, `gapless_offset_samples`) is derived
  by adding the ffmpeg decode's own (once-per-bitstream, cached) offset vs
  the original to each decoder's (much cheaper) offset vs that ffmpeg
  decode, instead of every decoder cross-correlating a full window against
  the original from scratch.
- Decoded WAVs are deleted once their metrics are computed; pass
  `--keep-decodes` to keep them on disk.

The separate **robustness pass** (corrupted-bitstream decode-only, no
scoring) still exits/timeouts/**runaway**-classifies each decoder
independently: a corrupted stream whose decode exceeds 4x the intact
stream's expected PCM size is flagged `runaway` (it didn't fail cleanly, it
kept synthesizing audio past a desynced length field) rather than counted as
a pass.

## Diagnostic and ad hoc tools

`run_benchmark.py --compare`/`--sweep`/`--diff` cover the everyday A/B and
sweep workflows above. For per-band spectral diagnostics, pre-echo/TNS A/B
tooling, VBR-q calibration, and other local investigation scripts, see
[scripts.md](scripts.md) — everything under `scripts/` at the repo root.
