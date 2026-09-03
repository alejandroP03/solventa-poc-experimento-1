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

from solventa_common import messaging, metrics
from solventa_common.app_factory import create_app, on_shutdown
from solventa_common.config import load_config
from solventa_common.correlation import get_correlation_id
from solventa_common.http_client import BoundedPool
from solventa_common.logging import get_logger

from .pricing import compute_premium
from .profiling import (
    DEFAULT,
    BaselineResolver,
    ProfileOutcome,
    ProviderClient,
    TreatmentResolver,
)
from .replies import ReplyRegistry, instance_id, reply_queue_name

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

    registry: ReplyRegistry | None = None
    reply_consumer: messaging.Consumer | None = None

    if cfg.is_baseline:
        # §3.4: baseline es cadena bloqueante, sin cola.
        resolver = BaselineResolver(cfg)
    else:
        registry = ReplyRegistry(cfg.reply_timeout_s)
        topology = messaging.Topology(
            exchange_quotes=cfg.exchange_quotes,
            queue_requests=cfg.queue_requests,
            exchange_events=cfg.exchange_events,
            single_active_consumer=cfg.consumer_mode == "single_active",
        )
        publisher = messaging.Publisher(cfg.rabbitmq_url, topology)

        # Cola de respuestas exclusiva de ESTE proceso (§3.2). El nombre incluye
        # el PID: con dos workers, una cola compartida repartiría las réplicas
        # entre ambos y la mitad llegaría al proceso sin espera activa.
        # Ver services/cotizacion/app/replies.py.
        reply_queue = reply_queue_name()

        def on_reply(properties, payload) -> None:  # noqa: ANN001
            registry.resolve(properties.correlation_id or "", payload)

        reply_consumer = messaging.Consumer(
            url=cfg.rabbitmq_url,
            queue=reply_queue,
            topology=topology,
            on_message=on_reply,
            exclusive_queue=True,
            # auto_ack: una réplica no vale la pena reprocesarla. Si este proceso
            # muere, la espera que la aguardaba murió con él.
            auto_ack=True,
            # Sin prefetch=1: las réplicas se resuelven en microsegundos y
            # limitarlas a una en vuelo añadiría latencia a broker_reply, que es
            # una de las tres etapas que §7.1 mide.
            prefetch=0,
            name="reply-consumer",
            transit_stage="broker_reply",
        )
        reply_consumer.start()
        on_shutdown(reply_consumer.stop)
        on_shutdown(publisher.close)

        # TÁCTICA: bulkhead — SP-5 (pool C: esperas de réplica concurrentes)
        pending_pool = BoundedPool(
            "pending_replies",
            cfg.pool_pending_replies_max,
            enabled=cfg.tactic_enabled("bulkhead"),
            # Rechazo inmediato: esperar por un slot para luego esperar la
            # réplica retendría el hilo de Gunicorn el doble de tiempo, que es
            # exactamente el recurso que este bulkhead protege.
            acquire_timeout_s=0.0,
        )
        resolver = TreatmentResolver(cfg, publisher, registry, reply_queue, pending_pool)

    def broker_ready() -> tuple[bool, str]:
        if cfg.is_baseline:
            return True, "no aplica en baseline"
        if reply_consumer is not None and reply_consumer.connected:
            return True, f"consumiendo {reply_queue_name()}"
        return False, "cola de respuestas no conectada"

    app = create_app(cfg, checks={"rabbitmq": broker_ready})

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

        outcome = _resolve_profile(resolver, client_id, cfg.is_baseline)

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


def _resolve_profile(resolver, client_id: str, is_baseline: bool) -> ProfileOutcome:  # noqa: ANN001
    """Resuelve el perfil midiendo la etapa que corresponda.

    Las etapas de §7.1 deben **sumar** el journey, así que no pueden anidarse. En
    `baseline` la llamada al profiler ocurre aquí y esta es la única que la mide.
    En `treatment` el trabajo se reparte entre `broker_publish`, `queue_wait`,
    `processor_handling` y `broker_reply`, que ya lo cubren: envolver además la
    espera completa contaría el mismo tiempo dos veces e inflaría la
    descomposición justo en la fila que decide si el diseño cabe en 250 ms.

    Cualquier excepción inesperada se degrada a DEFAULT en lugar de propagarse:
    el invariante de §3.1 no admite que un error de programación se convierta en
    el 5xx que el ASR prohíbe. Se registra en el log para que no pase inadvertido.
    """
    try:
        if is_baseline:
            with metrics.observe_stage("profiler_call"):
                return resolver.resolve(client_id)
        return resolver.resolve(client_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("fallo inesperado resolviendo el perfil", extra={"client_id": client_id})
        return ProfileOutcome(DEFAULT, upstream_error=str(exc), upstream_status=502)


app = create()
