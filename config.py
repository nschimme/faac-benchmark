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

# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------
# A corpus is one directory of reference WAVs in ONE fixed format. It is the
# only place a sample rate or channel count is declared; scenarios point at a
# corpus and inherit both. Before this existed, "mode" meant three things at
# once (which directory, how many channels, which metric engine), which is why
# the suite could only ever describe 16k mono speech and 48k stereo music.
#
# "source" says which downloaded dataset setup_datasets.py builds the corpus
# from; "family" groups corpora that share a rate/channel format so reports can
# chart them together (a MOS-vs-bitrate line is only meaningful within one
# family). "max_clips"/"strata" cap a corpus deterministically -- see
# utils.select_corpus_clips.
CORPORA = {
    "speech_clean_16k": {
        "dir": "speech_clean_16k",
        "rate": 16000,
        "channels": 1,
        "source": "tcd_ref",
        "family": "16k_mono",
        "label": "16 kHz Mono Speech",
        "max_clips": 40,
        # The clean refs mirror the degraded set's naming (R_01_CHOP_FA.wav),
        # one distinct recording per condition, so they stratify the same way:
        # (condition, talker).
        "strata": r"_([A-Z]+)_([A-Z]{2})\.wav$"},
    "speech_clean_24k": {
        "dir": "speech_clean_24k",
        "rate": 24000,
        "channels": 1,
        "source": "tcd_ref",
        "family": "24k_mono",
        "label": "24 kHz Mono Speech",
        "max_clips": 40,
        # The clean refs mirror the degraded set's naming (R_01_CHOP_FA.wav),
        # one distinct recording per condition, so they stratify the same way:
        # (condition, talker).
        "strata": r"_([A-Z]+)_([A-Z]{2})\.wav$"},
    # The degraded TCD-VoIP test set. Kept as a single spot-check scenario, not
    # as the speech quality ladder: ViSQOL scores the encode against the
    # degraded file, so the chop/clip/echo/noise sits in the *reference* and
    # widens the variance a real regression has to clear before it is visible.
    # What it still buys is a genuinely different workload for the quantizer,
    # PNS and block-switch decisions, plus VoIP-transcoding realism.
    "speech_voip_16k": {
        "dir": "speech",
        "rate": 16000,
        "channels": 1,
        "source": "tcd_test",
        "family": "16k_mono",
        "label": "16 kHz Mono Speech (VoIP-degraded)",
        "max_clips": 48,
        "strata": r"_([A-Z]+)_([A-Z]{2})\.wav$"},
    "audio_48k": {
        "dir": "audio",
        "rate": 48000,
        "channels": 2,
        "source": "music",
        "family": "48k_stereo",
        "label": "48 kHz Stereo"},
    # 44.1 kHz exercises a different sample-rate index and a different
    # scalefactor-band table than 48 kHz -- the most common real-world config,
    # and until now completely untested. Derived by downsampling the 48 kHz
    # corpus, which is honest here: most PMLT sources are 44.1 kHz material the
    # dataset had already upsampled (*.441.16b48k.wav), and the derived file is
    # itself the reference for scoring.
    "audio_44k1": {
        "dir": "audio_44k1",
        "rate": 44100,
        "channels": 2,
        "source": "music",
        "family": "44k1_stereo",
        "label": "44.1 kHz Stereo"},
    # 32 kHz is the SBR/HE-AAC eligibility boundary (see use_he_aac in
    # compare_encoders.py) as well as a real broadcast rate.
    "audio_32k": {
        "dir": "audio_32k",
        "rate": 32000,
        "channels": 2,
        "source": "music",
        "family": "32k_stereo",
        "label": "32 kHz Stereo"},
}

# Report ordering for families: mono speech first (ascending rate), then
# stereo (ascending rate). Also drives the sort key in utils.
FAMILY_ORDER = ["16k_mono", "24k_mono", "32k_stereo", "44k1_stereo", "48k_stereo"]

