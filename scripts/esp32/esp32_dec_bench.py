#!/usr/bin/env python3
"""On-device AAC decoder benchmark (libhelix vs FAAD2, ...) on ESP32 / S3 / C6 boards.

Run from the repo root; see docs/scripts.md and scripts/esp32/firmware_dec/README.md.

    scripts/esp32/setup_idf.sh                                   # once: pinned ESP-IDF
    python3 scripts/esp32/esp32_dec_bench.py build --target esp32s3
    python3 scripts/esp32/esp32_dec_bench.py run   --target esp32s3 --port /dev/serial/by-id/... --gate
    python3 scripts/esp32/esp32_dec_bench.py report --target esp32s3
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

FIRMWARE = os.path.join(HERE, "firmware_dec")
TOOLCHAIN = os.path.join(HERE, ".toolchain")
RESULTS_DIR = os.path.join(ROOT, "results")

# codec id -> (display name, build switches). Add FAAD3 / others here.
CODECS = {
    "helix": "libhelix-aac (fixed point)",
    "faad2": "FAAD2 (FIXED_POINT)",
    "faad3": "FAAD3 (float, stack-tests)",
}
# "<codec>-psram" builds the same decoder with its state allocated from PSRAM (S3 only).
for _id in list(CODECS):
    CODECS[_id + "-psram"] = CODECS[_id] + ", state in PSRAM"
BASE_CODECS = [c for c in CODECS if not c.endswith("-psram")]
STREAM_AUTO = ("esp32", "esp32c6")  # no PSRAM: the host feeds the clip in blocks (firmware FLAG_STREAM)
CPU_MHZ = {"esp32": 240, "esp32s3": 240, "esp32c6": 160}
# Largest clip the target can hold (S3 has 8 MB PSRAM; the others use internal RAM).
STACK_BYTES = 65536  # firmware main task stack (sdkconfig.defaults)
MAX_CLIP_BYTES = {"esp32": 90_000, "esp32s3": 1_500_000, "esp32c6": 90_000}


def build_dir(codec, target):
    return os.path.join("build", f"{codec}-{target}")


def idf(cmd, cwd=FIRMWARE):
    """Run a shell command with the pinned ESP-IDF environment active."""
    env_cmd = (f"export IDF_TOOLS_PATH={shlex.quote(os.path.join(TOOLCHAIN, 'tools'))} && "
               f". {shlex.quote(os.path.join(TOOLCHAIN, 'esp-idf', 'export.sh'))} >/dev/null 2>&1 && {cmd}")
    return subprocess.run(["bash", "-c", env_cmd], cwd=cwd, capture_output=True, text=True)


def results_path(target):
    return os.path.join(RESULTS_DIR, f"esp32_dec_{target}.json")


def load_results(target):
    try:
        with open(results_path(target)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"target": target, "runs": [], "footprint": {}}


def save_results(target, data):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(results_path(target), "w") as f:
        json.dump(data, f, indent=1)


# ---------------------------------------------------------------- build / flash

# Codec ids that are another configuration of a firmware codec: id -> (firmware codec, extra -D flags).
VARIANTS = {
    # stack-tests has the split allocations (PR #36) and only faad_decoder_open(); PS is built in (HE-AAC v2).
    "faad3": ("faad3", "-DCODEC_FAAD3_OPEN=1 -DCODEC_PS=1"),
}


def idf_args(codec, target, sbr):
    b = build_dir(codec, target)
    base, extra = VARIANTS.get(codec.removesuffix("-psram"), (codec.removesuffix("-psram"), ""))
    return (f"-B {b} -DSDKCONFIG={b}/sdkconfig -DCODEC={base} -DCODEC_SBR={sbr} "
            f"-DCODEC_PSRAM={1 if codec.endswith('-psram') else 0} {extra}")


def footprint(codec, target):
    """Flash (text+rodata+data) and static RAM of the codec adapter's archive, from the IDF map."""
    res = idf(f"python -m esp_idf_size --archives --format json {build_dir(codec, target)}/aac_bench.map")
    if res.returncode:
        return None
    try:
        arch = json.loads(res.stdout[res.stdout.index("{"):])
    except ValueError:
        return None
    base = VARIANTS.get(codec.removesuffix("-psram"), (codec.removesuffix("-psram"),))[0]
    name = next((k for k in arch if k.startswith(f"libcodec_{base}")), None)
    return arch[name] if name else None


