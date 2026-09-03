"""financial-profiler (8085) — el componente bajo prueba.

Envuelve la llamada a Open Finance con las tácticas que el experimento mide.
Alberga cuatro de las cinco tácticas bajo prueba: timeout acotado (SP-1),
circuit breaker (SP-2), caché de perfil con TTL y ventana de gracia (SP-3) y el
endpoint de señalización que usa el Monitor (SP-4). El bulkhead de salida (SP-5)
se añade en su fase.

En modo `treatment` este servicio **nunca** devuelve 5xx: todo fallo aguas abajo
sale como una degradación del profile_quality. En `baseline` se propaga a
propósito, porque es el control que demuestra que el problema existe.

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
from .cache import ProfileCache
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
    cache = ProfileCache(cfg)

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

    app = create_app(
        cfg, checks={"openfinance": openfinance_reachable, "redis": cache.ping}
    )

    @app.post("/profile")
    def profile():
        payload = request.get_json(silent=True) or {}
        client_id = payload.get("client_id")
        if not client_id:
            return jsonify(error="falta 'client_id'"), 400

        result = breaker.fetch_profile(client_id)

        if result.ok:
            # Éxito: se refresca la caché y el perfil sale FRESH. Cada acierto en
            # operación normal es lo que puebla la caché para la próxima ventana
            # de indisponibilidad — de ahí que un TTL corto deje la caché vacía
            # justo cuando se necesita, que es el trade-off central de SP-3.
            cache.put(client_id, result.profile)
            return jsonify(
                client_id=client_id,
                profile=result.profile,
                profile_quality=FRESH,
            ), 200

        if cfg.is_baseline:
            # §3.4: el baseline no tiene tácticas y el fallo se propaga como
            # 502/504 hasta el socio. **Debe fallar**: es el control que
            # demuestra que el problema existe.
            status = 504 if result.outcome is Outcome.TIMEOUT else 502
            log.warning(
                "baseline propaga el fallo del proveedor",
                extra={"client_id": client_id, "outcome": result.outcome.value},
            )
            return jsonify(
                client_id=client_id,
                error="open finance no disponible",
                outcome=result.outcome.value,
            ), status

        # Camino de caché (§3.1). Se llega aquí por fallo, por timeout o porque
        # el circuito estaba abierto y cortó sin llamar.
        lookup = cache.get(client_id)

        if lookup.hit:
            # Dato real pero no actual: DEGRADED, incluso dentro del TTL. El
            # socio necesita saber que el precio se calculó con un perfil viejo.
            log.info(
                "perfil degradado desde caché",
                extra={
                    "client_id": client_id,
                    "outcome": result.outcome.value,
                    "cache_result": lookup.result.value,
                    "profile_age_s": round(lookup.age_s, 1),
                    "breaker_state": breaker.state,
                },
            )
            return jsonify(
                client_id=client_id,
                profile=lookup.profile,
                profile_quality=DEGRADED,
                profile_age_s=round(lookup.age_s, 3),
                cache_result=lookup.result.value,
            ), 200

        # Sin dato: factores conservadores fijos. Aquí es donde la disponibilidad
        # se sostiene y la precisión del pricing colapsa — el límite honesto de
        # la táctica que SP-3 debe cuantificar (ver OBSERVACIONES OBS-02).
        log.info(
            "perfil por defecto: ni proveedor ni caché",
            extra={
                "client_id": client_id,
                "outcome": result.outcome.value,
                "breaker_state": breaker.state,
            },
        )
        return jsonify(
            client_id=client_id,
            profile=None,
            profile_quality=DEFAULT,
            outcome=result.outcome.value,
        ), 200

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
