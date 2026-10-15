#!/bin/sh
# Builds the Opendigitalradio fork of fdk-aac into $ODR_PREFIX (default
# ./odr-fdk). It is the only fdk at hand whose encoder accepts a 960-sample
# granule (stock fdk rejects AACENC_GRANULE_LENGTH = 960), so it is the source
# of independent DAB+ test streams. Needs autoconf, automake, libtool.
set -e
prefix=${ODR_PREFIX:-$PWD/odr-fdk}
src=$prefix/src
mkdir -p "$prefix"
[ -d "$src" ] || git clone --depth 1 https://github.com/Opendigitalradio/fdk-aac.git "$src"
cd "$src"
./bootstrap >/dev/null
./configure --disable-shared --prefix="$prefix" >/dev/null
make -j"$(getconf _NPROCESSORS_ONLN)" >/dev/null
make install >/dev/null
echo "installed to $prefix"
