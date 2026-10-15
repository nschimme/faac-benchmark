#!/bin/sh
# Encodes 960-sample streams with the ODR fdk fork, decodes each with libfaad3,
# FDK and FFmpeg (and FAAD2 when FAAD2_DIR is set) and prints how closely they
# agree. See README.md.
#
#   FAAC_REPO   faac checkout built with meson (default ../faac)
#   FAAC_BUILD  its build directory (default $FAAC_REPO/build)
#   ODR_PREFIX  prefix of build_odr_fdk.sh's install (default ./odr-fdk)
#   SOURCE_WAV  a 16-bit stereo 48 kHz wav (first 10 s are used)
#   FAAD2_DIR   optional FAAD2 build dir with libfaad.dylib/.so and include/
set -e
here=$(cd "$(dirname "$0")" && pwd)
faac=${FAAC_REPO:-$here/../../../faac}
build=${FAAC_BUILD:-$faac/build}
odr=${ODR_PREFIX:-$PWD/odr-fdk}
wav=${SOURCE_WAV:?set SOURCE_WAV}
work=${WORK_DIR:-$PWD/dabplus_960_work}
mkdir -p "$work"
cc=${CC:-cc}

lib=$(ls "$build"/libfaad/libfaad3.a 2>/dev/null) || { echo "no $build/libfaad/libfaad3.a: build faac with -Ddefault_library=static or both"; exit 1; }
$cc -O1 -o "$work/enc960" "$here/enc.c" -I"$odr/include" "$odr/lib/libfdk-aac.a" -lc++ 2>/dev/null || \
$cc -O1 -o "$work/enc960" "$here/enc.c" -I"$odr/include" "$odr/lib/libfdk-aac.a" -lstdc++ -lm
$cc -O1 -o "$work/dec3" "$here/dec3.c" -I"$faac/include" "$lib" -lm
$cc -O1 -o "$work/decfdk" "$here/decfdk.c" $(pkg-config --cflags --libs fdk-aac 2>/dev/null || echo "-lfdk-aac")
$cc -O2 -o "$work/psm" "$here/psm.c" -lm
$cc -O2 -o "$work/snr" "$here/snr.c" -lm

one() { # name aot bitrate channels rate
    n=$1; aot=$2; br=$3; ch=$4; sr=$5
    "$work/enc960" "$wav" "$work/$n.dab" "$aot" "$br" 960 "$ch" "$sr" >/dev/null
    python3 -I "$here/loas.py" "$work/$n.dab" "$work/$n.loas"
    "$work/dec3" "$work/$n.dab" "$work/$n.f3.pcm" >/dev/null
    "$work/decfdk" "$work/$n.dab" "$work/$n.fdk.pcm" >/dev/null
    ffmpeg -v error -y -f loas -i "$work/$n.loas" -f s16le -ac "$ch" -ar "$sr" "$work/$n.ff.pcm" 2>/dev/null || true
    printf '%-10s ' "$n"
    if [ "$ch" = 1 ]; then
        # FDK upmixes mono HE-AAC to duplicated stereo; compare its left channel
        python3 -I - "$work/$n.fdk.pcm" "$work/$n.f3.pcm" <<'PY'
import array, os, sys
fdk, f3 = sys.argv[1], sys.argv[2]
if os.path.getsize(fdk) == 2 * os.path.getsize(f3):
    a = array.array('h'); a.frombytes(open(fdk, 'rb').read())
    open(fdk, 'wb').write(a[0::2].tobytes())
PY
        printf 'vs fdk: %s\n' "$("$work/snr" "$work/$n.f3.pcm" "$work/$n.fdk.pcm" 100000 20000 4000 2>&1)"
    else
        printf 'vs fdk: %s\n' "$("$work/psm" "$work/$n.f3.pcm" "$work/$n.fdk.pcm" 100000 10000 4000 2>&1)"
    fi
}

# name aot bitrate channels rate   (aot 2 LC, 5 HE v1, 29 HE v2)
one lc_st48    2 128000 2 48000
one lc_st32    2  96000 2 32000
one lc_mono32  2  64000 1 32000
one lc_mono48  2  64000 1 48000
one he1_st32   5  48000 2 32000
one he1_mono48 5  40000 1 48000
one he2_st32  29  32000 2 32000
one he2_st48  29  40000 2 48000
