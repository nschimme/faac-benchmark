#!/usr/bin/env bash
#
# Build FAAD2 Decoders (Floating-Point and Fixed-Point) for compare_codecs.py
# Clones FAAD2 repository and compiles standalone faad-float and faad-fixed binaries.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD_DIR="${FAAD2_BUILD_DIR:-$BENCH_ROOT/build/faad2}"
BIN_DIR="$BENCH_ROOT/bin"
TARGET_FLOAT="$BIN_DIR/faad-float"
TARGET_FIXED="$BIN_DIR/faad-fixed"

if [[ -x "$TARGET_FLOAT" && -x "$TARGET_FIXED" ]]; then
    echo "$TARGET_FLOAT $TARGET_FIXED"
    exit 0
fi

mkdir -p "$BUILD_DIR" "$BIN_DIR"

if [[ ! -d "$BUILD_DIR/src" ]]; then
    echo "==> Cloning FAAD2 repository..." >&2
    git clone --depth 1 https://github.com/knik0/faad2.git "$BUILD_DIR/src" >&2
fi

echo "==> Building FAAD2 (Float and Fixed-Point)..." >&2
(
    cd "$BUILD_DIR"
    cmake -B build -S src -DBUILD_SHARED_LIBS=OFF >&2
    cmake --build build --target faad faad_fixed >&2

    gcc -O2 -I build/include -I src/frontend src/frontend/*.c build/libfaad.a -lm -o "$TARGET_FLOAT" >&2
    gcc -O2 -I build/include -I src/frontend src/frontend/*.c build/libfaad_fixed.a -lm -DFIXED_POINT -o "$TARGET_FIXED" >&2
) >&2

chmod +x "$TARGET_FLOAT" "$TARGET_FIXED"
echo "$TARGET_FLOAT $TARGET_FIXED"
