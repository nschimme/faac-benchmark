#!/usr/bin/env bash
#
# Build Helix AAC Decoder binary for compare_codecs.py
# Clones arduino-libhelix (RealNetworks Helix AAC fixed-point decoder)
# and compiles a standalone helix-aac-dec binary.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD_DIR="${HELIX_BUILD_DIR:-$BENCH_ROOT/build/helix-aac}"
BIN_DIR="$BENCH_ROOT/bin"
TARGET_BIN="$BIN_DIR/helix-aac-dec"

if [[ -x "$TARGET_BIN" ]]; then
    echo "$TARGET_BIN"
    exit 0
fi

mkdir -p "$BUILD_DIR" "$BIN_DIR"

if [[ ! -d "$BUILD_DIR/arduino-libhelix" ]]; then
    echo "==> Cloning RealNetworks libhelix-aac repository..." >&2
    git clone --depth 1 https://github.com/pschatzmann/arduino-libhelix.git "$BUILD_DIR/arduino-libhelix" >&2
fi

HELIX_SRC="$BUILD_DIR/arduino-libhelix/src"

cat > "$BUILD_DIR/helix_aac_dec.c" << 'CEOF'
/*
 * Helix AAC Decoder CLI Wrapper for FAAC Benchmark Suite
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "aacdec.h"

#define READ_BUF_SIZE (1024 * 64)
#define OUT_BUF_SIZE (AAC_MAX_NCHANS * AAC_MAX_NSAMPS * 2)

void write_wav_header(FILE *f, int sample_rate, int channels, int total_pcm_bytes) {
    unsigned char header[44];
    int data_size = total_pcm_bytes;
    int chunk_size = 36 + data_size;
    int byte_rate = sample_rate * channels * 2;
    int block_align = channels * 2;

    memcpy(header, "RIFF", 4);
    header[4] = (chunk_size >> 0) & 0xff;
    header[5] = (chunk_size >> 8) & 0xff;
    header[6] = (chunk_size >> 16) & 0xff;
    header[7] = (chunk_size >> 24) & 0xff;
    memcpy(header + 8, "WAVEfmt ", 8);
    header[16] = 16; header[17] = 0; header[18] = 0; header[19] = 0;
    header[20] = 1;  header[21] = 0;
    header[22] = channels & 0xff; header[23] = (channels >> 8) & 0xff;
    header[24] = (sample_rate >> 0) & 0xff;
    header[25] = (sample_rate >> 8) & 0xff;
    header[26] = (sample_rate >> 16) & 0xff;
    header[27] = (sample_rate >> 24) & 0xff;
    header[28] = (byte_rate >> 0) & 0xff;
    header[29] = (byte_rate >> 8) & 0xff;
    header[30] = (byte_rate >> 16) & 0xff;
    header[31] = (byte_rate >> 24) & 0xff;
    header[32] = block_align & 0xff; header[33] = (block_align >> 8) & 0xff;
    header[34] = 16; header[35] = 0;
    memcpy(header + 36, "data", 4);
    header[40] = (data_size >> 0) & 0xff;
    header[41] = (data_size >> 8) & 0xff;
    header[42] = (data_size >> 16) & 0xff;
    header[43] = (data_size >> 24) & 0xff;

    fseek(f, 0, SEEK_SET);
    fwrite(header, 1, 44, f);
}

int main(int argc, char **argv) {
    if (argc >= 2 && (strcmp(argv[1], "-v") == 0 || strcmp(argv[1], "--version") == 0 || strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0)) {
        printf("Helix AAC Decoder v1.0 (RealNetworks Helix DNA)\n");
        return 0;
    }

    if (argc < 3) {
        fprintf(stderr, "Usage: %s <input.aac|input.m4a> <output.wav>\n", argv[0]);
        return 1;
    }

    const char *in_filename = argv[1];
    const char *out_filename = argv[2];

    FILE *fin = fopen(in_filename, "rb");
    if (!fin) {
        fprintf(stderr, "Error opening input file: %s\n", in_filename);
        return 1;
    }

    FILE *fout = fopen(out_filename, "wb");
    if (!fout) {
        fclose(fin);
        fprintf(stderr, "Error opening output file: %s\n", out_filename);
        return 1;
    }

    unsigned char dummy_header[44] = {0};
    fwrite(dummy_header, 1, 44, fout);

    HAACDecoder hDecoder = AACInitDecoder();
    if (!hDecoder) {
        fprintf(stderr, "Error initializing Helix AAC Decoder\n");
        fclose(fin);
        fclose(fout);
        return 1;
    }

    unsigned char *read_buf = (unsigned char *)malloc(READ_BUF_SIZE);
    short *out_buf = (short *)malloc(OUT_BUF_SIZE * sizeof(short));

    int bytes_in_buf = 0;
    int eof_reached = 0;
    int total_pcm_bytes = 0;
    int sample_rate = 0;
    int channels = 0;

    unsigned char *read_ptr = read_buf;

    while (1) {
        if (bytes_in_buf < AAC_MAINBUF_SIZE && !eof_reached) {
            if (bytes_in_buf > 0 && read_ptr != read_buf) {
                memmove(read_buf, read_ptr, bytes_in_buf);
            }
            read_ptr = read_buf;
            int bytes_read = fread(read_buf + bytes_in_buf, 1, READ_BUF_SIZE - bytes_in_buf, fin);
            if (bytes_read <= 0) {
                eof_reached = 1;
            } else {
                bytes_in_buf += bytes_read;
            }
        }

        if (bytes_in_buf <= 0) {
            break;
        }

        int offset = AACFindSyncWord(read_ptr, bytes_in_buf);
        if (offset < 0) {
            read_ptr += bytes_in_buf;
            bytes_in_buf = 0;
            continue;
        }

        read_ptr += offset;
        bytes_in_buf -= offset;

        int bytes_left = bytes_in_buf;
        unsigned char *dec_ptr = read_ptr;

        int err = AACDecode(hDecoder, &dec_ptr, &bytes_left, out_buf);
        int consumed = bytes_in_buf - bytes_left;
        if (consumed > 0) {
            read_ptr += consumed;
            bytes_in_buf = bytes_left;
        } else {
            read_ptr++;
            bytes_in_buf--;
        }

        if (err == ERR_AAC_NONE) {
            AACFrameInfo frameInfo;
            AACGetLastFrameInfo(hDecoder, &frameInfo);
            if (frameInfo.outputSamps > 0) {
                sample_rate = frameInfo.sampRateOut;
                channels = frameInfo.nChans;
                int pcm_bytes = frameInfo.outputSamps * sizeof(short);
                fwrite(out_buf, 1, pcm_bytes, fout);
                total_pcm_bytes += pcm_bytes;
            }
        }
    }

    if (total_pcm_bytes > 0 && sample_rate > 0 && channels > 0) {
        write_wav_header(fout, sample_rate, channels, total_pcm_bytes);
    }

    AACFreeDecoder(hDecoder);
    free(read_buf);
    free(out_buf);
    fclose(fin);
    fclose(fout);

    return 0;
}
CEOF

echo "==> Compiling Helix AAC Decoder..." >&2
(
    cd "$BUILD_DIR"
    gcc -O3 -c -DUSE_DEFAULT_STDLIB -I "$HELIX_SRC" -I "$HELIX_SRC/utils" -I "$HELIX_SRC/libhelix-aac" helix_aac_dec.c "$HELIX_SRC/libhelix-aac"/*.c
    g++ -O3 -c -DUSE_DEFAULT_STDLIB -I "$HELIX_SRC" -I "$HELIX_SRC/utils" -I "$HELIX_SRC/libhelix-aac" "$HELIX_SRC/utils/helix_memory.cpp"
    g++ *.o -o "$TARGET_BIN"
    rm -f *.o
) >&2

chmod +x "$TARGET_BIN"
echo "$TARGET_BIN"
