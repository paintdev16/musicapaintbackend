#!/usr/bin/env bash
set -euo pipefail

export DENO_INSTALL="${HOME}/.deno"
export PATH="${DENO_INSTALL}/bin:${PATH}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