# vbr_q: FAAC's -q (percent quantizer quality, min 10) chosen so its VBR
# output lands near this scenario's "bitrate" for representative content --
# NOT a linear guess. The old table (q = bitrate * 1.25-ish) was never
# checked against real faac output: it undershot by 18-67%, worse at higher
# bitrates, because faac's -q-to-bitrate curve isn't linear. These values
# were found by grid search against GATE_CLIPS content (see
# scripts/calibrate_vbr_q.py) and validated to land within ~1-16% of "bitrate" on
# held-out clips not used in the search. Re-run scripts/calibrate_vbr_q.py after any
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
#
# thresh/vbr_q marked PROVISIONAL below belong to scenarios added with the
# corpus rework and have not been through calibrate_vbr_q.py / a baseline run
# yet. Run scripts/validate_scenarios.py (bitrate reachability), then
# scripts/calibrate_vbr_q.py (vbr_q), then one baseline run (thresh) and
# replace them.
#
# A scenario declares mode (which metric engine), corpus (which content), and
# bitrate. "rate", "channels" and "visqol_rate" are DERIVED below -- do not set
# them by hand.
SCENARIOS = {
    # -- 16 kHz mono speech ------------------------------------------------
    # ABR only tracks a target inside a narrow window here. Measured on this
    # corpus (faac 2.1.0, scripts/validate_scenarios.py): 12k -> +42.5%,
    # 14k -> +28.7%, 16k -> +18.5%, 18k -> +10.4%, 20k -> +4.1%, 24k -> -4.7%,
    # 28k -> -11.8%, 32k -> -17.4%. Below ~20 kbps the encoder will not go any
    # lower on this content (a floor around 17 kbps, so a smaller target just
    # overshoots); above ~24 kbps it saturates. The ladder is 20k/24k, the
    # window's two ends.
    #
    # 16 kbps was the old telephony rung and is retired for overshooting it by
    # 18.5% -- the mirror image of the 40 kbps scenario's -20% undershoot, and
    # just as uncloseable.
    "16k_mono_20k": {
        "mode": "speech",
        "corpus": "speech_clean_16k",
        "bitrate": 20,
        "vbr_q": 220,   # PROVISIONAL
        "thresh": 2.5},  # PROVISIONAL
    "16k_mono_24k": {
        "mode": "speech",
        "corpus": "speech_clean_16k",
        "bitrate": 24,
        "vbr_q": 300,   # PROVISIONAL
        "thresh": 3.0},  # PROVISIONAL
    # Deliberately at 24 kbps, the same rate and bitrate as 16k_mono_24k: the
    # only difference from it is the content, which is what makes the
    # clean-vs-degraded comparison readable. Degraded speech carries more bits
    # so it tracks slightly higher (measured +0.2% here vs -4.7% clean).
    "16k_mono_voip_24k": {
        "mode": "speech",
        "corpus": "speech_voip_16k",
        "bitrate": 24,
        "vbr_q": 300,   # PROVISIONAL
        "thresh": 2.5},  # PROVISIONAL
    # -- 24 kHz mono speech ------------------------------------------------
    # Scored in AUDIO mode (Zimtohrli at 48 kHz), not ViSQOL speech mode:
    # speech mode is 16 kHz and would band-limit the reference to 8 kHz,
    # hiding exactly the bandwidth this family exists to test.
    # Measured ABR reachability on this corpus (faac 2.1.0, gate clips, via
    # scripts/validate_scenarios.py): 24k -> +11.8%, 28k -> +1.8%,
    # 32k -> -6.2%, 36k -> -12.6%, 40k -> -17.8%, 48k -> -25.5%. Same shape as
    # the 16 kHz family: a floor near 27 kbps and saturation past ~34, so the
    # ladder is 28k/32k. 40 kbps was tried and dropped -- shipping it would
    # repeat exactly the mistake 16k_mono_40k made.
    "24k_mono_28k": {
        "mode": "audio",
        "corpus": "speech_clean_24k",
        "bitrate": 28,
        "vbr_q": 420,   # PROVISIONAL
        "thresh": 3.0},  # PROVISIONAL
    "24k_mono_32k": {
        "mode": "audio",
        "corpus": "speech_clean_24k",
        "bitrate": 32,
        "vbr_q": 500,   # PROVISIONAL
        "thresh": 3.2},  # PROVISIONAL
    # -- 32 kHz stereo -----------------------------------------------------
    "32k_stereo_48k": {
        "mode": "audio",
        "corpus": "audio_32k",
        "bitrate": 48,
        "vbr_q": 75,    # PROVISIONAL
        "thresh": 3.0},  # PROVISIONAL
    "32k_stereo_96k": {
        "mode": "audio",
        "corpus": "audio_32k",
        "bitrate": 96,
        "vbr_q": 133,   # PROVISIONAL
        "thresh": 4.0},  # PROVISIONAL
    # -- 44.1 kHz stereo ---------------------------------------------------
    "44k1_stereo_64k": {
        "mode": "audio",
        "corpus": "audio_44k1",
        "bitrate": 64,
        "vbr_q": 76,    # PROVISIONAL
        "thresh": 3.5},  # PROVISIONAL
    "44k1_stereo_128k": {
        "mode": "audio",
        "corpus": "audio_44k1",
        "bitrate": 128,
        "vbr_q": 203,   # PROVISIONAL
        "thresh": 4.0},  # PROVISIONAL
    "44k1_stereo_192k": {
        "mode": "audio",
        "corpus": "audio_44k1",
        "bitrate": 192,
        "vbr_q": 369,   # PROVISIONAL
        "thresh": 4.25},  # PROVISIONAL
    # -- 48 kHz stereo -----------------------------------------------------
    "48k_stereo_24k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 24,
        "vbr_q": 30,
        "thresh": 2.0},
    "48k_stereo_32k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 32,
        "vbr_q": 50,
        "thresh": 2.4},
    "48k_stereo_40k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 40,
        "vbr_q": 71,
        "thresh": 2.8},
    "48k_stereo_48k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 48,
        # Unreachable target -- see the module-level note above. Closest
        # achievable is HE-AAC's ceiling at q=75 (~41 kbps, -15%).
        "vbr_q": 75,
        "thresh": 3.0},
    "48k_stereo_56k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 56,
        # Unreachable target -- see the module-level note above. q=75
        # (~41 kbps, -27%) is marginally closer than q=76 (~71 kbps, +27%).
        "vbr_q": 75,
        "thresh": 3.2},
    "48k_stereo_64k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 64,
        "vbr_q": 76,
        "thresh": 3.5},
    "48k_stereo_96k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 96,
        "vbr_q": 133,
        "thresh": 3.8},
    "48k_stereo_128k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 128,
        "vbr_q": 203,
        "thresh": 4.0},
    "48k_stereo_160k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 160,
        "vbr_q": 284,
        "thresh": 4.2},
    "48k_stereo_192k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 192,
        "vbr_q": 369,
        "thresh": 4.25},
    "48k_stereo_256k": {
        "mode": "audio",
        "corpus": "audio_48k",
        "bitrate": 256,
        "vbr_q": 569,
        "thresh": 4.3}}

