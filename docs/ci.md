# CI Integration

The suite runs in GitHub Actions (see `action.yml`). The contract is unchanged
by the ergonomics work: results are written as `<suite>_base.json` and
`<suite>_cand.json` pairs into a results directory, and `compare_results.py`
consolidates every pair into one Markdown report + summary.

## Consolidating results

```bash
python3 compare_results.py <results_dir> \
    --output report.md --summary-output summary.md \
    [--base-sha SHA] [--cand-sha SHA] [--strict-decode]
```

`compare_results.py` exits non-zero when it finds a regression or missing data,
which fails the CI job.

## Decode-error gating (`--strict-decode`)

Each candidate clip carries a `decode_error` field from phase 1's decode
validation. The report always shows a **Decode Errors** count.

* **Default (no flag):** decode errors are reported but do **not** fail the run.
  This warn-first default exists so the long-reliable LC benchmark can't be
  red-walled by a benign ffmpeg stderr quirk before the strict check is proven
  clean on the full corpus.
* **`--strict-decode`:** a candidate that fails decode validation is treated as
  a hard regression (`💀`) and fails the run. Enable this once the strict
  decoder check has been validated as clean across the LC corpus (it has, as of
  this change: 0 decode errors over 448 LC clips spanning all scenarios).

## Provenance

Pass `--faac-git-sha` and `--faac-precision` to `run_benchmark.py` to stamp the
result JSON with build provenance (`faac_git_sha`, `faac_precision`,
`faac_args`), so CI artifacts are self-describing and comparable across runs.
