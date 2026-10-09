#!/usr/bin/env python3
"""On-device FAAC *encoder* benchmark on ESP32 / S3 / C6 boards (work in progress).

Separate from esp32_bench.py (the decoder tool): own firmware (scripts/esp32/firmware_enc/),
own protocol, own results file (results/esp32_enc_<target>.json). Run from the repo root;
see docs/esp32.md and scripts/esp32/firmware_enc/README.md.

    scripts/esp32/setup_idf.sh                                   # once: pinned ESP-IDF
    python3 scripts/esp32/esp32_enc_bench.py build  --target esp32s3
    python3 scripts/esp32/esp32_enc_bench.py run    --target esp32c6 --port /dev/serial/by-id/... --gate
    python3 scripts/esp32/esp32_enc_bench.py report --target esp32c6
"""
import argparse
import json
import os
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

FIRMWARE = os.path.join(HERE, "firmware_enc")
TOOLCHAIN = os.path.join(HERE, ".toolchain")
RESULTS_DIR = os.path.join(ROOT, "results")

CPU_MHZ = {"esp32": 240, "esp32s3": 240, "esp32c6": 160}
STACK_BYTES = 32768  # firmware main task stack (firmware_enc/sdkconfig.defaults)
# Input PCM the target can hold next to the encoder state (S3: 8 MB PSRAM; the others use
# internal RAM, where the state of the 2-channel encoder takes the bulk).
MAX_PCM_BYTES = {"esp32": 64_000, "esp32s3": 6_000_000, "esp32c6": 100_000}
PROFILES = {"lc": 2, "he": 5, "auto": 0}  # faac_object_type (MPEG-4 AOT numbering)
# libfaac refuses an explicit HE-AAC v1 request below this rate (frame.c HE_MIN_SAMPLE_RATE: the half-rate
# core would be too narrow for SBR), and its auto mode never picks HE there. Such a row is not a failure.
HE_MIN_SAMPLE_RATE = 32000
OBJECT_NAMES = {2: "LC", 5: "HE-AAC v1", 29: "HE-AAC v2"}

REQ_MAGIC = b"ENCB"
RSP_MAGIC = b"ENCR"
ACK_MAGIC = b"ENCA"  # device accepted the request and is ready for the PCM
# sample_rate, channels, bit_rate per channel, object_type, loops, flags, pcm_bytes
REQ_FMT = "<IIIIIII"
# status, frames, out_bytes, object_type, frame_samples, enc_rate, bit_rate, bandwidth,
# best, mean, stream_crc32, heap_delta, stack_hwm, init_heap, open_cycles
RSP_FMT = "<IIIIIIIIQQIIIII"
RSP_LEN = struct.calcsize(RSP_FMT)
CHUNK = 4096
FLAG_RETURN_STREAM = 1

STATUS = {0: "ok", 1: "bad request", 2: "clip too big", 3: "encoder open failed",
          4: "bitstream buffer overflow", 5: "no frames encoded", 6: "out of memory",
          7: "encode failed"}


# ---------------------------------------------------------------- pure helpers

def build_dir(target):
    return os.path.join("build", target)


def results_path(target):
    """ESP32_ENC_RESULTS points a probing run at a throw-away file instead of the real results."""
    return os.environ.get("ESP32_ENC_RESULTS") or os.path.join(RESULTS_DIR, f"esp32_enc_{target}.json")


def load_results(target):
    try:
        with open(results_path(target)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"target": target, "runs": [], "footprint": None}


def save_results(target, data):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(results_path(target), "w") as f:
        json.dump(data, f, indent=1)


def pack_request(rate, channels, bit_rate_per_ch, object_type, loops, want_stream, pcm_len):
    flags = FLAG_RETURN_STREAM if want_stream else 0
    return REQ_MAGIC + struct.pack(REQ_FMT, rate, channels, bit_rate_per_ch, object_type, loops, flags, pcm_len)


def unpack_response(raw):
    """Decode the fixed response header (after the magic) into a metrics dict."""
    if len(raw) != RSP_LEN:
        raise ValueError(f"response header is {len(raw)} bytes, expected {RSP_LEN}")
    (status, frames, out_bytes, obj, frame_samples, enc_rate, bit_rate, bandwidth, best, mean,
     crc, heap_delta, stack_hwm, init_heap, open_cycles) = struct.unpack(RSP_FMT, raw)
    return {"status": status, "status_text": STATUS.get(status, str(status)), "frames": frames,
            "out_bytes": out_bytes, "object_type": obj, "frame_samples": frame_samples,
            "enc_rate": enc_rate, "bit_rate": bit_rate, "bandwidth": bandwidth,
            "best_cycles": best, "mean_cycles": mean, "stream_crc32": crc,
            "heap_min_free_delta": heap_delta, "stack_hwm": stack_hwm,
            "init_heap_used": init_heap, "open_cycles": open_cycles}


