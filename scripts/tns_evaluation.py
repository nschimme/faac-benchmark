#!/usr/bin/env python3
"""Run a pinned LC TNS comparison through the benchmark's official scorer."""

import argparse
import csv
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = ROOT / "scripts/tns_evaluation/inputs-48k128.json"
sys.path.insert(0, str(ROOT))

from utils import find_linked_lib, resolve_wrapper_target, safe_run


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def binary_info(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"not a file: {resolved}")
    target = resolve_wrapper_target(str(resolved))
    linked_library = None
    for library_name in ("libfaac", "libfdk-aac"):
        try:
            linked_library = find_linked_lib(str(target or resolved), library_name)
        except Exception:
            linked_library = None
        if linked_library:
            break
    library_path = Path(linked_library).resolve() if linked_library else None
    return {"path": str(resolved), "sha256": sha256(resolved),
            "resolved_cli_target": str(target) if target else None,
            "linked_library": str(library_path) if library_path else None,
            "linked_library_sha256": sha256(library_path) if library_path and library_path.is_file() else None}


def verify_inputs(manifest, data_dir):
    corpus = Path(data_dir).expanduser().resolve() / manifest["corpus"]
    expected = {row["filename"]: row["sha256"] for row in manifest["clips"]}
    actual = {path.name for path in corpus.glob("*.wav")}
    if actual != set(expected):
        missing, extra = sorted(set(expected) - actual), sorted(actual - set(expected))
        raise ValueError(f"corpus clip set differs from pinned manifest; missing={missing[:5]}, extra={extra[:5]}")
    for name, digest in expected.items():
        path = corpus / name
        if sha256(path) != digest:
            raise ValueError(f"input hash mismatch: {path}")
    return corpus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="FAAC baseline executable")
    parser.add_argument("--candidate", required=True, help="FAAC TNS candidate executable")
    parser.add_argument("--fdkaac", required=True, help="fdkaac executable")
    parser.add_argument("--data", required=True, help="Benchmark external data directory")
    parser.add_argument("--out", required=True, help="New or empty evidence output directory")
    parser.add_argument("--inputs", type=Path, default=DEFAULT_INPUTS, help="Pinned input manifest")
    parser.add_argument("--faac-version", nargs=2, default=("baseline", "tns-candidate"), metavar=("BASE", "CAND"))
    args = parser.parse_args()

    out = Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f"output directory must be absent or empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.inputs.read_text())
    corpus = verify_inputs(manifest, args.data)
    bins = {"baseline": binary_info(args.baseline), "candidate": binary_info(args.candidate),
            "fdkaac": binary_info(args.fdkaac)}
    raw = out / "comparison-results.json"
    report = out / "leaderboard.md"
    command = [sys.executable, str(ROOT / "compare_codecs.py"), "--mode", "encoder",
               "--faac-bin", f'{bins["baseline"]["path"]},{bins["candidate"]["path"]}',
               "--faac-bin-version", f"{args.faac_version[0]},{args.faac_version[1]}",
               "--fdkaac-bin", bins["fdkaac"]["path"], "--scenarios", manifest["scenario"],
               "--coverage", "100", "--results-json", str(raw), "--output", str(report),
               "--skip-graphs"]
    env = os.environ.copy()
    env["EXTERNAL_DATA_DIR"] = str(Path(args.data).expanduser().resolve())
    env.setdefault("NUMBA_CACHE_DIR", str(out / "numba-cache"))
    (out / "command.json").write_text(json.dumps({"argv": command, "cwd": str(ROOT)}, indent=2) + "\n")
    os.chdir(ROOT)
    safe_run(command, env=env, capture_output=False)

    rows = json.loads(raw.read_text())
    rows = [row for row in rows if row.get("scenario") == manifest["scenario"]
            and row.get("profile") == "lc" and row.get("decode_valid") is True]
    expected_names = {clip["filename"] for clip in manifest["clips"]}
    indexed = {}
    for row in rows:
        key = (row.get("tool"), row.get("filename"))
        if key in indexed:
            raise ValueError(f"duplicate successful LC row: {key}")
        indexed[key] = row
    encoder_names = sorted({tool for tool, _ in indexed})
    if not any(name.startswith("fdkaac") for name in encoder_names):
        raise ValueError("no successful fdkaac LC rows; check its build and profile support")
    for label in args.faac_version:
        if not any(label in tool for tool in encoder_names):
            raise ValueError(f"no successful FAAC rows for version label {label!r}")
    apples = [name for name in encoder_names if name.startswith("Apple AAC")]
    arm_keys = [name for name in encoder_names if any(label in name for label in args.faac_version)
                or name.startswith("Apple AAC") or name.lower().startswith("fdkaac")]
    per_clip = []
    for name in sorted(expected_names):
        source_hash = next(c["sha256"] for c in manifest["clips"] if c["filename"] == name)
        for arm in arm_keys:
            row = indexed.get((arm, name))
            if row is None:
                per_clip.append({"encoder": arm, "filename": name, "source_sha256": source_hash,
                                 "status": "N/A", "mos": None, "actual_bitrate_kbps": None,
                                 "stereo_ic_error": None, "attack_centroid_ms": None,
                                 "pre_echo_db": None, "pre_echo_coverage": None})
                continue
            per_clip.append({"encoder": arm, "filename": name, "source_sha256": source_hash,
                             "status": "scored", "aac_sha256": sha256(row["aac_path"]),
                             "mos": row.get("mos"), "actual_bitrate_kbps": row.get("actual_bitrate"),
                             "stereo_ic_error": row.get("ic_err"),
                             "attack_centroid_ms": row.get("attack_centroid_ms"),
                             "pre_echo_db": row.get("pre_echo_db"),
                             "pre_echo_coverage": row.get("pre_echo_coverage")})
    provenance = {
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark_commit": safe_run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "python": sys.version, "platform": platform.platform(), "scenario": manifest["scenario"],
        "profile_requested": "LC", "sample_rate_hz": manifest["sample_rate_hz"],
        "channels": manifest["channels"], "target_bitrate_kbps": manifest["target_bitrate_kbps"],
        "input_manifest_sha256": sha256(args.inputs), "input_clip_count": len(manifest["clips"]),
        "encoders": bins, "apple": {"availability": "present" if apples else "unavailable on this host"},
        "metric_versions": {"pre_echo": 1},
        "limits": ["Apple AAC is available only on supported macOS hosts; its system framework provenance is identified by row/version and OS, not a standalone binary hash.",
                   "Pre-echo is N/A when the reference has no eligible quiet-onset events; N/A is not zero.",
                   "MOS, bitrate, stereo, attack, and pre-echo are separate measurements."]}
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    with (out / "per-clip.csv").open("w", newline="") as stream:
        columns = ["encoder", "filename", "source_sha256", "aac_sha256", "status", "mos",
                   "actual_bitrate_kbps", "stereo_ic_error", "attack_centroid_ms",
                   "pre_echo_db", "pre_echo_coverage"]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in per_clip:
            row["attack_centroid_ms"] = json.dumps(row["attack_centroid_ms"], separators=(",", ":")) if row["attack_centroid_ms"] is not None else ""
            row["pre_echo_db"] = json.dumps(row["pre_echo_db"], separators=(",", ":")) if row["pre_echo_db"] is not None else ""
            row["pre_echo_coverage"] = json.dumps(row["pre_echo_coverage"], separators=(",", ":")) if row["pre_echo_coverage"] is not None else ""
            writer.writerow(row)
    print(f"Wrote evidence to {out}")


if __name__ == "__main__":
    main()