def cmd_build(args):
    data = load_results(args.target)
    for codec in args.codec:
        b = idf_args(codec, args.target, args.sbr)
        for step in (f"idf.py {b} set-target {args.target}", f"idf.py {b} build"):
            res = idf(step)
            if res.returncode:
                tail = (res.stdout + res.stderr).splitlines()[-25:]
                sys.exit(f"{codec}/{args.target}: `{step}` failed\n" + "\n".join(tail))
        data["footprint"][codec] = footprint(codec, args.target)
        print(f"built {codec} for {args.target}: footprint {data['footprint'][codec]}")
    save_results(args.target, data)


def flash(codec, target, port, sbr):
    res = idf(f"idf.py {idf_args(codec, target, sbr)} -p {shlex.quote(port)} flash")
    if res.returncode:
        sys.exit(f"flash {codec}/{target} failed:\n" + "\n".join((res.stdout + res.stderr).splitlines()[-15:]))


# ---------------------------------------------------------------- bitstreams

def demux_adts(src, dst):
    """The firmware takes raw ADTS; the encoder-phase outputs are M4A."""
    if src.lower().endswith((".aac", ".adts")):
        with open(src, "rb") as f, open(dst, "wb") as g:
            g.write(f.read())
        return True
    res = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-c:a", "copy", "-f", "adts", dst],
                         capture_output=True)
    return res.returncode == 0 and os.path.getsize(dst) > 0


def select_clips(args):
    """Encoder-phase rows to benchmark: one bitstream per (scenario, clip) of the chosen encoder."""
    import config
    with open(args.bitstreams) as f:
        rows = json.load(f)
    gate = {name: set(config.GATE_CLIPS.get(name) or []) for name in config.SCENARIOS}
    want = set(args.scenarios.split(",")) if args.scenarios else None
    out = []
    for r in rows:
        if not r.get("decode_valid") or not r.get("aac_path") or not os.path.exists(r["aac_path"]):
            continue
        if args.encoder not in r.get("row_key", "") or r.get("profile") not in args.profiles:
            continue
        if want and r["scenario"] not in want:
            continue
        if args.gate and r["filename"] not in gate.get(r["scenario"], set()):
            continue
        out.append(r)
    out.sort(key=lambda r: (r["scenario"], r["filename"], r["profile"]))
    return out[: args.limit] if args.limit else out


# ---------------------------------------------------------------- verification

def snr_vs_ffmpeg(adts_path, pcm, channels, rate):
    """SNR of device PCM vs ffmpeg's decode, searching whole-frame alignment offsets."""
    import numpy as np
    ref = subprocess.run(["ffmpeg", "-v", "error", "-i", adts_path, "-f", "s16le", "-ac", str(channels),
                          "-ar", str(rate), "-"], capture_output=True).stdout
    dev = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    ref = np.frombuffer(ref, dtype="<i2").astype(np.float64)
    if min(len(dev), len(ref)) < 8192:
        return None
    # Decoders differ in priming/SBR delay by arbitrary sample counts, so find the lag by
    # cross-correlating a mono mix of the first seconds instead of assuming whole frames.
    n = min(len(dev), len(ref), 1 << 18)
    mono = lambda x: x[: n - n % channels].reshape(-1, channels).mean(axis=1)
    d, r = mono(dev), mono(ref)
    size = 1 << (2 * len(d)).bit_length()
    xc = np.fft.irfft(np.fft.rfft(r, size) * np.conj(np.fft.rfft(d, size)), size)
    lags = np.concatenate([xc[:8192], xc[-8192:]])
    lag = int(np.argmax(lags))
    lag = lag if lag < 8192 else lag - 8192 - 8192  # negative lags wrap to the end
    off = lag * channels
    a, b = (dev, ref[off:]) if off >= 0 else (dev[-off:], ref)
    m = min(len(a), len(b))
    a, b = a[:m], b[:m]
    err = float(np.sum((a - b) ** 2))
    return float(10 * np.log10(max(float(np.sum(b ** 2)), 1.0) / max(err, 1.0)))


# ---------------------------------------------------------------- run

SKIPPED = -2  # status of a bitstream the host did not send (too big for the target, demux failed)


def run_key(r):
    return (r["codec"], r["row_key"], r["scenario"], r["filename"])


