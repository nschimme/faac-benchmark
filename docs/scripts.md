# Ad Hoc / Diagnostic Scripts

## Pinned TNS comparison

[`tns_evaluation.py`](../scripts/tns_evaluation.py) runs the established encoder comparison and records MOS, achieved bitrate, stereo, attack, and pre-echo measurements on hash-pinned LC inputs. See the [TNS evaluation guide](tns-evaluation/README.md) for setup, the checked-in 77-clip manifest, the command, provenance format, and evidence limits. The example evidence snapshot is FAAC baseline/candidate only; rerun on macOS to include Apple AAC and provide an `fdkaac` binary for FDK.

Everything in `scripts/` is local tooling for investigating a specific
question — pre-echo behavior, spectral distortion, tuning-knob A/B tests. None
of it is invoked by CI (`.github/workflows/`, `action.yml`, `report/action.yml`);
the CI-critical pipeline is `run_benchmark.py`, `render_job_summary.py`,
`compare_codecs.py`, `compare_results.py`, and the `phase*.py` files at the
repo root — see [usage.md](usage.md) and [ci.md](ci.md) for those.

Run everything below from the repo root (so `data/external/...` and `config`/
`utils` imports resolve); each script's own `--help` has the full flag list.

## Per-clip / per-band diagnostics

### `scripts/compare_clips.py`

