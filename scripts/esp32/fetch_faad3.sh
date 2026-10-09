#!/usr/bin/env bash
# Export a snapshot of the FAAD3 decoder (libfaad + shared common code) into scripts/esp32/.toolchain/<dest>.
# Read-only on the faac checkout. REF is a git ref (exported with git archive) or a worktree directory
# (copied as it is, uncommitted changes included; HEAD and a hash of the diff are recorded).
# Usage: fetch_faad3.sh [ref-or-dir] [dest]     (defaults: origin/stack-tests, faad3-src; FAAC_REPO=../faac)
set -euo pipefail

REF="${1:-origin/stack-tests}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${FAAC_REPO:-$HERE/../../../faac}"
DEST="$HERE/.toolchain/${2:-faad3-src}"

rm -rf "$DEST"
mkdir -p "$DEST"
if [ -d "$REF" ]; then
    SRC="$(cd "$REF" && pwd)"
    (cd "$SRC" && tar --exclude='libfaad/test_*' -c libfaad common include) | tar -x -C "$DEST"
    DIFF="$(git -C "$SRC" diff HEAD -- libfaad common include | sha1sum | cut -c1-12)"
    echo "$(git -C "$SRC" rev-parse HEAD) + working tree (diff $DIFF)" > "$DEST/COMMIT"
else
    COMMIT="$(git -C "$REPO" rev-parse --verify "$REF^{commit}")"
    git -C "$REPO" archive "$COMMIT" libfaad common include | tar -x -C "$DEST"
    echo "$COMMIT" > "$DEST/COMMIT"
fi
echo "FAAD3 $REF -> $DEST ($(cut -c1-60 "$DEST/COMMIT"))"
