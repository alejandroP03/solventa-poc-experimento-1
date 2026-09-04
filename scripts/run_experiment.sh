#!/usr/bin/env bash
set -euo pipefail

SP="${1:-}"
FULL="${2:-}"

if [[ -z "$SP" || ! "$SP" =~ ^SP-[0-5]$ ]]; then
  echo "Uso: scripts/run_experiment.sh SP-0|SP-1|...|SP-5 [--full]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker no esta disponible en PATH" >&2
  exit 1
fi

ENV_BACKUP="$(mktemp)"
ENV_HAD_FILE=0
if [[ -f .env ]]; then
  cp .env "$ENV_BACKUP"
  ENV_HAD_FILE=1
fi

cleanup() {
  ./scripts/inject_fault.sh normal >/dev/null 2>&1 || true
  if [[ "$ENV_HAD_FILE" -eq 1 ]]; then
    cp "$ENV_BACKUP" .env
  else
    rm -f .env
  fi
  rm -f "$ENV_BACKUP"
}
trap cleanup EXIT

timestamp() {
  python3 - <<'PY'
import time
print(f"{time.time():.3f}")
PY
}

iso_now() {
  python3 - <<'PY'
import datetime as dt
print(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
PY
}

docker_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf "%s" "$1"
  fi
}

write_env() {
  cp .env.example .env
  {
    echo
    echo "# Overrides de la corrida $RUN_ID"
    echo "QUOTE_MODE=$QUOTE_MODE"
    for kv in "${OVERRIDES[@]}"; do
      echo "$kv"
    done
  } >> .env
}

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key {
      value = $0
      sub(/^[^=]*=/, "", value)
      sub(/[[:space:]]*#.*/, "", value)
      gsub(/^[ \t]+|[ \t]+$/, "", value)
      found = value
    }
    END { print found }
  ' .env
}

wait_http() {
  local name="$1"
  local url="$2"
  local max="${3:-90}"
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

reload_gateway() {
  docker compose restart kong >/dev/null 2>&1 || true
  wait_http "Kong" "http://localhost:8100/status" 90
}

case_matrix() {
  case "$SP" in
    SP-0)
      echo "treatment_error_5xx|treatment|error_5xx|1500|0.5|BREAKER_ENABLED=true CACHE_ENABLED=true BULKHEAD_ENABLED=true MONITOR_SIGNAL_ENABLED=true"
      echo "baseline_error_5xx|baseline|error_5xx|1500|0.5|"
      ;;
    SP-1)
      echo "timeout_400|treatment|slow|800|0.5|OPENFINANCE_TIMEOUT_MS=400"
      echo "timeout_700|treatment|slow|800|0.5|OPENFINANCE_TIMEOUT_MS=700"
      echo "timeout_1000|treatment|slow|800|0.5|OPENFINANCE_TIMEOUT_MS=1000"
      ;;
    SP-2)
      echo "failmax_3|treatment|error_5xx|1500|0.5|BREAKER_FAIL_MAX=3 BREAKER_RESET_TIMEOUT_S=30"
      echo "failmax_5|treatment|error_5xx|1500|0.5|BREAKER_FAIL_MAX=5 BREAKER_RESET_TIMEOUT_S=30"
      echo "failmax_10|treatment|error_5xx|1500|0.5|BREAKER_FAIL_MAX=10 BREAKER_RESET_TIMEOUT_S=30"
      if [[ "$FULL" == "--full" ]]; then
        echo "reset_10|treatment|error_5xx|1500|0.5|BREAKER_FAIL_MAX=5 BREAKER_RESET_TIMEOUT_S=10"
        echo "reset_60|treatment|error_5xx|1500|0.5|BREAKER_FAIL_MAX=5 BREAKER_RESET_TIMEOUT_S=60"
        echo "flaky_5|treatment|flaky|1500|0.5|BREAKER_FAIL_MAX=5 BREAKER_RESET_TIMEOUT_S=30"
      fi
      ;;
    SP-3)
      echo "preload_0|treatment|error_5xx|1500|0.5|CACHE_PRELOAD_RATIO=0.0 PROFILE_CACHE_TTL_S=300 PROFILE_CACHE_STALE_GRACE_S=1800"
      echo "preload_50|treatment|error_5xx|1500|0.5|CACHE_PRELOAD_RATIO=0.5 PROFILE_CACHE_TTL_S=300 PROFILE_CACHE_STALE_GRACE_S=1800"
      echo "preload_100|treatment|error_5xx|1500|0.5|CACHE_PRELOAD_RATIO=1.0 PROFILE_CACHE_TTL_S=300 PROFILE_CACHE_STALE_GRACE_S=1800"
      if [[ "$FULL" == "--full" ]]; then
        echo "ttl_60|treatment|error_5xx|1500|0.5|CACHE_PRELOAD_RATIO=0.5 PROFILE_CACHE_TTL_S=60 PROFILE_CACHE_STALE_GRACE_S=600"
        echo "ttl_900|treatment|error_5xx|1500|0.5|CACHE_PRELOAD_RATIO=0.5 PROFILE_CACHE_TTL_S=900 PROFILE_CACHE_STALE_GRACE_S=1800"
      fi
      ;;
    SP-4)
      echo "monitor_on_slow|treatment|slow|1500|0.5|MONITOR_SIGNAL_ENABLED=true"
      echo "monitor_off_slow|treatment|slow|1500|0.5|MONITOR_SIGNAL_ENABLED=false"
      if [[ "$FULL" == "--full" ]]; then
        echo "monitor_on_flaky|treatment|flaky|1500|0.5|MONITOR_SIGNAL_ENABLED=true"
        echo "monitor_off_flaky|treatment|flaky|1500|0.5|MONITOR_SIGNAL_ENABLED=false"
      fi
      ;;
    SP-5)
      echo "bulkhead_on_timeout|treatment|timeout|1500|0.5|BULKHEAD_ENABLED=true K6_RPS=50"
      echo "bulkhead_off_timeout|treatment|timeout|1500|0.5|BULKHEAD_ENABLED=false K6_RPS=50"
      if [[ "$FULL" == "--full" ]]; then
        echo "pending_8|treatment|timeout|1500|0.5|BULKHEAD_ENABLED=true POOL_PENDING_REPLIES_MAX=8"
        echo "pending_32|treatment|timeout|1500|0.5|BULKHEAD_ENABLED=true POOL_PENDING_REPLIES_MAX=32"
      fi
      ;;
  esac
}