Ranked per-clip diff of two `run_benchmark.py` result JSONs — per-scenario MOS
delta, wins/losses, worst/best clips, and a **bits-adjusted MOS delta** that
charges each result for the bitrate change that produced it (see
[metrics.md](metrics.md#bits-adjusted-mos-delta) — this is what separates
"allocates better" from "spent more"). Also invoked automatically by
`run_benchmark.py --compare`/`--sweep` (after each additional run) and
`run_benchmark.py --diff a.json b.json`.

```bash
python3 scripts/compare_clips.py base.json cand.json
python3 scripts/compare_clips.py base.json cand.json --bands [--bands-top N]
```

### `scripts/bd_rate.py`

Bjontegaard-delta rate between two `run_benchmark.py` result JSONs: how many
more/fewer bits the candidate needs for equal quality, fit per clip over a
bitrate ladder rather than read off a fixed `-b` delta (see
[metrics.md](metrics.md#bd-rate) for why — this is the metric rate-control
work is gated on). Positive = candidate needs more bits = worse. Segments by
`(corpus, object_type)` and needs ≥4 rungs at one object type to fit a
ladder; mono families (2 rungs) aren't covered and still use
`compare_clips.py`'s bits-adjusted MOS delta.

```bash
python3 scripts/bd_rate.py base.json cand.json
python3 scripts/bd_rate.py base.json --self-check   # BD-rate of a run against itself; must report 0
```

### `scripts/band_diag.py`

Per-band log-spectral distortion (RMS dB) of one encode vs its reference, in
fixed bands (0–4k, 4–8k, 8–12k, 12–18.4k, 18.4–24k). The 8–12k / 12–18.4k split
maps to an HE-AAC half-rate core's top octave vs its SBR band — this is the
tool that localized the HE-AAC percussive loss to the core, not SBR.

```bash
python3 scripts/band_diag.py reference.wav encoded.aac --mode audio|speech
```

### `scripts/score_clip.py`

One-shot MOS of a single encode vs its reference (no scenario matrix, no run
bookkeeping — just a quick number). Reuses `phase2_mos.py`'s scoring logic
(`score_wav_pair`), automatically routing speech to `visqol-python` and audio to
`Zimtohrli`.

```bash
python3 scripts/score_clip.py reference.wav encoded.aac --mode audio|speech
```

### `scripts/winseq.py`

Histogram of the `window_sequence` each ADTS frame actually carries (first
SCE/CPE), or one line per frame with `--frames`. Use it instead of the
encoder's `FAAC_STATS` block-type counters when the question is what reached
the bitstream; it parses past the SCE `global_gain` and a CPE without
`common_window`, which precede `ics_info`.

```bash
python3 scripts/winseq.py out.aac [more.aac ...]
python3 scripts/winseq.py --frames out.aac
```

## Full-CLI decoder A/B timing

`scripts/decoder_ab.py` compares two FAAD executables on the original AAC
inputs, after requiring matching PCM formats and byte-identical audio payloads
across RIFF and RF64 WAV output at 16-bit integer and
32-bit float. It uses the shared stream-copy preparation and one-decode timing
primitives from `codec_bench.decoders`; each input gets one warmup per decoder
followed by serial, rotating decoder order for every measured round. The
default workload is 600 seconds and seven rounds. The JSON retains executable
and resolved `libfaad` hashes, preparation metadata, commands, every timed
attempt, medians and spreads, and per-round paired speed changes. A control
decoder is optional and serves as a run-to-run noise reference; the report
does not claim statistical significance.

```bash
python3 scripts/decoder_ab.py \
  --baseline /path/to/base/faad \
  --candidate /path/to/candidate/faad \
  --control /path/to/control/faad \
  --inputs data/external/audio/clip-a.wav data/external/audio/clip-b.wav \
  --iterations 7 --audio-seconds 600 --bits 16 32f \
  --json /tmp/faad-decoder-ab.json
```

Use `--baseline-lib`, `--candidate-lib`, and `--control-lib` when the intended
`libfaad` cannot be resolved from the executable's linked libraries. The
preflight decodes each original input before the timing workload is prepared;
WAV headers and ancillary chunks may differ; any PCM or format mismatch stops the timing run.

The helper's unit tests run with `python3 -m unittest discover -s tests -p test_decoder_ab.py`.

## Decoder output-filesystem A/B timing

`scripts/storage_ab.py` compares full-CLI FAAD output to several existing
filesystem roots. It copies every original input into local scratch before
using the shared stream-copy timing preparation, so even an input longer than
`--audio-seconds` is decoded from local storage. Each `--dest LABEL=DIR` must
name an existing directory; the script creates a unique temporary child there
and removes only that child when the run ends. It refuses to overwrite an
existing `--json` report.

Each destination gets one warmup, then serial trials with rotating destination
order. The CLI wall time and direct child user+system CPU time are recorded;
WAV format, PCM byte count and PCM SHA-256 are inspected after timing and must
match across file destinations. Peak RSS is not collected by this direct
subprocess timer. `--include-null` adds a separately labeled raw PCM decode to
`os.devnull`; it discards WAV output and is not compared as a file destination.

```bash
python3 scripts/storage_ab.py \
  --faad build/frontend/faad \
  --faad-lib build/libfaad/libfaad3.dylib \
  --inputs /path/to/clip-a.m4a /path/to/clip-b.m4a \
  --dest local=/private/tmp \
  --dest wd_black=/Volumes/WD_BLACK/faad-bench-scratch \
  --dest old_nas=/Volumes/scratch/output \
  --iterations 5 --audio-seconds 300 \
  --include-null --json /private/tmp/faad-storage-ab.json
```

The report includes executable/library/input hashes, source and prepared-input
paths, destination filesystem paths, preparation details, warmups, every raw
sample, per-destination medians and failures. This is a serial filesystem
diagnostic. It does not reproduce the leaderboard's ten-worker contention, and
the raw `/dev/null` measurement answers a different output-path question from
the WAV-writing full-CLI timing.

The argument, timer, copy, PCM-mismatch, failure-report and cleanup checks run
with `python3 -m unittest discover -s tests -p test_storage_ab.py`.

## Transient fidelity / TNS tooling

For the reference-selected pre-echo metric, result-field semantics, resume
backfill commands, and comparison caveats, see [Transient measurements](transients.md).

### `scripts/score_transient.py`

Three complementary transient-fidelity metrics for TNS/block-switch
evaluation, and the shared A/B/sweep helper library (`encode_aac`,
`decode_aac`, `load_mono`, `find_lag`, `align_signals`, `zimtohrli_mos`,
`bootstrap_ci`, `cmd_env_ab`, `require_tuning_build`, ...) that
`scripts/sweep_binary_ab.py` and `scripts/visqol_env_ab.py` build on:

This script's NPER implementation is a legacy metric. The versioned pre-echo
metric used by `compare_codecs.py` and the encoder leaderboard is implemented
in the top-level `transient.py` and has different onset selection and scoring
rules. Treat their measurements as separate series; do not compare or combine
their NPER values.

- **NPER** — looks *backward* from a detected onset: energy leaking into
  the silence before a transient (pre-echo).
- **attack-centroid-shift** — looks *forward* from the same onset: the shift
  in energy-weighted temporal centroid within a fixed post-onset window,
  decoded vs. reference (positive ⇒ decoded energy arrives later, i.e.
  smeared). Validated: exact ref-vs-ref self-consistency, near-total yield
  on real transient-heavy material, and per-bitrate |r| vs NPER mostly
  under 0.3 across four clips — see the module comment above
  `compute_attack_centroid_shift()`. Exposed via `--metric attack_centroid`
  for `--tns-ab`; not yet wired into `env_ab_one`/`--env-ab`.
- **attack-smear** — an earlier, *crossing*-based attempt at the same
  forward-looking question (rise time from 10% to 90% of local peak, in the
  spirit of the MPEG-7 "Log Attack Time" descriptor). **Documented NO-GO,
  kept only as a negative result**: it requires a quiet gap before the
  onset to locate a clean 10% start point, which real percussive/dense
  material routinely doesn't have — real yield was 0-2/18 onsets on
  glockenspiel and 0/28-29 on sandman even after fixing an unrelated
  windowing bug. See the module comment above `compute_attack_smear()` for
  the full investigation. Still exposed via `--metric attack_smear` for
  reference, but do not build on it.

The encoder leaderboard reports versioned **Pre-echo (dB)** separately from
MOS and Attack Fidelity. It uses the high-pass-2-kHz NPER metric on
reference-only fine onsets, with a 6-to-1 ms pre-window and a 0-to-12 ms
post-window anchored to the refined reference onset. Only onsets whose
reference pre/post ratio is at most -20 dB are eligible; the energy floor is
-80 dB. Per-channel measurements use a shared global alignment for each
recording, and the report pools scored onsets. It reports mean and 95th
percentile positive excess, event coverage (scored/eligible), and clip
coverage, so missing or unscorable clips are not presented as perfect scores.
Dense audio can have no eligible quiet-prewindow onsets; those cases show N/A
with zero coverage. The metric is a diagnostic, not a calibrated audibility
score, and positive-excess mean is not an audibility threshold. It measures
pre-onset energy; it does not measure attack timing. It does not affect the
existing MOS-based ranking. Legacy rows without the current metric version are
shown as N/A and are excluded from aggregates.

Attenuation or high-frequency removal can also reduce measured NPER without
improving fidelity. Weigh it alongside MOS, stereo fidelity, and Attack
Fidelity, and compare only with matched scored-event and clip coverage. An
all-zero score or a low-coverage score is not a win by itself.

```bash
scripts/score_transient.py REF.wav DEC.wav [--verbose]        # score a pair (all three metrics)
scripts/score_transient.py --validate FAAC_BIN                # synthetic self-tests (all three metrics)
scripts/score_transient.py --sweep FAAC_BIN REF.wav            # all three metrics across bitrates + correlation
scripts/score_transient.py --tns-ab FAAC_BIN REF.wav [REF2...] # paired TNS on/off proof
scripts/score_transient.py --env-ab FAAC_BIN --env-a "K=V" --env-b "K=V" REF.wav [...]
```

### `scripts/sweep_binary_ab.py`

Local A/B sweep tool, two modes:

- **Two-binary** (default): same env, different `faac` binaries (e.g. master
  vs a branch), zimtohrli MOS per clip/bitrate, forces `--object-type=lc`.
  ```bash
  python3 scripts/sweep_binary_ab.py path/to/faac_a path/to/faac_b [clip.wav ...] \
      [--bitrates 20,32,48,64,96,128]
  ```
- **Env-var sweep** (`--env-var`): one binary, sweeps a plain-`getenv()` knob
  (e.g. `FAAC_TD_THRESH`, `FAAC_TNS_DIR`) across `--values`, each vs the first
  value as baseline, via `score_transient.cmd_env_ab` (bootstrap CI, byte delta,
  short-block %). An empty/`unset` value means "don't set the var at all".
  ```bash
  python3 scripts/sweep_binary_ab.py path/to/faac --env-var FAAC_TD_THRESH \
      --values 0.5,0.7,1.0,1.5,2.0,4.0 --bitrates 20,32,48 --metric zimtohrli
  python3 scripts/sweep_binary_ab.py path/to/faac --env-var FAAC_TNS_DIR \
      --values 0,unset --bitrates 20,32,48
  ```
  Absorbs what were previously three separate scripts (`tns_gate_ab.py`,
  `sweep_td_hard.py`, `sweep_tns_dir.py`) that duplicated this same A/B loop
  with only the bitrate ladder or env knob changed.

### `scripts/visqol_env_ab.py`

Targeted ViSQOL (audio mode) paired A/B over encoder env configs, for levers
where zimtohrli disagrees with ViSQOL (e.g. block-switch promotion). Scores
the CI's own metric on specific clips instead of running the full gate.

```bash
.venv/bin/python scripts/visqol_env_ab.py --faac BIN --env-a "K=V" [--env-b "K=V"] \
    --bitrates 12,16,24,32,48 [--reps 3] clip1.wav clip2.wav ...
```

## Sweep-result comparison

### `scripts/cmp_sweep.py`

Compares a set of sweep-output JSONs (e.g. from repeated
`run_benchmark.py --sweep "KEY=V"` runs, or hand-run `score_transient.py`/
`sweep_binary_ab.py --env-var` dumps) against the first value as baseline:
per-scenario net MOS delta, bitrate delta, changed-md5 count, and the worst
clip per scenario.

```bash
python3 scripts/cmp_sweep.py out_leverD FAAC_TNS_COEFF_THRESH 1.0,1.5,2.0
# loads out_leverD_FAAC_TNS_COEFF_THRESH1.0.json, ...1.5.json, ...2.0.json
```

## Calibration

### `scripts/validate_scenarios.py`

Checks that every scenario asks for a bitrate its content can actually carry:
encodes each scenario's gate clips and reports achieved-vs-target, exiting
non-zero on anything outside the tolerance (default ±15%). Two exemption lists
are reported but never fail: the documented 48 kHz VBR dead zone, and
`PENDING_UPSTREAM` — scenarios that are out of range only because of a known
encoder defect with a fix in flight, each entry naming the fix. **Run it before adding or retuning a scenario** —
a target the format cannot reach becomes a permanent accuracy deficit in every
report that no code change can fix, which is why `16k_mono_40k` was retired.

On a stock LC-only faac the HE-AAC-targeted stereo scenarios (24-56 kbps) read
as large overshoots; that is a property of the binary, not the scenario. The
script prints which binary it used.

```bash
python3 scripts/validate_scenarios.py [--scenarios NAME|FAMILY,...] \
    [--rate-control abr|vbr] [--tolerance 15] [--faac-bin PATH]
```

### `scripts/calibrate_vbr_q.py`

Regenerates `config.py`'s per-scenario `vbr_q` table: grid-searches faac's
`-q` so VBR output lands near each scenario's nominal `bitrate` for
representative content (see [metrics.md](metrics.md) for why this needs a
search rather than a linear formula). Prints a table and a ready-to-paste
`vbr_q` dict; does not edit `config.py` itself.

**Do not re-run this as routine maintenance after a libfaac change.** In VBR
mode the regression signal *is* the bitrate delta at a fixed `-q`: base and
candidate encode at the same `vbr_q`, so a shift in the quantizer curve shows
up in the report, and recalibrating afterwards is the one action that erases
it. Treat `vbr_q` like `bitrate` — a constant of the scenario, pinned to the
reference build recorded in `config.py`. Recalibrating is a deliberate
rebaseline (the same class of change as moving `thresh`), warranted only once
VBR has drifted so far that the run no longer measures the intended operating
point, and never in the same run as a comparison.

```bash
python3 scripts/calibrate_vbr_q.py [--scenarios NAME|FAMILY,...]
```

## Build/perf investigations

### `scripts/fft_bench.sh`

Reproducible size + timing + byte-identity harness for MDCT/FFT work in the
`faac` source repo (not this repo's data): builds a baseline git ref (in a
disposable worktree) against the current working tree with matching meson
options, and reports library size, best-of-3 encode timing, and byte-identity
over a corpus.

```bash
scripts/fft_bench.sh [-r BASE_REF] [-b BITRATE] [-n ITERS] [-p PRECISION] \
    [-f FFT] [-c CORPUS_GLOB] [-S]
```

### `scripts/amd64_denormal_perf_test.sh`

One-off investigation script (kept for reference) that checks out three
specific `faac` commits, builds each as a single-precision build, and times
batch-encodes to confirm/refute an x86-only denormal-float throughput
regression and its FTZ/DAZ fix. See the script's header comment for the full
history (nschimme/faac PR #319). Must run on real x86_64 hardware.

```bash
./scripts/amd64_denormal_perf_test.sh [path-to-wav-corpus-dir] [faac-repo-url]
```

### `scripts/corrupt_decode_check.py`

Decodes deliberately corrupted AAC streams and counts crashes and hangs. It
applies the robustness pass's corruption (`utils.corrupt_adts_bitstream`:
dropped frames and bit flips) with a fixed seed per run, then runs the decoder
on the result. A decoder that rejects the stream and exits non-zero is fine; a
signal, a sanitizer report, or a run past the timeout is a failure. Exits 1 if
anything failed.

The decoder is run as `DECODER corrupted.aac out.wav`, the command line of the
Helix wrapper built by `scripts/build_helix_aac.sh`. Inputs must be ADTS
(`ffmpeg -i in.m4a -c:a copy -f adts in.aac`).

```bash
python3 scripts/corrupt_decode_check.py --decoder bin/helix-aac-dec \
    [--seeds 40] [--timeout 20] in1.aac in2.aac ...
```

## Decoder timing protocol

`compare_codecs.py` retains its concurrent timing default (`--speed-workers 0`,
all available cores). Use `--speed-workers 1` for an isolated timing pass and
`--speed-audio-seconds 600` to extend the timing workload. Current leaderboards
use median latency after a separate warmup; results retain the minimum and every
raw latency in `speed_timing`. Reports label concurrent timing, which includes
contention. Old result files remain readable using their minimum latency.
The dedicated speed pass is remeasured even with `--resume`; quality results
can be reused, but cached baseline timing is not mixed with fresh candidate timing.

For loader-setting executable wrappers, supply explicit `--baseline-lib`,
`--candidate-lib` and (when used) `--control-lib`: the helper hashes the wrapper
and target executable, but cannot infer arbitrary wrapper loader settings.
Automatic workload calibration is not implemented; choose the same explicit
audio duration for all compared builds and inspect actual elapsed samples.

RIFF/RF64 inspection is shared by decoder A/B preflight, throughput byte counts,
and Phase 5 PCM comparisons. PCM hashes are streamed in 1 MiB blocks.

## dabplus_960/

DAB+ (960-sample AAC, HE-AAC v1/v2) conformance probe for libfaad3 against FDK,
FFmpeg and FAAD2. `build_odr_fdk.sh` builds the fdk-aac fork that can encode a
960-sample granule; `run_matrix.sh` encodes and compares a matrix of rates and
channel layouts. See `scripts/dabplus_960/README.md`.

## find_duplicate_clips.py

Finds clips in the FLAC listening-test sets (IgorCPBLT2020, Kamedo2PLT2012)
that are the same recording as a PMLT2014/SoundExpert clip, by content
(8 kHz mono cross-correlation; true duplicates score 0.89-1.00, everything else
< 0.25). It compares against the raw dataset sources and writes the committed
`duplicate_clips.json`, which `setup_datasets.py` reads. Re-run it when bumping a
dataset tag. A hand-edited `action` is preserved.

Each duplicate is `replace` (default: the lossless FLAC supersedes the older
clip, which is left out of every corpus and removed from an existing checkout on
rebuild, so the recording is scored once) or `skip` (the FLAC is left out). Run from the repo root:
`python3 scripts/find_duplicate_clips.py`.
