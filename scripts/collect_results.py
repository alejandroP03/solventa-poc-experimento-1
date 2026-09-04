#!/usr/bin/env python3
"""Recolecta metricas de Prometheus y arma resumenes del experimento."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.split("#", 1)[0].strip().strip('"')
    return env


def prom_query(base_url: str, query: str, at: float | None = None) -> list[dict[str, Any]]:
    params = {"query": query}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(payload)
    return payload["data"]["result"]


def scalar(results: list[dict[str, Any]]) -> float | None:
    if not results:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def labels(metric: dict[str, Any]) -> str:
    data = metric.get("metric") or {}
    return ",".join(f"{key}={value}" for key, value in sorted(data.items()))


def write_metrics_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=("query_name", "labels", "value"))
        writer.writeheader()
        writer.writerows(rows)


def read_k6_summary(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "k6_summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def k6_metric(summary: dict[str, Any] | None, name: str, field: str) -> float | None:
    if not summary:
        return None
    value = summary.get("metrics", {}).get(name, {}).get(field)
    return float(value) if isinstance(value, (int, float)) else None


def fmt(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/d"
    return f"{value:.4g}{suffix}"


def recommendation(metadata: dict[str, Any], values: dict[str, float | None]) -> str:
    sp = metadata.get("sp", "")
    params = metadata.get("params", {})
    gateway_5xx = values.get("gateway_5xx_fault_max")
    p95 = values.get("gateway_latency_p95_avg")
    provider_p95 = values.get("provider_latency_p95_avg")

    if sp == "SP-0":
        if metadata.get("quote_mode") == "baseline" and gateway_5xx and gateway_5xx > 0:
            return "Confirma premisa: baseline propaga 5xx"
        if gateway_5xx == 0:
            return "Treatment contiene el fallo observado"
    if sp == "SP-1":
        timeout = params.get("OPENFINANCE_TIMEOUT_MS")
        return f"Contrastar precision vs latencia para timeout={timeout}ms"
    if sp == "SP-2":
        return "Preferir menor deteccion sin flapping visible"
    if sp == "SP-3":
        return "Evaluar cobertura DEFAULT/DEGRADED contra antiguedad de cache"
    if sp == "SP-4":
        return "Comparar monitor_signal contra breaker_count en trafico real"
    if sp == "SP-5" and provider_p95 is not None:
        return "Bulkhead aceptable si provider p95 permanece cerca de linea sana"
    if p95 is not None and p95 <= 0.25 and (gateway_5xx or 0) == 0:
        return "Cumple presupuesto y ASR en esta corrida"
    return "Revisar series y logs de la corrida"


def append_matrix(metadata: dict[str, Any], values: dict[str, float | None]) -> None:
    sp = str(metadata.get("sp", "SP-X")).lower().replace("-", "_")
    matrix = ROOT / "results" / f"{sp}_matrix.md"
    matrix.parent.mkdir(parents=True, exist_ok=True)
    if not matrix.exists():
        matrix.write_text(
            "| run_id | case | params | fault | max 5xx fallo | p95 gateway | p95 provider | recomendacion |\n"
            "|---|---|---|---|---:|---:|---:|---|\n",
            encoding="utf-8",
        )

    params = ", ".join(f"{k}={v}" for k, v in (metadata.get("params") or {}).items()) or "-"
    fault = metadata.get("fault", {}).get("mode", "-")
    row = (
        f"| {metadata.get('run_id')} | {metadata.get('case_id')} | {params} | {fault} | "
        f"{fmt(values.get('gateway_5xx_fault_max'))} | "
        f"{fmt(values.get('gateway_latency_p95_avg'), 's')} | "
        f"{fmt(values.get('provider_latency_p95_avg'), 's')} | "
        f"{recommendation(metadata, values)} |\n"
    )
    with matrix.open("a", encoding="utf-8") as fh:
        fh.write(row)


def collect(metadata: dict[str, Any], run_dir: Path, prom_url: str) -> tuple[list[dict[str, Any]], dict[str, float | None], str | None]:
    started = float(metadata["timestamps"]["started_at"])
    ended = float(metadata["timestamps"].get("ended_at") or time.time())
    injected = float(metadata["timestamps"].get("fault_injected_at") or started)
    recovered = float(metadata["timestamps"].get("fault_recovered_at") or ended)
    total_window = max(1, int(ended - started))
    fault_window = max(1, int(recovered - injected))

    queries = {
        "gateway_5xx_fault_max": f"max_over_time(solventa:gateway_5xx_rate:ratio[{fault_window}s])",
        "gateway_latency_p95_avg": f"avg_over_time(solventa:gateway_latency_p95:seconds[{total_window}s])",
        "provider_latency_p95_avg": f"avg_over_time(solventa:provider_route_latency_p95:seconds[{total_window}s])",
        "quotes_by_quality": f"increase(solventa_quotes_total[{total_window}s])",
        "openfinance_calls_by_outcome": f"increase(solventa_openfinance_calls_total[{total_window}s])",
        "cache_operations": f"increase(solventa_profile_cache_operations_total[{total_window}s])",
        "breaker_transitions": f"increase(solventa_circuit_breaker_transitions_total[{total_window}s])",
        "detection_sources": f"increase(solventa_detection_source_total[{total_window}s])",
        "journey_stage_p95": (
            "histogram_quantile(0.95, "
            f"sum by (le, stage) (rate(solventa_journey_stage_duration_seconds_bucket[{total_window}s])))"
        ),
    }

    rows: list[dict[str, Any]] = []
    values: dict[str, float | None] = {}
    try:
        for name, query in queries.items():
            at = recovered if name == "gateway_5xx_fault_max" else ended
            result = prom_query(prom_url, query, at=at)
            values[name] = scalar(result)
            for item in result:
                rows.append(
                    {
                        "query_name": name,
                        "labels": labels(item),
                        "value": item.get("value", ["", ""])[1],
                    }
                )
        return rows, values, None
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        return rows, values, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--prometheus-url")
    args = parser.parse_args()

    run_dir = ROOT / "results" / args.run_id
    metadata_path = run_dir / "metadata.json"
    if not metadata_path.exists():
        print(f"No existe {metadata_path}", file=sys.stderr)
        return 2

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    env = load_env(ROOT / ".env")
    prom_url = args.prometheus_url or os.getenv("PROMETHEUS_URL") or env.get("PROMETHEUS_URL") or "http://localhost:9090"
    prom_url = prom_url.replace("http://prometheus:9090", "http://localhost:9090")

    rows, values, error = collect(metadata, run_dir, prom_url)
    write_metrics_csv(run_dir / "metrics.csv", rows)

    k6 = read_k6_summary(run_dir)
    values["k6_http_req_failed_rate"] = k6_metric(k6, "http_req_failed", "rate")
    values["k6_http_req_duration_p95_ms"] = k6_metric(k6, "http_req_duration", "p(95)")

    summary = [
        f"# {metadata.get('run_id')}",
        "",
        f"- SP: {metadata.get('sp')}",
        f"- Caso: {metadata.get('case_id')}",
        f"- Modo: {metadata.get('quote_mode')}",
        f"- Fallo: {metadata.get('fault', {}).get('mode')}",
        f"- Max 5xx durante fallo: {fmt(values.get('gateway_5xx_fault_max'))}",
        f"- p95 gateway promedio: {fmt(values.get('gateway_latency_p95_avg'), 's')}",
        f"- p95 provider promedio: {fmt(values.get('provider_latency_p95_avg'), 's')}",
        f"- k6 http_req_failed: {fmt(values.get('k6_http_req_failed_rate'))}",
        f"- k6 p95 global: {fmt(values.get('k6_http_req_duration_p95_ms'), 'ms')}",
        f"- Recomendacion: {recommendation(metadata, values)}",
    ]
    if error:
        summary.extend(["", f"> Prometheus no estuvo disponible o no respondio: `{error}`."])

    (run_dir / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    append_matrix(metadata, values)
    print(run_dir / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
