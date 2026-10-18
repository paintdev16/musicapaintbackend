#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt

if ! command -v deno >/dev/null 2>&1; then
  curl -fsSL https://deno.land/install.sh | sh
fi

export PATH="$HOME/.deno/bin:$PATH"
deno --version
