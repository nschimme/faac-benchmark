#!/usr/bin/env python3
"""Compare full-CLI FAAD WAV output on local and user-selected filesystems.

Each input is copied to local scratch before the shared stream-copy workload
preparation. File destinations are decoded to fresh WAVs; PCM identity is
checked after each timed attempt. The optional null destination discards raw
PCM and is reported separately because it does not measure WAV writes.
"""

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codec_bench.decoders import prepared_decode_input
from codec_bench.wav_pcm import wav_pcm_identity
from scripts.decoder_ab import balanced_order, decoder_identity, make_decoder


PROTOCOL_VERSION = 1
MEASURE_TIMEOUT_SECONDS = 600


def positive_finite_seconds(value):
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return seconds


def positive_iterations(value):
    try:
        iterations = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if iterations < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return iterations


def parse_destination(value):
    label, sep, directory = value.partition("=")
    if not sep or not label.strip() or not directory.strip():
        raise argparse.ArgumentTypeError("destination must be LABEL=DIR")
    label = label.strip()
    if label == "dev_null":
        raise argparse.ArgumentTypeError("dev_null is reserved for --include-null")
    return label, Path(directory.strip()).expanduser()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faad", required=True, help="FAAD executable")
    parser.add_argument("--faad-lib", help="Optional explicit libfaad path for identity and loader setup")
    parser.add_argument("--inputs", nargs="+", required=True, type=Path)
    parser.add_argument("--dest", action="append", required=True, type=parse_destination,
                        metavar="LABEL=DIR", help="Existing directory to receive a unique scratch child; repeatable")
    parser.add_argument("--include-null", action="store_true",
                        help="Add a raw PCM /dev/null destination reported separately")
    parser.add_argument("--scratch", type=Path,
                        help="Existing local scratch root; omitted uses an owned TemporaryDirectory")
    parser.add_argument("--json", required=True, type=Path, help="New report path; an existing file is never overwritten")
    parser.add_argument("--iterations", type=positive_iterations, default=5)
    parser.add_argument("--audio-seconds", type=positive_finite_seconds, default=300.0)
    return parser


