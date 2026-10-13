#!/usr/bin/env sh
set -eu

: "${REDIS_URL:=redis://localhost:6379/0}"
: "${CODEQC_QUEUE:=codeqc}"

exec rq worker --url "$REDIS_URL" "$CODEQC_QUEUE"
