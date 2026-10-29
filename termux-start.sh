#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Lock includes Python 3.13; use a matching Termux runtime, never rewrite metadata.
uv sync --locked --no-dev --python "$(command -v python)"
exec .venv/bin/python web.py