# Scoring rates are a property of the METRIC ENGINE, not of the content:
# ViSQOL speech mode is 16 kHz mono, ViSQOL audio / Zimtohrli are 48 kHz
# (ZIMT_RATE in phase2_mos.py). phase2 conforms both reference and degraded to
# this rate with the same call, so the comparison stays fair whatever the
# corpus rate is.
METRIC_RATE = {"speech": 16000, "audio": 48000}

# Derive rate/channels/visqol_rate so there is exactly one source of truth for
# each. Doing it here (rather than in every consumer) keeps cfg["rate"] and
# cfg["visqol_rate"] working for existing call sites.
for _name, _cfg in SCENARIOS.items():
    _corpus = CORPORA[_cfg["corpus"]]
    _cfg["rate"] = _corpus["rate"]
    _cfg["channels"] = _corpus["channels"]
    _cfg["visqol_rate"] = METRIC_RATE[_cfg["mode"]]
del _name, _cfg, _corpus

# Small, fixed, reproducible subsets for fast iteration (`run_benchmark --gate`).
# Curated to span the strata that matter (percussive vs tonal music; chop/noise/
# echo speech across voices). Corpora sharing content share a list. Any
# scenario without an entry falls back to a deterministic even-spaced slice
# (see phase1_encode.gate_filter), so --gate always works -- which is how the
# clean-speech corpora are handled, since their filenames come from the
# TCD-VoIP ref set rather than being curated here.
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
# Gate clips are a property of the corpus, not of the scenario: every scenario
# reading the same directory gates on the same clips.
CORPUS_GATE = {
    "audio_48k": _MUSIC_GATE,
    "audio_44k1": _MUSIC_GATE,
    "audio_32k": _MUSIC_GATE,
    "speech_voip_16k": _SPEECH_GATE,
}
GATE_CLIPS = {
    _s: CORPUS_GATE[_c["corpus"]]
    for _s, _c in SCENARIOS.items()
    if _c["corpus"] in CORPUS_GATE}
GATE_FALLBACK_N = 4
