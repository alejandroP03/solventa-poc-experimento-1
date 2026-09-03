"""mock-openfinance (8090) — proveedor externo simulado.

Es el instrumento del experimento: expone el endpoint de negocio que consume el
`financial-profiler`, el `/health` que sondea el `monitor`, y los controles de
inyección de fallos en caliente que arman cada fixture de §7.
"""

from __future__ import annotations

import time

from flask import Flask, jsonify, request
from prometheus_client import Gauge

from solventa_common import metrics
from solventa_common.app_factory import create_app
from solventa_common.config import load_config
from solventa_common.logging import get_logger

from .faults import MODE_CODES, FaultError, FaultInjector
from .profiles import build_profile

log = get_logger("mock-openfinance")

# Gauge del modo activo, para anotar la ventana de fallo como overlay en Grafana
# (§4). Sin esta serie, correlacionar visualmente "aquí empezó el fallo" con el
# resto de paneles dependería de recordar los timestamps a mano.
mock_mode = Gauge(
    "mock_openfinance_mode",
    "Modo de fallo activo: 0=normal 1=slow 2=error_5xx 3=timeout 4=flaky",
    multiprocess_mode="livemostrecent",
)


def create() -> Flask:
    cfg = load_config(service_name="mock-openfinance", default_port=8090)
    injector = FaultInjector()
    mock_mode.set(MODE_CODES["normal"])

    # health_alias=False: /health lo define este servicio, porque es el objetivo
    # del Ping-Echo del Monitor y debe reflejar el estado inyectado.
    app = create_app(cfg, health_alias=False)

    # --- Endpoint de negocio --------------------------------------------- #

    @app.get("/openfinance/v1/profiles/<client_id>")
    def get_profile(client_id: str):
        delay, status = injector.business_delay_and_status()
        if delay:
            time.sleep(delay)
        if status != 200:
            return jsonify(error="upstream unavailable", status=status), status
        return jsonify(build_profile(client_id)), 200

    # --- /health: el que sondea el Monitor -------------------------------- #

    @app.get("/health")
    def health():
        """Ping-Echo del Monitor.

        En `slow` y `flaky` responde 200 rápido mientras el endpoint de negocio se
        arrastra o falla. La discrepancia es deliberada (§4) y es el hallazgo que
        SP-4 debe producir: el Ping-Echo observa un endpoint que no es el que
        importa. No se corrige.
        """
        delay, status = injector.health_status()
        if delay:
            time.sleep(delay)
        if status != 200:
            return jsonify(status="unavailable", mode=injector.mode), status
        return jsonify(status="ok", mode=injector.mode), 200

    # --- Controles de inyección en caliente ------------------------------- #

    @app.post("/admin/mode")
    def set_mode():
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode")
        if not mode:
            return jsonify(error="falta 'mode'"), 400
        try:
            state = injector.set_mode(
                mode,
                latency_ms=payload.get("latency_ms"),
                failure_rate=payload.get("failure_rate"),
                duration_s=payload.get("duration_s"),
            )
        except FaultError as exc:
            return jsonify(error=str(exc)), 400

        mock_mode.set(MODE_CODES[state.mode])
        # Este log es el registro auditable del instante de inyección: es lo que
        # permite reconstruir la ventana si metadata.json se perdiera.
        log.info("modo de fallo inyectado", extra=state.as_dict())
        return jsonify(state.as_dict()), 200

    @app.get("/admin/state")
    def get_state():
        state = injector.state
        # El auto-revert por duration_s ocurre en un timer: se refleja el modo
        # vigente en el gauge por si la consulta llega justo después.
        mock_mode.set(MODE_CODES[state.mode])
        return jsonify(state.as_dict()), 200

    @app.post("/admin/reset")
    def reset():
        state = injector.reset()
        mock_mode.set(MODE_CODES[state.mode])
        log.info("modo de fallo restablecido", extra=state.as_dict())
        return jsonify(state.as_dict()), 200

    return app


app = create()
