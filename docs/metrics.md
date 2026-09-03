# Metric Definitions

## Perceptual Quality (MOS)

Computed in Phase 2 (`phase2_mos.py`). The engine is selected by the scenario's
**mode alone**: `speech` uses **visqol-python**, `audio` uses **Zimtohrli**
(`zimtohrli.Pyohrli()`), mapping psychoacoustic distance deterministically to a
1.0–5.0 MOS scale (`zimtohrli.mos_from_zimtohrli()`). Zimtohrli is particularly
sensitive to transient preservation, temporal smearing, and pre-echo artifacts.

Dispatch used to also trigger on a 16 kHz sample rate. That was equivalent while
16 kHz meant speech, but it would now hijack any 16 kHz corpus a scenario
deliberately scores in audio mode, so the rate no longer takes part.

**Scoring rate is a property of the engine, not of the content**: ViSQOL speech
mode is 16 kHz mono, Zimtohrli is 48 kHz. That is all `visqol_rate` can ever be
(`config.METRIC_RATE`, derived per scenario and enforced by a unit test), and
phase 2 conforms reference and degraded to it with the same call, so the
comparison is fair whatever the corpus rate is.

The `24k_mono_*` scenarios are mono content scored in **audio** mode for this
reason: ViSQOL speech mode would band-limit the reference to 8 kHz and hide
exactly the bandwidth those scenarios exist to test. Channel count comes from
the corpus, so mono content is not upmixed before scoring.

- **Scale**: 1.0 to 5.0 MOS scale (higher is better).
- **Reported Delta**: **Avg MOS Δ** (candidate − base); positive indicates improvement.
- **Significance**: $|\Delta| \le 0.01$ is treated as noise. $|\Delta| \ge 0.05$ represents a noticeable shift in perceptual quality.

## Stereo Fidelity (Inter-channel Coherence Fidelity)

From phase 3 (`phase3_stereo.py`). MOS is monaural and cannot see stereo-image
damage; this metric tracks inter-channel coherence fidelity.

- **Fidelity**: $1.0 - \text{Error}$, where Error is the deviation from the reference stereo image. $1.0$ means the stereo image is perfectly preserved.
- **Leaderboard**: Reports the raw fidelity value (**higher is truer**).
- **A/B Report**: Reports the signed delta where **positive = candidate improved the fidelity (truer stereo image)**.

## Transient Fidelity (Attack-Centroid-Shift)

From phase 3 (`phase3_stereo.py`, sharing its decode pass; logic in
`transient.py`). Complements MOS/NPER by looking specifically at whether an
attack, once it starts, is preserved or blurred — NPER
(`scripts/score_transient.py`, not yet wired into CI) only looks *backward*
from an onset for pre-echo.

- **Formula**: at each onset, within `[onset, onset+12ms)` (capped at the
  next onset), compute each side's energy-weighted temporal centroid
  ($\sum t \cdot e / \sum e$, $e$ = RMS envelope$^2$) independently, then
  `delta_ms = dec_centroid - ref_centroid`. Positive = decoded energy
  arrives later (smeared).
- **A/B sign convention**: per onset, `|candidate_delta| - |base_delta|`;
  negative = candidate moved closer to the reference (improved).
- **Reported as a verdict, not a number**: pooled at the onset level (not
  per-clip means), via a paired bootstrap 95% CI **and** an exact sign test
  — both must agree or it reads "inconclusive"
  (`transient.ci_signtest_verdict`). Below `MIN_CENTROID_ONSETS` (30) pooled
  onsets the row is omitted rather than shown as false precision.
- **Resolving power (measured, not assumed)**: a 128k-vs-96k bitrate-ladder
  check (~25% bitrate cut) was mostly "inconclusive" at single-clip scale
  (n=28-57 onsets, 3 of 4 clips) but resolved cleanly once pooled across all
  4 gate clips of that one scenario pair (n=148, CI [+0.013, +0.045]ms,
  sign-test p=0.006). This is why the top-line verdict pools at the
  **whole-suite** level, not per clip or per scenario — a default `--gate`
  run (all scenarios) comfortably clears the onset count this needed. A run
  narrowed with `--scenarios` to very few clips may read "inconclusive" on a
  real but modest change; that's the metric being honest about its own
  power, not a bug.
