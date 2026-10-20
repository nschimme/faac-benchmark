# Short-window TNS: evidence summary

This evidence evaluates the frozen `direction1-gain2` short-window TNS candidate against baseline FAAC in forced LC at nominal 128 kb/s. The compact paired per-clip CSV and its provenance are in [`evidence-48k128/`](evidence-48k128/). The source audio is not redistributed. Its 77 file hashes are pinned in [`inputs-48k128.json`](inputs-48k128.json).

## Results kept separate

- **MOS/bitrate:** across the two clips and four target curves used for bitrate validation, MOS BD-rate costs were +4.878% to +18.078%. The 77-clip corpus mean MOS delta was -0.002587; fatboy was the only loss beyond 0.05 MOS (-0.097342). Mean achieved bitrate changed by +0.0016 kb/s.
- **Pre-echo:** on the 77-clip corpus, eligible events were present in 36 clips (965 events). The mean positive excess among covered clips changed from 5.7813 to 3.9698 dB. Twenty-eight covered clips improved and eight worsened; 41 clips were N/A. On the eight-clip independent holdout, only three clips had eligible events. Coverage is limited and the event measurements are descriptive, not independent-event significance tests.
- **Stereo:** mean stereo-coherence error delta on the 77-clip corpus was -0.000016.
- **Attack:** mean absolute attack-centroid delta was -0.000130 ms; this did not consistently improve.

Do not combine these dimensions into a single score or call this an overall quality win. Pre-echo reductions do not reproduce the original ABX reports and do not show that listeners prefer the candidate. N/A is not zero. The candidate continued to trail Apple and FDK on pre-echo in the targeted eig/fatboy comparisons; the included 77-row snapshot itself is FAAC baseline/candidate only. The rerunnable harness can produce per-clip rows for baseline FAAC, candidate FAAC, Apple where available, and FDK.

## Provenance and source records

The candidate was frozen before holdout scoring. Candidate ID and settings, FAAC PR/commit, library and `tns.c` hashes, benchmark PR/commit, and input-manifest hash are retained in the evidence provenance JSON. Input WAV hashes are in each CSV row. The original local reports and raw AAC/audio artifacts remain outside this repository; the summary numbers above were transcribed from `TNS-TUNING.md` and the `full48k128`, `holdout-combined`, and `bitrate-validation` summaries. This compact CSV is a faithful export of the paired `per-clip-metrics.json` values, not a rerun.

## Limits

This is evidence for one frozen candidate and a limited selection of rates and clips. The bitrate result comes from four target curves on two clips, so it should not be generalized to every rate or program material. Pre-echo depends on quiet reference onsets and the scorer's thresholds; dense material may have no eligible event. Attack-centroid and stereo metrics are diagnostics, not audibility scores. The candidate's measured costs and benefits remain unresolved as an overall perceptual tradeoff.
