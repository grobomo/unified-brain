#!/usr/bin/env bash
# Stop the unified-brain service.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PID_FILE="data/brain.pid"
LOCK_FILE="data/brain.lock"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping brain (PID $PID)..."
        kill "$PID"
        # Wait up to 10s for graceful shutdown
        for i in $(seq 1 10); do
            if ! kill -0 "$PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "Force killing..."
            kill -9 "$PID"
        fi
        echo "Stopped"
    else
        echo "Process $PID not running"
    fi
    rm -f "$PID_FILE"
fi
rm -f "$LOCK_FILE"
