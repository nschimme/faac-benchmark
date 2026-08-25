#!/usr/bin/env bash
#
# fft_bench.sh - reproducible size + timing + byte-identity harness for MDCT/FFT work.
#
# Compares a baseline git ref against the current working tree, built with
# identical meson options. Reports three axes (faac priorities: perf, size,
# quality-proxy):
#   * library SIZE   - dylib + fft.o/filtbank.o text (the decider when perf is noise)
#   * TIMING         - best-of-3 N-iter encode, so real speedups clear the noise floor
#   * byte-IDENTITY  - cmp over a corpus (a bit-exact gate; only meaningful when the
#                      baseline shares this config, e.g. -r HEAD)
#
# Usage:
#   scripts/fft_bench.sh [-r BASE_REF] [-b BITRATE] [-n ITERS] [-p PRECISION]
#                        [-f FFT] [-c CORPUS_GLOB] [-S]
#
# Defaults: -r master  -b 32  -n 30  -p double  -f radix4  -c 'build/*.wav'
#   -r  baseline ref. master = cumulative-vs-mainline (default; cmp will differ).
#       Use -r HEAD to isolate an uncommitted change (cmp should stay identical).
#   -S  strict: exit nonzero if any clip differs (use with -r HEAD as a parity gate).
#
# The baseline is built in a throwaway git worktree so the working tree is untouched.
set -euo pipefail

BASE_REF=master
BITRATE=32
ITERS=30
PRECISION=double
FFT=radix4
CORPUS='build/*.wav'
STRICT=0

while getopts "r:b:n:p:f:c:Sh" opt; do
  case $opt in
    r) BASE_REF=$OPTARG ;;
    b) BITRATE=$OPTARG ;;
    n) ITERS=$OPTARG ;;
    p) PRECISION=$OPTARG ;;
    f) FFT=$OPTARG ;;
    c) CORPUS=$OPTARG ;;
    S) STRICT=1 ;;
    h) sed -n '2,25p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done

# Path to the faac source repo. Override with FAAC_REPO when running this from
# the faac-benchmark checkout (its default sibling location is assumed otherwise).
REPO=${FAAC_REPO:-"$HOME/gitprojects/faac"}
cd "$REPO"
WORK=$(mktemp -d)
BASE_WT="$WORK/base"
trap 'git worktree remove --force "$BASE_WT" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

echo "== config: $PRECISION / $FFT, -b $BITRATE, $ITERS iters, baseline=$BASE_REF =="

# Build a source tree into a build dir, passing only the meson options that tree
# actually declares (older refs like master lack -Dfft-type / -Dfloating-point).
build_tree() {
  local src=$1 bld=$2 opts="-Dbuildtype=release"
  local avail; avail=$(cat "$src/meson_options.txt" 2>/dev/null)
  [ "$PRECISION" != double ] && echo "$avail" | grep -q "floating-point" && opts="$opts -Dfloating-point=$PRECISION"
  echo "$avail" | grep -q "fft-type" && opts="$opts -Dfft-type=$FFT"
  ( cd "$src" && meson setup "$bld" $opts >/dev/null 2>&1 )
  ninja -C "$bld" >/dev/null
}

# --- build candidate (working tree) ---
CAND_BUILD="$WORK/cand-build"
build_tree "$REPO" "$CAND_BUILD"
CAND=$CAND_BUILD/frontend/faac

# --- build baseline (worktree at BASE_REF) ---
git worktree add --detach "$BASE_WT" "$BASE_REF" >/dev/null 2>&1
BASE_BUILD="$WORK/base-build"
build_tree "$BASE_WT" "$BASE_BUILD"
BASE=$BASE_BUILD/frontend/faac

# --- library size (the decider when perf is in the noise) ---
echo "-- library size --"
libsize() {
  local bd=$1 lbl=$2
  local dy fft fb
  dy=$(stat -f%z "$bd"/libfaac/libfaac.*.dylib 2>/dev/null | head -1)
  fft=$(size "$bd"/libfaac/libfaac.a.p/fft.c.o 2>/dev/null | awk 'NR==2{print $1}')
  fb=$(size "$bd"/libfaac/libfaac.a.p/filtbank.c.o 2>/dev/null | awk 'NR==2{print $1}')
  printf "  %-10s dylib=%s  fft.o=%s  filtbank.o=%s\n" "$lbl" "${dy:-?}" "${fft:-?}" "${fb:-?}" >&2
  echo "${dy:-0} ${fft:-0} ${fb:-0}"
}
sb=$(libsize "$BASE_BUILD" baseline | tail -1)
sc=$(libsize "$CAND_BUILD" candidate | tail -1)
awk -v b="$sb" -v c="$sc" 'BEGIN{split(b,B);split(c,C);
  printf "  delta      dylib=%+d  fft.o=%+d  filtbank.o=%+d bytes (negative = smaller)\n",C[1]-B[1],C[2]-B[2],C[3]-B[3]}'

# --- byte-identity check ---
clips=( $CORPUS )
echo "-- byte-identity over ${#clips[@]} clips --"
diffs=0
for w in "${clips[@]}"; do
  "$BASE" -b "$BITRATE" "$w" -o "$WORK/b.aac" >/dev/null 2>&1
  "$CAND" -b "$BITRATE" "$w" -o "$WORK/c.aac" >/dev/null 2>&1
  cmp -s "$WORK/b.aac" "$WORK/c.aac" || diffs=$((diffs+1))
done
fail=0
if [ $diffs -eq 0 ]; then
  echo "  all identical"
elif [ $STRICT -eq 1 ]; then
  echo "  *** $diffs/${#clips[@]} differ - PARITY FAILED (strict) ***"; fail=1
else
  echo "  $diffs/${#clips[@]} differ (expected when baseline is a different config, e.g. master=radix2; pass -S with -r HEAD for a strict parity gate)"
fi

# --- timing (single largest clip, N iters, best of 3 rounds) ---
big=$(ls -S $CORPUS | head -1)
echo "-- timing on $(basename "$big"), $ITERS iters x3 rounds (best) --"
timeit() {
  local bin=$1 best=999999
  for round in 1 2 3; do
    local t
    t=$( { /usr/bin/time -p sh -c 'for i in $(seq '"$ITERS"'); do '"$bin"' -b '"$BITRATE"' "'"$big"'" -o /dev/null 2>/dev/null; done'; } 2>&1 | awk '/real/{print $2}')
    awk "BEGIN{exit !($t<$best)}" && best=$t
  done
  echo "$best"
}
tb=$(timeit "$BASE")
tc=$(timeit "$CAND")
echo "  baseline : ${tb}s"
echo "  candidate: ${tc}s"
awk "BEGIN{d=($tb-$tc)/$tb*100; printf \"  delta    : %+.1f%% (positive = candidate faster)\n\", d}"

exit $fail
