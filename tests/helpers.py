"""Shared test helpers."""

import os
import struct
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_wav(path, seconds=1, sr=48000, ch=2):
    """Write a sawtooth WAV file with distinct per-channel values for stereo."""
    with wave.open(path, "w") as f:
        f.setnchannels(ch)
        f.setsampwidth(2)
        f.setframerate(sr)
        for j in range(sr * seconds):
            for c in range(ch):
                # Offset each channel so stereo files have a genuine L≠R image.
                f.writeframes(struct.pack('<h', (j + c * 500) % 1000 - 500))
