#!/usr/bin/env bash
# Run the installed environment; web.py loads .env as data, never shell code.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv; run uv sync --locked first" >&2
  exit 1
fi
exec .venv/bin/python web.py
