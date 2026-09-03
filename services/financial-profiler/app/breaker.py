"""Circuit breaker sobre la dependencia de Open Finance.

# TÁCTICA: circuit breaker — SP-2

Decide **cuántas peticiones pagan el costo completo del fallo** antes de que el
circuito abra, y con qué rapidez recupera precisión sin oscilar. Las dos
variables independientes de SP-2 son `BREAKER_FAIL_MAX` (umbral de apertura) y
`BREAKER_RESET_TIMEOUT_S` (ventana half-open).

Dos reglas de diseño que no son negociables:

1. **El rechazo por pool lleno no cuenta como fallo del proveedor.** Un
   `PoolRejected` es el bulkhead de SP-5 protegiendo recursos, no Open Finance
   cayéndose. Contarlo abriría el circuito por saturación propia y mezclaría los
   dos sub-experimentos: SP-2 mediría en parte el efecto de SP-5.

2. **La señal del Monitor solo puede forzar la apertura, nunca el cierre**
   (§3.3). El cierre queda siempre en la lógica half-open, que se apoya en
   tráfico real. Un Monitor que cerrara el circuito reabriría la propagación del
   fallo basándose en un endpoint que puede mentir — y en los modos `slow` y
   `flaky` miente por diseño (§4).
"""

from __future__ import annotations

import threading
import time

import pybreaker

from solventa_common import metrics
from solventa_common.config import Config
from solventa_common.logging import get_logger

from .openfinance import CallResult, OpenFinanceClient, Outcome

log = get_logger("breaker")

DEPENDENCY = "openfinance"

# pybreaker usa 'half-open' con guion; las etiquetas de Prometheus llevan guion
# bajo para que las consultas del dashboard no necesiten escapes.
_STATE_CODE = {"closed": 0, "open": 1, "half-open": 2}
_STATE_LABEL = {"closed": "closed", "open": "open", "half-open": "half_open"}


class OpenFinanceFailure(Exception):
    """Envuelve un CallResult fallido para que pybreaker lo cuente.

    `OpenFinanceClient` devuelve el desenlace en lugar de lanzar, porque el
    invariante de §3.1 exige que ningún fallo escale como excepción hasta el
    socio. Pero pybreaker detecta fallos por excepción, así que la frontera se
    cruza aquí y solo aquí, dentro de la llamada guardada.
    """

    def __init__(self, result: CallResult) -> None:
        super().__init__(result.detail or result.outcome.value)
        self.result = result


class _MetricsListener(pybreaker.CircuitBreakerListener):
    """Traduce el ciclo de vida del breaker a las métricas de SP-2."""

    def __init__(self, owner: "ProfileBreaker") -> None:
        self.owner = owner

    def state_change(self, cb, old_state, new_state) -> None:  # noqa: ANN001
        old = getattr(old_state, "name", None) or "closed"
        new = getattr(new_state, "name", None) or "closed"

        metrics.circuit_breaker_state.labels(dependency=DEPENDENCY).set(
            _STATE_CODE.get(new, 0)
        )
        metrics.circuit_breaker_transitions_total.labels(
            from_state=_STATE_LABEL.get(old, old), to_state=_STATE_LABEL.get(new, new)
        ).inc()

        if new == "open":
            self.owner._on_opened()
        elif new == "closed":
            self.owner._on_closed()

        log.info(
            "transición del circuito",
            extra={"dependency": DEPENDENCY, "from_state": old, "to_state": new},
        )

    def failure(self, cb, exc) -> None:  # noqa: ANN001
        metrics.circuit_breaker_calls_total.labels(outcome="failure").inc()
        self.owner._mark_failure_observed()

    def success(self, cb) -> None:  # noqa: ANN001
        metrics.circuit_breaker_calls_total.labels(outcome="success").inc()


