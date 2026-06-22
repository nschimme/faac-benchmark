# Metric Definitions

## MOS (Mean Opinion Score)

Perceptual quality via **ViSQOL**, computed in phase 2. Audio scenarios use the
SVR model (`libsvm_nu_svr_model.txt`); speech scenarios use ViSQOL speech mode
with the lattice TFLite model. Reported as **Avg MOS Δ** (candidate − base);
positive is better. Per-clip status: `🌟` significant win, `⚠️`/`❌`/`💀`
increasing regression severity, where `💀` means the candidate fell below the
scenario's pass threshold.

## Stereo Image Fidelity (Inter-channel Coherence)

From phase 3 (`phase3_stereo.py`). MOS is monaural and cannot see stereo-image
damage; this metric tracks inter-channel coherence error (`ic_err`, lower is
truer).

- **Leaderboard**: Reports the raw coherence error value (lower is truer).
- **A/B Report**: Reports the signed delta where **positive = candidate has the truer stereo image**.

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