def validate_arguments(args):
    if not args.dest:
        raise ValueError("at least one --dest is required")
    labels = [label for label, _ in args.dest]
    if len(labels) != len(set(labels)):
        raise ValueError("destination labels must be unique")
    roots = [Path(root).resolve() for _, root in args.dest]
    if len(roots) != len(set(roots)):
        raise ValueError("destination roots must be distinct")
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"destination root must already exist: {root}")
    if args.scratch is not None and not args.scratch.is_dir():
        raise ValueError(f"scratch root must already exist: {args.scratch}")
    if args.iterations < 1 or not math.isfinite(args.audio_seconds) or args.audio_seconds <= 0:
        raise ValueError("iterations and audio-seconds must be positive; audio-seconds must be finite")
    if Path(args.json).exists():
        raise FileExistsError(f"refusing to overwrite existing result: {args.json}")
    for source in args.inputs:
        if not Path(source).is_file():
            raise FileNotFoundError(source)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def input_identity(path):
    path = Path(path).resolve()
    return {"path": str(path), "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path)}


def copy_source_to_local(source, work_dir, source_tag="input"):
    """Always materialize the source locally, including already-long inputs."""
    source = Path(source).resolve()
    work_dir = Path(work_dir).resolve()
    copied = work_dir / f"{source_tag}-{source.name}"
    if copied.exists():
        raise FileExistsError(copied)
    shutil.copy2(source, copied)
    return copied


@contextmanager
def prepare_local_workload(decoder, source, work_dir, audio_seconds, source_tag="input"):
    copied = copy_source_to_local(source, work_dir, source_tag)
    with prepared_decode_input(decoder, str(copied), str(work_dir), audio_seconds) as prepared:
        prepared_path = Path(prepared["path"]).resolve()
        if not prepared_path.is_relative_to(Path(work_dir).resolve()):
            raise RuntimeError(f"prepared workload escaped local scratch: {prepared_path}")
        yield copied, prepared_path, prepared


def _cpu_seconds_delta(before, after):
    return ((after.ru_utime - before.ru_utime) +
            (after.ru_stime - before.ru_stime))


def run_measured_command(command, env=None, runner=subprocess.run, clock=time.perf_counter,
                         resource_module=resource):
    """Measure direct serial child wall and CPU; leave output hashing outside."""
    before = (resource_module.getrusage(resource_module.RUSAGE_CHILDREN)
              if resource_module is not None else None)
    started = clock()
    proc = runner(command, capture_output=True, env=env, timeout=MEASURE_TIMEOUT_SECONDS,
                  check=False)
    wall_ms = (clock() - started) * 1000.0
    cpu_ms = None
    if resource_module is not None:
        after = resource_module.getrusage(resource_module.RUSAGE_CHILDREN)
        cpu_ms = _cpu_seconds_delta(before, after) * 1000.0
    return proc, wall_ms, cpu_ms


def _decode_command(decoder, input_path, output_path):
    return list(decoder.get_decode_cmd(str(input_path), str(output_path)))


def _null_command(decoder, input_path):
    command = _decode_command(decoder, input_path, os.devnull)
    output_index = command.index("-o")
    command[output_index:output_index] = ["--overwrite", "-f", "raw"]
    return command


def measure_attempt(decoder, input_path, scratch_dir, is_null=False):
    """Run one direct child decode, then inspect/hash output after timing."""
    with tempfile.TemporaryDirectory(prefix="attempt-", dir=scratch_dir) as attempt_dir:
        if is_null:
            command = _null_command(decoder, input_path)
            proc, wall_ms, cpu_ms = run_measured_command(command, env=decoder.get_run_env() or None)
            if proc.returncode:
                raise RuntimeError(f"raw /dev/null decode failed ({proc.returncode}): {proc.stderr!r}")
            return {"mode": "raw_dev_null", "command": command,
                    "wall_ms": wall_ms, "cpu_ms": cpu_ms,
                    "peak_rss_kb": None, "pcm_identity": None,
                    "output_path": os.devnull,
                    "pcm_validation": "raw output discarded; success status only",
                    "peak_rss_note": "not collected; per-process RSS is unavailable from the direct child timer"}

        wall_out = Path(attempt_dir) / "decoded.wav"
        command = _decode_command(decoder, input_path, wall_out)
        proc, wall_ms, cpu_ms = run_measured_command(command, env=decoder.get_run_env() or None)
        if proc.returncode or not wall_out.is_file():
            raise RuntimeError(f"timed decode failed ({proc.returncode}): {proc.stderr!r}")
        pcm = wav_pcm_identity(wall_out)
        if not pcm["pcm_bytes"]:
            raise RuntimeError("decoded WAV contains no PCM")
        return {"mode": "wav_file", "command": command,
                "wall_ms": wall_ms, "cpu_ms": cpu_ms,
                "peak_rss_kb": None,
                "wav_sha256": sha256_file(wall_out),
                "wav_bytes": wall_out.stat().st_size,
                "pcm_identity": pcm,
                "output_path": str(wall_out),
                "pcm_validation": "RIFF/RF64 format and PCM SHA-256 checked after timing",
                "peak_rss_note": "not collected; per-process RSS is unavailable from the direct child timer"}


def same_pcm(a, b):
    left, right = a.get("pcm_identity"), b.get("pcm_identity")
    if not left or not right:
        return False
    return (left["format"] == right["format"] and
            left["pcm_bytes"] == right["pcm_bytes"] and
            left["pcm_sha256"] == right["pcm_sha256"])


def _write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    os.replace(temporary, path)


def _check_round_pcm(round_record, file_destinations):
    if len(file_destinations) < 2:
        round_record["pcm_matches"] = None
        return
    first_label = file_destinations[0]
    baseline = round_record["results"][first_label]
    comparisons = {}
    for label in file_destinations[1:]:
        candidate = round_record["results"][label]
        comparisons[label] = same_pcm(baseline, candidate)
    round_record["pcm_matches"] = {first_label: True, **comparisons}
    if not all(comparisons.values()):
        raise RuntimeError(f"decoded PCM mismatch across file destinations: {comparisons}")


def run(args):
    validate_arguments(args)
    result_path = Path(args.json).resolve()
    if result_path.exists():
        raise FileExistsError(f"refusing to overwrite existing result: {result_path}")
    decoder = make_decoder("storage", str(Path(args.faad).resolve()),
                           lib=str(Path(args.faad_lib).resolve()) if args.faad_lib else None)
    identity = decoder_identity(decoder, args.faad_lib)
    destinations = [(label, Path(root).resolve()) for label, root in args.dest]
    if args.include_null:
        destinations.append(("dev_null", None))
    report = {
        "status": "running",
        "protocol": {"name": "faad_storage_ab", "version": PROTOCOL_VERSION,
                     "warmups_per_destination": 1, "iterations": args.iterations,
                     "audio_seconds": args.audio_seconds, "workers": 1,
                     "order": "serial rotating destination order",
                     "hash_interval": "PCM and WAV hashes are computed after timing",
                     "cpu_measurement": "direct-child RUSAGE_CHILDREN user+system delta",
                     "peak_rss": "not collected; no per-invocation value is available from this direct timer"},
        "platform": platform.platform(),
        "decoder": identity,
        "faad_executable_path": str(Path(args.faad).resolve()),
        "faad_library_path": str(Path(args.faad_lib).resolve()) if args.faad_lib else None,
        "include_null": args.include_null,
        "null_device": os.devnull if args.include_null else None,
        "destination_roots": {label: str(root) for label, root in destinations},
        "scratch_root": str(Path(args.scratch).resolve()) if args.scratch else "TemporaryDirectory",
        "inputs": [],
    }
    _write_report(result_path, report)
    try:
        with ExitStack() as stack:
            if args.scratch:
                scratch_root = Path(stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="faad-storage-local-", dir=args.scratch)))
            else:
                scratch_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="faad-storage-local-")))
            report["scratch_path"] = str(scratch_root.resolve())
            destination_scratch = {}
            for label, root in destinations:
                if label == "dev_null":
                    continue
                path = Path(stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="faad-storage-", dir=root)))
                destination_scratch[label] = path
            report["destination_scratch_paths"] = {label: str(path.resolve())
                                                   for label, path in destination_scratch.items()}
            _write_report(result_path, report)

            names = [label for label, _ in destinations]
            file_destinations = [label for label, _ in destinations if label != "dev_null"]
            for input_index, source_arg in enumerate(args.inputs):
                source = Path(source_arg).resolve()
                input_record = {"source": input_identity(source), "runs": []}
                report["inputs"].append(input_record)
                with prepare_local_workload(decoder, source, scratch_root, args.audio_seconds,
                                            source_tag=f"input-{input_index}") as prepared_tuple:
                    copied, prepared_path, prepared = prepared_tuple
                    input_record["local_copy"] = input_identity(copied)
                    input_record["prepared_input"] = input_identity(prepared_path)
                    input_record["preparation"] = {key: value for key, value in prepared.items()
                                                    if key != "path"}
                    _write_report(result_path, report)

                    warmup = {"order": names[:], "results": {}}
                    input_record["warmup"] = warmup
                    for label in names:
                        is_null = label == "dev_null"
                        output_root = scratch_root if is_null else destination_scratch[label]
                        warmup["results"][label] = measure_attempt(decoder, prepared_path,
                                                                   output_root, is_null=is_null)
                        _write_report(result_path, report)
                    _check_round_pcm(warmup, file_destinations)

                    for round_index in range(args.iterations):
                        order = balanced_order(names, round_index)
                        row = {"round": round_index + 1, "order": order, "results": {}}
                        input_record["runs"].append(row)
                        for label in order:
                            is_null = label == "dev_null"
                            output_root = scratch_root if is_null else destination_scratch[label]
                            row["results"][label] = measure_attempt(decoder, prepared_path,
                                                                    output_root, is_null=is_null)
                            _write_report(result_path, report)
                        _check_round_pcm(row, file_destinations)
                        samples = {label: [run["results"][label] for run in input_record["runs"]]
                                   for label in names}
                        input_record["summary"] = {
                            label: {"wall_ms_median": statistics.median(x["wall_ms"] for x in values),
                                    "cpu_ms_median": statistics.median(
                                        [x["cpu_ms"] for x in values if x["cpu_ms"] is not None])
                                    if any(x["cpu_ms"] is not None for x in values) else None,
                                    "peak_rss_kb_median": None}
                            for label, values in samples.items()}
                        _write_report(result_path, report)
            report["status"] = "complete"
            _write_report(result_path, report)
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_report(result_path, report)
        raise
    return report


def main(argv=None):
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
