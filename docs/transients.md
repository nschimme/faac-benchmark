# Transient diagnostics

`transient.py` computes reference-locked pre-echo (NPER) and **post-onset HF
residual, 12-50 ms** from one selection of scored events, per channel. The
residual measures post-attack error in the high band; it is not a measurement
of “smearing” or audibility.

Both metrics use the same global alignment lag, reference-only onset
refinement, quiet-pre-window eligibility, and causal fourth-order Butterworth
2 kHz high-pass signals at 48 kHz. The residual window is
[onset+12 ms, onset+50 ms), or samples [onset+576, onset+2400).
For each event it reports `10*log10(mean((dec-ref)^2)/mean(ref^2))`, with the
numerator floored at `1e-8` times that window's own reference energy.
Identity is therefore -80 dB. Values are signed: more negative means less
residual error; larger means more error. Average the signed event values,
without taking a positive part.

JSON rows contain `post_onset_residual_db` and `post_onset_metric_version`
(version 1), reusing `pre_echo_coverage` and `pre_echo_lag_samples`. Each list
position corresponds to the same NPER event. A `null` denotes an incomplete
12-50 ms window or zero reference energy and is excluded from the signed
mean; it does not change NPER's event coverage. An empty list means no scored
events. Missing or stale fields are unavailable, not perfect scores. Resume
and Phase 3 backfill both metrics together. The leaderboard adds the residual
column only to its existing collapsed pre-echo diagnostic when the field is
present. Neither metric changes MOS ranking or adds a CI gate.

The metric was validated before use: it passed identity at the floor, aligned ±1 sample stability
within 0.01 dB, specificity against clean pre-echo, monotone sensitivity to
post-onset noise, calibration bitrate ladders, and deterministic repeated
scoring. The clean control adds white noise only in [onset-6 ms, onset-1 ms)
at -20 dB relative to the unfiltered 12 ms onset RMS, evaluating each event
separately: NPER rises by more than 5 dB while the residual changes by less
than 0.5 dB. These are measurement validation results, without audibility claims.

- The 0-12 ms residual window failed the clean pre-echo control on girl.
- The 2-8 kHz dulling candidate was not monotone across bitrate at 44.1 kHz.
- The 10-90% rise-time candidate moves under pure pre-echo.

`attack_centroid_ms` did not discriminate between encoders at 128 kbps in the
validated comparison.
