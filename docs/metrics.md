# Metric Definitions

# Metric Definitions & Audio Quality Assessment

This document outlines the perceptual, acoustic, and efficiency metrics used to evaluate audio encoders.

## Perceptual Audio Quality (MOS)

Perceptual quality is measured on a **1.0 to 5.0 Mean Opinion Score (MOS)** scale, reflecting objective human listening quality models:
- **Speech Content**: Evaluated using **ViSQOL** (Virtual Speech Quality Objective Listener), optimized for speech intelligibility and telephony bandwidths.
- **Music & Full-Band Audio**: Evaluated using **Zimtohrli**, a psychoacoustic model sensitive to temporal smearing, transient preservation, pre-echo, and high-frequency distortion.

### Overall Ranking Floor: 1st Percentile MOS ($P_1$) / 99% Floor MOS
- **1st Percentile MOS ($P_1$)**: The score threshold that **99% of tested audio clips equal or exceed**.
- **Why $P_1$ beats Worst MOS**: Absolute Minimum/Worst MOS is a single-sample metric ($N=1$) vulnerable to anomalous test sample artifacts or single-file flukes. $P_1$ filters out single-file outliers while accurately measuring the encoder's true quality floor on difficult audio.

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

## Bass Haze

A loud, steady bass that plays on its own (nothing else above about 1 kHz comes close to its level) can leave a faint broadband hiss in the encoded file, because the transform windows spread the bass energy across the whole spectrum and the encoder then quantizes that spread badly. It is audible on clean recordings and invisible to the music corpora, which never contain such a passage. It also barely moves MOS: the same defect costs about 0.9 MOS when the content above 1 kHz is scored alone and about 0.1 when it is scored together with the bass. So this metric measures it directly.

- **Measurement**: a short synthetic test clip, a deep bass tone over digital silence, is encoded with fixed settings (LC and HE-AAC), decoded, and the decoded audio is compared with the source, with no perceptual model involved. It records how much extra energy the decoded audio carries between 1.2 and 8 kHz, the part of the spectrum the codec reproduces waveform by waveform (the haze). HE-AAC codes the band above it parametrically, so its level there reflects other settings, not this defect.
- **Scale**: haze is in dB above the source; 0 dB means none. The number is only meaningful against the baseline, not as an absolute level: on the bass clip an encoder with the defect sits roughly 37-55 dB above the source and one without it roughly 7-16 dB, while the control clips read 0 dB.
- **Interpretation**: lower is better. A negative delta against the baseline means the candidate removes haze; a positive one means it has come back. Two more clips keep the result honest: the same bass under quiet treble, where there is no haze to remove and the reading must stay near 0 dB, and a series of kick drums, where the error just before each kick must not grow, so that removing the haze does not smear real attacks.
- **Reporting**: unlike the metrics above, it is not an average over the corpus. It is a pass/fail regression guard, reported as the `haze` gate in the summary, so a change to window selection or block switching cannot bring the haze back unnoticed. It does not replace MOS or a listening test, and the clips and thresholds are described under [CI](ci.md#the-haze-gate).

---

## Bjontegaard-Delta Bitrate (BD-rate)

Evaluating quality at a fixed target bitrate can be misleading because encoders often slightly overshoot or undershoot their target. A codec that overshoots spends extra bits to gain quality, appearing "better" than it actually is.

**BD-rate solves this by measuring the percentage difference in bitrate required to achieve the exact same perceptual quality (MOS).**

### How to Read BD-rate
- **Negative BD-rate (%)**: **Superior efficiency.** The candidate encoder achieves identical sound quality using fewer bits (e.g., $-5.0\%$ BD-rate means 5% bit savings).
- **Positive BD-rate (%)**: **Inferior efficiency.** The candidate encoder requires more bits to reach equal quality.

### Curve Fitting & Profile Segmentation
BD-rate models rate-quality curves across bitrates with monotone piecewise-cubic (PCHIP) interpolation through every rung. A least-squares cubic swings between unevenly spaced rungs near MOS saturation and can score a candidate that uses fewer bits for higher MOS at every rung as a loss; PCHIP cannot. Curves are segmented by codec profile (e.g., Low Complexity LC vs. High Efficiency HE-AAC) to ensure fair comparisons within identical coding profiles.

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

## Decoder Conformance SNR & MOS Inheritance

Decoder correctness is measured against the **cached ffmpeg decode of the
same bitstream** (the encoder phase's own conformance reference), not the
original uncompressed source -- this isolates decoder-implementation bugs
from the encoder's lossy-compression error, which `snr_db` (measured against
the original) still captures separately.

- **Conformance SNR (`conformance_snr_db`)**: SNR in dB between a decoder's
  output and the ffmpeg decode of the same bitstream. `inf`/bit-exact is
  ideal; the `--gate` FAAD3 check requires >= 60 dB.
- **MOS inheritance**: at conformance SNR >= 60 dB, a decoder's output is
  perceptually indistinguishable from the ffmpeg decode already scored in
  the encoder phase, so it inherits that MOS (`mos_source: "inherited"`)
  instead of a fresh scoring pass. Below 60 dB, or when no reference decode
  is available, the decoder's own output is scored directly
  (`mos_source: "scored"`).

## Robustness: Pass / Fail / Timeout / Runaway

Corrupted-bitstream decoding (a separate decode-only pass, not scored) is
classified per decoder:
- **Pass**: exits cleanly, output PCM size is consistent with the intact
  stream.
- **Fail**: nonzero exit / decode error.
- **Timeout**: exceeded the decode time budget.
- **Runaway**: exits 0 but its output PCM exceeds 4x the intact stream's
  expected size -- it did not fail cleanly, it kept synthesizing audio past
  a desynced frame/length field. Counted as a failure, not a pass.

## Per-Band Spectral Distortion

A diagnostic metric analyzing RMS log-spectral error across 5 frequency bands (0–4 kHz, 4–8 kHz, 8–12 kHz, 12–18.4 kHz, 18.4–24 kHz). This pinpoints precisely where in the frequency spectrum high-frequency roll-off, spectral hole filling, or Bandwidth Extension (SBR) distortion occurs.

---

## Code & Memory Footprint (Flash & RAM)

Evaluating library memory requirements separates non-volatile code storage (Flash/ROM) from active process memory (SRAM):

- **Flash / Code Footprint (ROM)**: Calculated as `.text + .rodata + .data`. Includes executable instructions (`.text`), read-only constants/tables (`.rodata`), and initial values for writable variables (`.data` stored in Flash before startup).
- **Static Process RAM Footprint**: Calculated as `.data + .bss`. Initialized and uninitialized writable variables allocated in RAM when loaded into process memory. Converting mutable tables to `const` reduces `.data`, directly saving RAM.
- **Peak Dynamic RAM (Max RSS)**: Measured in isolated single-process runs during throughput benchmark passes to track peak physical memory high-water marks (in KB/MB) free from parallel worker pool overhead.
