# AGENTS.md

- Tests are stdlib `unittest`, not pytest: `.venv/bin/python -m unittest discover -s tests -v`.
- CI-critical code lives at repo root (`run_benchmark.py`, `compare_encoders.py`,
  `compare_results.py`, `phase*.py`, `config.py`, `utils.py`). Local/diagnostic
  tooling belongs in `scripts/`, documented in `docs/scripts.md` — don't add
  new one-off scripts at root.
- Run everything from the repo root (data paths and intra-repo imports assume it).
- `-b` to `faac` is the *total* bitrate, not per-channel — libfaac's internals
  and scenario thresholds are per-channel. This has caused real bugs; see
  `scripts/visqol_env_ab.py`'s header comment before writing anything that
  passes bitrates to faac.
- Low-bitrate mono clips auto-select HE-AAC v1, which skips the core TNS/
  blockswitch path entirely — a TNS/blockswitch A/B at those bitrates without
  forcing `--object-type=lc` silently measures nothing and reports "no
  difference".
- CI's throughput/timing numbers aren't trustworthy on their own: baseline
  timing is cached across runs and only the candidate is re-measured fresh
  each run, so before/after comparisons can reflect cross-VM noise rather
  than a real change. Confirm suspected regressions with a same-machine,
  back-to-back run (see `scripts/amd64_denormal_perf_test.sh`).
- `--skip-mos` still encodes the full corpus (it only skips scoring); use
  `--skip-encode` for a footprint/throughput-only run that shouldn't touch it.
