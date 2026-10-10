"""Serial link to the on-device AAC benchmark firmware (see firmware_dec/README.md)."""
import struct
import time

import serial

REQ_MAGIC = b"AACB"
RSP_MAGIC = b"AACR"
REQ_BLOCK = b"AACQ"   # streaming: device asks for a block of the clip
PCM_MAGIC = b"AACD"   # streaming: a block of decoded PCM
FLAG_PCM, FLAG_STREAM = 1, 2
MAGICS = (RSP_MAGIC, REQ_BLOCK, PCM_MAGIC)
# status, frames, samples_out, out_rate, out_channels, best, mean, crc, heap_delta, stack_hwm, init_heap
RSP_FMT = "<IIIIIQQIIII"
RSP_LEN = struct.calcsize(RSP_FMT)
CHUNK = 4096

STATUS = {0: "ok", 1: "bad request", 2: "clip too big", 3: "codec init failed",
          4: "pcm buffer overflow", 5: "no frames decoded", 6: "out of memory"}


class DeviceError(RuntimeError):
    pass


class Device:
    def __init__(self, port, baud=921600, cpu_mhz=None):
        self.ser = serial.Serial(port, baud, timeout=2)
        self.cpu_mhz = cpu_mhz

    def close(self):
        self.ser.close()

    def reset(self):
        """Reboot through RTS and drop the boot log; the firmware prints nothing after its first request."""
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.1)
        self.ser.rts = False
        time.sleep(1.0)
        self.ser.reset_input_buffer()

    def _sync(self, timeout):
        """Scan the stream for the response magic, skipping boot noise."""
        deadline = time.monotonic() + timeout
        win = b""
        while time.monotonic() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            win = (win + b)[-4:]
            if win == RSP_MAGIC:
                return
        raise DeviceError("no response magic from device")

    def _next_magic(self, timeout):
        """Scan for the next device message, skipping noise; returns its magic."""
        deadline = time.monotonic() + timeout
        win = b""
        while time.monotonic() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            win = (win + b)[-4:]
            if win in MAGICS:
                return win
        raise DeviceError("no response magic from device")

    def _parse(self, raw):
        (status, frames, samples_out, rate, ch, best, mean, crc,
         heap_delta, stack_hwm, init_heap) = struct.unpack(RSP_FMT, raw)
        return {"status": status, "status_text": STATUS.get(status, str(status)), "frames": frames,
                "samples_out": samples_out, "out_rate": rate, "out_channels": ch,
                "best_cycles": best, "mean_cycles": mean, "pcm_crc32": crc,
                "heap_min_free_delta": heap_delta, "stack_hwm": stack_hwm, "init_heap_used": init_heap}

    def decode_stream(self, clip, loops=1, want_pcm=False, timeout=120):
        """Like decode(), but the clip stays here: the device requests blocks of it as it needs them (no size
        ceiling from the chip's RAM) and returns the PCM of the first pass in blocks as it decodes."""
        self.ser.reset_input_buffer()
        self.ser.write(REQ_MAGIC + struct.pack("<III", len(clip), loops, FLAG_STREAM | (FLAG_PCM if want_pcm else 0)))
        self.ser.flush()
        pcm = bytearray()
        while True:
            magic = self._next_magic(timeout)
            if magic == REQ_BLOCK:
                raw = self.ser.read(8)
                if len(raw) != 8:
                    raise DeviceError("short block request")
                offset, n = struct.unpack("<II", raw)
                self.ser.write(clip[offset:offset + n])
                self.ser.flush()
            elif magic == PCM_MAGIC:
                raw = self.ser.read(4)
                if len(raw) != 4:
                    raise DeviceError("short PCM header")
                n = struct.unpack("<I", raw)[0]
                data = self.ser.read(n)
                if len(data) != n:
                    raise DeviceError(f"short PCM block: {len(data)} of {n}")
                pcm += data
            else:
                raw = self.ser.read(RSP_LEN)
                if len(raw) != RSP_LEN:
                    raise DeviceError("short response header")
                res = self._parse(raw)
                if want_pcm and res["status"] == 0:
                    return res, bytes(pcm)
                return res

    def decode(self, clip, loops=1, want_pcm=False, timeout=120):
        """Send one ADTS clip; return the metrics dict (and PCM bytes when requested)."""
        self.ser.reset_input_buffer()
        self.ser.write(REQ_MAGIC + struct.pack("<III", len(clip), loops, 1 if want_pcm else 0))
        for i in range(0, len(clip), CHUNK):
            self.ser.write(clip[i:i + CHUNK])
        self.ser.flush()
        self._sync(timeout)
        raw = self.ser.read(RSP_LEN)
        if len(raw) != RSP_LEN:
            raise DeviceError("short response header")
        (status, frames, samples_out, rate, ch, best, mean, crc,
         heap_delta, stack_hwm, init_heap) = struct.unpack(RSP_FMT, raw)
        res = {"status": status, "status_text": STATUS.get(status, str(status)), "frames": frames,
               "samples_out": samples_out, "out_rate": rate, "out_channels": ch,
               "best_cycles": best, "mean_cycles": mean, "pcm_crc32": crc,
               "heap_min_free_delta": heap_delta, "stack_hwm": stack_hwm, "init_heap_used": init_heap}
        if want_pcm and status == 0:
            n = samples_out * 2
            # ~92 KB/s at 921600 baud, so a read timeout sized for headers is far too short
            self.ser.timeout = max(5, n / 50000)
            pcm = self.ser.read(n)
            self.ser.timeout = 2
            if len(pcm) != n:
                raise DeviceError(f"short PCM: {len(pcm)} of {n}")
            return res, pcm
        return res
