"""financial-profiler (8085) — el componente bajo prueba.

Envuelve la llamada a Open Finance con las tácticas que el experimento mide.
En esta fase solo está la frontera con el timeout (SP-1); el circuit breaker
(SP-2), la caché de perfil (SP-3), la señalización del Monitor (SP-4) y el
bulkhead (SP-5) se añaden en sus fases sin mover esta interfaz.

Contrato con `procesador-cotizacion`:

    POST /profile { "client_id": "..." }
    -> 200 { "client_id", "profile", "profile_quality": FRESH|DEGRADED|DEFAULT }
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from solventa_common.app_factory import create_app
from solventa_common.config import load_config
from solventa_common.logging import get_logger

from .openfinance import OpenFinanceClient, Outcome

log = get_logger("financial-profiler")

# Calidad del perfil que acompaña a toda cotización (§3.1).
FRESH = "FRESH"      # dato vivo del proveedor
DEGRADED = "DEGRADED"  # dato de caché dentro de TTL + gracia (fase de SP-3)
DEFAULT = "DEFAULT"    # sin dato: factores conservadores fijos (fase de SP-3)


def create() -> Flask:
    cfg = load_config(service_name="financial-profiler", default_port=8085)
    openfinance = OpenFinanceClient(cfg)

    def openfinance_reachable() -> tuple[bool, str]:
        """Check de /health/ready. No participa en ninguna decisión de tráfico.

        Deliberadamente separado del Ping-Echo del Monitor: mezclarlos haría que
        el veredicto de disponibilidad dependiera de quién preguntó, y SP-4 mide
        precisamente el desacuerdo entre fuentes de detección.
        """
        result = openfinance.fetch_profile("healthcheck")
        return result.ok, result.detail or result.outcome.value

    app = create_app(cfg, checks={"openfinance": openfinance_reachable})

    @app.post("/profile")
    def profile():
        payload = request.get_json(silent=True) or {}
        client_id = payload.get("client_id")
        if not client_id:
            return jsonify(error="falta 'client_id'"), 400

        result = openfinance.fetch_profile(client_id)

        if result.ok:
            return jsonify(
                client_id=client_id,
                profile=result.profile,
                profile_quality=FRESH,
            ), 200

        # Sin tácticas todavía: el fallo del proveedor se propaga. Es el
        # comportamiento del modo `baseline` (§3.4) y lo que SP-0 debe demostrar
        # que ocurre antes de introducir la degradación. A partir de la fase de
        # SP-3 este camino devuelve 200 con DEGRADED o DEFAULT.
        status = 504 if result.outcome is Outcome.TIMEOUT else 502
        log.warning(
            "perfilamiento sin resolver",
            extra={
                "client_id": client_id,
                "outcome": result.outcome.value,
                "detail": result.detail,
            },
        )
        return jsonify(
            client_id=client_id,
            error="open finance no disponible",
            outcome=result.outcome.value,
        ), status

    @app.post("/internal/dependency-health")
    def dependency_health():
        """Señal del Monitor hacia el circuit breaker (§3.3).

        Declarado desde ahora para fijar el contrato, pero **inerte**: no hay
        breaker al que señalizar hasta la fase de SP-2, y la regla de que la señal
        solo puede forzar la apertura y nunca el cierre se implementa en la fase
        de SP-4. Aceptar la señal y no actuar todavía es preferible a que el
        Monitor reciba 404 y su contador de errores midiera un problema inexistente.
        """
        payload = request.get_json(silent=True) or {}
        state = payload.get("state")
        if state not in ("up", "down"):
            return jsonify(error="'state' debe ser 'up' o 'down'"), 400

        log.info(
            "señal de salud recibida (aún sin efecto)",
            extra={
                "dependency": payload.get("dependency"),
                "state": state,
                "observed_at": payload.get("observed_at"),
            },
        )
        return jsonify(accepted=True, applied=False, reason="breaker no implementado aún"), 202

    return app


app = create()
