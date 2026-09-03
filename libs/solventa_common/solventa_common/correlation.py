"""Propagación del X-Correlation-Id a través del journey.

Alcance del correlation_id (kickoff §3.5) — **dos funciones y ninguna más**:

1. Mecanismo del patrón request-reply: casar cada ProfileResponse con la espera
   activa que la aguarda en `cotizacion`.
2. Trazabilidad diagnóstica: aparecer en cada línea de log de cada servicio para
   poder reconstruir por qué una cotización concreta salió DEGRADED durante la
   ventana de fallo. Es evidencia para el informe.

**No** es clave de idempotencia ni de deduplicación, y no participa en ninguna
decisión de negocio (§2.3). La deduplicación es un efecto del patrón request-reply
—el registro de correlación ya no tiene la espera activa cuando llega la réplica
tardía—, no una responsabilidad implementada aquí.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, Mapping, MutableMapping

from flask import Flask, g, request

HEADER = "X-Correlation-Id"
PARTNER_HEADER = "X-Partner-Id"

# ContextVar y no threading.local: Gunicorn corre con worker class gthread, y un
# ContextVar se comporta correctamente tanto entre hilos como dentro del hilo
# dedicado que consume las réplicas AMQP en `cotizacion`.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
_partner_id: ContextVar[str] = ContextVar("partner_id", default="-")


def new_correlation_id() -> str:
    """UUID4 nuevo. Solo el api-gateway debería llamar a esto (§3.5)."""
    return str(uuid.uuid4())


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> str:
    resolved = value or new_correlation_id()
    _correlation_id.set(resolved)
    return resolved


def get_partner_id() -> str:
    return _partner_id.get()


def set_partner_id(value: str | None) -> str:
    resolved = value or "-"
    _partner_id.set(resolved)
    return resolved


def outbound_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Cabeceras para propagar el contexto en una llamada HTTP saliente."""
    headers = {HEADER: get_correlation_id(), PARTNER_HEADER: get_partner_id()}
    if extra:
        headers.update(extra)
    return headers


def bind_from_amqp(properties: Any, body: Mapping[str, Any] | None = None) -> str:
    """Reconstruye el contexto al consumir un mensaje AMQP.

    El correlation_id viaja en la propiedad AMQP `correlation_id` (que es lo que
    hace funcionar el request-reply); el partner_id, que es solo diagnóstico,
    viaja en el cuerpo.
    """
    correlation_id = getattr(properties, "correlation_id", None)
    set_correlation_id(correlation_id)
    if body:
        set_partner_id(body.get("partner_id"))
    return get_correlation_id()


def install(app: Flask, *, generate_if_missing: bool = False) -> None:
    """Middleware Flask que abre y cierra el contexto de cada petición.

    `generate_if_missing=True` solo en el api-gateway: es el único punto donde el
    correlation_id nace (§3.5). En el resto de servicios, una petición sin la
    cabecera indica que alguien saltó el gateway, y conviene que el id sintético
    se note en los logs en lugar de disimularse.
    """

    @app.before_request
    def _bind_context() -> None:  # pragma: no cover - integración con Flask
        incoming = request.headers.get(HEADER)
        if incoming or generate_if_missing:
            correlation_id = set_correlation_id(incoming)
        else:
            correlation_id = set_correlation_id(f"orphan-{uuid.uuid4()}")
        set_partner_id(request.headers.get(PARTNER_HEADER))
        g.correlation_id = correlation_id

    @app.after_request
    def _echo_context(response):  # pragma: no cover - integración con Flask
        response.headers[HEADER] = get_correlation_id()
        return response


def inject_into(payload: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Añade el contexto diagnóstico al cuerpo de un mensaje saliente."""
    payload.setdefault("correlation_id", get_correlation_id())
    payload.setdefault("partner_id", get_partner_id())
    return payload
