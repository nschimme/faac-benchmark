# CI Integration

The suite runs in GitHub Actions (see `action.yml`). The contract is unchanged
by the ergonomics work: results are written as `<suite>_base.json` and
`<suite>_cand.json` pairs into a results directory, and `compare_results.py`
consolidates every pair into one Markdown report + summary.

## Use as a GitHub Action

Run benchmarks in a matrix, then consolidate with the reporting action.

### Example workflow (PR regression testing)

```yaml
jobs:
  benchmark:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        arch: [amd64]
        precision: [single, double]
    steps:
      - name: Checkout Candidate
        uses: actions/checkout@v4
        with:
          path: candidate
      - name: Build Candidate
        run: |
          cd candidate
          meson setup build_cand -Dfloating-point=${{ matrix.precision }} --buildtype=release
          ninja -C build_cand
      - name: Determine Baseline SHA
        id: baseline-sha
        run: |
          if [ "${{ github.event_name }}" == "push" ]; then
            echo "sha=${{ github.sha }}" >> $GITHUB_OUTPUT
          else
            echo "sha=${{ github.event.pull_request.base.sha }}" >> $GITHUB_OUTPUT
          fi
      - name: Checkout Baseline
        uses: actions/checkout@v4
        with:
          ref: ${{ steps.baseline-sha.outputs.sha }}
          path: baseline
      - name: Build Baseline
        run: |
          cd baseline
          meson setup build_base -Dfloating-point=${{ matrix.precision }} --buildtype=release
          ninja -C build_base
      - name: Run Benchmark (Baseline)
        uses: nschimme/faac-benchmark@v1
        with:
          faac-bin: ./baseline/build_base/frontend/faac
          libfaac-so: ./baseline/build_base/libfaac/libfaac.so
          run-name: ${{ matrix.arch }}_${{ matrix.precision }}_base
          output-json: ./results/${{ matrix.arch }}_${{ matrix.precision }}_base.json
      - name: Run Benchmark (Candidate)
        uses: nschimme/faac-benchmark@v1
        with:
          faac-bin: ./candidate/build_cand/frontend/faac
          libfaac-so: ./candidate/build_cand/libfaac/libfaac.so
          run-name: ${{ matrix.arch }}_${{ matrix.precision }}_cand
          output-json: ./results/${{ matrix.arch }}_${{ matrix.precision }}_cand.json
      - name: Upload Results
        uses: actions/upload-artifact@v4
        with:
          name: results-${{ matrix.arch }}-${{ matrix.precision }}
          path: results/*.json

  report:
    needs: benchmark
    runs-on: ubuntu-latest
    if: always()
    permissions:
      pull-requests: write
    steps:
      - name: Download all results
        uses: actions/download-artifact@v4
        with:
          path: results
          pattern: results-*
          merge-multiple: true
      - name: Generate Report
        uses: nschimme/faac-benchmark/report@v1
        with:
          results-path: ./results
          base-sha: ${{ github.event.pull_request.base.sha }}
          cand-sha: ${{ github.event.pull_request.head.sha }}
      - name: Post Summary to PR
        if: github.event_name == 'pull_request'
        shell: bash
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh pr comment ${{ github.event.pull_request.number }} --body-file summary.md
```

### Action: `nschimme/faac-benchmark`

Runs the encoding benchmark and MOS computation for a single configuration.

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `faac-bin` | Path to the `faac` binary. | Yes | |
| `libfaac-so` | Path to the `libfaac.so` library. | Yes | |
| `run-name` | Identifier for this run (e.g. `amd64_single_base`). | Yes | |
| `output-json` | Path where the result JSON should be saved. | Yes | |
| `coverage` | Percentage of dataset to cover (1-100). | No | `100` |
| `skip-mos` | Skip perceptual quality (MOS) computation. | No | `false` |
| `visqol-image` | Docker image for ViSQOL (else internal discovery). | No | `""` |
| `sha` | Commit SHA to associate with these results. | No | `${{ github.sha }}` |
| `scenarios` | Comma-separated scenarios (e.g. `voip,vss`). | No | |
| `include-tests` | Comma-separated include globs (e.g. `TCD_*`). | No | |
| `exclude-tests` | Comma-separated exclude globs. | No | |
| `backend` | `auto`, `docker`, `visqol`, `visqol-py`, `visqol-python`. | No | `visqol-python` |

### Action: `nschimme/faac-benchmark/report`

Consolidates multiple result JSONs into one Markdown report + GitHub Step
Summary, and writes `summary.md` for a PR comment.

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `results-path` | Directory containing result JSON files. | Yes | |
| `base-sha` | Baseline commit SHA (else pulled from JSONs). | No | |
| `cand-sha` | Candidate commit SHA (else pulled from JSONs). | No | |
| `summary-only` | Generate only the high-signal summary. | No | `false` |

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

## Multi-Encoder Leaderboard (`leaderboard.yml`)

The repository includes an automated workflow to compare `faac` against other
stable AAC encoders (FDK-AAC, FFmpeg internal).

- **Triggers**: On every push or pull request to `master`.
- **Output**: Generates a competitive leaderboard in the **GitHub Action Summary** and as a downloadable `leaderboard_ci.md` artifact.
- **Metrics**: Reports Quality (MOS), Stereo Fidelity, Encoding Speed, and Bitrate Accuracy across all scenarios.

To trigger manually:
Go to **Actions** -> **Multi-Encoder Leaderboard** -> **Run workflow**.

## Provenance

Pass `--faac-git-sha` and `--faac-precision` to `run_benchmark.py` to stamp the
result JSON with build provenance (`faac_git_sha`, `faac_precision`,
`faac_args`), so CI artifacts are self-describing and comparable across runs.
