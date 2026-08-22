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

# vbr_q: FAAC's -q (percent quantizer quality, min 10) chosen so its VBR
# output lands near this scenario's "bitrate" for representative content --
# NOT a linear guess. The old table (q = bitrate * 1.25-ish) was never
# checked against real faac output: it undershot by 18-67%, worse at higher
# bitrates, because faac's -q-to-bitrate curve isn't linear. These values
# were found by grid search against GATE_CLIPS content (see
# calibrate_vbr_q.py) and validated to land within ~1-16% of "bitrate" on
# held-out clips not used in the search. Re-run calibrate_vbr_q.py after any
# libfaac change that could shift its quantizer/bitrate curve.
#
# Two scenarios (48k_stereo_48k, 48k_stereo_56k) are UNAVOIDABLY off target:
# for 48kHz stereo, libfaac's AUTO object-type resolution in VBR mode picks
# HE-AAC when quantqual <= 75 and forces plain LC-AAC above that (see
# HE_VBR_QUANTQUAL_MAX in libfaac/frame.c). On this content HE-AAC tops out
# around ~41 kbps at q=75, and LC-AAC's cheapest encode is ~71 kbps at q=76 --
# there is no -q value that lands in the 42-70 kbps gap between them. Both
# scenarios use q=75 (the closer edge of the gap) and will show a real,
# expected, non-regression bitrate-accuracy deficit (~-15% to -35%) in every
# report; watch the baseline-vs-candidate DELTA for these two, not the
# absolute deviation from "bitrate".
SCENARIOS = {
    "16k_mono_16k": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 16,
        "vbr_q": 164,
        "thresh": 2.5},
    "16k_mono_40k": {
        "mode": "speech",
        "rate": 16000,
        "visqol_rate": 16000,
        "bitrate": 40,
        "vbr_q": 856,
        "thresh": 3.0},
    "48k_stereo_24k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 24,
        "vbr_q": 30,
        "thresh": 2.0},
    "48k_stereo_32k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 32,
        "vbr_q": 50,
        "thresh": 2.4},
    "48k_stereo_40k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 40,
        "vbr_q": 71,
        "thresh": 2.8},
    "48k_stereo_48k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 48,
        # Unreachable target -- see the module-level note above. Closest
        # achievable is HE-AAC's ceiling at q=75 (~41 kbps, -15%).
        "vbr_q": 75,
        "thresh": 3.0},
    "48k_stereo_56k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 56,
        # Unreachable target -- see the module-level note above. q=75
        # (~41 kbps, -27%) is marginally closer than q=76 (~71 kbps, +27%).
        "vbr_q": 75,
        "thresh": 3.2},
    "48k_stereo_64k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 64,
        "vbr_q": 76,
        "thresh": 3.5},
    "48k_stereo_96k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 96,
        "vbr_q": 133,
        "thresh": 3.8},
    "48k_stereo_128k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 128,
        "vbr_q": 203,
        "thresh": 4.0},
    "48k_stereo_160k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 160,
        "vbr_q": 284,
        "thresh": 4.2},
    "48k_stereo_192k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 192,
        "vbr_q": 369,
        "thresh": 4.25},
    "48k_stereo_256k": {
        "mode": "audio",
        "rate": 48000,
        "visqol_rate": 48000,
        "bitrate": 256,
        "vbr_q": 569,
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
    "16k_mono_16k": _SPEECH_GATE,
    "16k_mono_40k": _SPEECH_GATE,
    "48k_stereo_24k": _MUSIC_GATE,
    "48k_stereo_32k": _MUSIC_GATE,
    "48k_stereo_40k": _MUSIC_GATE,
    "48k_stereo_48k": _MUSIC_GATE,
    "48k_stereo_56k": _MUSIC_GATE,
    "48k_stereo_64k": _MUSIC_GATE,
    "48k_stereo_96k": _MUSIC_GATE,
    "48k_stereo_128k": _MUSIC_GATE,
    "48k_stereo_160k": _MUSIC_GATE,
    "48k_stereo_192k": _MUSIC_GATE,
    "48k_stereo_256k": _MUSIC_GATE,
}
GATE_FALLBACK_N = 4