- **Gating**: diagnostic-only, same tier as Stereo Fidelity above.
- **Not the same design as `--metric attack_smear`** in
  `scripts/score_transient.py`, a crossing-based rise-time estimator kept
  there only as a documented negative result (it needs a quiet pre-onset gap
  that dense percussive material doesn't have).

## Throughput Δ

Throughput measures the speed of the encoder on fixed throughput stimuli.
- **Cachegrind Instruction Counts (CI / Default when valgrind is installed)**: Measures exact instructions executed (`I refs`) via `valgrind --tool=cachegrind` on short (~5s) audio clips. This yields ~0.002% deterministic reproducibility across runs, immune to host CPU load/VM scheduling noise.
- **Wall-Clock Timing (Fallback when valgrind is absent)**: Measures single-core wall-clock timing minimum across repetitions on fixed stimuli.
- **Difference from Leaderboard Speed (xRT)**: Throughput Δ evaluates relative candidate-vs-baseline performance on fixed benchmark stimuli in `phase1_encode.py`. The multi-encoder leaderboard (`compare_encoders.py`), by contrast, reports **Speed (xRT)** (realtime factor = audio duration / encode duration) across the full scenario corpus.

## Rate Control: ABR vs. VBR Semantics

The benchmark suite evaluates ABR (Average Bitrate) and VBR (Variable Bitrate) using fundamentally different metrics to maximize Signal-to-Noise Ratio (SNR) and avoid false positives.

### ABR Mode (`-b <bitrate>`)
- **Objective**: Target a fixed target average bitrate across content, utilizing the bit reservoir to smooth frame-to-frame bitrate variations.
- **Bitrate Accuracy & Bias**: Measures how closely output bitrates hit the nominal target `cfg["bitrate"]`. Accuracy is reported as `(1.0 - |actual - target| / target) * 100%`, and Bias measures systematic overshooting or undershooting.

### VBR Mode (`-q <vbr_q>`)
- **Objective**: Maintain a constant perceptual quality level across clips. Clip bitrates naturally fluctuate based on spectral and transient complexity (e.g. complex/noisy clips spend significantly more bits than quiet/simple clips).
- **VBR Bitrate Δ (vs Base)**: Evaluates the candidate's bitrate change relative to baseline for the same `-q` quality setting: $\frac{\text{cand\_bitrate} - \text{base\_bitrate}}{\text{base\_bitrate}} \times 100\%$.
- **Target-Bitrate Accuracy Excluded**: VBR is not evaluated against fixed ABR scenario targets, as penalizing content-adaptive bit allocation produces false-positive warnings.
- **VBR Bitrate Anomaly Detection**: Automatically flags individual clips whose candidate bitrate drifts by $\ge 15\%$ from baseline at the same `-q` setting. This pinpoints specific encoder regressions (e.g., TNS, SBR, or psychoacoustic bugs on specific audio material).

Two scenarios, `48k_stereo_48k` and `48k_stereo_56k`, have **no achievable `vbr_q`**: libfaac's AUTO object-type resolution in VBR mode picks HE-AAC when `quantqual <= 75` and forces plain LC-AAC above that (`HE_VBR_QUANTQUAL_MAX` in `libfaac/frame.c`). On 48kHz stereo content HE-AAC tops out around ~41 kbps at q=75 and LC-AAC's cheapest encode is ~71 kbps at q=76 — there is no `-q` value that lands in the 42-70 kbps gap between them. For these scenarios, judge a PR by the **baseline-vs-candidate delta**, not the absolute deviation from `bitrate`.

## Bits-adjusted MOS delta

A candidate that simply spends more bits scores higher. Scenarios are compared
at a fixed `-b`, but nothing holds the *actual* output bitrate equal, so a raw
`avgMOSd` cannot distinguish **"allocates bits better"** from **"spent more of
them"**. This has decided rate-control work the wrong way more than once: a
+1.3% bitrate increase is worth roughly +0.03 MOS at 64 kbps on its own, which
is larger than most real wins.

`compare_clips.py` therefore prints, for any scenario whose bitrate moved by
≥0.1%:

```
48k_stereo_64k: n=49 avgMOSd=+0.0260 ... avg_br=67.4->68.2 (+1.26%) ...
   bits-adjusted avgMOSd=-0.0056 (at +0.0251 MOS per +1% bits)   <-- the raw delta is explained by bit spend, not allocation
```

The exchange rate comes from the **baseline's own bitrate ladder**: within a
family (`48k_stereo` at 24k…256k), the slope of MOS against bitrate is measured
by central difference around each scenario. No constant is assumed, and the
estimate is re-derived from whatever baseline you pass in.

Treat it as an estimate, not a verdict — it is a corpus-average slope applied to
a scenario mean. To settle a close call, **re-encode the candidate at a scaled
`-b` so actual bytes match the baseline, then compare directly**. That test
agreed with this adjustment to within 0.013 MOS on both cases it was checked
against.

Judge a rate-control change on the adjusted number. Judge a change that is meant
to alter bitrate (rate-control accuracy work) on both, and report them
separately.

## Decode errors

Count of candidate clips whose encoded `.aac` ffmpeg could not decode cleanly
(non-zero exit **or** any `-v error` stderr — ffmpeg returns 0 even on hard
decoder errors). Always reported; only fails the run under `--strict-decode`.
See [ci.md](ci.md).

## Per-band distortion (diagnostic)

Not a headline metric — an on-demand tool (`scripts/band_diag.py`,
`scripts/compare_clips.py --bands`). Reports RMS log-spectral error vs the reference in
fixed bands (0–4k, 4–8k, 8–12k, 12–18.4k, 18.4–24k). It localizes *where* in the
spectrum quality is lost; the 8–12k vs 12–18.4k split maps to an HE-AAC
half-rate core's top octave vs its SBR band.
