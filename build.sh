#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade -r requirements.txt
export DENO_INSTALL="${HOME}/.deno"
curl -fsSL https://deno.land/install.sh | sh
