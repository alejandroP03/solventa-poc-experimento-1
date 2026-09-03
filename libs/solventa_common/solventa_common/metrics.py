"""Registro Prometheus compartido — el entregable real del POC (kickoff §6).

Cada métrica declarada aquí existe porque algún punto de sensibilidad la necesita
para decidir. No se agregan métricas "por si acaso": la proliferación de series sin
propósito experimental es ruido.

MODO MULTIPROCESO — punto crítico
---------------------------------
Los servicios corren con `GUNICORN_WORKERS=2`, y Prometheus scrapea un único
puerto. Sin `prometheus_client.multiprocess`, cada scrape devolvería los
contadores del proceso que casualmente atendió esa petición: **la mitad de los
datos del experimento se perdería en silencio**, y las tasas quedarían
subestimadas de forma no determinista. Peor aún, el error no se nota — las series
existen y las gráficas se pintan.

La solución es `PROMETHEUS_MULTIPROC_DIR` sobre un tmpfs interno del contenedor
(no es un volumen de estado de aplicación, así que respeta §8.1) y un
`multiprocess_mode` explícito en cada Gauge, porque el default de un Gauge en
multiproceso ('all') expone una serie por PID en lugar de un valor agregado.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"

# --------------------------------------------------------------------------- #
# Buckets                                                                      #
# --------------------------------------------------------------------------- #

# Fijados por §6. La línea en 0.25 es el presupuesto del journey
# (JOURNEY_LATENCY_BUDGET_MS) y la cola hasta 30 s cubre el timeout del baseline.
HTTP_BUCKETS = (
    0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.7, 1, 1.5, 2, 3, 5, 10, 30,
)

# Resolución fina alrededor de los tres valores contrastados de SP-1
# (400 / 700 / 1000 ms): sin bordes cerca de esos puntos, la pérdida de perfiles
# FRESH por corte prematuro no sería distinguible entre configuraciones.
OPENFINANCE_BUCKETS = (
    0.02, 0.05, 0.08, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2, 5, 30,
)

# Las etapas del journey se reparten un presupuesto de 250 ms, así que hay que
# poder resolver por debajo del milisegundo: el sobrecosto del broker (§7.1) puede
# ser una fracción pequeña y aun así decidir si el diseño cabe.
STAGE_BUCKETS = (
    0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25,
    0.3, 0.5, 0.75, 1, 2, 5, 30,
)

AGE_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)

DETECTION_BUCKETS = (0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 45, 60, 120)


# --------------------------------------------------------------------------- #
# Transversales — journey                                                      #
# --------------------------------------------------------------------------- #

http_requests_total = Counter(
    "solventa_http_requests_total",
    "Peticiones HTTP atendidas. La frontera de medición del 5xx del ASR es el api-gateway.",
    ["service", "route", "status", "quote_mode"],
)

http_request_duration_seconds = Histogram(
    "solventa_http_request_duration_seconds",
    "Duración de las peticiones HTTP atendidas.",
    ["service", "route", "quote_mode"],
    buckets=HTTP_BUCKETS,
)

quotes_total = Counter(
    "solventa_quotes_total",
    "Cotizaciones entregadas, por calidad del perfil usado para tarificar.",
    ["profile_quality"],  # FRESH | DEGRADED | DEFAULT
)

# Obligatoria y no opcional (§6): es la que permite la descomposición del
# presupuesto de latencia de §7.1, con el sobrecosto del broker como fila propia.
journey_stage_duration_seconds = Histogram(
    "solventa_journey_stage_duration_seconds",
    "Duración por etapa del journey de cotización.",
    ["stage"],
    buckets=STAGE_BUCKETS,
)

# Las etapas cuya suma reconstruye el journey. `broker_*` y `queue_wait` son las
# tres que componen el sobrecosto atribuible al broker en §7.1.
STAGES = (
    "gateway",
    "provider_call",
    "broker_publish",
    "queue_wait",
    "processor_handling",
    "profiler_call",
    "broker_reply",
    "compose",
)
BROKER_STAGES = ("broker_publish", "queue_wait", "broker_reply")


# --------------------------------------------------------------------------- #
# SP-1 — timeout hacia Open Finance                                            #
# --------------------------------------------------------------------------- #

openfinance_duration_seconds = Histogram(
    "solventa_openfinance_duration_seconds",
    "Duración de las llamadas salientes a Open Finance, incluidas las que agotan el timeout.",
    buckets=OPENFINANCE_BUCKETS,
)

openfinance_calls_total = Counter(
    "solventa_openfinance_calls_total",
    "Llamadas a Open Finance por desenlace.",
    ["outcome"],  # success | timeout | error | rejected_open
)

openfinance_timeout_exhausted_total = Counter(
    "solventa_openfinance_timeout_exhausted_total",
    "Peticiones que consumieron el timeout completo antes de rendirse. "
    "Es el costo en latencia que SP-1 busca acotar.",
)


# --------------------------------------------------------------------------- #
# SP-2 — circuit breaker                                                       #
# --------------------------------------------------------------------------- #

circuit_breaker_state = Gauge(
    "solventa_circuit_breaker_state",
    "Estado del circuito: 0=closed 1=open 2=half_open.",
    ["dependency"],
    multiprocess_mode="max",  # abierto en cualquier worker es la señal relevante
)

circuit_breaker_transitions_total = Counter(
    "solventa_circuit_breaker_transitions_total",
    "Transiciones de estado del circuito. Su conteo bajo `flaky` mide el flapping.",
    ["from_state", "to_state"],
)

circuit_breaker_calls_total = Counter(
    "solventa_circuit_breaker_calls_total",
    "Llamadas vistas por el breaker.",
    ["outcome"],  # success | failure | rejected
)


# --------------------------------------------------------------------------- #
# SP-3 — caché de perfil                                                       #
# --------------------------------------------------------------------------- #

profile_cache_operations_total = Counter(
    "solventa_profile_cache_operations_total",
    "Operaciones contra la caché de perfil.",
    ["result"],  # hit_fresh | hit_stale | miss | write
)

profile_cache_age_seconds = Histogram(
    "solventa_profile_cache_age_seconds",
    "Antigüedad del dato servido desde caché. Es la evidencia de la implicación "
    "de pricing y auditoría de un TTL largo.",
    buckets=AGE_BUCKETS,
)


# --------------------------------------------------------------------------- #
# SP-4 — fuente de verdad de la detección                                      #
# --------------------------------------------------------------------------- #

monitor_dependency_up = Gauge(
    "solventa_monitor_dependency_up",
    "Veredicto del Ping-Echo del Monitor: 1=sano 0=caído. En los modos slow y flaky "
    "vale 1 mientras el tráfico real falla — ese desacuerdo es el hallazgo de SP-4.",
    ["dependency"],
    multiprocess_mode="livemostrecent",
)

detection_source_total = Counter(
    "solventa_detection_source_total",
    "Aperturas del circuito atribuidas a cada fuente de detección.",
    ["source"],  # monitor_signal | breaker_count
)

detection_latency_seconds = Histogram(
    "solventa_detection_latency_seconds",
    "Latencia de detección medida in-process: primer fallo observado -> corte efectivo. "
    "Es una cota inferior; la latencia desde la inyección real la calcula "
    "collect_results.py con los timestamps de metadata.json (ver OBSERVACIONES OBS-04).",
    ["source"],
    buckets=DETECTION_BUCKETS,
)

health_signal_received_total = Counter(
    "solventa_health_signal_received_total",
    "Señales de salud recibidas del Monitor en el financial-profiler.",
    ["state"],  # down | up
)


# --------------------------------------------------------------------------- #
# SP-5 — aislamiento de recursos                                               #
# --------------------------------------------------------------------------- #

pool_inflight = Gauge(
    "solventa_pool_inflight",
    "Ocupación actual de cada pool acotado (bulkhead).",
    ["pool"],  # provider | openfinance | pending_replies
    multiprocess_mode="livesum",
)

pool_rejected_total = Counter(
    "solventa_pool_rejected_total",
    "Trabajo rechazado por pool lleno. Un rechazo nunca produce 5xx: se degrada "
    "el profile_quality (invariante duro de §3.1).",
    ["pool"],
)

pool_wait_seconds = Histogram(
    "solventa_pool_wait_seconds",
    "Espera para obtener un slot del pool.",
    ["pool"],
    buckets=STAGE_BUCKETS,
)

gunicorn_busy_workers = Gauge(
    "solventa_gunicorn_busy_workers",
    "Hilos de Gunicorn atendiendo una petición, agregados entre workers. "
    "Su saturación es lo que propaga el daño de una ruta a otra.",
    ["service"],
    multiprocess_mode="livesum",
)


# --------------------------------------------------------------------------- #
# Broker y reconfiguración                                                     #
# --------------------------------------------------------------------------- #

queue_published_total = Counter(
    "solventa_queue_published_total", "Mensajes publicados.", ["queue"]
)

queue_consumed_total = Counter(
    "solventa_queue_consumed_total",
    "Mensajes consumidos, por rol del procesador. Evidencia el takeover PRIMARY->BACKUP.",
    ["queue", "processor_role"],
)

queue_depth = Gauge(
    "solventa_queue_depth",
    "Profundidad de la cola, leída del API de management de RabbitMQ.",
    ["queue"],
    multiprocess_mode="livemostrecent",
)

queue_wait_seconds = Histogram(
    "solventa_queue_wait_seconds",
    "Tiempo que un mensaje pasa encolado antes de ser consumido.",
    buckets=STAGE_BUCKETS,
)

reply_timeout_total = Counter(
    "solventa_reply_timeout_total",
    "Esperas de réplica que vencieron REPLY_TIMEOUT_MS. La cotización se entrega "
    "igualmente con 200 y profile_quality=DEFAULT, nunca con 5xx.",
)

orphan_reply_total = Counter(
    "solventa_orphan_reply_total",
    "Réplicas llegadas sin espera activa (vencidas o reprocesadas tras re-encolado). "
    "Se descartan en silencio: la deduplicación es un efecto del patrón, no una "
    "responsabilidad implementada (§3.5).",
)

processor_active = Gauge(
    "solventa_processor_active",
    "1 si ese rol es el consumidor activo de cotizacion.requests.",
    ["role"],
    multiprocess_mode="livemostrecent",
)

takeover_events_total = Counter(
    "solventa_takeover_events_total",
    "Promociones PRIMARY->BACKUP observadas por el health-manager.",
)


# --------------------------------------------------------------------------- #
# Utilidades                                                                   #
# --------------------------------------------------------------------------- #


@contextmanager
def observe_stage(stage: str) -> Iterator[None]:
    """Cronometra una etapa del journey (§7.1).

    Se mide siempre, también cuando la etapa falla: una etapa que agota su timeout
    consume presupuesto igual que una exitosa, y omitirla sesgaría la
    descomposición justo en la ventana de indisponibilidad, que es donde importa.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        journey_stage_duration_seconds.labels(stage=stage).observe(
            time.perf_counter() - started
        )


