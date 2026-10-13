#!/usr/bin/env sh
set -eu

: "${CODEQC_WS_HOST:=0.0.0.0}"
: "${CODEQC_WS_PORT:=8081}"
export CODEQC_WS_HOST CODEQC_WS_PORT

exec python -m codeqc.websocket
