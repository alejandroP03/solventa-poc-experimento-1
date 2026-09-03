"""Ensamblado común de los servicios Flask.

Centraliza lo que todos los servicios comparten —contexto de correlación, métricas
HTTP, health checks, /metrics y apagado limpio— para que el código de cada servicio
contenga solo su lógica de arquitectura y las tácticas que le tocan.
"""

from __future__ import annotations

import atexit
import signal
import threading
import time
from typing import Callable, Mapping

from flask import Flask, Response, g, request

from . import health, metrics
from . import logging as slog
from . import correlation
from .config import Config

_shutdown_hooks: list[Callable[[], None]] = []
_shutdown_started = threading.Event()


def on_shutdown(hook: Callable[[], None]) -> None:
    """Registra trabajo de cierre limpio (§8.2).

    Importa especialmente en `procesador-cotizacion`: un consumidor que no cierra
    su canal AMQP limpio deja que RabbitMQ espere al timeout de la conexión antes
    de promover al BACKUP, lo que alarga artificialmente la ventana de takeover
    que el experimento mide.
    """
    _shutdown_hooks.append(hook)


def _run_shutdown(*_args: object) -> None:
    if _shutdown_started.is_set():
        return
    _shutdown_started.set()
    log = slog.get_logger("shutdown")
    for hook in reversed(_shutdown_hooks):
        try:
            hook()
        except Exception:  # noqa: BLE001 - el cierre nunca debe abortar a medias
            log.exception("hook de shutdown falló", extra={"hook": getattr(hook, "__name__", "?")})


def create_app(
    cfg: Config,
    *,
    checks: Mapping[str, health.Check] | None = None,
    generate_correlation: bool = False,
) -> Flask:
    """Crea la app Flask con todo lo transversal instalado."""
    slog.configure(cfg.service_name, cfg.log_level, cfg.log_format)
    log = slog.get_logger(cfg.service_name)

    app = Flask(cfg.service_name)
    app.config["SOLVENTA"] = cfg

    correlation.install(app, generate_if_missing=generate_correlation)
    app.register_blueprint(health.build_blueprint(cfg.service_name, checks))

    _install_http_metrics(app, cfg)

    @app.get("/metrics")
    def scrape() -> Response:
        payload, content_type = metrics.render()
        return Response(payload, mimetype=content_type)

    signal.signal(signal.SIGTERM, _run_shutdown)
    atexit.register(_run_shutdown)

    log.info(
        "servicio iniciado",
        extra={
            "port": cfg.port,
            "quote_mode": cfg.quote_mode,
            "role": cfg.role or None,
            "multiprocess_metrics": metrics.is_multiprocess(),
        },
    )
    return app


def _install_http_metrics(app: Flask, cfg: Config) -> None:
    """Instrumenta toda petición atendida.

    El gauge de hilos ocupados sube y baja alrededor de cada petición: es la
    evidencia de saturación de SP-5 y la que muestra por qué el baseline colapsa
    en SP-0.
    """
    service = cfg.service_name
    quote_mode = cfg.quote_mode

    @app.before_request
    def _start_timer() -> None:
        if request.path == "/metrics":
            return
        g.solventa_started = time.perf_counter()
        metrics.gunicorn_busy_workers.labels(service=service).inc()

    # teardown y no after_request: after_request no corre si la vista lanza una
    # excepción, y el gauge se quedaría contando un hilo ocupado para siempre.
    # Una fuga aquí falsearía justo la métrica de saturación de SP-5.
    @app.teardown_request
    def _release_busy(_exc: BaseException | None) -> None:
        if getattr(g, "solventa_started", None) is None:
            return
        metrics.gunicorn_busy_workers.labels(service=service).dec()

    @app.after_request
    def _observe(response: Response) -> Response:
        started = getattr(g, "solventa_started", None)
        if started is None:
            return response

        # `url_rule` y no `request.path`: usar la ruta cruda haría de cada
        # client_id una serie distinta y volaría la cardinalidad de Prometheus
        # con un scrape cada segundo.
        route = request.url_rule.rule if request.url_rule else "unmatched"
        elapsed = time.perf_counter() - started

        metrics.http_requests_total.labels(
            service=service, route=route, status=str(response.status_code), quote_mode=quote_mode
        ).inc()
        metrics.http_request_duration_seconds.labels(
            service=service, route=route, quote_mode=quote_mode
        ).observe(elapsed)
        return response
