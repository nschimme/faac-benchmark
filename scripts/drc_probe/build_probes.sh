#!/bin/sh
# Build the DRC oracle probes into ../../bin.
#
# fdk-aac supplies drcenc (encoder metadata oracle) and fdkdrc (decoder DRC
# application). atdrc uses Apple AudioToolbox directly and is macOS-only.
# The separate fdkdec probe is built by scripts/build_fdkdec.sh.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
fdk_prefix=${FDK_PREFIX:-$(pkg-config --variable=prefix fdk-aac)}
mkdir -p "$repo/bin"

cc -O2 -I"$fdk_prefix/include/fdk-aac" \
    -o "$repo/bin/drcenc" "$here/drcenc.c" -L"$fdk_prefix/lib" -lfdk-aac
cc -O2 -I"$fdk_prefix/include/fdk-aac" \
    -o "$repo/bin/fdkdrc" "$here/fdkdrc.c" -L"$fdk_prefix/lib" -lfdk-aac

if [ "$(uname -s)" = Darwin ]; then
    cc -O2 -o "$repo/bin/atdrc" "$here/atdrc.c" \
        -framework AudioToolbox -framework CoreFoundation
    echo "wrote $repo/bin/drcenc, fdkdrc, atdrc"
else
    echo "wrote $repo/bin/drcenc, fdkdrc; atdrc skipped (macOS only)" >&2
fi
