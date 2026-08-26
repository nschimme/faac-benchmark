# Ad Hoc / Diagnostic Scripts

Everything in `scripts/` is local tooling for investigating a specific
question — pre-echo behavior, spectral distortion, tuning-knob A/B tests. None
of it is invoked by CI (`.github/workflows/`, `action.yml`, `report/action.yml`);
the CI-critical pipeline is `run_benchmark.py`, `compare_encoders.py`,
`compare_results.py`, and the `phase*.py` files at the repo root — see
[usage.md](usage.md) and [ci.md](ci.md) for those.

Run everything below from the repo root (so `data/external/...` and `config`/
`utils` imports resolve); each script's own `--help` has the full flag list.

## Per-clip / per-band diagnostics

### `scripts/compare_clips.py`

Ranked per-clip diff of two `run_benchmark.py` result JSONs — per-scenario MOS
delta, wins/losses, worst/best clips. Also invoked automatically by
`run_benchmark.py --compare`/`--sweep` (after each additional run) and
`run_benchmark.py --diff a.json b.json`.

```bash
python3 scripts/compare_clips.py base.json cand.json
python3 scripts/compare_clips.py base.json cand.json --bands [--bands-top N]
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
bookkeeping — just a quick number). Reuses `phase2_mos.py`'s backend-selection
logic (`score_wav_pair`), so `--backend auto` (default) prefers zimtohrli
exactly like `run_benchmark.py`.

```bash
python3 scripts/score_clip.py reference.wav encoded.aac --mode audio|speech \
    [--backend auto|zimtohrli|visqol|visqol-py|visqol-python]
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

### `scripts/calibrate_vbr_q.py`

Regenerates `config.py`'s per-scenario `vbr_q` table: grid-searches faac's
`-q` so VBR output lands near each scenario's nominal `bitrate` for
representative content (see [metrics.md](metrics.md) for why this needs a
search rather than a linear formula). Prints a table and a ready-to-paste
`vbr_q` dict; does not edit `config.py` itself. Re-run after any libfaac
change that could shift its quantizer/bitrate curve.

```bash
python3 scripts/calibrate_vbr_q.py [--scenarios NAME,...]
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