def observe_stage_value(stage: str, seconds: float) -> None:
    """Registra una etapa ya cronometrada.

    Necesario para `queue_wait` y `broker_reply`, que se calculan restando
    timestamps estampados en otro servicio. Todos los contenedores comparten el
    reloj del host Docker, así que la resta entre procesos es válida; en un
    despliegue multi-host habría que revisar el desfase de relojes.
    """
    if seconds >= 0:
        journey_stage_duration_seconds.labels(stage=stage).observe(seconds)


def is_multiprocess() -> bool:
    return bool(os.environ.get(MULTIPROC_ENV))


def reset_multiproc_dir() -> None:
    """Limpia el directorio de métricas al arrancar el máster de Gunicorn.

    Sin esto, los ficheros .db de una corrida anterior sobreviven al reinicio del
    contenedor y sus contadores se suman a los de la corrida nueva, mezclando dos
    experimentos en una misma serie.
    """
    path = os.environ.get(MULTIPROC_ENV)
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def mark_process_dead(pid: int) -> None:
    """Consolida las métricas de un worker que muere (hook de Gunicorn)."""
    if is_multiprocess():
        multiprocess.mark_process_dead(pid)


def build_registry() -> CollectorRegistry:
    """Registro a usar en cada scrape.

    En multiproceso hay que construirlo en cada petición: el colector recorre los
    ficheros .db de todos los workers vivos, y ese conjunto cambia con el tiempo.
    """
    if not is_multiprocess():
        return REGISTRY
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def render() -> tuple[bytes, str]:
    """Cuerpo y content-type de la respuesta de /metrics."""
    return generate_latest(build_registry()), CONTENT_TYPE_LATEST
