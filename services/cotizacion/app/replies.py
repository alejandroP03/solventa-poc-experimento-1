"""Registro de esperas correlacionadas del patrón request-reply (§3.2).

Un diccionario en memoria `{correlation_id: espera}` protegido por lock, con purga
por TTL de las entradas vencidas. Las respuestas que llegan con un
`correlation_id` que ya no está en el registro **se descartan silenciosamente** e
incrementan `solventa_orphan_reply_total`.

Ese descarte es también la razón por la que el diseño no lleva almacén de
idempotencia (§2.3, §3.5): si un mensaje se reprocesa tras un re-encolado y llega
una segunda respuesta, la espera ya no existe y la réplica muere aquí. La
deduplicación es un **efecto** del patrón, no una responsabilidad implementada.

UNA COLA DE RESPUESTAS POR PROCESO
----------------------------------
`cotizacion` corre con `GUNICORN_WORKERS=2`, y este registro vive en memoria de
proceso. Si los dos workers compartieran una sola cola de respuestas, RabbitMQ
repartiría las réplicas entre ambos y aproximadamente **la mitad llegaría al
proceso equivocado**: allí no habría espera activa, se contarían como huérfanas y
la petición original vencería por `REPLY_TIMEOUT_MS`. El resultado sería ~50 % de
cotizaciones DEFAULT en condiciones perfectamente sanas, que se leería como un
fallo del diseño cuando sería un fallo de la implementación.

Por eso el `instance_id` incluye el PID: cada worker declara su propia cola
exclusiva. Es lo que §3.2 quiere decir con "una por instancia de cotizacion".
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from solventa_common import metrics
from solventa_common.logging import get_logger

log = get_logger("replies")


def instance_id() -> str:
    """Identidad única del proceso, no del contenedor.

    Ver la nota del encabezado: usar solo el hostname haría que los dos workers
    de Gunicorn compartieran cola y perdieran la mitad de las réplicas.
    """
    return f"{socket.gethostname()}.{os.getpid()}"


def reply_queue_name(prefix: str = "cotizacion.replies") -> str:
    return f"{prefix}.{instance_id()}"


@dataclass
class Waiter:
    correlation_id: str
    event: threading.Event = field(default_factory=threading.Event)
    payload: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.monotonic)


class ReplyRegistry:
    def __init__(self, ttl_s: float) -> None:
        # El TTL de purga excede el presupuesto de espera: una entrada solo se
        # barre cuando ya es imposible que alguien la esté esperando.
        self._ttl_s = ttl_s * 2
        self._waiters: dict[str, Waiter] = {}
        self._lock = threading.Lock()

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._waiters)

    def register(self, correlation_id: str) -> Waiter:
        waiter = Waiter(correlation_id)
        with self._lock:
            self._waiters[correlation_id] = waiter
            self._purge_expired_locked()
        return waiter

    def resolve(self, correlation_id: str, payload: dict[str, Any]) -> bool:
        """Entrega una réplica. Devuelve False si ya no había espera activa."""
        with self._lock:
            waiter = self._waiters.pop(correlation_id, None)

        if waiter is None:
            # Réplica huérfana: venció el presupuesto, o es el segundo resultado
            # de un mensaje reprocesado tras un re-encolado. Se descarta sin
            # alterar la respuesta ya entregada al socio.
            metrics.orphan_reply_total.inc()
            log.debug("réplica huérfana descartada", extra={"correlation_id": correlation_id})
            return False

        waiter.payload = payload
        waiter.event.set()
        return True

    def wait(self, waiter: Waiter, timeout_s: float) -> dict[str, Any] | None:
        """Espera la réplica. None si vence el presupuesto."""
        if waiter.event.wait(timeout_s):
            return waiter.payload

        # Se retira la espera para que la réplica tardía se cuente como huérfana
        # en lugar de quedarse residente ocupando memoria.
        with self._lock:
            self._waiters.pop(waiter.correlation_id, None)
        metrics.reply_timeout_total.inc()
        return None

    def _purge_expired_locked(self) -> None:
        """Barrido de entradas imposibles de resolver.

        Se hace al registrar y no en un hilo aparte: el registro solo crece
        cuando llegan peticiones, así que la purga ocurre exactamente cuando hace
        falta y no consume un hilo más de los que SP-5 mide.
        """
        if len(self._waiters) < 64:
            return
        cutoff = time.monotonic() - self._ttl_s
        expired = [cid for cid, w in self._waiters.items() if w.created_at < cutoff]
        for cid in expired:
            self._waiters.pop(cid, None)
        if expired:
            log.warning("esperas vencidas purgadas", extra={"count": len(expired)})
