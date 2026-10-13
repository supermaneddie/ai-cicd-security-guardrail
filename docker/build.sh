#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_IMAGE="${1:-${DOCKER_IMAGE:-}}"
: "${DOCKER_IMAGE:?Pass the public_url.docker_image value as the first argument or set DOCKER_IMAGE}"

docker build \
    -t "$DOCKER_IMAGE" \
    -f "$SCRIPT_DIR/Dockerfile" \
    "$SCRIPT_DIR"

docker push "$DOCKER_IMAGE"