def bit_rate_per_channel(total_kbps, channels):
    """libfaac's bit_rate is per channel; the scenarios give the stream total."""
    return int(total_kbps * 1000) // channels


def clip_seconds(target, rate, channels, wanted, cap_bytes=None):
    """Wanted duration, trimmed so the PCM fits the target (whole milliseconds).
    `cap_bytes` replaces the built-in cap, to probe how much a chip really holds."""
    fit = (cap_bytes or MAX_PCM_BYTES[target]) / (rate * channels * 2)
    return int(min(wanted, fit) * 1000) / 1000


def cut_command(src, seconds, rate, channels, start=0.0):
    """ffmpeg argv that writes `seconds` of `src` (from `start`) as s16le at the corpus rate/channels."""
    return ["ffmpeg", "-v", "error", "-ss", f"{start:g}", "-i", src, "-t", f"{seconds:g}", "-ac", str(channels),
            "-ar", str(rate), "-f", "s16le", "-"]


def audio_seconds(pcm_bytes, rate, channels):
    return pcm_bytes / (rate * channels * 2)


def xrt(best_cycles, audio_s, mhz):
    return audio_s / (best_cycles / (mhz * 1e6)) if best_cycles else 0.0


def mhz_for_realtime(best_cycles, audio_s):
    return best_cycles / audio_s / 1e6 if audio_s else 0.0


def actual_kbps(out_bytes, audio_s):
    return out_bytes * 8 / audio_s / 1000 if audio_s else 0.0


def profile_applies(profile, rate):
    """False for a configuration libfaac rejects by design (HE-AAC v1 below HE_MIN_SAMPLE_RATE)."""
    return not (profile == "he" and rate < HE_MIN_SAMPLE_RATE)


def clamped_by_library(row):
    """True when libfaac resolved a lower per-channel rate than asked, e.g. the AAC core limit of
    6144 bits per frame and channel at the (half-rate, for HE) core rate."""
    if row.get("bit_rate") is None or not row.get("channels"):
        return False
    return row["bit_rate"] < row["target_kbps"] * 1000 / row["channels"] * 0.99


def stream_seconds(frames, frame_samples, enc_rate):
    """Duration the bitstream covers, priming and flush frames included."""
    return frames * frame_samples / enc_rate if enc_rate else 0.0