class ProfileBreaker:
    """Envuelve al cliente de Open Finance con el circuito."""

    def __init__(self, cfg: Config, client: OpenFinanceClient) -> None:
        self.cfg = cfg
        self.client = client
        self.enabled = cfg.tactic_enabled("breaker")

        self._lock = threading.Lock()
        # Instante del primer fallo de la racha actual. Sirve para medir la
        # ventana de detección reactiva: primer fallo observado -> corte efectivo.
        # Es una cota inferior de la latencia de detección real; la que parte de
        # la inyección la calcula collect_results.py (ver OBSERVACIONES OBS-04).
        self._first_failure_at: float | None = None
        self._forced_open = False

        self._breaker = pybreaker.CircuitBreaker(
            fail_max=cfg.breaker_fail_max,
            reset_timeout=cfg.breaker_reset_timeout_s,
            listeners=[_MetricsListener(self)],
            name=DEPENDENCY,
        )
        metrics.circuit_breaker_state.labels(dependency=DEPENDENCY).set(0)

    # --- Estado ----------------------------------------------------------- #

    @property
    def state(self) -> str:
        if not self.enabled:
            return "disabled"
        return self._breaker.current_state

    @property
    def fail_counter(self) -> int:
        return self._breaker.fail_counter

    # --- Llamada guardada -------------------------------------------------- #

    def fetch_profile(self, client_id: str) -> CallResult:
        """Pide el perfil a través del circuito.

        Devuelve siempre un CallResult; nunca lanza. Con el circuito abierto
        devuelve `rejected_open` **sin llamar al proveedor**, que es el ahorro de
        latencia y de recursos que la táctica persigue.
        """
        if not self.enabled:
            # Ablación de SP-2: sin breaker, cada petición paga el costo completo
            # del fallo durante toda la ventana de indisponibilidad.
            return self.client.fetch_profile(client_id)

        try:
            return self._breaker.call(self._guarded, client_id)
        except pybreaker.CircuitBreakerError:
            metrics.circuit_breaker_calls_total.labels(outcome="rejected").inc()
            metrics.openfinance_calls_total.labels(
                outcome=Outcome.REJECTED_OPEN.value
            ).inc()
            return CallResult(Outcome.REJECTED_OPEN, detail="circuito abierto")
        except OpenFinanceFailure as failure:
            return failure.result

    def _guarded(self, client_id: str) -> CallResult:
        result = self.client.fetch_profile(client_id)

        if result.outcome in (Outcome.TIMEOUT, Outcome.ERROR):
            # Fallo atribuible al proveedor: cuenta para el umbral.
            raise OpenFinanceFailure(result)

        if result.outcome is Outcome.REJECTED_OPEN:
            # El bulkhead rechazó antes de llamar (SP-5). NO es un fallo del
            # proveedor: contarlo abriría el circuito por saturación propia y
            # contaminaría la medición de SP-2 con el efecto de SP-5.
            return result

        return result

    # --- Señal del Monitor (§3.3) ----------------------------------------- #

    def force_open(self, source: str = "monitor_signal") -> bool:
        """Fuerza la apertura. Devuelve False si ya estaba abierto.

        Es la única acción que la señal externa puede provocar. No existe un
        `force_close` a propósito: el cierre depende de tráfico real.
        """
        if not self.enabled:
            return False
        with self._lock:
            if self._breaker.current_state == "open":
                return False
            self._forced_open = True
        self._breaker.open()
        metrics.detection_source_total.labels(source=source).inc()
        log.warning(
            "circuito abierto por señal externa",
            extra={"dependency": DEPENDENCY, "source": source},
        )
        return True

    # --- Hooks del listener ------------------------------------------------ #

    def _mark_failure_observed(self) -> None:
        with self._lock:
            if self._first_failure_at is None:
                self._first_failure_at = time.monotonic()

    def _on_opened(self) -> None:
        with self._lock:
            first_failure_at = self._first_failure_at
            forced = self._forced_open
            self._forced_open = False
            self._first_failure_at = None

        if forced:
            # La apertura la provocó el Monitor; su latencia de detección se
            # atribuye a esa fuente, no al conteo del breaker.
            return

        metrics.detection_source_total.labels(source="breaker_count").inc()
        if first_failure_at is not None:
            metrics.detection_latency_seconds.labels(source="breaker_count").observe(
                time.monotonic() - first_failure_at
            )

    def _on_closed(self) -> None:
        with self._lock:
            self._first_failure_at = None
