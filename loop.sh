#!/usr/bin/env bash
# Foreground supervisor; logs go to stdout. Do not combine with systemd/Docker.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec .venv/bin/python scripts/service_control.py run "$@"
