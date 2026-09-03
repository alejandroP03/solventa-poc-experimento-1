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

def _single_process() -> bool:
    """¿Este servicio alberga estado singleton?

    Lo declara el Dockerfile con `SOLVENTA_SINGLE_PROCESS=true`, y el nombre
    importa: `.env` **no** define esta variable, así que el `env_file:` de
    compose no puede pisarla como sí pisa a `GUNICORN_WORKERS`. Ese fue un fallo
    real durante la construcción — el mock arrancó con dos workers, cada uno con
    su propio estado de fallo, y la mitad del tráfico veía al proveedor sano.

    Aplica a los servicios cuyo estado en memoria ES la táctica bajo prueba: el
    circuit breaker y la señal del Monitor en financial-profiler, el estado del
    fixture en mock-openfinance, el lazo periódico del monitor y los consumidores
    AMQP. Ver OBSERVACIONES.md, OBS-05.
    """
    return os.environ.get("SOLVENTA_SINGLE_PROCESS", "").lower() in {"1", "true", "yes"}


bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1 if _single_process() else int(os.environ.get("GUNICORN_WORKERS", "2"))
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
