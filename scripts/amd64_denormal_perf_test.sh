#!/usr/bin/env bash
# Investigation script: does the "skip TNS on promoted frames" commit cause a real
# encode-time regression on x86/amd64 single-precision builds, and does the
# following "FTZ/DAZ" commit fix it?
#
# CONTEXT (see nschimme/faac PR #319, branch tns-default-on-core):
# - CI (GitHub Actions, amd64) showed a catastrophic single-precision throughput
#   regression (up to -99.8%) after commit 566d8e33 ("Skip TNS analysis on
#   frames promoted long only by td_hard hysteresis").
# - A local test on ARM (Apple Silicon) showed NO regression at all (if
#   anything, single-precision got slightly faster). ARM's NEON doesn't have
#   the x86 SSE microcode-assisted slow path for subnormal/denormal float
#   arithmetic that's the leading suspect (libfaac/quantize_sse.c's
#   sqrt(x*sfacfix) chain).
# - Commit ab5ef566 ("Flush denormals to zero on x86 SSE...") adds FTZ/DAZ at
#   faacEncOpen() to neutralize this, gated to x86 builds only.
# - IMPORTANT CAVEAT discovered separately: the CI benchmark harness caches
#   baseline encode-TIME results across runs and only re-measures the
#   candidate/PR side fresh each run -- so CI's before/after comparisons may
#   reflect cross-VM noise (different ephemeral runners), not real code
#   behavior. That's why this script does a clean SAME-MACHINE, back-to-back
#   comparison instead of trusting CI's numbers.
#
# WHAT THIS SCRIPT DOES: checks out three commits on tns-default-on-core in
# turn, builds each as a single-precision (float) build, batch-encodes a
# corpus at -b 256 three times each, and reports average wall-clock time so
# you can see whether the regression is real on actual x86 hardware, and
# whether FTZ/DAZ fixes it.
#
# REQUIREMENTS: run this ON A REAL amd64/x86_64 MACHINE (not an ARM machine,
# not QEMU/Docker emulation -- the whole point is exercising real x86 SSE
# microcode behavior, which emulation may not faithfully reproduce timing-wise).
# Needs: git, meson, ninja, a C compiler. `uname -m` should print x86_64.
#
# USAGE:
#   ./amd64_denormal_perf_test.sh [path-to-wav-corpus-dir] [faac-repo-url]
# If no corpus dir is given, it looks for ./corpus/*.wav; if that doesn't
# exist either, point it at any directory of ~20-50 real wav files (music or
# speech, a few seconds to a few minutes each -- doesn't need to be the exact
# faac-benchmark corpus, just enough audio for a stable timing signal).

set -euo pipefail

CORPUS_DIR="${1:-./corpus}"
REPO_URL="${2:-https://github.com/nschimme/faac.git}"
WORKDIR="$(mktemp -d /tmp/amd64-denormal-test.XXXXXX)"
BITRATE=256
REPS=3

echo "=== Sanity check ==="
ARCH="$(uname -m)"
echo "uname -m: $ARCH"
if [[ "$ARCH" != "x86_64" && "$ARCH" != "amd64" ]]; then
  echo "ERROR: this must run on a real x86_64/amd64 machine, not $ARCH." >&2
  echo "The whole point is exercising x86-specific SSE denormal behavior." >&2
  exit 1
fi

if [[ ! -d "$CORPUS_DIR" ]] || [[ -z "$(ls -A "$CORPUS_DIR"/*.wav 2>/dev/null)" ]]; then
  echo "ERROR: no wav files found in $CORPUS_DIR" >&2
  echo "Point this script at a directory of ~20-50 real audio wav files." >&2
  exit 1
fi
NUM_FILES=$(ls "$CORPUS_DIR"/*.wav | wc -l | tr -d ' ')
echo "Corpus: $CORPUS_DIR ($NUM_FILES wav files)"
echo "Workdir: $WORKDIR"
echo

echo "=== Cloning $REPO_URL ==="
git clone --quiet "$REPO_URL" "$WORKDIR/src"
cd "$WORKDIR/src"

# The three commits under test, oldest first:
#   BEFORE   = before the promoted-frame-skip commit (TNS always analyzed on long frames)
#   PROMOTED = with the promoted-frame-skip commit, WITHOUT the FTZ/DAZ fix
#   HEAD     = current tip, WITH both the promoted-frame-skip commit AND FTZ/DAZ
declare -A COMMITS=(
  [BEFORE]="9dba75ad"
  [PROMOTED]="566d8e33"
  [HEAD]="ab5ef566"
)

declare -A TIMES

for label in BEFORE PROMOTED HEAD; do
  sha="${COMMITS[$label]}"
  echo "=== Building $label ($sha) ==="
  git checkout --quiet "$sha"
  builddir="$WORKDIR/build_$label"
  meson setup "$builddir" -Dfloating-point=single >/dev/null
  ninja -C "$builddir" >/dev/null 2>&1
  BIN="$builddir/frontend/faac"

  # Correctness sanity check: --tns must NOT be byte-identical to --no-tns
  "$BIN" -o "$WORKDIR/check_on.aac" --tns "$CORPUS_DIR"/*.wav >/dev/null 2>&1 || true
  first_file=$(ls "$CORPUS_DIR"/*.wav | head -1)
  "$BIN" -o "$WORKDIR/check_on.aac" --tns "$first_file" >/dev/null 2>&1
  "$BIN" -o "$WORKDIR/check_off.aac" --no-tns "$first_file" >/dev/null 2>&1
  if cmp -s "$WORKDIR/check_on.aac" "$WORKDIR/check_off.aac"; then
    echo "  WARNING: --tns produced byte-identical output to --no-tns at $label (TNS may be inert here)"
  else
    echo "  OK: --tns differs from --no-tns at $label (TNS is live)"
  fi

  echo "  Timing $REPS reps of batch-encode @ -b $BITRATE over $NUM_FILES files..."
  total=0
  for rep in $(seq 1 $REPS); do
    t0=$(date +%s.%N)
    for f in "$CORPUS_DIR"/*.wav; do
      "$BIN" -o /dev/null -b $BITRATE "$f" >/dev/null 2>&1
    done
    t1=$(date +%s.%N)
    dt=$(echo "$t1 - $t0" | bc)
    echo "    rep $rep: ${dt}s"
    total=$(echo "$total + $dt" | bc)
  done
  avg=$(echo "scale=4; $total / $REPS" | bc)
  TIMES[$label]="$avg"
  echo "  Average: ${avg}s"
  echo
done

echo "=== Summary ==="
printf "%-10s %10s\n" "Variant" "Avg time"
for label in BEFORE PROMOTED HEAD; do
  printf "%-10s %10ss\n" "$label" "${TIMES[$label]}"
done
echo

before="${TIMES[BEFORE]}"
promoted="${TIMES[PROMOTED]}"
head_t="${TIMES[HEAD]}"

pct_promoted=$(echo "scale=1; ($promoted - $before) / $before * 100" | bc)
pct_head=$(echo "scale=1; ($head_t - $before) / $before * 100" | bc)
pct_fix=$(echo "scale=1; ($head_t - $promoted) / $promoted * 100" | bc)

echo "PROMOTED vs BEFORE: ${pct_promoted}% (does the promoted-skip commit alone regress on real x86?)"
echo "HEAD vs BEFORE:     ${pct_head}% (net effect of both commits together)"
echo "HEAD vs PROMOTED:   ${pct_fix}% (does FTZ/DAZ recover the PROMOTED regression, if any?)"
echo
echo "Workdir left at $WORKDIR for inspection (not auto-cleaned)."
