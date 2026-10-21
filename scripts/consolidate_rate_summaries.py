#!/usr/bin/env python3
"""Roll up the per-rate-control benchmark summaries for the CI job summary."""

import argparse
from pathlib import Path


def first_heading(lines):
    return next((line.lstrip("#").strip() for line in lines
                 if line.startswith("## ") and not line.startswith("### ")),
                "Benchmark summary")


def section_table(lines, heading):
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == heading), None)
    if start is None:
        return []

    table = []
    started = False
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            started = True
            if "Transient" not in line:
                table.append(line)
        elif started:
            break
    return table


def render_summary(results_dir):
    lines = [
        "# Consolidated FAAC Benchmark Summary",
        "",
        "_Roll-up of the per-rate-control reports. Decoder output comparisons and transient analysis are shown in those reports._",
        "",
    ]
    found = False
    for mode in ("abr", "vbr"):
        path = results_dir / f"summary_{mode}.md"
        if not path.is_file():
            lines.extend([f"## {mode.upper()}", "", "No per-rate summary was produced.", ""])
            continue

        found = True
        content = path.read_text(encoding="utf-8").splitlines()
        lines.extend([f"## {mode.upper()}", "", f"**Per-rate result:** {first_heading(content)}", ""])

        executive = section_table(content, "### Executive 3-Pillar Balance")
        if executive:
            lines.extend(["### Executive 3-Pillar Balance", "", *executive, ""])

        details = section_table(content, "### Summary Details")
        if details:
            lines.extend(["### Summary Details", "", *details, ""])

    if not found:
        lines.extend(["No per-rate summary files were available.", ""])
    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    summary = render_summary(args.results_dir)
    args.output.write_text(summary, encoding="utf-8")
    print(summary, end="")


if __name__ == "__main__":
    main()
