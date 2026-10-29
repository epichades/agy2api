#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# 启动只运行当前代码；更新请单独执行并保留本地修改。
uv sync --locked --no-dev
exec ./loop.sh
