#!/usr/bin/env bash
#
# Build or verify qaac64 Docker/Podman image for Apple AudioToolbox proxy encoding/decoding.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE_DIR="$BENCH_ROOT/docker/qaac"
IMAGE_TAG="${QAAC_IMAGE_TAG:-qaac64:latest}"

CONTAINER_ENGINE=""
if command -v docker &>/dev/null && docker info &>/dev/null; then
    CONTAINER_ENGINE="docker"
elif command -v podman &>/dev/null && podman info &>/dev/null; then
    CONTAINER_ENGINE="podman"
elif command -v docker &>/dev/null; then
    CONTAINER_ENGINE="docker"
elif command -v podman &>/dev/null; then
    CONTAINER_ENGINE="podman"
fi

if [[ -z "$CONTAINER_ENGINE" ]]; then
    echo "Error: Neither docker nor podman is available." >&2
    exit 1
fi

# Check if image exists
if "$CONTAINER_ENGINE" image inspect "$IMAGE_TAG" &>/dev/null; then
    echo "$IMAGE_TAG"
    exit 0
fi

echo "==> Building $IMAGE_TAG image using $CONTAINER_ENGINE..." >&2
"$CONTAINER_ENGINE" build -t "$IMAGE_TAG" "$DOCKERFILE_DIR" >&2

echo "$IMAGE_TAG"
