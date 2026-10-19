#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POT_PROVIDER_DIR="$APP_DIR/.render/bgutil-ytdlp-pot-provider"
mkdir -p "$(dirname "$POT_PROVIDER_DIR")"
if [ ! -d "$POT_PROVIDER_DIR/server" ]; then
  git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_PROVIDER_DIR"
fi
(cd "$POT_PROVIDER_DIR/server" && npm ci && npx tsc)
test -f "$POT_PROVIDER_DIR/server/build/main.js"
