#!/usr/bin/env bash
# Install a pinned ESP-IDF under scripts/esp32/.toolchain (never system-wide).
# Usage: scripts/esp32/setup_idf.sh   then   . scripts/esp32/.toolchain/esp-idf/export.sh
set -euo pipefail

IDF_TAG="${IDF_TAG:-v5.5.5}"
TARGETS="${IDF_TARGETS:-esp32,esp32s3,esp32c6}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/.toolchain"
IDF="$TOOL/esp-idf"

mkdir -p "$TOOL"
if [ ! -d "$IDF/.git" ]; then
    git clone --depth 1 --branch "$IDF_TAG" --recursive --shallow-submodules \
        https://github.com/espressif/esp-idf.git "$IDF"
fi
(cd "$IDF" && test "$(git describe --tags --exact-match 2>/dev/null)" = "$IDF_TAG") \
    || { echo "ESP-IDF at $IDF is not $IDF_TAG" >&2; exit 1; }

export IDF_TOOLS_PATH="$TOOL/tools"
"$IDF/install.sh" "$TARGETS"
echo "ESP-IDF $IDF_TAG ready. Activate with:"
echo "  export IDF_TOOLS_PATH=$IDF_TOOLS_PATH && . $IDF/export.sh"