def outcome_lines(runs):
    """Per codec/profile count of every outcome, so the limits of a target/variant show in the report."""
    lines = ["", "## Outcomes (every bitstream tried, including skipped and failed)", "",
             "| Codec | Profile | Bitstreams | ok | Not ok (reason: count) |", "|---|---|---|---|---|"]
    for codec in sorted({r["codec"] for r in runs}):
        for prof in sorted({r["profile"] for r in runs if r["codec"] == codec}):
            rs = [r for r in runs if r["codec"] == codec and r["profile"] == prof]
            bad = {}
            for r in rs:
                if r["status"] != 0:
                    t = r.get("status_text", f"status {r['status']}")
                    bad[t] = bad.get(t, 0) + 1
            lines.append(f"| {CODECS.get(codec, codec)} | {prof} | {len(rs)} | {sum(r['status'] == 0 for r in rs)} | "
                         f"{', '.join(f'{k}: {v}' for k, v in sorted(bad.items())) or '-'} |")
    return lines

def use_stream(args):
    """--stream auto streams on the targets without PSRAM, where the clip would not fit next to the decoder."""
    return args.stream == "on" or (args.stream == "auto" and args.target in STREAM_AUTO)


def cmd_run(args):
    from esp32_dec_device import Device
    data = load_results(args.target)
    def wants_verify(scenario, filename, profile):
        return bool(args.verify) and zlib.crc32(f"{scenario}/{filename}/{profile}".encode()) % args.verify == 0

    done = {(r["codec"], r["row_key"], r["scenario"], r["filename"]) for r in data["runs"]
            if r["status"] == 0 and (r.get("snr_db") is not None or not wants_verify(r["scenario"], r["filename"], r["profile"]))}
    clips = select_clips(args)
    stream = use_stream(args)
    mhz = CPU_MHZ[args.target]
    print(f"{len(clips)} bitstreams, target {args.target} @ {mhz} MHz, "
          f"{'streamed' if stream else 'resident'} clips")
    for codec in args.codec:
        todo = [r for r in clips if (codec, r["row_key"], r["scenario"], r["filename"]) not in done]
        if not todo:
            print(f"{codec}: nothing left")
            continue
        flash(codec, args.target, args.port, args.sbr)
        dev = Device(args.port, cpu_mhz=mhz)
        dev.reset()
        with tempfile.TemporaryDirectory() as td:
            for n, r in enumerate(todo, 1):
                adts = os.path.join(td, "clip.aac")
                base = {"codec": codec, "target": args.target, "row_key": r["row_key"], "profile": r["profile"],
                        "scenario": r["scenario"], "filename": r["filename"], "audio_duration": r.get("audio_duration"),
                        "cpu_mhz": mhz, "loops": args.loops, "stream": stream}
                limit = args.max_bytes or MAX_CLIP_BYTES[args.target]
                clip = open(adts, "rb").read() if demux_adts(r["aac_path"], adts) else None
                skip = ("demux failed" if clip is None else f"clip over {limit} B" if not stream and len(clip) > limit else None)
                if skip:  # recorded, so the report can show the limits of a target/variant
                    print(f"  skip ({skip}): {r['scenario']} {r['filename']}")
                    row = {**base, "clip_bytes": len(clip or b""), "status": SKIPPED, "status_text": "skipped: " + skip}
                    data["runs"] = [x for x in data["runs"] if run_key(x) != run_key(row)] + [row]
                    save_results(args.target, data)
                    continue
                want_pcm = wants_verify(r["scenario"], r["filename"], r["profile"])
                try:
                    send = dev.decode_stream if stream else dev.decode
                    out = send(clip, loops=args.loops, want_pcm=want_pcm)
                    res, pcm = out if isinstance(out, tuple) else (out, None)  # no PCM when the device reports an error
                except Exception as exc:
                    print(f"  device error ({exc}); resetting")
                    dev.reset()
                    res, pcm = {"status": -1, "status_text": str(exc)}, None
                row = {**base, "clip_bytes": len(clip), **res}
                if pcm is not None and res["status"] == 0:
                    row["snr_db"] = snr_vs_ffmpeg(adts, pcm, res["out_channels"], res["out_rate"])
                data["runs"] = [x for x in data["runs"] if run_key(x) != run_key(row)] + [row]
                save_results(args.target, data)
                ms = res.get("best_cycles", 0) / (mhz * 1000)
                dur = r.get("audio_duration") or 0
                print(f"  [{n}/{len(todo)}] {codec} {r['scenario']} {r['filename']} "
                      f"{res['status_text']} {ms:.0f} ms ({dur / (ms / 1000):.1f}x RT)" if ms and dur
                      else f"  [{n}/{len(todo)}] {codec} {r['scenario']} {r['filename']} {res['status_text']}")
        dev.close()


