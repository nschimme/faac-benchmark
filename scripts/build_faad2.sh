#!/usr/bin/env bash
#
# Build the reference FAAD2 decoder (faad) for the decoder gate and the
# conformance reference.
#
# Pinned to a release rather than the distro package: the Ubuntu package lags
# upstream, and 2.11.4 is the first release that trims the encoder delay and the
# SBR decoder delay from MP4 output using the elst atom and iTunSMPB (gapless),
# so decodes line up with the source without a search.
#
# Prints the path of the built binary. Idempotent: reuses bin/faad when it was
# built from this version.
#

set -euo pipefail

FAAD2_VERSION="2.11.4"
FAAD2_SHA256="ee479ccbae4a8387ab696e6f21a481bd83fe3881471cafa81b4ae59d7d3aed43"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD_DIR="${FAAD2_BUILD_DIR:-$BENCH_ROOT/build/faad2-$FAAD2_VERSION}"
BIN_DIR="$BENCH_ROOT/bin"
TARGET_BIN="$BIN_DIR/faad"
STAMP="$BIN_DIR/faad.version"

if [[ -x "$TARGET_BIN" && "$(cat "$STAMP" 2>/dev/null)" == "$FAAD2_VERSION" ]]; then
    echo "$TARGET_BIN"
    exit 0
fi

mkdir -p "$BUILD_DIR" "$BIN_DIR"
TARBALL="$BUILD_DIR/faad2-$FAAD2_VERSION.tar.gz"

if [[ ! -f "$TARBALL" ]]; then
    echo "==> Downloading FAAD2 $FAAD2_VERSION..." >&2
    curl -fsSL -o "$TARBALL" "https://github.com/knik0/faad2/archive/refs/tags/$FAAD2_VERSION.tar.gz" >&2
fi

if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
else
    ACTUAL="$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)"
fi
if [[ "$ACTUAL" != "$FAAD2_SHA256" ]]; then
    echo "error: FAAD2 $FAAD2_VERSION checksum mismatch (got $ACTUAL)" >&2
    rm -f "$TARBALL"
    exit 1
fi

SRC_DIR="$BUILD_DIR/src"
rm -rf "$SRC_DIR"
mkdir -p "$SRC_DIR"
tar -xzf "$TARBALL" -C "$SRC_DIR" --strip-components=1

echo "==> Building FAAD2 $FAAD2_VERSION..." >&2
cmake -S "$SRC_DIR" -B "$BUILD_DIR/build" -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF -DFAAD_BUILD_CLI=ON >&2
cmake --build "$BUILD_DIR/build" --parallel >&2

BUILT="$(find "$BUILD_DIR/build" -type f -name faad -perm -u+x | head -n 1)"
if [[ -z "$BUILT" ]]; then
    echo "error: the FAAD2 build produced no faad binary" >&2
    exit 1
fi
cp "$BUILT" "$TARGET_BIN"
echo "$FAAD2_VERSION" > "$STAMP"
echo "$TARGET_BIN"