def adts_frame_lengths(data):
    """Frame sizes of an ADTS stream, or None when the framing breaks."""
    out, pos = [], 0
    while pos < len(data):
        if pos + 7 > len(data) or data[pos] != 0xFF or (data[pos + 1] & 0xF0) != 0xF0:
            return None
        n = ((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        if n < 7 or pos + n > len(data):
            return None
        out.append(n)
        pos += n
    return out


def check_stream(stream, res):
    """Cross-check a returned bitstream against the device's own counters."""
    frames = adts_frame_lengths(stream)
    return {"crc_ok": zlib.crc32(stream) == res["stream_crc32"],
            "size_ok": len(stream) == res["out_bytes"],
            "adts_frames": len(frames) if frames is not None else None,
            "frames_ok": frames is not None and len(frames) == res["frames"]}


def select_clips(scenarios=None, gate=False, per_scenario=1, include_multichannel=False):
    """(scenario, clip) pairs to encode: the first `per_scenario` of each scenario's clip list."""
    import config
    import utils
    out = []
    for name, cfg in config.SCENARIOS.items():
        if scenarios and name not in scenarios:
            continue
        channels = utils.scenario_channels(cfg)
        if channels > 2 and not include_multichannel:
            continue  # the firmware is built for 2 channels
        d = utils.corpus_dir(cfg)
        names = list(config.GATE_CLIPS.get(name) or [])
        if not gate or not names:
            names = sorted(f for f in os.listdir(d) if f.lower().endswith(".wav")) if os.path.isdir(d) else []
        names = [n for n in names if os.path.exists(os.path.join(d, n))][:per_scenario]
        for fn in names:
            out.append({"scenario": name, "filename": fn, "path": os.path.join(d, fn),
                        "rate": utils.scenario_rate(cfg), "channels": channels,
                        "target_kbps": cfg["bitrate"]})
    return out


def row_key(r):
    return (r["profile"], r["scenario"], r["filename"], r["seconds"], r.get("start", 0.0))


# ---------------------------------------------------------------- build / flash

def idf(cmd, cwd=FIRMWARE):
    """Run a shell command with the pinned ESP-IDF environment active."""
    env_cmd = (f"export IDF_TOOLS_PATH={shlex.quote(os.path.join(TOOLCHAIN, 'tools'))} && "
               f". {shlex.quote(os.path.join(TOOLCHAIN, 'esp-idf', 'export.sh'))} >/dev/null 2>&1 && {cmd}")
    return subprocess.run(["bash", "-c", env_cmd], cwd=cwd, capture_output=True, text=True)


def idf_args(target, extra=""):
    b = build_dir(target)
    return f"-B {b} -DSDKCONFIG={b}/sdkconfig {extra}".strip()


def configured_for(target):
    """True when the build dir was already set to this target (set-target would wipe it)."""
    try:
        with open(os.path.join(FIRMWARE, build_dir(target), "sdkconfig")) as f:
            return f'CONFIG_IDF_TARGET="{target}"' in f.read()
    except OSError:
        return False


def footprint(target):
    """Flash (text+rodata+data) and static RAM of libfaac's archive, from the IDF map."""
    res = idf(f"python -m esp_idf_size --archives --format json {build_dir(target)}/aac_enc_bench.map")
    if res.returncode:
        return None
    try:
        arch = json.loads(res.stdout[res.stdout.index("{"):])
    except ValueError:
        return None
    name = next((k for k in arch if k.startswith("libfaac_lib")), None)
    return arch[name] if name else None


def cmd_build(args):
    extra = (f"-DFAAC_DIR={shlex.quote(args.faac_dir)} -DFAAC_SBR_DECIMATION={args.sbr_decimation} "
             f"-DFAAC_MAX_CHANNELS={args.max_channels}")
    data = load_results(args.target)
    steps = [] if configured_for(args.target) and not args.clean else [
        f"idf.py {idf_args(args.target, extra)} set-target {args.target}"]
    steps.append(f"idf.py {idf_args(args.target, extra)} build")
    for step in steps:
        res = idf(step)
        if res.returncode:
            sys.exit(f"{args.target}: `{step}` failed\n" + "\n".join((res.stdout + res.stderr).splitlines()[-25:]))
    data["footprint"] = footprint(args.target)
    save_results(args.target, data)
    print(f"built {args.target}: footprint {data['footprint']}")


def flash(target, port):
    res = idf(f"idf.py {idf_args(target)} -p {shlex.quote(port)} flash")
    if res.returncode:
        sys.exit(f"flash {target} failed:\n" + "\n".join((res.stdout + res.stderr).splitlines()[-15:]))


# ---------------------------------------------------------------- device link

class DeviceError(RuntimeError):
    pass


class Device:
    def __init__(self, port, baud=921600):
        import serial  # only the run command needs pyserial
        self.ser = serial.Serial(port, baud, timeout=2)

    def close(self):
        self.ser.close()

    def reset(self):
        """Reboot through RTS and drop the boot log. A fresh boot per clip keeps the all-time
        heap low-water mark from carrying over a larger earlier request."""
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.1)
        self.ser.rts = False
        time.sleep(1.0)
        self.ser.reset_input_buffer()

    def _sync(self, timeout, magics):
        """Scan the stream for one of the magics, skipping boot noise; returns the one found."""
        deadline = time.monotonic() + timeout
        win = b""
        while time.monotonic() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            win = (win + b)[-4:]
            if win in magics:
                return win
        raise DeviceError("no response magic from device")

    def encode(self, pcm, rate, channels, bit_rate, object_type, loops=1, want_stream=False, timeout=900):
        """Send one PCM clip; return the metrics dict (and the ADTS bytes when requested).

        The device sizes its buffers (and tries the configuration) first and only then asks for the
        PCM, because it cannot drain the UART while it does that."""
        self.ser.reset_input_buffer()
        self.ser.write(pack_request(rate, channels, bit_rate, object_type, loops, want_stream, len(pcm)))
        self.ser.flush()
        if self._sync(30, (ACK_MAGIC, RSP_MAGIC)) == ACK_MAGIC:
            for i in range(0, len(pcm), CHUNK):
                self.ser.write(pcm[i:i + CHUNK])
            self.ser.flush()
            self._sync(timeout, (RSP_MAGIC,))
        res = unpack_response(self.ser.read(RSP_LEN))
        if want_stream and res["status"] == 0:
            n = res["out_bytes"]
            self.ser.timeout = max(5, n / 50000)
            stream = self.ser.read(n)
            self.ser.timeout = 2
            if len(stream) != n:
                raise DeviceError(f"short bitstream: {len(stream)} of {n}")
            return res, stream
        return res


# ---------------------------------------------------------------- verification

def decode_check(adts_path, src_pcm, channels, rate):
    """Decode the device bitstream with ffmpeg; report stream facts and an SNR against the source.

    The SNR is a sanity figure (lossy codec, lag found by cross-correlation), not quality."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_name,profile,sample_rate,channels", "-of", "json", adts_path],
                           capture_output=True, text=True)
    try:
        st = json.loads(probe.stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        return {"decodes": False}
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", adts_path, "-f", "s16le", "-ac", str(channels),
                          "-ar", str(rate), "-"], capture_output=True)
    out = {"decodes": dec.returncode == 0 and len(dec.stdout) > 0, "dec_profile": st.get("profile"),
           "dec_rate": int(st.get("sample_rate", 0)), "dec_channels": int(st.get("channels", 0)),
           "dec_seconds": audio_seconds(len(dec.stdout), rate, channels)}
    out["snr_db"] = lag_snr(src_pcm, dec.stdout, channels)
    return out


def lag_snr(ref_bytes, dec_bytes, channels, max_lag=8192):
    """SNR of dec vs ref after aligning by cross-correlation of a mono mix; None when too short."""
    try:
        import numpy as np
    except ImportError:
        return None
    ref = np.frombuffer(ref_bytes[:len(ref_bytes) // 2 * 2], dtype="<i2").astype(np.float64)
    dec = np.frombuffer(dec_bytes[:len(dec_bytes) // 2 * 2], dtype="<i2").astype(np.float64)
    if min(len(ref), len(dec)) < 2 * max_lag + 4096:
        return None
    n = min(len(ref), len(dec), 1 << 18)
    mono = lambda x: x[: n - n % channels].reshape(-1, channels).mean(axis=1)
    r, d = mono(ref), mono(dec)
    size = 1 << (2 * len(r)).bit_length()
    xc = np.fft.irfft(np.fft.rfft(d, size) * np.conj(np.fft.rfft(r, size)), size)
    lags = np.concatenate([xc[:max_lag], xc[-max_lag:]])
    lag = int(np.argmax(lags))
    lag = lag if lag < max_lag else lag - 2 * max_lag  # dec lags ref by `lag` samples
    off = lag * channels
    a, b = (ref, dec[off:]) if off >= 0 else (ref[-off:], dec)
    m = min(len(a), len(b))
    a, b = a[:m], b[:m]
    err = float(np.sum((a - b) ** 2))
    return float(10 * np.log10(max(float(np.sum(a ** 2)), 1.0) / max(err, 1.0)))


# ---------------------------------------------------------------- run

def cut_pcm(clip, seconds, start=0.0):
    res = subprocess.run(cut_command(clip["path"], seconds, clip["rate"], clip["channels"], start), capture_output=True)
    if res.returncode:
        return b""
    return res.stdout


def cmd_run(args):
    data = load_results(args.target)
    mhz = CPU_MHZ[args.target]
    done = {row_key(r) for r in data["runs"] if r["status"] == 0 and (r.get("decodes") is not None or not args.verify)}
    clips = select_clips(args.scenarios.split(",") if args.scenarios else None, args.gate, args.clips)
    if args.limit:
        clips = clips[: args.limit]
    plan, skipped = [], []
    for c in clips:
        sec = clip_seconds(args.target, c["rate"], c["channels"], args.seconds, args.max_pcm_bytes)
        for prof in args.profiles:
            if not profile_applies(prof, c["rate"]):
                skipped.append((prof, c["scenario"]))
            elif (prof, c["scenario"], c["filename"], sec, args.start) not in done:
                plan.append((c, prof, sec))
    if skipped:
        print(f"not applicable (HE-AAC v1 needs >= {HE_MIN_SAMPLE_RATE} Hz in libfaac): "
              + ", ".join(f"{p}/{sc}" for p, sc in skipped))
    print(f"{len(plan)} encodes to do ({len(clips)} clips x {len(args.profiles)} profiles), target {args.target} @ {mhz} MHz")
    if not plan:
        return
    if not args.no_flash:
        flash(args.target, args.port)
    dev = Device(args.port)
    try:
        for n, (c, prof, sec) in enumerate(plan, 1):
            pcm = cut_pcm(c, sec, args.start)
            if not pcm:
                print(f"  skip (ffmpeg cut failed): {c['scenario']} {c['filename']}")
                continue
            audio_s = audio_seconds(len(pcm), c["rate"], c["channels"])
            bpc = bit_rate_per_channel(c["target_kbps"], c["channels"])
            dev.reset()
            try:
                out = dev.encode(pcm, c["rate"], c["channels"], bpc, PROFILES[prof], loops=args.loops,
                                 want_stream=bool(args.verify), timeout=args.timeout)
                res, stream = out if isinstance(out, tuple) else (out, None)
            except Exception as exc:
                print(f"  device error ({exc})")
                res, stream = {"status": -1, "status_text": str(exc)}, None
            row = {"profile": prof, "target": args.target, "scenario": c["scenario"], "filename": c["filename"],
                   "seconds": sec, "start": args.start, "audio_seconds": audio_s, "rate": c["rate"], "channels": c["channels"],
                   "target_kbps": c["target_kbps"], "cpu_mhz": mhz, "loops": args.loops, **res}
            if res["status"] == 0:
                # Rate over the duration the stream covers: the encoder emits priming and flush
                # frames, which on a short clip would otherwise read as extra bitrate.
                row["actual_kbps"] = actual_kbps(res["out_bytes"], stream_seconds(
                    res["frames"], res["frame_samples"], res["enc_rate"]))
                row["input_kbps"] = actual_kbps(res["out_bytes"], audio_s)
                row["xrt"] = xrt(res["best_cycles"], audio_s, mhz)
                row["mhz_for_1x"] = mhz_for_realtime(res["best_cycles"], audio_s)
            if stream is not None:
                row.update(check_stream(stream, res))
                with tempfile.TemporaryDirectory() as td:
                    path = os.path.join(td, "enc.aac")
                    with open(path, "wb") as f:
                        f.write(stream)
                    if args.keep:
                        os.makedirs(args.keep, exist_ok=True)
                        with open(os.path.join(args.keep, f"{args.target}_{prof}_{c['scenario']}.aac"), "wb") as f:
                            f.write(stream)
                    row.update(decode_check(path, pcm, c["channels"], c["rate"]))
            data["runs"] = [x for x in data["runs"] if row_key(x) != row_key(row)] + [row]
            save_results(args.target, data)
            if res["status"] == 0:
                print(f"  [{n}/{len(plan)}] {prof} {c['scenario']} {c['filename']} {sec:g}s: "
                      f"{row['xrt']:.2f}x RT, {row['mhz_for_1x']:.0f} MHz/RT, "
                      f"{row['actual_kbps']:.1f}/{c['target_kbps']} kbps, "
                      f"{OBJECT_NAMES.get(res['object_type'], res['object_type'])}, "
                      f"decodes={row.get('decodes')}, snr={row.get('snr_db')}")
            else:
                print(f"  [{n}/{len(plan)}] {prof} {c['scenario']} {c['filename']}: {res['status_text']}")
    finally:
        dev.close()


# ---------------------------------------------------------------- report

def cmd_report(args):
    data = load_results(args.target)
    runs = [r for r in data["runs"] if r["status"] == 0]
    mhz = CPU_MHZ[args.target]
    lines = [f"# {args.target} @ {mhz} MHz: FAAC encoder, {len(runs)} encodes", "",
             "| Profile | Clips | Mean xRT | Min xRT | MHz for 1x RT (mean / worst) | Mean actual/target kbps | Heap (KB, max) | Stack (KB, max) | Decodes (ffmpeg) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for prof in sorted({r["profile"] for r in runs}):
        rs = [r for r in runs if r["profile"] == prof]
        x = [r["xrt"] for r in rs]
        need = [r["mhz_for_1x"] for r in rs]
        ratio = [r["actual_kbps"] / r["target_kbps"] for r in rs]
        checked = [r for r in rs if r.get("decodes") is not None]
        ok = sum(1 for r in checked if r["decodes"] and r.get("crc_ok") and r.get("frames_ok"))
        lines.append(f"| {prof} | {len(rs)} | {sum(x) / len(x):.2f} | {min(x):.2f} | "
                     f"{sum(need) / len(need):.0f} / {max(need):.0f} | {sum(ratio) / len(ratio):.2f}x | "
                     f"{max(r['init_heap_used'] for r in rs) / 1024:.0f} ({max(r['heap_min_free_delta'] for r in rs) / 1024:.0f} peak) | "
                     f"{max(STACK_BYTES - r['stack_hwm'] for r in rs) / 1024:.1f} | {ok}/{len(checked)} |")
    lines += ["", "| Profile | Scenario | Clip | s | Resolved | xRT | MHz/RT | kbps (target) | SNR (dB) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda r: (r["profile"], r["scenario"], r["filename"])):
        snr = r.get("snr_db")
        lines.append(f"| {r['profile']} | {r['scenario']} | {r['filename']} | {r['seconds']:g} | "
                     f"{OBJECT_NAMES.get(r['object_type'], r['object_type'])} | {r['xrt']:.2f} | {r['mhz_for_1x']:.0f} | "
                     f"{r['actual_kbps']:.1f} ({r['target_kbps']}) | {'n/a' if snr is None else f'{snr:.1f}'} |")
    clamped = [r for r in runs if clamped_by_library(r)]
    if clamped:
        lines += ["", "Rate clamped by libfaac (target not reachable in this profile): " + ", ".join(
            f"{r['profile']}/{r['scenario']} ({r['target_kbps']} asked, {r['actual_kbps']:.0f} actual)" for r in clamped)]
    failed = [r for r in data["runs"] if r["status"] != 0 and profile_applies(r["profile"], r.get("rate", HE_MIN_SAMPLE_RATE))]
    na = [r for r in data["runs"] if r["status"] != 0 and not profile_applies(r["profile"], r.get("rate", HE_MIN_SAMPLE_RATE))]
    if na:
        lines += ["", "Not applicable (libfaac rejects HE-AAC v1 below 32 kHz): " + ", ".join(
            f"{r['profile']}/{r['scenario']}" for r in na)]
    if failed:
        lines += ["", "Failed: " + ", ".join(f"{r['profile']}/{r['scenario']} ({r['status_text']})" for r in failed)]
    if data.get("footprint"):
        lines += ["", f"Footprint (libfaac archive, from the IDF map): {data['footprint']}"]
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

    b = sub.add_parser("build", help="build the encoder firmware")
    common(b)
    b.add_argument("--faac-dir", default="/home/nschimme/gitprojects/faac", help="FAAC checkout")
    b.add_argument("--sbr-decimation", type=int, default=1, choices=range(1, 9))
    b.add_argument("--max-channels", type=int, default=2, choices=(1, 2))
    b.add_argument("--clean", action="store_true", help="re-run set-target (wipes the build dir)")
    b.set_defaults(fn=cmd_build)

    r = sub.add_parser("run", help="flash and benchmark")
    common(r)
    r.add_argument("--port", required=True)
    r.add_argument("--no-flash", action="store_true")
    r.add_argument("--profiles", type=lambda s: s.split(","), default=["lc", "he"],
                   help="comma list of lc, he, auto (HE-AAC v1 or LC by bitrate)")
    r.add_argument("--scenarios")
    r.add_argument("--gate", action="store_true", help="take clips from the fixed gate list")
    r.add_argument("--clips", type=int, default=1, help="clips per scenario")
    r.add_argument("--limit", type=int, default=0, help="stop after this many clips")
    r.add_argument("--start", type=float, default=0.0,
                   help="offset into each clip; quiet intros make xRT look better than it is")
    r.add_argument("--max-pcm-bytes", type=int, default=0,
                   help="replace the per-chip cap on the PCM sent (to find what a chip really holds)")
    r.add_argument("--seconds", type=float, default=10.0, help="first N seconds of each clip (trimmed to fit the chip)")
    r.add_argument("--loops", type=int, default=3, help="encode passes per clip; best is reported")
    r.add_argument("--verify", type=int, default=1, choices=(0, 1), help="return the bitstream and decode it with ffmpeg")
    r.add_argument("--keep", help="directory to save returned bitstreams to")
    r.add_argument("--timeout", type=float, default=900, help="seconds to wait for each response")
    r.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="markdown summary")
    common(p)
    p.add_argument("--output")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    if args.cmd == "run":
        bad = [x for x in args.profiles if x not in PROFILES]
        if bad:
            ap.error(f"unknown profile(s) {bad}; choose from {sorted(PROFILES)}")
    args.fn(args)


if __name__ == "__main__":
    main()
