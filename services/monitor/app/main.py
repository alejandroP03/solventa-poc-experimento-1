"""monitor (8086) — detección proactiva por Ping-Echo.

# TÁCTICA: Ping-Echo + señalización al circuit breaker — SP-4

Sondea `GET /health` de `mock-openfinance` cada `MONITOR_INTERVAL_MS` y, al
acumular `MONITOR_UNHEALTHY_THRESHOLD` fallos consecutivos, notifica al
`financial-profiler` por HTTP para que fuerce la apertura del circuito.

Es **una de las dos fuentes de verdad** de SP-4. La otra es el conteo de fallos
del breaker sobre tráfico real. El sub-experimento existe para cuantificar el
trade-off entre ellas:

- El Monitor detecta antes y **sin gastar peticiones de usuario**, pero observa un
  endpoint que no es el que importa. En los modos `slow` y `flaky` el `/health`
  del proveedor responde 200 mientras el endpoint de negocio se arrastra o falla
  (§4): el Monitor declarará sano un proveedor que no lo está. Ese punto ciego es
  el hallazgo que SP-4 debe producir, y **no se corrige**.
- El breaker observa la verdad operativa, pero solo la descubre gastando
  peticiones reales.

Con `MONITOR_SIGNAL_ENABLED=false` el Monitor **sigue midiendo y exportando
métricas pero no envía la señal**, dejando solo la detección reactiva. Esa es la
variable independiente de SP-4, y por eso el lazo de sondeo no se apaga con el
flag: si se apagara, el dashboard no podría mostrar el desacuerdo entre lo que el
Ping-Echo creía y lo que el tráfico real encontraba.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import requests
from flask import Flask, jsonify

from solventa_common import metrics
from solventa_common.app_factory import create_app, on_shutdown
from solventa_common.config import load_config
from solventa_common.logging import get_logger

log = get_logger("monitor")

DEPENDENCY = "openfinance"


class DependencyMonitor:
    """Lazo de Ping-Echo con histéresis y señalización."""

    def __init__(self, cfg) -> None:  # noqa: ANN001
        self.cfg = cfg
        self.interval_s = cfg.monitor_interval_ms / 1000.0
        self.timeout_s = cfg.monitor_timeout_ms / 1000.0
        self.signal_enabled = cfg.tactic_enabled("monitor_signal")

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Histéresis: N fallos seguidos para declarar caído, M éxitos seguidos
        # para declarar sano. Sin ella, un único ping perdido por jitter de red
        # abriría el circuito y el flapping que SP-2 mide vendría del Monitor en
        # lugar de la configuración del breaker.
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._declared_up = True
        self._last_probe: dict[str, object] = {}

        self._session = requests.Session()
        metrics.monitor_dependency_up.labels(dependency=DEPENDENCY).set(1)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ping-echo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    @property
    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "dependency": DEPENDENCY,
                "declared_up": self._declared_up,
                "consecutive_failures": self._consecutive_failures,
                "consecutive_successes": self._consecutive_successes,
                "signal_enabled": self.signal_enabled,
                "interval_ms": self.cfg.monitor_interval_ms,
                "unhealthy_threshold": self.cfg.monitor_unhealthy_threshold,
                "healthy_threshold": self.cfg.monitor_healthy_threshold,
                "last_probe": dict(self._last_probe),
            }

    # --- Lazo -------------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            healthy, detail = self._probe()
            self._record(healthy, detail, time.perf_counter() - started)
            # El intervalo se cuenta desde el inicio del sondeo, no desde su fin:
            # así la cadencia es estable aunque un ping tarde, y la latencia de
            # detección de SP-4 depende del intervalo configurado y no de la
            # latencia del propio proveedor.
            self._stop.wait(max(self.interval_s - (time.perf_counter() - started), 0.0))

    def _probe(self) -> tuple[bool, str]:
        """Ping-Echo contra el endpoint de salud del proveedor."""
        try:
            response = self._session.get(
                f"{self.cfg.openfinance_url}/health", timeout=self.timeout_s
            )
        except requests.Timeout:
            return False, "timeout"
        except requests.RequestException as exc:
            return False, f"{type(exc).__name__}"

        if response.status_code == 200:
            return True, "200"
        return False, f"HTTP {response.status_code}"

    def _record(self, healthy: bool, detail: str, elapsed_s: float) -> None:
        with self._lock:
            if healthy:
                self._consecutive_successes += 1
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                self._consecutive_successes = 0

            self._last_probe = {
                "healthy": healthy,
                "detail": detail,
                "duration_ms": round(elapsed_s * 1000, 1),
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            }

            crossed_down = (
                self._declared_up
                and self._consecutive_failures >= self.cfg.monitor_unhealthy_threshold
            )
            crossed_up = (
                not self._declared_up
                and self._consecutive_successes >= self.cfg.monitor_healthy_threshold
            )
            if crossed_down:
                self._declared_up = False
            elif crossed_up:
                self._declared_up = True

        # El gauge se actualiza en CADA sondeo, no solo al cruzar el umbral: es
        # la serie que, contrastada con la tasa de error real, hace visible el
        # desacuerdo del modo `slow` en el dashboard de SP-4.
        metrics.monitor_dependency_up.labels(dependency=DEPENDENCY).set(
            1 if self._declared_up else 0
        )

        if crossed_down:
            log.warning(
                "proveedor declarado CAÍDO por Ping-Echo",
                extra={"consecutive_failures": self.cfg.monitor_unhealthy_threshold,
                       "detail": detail},
            )
            self._signal("down")
        elif crossed_up:
            log.info("proveedor declarado SANO por Ping-Echo", extra={"detail": detail})
            self._signal("up")

    def _signal(self, state: str) -> None:
        """Notifica al circuit breaker (§3.3).

        La señal `up` se envía igualmente, aunque el profiler la ignore por
        diseño: enviarla y que se registre en `health_signal_received_total`
        deja en los datos la prueba de que el Monitor **quiso** cerrar el
        circuito y el diseño no le dejó. Esa asimetría es lo que SP-4 valida.
        """
        if not self.signal_enabled:
            # Variable independiente de SP-4: el Monitor sigue midiendo y
            # exportando, pero no interviene. La detección queda solo en el
            # conteo reactivo del breaker.
            log.info(
                "señal suprimida (MONITOR_SIGNAL_ENABLED=false)",
                extra={"state": state},
            )
            return

        payload = {
            "dependency": DEPENDENCY,
            "state": state,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        try:
            response = self._session.post(
                f"{self.cfg.profiler_url}/internal/dependency-health",
                json=payload,
                timeout=self.timeout_s,
            )
            log.info(
                "señal enviada al circuit breaker",
                extra={
                    "state": state,
                    "status": response.status_code,
                    "applied": response.json().get("applied") if response.ok else None,
                },
            )
        except requests.RequestException as exc:
            # Un fallo al señalizar no es fatal: el breaker seguirá detectando
            # por su propio conteo. Se registra porque una señal perdida
            # explicaría una latencia de detección anómala en SP-4.
            log.error("no se pudo señalizar", extra={"state": state, "error": str(exc)})


def create() -> Flask:
    cfg = load_config(service_name="monitor", default_port=8086)
    monitor = DependencyMonitor(cfg)
    monitor.start()
    on_shutdown(monitor.stop)

    def probe_ok() -> tuple[bool, str]:
        last = monitor.snapshot["last_probe"]
        if not last:
            return False, "sin sondeos todavía"
        return True, f"último sondeo: {last.get('detail')}"

    app = create_app(cfg, checks={"ping_echo": probe_ok})

    @app.get("/internal/monitor")
    def state():
        """Introspección del Ping-Echo, para el smoke test y el informe."""
        return jsonify(monitor.snapshot), 200

    return app


app = create()
