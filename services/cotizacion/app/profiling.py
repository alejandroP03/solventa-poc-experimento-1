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
