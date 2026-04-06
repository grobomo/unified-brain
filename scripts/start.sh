#!/usr/bin/env bash
# Start the unified-brain service.
# Checks lock file to prevent duplicate instances.
# Usage: bash scripts/start.sh [--once] [--health-port 8790]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

LOCK_FILE="data/brain.lock"
LOG_FILE="data/brain.log"
PID_FILE="data/brain.pid"

# Check for existing instance
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Brain already running (PID $OLD_PID)"
        exit 0
    fi
    echo "Stale lock file, removing"
    rm -f "$LOCK_FILE"
fi

echo "Starting unified-brain..."
PYTHONPATH=src python -m unified_brain \
    --config config/brain.yaml \
    --health-port 8790 \
    "$@" \
    >> "$LOG_FILE" 2>&1 &

BRAIN_PID=$!
echo "$BRAIN_PID" > "$PID_FILE"
echo "Started (PID $BRAIN_PID), logs at $LOG_FILE"
