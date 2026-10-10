# Ad Hoc / Diagnostic Scripts

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

## Transient fidelity / TNS tooling

### `scripts/score_transient.py`

Three complementary transient-fidelity metrics for TNS/block-switch
evaluation, and the shared A/B/sweep helper library (`encode_aac`,
`decode_aac`, `load_mono`, `find_lag`, `align_signals`, `zimtohrli_mos`,
`bootstrap_ci`, `cmd_env_ab`, `require_tuning_build`, ...) that
`scripts/sweep_binary_ab.py` and `scripts/visqol_env_ab.py` build on:

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

```bash
scripts/score_transient.py REF.wav DEC.wav [--verbose]        # score a pair (all three metrics)
scripts/score_transient.py --validate FAAC_BIN                # synthetic self-tests (all three metrics)
scripts/score_transient.py --sweep FAAC_BIN REF.wav            # all three metrics across bitrates + correlation
scripts/score_transient.py --tns-ab FAAC_BIN REF.wav [REF2...] # paired TNS on/off proof
scripts/score_transient.py --env-ab FAAC_BIN --env-a "K=V" --env-b "K=V" REF.wav [...]
```

Note: `faac` builds encountered during this work produced byte-identical
TNS on/off output at every tested bitrate (confirmed outside the harness) —
per repo maintainer, TNS doesn't reliably engage in `faac`. Use
`--tns-ab --encoder ffmpeg` (ffmpeg's AAC encoder does toggle TNS) for a
real TNS A/B proof.

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

## On-device decoder benchmark

Results, methodology and tested versions are in [esp32.md](esp32.md).

### `scripts/esp32/esp32_dec_bench.py`

Decode speed, memory and flash cost of AAC decoders running on real ESP32
boards: libhelix (fixed point) vs FAAD2 (`FIXED_POINT`). Other decoders (FAAD3,
FAAC's own) plug in by adding an entry to the `CODECS` table in
`esp32_dec_bench.py` plus an adapter under `scripts/esp32/firmware_dec/` (see its
`README.md` for the build switches and the serial protocol). Decoder sources
are referenced in place, not copied.

Prerequisite: a pinned ESP-IDF (v5.5.5) installed under
`scripts/esp32/.toolchain`, never system-wide.

```bash
scripts/esp32/setup_idf.sh                                    # once
python3 scripts/esp32/esp32_dec_bench.py build  --target esp32s3 [--codec helix,faad2] [--sbr 0|1]
python3 scripts/esp32/esp32_dec_bench.py run    --target esp32s3 --port /dev/serial/by-id/... \
    [--gate] [--scenarios A,B] [--profiles lc,he] [--limit N] [--loops 3] [--verify N] [--stream auto|on|off] \
    [--bitstreams comparison_results.json] [--encoder faac]
python3 scripts/esp32/esp32_dec_bench.py report --target esp32s3 [--output summary.md]
```

`build` compiles one firmware per codec and records each codec archive's flash
footprint from the IDF map. `run` flashes each codec in turn and sends it the
bitstreams; `report` prints a markdown table (and writes it with `--output`).
`--target` is `esp32`, `esp32s3` (default) or `esp32c6`; `--sbr 0` builds
AAC-LC only.

What is measured, per clip:

- **Cycles** — best of `--loops` passes (default 3) of the CPU cycle count
  around the decode calls only; the decoder is closed and reopened between
  passes outside the timed region. Reported as x real time and as MHz needed
  for 1x real time.
- **Heap and stack** — internal-heap high-water use and main-task stack use.
- **Flash footprint** — the codec adapter's archive (text + rodata + data) from
  the IDF map.
- **SNR vs ffmpeg** (optional) — with `--verify N` the device returns its PCM
  for every Nth clip and it is compared with ffmpeg's decode, searching
  whole-frame alignment offsets. Slow (PCM crosses the serial link), so off by
  default.

Bitstreams come from the encoder-phase results JSON (`comparison_results.json`
by default): one per (scenario, clip, profile) of the chosen `--encoder`,
converted from M4A to raw ADTS with ffmpeg. `--gate` restricts to the fixed
gate clips. Clips larger than the target can hold are skipped.

Results go to `results/esp32_dec_<target>.json`. `run` saves after every clip and
skips clips already recorded with status 0 for that codec, so an interrupted
run resumes by re-running the same command; delete the file (or the rows) to
re-measure. `report` only compares bitstreams that every codec decoded.

Caveats:

- Only the S3 is a benchmark target. The ESP32 and C6 have too little RAM for
  the larger clips and exist as smoke tests of the firmware.
- Boards are reached through their USB-UART bridges on UART0 at 921600 baud;
  pass that bridge's port. Don't run `idf.py monitor` while the harness owns
  the port.
- FAAD2 builds with SBR keep PS enabled: fixed point plus `SBR_LOW_POWER` does
  not compile.
- FAAD2 needs about 46 KB of stack.
- Run from the repo root. Needs `ffmpeg` on `PATH`, and `numpy` for `--verify`.

### `scripts/esp32/esp32_dec_profile.py`

Per-function cycle profile of FAAD3 on a board: builds the FAAD3 adapter with `-finstrument-functions`
(`CODEC_PROFILE=1`, a separate `build/faad3prof-<target>` directory), flashes it, decodes one ADTS clip for a
single pass and prints exclusive cycles per function. Hook cost (about 50 cycles per call) lands in the callee, so
trust the ranking of the large functions only. The clip must fit the chip's internal RAM. Results are in
[esp32.md](esp32.md#where-faad3-spends-its-cycles-profile).

```
python3 scripts/esp32/esp32_dec_profile.py --target esp32 --port /dev/ttyUSB0 clip.aac [--no-build]
```

### `scripts/esp32/esp32_enc_bench.py` (work in progress)

On-device FAAC encoder benchmark (LC and HE-AAC v1): xRT, MHz for 1x RT, bitrate accuracy, heap, stack and
flash on ESP32 / S3 / C6, with the bitstream checked and decoded by ffmpeg. Separate firmware
(`scripts/esp32/firmware_enc/`) and results (`results/esp32_enc_<target>.json`) from the decoder tool;
usage and smoke numbers are in [esp32.md](esp32.md#faac-encoder-work-in-progress), tests in
`tests/test_esp32_enc_bench.py`.
