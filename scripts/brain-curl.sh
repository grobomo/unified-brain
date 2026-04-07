#!/usr/bin/env bash
# brain-curl.sh — quick helper to interact with the running brain service
#
# Usage:
#   ./scripts/brain-curl.sh health        # check health/heartbeat
#   ./scripts/brain-curl.sh metrics       # Prometheus metrics
#   ./scripts/brain-curl.sh stats         # webhook queue stats
#   ./scripts/brain-curl.sh send <json>   # send event to webhook endpoint
#   ./scripts/brain-curl.sh send-file <f> # send event from JSON file

HEALTH_PORT="${BRAIN_HEALTH_PORT:-8790}"
WEBHOOK_PORT="${BRAIN_WEBHOOK_PORT:-8791}"
HOST="${BRAIN_HOST:-127.0.0.1}"

case "${1:-health}" in
  health|status)
    curl -s "http://${HOST}:${HEALTH_PORT}/healthz" | python -m json.tool 2>/dev/null || curl -s "http://${HOST}:${HEALTH_PORT}/healthz"
    ;;
  metrics)
    curl -s "http://${HOST}:${HEALTH_PORT}/metrics"
    ;;
  stats)
    curl -s "http://${HOST}:${WEBHOOK_PORT}/events/stats" | python -m json.tool 2>/dev/null || curl -s "http://${HOST}:${WEBHOOK_PORT}/events/stats"
    ;;
  send)
    shift
    if [ -z "$1" ]; then
      echo "Usage: brain-curl.sh send '{\"source\":\"test\",\"title\":\"hello\"}'"
      exit 1
    fi
    curl -s -X POST "http://${HOST}:${WEBHOOK_PORT}/events" \
      -H "Content-Type: application/json" \
      -d "$1" | python -m json.tool 2>/dev/null
    ;;
  send-file)
    shift
    if [ -z "$1" ] || [ ! -f "$1" ]; then
      echo "Usage: brain-curl.sh send-file event.json"
      exit 1
    fi
    curl -s -X POST "http://${HOST}:${WEBHOOK_PORT}/events" \
      -H "Content-Type: application/json" \
      -d @"$1" | python -m json.tool 2>/dev/null
    ;;
  *)
    echo "Usage: brain-curl.sh {health|metrics|stats|send <json>|send-file <file>}"
    exit 1
    ;;
esac
