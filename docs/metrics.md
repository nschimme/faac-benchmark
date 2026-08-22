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

## Throughput Δ

Encode-time change vs base (positive = faster). Measured single-core on the
fixed throughput stimuli. The report also breaks it down per stimulus and flags
the worst-case scenario.

## Bitrate accuracy / bias

How close the actual output bitrate is to the scenario target, and whether the
encoder systematically over- or under-shoots.

## Decode errors

Count of candidate clips whose encoded `.aac` ffmpeg could not decode cleanly
(non-zero exit **or** any `-v error` stderr — ffmpeg returns 0 even on hard
decoder errors). Always reported; only fails the run under `--strict-decode`.
See [ci.md](ci.md).

## Per-band distortion (diagnostic)

Not a headline metric — an on-demand tool (`band_diag.py`,
`compare_clips.py --bands`). Reports RMS log-spectral error vs the reference in
fixed bands (0–4k, 4–8k, 8–12k, 12–18.4k, 18.4–24k). It localizes *where* in the
spectrum quality is lost; the 8–12k vs 12–18.4k split maps to an HE-AAC
half-rate core's top octave vs its SBR band.
