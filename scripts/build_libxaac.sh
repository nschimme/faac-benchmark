#!/usr/bin/env bash
#
# Build libxaac encoder and decoder for compare_codecs.py
# Clones https://github.com/ittiam-systems/libxaac into build/libxaac
# and builds static libraries with CMake, then compiles standalone C frontends
# linked directly with libxaac and libfaam (in-process, no subprocess forks).
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD_DIR="${XAAC_BUILD_DIR:-$BENCH_ROOT/build/libxaac}"
FAAC_DIR="${FAAC_DIR:-$BENCH_ROOT/build/faac/src}"
BIN_DIR="$BENCH_ROOT/bin"

mkdir -p "$BIN_DIR" "$BUILD_DIR"

# Ensure FAAC / libfaam is checked out and built
if [[ ! -d "$FAAC_DIR" && -d "$BENCH_ROOT/../faac" ]]; then
    FAAC_DIR="$BENCH_ROOT/../faac"
fi

if [[ ! -d "$FAAC_DIR" ]]; then
    echo "==> Cloning FAAC (for libfaam)..." >&2
    mkdir -p "$(dirname "$FAAC_DIR")"
    git clone --depth 1 https://github.com/FreewareAdvancedAudio/faac.git "$FAAC_DIR" >&2
fi

LIBFAAM_A="$FAAC_DIR/build/libfaam/libfaam.a"
if [[ ! -f "$LIBFAAM_A" ]]; then
    echo "==> Building libfaam..." >&2
    (
        cd "$FAAC_DIR"
        if [[ ! -d "build" ]]; then
            meson setup build -Dfrontend-static=true >&2
        else
            meson configure build -Dfrontend-static=true >&2
        fi
        ninja -C build >&2
    )
fi

# Clone and build libxaac static libraries
if [[ ! -d "$BUILD_DIR/src" ]]; then
    echo "==> Cloning libxaac..." >&2
    git clone --depth 1 https://github.com/ittiam-systems/libxaac.git "$BUILD_DIR/src" >&2
fi

XAAC_SRC="$BUILD_DIR/src"

# Patch ixheaace_testbench.c to support WAV files with LIST/INFO metadata chunks
if grep -q "count > 40" "$XAAC_SRC/test/encoder/ixheaace_testbench.c" 2>/dev/null; then
    sed -i 's/count > 40/count > 65536/g' "$XAAC_SRC/test/encoder/ixheaace_testbench.c"
fi

echo "==> Building libxaac static libraries with CMake..." >&2
mkdir -p "$BUILD_DIR/build"
(
    cd "$BUILD_DIR/build"
    cmake "$XAAC_SRC" >&2
    make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)" libxaacenc libxaacdec >&2
)

# Compile testbench support objects with renamed main to avoid symbol collisions
echo "==> Compiling in-process frontends linked with libxaac and libfaam..." >&2
gcc -O3 -c \
    -I"$XAAC_SRC/encoder" \
    -I"$XAAC_SRC/encoder/drc_src" \
    -I"$XAAC_SRC/common" \
    -I"$XAAC_SRC/test/encoder" \
    -Dmain=libxaac_enc_testbench_main \
    "$XAAC_SRC/test/encoder/ixheaace_testbench.c" \
    -o "$BUILD_DIR/build/ixheaace_testbench.o"

gcc -O3 \
    -I"$XAAC_SRC/encoder" \
    -I"$XAAC_SRC/encoder/drc_src" \
    -I"$XAAC_SRC/common" \
    -I"$XAAC_SRC/test/encoder" \
    -I"$FAAC_DIR/include" \
    "$BENCH_ROOT/scripts/xaacenc/xaacenc.c" \
    "$BUILD_DIR/build/ixheaace_testbench.o" \
    "$XAAC_SRC/test/encoder/ixheaace_error.c" \
    "$XAAC_SRC/test/encoder/impd_drc_user_config.c" \
    "$LIBFAAM_A" \
    "$BUILD_DIR/build/libxaacenc.a" \
    -lm -lpthread \
    -o "$BIN_DIR/xaacenc"

gcc -O3 -c \
    -I"$XAAC_SRC/decoder" \
    -I"$XAAC_SRC/decoder/drc_src" \
    -I"$XAAC_SRC/common" \
    -I"$XAAC_SRC/test/decoder" \
    -Dmain=libxaac_dec_testbench_main \
    "$XAAC_SRC/test/decoder/ixheaacd_main.c" \
    -o "$BUILD_DIR/build/ixheaacd_main.o"

gcc -O3 \
    -I"$XAAC_SRC/decoder" \
    -I"$XAAC_SRC/decoder/drc_src" \
    -I"$XAAC_SRC/common" \
    -I"$XAAC_SRC/test/decoder" \
    -I"$FAAC_DIR/include" \
    "$BENCH_ROOT/scripts/xaacdec/xaacdec.c" \
    "$BUILD_DIR/build/ixheaacd_main.o" \
    "$XAAC_SRC/test/decoder/ixheaacd_error.c" \
    "$XAAC_SRC/test/decoder/ixheaacd_fileifc.c" \
    "$XAAC_SRC/test/decoder/ixheaacd_metadata_read.c" \
    "$LIBFAAM_A" \
    "$BUILD_DIR/build/libxaacdec.a" \
    -lm -lpthread \
    -o "$BIN_DIR/xaacdec"

chmod +x "$BIN_DIR/xaacenc" "$BIN_DIR/xaacdec"

# Clean up any leftover helper binaries
rm -f "$BIN_DIR/xaacenc_core" "$BIN_DIR/xaacdec_core"

echo "$BIN_DIR/xaacenc"
echo "$BIN_DIR/xaacdec"
