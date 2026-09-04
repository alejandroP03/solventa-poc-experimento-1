#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-normal}"
LATENCY_MS="${2:-1500}"
FAILURE_RATE="${3:-0.5}"
DURATION_S="${4:-}"
OPENFINANCE_ADMIN_URL="${OPENFINANCE_ADMIN_URL:-http://localhost:8090}"

if [[ "$MODE" == "normal" || "$MODE" == "reset" ]]; then
  curl -fsS -X POST "$OPENFINANCE_ADMIN_URL/admin/reset"
  echo
  exit 0
fi

payload="{\"mode\":\"$MODE\",\"latency_ms\":$LATENCY_MS,\"failure_rate\":$FAILURE_RATE"
if [[ -n "$DURATION_S" ]]; then
  payload="$payload,\"duration_s\":$DURATION_S"
fi
payload="$payload}"

curl -fsS -X POST "$OPENFINANCE_ADMIN_URL/admin/mode" \
  -H "Content-Type: application/json" \
  -d "$payload"
echo
