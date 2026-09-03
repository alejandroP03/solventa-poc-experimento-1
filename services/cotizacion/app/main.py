"""cotizacion (8082) — orquestador del journey.

Compone el precio final a partir de dos fuentes independientes: el perfil
financiero (que depende de Open Finance) y el precio del socio (que no depende de
nada externo). Esa independencia es lo que SP-5 mide.

INVARIANTE DURO (§3.1)
----------------------
En modo `treatment`, este servicio **nunca** devuelve 5xx por causa de Open Finance
ni por vencimiento del presupuesto de espera. Todo fallo aguas abajo se traduce en
una degradación del campo `profile_quality`, jamás en un código de error. Es el
ASR-Disp-09 expresado en código, y está implementado en un único punto —
`_resolve_profile`— para que no haya un segundo camino por donde se escape un 500.

En modo `baseline` ocurre lo contrario a propósito: el fallo se propaga.
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from solventa_common import metrics
from solventa_common.app_factory import create_app
from solventa_common.config import load_config
from solventa_common.correlation import get_correlation_id
from solventa_common.http_client import BoundedPool
from solventa_common.logging import get_logger

from .pricing import compute_premium
from .profiling import DEFAULT, BaselineResolver, ProfileOutcome, ProviderClient

log = get_logger("cotizacion")

REQUIRED_FIELDS = ("client_id", "product_code")


def create() -> Flask:
    cfg = load_config(service_name="cotizacion", default_port=8082)

    # TÁCTICA: bulkhead — SP-5 (pool aislado A, el control)
    provider_pool = BoundedPool(
        "provider",
        cfg.pool_provider_max,
        enabled=cfg.tactic_enabled("bulkhead"),
        # La ruta Provider tolera una espera corta: es rápida y su rechazo
        # prematuro empeoraría el p95 que SP-5 usa como línea base.
        acquire_timeout_s=0.25,
    )
    provider = ProviderClient(cfg, provider_pool)

    # La ruta por cola (request-reply sobre RabbitMQ) llega en la fase del
    # broker. Hasta entonces ambos modos usan la cadena síncrona: `treatment` aún
    # no se diferencia de `baseline` salvo en que no propaga el 5xx.
    resolver = BaselineResolver(cfg)

    app = create_app(cfg)

    @app.post("/quotes")
    def quote():
        payload = request.get_json(silent=True) or {}
        missing = [f for f in REQUIRED_FIELDS if not payload.get(f)]
        if missing:
            # 400 y no 5xx: una petición mal formada es culpa del socio y no
            # cuenta contra el ASR, que mide indisponibilidad del sistema.
            return jsonify(error=f"faltan campos: {', '.join(missing)}"), 400

        client_id = payload["client_id"]
        product_code = payload["product_code"]
        age = int(payload.get("age", 35))
        insured_amount = float(payload.get("insured_amount", 1_000_000))

        outcome = _resolve_profile(resolver, client_id)

        # Único punto donde un fallo aguas abajo puede convertirse en 5xx, y solo
        # en baseline. En treatment esta rama es inalcanzable por construcción.
        if outcome.upstream_error and cfg.is_baseline:
            log.error(
                "baseline propaga el fallo del proveedor",
                extra={"client_id": client_id, "detail": outcome.upstream_error},
            )
            return jsonify(
                error="no fue posible cotizar",
                detail=outcome.upstream_error,
                correlation_id=get_correlation_id(),
            ), outcome.upstream_status or 502

        provider_price = provider.price(product_code)

        with metrics.observe_stage("compose"):
            pricing = compute_premium(
                product_code=product_code,
                age=age,
                insured_amount=insured_amount,
                profile=outcome.profile,
            )
            total = round(pricing["premium"] + (provider_price or 0.0), 2)

        metrics.quotes_total.labels(profile_quality=outcome.quality).inc()

        return jsonify(
            quote={
                "premium": pricing["premium"],
                "provider_price": provider_price,
                "total": total,
                "currency": "COP",
                "factors": pricing["factors"],
            },
            client_id=client_id,
            product_code=product_code,
            profile_quality=outcome.quality,
            # Marca explícita de que el precio es preliminar (§10): el socio debe
            # saber que se tarificó sin información del cliente.
            preliminary=outcome.quality == DEFAULT,
            correlation_id=get_correlation_id(),
        ), 200

    @app.get("/provider-quote")
    def provider_quote():
        """Serie de control de SP-5: solo la ruta Provider.

        No toca el perfilamiento —ni Open Finance, ni la cola, ni el profiler—
        pero **sí comparte los hilos de Gunicorn de este servicio** con la ruta
        completa. Esa es la razón de que exista aquí y no como una llamada
        directa a `socio-distribucion`: golpear el otro contenedor medería el
        aislamiento por separación de procesos, que es gratuito y siempre
        perfecto, en lugar del aislamiento por bulkhead, que es la variable
        independiente de SP-5.

        Su p95 durante la saturación, contra su p95 en línea base sana, es la
        métrica de decisión del sub-experimento.
        """
        product_code = request.args.get("product_code", "VIAJE")
        price = provider.price(product_code)

        if price is None:
            # Ni siquiera aquí se devuelve 5xx: el pool de Provider lleno o el
            # socio caído son degradación, no indisponibilidad del sistema.
            return jsonify(
                product_code=product_code,
                provider_price=None,
                degraded=True,
                correlation_id=get_correlation_id(),
            ), 200

        return jsonify(
            product_code=product_code,
            provider_price=price,
            currency="COP",
            degraded=False,
            correlation_id=get_correlation_id(),
        ), 200

    return app


def _resolve_profile(resolver, client_id: str) -> ProfileOutcome:  # noqa: ANN001
    """Resuelve el perfil midiendo la etapa.

    Cualquier excepción inesperada se degrada a DEFAULT en lugar de propagarse:
    el invariante de §3.1 no admite que un error de programación se convierta en
    el 5xx que el ASR prohíbe. Se registra en el log para que no pase inadvertido.
    """
    try:
        with metrics.observe_stage("profiler_call"):
            return resolver.resolve(client_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("fallo inesperado resolviendo el perfil", extra={"client_id": client_id})
        return ProfileOutcome(DEFAULT, upstream_error=str(exc), upstream_status=502)


app = create()
