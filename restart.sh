#!/usr/bin/env bash
# Restart only this checkout's managed loop.sh child; never kill by port/PID file.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv; run uv sync --locked first" >&2
  exit 1
fi
exec .venv/bin/python scripts/service_control.py restart "$@"
