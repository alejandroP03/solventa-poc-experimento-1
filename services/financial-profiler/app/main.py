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

from solventa_common import metrics
from solventa_common.app_factory import create_app
from solventa_common.config import load_config
from solventa_common.logging import get_logger

from .breaker import ProfileBreaker
from .openfinance import OpenFinanceClient, Outcome

log = get_logger("financial-profiler")

# Calidad del perfil que acompaña a toda cotización (§3.1).
FRESH = "FRESH"      # dato vivo del proveedor
DEGRADED = "DEGRADED"  # dato de caché dentro de TTL + gracia (fase de SP-3)
DEFAULT = "DEFAULT"    # sin dato: factores conservadores fijos (fase de SP-3)


def create() -> Flask:
    cfg = load_config(service_name="financial-profiler", default_port=8085)
    openfinance = OpenFinanceClient(cfg)
    breaker = ProfileBreaker(cfg, openfinance)

    def openfinance_reachable() -> tuple[bool, str]:
        """Check de /health/ready. No participa en ninguna decisión de tráfico.

        Deliberadamente separado del Ping-Echo del Monitor: mezclarlos haría que
        el veredicto de disponibilidad dependiera de quién preguntó, y SP-4 mide
        precisamente el desacuerdo entre fuentes de detección.
        """
        # Llama al cliente crudo y no al circuito: un /health/ready que pasara
        # por el breaker contaría como tráfico real y podría abrirlo, de modo que
        # el propio diagnóstico alteraría la táctica que se está midiendo.
        result = openfinance.fetch_profile("healthcheck")
        return result.ok, result.detail or result.outcome.value

    app = create_app(cfg, checks={"openfinance": openfinance_reachable})

    @app.post("/profile")
    def profile():
        payload = request.get_json(silent=True) or {}
        client_id = payload.get("client_id")
        if not client_id:
            return jsonify(error="falta 'client_id'"), 400

        result = breaker.fetch_profile(client_id)

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
                "breaker_state": breaker.state,
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

        REGLA DE DISEÑO: la señal **solo puede forzar la apertura, nunca el
        cierre**. El cierre queda siempre en manos de la lógica half-open, que se
        apoya en tráfico real. Un Monitor que cerrara el circuito reabriría la
        propagación del fallo basándose en un endpoint que puede mentir — y en
        los modos `slow` y `flaky` miente por diseño (§4).

        Esa asimetría es la que SP-4 debe validar con datos, no asumir.
        """
        payload = request.get_json(silent=True) or {}
        state = payload.get("state")
        if state not in ("up", "down"):
            return jsonify(error="'state' debe ser 'up' o 'down'"), 400

        metrics.health_signal_received_total.labels(state=state).inc()

        applied = False
        if state == "down":
            applied = breaker.force_open()
        # state == "up" se registra y se ignora deliberadamente: ver la regla.

        log.info(
            "señal de salud recibida",
            extra={
                "dependency": payload.get("dependency"),
                "state": state,
                "observed_at": payload.get("observed_at"),
                "applied": applied,
                "breaker_state": breaker.state,
            },
        )
        return jsonify(
            accepted=True,
            applied=applied,
            breaker_state=breaker.state,
            note=None if state == "down" else "la señal 'up' no cierra el circuito (§3.3)",
        ), 202

    @app.get("/internal/breaker")
    def breaker_state():
        """Introspección del circuito, para el smoke test y la depuración."""
        return jsonify(
            dependency="openfinance",
            state=breaker.state,
            fail_counter=breaker.fail_counter,
            fail_max=cfg.breaker_fail_max,
            reset_timeout_s=cfg.breaker_reset_timeout_s,
        ), 200

    return app


app = create()
