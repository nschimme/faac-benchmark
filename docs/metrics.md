# Metric Definitions

## Perceptual Quality (MOS)

Computed in Phase 2 (`phase2_mos.py`). By default, Phase 2 uses **Zimtohrli** (`zimtohrli.Pyohrli()`) as the primary perceptual engine, mapping psychoacoustic distance deterministically to a 1.0–5.0 MOS scale (`zimtohrli.mos_from_zimtohrli()`). Zimtohrli is particularly sensitive to transient preservation, temporal smearing, and pre-echo artifacts.

ViSQOL is also supported as an explicit or fallback backend (`--backend visqol-python` / `--backend visqol`).

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

Encode-time change vs base (positive = faster). Measured single-core on the
fixed throughput stimuli. The report also breaks it down per stimulus and flags
the worst-case scenario.

## Bitrate accuracy / bias

How close the actual output bitrate is to the scenario target, and whether the
encoder systematically over- or under-shoots.

In VBR mode this compares against `config.py`'s `vbr_q` (faac's `-q`, a percent
quantizer quality, not a bitrate). `vbr_q` per scenario is chosen by grid
search (`scripts/calibrate_vbr_q.py`) against representative content so it lands near
the scenario's nominal `bitrate`, but VBR is inherently content-dependent, so
expect single-digit-to-teens percent deviation even with no code change.

Two scenarios, `48k_stereo_48k` and `48k_stereo_56k`, have **no achievable
`vbr_q`** and will always show a larger (~15-35%) deviation: libfaac's AUTO
object-type resolution in VBR mode picks HE-AAC when `quantqual <= 75` and
forces plain LC-AAC above that (`HE_VBR_QUANTQUAL_MAX` in `libfaac/frame.c`).
On 48kHz stereo content HE-AAC tops out around ~41 kbps at q=75 and LC-AAC's
cheapest encode is ~71 kbps at q=76 — there is no `-q` value that lands in the
42-70 kbps gap between them. For these two scenarios, judge a PR by the
**baseline-vs-candidate delta**, not the absolute deviation from `bitrate`; a
large but *unchanged* deviation on both sides is expected and not a
regression.

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
