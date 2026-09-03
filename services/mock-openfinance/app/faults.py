"""Inyector de fallos del proveedor externo simulado (kickoff §4).

Este módulo **es el instrumento del experimento**: los modos de fallo no son el
experimento en sí, son el fixture bajo el cual se mide cada punto de sensibilidad.
Debe ser controlable en caliente, sin reiniciar contenedores, para que la línea de
tiempo estándar de §7 (sano / fallo / recuperación) ocurra dentro de una sola
corrida de carga.

| Modo      | /openfinance/v1/profiles      | /health       |
|-----------|-------------------------------|---------------|
| normal    | 200 en 40-90 ms con jitter    | 200           |
| slow      | 200 en latency_ms +-15 %      | **200 rápido**|
| error_5xx | 503 inmediato                 | 503           |
| timeout   | nunca responde (sleep 60 s)   | no responde   |
| flaky     | normal/503 según failure_rate | 200           |

La asimetría de `/health` en `slow` y `flaky` es deliberada y central para SP-4:
el health check declara sano al proveedor mientras el endpoint de negocio se
arrastra o falla de forma intermitente. Ese es el punto ciego estructural de la
detección proactiva por Ping-Echo frente a la detección reactiva del breaker, que
observa tráfico real. **No se arregla.**
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MODES = ("normal", "slow", "error_5xx", "timeout", "flaky")

# Mapeo a entero para el gauge `mock_openfinance_mode`, que anota la ventana de
# fallo como overlay en Grafana (§4).
MODE_CODES = {"normal": 0, "slow": 1, "error_5xx": 2, "timeout": 3, "flaky": 4}

# Constantes del modo normal. Hardcodeadas a propósito: no son la variable
# independiente de ningún SP, definen qué significa "sano" (§0, regla rectora).
NORMAL_LATENCY_MIN_S = 0.040
NORMAL_LATENCY_MAX_S = 0.090

# El sleep del modo timeout supera con holgura cualquier OPENFINANCE_TIMEOUT_MS
# contrastado (máx. 1000 ms) y también el timeout de 30 s del baseline, de modo
# que el cliente siempre se rinde primero. Es lo que retiene recursos ocupados de
# forma sostenida, que es lo que SP-5 necesita del fixture.
TIMEOUT_SLEEP_S = 60.0

SLOW_JITTER = 0.15


class FaultError(ValueError):
    """Parámetros de inyección inválidos."""


@dataclass
class FaultState:
    mode: str = "normal"
    latency_ms: int = 1500
    failure_rate: float = 0.5
    changed_at: float = field(default_factory=time.time)
    reverts_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "latency_ms": self.latency_ms,
            "failure_rate": self.failure_rate,
            "changed_at": self.changed_at,
            "reverts_at": self.reverts_at,
            "seconds_in_mode": round(time.time() - self.changed_at, 3),
        }


class FaultInjector:
    """Estado de fallo mutable en caliente, seguro entre hilos."""

    def __init__(self) -> None:
        self._state = FaultState()
        self._lock = threading.Lock()
        self._revert_timer: threading.Timer | None = None
        # Semilla fija: dos corridas con la misma configuración deben producir la
        # misma secuencia de fallos en modo flaky. Sin esto, el flapping medido en
        # SP-2 variaría entre corridas por azar y no por la configuración.
        self._random = random.Random(20260902)

    @property
    def state(self) -> FaultState:
        with self._lock:
            return FaultState(**vars(self._state))

    @property
    def mode(self) -> str:
        with self._lock:
            return self._state.mode

    def set_mode(
        self,
        mode: str,
        *,
        latency_ms: int | None = None,
        failure_rate: float | None = None,
        duration_s: float | None = None,
    ) -> FaultState:
        if mode not in MODES:
            raise FaultError(f"modo {mode!r} desconocido; usar uno de {list(MODES)}")
        if latency_ms is not None and latency_ms < 0:
            raise FaultError("latency_ms debe ser >= 0")
        if failure_rate is not None and not 0.0 <= failure_rate <= 1.0:
            raise FaultError("failure_rate debe estar entre 0.0 y 1.0")
        if duration_s is not None and duration_s <= 0:
            raise FaultError("duration_s debe ser > 0")

        with self._lock:
            self._cancel_timer_locked()
            now = time.time()
            self._state = FaultState(
                mode=mode,
                latency_ms=latency_ms if latency_ms is not None else self._state.latency_ms,
                failure_rate=(
                    failure_rate if failure_rate is not None else self._state.failure_rate
                ),
                changed_at=now,
                reverts_at=now + duration_s if duration_s else None,
            )
            # La secuencia de flaky se reinicia con cada inyección para que la
            # ventana de indisponibilidad sea idéntica entre corridas comparables.
            self._random.seed(20260902)
            if duration_s:
                self._revert_timer = threading.Timer(duration_s, self._auto_revert)
                self._revert_timer.daemon = True
                self._revert_timer.start()
            return FaultState(**vars(self._state))

    def reset(self) -> FaultState:
        return self.set_mode("normal")

    def _auto_revert(self) -> None:
        with self._lock:
            self._state = FaultState(
                mode="normal",
                latency_ms=self._state.latency_ms,
                failure_rate=self._state.failure_rate,
                changed_at=time.time(),
            )
            self._random.seed(20260902)
            self._revert_timer = None

    def _cancel_timer_locked(self) -> None:
        if self._revert_timer is not None:
            self._revert_timer.cancel()
            self._revert_timer = None

    # --- Decisiones de comportamiento ------------------------------------- #

    def business_delay_and_status(self) -> tuple[float, int]:
        """(segundos a dormir, código HTTP) del endpoint de negocio."""
        with self._lock:
            mode = self._state.mode
            latency_ms = self._state.latency_ms
            failure_rate = self._state.failure_rate
            roll = self._random.random() if mode == "flaky" else 0.0

        if mode == "normal":
            return self._normal_latency(), 200
        if mode == "slow":
            base = latency_ms / 1000.0
            return base * (1 + self._random.uniform(-SLOW_JITTER, SLOW_JITTER)), 200
        if mode == "error_5xx":
            return 0.0, 503
        if mode == "timeout":
            return TIMEOUT_SLEEP_S, 200  # el cliente se rinde mucho antes
        if mode == "flaky":
            if roll < failure_rate:
                return 0.0, 503
            return self._normal_latency(), 200
        raise FaultError(f"modo {mode!r} sin comportamiento definido")

    def health_status(self) -> tuple[float, int]:
        """(segundos a dormir, código HTTP) del endpoint de health.

        En `slow` y `flaky` responde 200 rápido **a propósito**: es el punto ciego
        del Ping-Echo que SP-4 debe cuantificar.
        """
        mode = self.mode
        if mode == "error_5xx":
            return 0.0, 503
        if mode == "timeout":
            return TIMEOUT_SLEEP_S, 200
        return 0.0, 200

    def _normal_latency(self) -> float:
        return self._random.uniform(NORMAL_LATENCY_MIN_S, NORMAL_LATENCY_MAX_S)
