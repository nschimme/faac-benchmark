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
    "16k_16k_mono": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 16,
        "thresh": 2.5},
    "16k_40k_mono": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 40,
        "thresh": 3.0},
    "48k_24k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 24,
        "thresh": 2.0},
    "48k_32k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 32,
        "thresh": 2.4},
    "48k_40k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 40,
        "thresh": 2.8},
    "48k_48k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 48,
        "thresh": 3.0},
    "48k_56k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 56,
        "thresh": 3.2},
    "48k_64k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 64,
        "thresh": 3.5},
    "48k_96k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 96,
        "thresh": 3.8},
    "48k_128k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 128,
        "thresh": 4.0},
    "48k_160k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 160,
        "thresh": 4.2},
    "48k_192k_stereo": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 192,
        "thresh": 4.25},
    "48k_256k_stereo": {
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
    "16k_16k_mono": _SPEECH_GATE,
    "16k_40k_mono": _SPEECH_GATE,
    "48k_24k_stereo": _MUSIC_GATE,
    "48k_32k_stereo": _MUSIC_GATE,
    "48k_40k_stereo": _MUSIC_GATE,
    "48k_48k_stereo": _MUSIC_GATE,
    "48k_56k_stereo": _MUSIC_GATE,
    "48k_64k_stereo": _MUSIC_GATE,
    "48k_96k_stereo": _MUSIC_GATE,
    "48k_128k_stereo": _MUSIC_GATE,
    "48k_160k_stereo": _MUSIC_GATE,
    "48k_192k_stereo": _MUSIC_GATE,
    "48k_256k_stereo": _MUSIC_GATE,
}
GATE_FALLBACK_N = 4
