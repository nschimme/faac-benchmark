#!/usr/bin/env bash
#
# Build the fdkdec CLI (decodes MP4/M4A or raw ADTS AAC via libfdk-aac)
# for compare_codecs.py.
#
# Requires: libfdk-aac development files and a FAAC checkout (path via
# $FAAC_SRC, default ../faac next to this faac-benchmark checkout).
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BIN_DIR="$BENCH_ROOT/bin"
TARGET_BIN="$BIN_DIR/fdkdec"

FAAC_SRC="${FAAC_SRC:-$BENCH_ROOT/../faac}"
if [[ ! -f "$FAAC_SRC/include/faam.h" ]]; then
    echo "error: faam.h not found under $FAAC_SRC/include (set FAAC_SRC to a faac checkout)" >&2
    exit 1
fi

# fdkdec uses FAAM for MP4 sample and gapless metadata. Prefer the standard
# sibling build, while allowing callers to select another FAAC build with
# FAAM_LIB.
FAAM_LIB="${FAAM_LIB:-}"
if [[ -z "$FAAM_LIB" ]]; then
    FAAM_LIB="$FAAC_SRC/build/libfaam/libfaam.a"
fi
if [[ -z "$FAAM_LIB" || ! -f "$FAAM_LIB" ]]; then
    echo "error: libfaam.a not found at $FAAM_LIB (build FAAC in $FAAC_SRC/build, or set FAAM_LIB)" >&2
    exit 1
fi

if pkg-config --exists fdk-aac; then
    FDK_PREFIX="$(pkg-config --variable=prefix fdk-aac)"
    read -r -a FDK_CFLAGS <<< "$(pkg-config --cflags fdk-aac)"
    read -r -a FDK_LIBS <<< "$(pkg-config --libs fdk-aac)"
else
    FDK_PREFIX="${FDK_PREFIX:-$(brew --prefix fdk-aac 2>/dev/null || echo /opt/homebrew)}"
    FDK_CFLAGS=(-I "$FDK_PREFIX/include")
    FDK_LIBS=(-L "$FDK_PREFIX/lib" -lfdk-aac)
fi
if [[ ! -f "$FDK_PREFIX/include/fdk-aac/aacdecoder_lib.h" ]]; then
    echo "error: libfdk-aac headers not found under $FDK_PREFIX (install libfdk-aac development files)" >&2
    exit 1
fi

mkdir -p "$BIN_DIR"

echo "==> Compiling fdkdec..." >&2
gcc -O2 -o "$TARGET_BIN" "$SCRIPT_DIR/fdkdec/fdkdec.c" \
    -I "$FAAC_SRC/include" "${FDK_CFLAGS[@]}" \
    "$FAAM_LIB" "${FDK_LIBS[@]}" >&2

chmod +x "$TARGET_BIN"
echo "$TARGET_BIN"
