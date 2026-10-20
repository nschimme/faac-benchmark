# Transient measurements

The benchmark records two distinct transient measurements. `attack_centroid_ms` measures post-onset attack timing. `pre_echo_db` measures energy before a reference-selected onset. Neither replaces MOS or the stereo-image metric.

## Measurement definition

Pre-echo uses the high-pass 2 kHz NPER measurement on fine onsets found in the reference. It compares decoded and reference pre-onset energy from 6 ms to 1 ms before the refined reference onset; a 0–12 ms post-onset window supplies the local event context. Events are eligible only when the reference pre/post energy ratio is at most -20 dB. The energy floor is -80 dB relative to that event's reference post-window power, not an absolute dBFS floor. One global lag per recording is estimated from normalized channel correlations and bounded to ±250 ms at 48 kHz; measurements are retained per channel.

Raw `pre_echo_db` values are signed: positive values indicate excess decoded pre-onset energy relative to the reference; negative values can reflect attenuation. The leaderboard clamps each event to `max(0, dB)` before pooling, so attenuation cannot cancel positive excess. Its mean and p95 are pooled over scored events. It also reports candidate, refined, eligible, and scored event counts, plus scored, eligible, and current clip coverage.

`compare_results.py` reports A/B comparisons differently: it first computes each clip's mean positive excess, then takes an unweighted mean across comparable clips. Event arrays are not paired by index, because event positions are not part of the saved metric. The A/B event and eligible counts show the amount of data behind that clip-level summary. These pooled-event leaderboard values and unweighted clip means answer different aggregation questions.

Quiet-reference eligibility limits coverage. Dense audio can have no eligible events, and a recording can have eligible events but no scored events. Those cases have no pre-echo score and display N/A with coverage counts. An empty score list with valid zero counts is a completed measurement, not a perfect zero. Attenuation or high-frequency removal can also lower NPER without improving fidelity. Compare candidates on matched scored-event and clip coverage; do not treat an all-zero score or low coverage as a win.

## Fresh measurements

Run `compare_codecs.py` in encoder mode for a new run. By default it encodes and decodes the selected matrix and computes MOS, stereo image, attack-centroid shift, and pre-echo. `--skip-mos` omits MOS scoring; `--skip-stereo` omits stereo-image scoring. `--skip-transient` omits both attack-centroid and pre-echo measurements.

```bash
NUMBA_CACHE_DIR=/tmp/faac-benchmark-numba \
  python compare_codecs.py --mode encoder \
  --faac-bin ../faac/build/frontend/faac \
  --faac-lib ../faac/build/libfaac/libfaac.3.dylib \
  --gate --scenarios 44k1_stereo_128k,48k_stereo_128k \
  --results-json results/faac-transients.json \
  --output results/faac-transients.md
```

Adjust the executable and library paths for the build platform. The example selects the fast gate clips in two scenarios. `--faac-bin` selects the FAAC executable; supported FAAC profiles are still probed, and other installed encoder families can be auto-detected too. A fresh run is the default; `--resume` reuses compatible saved encoder rows.

## Backfill saved encodes

For a complete saved `compare_codecs.py` matrix, `--resume --skip-mos --skip-stereo` allows the versioned pre-echo field to be backfilled from each row's recorded AAC and reference paths. This helper does not encode, score MOS, or recompute stereo or attack-centroid values. It writes the updated result JSON. Keep the encoder selection and scenario set compatible with the saved rows. If selected rows are missing, resume schedules those missing encodes; use a complete matrix when the intent is backfill-only.

```bash
NUMBA_CACHE_DIR=/tmp/faac-benchmark-numba \
  python compare_codecs.py --mode encoder --resume \
  --skip-mos --skip-stereo \
  --faac-bin ../faac/build/frontend/faac \
  --faac-lib ../faac/build/libfaac/libfaac.3.dylib \
  --gate --scenarios 44k1_stereo_128k,48k_stereo_128k \
  --results-json results/faac-transients.json \
  --output results/faac-transients.md
```

For `run_benchmark.py` result JSON files, Phase 3 can backfill attack-centroid and pre-echo from existing AAC files without running MOS, encoding, or stereo analysis:

```bash
python phase3_stereo.py results/faac.json output/ data/external \
  --skip-stereo --decoder ffmpeg
```

Phase 3 updates only missing transient values and writes the result JSON. Its result format is the `run_benchmark.py` matrix format; it is not the list format used by `compare_codecs.py`.

`--skip-transient` suppresses both transient metrics in either workflow. A versioned pre-echo result has `pre_echo_metric_version: 1`, a finite `pre_echo_db` list, a coverage object, and an integer alignment lag. A valid empty list is allowed when no events were scored. `pre_echo_error` records a failed measurement; error rows are not current and are not aggregated. Missing, old-version, incomplete, or failed values are reported as N/A and can be retried during backfill. Inspect the raw JSON for the error string and event counts when a result is N/A.

## TNS evaluation

Use transient metrics as separate evidence when evaluating a TNS change. Review MOS and, where a bitrate ladder exists, BD-rate alongside stereo fidelity, attack timing, pre-echo coverage and excess, bitrate, interoperability, speed, and footprint. A pre-echo improvement does not by itself establish an overall quality improvement, and a MOS-only veto omits the other measured tradeoffs. Keep the comparison matched by source clips, sample rate, bitrate control, decoder, and scored-event coverage. Treat diagnostic metrics as evidence for a decision, not as standalone perceptual scores.
