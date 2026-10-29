#!/usr/bin/env bash
# Portable foreground supervisor for hosts without systemd. Logs go to stdout.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mkdir -p data
chmod 700 data
if ! command -v flock >/dev/null; then
  echo "flock is required; otherwise use systemd or Docker" >&2
  exit 1
fi
exec 200>data/agy2api.lock
flock -n 200 || exit 0
child_pid=""
stop_child() {
  trap - TERM INT
  if [[ -n "$child_pid" ]]; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 0
}
trap stop_child TERM INT
while true; do
  ./restart.sh &
  child_pid=$!
  wait "$child_pid" || true
  child_pid=""
  sleep 5 &
  child_pid=$!
  wait "$child_pid" || true
  child_pid=""
done
