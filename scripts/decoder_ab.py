#!/usr/bin/env python3
"""Paired full-CLI FAAD decoder comparison on original AAC inputs."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codec_bench.decoders import (FAADDecoder, prepared_decode_input, time_decode_once,
                                  resolved_decoder_library, timing_file_identity)
from utils import resolve_wrapper_target, safe_run
from codec_bench.wav_pcm import wav_pcm_identity


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256_file(path)}


def make_decoder(label, binary, lib=None):
    return FAADDecoder(f"FAAD3 {label}", str(binary), tool_id=f"faad3_{label}",
                       lib_override=str(lib) if lib else None, is_faad3=True)


def decoder_identity(decoder, explicit_lib=None):
    binary = Path(resolve_wrapper_target(decoder.binary_path)).resolve()
    lib = explicit_lib or resolved_decoder_library(decoder)
    return {
        "name": decoder.name,
        "tool_id": decoder.tool_id,
        "binary": file_identity(decoder.binary_path),
        "target_binary": file_identity(binary),
        "libfaad": file_identity(lib) if lib and Path(lib).is_file() else None,
        "library_resolution": "resolved" if lib else "unresolved or statically linked",
    }


def decode_command(decoder, input_path, output_path, bits):
    command = list(decoder.get_decode_cmd(str(input_path), str(output_path)))
    try:
        output_index = command.index("-o")
    except ValueError as exc:
        raise ValueError(f"FAAD decoder command has no -o option: {command}") from exc
    command[output_index:output_index] = ["-b", bits]
    return command


def file_sha256(path):
    return sha256_file(path)


def preflight_pcm(decoders, input_paths, output_dir, bit_depths):
    """Decode original inputs at each PCM depth and require byte identity."""
    results = []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for input_index, source in enumerate(input_paths):
        for bits in bit_depths:
            outputs = {}
            output_paths = []
            try:
                for decoder in decoders:
                    output = output_dir / f"preflight-{input_index}-{decoder.tool_id}-{bits}.wav"
                    if output.exists():
                        output.unlink()
                    output_paths.append(output)
                    command = decode_command(decoder, source, output, bits)
                    proc = safe_run(command, env=decoder.get_run_env() or None,
                                    capture_output=True, check=False)
                    if proc.returncode != 0 or not output.is_file():
                        detail = proc.stderr.decode(errors="replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
                        raise RuntimeError(f"{decoder.name} failed {bits}-bit preflight for {source}: "
                                           f"exit={proc.returncode}; {detail[-1000:]}")
                    outputs[decoder.tool_id] = {
                        "path": output,
                        "sha256": file_sha256(output),
                        "bytes": output.stat().st_size,
                        "command": command,
                        **wav_pcm_identity(output),
                    }

                baseline = outputs[decoders[0].tool_id]
                for decoder in decoders[1:]:
                    candidate = outputs[decoder.tool_id]
                    same = (baseline["format"] == candidate["format"] and
                            baseline["pcm_bytes"] == candidate["pcm_bytes"] and
                            baseline["pcm_sha256"] == candidate["pcm_sha256"])
                    if not same:
                        raise RuntimeError(f"PCM mismatch at {bits}-bit for {source}: "
                                           f"{decoders[0].name}={baseline['sha256']} "
                                           f"{decoder.name}={candidate['sha256']}")
                results.append({
                    "input": str(source),
                    "bits": bits,
                    "byte_identical": True,
                    "outputs": {
                        name: {k: str(v) if k == "path" else v for k, v in output.items()}
                        for name, output in outputs.items()
                    },
                })
            finally:
                for path in output_paths:
                    try:
                        path.unlink()
                    except OSError:
                        pass
    return results


def balanced_order(names, round_index):
    """Rotate decoder positions by round for balanced serial interleaving."""
    offset = round_index % len(names)
    return names[offset:] + names[:offset]


def _valid_ms(result):
    if not isinstance(result, dict):
        return None
    try:
        value = float(result["milliseconds"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def summarize_trials(samples):
    values = [v for v in samples if v is not None]
    if not values:
        return {"attempts": len(samples), "valid_attempts": 0}
    return {
        "attempts": len(samples),
        "valid_attempts": len(values),
        "samples_ms": values,
        "median_ms": statistics.median(values),
        "best_ms": min(values),
        "mean_ms": statistics.mean(values),
        "std_ms": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def paired_gain(baseline, candidate):
    pairs = []
    for base, cand in zip(baseline, candidate):
        if base is not None and cand is not None and base > 0:
            pairs.append((base / cand - 1.0) * 100.0)
    if not pairs:
        return {"paired_rounds": 0, "speedup_pct_median": None}
    return {
        "paired_rounds": len(pairs),
        "speedup_pct_per_round": pairs,
        "speedup_pct_median": statistics.median(pairs),
        "speedup_pct_min": min(pairs),
        "speedup_pct_max": max(pairs),
    }


def run_interleaved_trials(decoders, prepared_by_name, output_dir, iterations, measure_once=None):
    """Warm each decoder once, then retain every serial interleaved attempt."""
    measure_once = measure_once or time_decode_once
    names = [decoder.tool_id for decoder in decoders]
    by_name = {decoder.tool_id: decoder for decoder in decoders}
    warmups = {}
    for name in names:
        try:
            warmups[name] = measure_once(by_name[name], prepared_by_name[name]["path"], output_dir)
        except Exception as exc:  # retain the failed attempt and continue to the paired rounds
            warmups[name] = {"error": f"{type(exc).__name__}: {exc}"}

    expected_info = warmups.get(names[0], {}).get("output_info")
    attempts = {name: [] for name in names}
    rounds = []
    for round_index in range(iterations):
        order = balanced_order(names, round_index)
        round_attempts = {}
        for name in order:
            try:
                result = measure_once(by_name[name], prepared_by_name[name]["path"], output_dir)
                if not isinstance(result, dict) or _valid_ms(result) is None:
                    raise RuntimeError(f"invalid timing result: {result!r}")
                if expected_info is not None and result.get("output_info") != expected_info:
                    raise RuntimeError("Timed output format/frame count differs from baseline")
                round_attempts[name] = {"ok": True, **result}
                attempts[name].append(result)
            except Exception as exc:
                failed = {"error": f"{type(exc).__name__}: {exc}"}
                round_attempts[name] = {"ok": False, **failed}
                attempts[name].append(None)
        rounds.append({"round": round_index + 1, "order": order, "attempts": round_attempts})

    summaries = {
        name: summarize_trials([_valid_ms(r) if r is not None else None for r in rows])
        for name, rows in attempts.items()
    }
    paired = {}
    if len(names) >= 2:
        paired[f"{names[1]}_vs_{names[0]}"] = paired_gain(
            [_valid_ms(r) if r is not None else None for r in attempts[names[0]]],
            [_valid_ms(r) if r is not None else None for r in attempts[names[1]]])
    if len(names) == 3:
        paired[f"{names[2]}_vs_{names[0]}"] = paired_gain(
            [_valid_ms(r) if r is not None else None for r in attempts[names[0]]],
            [_valid_ms(r) if r is not None else None for r in attempts[names[2]]])
        paired[f"{names[1]}_vs_{names[2]}"] = paired_gain(
            [_valid_ms(r) if r is not None else None for r in attempts[names[2]]],
            [_valid_ms(r) if r is not None else None for r in attempts[names[1]]])
    return {"warmups": warmups, "rounds": rounds, "summary": summaries, "paired": paired}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Baseline FAAD3 executable")
    parser.add_argument("--candidate", required=True, help="Candidate FAAD3 executable")
    parser.add_argument("--control", help="Optional control FAAD3 executable")
    parser.add_argument("--baseline-lib", help="Explicit baseline libfaad path")
    parser.add_argument("--candidate-lib", help="Explicit candidate libfaad path")
    parser.add_argument("--control-lib", help="Explicit control libfaad path")
    parser.add_argument("--inputs", nargs="+", required=True, help="Original AAC/MP4 inputs")
    parser.add_argument("--iterations", type=int, default=7, help="Paired serial rounds per input")
    parser.add_argument("--audio-seconds", type=float, default=600,
                        help="Target duration for the shared prepared decode input")
    parser.add_argument("--bits", nargs="+", choices=("16", "32f"), default=("16", "32f"),
                        help="PCM depths required to match byte-for-byte before timing")
    parser.add_argument("--json", required=True, help="Output JSON path")
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if not (0 < args.audio_seconds < float("inf")):
        parser.error("--audio-seconds must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    input_paths = [Path(p).resolve() for p in args.inputs]
    missing = [str(p) for p in input_paths if not p.is_file()]
    if missing:
        raise FileNotFoundError("missing AAC input(s): " + ", ".join(missing))

    specs = [("baseline", args.baseline, args.baseline_lib),
             ("candidate", args.candidate, args.candidate_lib)]
    if args.control:
        specs.append(("control", args.control, args.control_lib))
    decoders = []
    identities = {}
    for label, binary, lib in specs:
        if not Path(binary).is_file():
            raise FileNotFoundError(f"{label} executable not found: {binary}")
        if lib and not Path(lib).is_file():
            raise FileNotFoundError(f"{label} libfaad not found: {lib}")
        decoder = make_decoder(label, binary, lib)
        decoders.append(decoder)
        identities[decoder.tool_id] = decoder_identity(decoder, lib)

    output_json = Path(args.json).resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output_json.parent / (output_json.stem + "-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        preflight = preflight_pcm(decoders, input_paths, work_dir / "preflight", args.bits)
    except Exception as exc:
        failure_report = {
            "schema": "faad-decoder-ab-v1",
            "platform": {"system": platform.platform(), "machine": platform.machine(),
                         "python": sys.version},
            "settings": {"iterations": args.iterations, "audio_seconds": args.audio_seconds,
                         "preflight_bits": args.bits},
            "decoders": identities,
            "preflight": {"byte_identical": False, "error": f"{type(exc).__name__}: {exc}"},
            "inputs": [],
            "timing_started": False,
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(failure_report, f, indent=2)
        raise SystemExit(f"PCM preflight failed; details saved to {output_json}: {exc}") from exc

    report = {
        "schema": "faad-decoder-ab-v1",
        "platform": {"system": platform.platform(), "machine": platform.machine(),
                     "python": sys.version},
        "settings": {"iterations": args.iterations, "audio_seconds": args.audio_seconds,
                     "preflight_bits": args.bits},
        "decoders": identities,
        "preflight": preflight,
        "inputs": [],
        "interpretation": "Positive paired percentage is throughput gain: (baseline_ms / candidate_ms - 1) * 100. "
                          "Control results describe run-to-run noise; no statistical significance claim is made.",
    }

    for input_path in input_paths:
        with prepared_decode_input(decoders[0], str(input_path), str(work_dir), args.audio_seconds) as workload:
            prepared = {decoder.tool_id: dict(workload) for decoder in decoders}
            result = run_interleaved_trials(decoders, prepared, str(work_dir), args.iterations)
            report["inputs"].append({
                "source": file_identity(input_path),
                "timing_input": file_identity(workload["path"]),
                "prepared": prepared,
                **result,
            })

    failed = any("error" in sample for item in report["inputs"] for sample in item["warmups"].values()) or any(not attempt["ok"] for item in report["inputs"] for round_ in item["rounds"]
                 for attempt in round_["attempts"].values())
    report["status"] = "failed" if failed else "completed"
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, allow_nan=False)
    print(f"Wrote paired decoder results to {output_json}")
    if failed:
        raise SystemExit("One or more timed decodes failed; see the retained attempt records")


if __name__ == "__main__":
    main()
