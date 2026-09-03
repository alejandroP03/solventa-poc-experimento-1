"""Generación determinista de perfiles financieros.

El mismo `client_id` produce siempre el mismo perfil, en esta y en cualquier
corrida futura. La reproducibilidad no es un detalle estético: si el perfil
variara entre llamadas, la prima calculada variaría con él y la comparación de
pricing entre FRESH, DEGRADED y DEFAULT —que es la métrica de decisión de SP-3—
mezclaría el efecto de la degradación con ruido del generador.

Se deriva de un hash del client_id en lugar de un RNG con estado: así el perfil no
depende del orden en que lleguen las peticiones ni del worker que las atienda.

Sin realismo actuarial (§2.3): estos números alimentan un factor de riesgo, no
una decisión de negocio.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

INCOME_BANDS = ("LOW", "MEDIUM_LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH")


def _digest(client_id: str) -> bytes:
    return hashlib.sha256(f"solventa:{client_id}".encode("utf-8")).digest()


def _unit(digest: bytes, offset: int) -> float:
    """Valor en [0, 1) a partir de dos bytes del digest."""
    return int.from_bytes(digest[offset : offset + 2], "big") / 65536.0


def build_profile(client_id: str) -> dict[str, Any]:
    digest = _digest(client_id)
    return {
        "client_id": client_id,
        "income_band": INCOME_BANDS[digest[0] % len(INCOME_BANDS)],
        "debt_ratio": round(_unit(digest, 2) * 0.85, 4),
        "payment_behavior_score": round(300 + _unit(digest, 4) * 550, 1),
        "stability_index": round(_unit(digest, 6), 4),
        # generated_at es el instante de la respuesta, no del hash: es lo que
        # permite a la caché calcular la antigüedad del dato servido
        # (solventa_profile_cache_age_seconds, métrica de decisión de SP-3).
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
    }