# ---------------------------------------------------------------- report

def cmd_report(args):
    data = load_results(args.target)
    runs = [r for r in data["runs"] if r["status"] == 0 and r.get("audio_duration")]
    codecs = sorted({r["codec"] for r in runs})
    key = lambda r: (r["row_key"], r["scenario"], r["filename"])
    common = set.intersection(*[{key(r) for r in runs if r["codec"] == c} for c in codecs]) if codecs else set()
    mhz = CPU_MHZ[args.target]
    lines = [f"# {args.target} @ {mhz} MHz: decoders on {len(common)} common bitstreams", "",
             "| Codec | Profile | Clips | Mean xRT | Min xRT | MHz for 1x RT (mean) | Heap used (KB) | Stack used (KB) | SNR vs ffmpeg (dB) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for codec in codecs:
        for prof in sorted({r["profile"] for r in runs}):
            rs = [r for r in runs if r["codec"] == codec and r["profile"] == prof and key(r) in common]
            if not rs:
                continue
            xrt = [r["audio_duration"] / (r["best_cycles"] / (mhz * 1e6)) for r in rs]
            need = [r["best_cycles"] / r["audio_duration"] / 1e6 for r in rs]
            snr = [r["snr_db"] for r in rs if r.get("snr_db") is not None]
            lines.append(f"| {CODECS.get(codec, codec)} | {prof} | {len(rs)} | {sum(xrt) / len(xrt):.1f} | {min(xrt):.1f} | "
                         f"{sum(need) / len(need):.1f} | {max(r['heap_min_free_delta'] for r in rs) / 1024:.0f} | "
                         f"{max(STACK_BYTES - r['stack_hwm'] for r in rs) / 1024:.0f} | "
                         f"{(f'{min(snr):.1f}..{max(snr):.1f}' if snr else 'n/a')} |")
    lines += outcome_lines(data["runs"])
    if data.get("footprint"):
        lines += ["", "## Footprint (codec archive, from the IDF map)", ""]
        for codec, fp in data["footprint"].items():
            lines.append(f"- {CODECS.get(codec, codec)}: {fp}")
    text = "\n".join(lines)
    print(text)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--target", default="esp32s3", choices=sorted(CPU_MHZ))
        p.add_argument("--codec", type=lambda s: s.split(","), default=BASE_CODECS,
                       help="comma list from: " + ",".join(CODECS))
        p.add_argument("--sbr", type=int, default=1, choices=(0, 1), help="build with SBR (HE-AAC v1) support")

    b = sub.add_parser("build", help="build firmware per codec")
    common(b)
    b.set_defaults(fn=cmd_build)

    r = sub.add_parser("run", help="flash and benchmark")
    common(r)
    r.add_argument("--port", required=True)
    r.add_argument("--stream", choices=("auto", "on", "off"), default="auto",
                   help="feed the clip to the device in blocks instead of holding it (auto: chips without PSRAM)")
    r.add_argument("--bitstreams", default=os.path.join(ROOT, "comparison_results.json"),
                   help="encoder-phase results JSON listing the bitstreams")
    r.add_argument("--encoder", default="faac", help="substring of the encoder row_key to take bitstreams from")
    r.add_argument("--profiles", type=lambda s: s.split(","), default=["lc", "he"])
    r.add_argument("--scenarios")
    r.add_argument("--gate", action="store_true", help="only the fixed gate clips")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--max-bytes", type=int, default=0, help="skip clips larger than this (default per target)")
    r.add_argument("--loops", type=int, default=3, help="decode passes per clip; best is reported")
    r.add_argument("--verify", type=int, default=0, help="return PCM and SNR-check every Nth clip (0 = never)")
    r.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="markdown summary")
    p.add_argument("--target", default="esp32s3", choices=sorted(CPU_MHZ))
    p.add_argument("--output")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
