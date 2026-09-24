"""
 * FAAC Benchmark Suite - Codecs Package
 * Copyright (C) 2026 Nils Schimmelmann
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
"""

from codec_bench.encoders import (
    PROFILE_LABELS, profile_label, encoder_row_key, row_key,
    use_he_aac, use_he_v2_aac, Encoder, FAACEncoder, FFmpegEncoder,
    FDKAACEncoder, AACEncEncoder, FalabaacEncoder, AFConvertEncoder,
    OpusEncoder, LameEncoder, probe_faac_version, probe_encoder_capability,
    detect_encoders
)

from codec_bench.decoders import (
    decoder_row_key, Decoder, FAADDecoder, FFmpegDecoder, AFConvertDecoder,
    HelixAACDecoder, FDKDecoder, detect_decoders, process_decoder_task, process_decoder_robustness_task,
    get_conformance_ref_wav, get_conformance_ref_offset, CONFORMANCE_SNR_FLOOR_DB
)

from codec_bench.report import (
    CLIP_PEER_BUG_GAP, cell_peer_gap, generate_leaderboard,
    generate_decoder_leaderboard, generate_decoder_report
)
