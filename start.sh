#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POT_PROVIDER_ENTRY="$APP_DIR/.render/bgutil-ytdlp-pot-provider/server/build/main.js"
if [ ! -f "$POT_PROVIDER_ENTRY" ]; then
  echo "PO token provider is missing: $POT_PROVIDER_ENTRY" >&2
  exit 1
fi

node "$POT_PROVIDER_ENTRY" --host 127.0.0.1 --port 4416 &
POT_PROVIDER_PID=$!
trap 'kill "$POT_PROVIDER_PID" 2>/dev/null || true' EXIT

for attempt in $(seq 1 20); do
  if node -e 'const net=require("net");const s=net.connect(4416,"127.0.0.1",()=>{s.end();process.exit(0)});s.on("error",()=>process.exit(1));setTimeout(()=>process.exit(1),400)' ; then
    break
  fi
  if [ "$attempt" -eq 20 ]; then
    echo "PO token provider did not start on 127.0.0.1:4416" >&2
    exit 1
  fi
  sleep 0.5
done

uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" &
APP_PID=$!
wait "$APP_PID"
