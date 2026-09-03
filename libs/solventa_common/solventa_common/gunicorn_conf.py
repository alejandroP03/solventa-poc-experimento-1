"""Configuración compartida de Gunicorn.

Todos los servicios arrancan con `gunicorn -c python:solventa_common.gunicorn_conf`.
La concurrencia se lee del entorno (§5.2) porque es una variable de control del
experimento: deliberadamente baja para que la saturación sea observable en SP-5.

Los hooks son la otra mitad del modo multiproceso de `metrics.py`: sin ellos, los
ficheros .db de una corrida anterior se sumarían a la siguiente y las métricas de
un worker muerto quedarían congeladas contando trabajo que ya no existe.
"""

from __future__ import annotations

import os

from solventa_common import metrics

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))

# Logs solo a stdout, en JSON, con el formato de solventa_common.logging (§8.2).
# El access log de Gunicorn se apaga: duplicaría cada petición que ya se cuenta en
# solventa_http_requests_total y a 50 rps durante 240 s enterraría la evidencia.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()

# Margen sobre el timeout para drenar peticiones en vuelo antes del SIGKILL (§8.2).
graceful_timeout = 20

# El master no precarga la app: cada worker debe registrar sus propias métricas en
# su fichero .db. Con preload_app los workers compartirían objetos de métrica
# creados antes del fork y el colector multiproceso los contaría mal.
preload_app = False


def on_starting(server) -> None:  # noqa: ANN001 - firma fijada por Gunicorn
    metrics.reset_multiproc_dir()
    server.log.info("solventa: directorio de métricas multiproceso reiniciado")


def child_exit(server, worker) -> None:  # noqa: ANN001 - firma fijada por Gunicorn
    metrics.mark_process_dead(worker.pid)
