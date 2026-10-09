"""
 * FAAC Benchmark Suite - Unit Tests for scripts/esp32 (no hardware)
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESP32_DIR = os.path.join(SCRIPT_DIR, "scripts", "esp32")
for p in (SCRIPT_DIR, ESP32_DIR):
    if p not in sys.path:
        sys.path.append(p)

import config
import esp32_dec_device as device
import esp32_dec_bench as eb


class FakeSerial:
    """Serves a canned byte stream one read at a time; empty when exhausted."""

    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.written = bytearray()
        self.timeout = 2

    def read(self, n=1):
        out = bytes(self.data[:n])
        del self.data[:n]
        return out

    def write(self, b):
        self.written += b

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass


def header(status=0, samples_out=0, best=1000):
    return device.RSP_MAGIC + struct.pack(device.RSP_FMT, status, 7, samples_out, 44100, 2,
                                          best, best + 5, 0xDEADBEEF, 4096, 2048, 999)


def make_device(data):
    dev = device.Device.__new__(device.Device)
    dev.ser = FakeSerial(data)
    dev.cpu_mhz = 240
    return dev


class TestDevice(unittest.TestCase):
    def test_parses_header(self):
        dev = make_device(header(best=123456))
        res = dev.decode(b"\x01" * 10, loops=2)
        self.assertEqual(res["status"], 0)
        self.assertEqual(res["status_text"], "ok")
        self.assertEqual(res["frames"], 7)
        self.assertEqual(res["out_rate"], 44100)
        self.assertEqual(res["out_channels"], 2)
        self.assertEqual(res["best_cycles"], 123456)
        self.assertEqual(res["mean_cycles"], 123461)
        self.assertEqual(res["pcm_crc32"], 0xDEADBEEF)
        self.assertEqual(res["heap_min_free_delta"], 4096)
        self.assertEqual(res["stack_hwm"], 2048)
        self.assertEqual(res["init_heap_used"], 999)

    def test_request_framing(self):
        dev = make_device(header())
        dev.decode(b"abc", loops=5, want_pcm=True)
        self.assertEqual(bytes(dev.ser.written), b"AACB" + struct.pack("<III", 3, 5, 1) + b"abc")

    def test_resync_skips_boot_noise_and_partial_magic(self):
        dev = make_device(b"ets Jun boot\r\nAAC" + b"AACB junk " + header(status=3))
        res = dev.decode(b"x")
        self.assertEqual(res["status"], 3)
        self.assertEqual(res["status_text"], "codec init failed")

    def test_unknown_status_text(self):
        res = make_device(header(status=42)).decode(b"x")
        self.assertEqual(res["status_text"], "42")

    def test_short_header(self):
        dev = make_device(header()[:-4])
        with self.assertRaisesRegex(device.DeviceError, "short response header"):
            dev.decode(b"x")

    def test_no_magic_times_out(self):
        dev = make_device(b"noise only")
        with self.assertRaisesRegex(device.DeviceError, "no response magic"):
            dev.decode(b"x", timeout=0.05)

    def test_pcm_returned(self):
        pcm = bytes(range(8))
        dev = make_device(header(samples_out=4) + pcm)
        res, got = dev.decode(b"x", want_pcm=True)
        self.assertEqual(got, pcm)
        self.assertEqual(res["samples_out"], 4)
        self.assertEqual(dev.ser.timeout, 2)

    def test_short_pcm(self):
        dev = make_device(header(samples_out=4) + b"\x00\x01")
        with self.assertRaisesRegex(device.DeviceError, "short PCM"):
            dev.decode(b"x", want_pcm=True)

    def test_pcm_not_read_on_error_status(self):
        res = make_device(header(status=5, samples_out=4)).decode(b"x", want_pcm=True)
        self.assertEqual(res["status"], 5)

    def test_constructor_uses_serial(self):
        with patch.object(device.serial, "Serial", return_value=FakeSerial()) as s:
            dev = device.Device("/dev/null", cpu_mhz=160)
        s.assert_called_once_with("/dev/null", 921600, timeout=2)
        self.assertEqual(dev.cpu_mhz, 160)


class TestSelectClips(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.scenario = next(iter(config.SCENARIOS))
        self.gate = (config.GATE_CLIPS.get(self.scenario) or [None])[0]
        self.rows = []
        self.add("faac_abr", "lc", self.scenario, "b.wav")
        self.add("faac_abr", "he", self.scenario, "a.wav")
        self.add("ffmpeg_abr", "lc", self.scenario, "c.wav")
        self.add("faac_abr", "lc", self.scenario, "d.wav", valid=False)
        self.add("faac_abr", "lc", self.scenario, "e.wav", exists=False)
        self.add("faac_abr", "lc", "other_scenario", "f.wav")
        if self.gate:
            self.add("faac_abr", "lc", self.scenario, self.gate)
        self.path = os.path.join(self.td.name, "rows.json")
        with open(self.path, "w") as f:
            json.dump(self.rows, f)

    def add(self, row_key, profile, scenario, filename, valid=True, exists=True):
        aac = os.path.join(self.td.name, f"{row_key}_{profile}_{scenario}_{filename}.m4a")
        if exists:
            open(aac, "wb").close()
        self.rows.append({"row_key": row_key, "profile": profile, "scenario": scenario,
                          "filename": filename, "aac_path": aac, "decode_valid": valid})

    def args(self, **kw):
        base = dict(bitstreams=self.path, encoder="faac", profiles=["lc", "he"], scenarios=None,
                    gate=False, limit=0)
        base.update(kw)
        return Namespace(**base)

    def names(self, **kw):
        return [(r["scenario"], r["filename"], r["profile"]) for r in eb.select_clips(self.args(**kw))]

    def test_filters_and_sorts(self):
        got = self.names()
        self.assertNotIn("c.wav", [g[1] for g in got])
        self.assertNotIn("d.wav", [g[1] for g in got])
        self.assertNotIn("e.wav", [g[1] for g in got])
        self.assertEqual(got, sorted(got))
        self.assertIn(("other_scenario", "f.wav", "lc"), got)

    def test_profiles(self):
        self.assertTrue(all(g[2] == "he" for g in self.names(profiles=["he"])))

    def test_scenarios(self):
        self.assertEqual({g[0] for g in self.names(scenarios=self.scenario)}, {self.scenario})

    def test_limit(self):
        self.assertEqual(len(self.names(limit=1)), 1)

    def test_gate(self):
        with patch.object(config, "GATE_CLIPS", {self.scenario: ["a.wav"]}):
            self.assertEqual(self.names(gate=True), [(self.scenario, "a.wav", "he")])


class TestReport(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = patch.object(eb, "RESULTS_DIR", td.name)
        p.start()
        self.addCleanup(p.stop)

    def run_row(self, codec, name, cycles, snr=None, status=0, profile="lc"):
        r = {"codec": codec, "row_key": "faac_abr", "profile": profile, "scenario": "s",
             "filename": name, "audio_duration": 10.0, "best_cycles": cycles, "status": status,
             "heap_min_free_delta": 20480, "stack_hwm": 65536 - 8192}
        if snr is not None:
            r["snr_db"] = snr
        return r

    def report(self, data, **kw):
        eb.save_results("esp32s3", data)
        out = StringIO()
        with redirect_stdout(out):
            eb.cmd_report(Namespace(target="esp32s3", output=kw.get("output")))
        return out.getvalue()

    def test_table(self):
        # 240 MHz: 24e6 cycles = 0.1 s for 10 s of audio = 100x RT, 2.4 MHz per audio second
        runs = [self.run_row("helix", "a", 24_000_000, snr=60.0),
                self.run_row("helix", "b", 48_000_000, snr=50.0),
                self.run_row("faad2", "a", 120_000_000),
                self.run_row("faad2", "b", 120_000_000),
                self.run_row("faad2", "only_faad2", 1, status=0),
                self.run_row("helix", "failed", 1, status=5)]
        text = self.report({"target": "esp32s3", "runs": runs, "footprint": {"helix": {"flash": 1}}})
        self.assertIn("decoders on 2 common bitstreams", text)
        self.assertIn("| libhelix-aac (fixed point) | lc | 2 | 75.0 | 50.0 | 3.6 | 20 | 8 | 50.0..60.0 |", text)
        self.assertIn("| FAAD2 (FIXED_POINT) | lc | 2 | 20.0 | 20.0 | 12.0 | 20 | 8 | n/a |", text)
        self.assertIn("## Footprint", text)

    def test_output_file(self):
        out = os.path.join(eb.RESULTS_DIR, "r.md")
        text = self.report({"target": "esp32s3", "runs": [self.run_row("helix", "a", 1000)],
                            "footprint": {}}, output=out)
        with open(out) as f:
            self.assertEqual(f.read(), text)

    def test_missing_results_file(self):
        text = self.report_missing()
        self.assertIn("on 0 common bitstreams", text)

    def report_missing(self):
        out = StringIO()
        with redirect_stdout(out):
            eb.cmd_report(Namespace(target="esp32c6", output=None))
        return out.getvalue()

    def test_resume_roundtrip(self):
        data = eb.load_results("esp32s3")
        self.assertEqual(data, {"target": "esp32s3", "runs": [], "footprint": {}})
        data["runs"].append({"x": 1})
        eb.save_results("esp32s3", data)
        self.assertEqual(eb.load_results("esp32s3")["runs"], [{"x": 1}])
        self.assertTrue(eb.results_path("esp32s3").endswith("esp32_dec_esp32s3.json"))


class TestDemux(unittest.TestCase):
    def test_aac_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            for ext in (".aac", ".ADTS"):
                src, dst = os.path.join(td, "in" + ext), os.path.join(td, "out.aac")
                with open(src, "wb") as f:
                    f.write(b"\xff\xf1payload")
                with patch.object(eb.subprocess, "run") as run:
                    self.assertTrue(eb.demux_adts(src, dst))
                run.assert_not_called()
                with open(dst, "rb") as f:
                    self.assertEqual(f.read(), b"\xff\xf1payload")

    def test_m4a_uses_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, "out.aac")

            def fake(cmd, **kw):
                with open(cmd[-1], "wb") as f:
                    f.write(b"x")
                return type("R", (), {"returncode": 0})()

            with patch.object(eb.subprocess, "run", side_effect=fake) as run:
                self.assertTrue(eb.demux_adts("in.m4a", dst))
            self.assertEqual(run.call_args[0][0][0], "ffmpeg")

    def test_ffmpeg_failure(self):
        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, "out.aac")
            with patch.object(eb.subprocess, "run", return_value=type("R", (), {"returncode": 1})()):
                self.assertFalse(eb.demux_adts("in.m4a", dst))


if __name__ == "__main__":
    unittest.main()
