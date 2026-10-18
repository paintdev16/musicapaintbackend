#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.deno/bin:$PATH"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
