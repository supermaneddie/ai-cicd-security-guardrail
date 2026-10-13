#!/usr/bin/env bash
set -euo pipefail

./scripts/worker.sh &
worker_pid=$!
./scripts/start.sh &
web_pid=$!
./scripts/websocket.sh &
websocket_pid=$!

shutdown() {
  kill "$websocket_pid" "$web_pid" "$worker_pid" 2>/dev/null || true
  wait "$websocket_pid" "$web_pid" "$worker_pid" 2>/dev/null || true
}
trap shutdown EXIT INT TERM

wait -n "$websocket_pid" "$web_pid" "$worker_pid"
