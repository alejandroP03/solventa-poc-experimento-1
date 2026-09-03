"""procesador-cotizacion (8083 / 8084) — consumidor de la cola de cotizaciones.

Dos instancias con el mismo código y distinto `ROLE` (PRIMARY / BACKUP) en modo
*single active consumer*: RabbitMQ entrega todos los mensajes a una sola y
**promueve automáticamente a la otra** cuando la activa se desconecta.

# TÁCTICA: reconfiguración / takeover PRIMARY->BACKUP — single active consumer
Esto implementa la táctica del modelo de despliegue de forma nativa, sin escribir
un coordinador. Combinado con ack manual, un procesador que muera a mitad de
trabajo provoca el re-encolado del mensaje y su reprocesamiento por el BACKUP, sin
pérdida.

UN SOLO WORKER DE GUNICORN
--------------------------
`SOLVENTA_SINGLE_PROCESS=true` en el Dockerfile. Con dos workers habría **dos
consumidores AMQP registrados por instancia**, y la semántica PRIMARY/BACKUP
dejaría de significar lo que dice el modelo: el "backup" ya estaría consumiendo
desde el principio en la mitad de los casos. Flask aquí solo sirve `/health/live`
y `/metrics`; el trabajo real ocurre en el hilo consumidor.
"""

from __future__ import annotations

import time

import requests
from flask import Flask, jsonify

from solventa_common import messaging, metrics
from solventa_common.app_factory import create_app, on_shutdown
from solventa_common.config import load_config
from solventa_common.correlation import bind_from_amqp, get_correlation_id
from solventa_common.http_client import HttpClient, PoolRejected
from solventa_common.logging import get_logger

log = get_logger("procesador-cotizacion")

DEFAULT_QUALITY = "DEFAULT"

# Presupuesto hacia el profiler. Debe superar OPENFINANCE_TIMEOUT_MS con margen
# para el trabajo propio del profiler (caché, breaker); si cortara antes, el
# procesador atribuiría a "profiler caído" lo que en realidad es el timeout de
# SP-1 haciendo su trabajo, y SP-1 dejaría de ser medible desde aquí.
PROFILER_TIMEOUT_MARGIN_S = 1.5


