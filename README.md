# FAAC Benchmark Suite

FAAC is the high-efficiency encoder for the resource-constrained world. This repository contains the FAAC Benchmark Suite, providing objective data to ensure quality, speed, and size are balanced.

## Quick Start

1.  **Install**: `pip install -r requirements.txt`
2.  **Setup**: `python3 setup_datasets.py`
3.  **Run**: `python3 run_benchmark.py <faac> <libfaac.so> <name> <output.json>`

## Documentation

*   **[Local Usage](docs/usage.md)**: How to run benchmarks and diagnostic tools locally.
*   **[Benchmarking Guide](docs/benchmarking.md)**: Details on scenarios, filtering, A/B mode, and sweeps.
*   **[CI Integration](docs/ci.md)**: Using the benchmark suite in GitHub Actions.
*   **[Metric Definitions](docs/metrics.md)**: Understanding MOS, Stereo Image Δ, and other metrics.

## Philosophy

We evaluate every contribution against the **Golden Triangle**:

1.  **Audio Fidelity**: Transparent quality at target bitrates.
2.  **Computational Efficiency**: Optimized for low-power cores.
3.  **Minimal Footprint**: Small binary size for embedded systems.

## Dataset Sources

We are grateful to the following projects for providing high-quality research material:

*   **TCD-VoIP (Sigmedia-VoIP)**: [Listener Test Database](https://www.sigmedia.tv/datasets/tcd_voip_ltd/) — designed for assessing quality in VoIP applications.
*   **PMLT2014**: [Public Multiformat Listening Test](https://listening-test.coresv.net/) — a community-defined comprehensive multi-codec benchmark.
*   **SoundExpert**: [Sound Samples](https://soundexpert.org/sound-samples) — high-precision EBU SQAM CD excerpts for transparency testing.

## License

This project is licensed under the LGPL v2.1. See the [LICENSE.md](LICENSE.md) file for details.
