# Benchmarking Guide

## Scenarios

Scenarios are defined in `config.py` (`SCENARIOS`). Each fixes a **mode**
(speech/audio), a sample rate, a **bitrate**, and a quality threshold. The
bitrate is part of the scenario's identity, so studying a different bitrate
means adding a scenario, not overriding `-b`.

| Scenario | Mode | Bitrate | Per-channel | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `voip` | speech | 16k | 16k mono | telephony |
| `vss` | speech | 40k | 40k mono | wideband speech |
| `music_40` | audio | 40k | 20k/ch | low-rate music |
| `music_48` | audio | 48k | 24k/ch | low-rate music |
| `music_low` | audio | 64k | 32k/ch | |
| `music_std` | audio | 128k | 64k/ch | |
| `music_high` | audio | 256k | 128k/ch | transparency |

`music_40` / `music_48` are named **by rate, not codec**. While HE-AAC is not
auto-engaged in faac they run as pure LC (valid low-rate LC tests); once faac's
auto-mode picks HE at those per-channel rates, the same scenario becomes the
HE-vs-LC comparison at the bitrates where HE-AAC v1 is designed to win — no
benchmark change required.

Music clips live in `data/external/audio/`, speech in `data/external/speech/`,
throughput stimuli in `data/external/throughput/`.

## Filtering and sampling

* `--scenarios a,b` — run only these scenarios.
* `--include-tests`, `--exclude-tests` — filename globs.
* `--coverage N` — deterministic N% stride sample of each scenario.
* `--gate` — the fixed fast subset (`config.GATE_CLIPS`); ignores `--coverage`.

## A/B mode

`--compare "TAG:--args" ...` encodes the corpus once per arg-set and prints a
ranked per-clip diff (first run is the baseline). Example: `lc` vs `he`.

## Sweeps

`--sweep "KEY=v1,v2,..."` runs one tagged encode per value, each auto-diffed
against the first value:

* `KEY` starting with `-` is a **faac CLI flag** (e.g. `--pns=0,2,4`).
* otherwise `KEY` is an **environment variable** (for builds with tuning hooks).
* `-b` / `--bitrate` / `-q` are **rejected** — use a scenario instead.

## Reproducibility: provenance & decode validation

* Each encoded clip records a **provenance hash** of `(faac binary, libfaac.so,
  faac args, FAAC_* env, input file)`. Phase 2 refuses to reuse a cached MOS
  whose hash no longer matches — so a stale `.aac` can never be silently
  re-scored. Pass `--faac-bin`/`--lib-path` (run_benchmark does this
  automatically) to enable the check.
* Each encode is **decode-validated** with ffmpeg. ffmpeg exits 0 even on hard
  decoder errors (e.g. a corrupt SBR payload), so validation also treats any
  `-v error` stderr as a failure. Failures are recorded per clip
  (`decode_error`) and counted in the report. See [ci.md](ci.md) for
  `--strict-decode`.
