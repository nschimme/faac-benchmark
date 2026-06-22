# AAC Encoder Leaderboard

## Overall Rankings

| Rank | Encoder | Avg MOS | Worst MOS | Speed (xRT) | Bitrate Error | Footprint |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| 1 | FAAC | 0.000 | 0.000 | 181.3x | 8.9% | 0.1 MB |
| 2 | FFmpeg AAC | 0.000 | 0.000 | 23.2x | 10.2% | 0.3 MB |
| 3 | fdkaac | 0.000 | 0.000 | 123.7x | 6.0% | 0.1 MB |

## Per-Scenario Quality (MOS)

| Encoder | music_40 | music_48 | music_high | music_low | music_std | voip | vss |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| FAAC | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| FFmpeg AAC | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| fdkaac | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

## Per-Scenario Bitrate Accuracy (Error %)

| Encoder | music_40 | music_48 | music_high | music_low | music_std | voip | vss |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| FAAC | 9.5% | 9.1% | 6.2% | 10.6% | 9.7% | 5.0% | 11.9% |
| FFmpeg AAC | 5.8% | 6.3% | 7.5% | 5.5% | 8.1% | 18.1% | 19.7% |
| fdkaac | 8.2% | 6.6% | 1.4% | 5.3% | 2.6% | 11.7% | 5.9% |

## Per-Scenario Efficiency (Speed xRT)

| Encoder | music_40 | music_48 | music_high | music_low | music_std | voip | vss |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| FAAC | 123.8x | 94.4x | 86.8x | 117.9x | 98.4x | 329.6x | 418.1x |
| FFmpeg AAC | 15.8x | 13.2x | 13.9x | 14.4x | 13.8x | 51.9x | 39.6x |
| fdkaac | 72.3x | 71.5x | 42.4x | 58.6x | 48.4x | 323.3x | 249.7x |
