"""Resolución del perfil financiero — las dos rutas de §3.4.

`baseline`  : HTTP síncrono y bloqueante contra `financial-profiler`. El fallo del
              proveedor se propaga al socio. **Este modo debe fallar**: es el
              control que demuestra que el problema existe (SP-0).
`treatment` : publica un ProfileRequest en la cola y espera la respuesta
              correlacionada con presupuesto acotado. Se añade en la fase del
              broker; hasta entonces esta ruta no está disponible.

La abstracción existe para que ambas rutas compartan el resto del journey —
llamada al Provider, composición del precio, métricas— y la única diferencia
medida sea la que el experimento quiere aislar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from solventa_common import metrics
from solventa_common.config import Config
from solventa_common.http_client import HttpClient, PoolRejected
from solventa_common.logging import get_logger

log = get_logger("profiling")

FRESH = "FRESH"
DEGRADED = "DEGRADED"
DEFAULT = "DEFAULT"


@dataclass(frozen=True)
class ProfileOutcome:
    quality: str
    profile: dict[str, Any] | None = None
    # Solo en baseline: el fallo que debe propagarse como 5xx. En treatment
    # siempre es None, porque el invariante duro de §3.1 no admite excepciones.
    upstream_error: str | None = None
    upstream_status: int | None = None


class ProfileResolver(Protocol):
    def resolve(self, client_id: str) -> ProfileOutcome: ...


class BaselineResolver:
    """Cadena bloqueante sin tácticas (§3.4).

    Sin caché, sin breaker, sin cola y sin bulkhead, con timeout de 30 s. La
    saturación de workers que esto provoca no es un defecto de la implementación:
    es el fenómeno que SP-0 debe exhibir para que el resto del experimento tenga
    premisa.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = HttpClient(cfg.profiler_url, timeout_s=cfg.openfinance_timeout_s)

    def resolve(self, client_id: str) -> ProfileOutcome:
        try:
            response = self._client.post("/profile", json={"client_id": client_id})
        except requests.Timeout:
            return ProfileOutcome(
                DEFAULT, upstream_error="timeout hacia financial-profiler", upstream_status=504
            )
        except requests.RequestException as exc:
            return ProfileOutcome(DEFAULT, upstream_error=str(exc), upstream_status=502)

        if response.status_code == 200:
            body = response.json()
            return ProfileOutcome(body.get("profile_quality", FRESH), body.get("profile"))

        # El profiler ya devolvió 502/504 porque Open Finance no respondió. En
        # baseline eso viaja hasta el socio tal cual: es el acoplamiento temporal
        # que el experimento quiere demostrar.
        return ProfileOutcome(
            DEFAULT,
            upstream_error=f"financial-profiler devolvió {response.status_code}",
            upstream_status=response.status_code,
        )


class TreatmentResolver:
    """Request-reply sobre RabbitMQ con presupuesto acotado (§3.1, §3.2).

    Publica un `ProfileRequest` en `cotizacion.requests` con `correlation_id` y
    `reply_to`, y espera la respuesta correlacionada hasta `REPLY_TIMEOUT_MS`.

    NUNCA propaga un fallo. Las tres formas de no obtener perfil —pool lleno,
    fallo al publicar, presupuesto vencido— se resuelven con `DEFAULT`, porque el
    invariante duro de §3.1 no admite 5xx por causa de Open Finance ni por
    vencimiento de la espera.
    """

    def __init__(self, cfg: Config, publisher, registry, reply_queue: str, pending_pool) -> None:  # noqa: ANN001
        self.cfg = cfg
        self.publisher = publisher
        self.registry = registry
        self.reply_queue = reply_queue
        self.pending_pool = pending_pool

    def resolve(self, client_id: str) -> ProfileOutcome:
        from solventa_common.correlation import get_correlation_id

        correlation_id = get_correlation_id()

        # TÁCTICA: bulkhead — SP-5 (pool C: esperas de réplica concurrentes)
        # Este es el frente de saturación que amenaza directamente a la ruta
        # Provider: cada espera retiene un hilo de Gunicorn mientras la cola no
        # responde. Rechazar sin esperar es lo que deja hilos libres para la otra
        # ruta; el rechazo se degrada a DEFAULT, jamás a 5xx.
        try:
            with self.pending_pool.slot():
                return self._publish_and_wait(client_id, correlation_id)
        except PoolRejected:
            log.warning(
                "espera de réplica rechazada por bulkhead",
                extra={"client_id": client_id, "pool": "pending_replies"},
            )
            return ProfileOutcome(DEFAULT)

    def _publish_and_wait(self, client_id: str, correlation_id: str) -> ProfileOutcome:
        # Registrar ANTES de publicar: si el procesador fuera más rápido que este
        # hilo, una réplica que llegara antes del registro se contaría como
        # huérfana y la petición vencería por timeout con la respuesta ya en casa.
        waiter = self.registry.register(correlation_id)

        try:
            with metrics.observe_stage("broker_publish"):
                self.publisher.publish(
                    exchange=self.cfg.exchange_quotes,
                    routing_key=self.cfg.queue_requests,
                    payload={"client_id": client_id, "requested_at": time.time()},
                    correlation_id=correlation_id,
                    reply_to=self.reply_queue,
                    queue_label=self.cfg.queue_requests,
                )
        except Exception as exc:  # noqa: BLE001 - el broker caído no puede dar 5xx
            self.registry.resolve(correlation_id, {})  # retira la espera
            log.error("no se pudo publicar el ProfileRequest", extra={"error": str(exc)})
            return ProfileOutcome(DEFAULT)

        payload = self.registry.wait(waiter, self.cfg.reply_timeout_s)

        if payload is None:
            # Venció REPLY_TIMEOUT_MS: 200 con DEFAULT e incremento del contador
            # (§3.1). La réplica que llegue después se descartará como huérfana
            # sin alterar la respuesta ya entregada.
            log.warning(
                "presupuesto de espera agotado",
                extra={"client_id": client_id, "reply_timeout_ms": self.cfg.reply_timeout_ms},
            )
            return ProfileOutcome(DEFAULT)

        return ProfileOutcome(
            payload.get("profile_quality", DEFAULT), payload.get("profile")
        )


class ProviderClient:
    """Llamada al servicio propio del socio.

    # TÁCTICA: bulkhead — SP-5 (pool aislado A)
    Este pool es el **control** de SP-5: `POOL_PROVIDER_MAX` es fijo y no una
    variable independiente. Que la ruta Provider tenga su propio pool es lo que
    debe impedir que la saturación del perfilamiento la alcance.
    """

    def __init__(self, cfg: Config, pool) -> None:  # noqa: ANN001 - BoundedPool
        self.cfg = cfg
        self._client = HttpClient(cfg.socio_url, timeout_s=2.0, pool=pool)

    def price(self, product_code: str) -> float | None:
        """Precio del socio, o None si no se pudo obtener.

        None no es un error del journey: la cotización se entrega igualmente. Un
        5xx aquí rompería el ASR por una causa distinta de la que se estudia.
        """
        try:
            with metrics.observe_stage("provider_call"):
                response = self._client.get(f"/provider/price?product_code={product_code}")
        except (PoolRejected, requests.RequestException) as exc:
            log.warning("provider no disponible", extra={"error": str(exc)})
            return None

        if response.status_code != 200:
            return None
        return float(response.json().get("provider_price", 0.0))
