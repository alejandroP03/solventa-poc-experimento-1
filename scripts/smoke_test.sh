#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
OPENFINANCE_ADMIN_URL="${OPENFINANCE_ADMIN_URL:-http://localhost:8090}"

wait_for() {
  local name="$1"
  local url="$2"
  local max="${3:-60}"

  printf "esperando %s" "$name"
  for _ in $(seq 1 "$max"); do
    if curl -fsS -o /dev/null "$url"; then
      printf " listo\n"
      return 0
    fi
    printf "."
    sleep 1
  done
  printf " agotado\n"
  return 1
}

wait_for "Kong" "http://localhost:8100/status" 90
wait_for "mock-openfinance" "$OPENFINANCE_ADMIN_URL/health" 90

curl -fsS -X POST "$OPENFINANCE_ADMIN_URL/admin/reset" >/dev/null

response_file="$(mktemp)"
status="$(
  curl -sS -o "$response_file" -w "%{http_code}" \
    -X POST "$BASE_URL/v1/quotes" \
    -H "Content-Type: application/json" \
    -H "X-Partner-Id: smoke" \
    -d '{"client_id":"CLI-0001","product_code":"VIAJE","insured_amount":1000000,"age":35,"city":"Bogota","partner_id":"smoke"}'
)"

cat "$response_file"
echo
rm -f "$response_file"

if [[ "$status" -ge 500 ]]; then
  echo "smoke fallido: gateway devolvio $status" >&2
  exit 1
fi

echo "smoke OK: status $status"
