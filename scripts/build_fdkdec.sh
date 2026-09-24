#!/usr/bin/env bash
#
# Build the fdkdec CLI (decodes MP4/M4A or raw ADTS AAC via Homebrew
# libfdk-aac) for compare_codecs.py.
#
# Requires: `brew install fdk-aac` and a FAAC checkout for
# frontend/mp4read.c (path via $FAAC_SRC, default ../faac next to this
# faac-benchmark checkout).
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BIN_DIR="$BENCH_ROOT/bin"
TARGET_BIN="$BIN_DIR/fdkdec"

if [[ -x "$TARGET_BIN" ]]; then
    echo "$TARGET_BIN"
    exit 0
fi

FAAC_SRC="${FAAC_SRC:-$BENCH_ROOT/../faac}"
MP4READ="$FAAC_SRC/frontend/mp4read.c"
if [[ ! -f "$MP4READ" ]]; then
    echo "error: mp4read.c not found at $MP4READ (set FAAC_SRC to a faac checkout)" >&2
    exit 1
fi

# mp4read.c's HAVE_LIBFAAM path calls into libfaam (FAAC's MP4/gapless
# demuxer) rather than parsing boxes itself; find a built static lib from
# any of the FAAC checkout's build directories.
FAAM_LIB="${FAAM_LIB:-}"
if [[ -z "$FAAM_LIB" ]]; then
    FAAM_LIB="$(find "$FAAC_SRC" -maxdepth 3 -name 'libfaam.a' -print -quit 2>/dev/null || true)"
fi
if [[ -z "$FAAM_LIB" || ! -f "$FAAM_LIB" ]]; then
    echo "error: libfaam.a not found under $FAAC_SRC (build FAAC first, or set FAAM_LIB)" >&2
    exit 1
fi

FDK_PREFIX="$(brew --prefix fdk-aac 2>/dev/null || echo /opt/homebrew)"
if [[ ! -f "$FDK_PREFIX/include/fdk-aac/aacdecoder_lib.h" ]]; then
    echo "error: libfdk-aac headers not found under $FDK_PREFIX (brew install fdk-aac)" >&2
    exit 1
fi

mkdir -p "$BIN_DIR"

echo "==> Compiling fdkdec..." >&2
gcc -O2 -o "$TARGET_BIN" "$SCRIPT_DIR/fdkdec/fdkdec.c" \
    -I "$FAAC_SRC/frontend" -I "$FAAC_SRC/include" -I "$FDK_PREFIX/include" \
    "$FAAM_LIB" -L "$FDK_PREFIX/lib" -lfdk-aac >&2

chmod +x "$TARGET_BIN"
echo "$TARGET_BIN"
