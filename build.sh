#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt

POT_PROVIDER_DIR="$HOME/bgutil-ytdlp-pot-provider"
if [ ! -d "$POT_PROVIDER_DIR/server" ]; then
  git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_PROVIDER_DIR"
fi
(cd "$POT_PROVIDER_DIR/server" && npm ci && npx tsc)
