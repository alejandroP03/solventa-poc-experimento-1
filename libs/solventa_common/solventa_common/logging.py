"""Log estructurado JSON de una línea, solo a stdout.

Requisito de §8.2: logs únicamente a stdout, JSON de una línea, con
`correlation_id`, `service`, `level` y `timestamp`. Nunca a archivo — en ECS el
sistema de archivos del contenedor es efímero y el driver de logs recoge stdout.

El `correlation_id` se inyecta automáticamente desde el contexto (§3.5, función 2:
trazabilidad diagnóstica), de modo que ninguna llamada a `log.info(...)` tenga que
acordarse de pasarlo. Reconstruir por qué una cotización concreta salió DEGRADED
durante la ventana de fallo depende de que esté en *todas* las líneas.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

from .correlation import get_correlation_id, get_partner_id

# Atributos propios de logging.LogRecord: todo lo que no esté aquí es un campo
# extra que el llamante añadió y que debe salir en el JSON.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "correlation_id": get_correlation_id(),
            "logger": record.name,
            "message": record.getMessage(),
        }

        partner_id = get_partner_id()
        if partner_id != "-":
            payload["partner_id"] = partner_id

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Formato legible para depurar en local. `LOG_FORMAT=json` es el del experimento."""

    def __init__(self, service: str) -> None:
        super().__init__(
            fmt=f"%(asctime)s %(levelname)-5s [{service}] %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        return f"{base} corr={get_correlation_id()}"


def configure(service: str, level: str = "INFO", fmt: str = "json") -> logging.Logger:
    """Instala el handler de stdout. Idempotente: seguro si Gunicorn recarga el módulo."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else TextFormatter(service))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # El access log de Werkzeug duplicaría cada petición que ya contamos en
    # solventa_http_requests_total, y a 50 rps durante 240 s es mucho ruido en
    # la evidencia del informe.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("pika").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger(service)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
