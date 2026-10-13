#!/usr/bin/env bash
set -euo pipefail

: "${CODEQC_CONFIG:=config.yaml}"
: "${CODEQC_START_WORKER:=1}"
export CODEQC_CONFIG FLASK_APP=wsgi:app FLASK_DEBUG=1

if [[ "$CODEQC_START_WORKER" == "0" ]]; then
  exec flask run --host 0.0.0.0 --port "${CODEQC_PORT:-8080}"
fi

./scripts/worker.sh &
worker_pid=$!

cleanup() {
  kill "$worker_pid" 2>/dev/null || true
  wait "$worker_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

flask run --host 0.0.0.0 --port "${CODEQC_PORT:-8080}"
