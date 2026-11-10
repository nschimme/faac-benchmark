#!/usr/bin/env bash
#
# Build libxaac encoder and decoder for compare_codecs.py
# Clones https://github.com/ittiam-systems/libxaac into build/libxaac
# and builds binaries with CMake, installing them to bin/xaacenc and bin/xaacdec.
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
fi

mkdir -p "$BUILD_DIR" "$BIN_DIR"

# If faam tool is present under FAAC build or checkout, install it to bin/
if [[ ! -x "$BIN_DIR/faam" ]]; then
    if [[ -x "$FAAC_SRC/build/frontend/faam" ]]; then
        cp "$FAAC_SRC/build/frontend/faam" "$BIN_DIR/faam"
    elif [[ -f "$FAAC_SRC/meson.build" ]]; then
        (
            cd "$FAAC_SRC"
            if [[ ! -d "build" ]]; then
                meson setup build -Dfrontend-static=true >&2
            else
                meson configure build -Dfrontend-static=true >&2
            fi
            ninja -C build >&2
            cp build/frontend/faam "$BIN_DIR/faam"
        ) || true
    fi
fi

if [[ ! -d "$BUILD_DIR/src" ]]; then
    echo "==> Cloning libxaac..." >&2
    git clone --depth 1 https://github.com/ittiam-systems/libxaac.git "$BUILD_DIR/src" >&2
fi

XAAC_SRC="$BUILD_DIR/src"

if [[ ! -f "$TARGET_ENC" || ! -f "$TARGET_DEC" ]]; then
    echo "==> Building libxaac with CMake..." >&2
    mkdir -p "$BUILD_DIR/build"
    (
        cd "$BUILD_DIR/build"
        cmake "$XAAC_SRC"
        make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
    ) >&2

    cp "$BUILD_DIR/build/xaacenc" "$TARGET_ENC"
    cp "$BUILD_DIR/build/xaacdec" "$TARGET_DEC"

    chmod +x "$TARGET_ENC" "$TARGET_DEC"
fi

echo "$TARGET_ENC"
echo "$TARGET_DEC"
