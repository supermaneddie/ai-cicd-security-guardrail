#!/usr/bin/env sh
set -eu

: "${CODEQC_CONFIG:=config.yaml}"
: "${CODEQC_BIND:=0.0.0.0:8080}"
: "${CODEQC_WORKERS:=4}"
export CODEQC_CONFIG

exec gunicorn --bind "$CODEQC_BIND" --workers "$CODEQC_WORKERS" --timeout 300 wsgi:app

