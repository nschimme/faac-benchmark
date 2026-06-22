# AAC Encoder Leaderboard

## Overall Rankings

| Rank | Encoder | Avg MOS | Worst MOS | Stereo Δ | Speed (xRT) | Bitrate Error | Footprint |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | FAAC | 0.000 | 0.000 | N/A | **177.3x** | 8.9% | 0.1 MB |
| 2 | FFmpeg AAC | 0.000 | 0.000 | N/A | 21.8x | 10.2% | 0.3 MB |
| 3 | fdkaac | 0.000 | 0.000 | N/A | 118.6x | 6.0% | 0.1 MB |

## Per-Scenario Quality (MOS)

| Scenario | FAAC | FFmpeg AAC | fdkaac |
| :--- | :---: | :---: | :---: |
| music_40 | N/A | N/A | N/A |
| music_48 | N/A | N/A | N/A |
| music_high | N/A | N/A | N/A |
| music_low | N/A | N/A | N/A |
| music_std | N/A | N/A | N/A |
| voip | N/A | N/A | N/A |
| vss | N/A | N/A | N/A |

## Per-Scenario Stereo Image Fidelity (Coherence Error)

| Scenario | FAAC | FFmpeg AAC | fdkaac |
| :--- | :---: | :---: | :---: |
| music_40 | N/A | N/A | N/A |
| music_48 | N/A | N/A | N/A |
| music_high | N/A | N/A | N/A |
| music_low | N/A | N/A | N/A |
| music_std | N/A | N/A | N/A |
| voip | N/A | N/A | N/A |
| vss | N/A | N/A | N/A |

## Per-Scenario Bitrate Accuracy (Error %)

| Scenario | FAAC | FFmpeg AAC | fdkaac |
| :--- | :---: | :---: | :---: |
| music_40 | 9.5% | **5.8%** | 8.2% |
| music_48 | 9.1% | **6.3%** | 6.6% |
| music_high | 6.2% | 7.5% | **1.4%** |
| music_low | 10.6% | 5.5% | **5.3%** |
| music_std | 9.7% | 8.1% | **2.6%** |
| voip | **5.0%** | 18.1% | 11.7% |
| vss | 11.9% | 19.7% | **5.9%** |

## Per-Scenario Efficiency (Speed xRT)

| Scenario | FAAC | FFmpeg AAC | fdkaac |
| :--- | :---: | :---: | :---: |
| music_40 | **125.5x** | 15.6x | 74.5x |
| music_48 | **118.5x** | 14.4x | 71.8x |
| music_high | **89.0x** | 13.6x | 42.6x |
| music_low | **108.6x** | 14.6x | 59.5x |
| music_std | **99.5x** | 13.9x | 50.9x |
| voip | **356.7x** | 45.8x | 304.7x |
| vss | **343.3x** | 34.7x | 226.4x |
