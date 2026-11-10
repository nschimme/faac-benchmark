#!/usr/bin/env bash
#
# Build libxaac encoder and decoder with native gapless M4A FAAM support
# for compare_codecs.py.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD_DIR="${XAAC_BUILD_DIR:-$BENCH_ROOT/build/libxaac}"
BIN_DIR="$BENCH_ROOT/bin"
TARGET_ENC="$BIN_DIR/xaacenc"
TARGET_DEC="$BIN_DIR/xaacdec"

FAAC_SRC="${FAAC_SRC:-$BENCH_ROOT/../faac}"
if [[ ! -d "$FAAC_SRC" && -d "$BENCH_ROOT/build/faac/src" ]]; then
    FAAC_SRC="$BENCH_ROOT/build/faac/src"
elif [[ ! -d "$FAAC_SRC" ]]; then
    mkdir -p "$BENCH_ROOT/build/faac"
    git clone --depth 1 https://github.com/FreewareAdvancedAudio/faac.git "$BENCH_ROOT/build/faac/src" >&2
    FAAC_SRC="$BENCH_ROOT/build/faac/src"
fi

mkdir -p "$BUILD_DIR" "$BIN_DIR"

# Ensure faam & libfaam are built
if [[ ! -f "$FAAC_SRC/build/libfaam/libfaam.a" || ! -x "$BIN_DIR/faam" ]]; then
    echo "==> Building faam & libfaam from FAAC..." >&2
    (
        cd "$FAAC_SRC"
        if [[ ! -d "build" ]]; then
            meson setup build -Dfrontend-static=true >&2
        else
            meson configure build -Dfrontend-static=true >&2
        fi
        ninja -C build >&2
        cp build/frontend/faam "$BIN_DIR/faam"
    ) >&2
fi

if [[ ! -d "$BUILD_DIR/src" ]]; then
    echo "==> Cloning libxaac..." >&2
    git clone --depth 1 https://github.com/ittiam-systems/libxaac.git "$BUILD_DIR/src" >&2
fi

XAAC_SRC="$BUILD_DIR/src"

if [[ ! -f "$BIN_DIR/xaacenc_core" || ! -f "$BIN_DIR/xaacdec_core" ]]; then
    echo "==> Building libxaac core with CMake..." >&2
    mkdir -p "$BUILD_DIR/build"
    (
        cd "$BUILD_DIR/build"
        cmake "$XAAC_SRC" >&2
        make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)" >&2
    ) >&2
    cp "$BUILD_DIR/build/xaacenc" "$BIN_DIR/xaacenc_core"
    cp "$BUILD_DIR/build/xaacdec" "$BIN_DIR/xaacdec_core"
fi

echo "==> Compiling native M4A/gapless xaacenc & xaacdec..." >&2
gcc -O2 -o "$TARGET_ENC" "$SCRIPT_DIR/xaacenc/xaacenc.c" \
    -I "$FAAC_SRC/include" "$FAAC_SRC/build/libfaam/libfaam.a" >&2

gcc -O2 -o "$TARGET_DEC" "$SCRIPT_DIR/xaacdec/xaacdec.c" \
    -I "$FAAC_SRC/include" "$FAAC_SRC/build/libfaam/libfaam.a" >&2

chmod +x "$TARGET_ENC" "$TARGET_DEC" "$BIN_DIR/xaacenc_core" "$BIN_DIR/xaacdec_core"

echo "$TARGET_ENC"
echo "$TARGET_DEC"
