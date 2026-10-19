#!/usr/bin/env bash
set -euo pipefail

node "$HOME/bgutil-ytdlp-pot-provider/server/build/main.js" --host 127.0.0.1 --port 4416 &
POT_PROVIDER_PID=$!
trap 'kill "$POT_PROVIDER_PID" 2>/dev/null || true' EXIT

uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" &
APP_PID=$!
wait "$APP_PID"
