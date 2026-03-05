# FAAC Benchmark Suite

FAAC is the high-efficiency encoder for the resource-constrained world. From hobbyist projects to professional surveillance (VSS) and embedded VoIP, we prioritize performance where every cycle and byte matters.

This repository contains the FAAC Benchmark Suite, which provides the objective data necessary to ensure that every change moves us closer to our Northstar: the optimal balance of quality, speed, and size.

---

## Use as a GitHub Action

You can use this action in your workflow to run benchmarks. It is recommended to run benchmarks in a matrix and then use the reporting tool to consolidate results.

### Example Workflow (PR Regression Testing)

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
```

### Action: `nschimme/faac-benchmark`

Runs the encoding benchmark and MOS computation for a single configuration.

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `faac-bin` | Path to the `faac` binary. | Yes | |
| `libfaac-so` | Path to the `libfaac.so` library. | Yes | |
| `run-name` | Identifier for this benchmark run (e.g., `amd64_single_base`). | Yes | |
| `output-json` | Path where the result JSON should be saved. | Yes | |
| `coverage` | Percentage of dataset to cover (1-100). | No | `100` |
| `skip-mos` | Skip perceptual quality (MOS) computation. | No | `false` |
| `visqol-image` | Docker image for ViSQOL. Defaults to internal discovery logic. | No | `""` |

### Action: `nschimme/faac-benchmark/report`

Consolidates multiple result JSONs into a single Markdown report and GitHub Step Summary.

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `results-path` | Path to the directory containing result JSON files. | Yes | |
| `base-sha` | Baseline commit SHA for display in the report. | No | |
| `cand-sha` | Candidate commit SHA for display in the report. | No | |
| `summary-only` | Generate only the high-signal summary. | No | `false` |

---

## The "Golden Triangle" Philosophy

We evaluate every contribution against three competing pillars. While high-bitrate encoders like FDK-AAC or Opus target multi-channel, high-fidelity entertainment, FAAC focuses on remaining approachable and distributable for the global open-source community. We prioritize non-patent encumbered areas and the standard Low Complexity (LC-AAC) profile.

1.  **Audio Fidelity**: We target transparent audio quality for our bitrates. We use objective metrics like ViSQOL (MOS) to ensure psychoacoustic improvements truly benefit the listener without introducing "metallic" ringing or "underwater" artifacts.
2.  **Computational Efficiency**: FAAC must remain fast. We optimize for low-power cores where encoding speed is a critical requirement. Every CPU cycle saved is a win for our users.
3.  **Minimal Footprint**: Binary size is a feature. We ensure the library remains small enough to fit within restrictive embedded firmware.

---

## Benchmarking Scenarios

| Scenario | Mode | Source | Config | Project Goal |
| :--- | :--- | :--- | :--- | :--- |
| **VoIP** | Speech (16k) | TCD-VOIP | `-b 16` | Clear communication at low bitrates (16kbps). |
| **VSS** | Speech (16k) | TCD-VOIP | `-b 40` | High-fidelity Video Surveillance Systems recording (40kbps). |
| **Music** | Audio (48k) | PMLT / SoundExpert | `-b 64-256` | Full-range transparency for storage & streaming. |
| **Throughput** | Efficiency | Synthetic Signals | Default | Stability test using 10-minute Sine/Sweep/Noise/Silence. |

---

## Local Usage

The suite can also be run locally for development and testing.

### 1. Install Dependencies
```bash
# System (Ubuntu/Debian)
sudo apt-get update && sudo apt-get install -y meson ninja-build bc ffmpeg

# Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Prepare Datasets
Downloads samples and generates 10-minute synthetic throughput signals (Sine, Sweep, Noise, Silence).
```bash
python3 setup_datasets.py
```

### 3. Run a Benchmark

You can run the full benchmark using the user-friendly entrypoint:
```bash
python3 run_benchmark.py path/to/faac path/to/libfaac.so my_run results/my_run.json --coverage 100
```

This script manages everything for you:
1.  **Phase 1**: Encodes samples and measures throughput and library size.
2.  **Phase 2**: Computes perceptual quality (MOS). It automatically attempts to use your local ViSQOL installation or falls back to containerized execution via **Docker** or **Podman**.

#### Docker Image Discovery
The benchmark suite uses a deterministic approach to find the correct ViSQOL Docker image:
1.  **Search**: It first looks for a local image named `ghcr.io/nschimme/faac-benchmark-visqol` tagged with the current Git tag (if any) or a short hash of the build files (`Dockerfile.visqol`, etc.).
2.  **Pull**: If not found locally, it attempts to pull that same image/tag from GitHub Container Registry.
3.  **Build**: As a last resort, it builds the image locally.

You can override this behavior by passing `--visqol-image <your-image>` to `run_benchmark.py`.

---

## Metric Definitions

| Metric | Definition | Reference |
| :--- | :--- | :--- |
| **MOS** | Mean Opinion Score (LQO). Predicted perceptual quality from 1.0 (Bad) to 5.0 (Excellent), computed via the **ViSQOL** model. | [ITU-T P.800](https://www.itu.int/rec/T-REC-P.800), [ViSQOL](https://github.com/google/visqol) |
| **Regressions** | Critical failure or a drop in MOS ≥ 0.1 compared to the baseline commit. Significant throughput drops (>10%) or increased binary size also warrant review. | |
| **Significant Win** | An improvement in MOS ≥ 0.1 compared to the baseline commit. | |
| **Consistency** | Percentage of test cases where bitstreams are MD5-identical to the baseline. | |
| **Throughput** | Normalized encoding speed improvement against baseline. Higher % indicates faster execution. | |
| **Library Size** | Binary footprint of `libfaac.so`. Delta measured against baseline. Critical for embedded VSS/IoT targets. | |
| **Bitrate Δ** | Percentage change in generated file size against baseline. Relative shift in bits used for the same target. | |
| **Bitrate Accuracy** | The closeness of the achieved bitrate to the specified target (ABR mode). Measures the encoder's ability to respect the user-defined bitrate budget. | |

---

## Dataset Sources

We are grateful to the following projects for providing high-quality research material:

*   **TCD-VoIP (Sigmedia-VoIP)**: [Listener Test Database](https://www.sigmedia.tv/datasets/tcd_voip_ltd/) - Specifically designed for assessing quality in VoIP applications.
*   **PMLT2014**: [Public Multiformat Listening Test](https://listening-test.coresv.net/) - A community-defined comprehensive multi-codec benchmark.
*   **SoundExpert**: [Sound Samples](https://soundexpert.org/sound-samples) - High-precision EBU SQAM CD excerpts for transparency testing.

---

## License

This project is licensed under the LGPL v2.1. See the [LICENSE.md](LICENSE.md) file for details.
