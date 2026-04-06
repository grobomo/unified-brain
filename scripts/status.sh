#!/usr/bin/env bash
# Check unified-brain service status.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PID_FILE="data/brain.pid"
HEARTBEAT="data/heartbeat.json"

echo "=== Unified Brain Status ==="

# Process
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Process: RUNNING (PID $PID)"
    else
        echo "Process: DEAD (stale PID $PID)"
    fi
else
    echo "Process: NOT RUNNING"
fi

# Heartbeat
if [ -f "$HEARTBEAT" ]; then
    echo ""
    echo "Heartbeat:"
    cat "$HEARTBEAT"
fi

# Health endpoint
echo ""
if curl -s --connect-timeout 2 http://localhost:8790/healthz > /dev/null 2>&1; then
    echo "Health endpoint: OK"
    curl -s http://localhost:8790/healthz
else
    echo "Health endpoint: UNREACHABLE"
fi

# Database stats
if [ -f "data/brain.db" ]; then
    echo ""
    echo "Database:"
    python -c "
import sqlite3
conn = sqlite3.connect('data/brain.db')
total = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
processed = conn.execute('SELECT COUNT(*) FROM events WHERE processed=1').fetchone()[0]
print(f'  Events: {total} total, {processed} processed, {total-processed} pending')
conn.close()
" 2>/dev/null || echo "  (could not read database)"
fi
