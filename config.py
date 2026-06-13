"""
 * FAAC Benchmark Suite
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""

SCENARIOS = {
    "voip": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 16,
        "thresh": 2.5},
    "vss": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 40,
        "thresh": 3.0},
    # Low-bitrate music points (24 and 20 kbps/ch stereo). Named by rate, not
    # codec: while HE-AAC is dormant these run as pure LC (valid low-rate LC
    # tests); once HE auto-engages in faac they become the HE-vs-LC comparison
    # at the bitrates where HE-AAC v1 is designed to win.
    "music_48": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 48,
        "thresh": 3.0},
    "music_40": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 40,
        "thresh": 2.8},
    "music_low": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 64,
        "thresh": 3.5},
    "music_std": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 128,
        "thresh": 4.0},
    "music_high": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 256,
        "thresh": 4.3}}

# Small, fixed, reproducible subsets for fast iteration (`run_benchmark --gate`).
# Curated to span the strata that matter (percussive vs tonal music; chop/noise/
# echo speech across voices). Scenarios sharing a corpus share a list. Any
# scenario without an entry falls back to a deterministic even-spaced slice
# (see phase1_encode.gate_filter), so --gate always works.
_MUSIC_GATE = [
    "sandman.16b48k.wav",          # percussive (LC-favoured)
    "velvet.16b48k.wav",           # tonal/bright (HE-favoured)
    "21-classic.441.16b48k.wav",   # tonal classical (HE-favoured)
    "fms.wav",                     # mixed
]
_SPEECH_GATE = [
    "C_01_CHOP_FA.wav",
    "C_10_NOISE_MK.wav",
    "C_15_ECHO_FG.wav",
    "C_18_NOISE_ML.wav",
]
GATE_CLIPS = {
    "voip": _SPEECH_GATE,
    "vss": _SPEECH_GATE,
    "music_40": _MUSIC_GATE,
    "music_48": _MUSIC_GATE,
    "music_low": _MUSIC_GATE,
    "music_std": _MUSIC_GATE,
    "music_high": _MUSIC_GATE,
}
GATE_FALLBACK_N = 4