mkdir -p results
python3 scripts/seed_data.py >/dev/null

while IFS="|" read -r CASE_ID QUOTE_MODE FAULT_MODE FAULT_MS FAULT_RATE PARAMS; do
  [[ -z "$CASE_ID" ]] && continue

  RUN_ID="$(echo "${SP}_${CASE_ID}_$(date +%Y%m%d_%H%M%S)" | tr '[:upper:]' '[:lower:]')"
  RUN_DIR="results/$RUN_ID"
  mkdir -p "$RUN_DIR"

  OVERRIDES=()
  if [[ -n "$PARAMS" ]]; then
    for kv in $PARAMS; do
      OVERRIDES+=("$kv")
    done
  fi

  echo "==> $RUN_ID"
  write_env

  COMPOSE_ARGS=(-f docker-compose.yml)
  if [[ "$QUOTE_MODE" == "baseline" ]]; then
    COMPOSE_ARGS+=(-f docker-compose.baseline.yml)
  fi

  docker compose "${COMPOSE_ARGS[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  docker compose "${COMPOSE_ARGS[@]}" up -d --build
  reload_gateway
  wait_http "Prometheus" "http://localhost:9090/-/ready" 90
  wait_http "mock-openfinance" "http://localhost:8090/health" 90

  ratio="$(env_value CACHE_PRELOAD_RATIO)"
  python3 scripts/seed_data.py --preload --ratio "${ratio:-0.5}" > "$RUN_DIR/seed.json"
  ./scripts/inject_fault.sh normal >/dev/null

  STARTED_AT="$(timestamp)"
  K6_RPS_VALUE="$(env_value K6_RPS)"
  K6_DURATION_VALUE="$(env_value K6_DURATION)"

  load_mount="$(docker_path "$ROOT/load/k6")"
  results_mount="$(docker_path "$ROOT/$RUN_DIR")"
  set +e
  docker run --rm --network solventa_default \
    -v "$load_mount:/scripts:ro" \
    -v "$results_mount:/results" \
    -e BASE_URL=http://kong:8000 \
    -e LOAD_RPS="${K6_RPS_VALUE:-50}" \
    -e LOAD_DURATION="${K6_DURATION_VALUE:-240s}" \
    grafana/k6:0.54.0 run /scripts/quote_load.js \
      --summary-export /results/k6_summary.json > "$RUN_DIR/k6.log" 2>&1 &
  K6_PID=$!
  set -e

  sleep 60
  FAULT_INJECTED_AT="$(timestamp)"
  ./scripts/inject_fault.sh "$FAULT_MODE" "$FAULT_MS" "$FAULT_RATE" > "$RUN_DIR/fault_injected.json"

  if [[ "$SP" == "SP-3" && "$CASE_ID" == "preload_0" ]]; then
    python3 scripts/seed_data.py --purge-only > "$RUN_DIR/cache_purge_at_fault.json"
  fi

  sleep 120
  FAULT_RECOVERED_AT="$(timestamp)"
  ./scripts/inject_fault.sh normal > "$RUN_DIR/fault_recovered.json"

  set +e
  wait "$K6_PID"
  K6_EXIT=$?
  set -e
  ENDED_AT="$(timestamp)"

  python3 - "$RUN_DIR/metadata.json" <<PY
import json
import sys
metadata = {
  "run_id": "$RUN_ID",
  "sp": "$SP",
  "case_id": "$CASE_ID",
  "quote_mode": "$QUOTE_MODE",
  "created_at": "$(iso_now)",
  "params": dict(kv.split("=", 1) for kv in """${PARAMS:-}""".split() if "=" in kv),
  "fault": {"mode": "$FAULT_MODE", "latency_ms": int("$FAULT_MS"), "failure_rate": float("$FAULT_RATE")},
  "timestamps": {
    "started_at": float("$STARTED_AT"),
    "fault_injected_at": float("$FAULT_INJECTED_AT"),
    "fault_recovered_at": float("$FAULT_RECOVERED_AT"),
    "ended_at": float("$ENDED_AT")
  },
  "k6_exit_code": $K6_EXIT
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(metadata, fh, indent=2)
    fh.write("\\n")
PY

  if [[ "$QUOTE_MODE" != "baseline" && "$K6_EXIT" -ne 0 ]]; then
    echo "k6 fallo en $RUN_ID; se recolectan resultados de todas formas" >&2
  elif [[ "$QUOTE_MODE" == "baseline" && "$K6_EXIT" -ne 0 ]]; then
    echo "k6 fallo en baseline como resultado esperado"
  fi

  python3 scripts/collect_results.py --run-id "$RUN_ID"
done < <(case_matrix)

echo "Listo: resultados en results/"