def create() -> Flask:
    cfg = load_config(service_name="procesador-cotizacion", default_port=8083)
    role = cfg.role or "PRIMARY"

    topology = messaging.Topology(
        exchange_quotes=cfg.exchange_quotes,
        queue_requests=cfg.queue_requests,
        exchange_events=cfg.exchange_events,
        single_active_consumer=cfg.consumer_mode == "single_active",
    )
    publisher = messaging.Publisher(cfg.rabbitmq_url, topology)

    profiler = HttpClient(
        cfg.profiler_url,
        timeout_s=cfg.openfinance_timeout_s + PROFILER_TIMEOUT_MARGIN_S,
    )

    def handle(properties, payload) -> None:  # noqa: ANN001
        """Procesa un ProfileRequest y publica la respuesta correlacionada."""
        started = time.perf_counter()
        # Reconstruye el contexto para que los logs de este servicio se puedan
        # cruzar con los del resto del journey (§3.5, función 2).
        correlation_id = bind_from_amqp(properties, payload)
        client_id = payload.get("client_id", "")

        quality, profile, profiler_s = _fetch_profile(profiler, client_id)

        metrics.queue_consumed_total.labels(
            queue=cfg.queue_requests, processor_role=role
        ).inc()

        reply_to = getattr(properties, "reply_to", None)
        if not reply_to:
            # Sin reply_to no hay a quién responder. Se ack-ea igualmente: dejarlo
            # en la cola lo reintentaría para siempre.
            log.error("ProfileRequest sin reply_to", extra={"client_id": client_id})
            return

        publisher.publish(
            # Exchange por defecto: el routing_key ES el nombre de la cola de
            # respuestas exclusiva que declaró esa instancia de cotizacion.
            exchange="",
            routing_key=reply_to,
            payload={
                "client_id": client_id,
                "profile": profile,
                "profile_quality": quality,
                "processed_by": role,
            },
            correlation_id=correlation_id,
            # Las réplicas no son persistentes: si el broker las perdiera, la
            # espera vencería por REPLY_TIMEOUT_MS y la cotización saldría
            # DEFAULT, que es justo el comportamiento previsto. Persistirlas
            # costaría un fsync por respuesta dentro de un presupuesto de 250 ms.
            persistent=False,
            queue_label="replies",
        )

        # `processor_handling` EXCLUYE la llamada al profiler, que ya es su
        # propia etapa. Anidarlas haría que la descomposición de §7.1 contara el
        # mismo tiempo dos veces y el sobrecosto del broker pareciera menor de lo
        # que es en proporción al total.
        metrics.observe_stage_value(
            "processor_handling", time.perf_counter() - started - profiler_s
        )

    consumer = messaging.Consumer(
        url=cfg.rabbitmq_url,
        queue=cfg.queue_requests,
        topology=topology,
        on_message=handle,
        # prefetch=1: este procesador solo retiene el mensaje que está
        # trabajando. Con un prefetch mayor, matar al PRIMARY re-encolaría un
        # lote entero y la medición del takeover incluiría trabajo que nunca
        # llegó a empezar.
        prefetch=1,
        auto_ack=False,
        name=f"procesador-{role.lower()}",
        # `solventa_processor_active` NO se fija aquí, y es deliberado. El callback
        # de conexión solo sabe que hay conexión AMQP, no que ESTA instancia sea
        # la consumidora activa: con single-active-consumer ambas están conectadas
        # y solo una recibe, así que fijarlo desde aquí daría 1 en las dos y el
        # takeover sería invisible en las métricas. Verificado durante la fase.
        #
        # Quién es la activa lo decide RabbitMQ y solo lo sabe el broker: lo lee el
        # health-manager del API de management, que es su responsabilidad en §2.2.
        transit_stage="queue_wait",
    )
    consumer.start()
    on_shutdown(consumer.stop)
    on_shutdown(publisher.close)

    def broker_ready() -> tuple[bool, str]:
        if consumer.connected:
            return True, f"conectado a {cfg.queue_requests} como {role}"
        return False, "sin conexión al broker"

    app = create_app(cfg, checks={"rabbitmq": broker_ready})

    @app.get("/status")
    def status():
        """Latido que observa el health-manager (§2.2)."""
        return jsonify(
            role=role,
            consumer_mode=cfg.consumer_mode,
            # `connected` dice que hay conexión AMQP, no que sea el consumidor
            # ACTIVO. Con single-active-consumer ambas instancias están
            # conectadas y solo una recibe: quién es la activa lo determina
            # RabbitMQ y lo lee el health-manager del API de management.
            connected=consumer.connected,
        ), 200

    return app


def _fetch_profile(profiler: HttpClient, client_id: str):
    """Pide el perfil al financial-profiler. Devuelve (calidad, perfil, segundos).

    Cualquier fallo se degrada a DEFAULT en lugar de propagarse: si este
    consumidor lanzara, el mensaje se re-encolaría y volvería a intentarse contra
    un profiler que sigue sin poder responder, gastando la capacidad del BACKUP
    en trabajo condenado a fallar.
    """
    started = time.perf_counter()
    try:
        response = profiler.post("/profile", json={"client_id": client_id})
    except (requests.RequestException, PoolRejected) as exc:
        elapsed = time.perf_counter() - started
        metrics.observe_stage_value("profiler_call", elapsed)
        log.warning(
            "profiler no disponible", extra={"client_id": client_id, "error": str(exc)}
        )
        return DEFAULT_QUALITY, None, elapsed

    elapsed = time.perf_counter() - started
    metrics.observe_stage_value("profiler_call", elapsed)

    if response.status_code != 200:
        return DEFAULT_QUALITY, None, elapsed

    body = response.json()
    return body.get("profile_quality", DEFAULT_QUALITY), body.get("profile"), elapsed


app = create()
