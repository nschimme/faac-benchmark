# Metric Definitions

# Metric Definitions & Audio Quality Assessment

This document outlines the perceptual, acoustic, and efficiency metrics used to evaluate audio encoders.

## Perceptual Audio Quality (MOS)

Perceptual quality is measured on a **1.0 to 5.0 Mean Opinion Score (MOS)** scale, reflecting objective human listening quality models:
- **Speech Content**: Evaluated using **ViSQOL** (Virtual Speech Quality Objective Listener), optimized for speech intelligibility and telephony bandwidths.
- **Music & Full-Band Audio**: Evaluated using **Zimtohrli**, a psychoacoustic model sensitive to temporal smearing, transient preservation, pre-echo, and high-frequency distortion.

### Interpretation Guide
- **Scale**: 1.0 (Bad) to 5.0 (Imperceptible / Transparent).
- **Score Delta ($\Delta \text{MOS}$)**: $\text{Candidate Score} - \text{Baseline Score}$. Positive values indicate quality gains.
- **Perceptual Significance**:
  - $|\Delta| \le 0.01$: Inaudible / measurement noise.
  - $|\Delta| \ge 0.05$: Noticeable quality shift in listening tests.

---

## Stereo Image Fidelity

Stereo fidelity measures how accurately an encoder preserves the spatial soundstage, inter-channel phase, and inter-aural coherence of stereo recordings.

- **Metric**: $1.0 - \text{Inter-Channel Coherence Error}$.
- **Scale**: $1.0$ represents perfect spatial imaging relative to the uncompressed reference.
- **Interpretation**: Higher values indicate truer stereo imaging. A positive delta ($\Delta > 0$) means the candidate encoder preserved stereo separation and spatial depth better than the baseline.

---

## Transient Fidelity & Attack Preservation

Percussive attacks (percussion, drum hits, acoustic guitar plucks) are prone to temporal smearing or pre-echo artifacts when transform codecs quantize sharp transients.

- **Measurement**: Detects transient attack onsets and measures the temporal centroid shift ($\Delta t$ in milliseconds) of energy following each attack.
- **Significance**: Lower temporal shift means attacks remain sharp and punchy without smearing or dulling.
- **Reporting**: Onset results are statistically pooled across audio clips. Improvements are reported when candidate attacks are verifiably closer in time and envelope to the reference.

---

## Bjontegaard-Delta Bitrate (BD-rate)

Evaluating quality at a fixed target bitrate can be misleading because encoders often slightly overshoot or undershoot their target. A codec that overshoots spends extra bits to gain quality, appearing "better" than it actually is.

**BD-rate solves this by measuring the percentage difference in bitrate required to achieve the exact same perceptual quality (MOS).**

### How to Read BD-rate
- **Negative BD-rate (%)**: **Superior efficiency.** The candidate encoder achieves identical sound quality using fewer bits (e.g., $-5.0\%$ BD-rate means 5% bit savings).
- **Positive BD-rate (%)**: **Inferior efficiency.** The candidate encoder requires more bits to reach equal quality.

### Curve Fitting & Profile Segmentation
BD-rate models rate-quality curves across bitrates using polynomial interpolation (cubic for 4+ test bitrates, quadratic for 3 test bitrates). Curves are segmented by codec profile (e.g., Low Complexity LC vs. High Efficiency HE-AAC) to ensure fair comparisons within identical coding profiles.

---

## Rate Control & Elementary Stream Bitrate Accuracy

To measure true codec bit distribution without container noise, bitrates are calculated directly from the **audio elementary stream (ES) payload**:

### Why Container Overhead is Excluded
Container headers (such as MP4/M4A `ftyp`, `moov`, and metadata atoms) add 1.5–2.0 KB of fixed non-audio byte overhead. On short audio clips (5–10 seconds at 16–32 kbps), container overhead introduces a false +7% to +15% bitrate "overshoot" that the encoder's rate-control engine is not responsible for.

### Calculation
$$\text{Actual Bitrate (kbps)} = \frac{\text{Elementary Stream Audio Bytes} \times 8}{\text{Uncompressed Audio Duration (seconds)} \times 1000}$$

### Rate Control Modes
- **Average Bitrate (ABR)**: Evaluates how accurately the encoder hits target bitrates and flags systematic overshoot or undershoot bias.
- **Variable Bitrate (VBR)**: Evaluates relative bitrate changes at equal quality settings ($q$), flagging clips where bitrate drifts significantly ($\ge 15\%$) relative to baseline.

---

## Encoding Throughput & Speed

- **Speed (xRealtime / xRT)**: Ratio of encoded audio duration to processing time (e.g., $50\text{xRT}$ means 1 second of compute time encodes 50 seconds of audio).
- **Deterministic Instruction Counting**: Evaluates CPU instruction counts (`I refs`) on fixed benchmark stimuli to eliminate background runner load and host VM timing noise.

---

## Per-Band Spectral Distortion

A diagnostic metric analyzing RMS log-spectral error across 5 frequency bands (0–4 kHz, 4–8 kHz, 8–12 kHz, 12–18.4 kHz, 18.4–24 kHz). This pinpoints precisely where in the frequency spectrum high-frequency roll-off, spectral hole filling, or Bandwidth Extension (SBR) distortion occurs.
