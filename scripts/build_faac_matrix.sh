#!/usr/bin/env bash
#
# Build a matrix of FAAC releases (plus the current checkout's HEAD) as
# separate git worktrees, and optionally feed all of them into
# compare_codecs.py alongside every other encoder it can detect.
#
# Usage:
#   scripts/build_faac_matrix.sh [--run] [--tags TAG1,TAG2,...] [--gate]
#
#   --run          Also invoke compare_codecs.py once all versions are
#                   built (default: just build and print the resolved
#                   --faac-bin list).
#   --tags LIST    Comma-separated FAAC repo tags to build.
#                   Default: faac-1.31.1,faac-1.40,faac-1.50,faac-2.0,faac-2.1
#                   (HEAD is always included in addition to these.)
#   --gate         Pass --gate through to compare_codecs.py (default: full
#                   coverage, i.e. no --gate).
#
# Environment:
#   FAAC_REPO       Path to the faac checkout (default: ../faac relative to
#                   this repo).
#   FAAC_BUILD_ROOT Where per-tag worktrees/builds are created (default:
#                   ../faac-multibuild relative to this repo).
#
# Safe to re-run: already-built versions are skipped.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FAAC_REPO="${FAAC_REPO:-$(cd "$BENCH_ROOT/../faac" && pwd)}"
FAAC_BUILD_ROOT="${FAAC_BUILD_ROOT:-$BENCH_ROOT/../faac-multibuild}"

TAGS="faac-1.31.1,faac-1.40,faac-1.50,faac-2.0,faac-2.1"
DO_RUN=0
GATE_FLAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --run) DO_RUN=1; shift ;;
        --tags) TAGS="$2"; shift 2 ;;
        --gate) GATE_FLAG="--gate"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$FAAC_BUILD_ROOT"

# uname -s -> darwin/linux for the dynamic loader env var this platform wants.
OS="$(uname -s)"
if [[ "$OS" == "Darwin" ]]; then
    LOADER_VAR="DYLD_LIBRARY_PATH"
else
    LOADER_VAR="LD_LIBRARY_PATH"
fi

# make_wrapper REAL_BIN LIB_DIR WRAPPER_PATH -> writes a loader-env-setting
# wrapper script. Always used, on every platform and every build system: a
# shared libfaac is only guaranteed to be found without help when the binary
# embeds a working rpath back to its own build tree, and that's a property
# of the toolchain/platform (meson's build-rpath default, Linux distro
# linker flags, etc.), not something this script can assume holds -- e.g. it
# does not on Linux, where a meson-built faac still fails to find libfaac.so
# without LD_LIBRARY_PATH set explicitly. Wrapping unconditionally means one
# code path works everywhere instead of silently depending on rpath.
make_wrapper() {
    local real_bin="$1" lib_dir="$2" wrapper="$3"
    cat > "$wrapper" <<EOF
#!/bin/sh
export $LOADER_VAR="$lib_dir\${$LOADER_VAR:+:\$$LOADER_VAR}"
exec "$real_bin" "\$@"
EOF
    chmod +x "$wrapper"
}

# build_one TAG -> prints the resolved faac binary path to run on stdout.
build_one() {
    local tag="$1"
    local wt="$FAAC_BUILD_ROOT/$tag"

    if [[ ! -d "$wt" ]]; then
        echo "==> Adding worktree for $tag" >&2
        git -C "$FAAC_REPO" worktree add --detach "$wt" "$tag" >&2
    fi

    if [[ -f "$wt/meson.build" ]]; then
        if [[ ! -f "$wt/build/build.ninja" ]]; then
            echo "==> [$tag] meson setup" >&2
            (cd "$wt" && meson setup build --buildtype=release) >&2
        fi
        echo "==> [$tag] ninja" >&2
        ninja -C "$wt/build" >&2
        # This also sidesteps compare_codecs.py's --faac-lib list, which is
        # positionally matched to --faac-bin and would misalign if only some
        # binaries in the list needed an override.
        make_wrapper "$wt/build/frontend/faac" "$wt/build/libfaac" "$wt/faac-wrapped.sh"
        echo "$wt/faac-wrapped.sh"
        return
    fi

    # Autotools (pre-1.40): configure.ac + Makefile.am.
    if [[ ! -x "$wt/frontend/.libs/faac" ]]; then
        echo "==> [$tag] autoreconf/configure/make" >&2
        (cd "$wt" && autoreconf -fi && ./configure && make -j"$(getconf _NPROCESSORS_ONLN)") >&2
    fi

    local real_bin="$wt/frontend/.libs/faac"
    local lib_dir="$wt/libfaac/.libs"

    # The autotools build additionally hardcodes the install prefix
    # (/usr/local/lib) as the dylib's load path on macOS, which is never
    # where we actually built it. install_name_tool can't rewrite it after
    # the fact ("larger updated load commands do not fit" -- the binary
    # would need relinking with -headerpad_max_install_names), so the
    # wrapper is the fix here too, not just a Linux-only concern.
    local wrapper="$wt/faac-wrapped.sh"
    make_wrapper "$real_bin" "$lib_dir" "$wrapper"
    echo "$wrapper"
}

FAAC_BINS=()

echo "==> Building HEAD ($FAAC_REPO)" >&2
if [[ ! -f "$FAAC_REPO/build/build.ninja" ]]; then
    (cd "$FAAC_REPO" && meson setup build --buildtype=release) >&2
fi
ninja -C "$FAAC_REPO/build" >&2
FAAC_BINS+=("$FAAC_REPO/build/frontend/faac")

IFS=',' read -ra TAG_LIST <<< "$TAGS"
for tag in "${TAG_LIST[@]}"; do
    bin_path="$(build_one "$tag")"
    FAAC_BINS+=("$bin_path")
done

echo "==> Verifying all binaries run" >&2
# faac never prints its version from a bare invocation -- the banner is only
# emitted mid-encode (see probe_faac_version in compare_codecs.py) -- so
# verify with a real throwaway encode via that same helper rather than
# grepping --help output.
python3 - "$BENCH_ROOT" "${FAAC_BINS[@]}" <<'PYEOF' >&2
import sys
sys.path.insert(0, sys.argv[1])
from compare_codecs import probe_faac_version

failed = False
for b in sys.argv[2:]:
    ver = probe_faac_version(b)
    print(f"  {b} -> {ver or 'FAILED TO RUN'}")
    if not ver:
        failed = True
if failed:
    sys.exit(1)
PYEOF

FAAC_BIN_ARG="$(IFS=,; echo "${FAAC_BINS[*]}")"
echo "$FAAC_BIN_ARG"

if [[ "$DO_RUN" -eq 1 ]]; then
    echo "==> Running compare_codecs.py" >&2
    cd "$BENCH_ROOT"
    python3 compare_codecs.py \
        --faac-bin "$FAAC_BIN_ARG" \
        --include-other-codecs \
        $GATE_FLAG
fi
