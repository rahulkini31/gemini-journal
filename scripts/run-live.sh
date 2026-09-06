#!/usr/bin/env bash
# Start the Bookbound live loop detached, logging to state/runner.log.
#
#   bash scripts/run-live.sh            # DRY RUN - no orders placed
#   bash scripts/run-live.sh --live     # places orders on the paper account
#
# Stop with:  bash scripts/stop-live.sh
# Watch with: tail -f state/runner.log
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONPATH=src

mkdir -p state
if [ -f state/runner.pid ] && kill -0 "$(cat state/runner.pid)" 2>/dev/null; then
  echo "already running as PID $(cat state/runner.pid)" >&2
  exit 1
fi

INTERVAL="${INTERVAL:-300}"
# python -u as well as the runner's own flush, so the log is tailable even if
# someone runs this without the package's unbuffered print.
nohup python3 -u -m bookbound run --interval "$INTERVAL" "$@" \
  >> state/runner.log 2>&1 &
echo $! > state/runner.pid
sleep 2
echo "started PID $(cat state/runner.pid), interval ${INTERVAL}s"
echo "tail -f state/runner.log"
tail -n 8 state/runner.log || true
