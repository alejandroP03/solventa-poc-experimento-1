"""Caché de perfil en Redis con TTL y ventana de gracia.

# TÁCTICA: caché de perfil (degradación elegante) — SP-3

Es lo que convierte una indisponibilidad de Open Finance en una degradación del
`profile_quality` en lugar de un 5xx. Sus dos variables independientes son
`PROFILE_CACHE_TTL_S` y `PROFILE_CACHE_STALE_GRACE_S`, más la proporción de
precarga `CACHE_PRELOAD_RATIO` que gobierna el escenario de caché fría.

Redis se usa **exclusivamente como caché**, nunca como broker (§1). Compartir la
instancia entre caché y mensajería crearía un dominio de fallo común que
confundiría la medición de SP-5, cuyo objeto es precisamente el aislamiento.

Clasificación de la calidad
---------------------------
`FRESH` se reserva para el dato **vivo** del proveedor. Cualquier acierto de
caché es `DEGRADED`, incluso dentro del TTL: el dato es real pero no es actual, y
el socio necesita saberlo porque el precio se calculó con él.

La distinción `hit_fresh` / `hit_stale` de las métricas es más fina que la del
`profile_quality` a propósito: SP-3 necesita saber **qué antigüedad** tenía el
dato servido para poder discutir las implicaciones de pricing y de auditoría de
un TTL largo, y eso no cabe en un campo de tres valores.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import redis

from solventa_common import metrics
from solventa_common.config import Config
from solventa_common.logging import get_logger

log = get_logger("cache")

KEY_PREFIX = "solventa:profile:"
FIELD_CACHED_AT = "_cached_at"


class CacheResult(str, Enum):
    HIT_FRESH = "hit_fresh"
    HIT_STALE = "hit_stale"
    MISS = "miss"
    WRITE = "write"


@dataclass(frozen=True)
class CacheLookup:
    result: CacheResult
    profile: dict[str, Any] | None = None
    age_s: float = 0.0

    @property
    def hit(self) -> bool:
        return self.result in (CacheResult.HIT_FRESH, CacheResult.HIT_STALE)


def profile_key(client_id: str) -> str:
    return f"{KEY_PREFIX}{client_id}"


class ProfileCache:
    def __init__(self, cfg: Config, client: redis.Redis | None = None) -> None:
        self.cfg = cfg
        self.enabled = cfg.tactic_enabled("cache")
        self.ttl_s = cfg.profile_cache_ttl_s
        self.grace_s = cfg.profile_cache_stale_grace_s

        # `from_url` no conecta hasta la primera operación, así que el servicio
        # arranca aunque Redis no esté listo (§8.2). Los timeouts son cortos a
        # propósito: la caché está en el camino crítico de un presupuesto de
        # 250 ms, y una caché lenta sería peor que no tenerla.
        self._redis = client or redis.Redis.from_url(
            cfg.redis_url,
            decode_responses=True,
            socket_timeout=0.25,
            socket_connect_timeout=0.25,
        )

    # --- Lectura ----------------------------------------------------------- #

    def get(self, client_id: str) -> CacheLookup:
        """Busca el perfil. Nunca lanza: un fallo de Redis es un MISS.

        Que un Redis caído se comporte como caché vacía y no como error es lo que
        mantiene el invariante de §3.1: la cotización sale con `DEFAULT`, no con
        un 5xx.
        """
        if not self.enabled:
            # Ablación de SP-3: sin caché, toda indisponibilidad produce DEFAULT.
            return CacheLookup(CacheResult.MISS)

        try:
            raw = self._redis.get(profile_key(client_id))
        except redis.RedisError as exc:
            log.warning("lectura de caché falló", extra={"error": str(exc)})
            metrics.profile_cache_operations_total.labels(result=CacheResult.MISS.value).inc()
            return CacheLookup(CacheResult.MISS)

        if raw is None:
            metrics.profile_cache_operations_total.labels(result=CacheResult.MISS.value).inc()
            return CacheLookup(CacheResult.MISS)

        try:
            payload = json.loads(raw)
            cached_at = float(payload.pop(FIELD_CACHED_AT))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.warning("entrada de caché ilegible", extra={"client_id": client_id})
            metrics.profile_cache_operations_total.labels(result=CacheResult.MISS.value).inc()
            return CacheLookup(CacheResult.MISS)

        age_s = max(time.time() - cached_at, 0.0)

        # Edad del dato servido: evidencia de la implicación de pricing y de
        # auditoría de un TTL largo (métrica de decisión de SP-3).
        metrics.profile_cache_age_seconds.observe(age_s)

        if age_s > self.ttl_s + self.grace_s:
            # Fuera de TTL + gracia. En teoría Redis ya lo habría expirado; se
            # comprueba igualmente porque bajar el TTL entre corridas deja
            # entradas viejas con el TTL anterior, y servirlas mediría un TTL
            # que no es el configurado.
            metrics.profile_cache_operations_total.labels(result=CacheResult.MISS.value).inc()
            return CacheLookup(CacheResult.MISS, age_s=age_s)

        result = CacheResult.HIT_FRESH if age_s <= self.ttl_s else CacheResult.HIT_STALE
        metrics.profile_cache_operations_total.labels(result=result.value).inc()
        return CacheLookup(result, profile=payload, age_s=age_s)

    # --- Escritura --------------------------------------------------------- #

    def put(self, client_id: str, profile: dict[str, Any]) -> None:
        """Guarda un perfil recién obtenido. Nunca lanza.

        El TTL de Redis es `TTL + gracia`: la entrada debe **sobrevivir** al TTL
        lógico para poder servirse como `hit_stale` durante la ventana de gracia.
        Si Redis expirara en el TTL, `PROFILE_CACHE_STALE_GRACE_S` no tendría
        ningún efecto observable y SP-3 perdería una de sus tres variables.
        """
        if not self.enabled:
            return

        payload = dict(profile)
        payload[FIELD_CACHED_AT] = time.time()

        try:
            self._redis.set(
                profile_key(client_id),
                json.dumps(payload),
                ex=self.ttl_s + self.grace_s,
            )
        except redis.RedisError as exc:
            log.warning("escritura de caché falló", extra={"error": str(exc)})
            return

        metrics.profile_cache_operations_total.labels(result=CacheResult.WRITE.value).inc()

    # --- Diagnóstico ------------------------------------------------------- #

    def ping(self) -> tuple[bool, str]:
        if not self.enabled:
            return True, "caché desactivada (ablación de SP-3)"
        try:
            self._redis.ping()
            return True, "conectado"
        except redis.RedisError as exc:
            return False, str(exc)
