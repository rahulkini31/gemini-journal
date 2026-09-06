#!/usr/bin/env bash
# Graceful stop: SIGINT lets the runner finish its cycle and log runner_stop.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f state/runner.pid ] || { echo "no pidfile; not running"; exit 0; }
PID=$(cat state/runner.pid)
if ! kill -0 "$PID" 2>/dev/null; then
  echo "PID $PID not alive; clearing pidfile"; rm -f state/runner.pid; exit 0
fi
echo "stopping PID $PID..."
kill -INT "$PID"
for _ in $(seq 1 15); do
  sleep 1
  kill -0 "$PID" 2>/dev/null || { echo "stopped cleanly"; rm -f state/runner.pid; exit 0; }
done
echo "did not stop in 15s; sending SIGTERM"
kill -TERM "$PID" 2>/dev/null || true
rm -f state/runner.pid
