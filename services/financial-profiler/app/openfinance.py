"""Cliente de salida hacia Open Finance.

# TÁCTICA: timeout acotado — SP-1

Aquí se decide dónde se corta la espera a una dependencia externa no controlable.
El valor del timeout es la variable independiente de SP-1 y el trade-off que
expone es directo: cortar pronto protege el presupuesto de latencia del journey
pero descarta respuestas que sí iban a llegar, degradando el pricing; cortar tarde
conserva la precisión pero consume el presupuesto y retiene hilos.

Este módulo **no** decide qué hacer ante el fallo. Solo llama, mide y clasifica el
desenlace. La degradación a caché y el corte del circuito se añaden en las fases
de SP-2 y SP-3 sin tocar esta frontera.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import requests

from solventa_common import metrics
from solventa_common.config import Config
from solventa_common.http_client import BoundedPool, HttpClient, PoolRejected
from solventa_common.logging import get_logger

log = get_logger("openfinance")


class Outcome(str, Enum):
    """Desenlaces de `solventa_openfinance_calls_total{outcome}`."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    REJECTED_OPEN = "rejected_open"  # el breaker cortó sin llamar (SP-2)
    REJECTED_POOL = "rejected_pool"  # el bulkhead cortó sin llamar (SP-5)


@dataclass(frozen=True)
class CallResult:
    outcome: Outcome
    profile: dict | None = None
    duration_s: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS


class OpenFinanceClient:
    def __init__(self, cfg: Config, client: HttpClient | None = None) -> None:
        self.cfg = cfg
        self.timeout_s = cfg.openfinance_timeout_s

        # TÁCTICA: bulkhead — SP-5 (pool aislado B: salida hacia Open Finance)
        # Uno de los dos frentes de saturación que SP-5 mide. Acota cuántas
        # llamadas pueden estar colgadas contra el proveedor a la vez, de modo
        # que un proveedor que no responde no consuma todos los hilos de este
        # servicio. `POOL_OPENFINANCE_MAX` es su variable independiente.
        #
        # Rechazo inmediato (acquire_timeout_s=0): esperar por un slot para
        # luego esperar el timeout completo retendría el hilo el doble de tiempo,
        # que es justo el recurso que este bulkhead protege.
        self.pool = BoundedPool(
            "openfinance",
            cfg.pool_openfinance_max,
            enabled=cfg.tactic_enabled("bulkhead"),
            acquire_timeout_s=0.0,
        )
        self._client = client or HttpClient(
            cfg.openfinance_url, timeout_s=self.timeout_s, pool=self.pool
        )

    def fetch_profile(self, client_id: str) -> CallResult:
        """Pide el perfil. Nunca lanza: clasifica el fallo y lo devuelve.

        Que no lance es deliberado. El invariante duro de §3.1 exige que ningún
        fallo aguas abajo se convierta en 5xx hacia el socio, y una excepción que
        atraviesa capas es justo el mecanismo por el que eso suele ocurrir.
        """
        started = time.perf_counter()
        try:
            response = self._client.get(f"/openfinance/v1/profiles/{client_id}")
        except requests.Timeout:
            elapsed = time.perf_counter() - started
            self._observe(Outcome.TIMEOUT, elapsed)
            # Métrica de decisión de SP-1: cuántas peticiones pagan el timeout
            # completo antes de que alguna táctica corte la propagación.
            metrics.openfinance_timeout_exhausted_total.inc()
            log.warning(
                "timeout hacia Open Finance",
                extra={"client_id": client_id, "timeout_s": self.timeout_s},
            )
            return CallResult(Outcome.TIMEOUT, duration_s=elapsed, detail="timeout")
        except PoolRejected as exc:
            # El bulkhead (SP-5) rechazó sin llegar a llamar. NO es un fallo del
            # proveedor y no debe contaminar el conteo del breaker: contarlo
            # abriría el circuito por saturación propia y SP-2 mediría en parte
            # el efecto de SP-5.
            #
            # Se etiqueta `rejected_pool` y no `rejected_open` para poder separar
            # en el informe cuántas peticiones cortó el bulkhead de cuántas cortó
            # el circuito: son tácticas distintas con criterios distintos.
            elapsed = time.perf_counter() - started
            self._observe(Outcome.REJECTED_POOL, elapsed)
            return CallResult(Outcome.REJECTED_POOL, duration_s=elapsed, detail=str(exc))
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            self._observe(Outcome.ERROR, elapsed)
            log.warning(
                "error de transporte hacia Open Finance",
                extra={"client_id": client_id, "error": str(exc)},
            )
            return CallResult(Outcome.ERROR, duration_s=elapsed, detail=str(exc))

        elapsed = time.perf_counter() - started

        if response.status_code >= 500:
            self._observe(Outcome.ERROR, elapsed)
            return CallResult(
                Outcome.ERROR, duration_s=elapsed, detail=f"HTTP {response.status_code}"
            )
        if response.status_code != 200:
            # Un 4xx es un problema de la petición, no una indisponibilidad del
            # proveedor. Se separa para no inflar el conteo de fallos del breaker
            # con errores que reintentar no arreglaría.
            self._observe(Outcome.ERROR, elapsed)
            return CallResult(
                Outcome.ERROR, duration_s=elapsed, detail=f"HTTP {response.status_code}"
            )

        self._observe(Outcome.SUCCESS, elapsed)
        return CallResult(Outcome.SUCCESS, profile=response.json(), duration_s=elapsed)

    def _observe(self, outcome: Outcome, elapsed: float) -> None:
        # La duración se observa también en los fallos: una llamada que agota el
        # timeout consume presupuesto igual que una exitosa, y omitirla sesgaría
        # el histograma justo durante la ventana de indisponibilidad.
        metrics.openfinance_duration_seconds.observe(elapsed)
        metrics.openfinance_calls_total.labels(outcome=outcome.value).inc()
