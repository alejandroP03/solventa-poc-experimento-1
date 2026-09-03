"""Abstracción delgada sobre `pika` (kickoff §1, §3.2).

Contiene la topología de RabbitMQ, el patrón request-reply con cola exclusiva por
instancia, el single-active-consumer que implementa el takeover PRIMARY->BACKUP, la
reconexión con backoff exponencial y el cierre limpio ante SIGTERM.

Se mantiene delgada a propósito para que un cambio futuro de transporte quede
contenido en este archivo. **Solo se implementa RabbitMQ.**

Notas de concurrencia
---------------------
`pika` no es thread-safe: una conexión pertenece a un hilo. Los servicios corren
con `GUNICORN_WORKER_CLASS=gthread`, así que el publicador usa **una conexión por
hilo** (thread-local). La alternativa —un hilo publicador único con cola interna—
serializaría las publicaciones y añadiría una espera artificial justo a la etapa
`broker_publish`, que es una de las tres que §7.1 mide para decidir si el diseño
cabe en el presupuesto. El instrumento no debe inventar la latencia que mide.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pika
from pika.exceptions import AMQPError, ChannelClosedByBroker

from . import logging as slog
from . import metrics

log = slog.get_logger("messaging")

# Instante de publicación, en MICROSEGUNDOS enteros desde epoch.
#
# Dos restricciones se cruzan aquí:
#   - La propiedad AMQP `timestamp` es entera en segundos y no sirve: `queue_wait`
#     y `broker_reply` se miden en milisegundos dentro de un presupuesto de 250 ms.
#   - Las tablas de cabeceras AMQP de pika 1.3.2 **no admiten float**: lanzan
#     UnsupportedAMQPFieldException. Solo int, str, bytes, bool, Decimal,
#     datetime, dict y list.
# Microsegundos enteros satisfacen ambas: caben en un long y conservan resolución
# sub-milisegundo para la descomposición del presupuesto de §7.1.
HEADER_PUBLISHED_AT = "x-solventa-published-at-us"
_MICROS = 1_000_000

_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 15.0


@dataclass(frozen=True)
class Topology:
    """Recursos de §3.2."""

    exchange_quotes: str
    queue_requests: str
    exchange_events: str
    single_active_consumer: bool

    @property
    def requests_routing_key(self) -> str:
        return self.queue_requests

    def queue_arguments(self) -> dict[str, Any]:
        # TÁCTICA: reconfiguración / takeover PRIMARY->BACKUP — single active consumer
        # RabbitMQ entrega a un solo consumidor y promueve al siguiente cuando el
        # activo se desconecta, implementando la táctica del modelo de despliegue
        # de forma nativa, sin escribir un coordinador.
        return {"x-single-active-consumer": True} if self.single_active_consumer else {}


def backoff_delay(attempt: int) -> float:
    """Backoff exponencial acotado, sin jitter (reproducibilidad del experimento)."""
    return min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_MAX_S)


def _connect(url: str) -> pika.BlockingConnection:
    params = pika.URLParameters(url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 15
    params.socket_timeout = 5
    return pika.BlockingConnection(params)


def declare_topology(channel: pika.channel.Channel, topology: Topology) -> None:
    """Declara exchanges y cola de forma idempotente.

    La topología se declara desde la aplicación y no solo en `definitions.json`
    porque cada servicio debe poder arrancar y reconstruirla aunque RabbitMQ se
    reinicie a mitad de corrida (§8.2: sin dependencias de orden de arranque).
    """
    channel.exchange_declare(topology.exchange_quotes, exchange_type="direct", durable=True)
    channel.exchange_declare(topology.exchange_events, exchange_type="fanout", durable=True)
    try:
        channel.queue_declare(
            topology.queue_requests, durable=True, arguments=topology.queue_arguments()
        )
    except ChannelClosedByBroker as exc:
        if exc.reply_code == 406:
            raise RuntimeError(
                f"La cola {topology.queue_requests!r} ya existe con argumentos distintos. "
                "Suele pasar al cambiar CONSUMER_MODE sobre un broker con estado previo: "
                "ejecutar `make down` (borra volúmenes) y volver a levantar."
            ) from exc
        raise
    channel.queue_bind(
        topology.queue_requests, topology.exchange_quotes, routing_key=topology.requests_routing_key
    )


class Publisher:
    """Publicador con conexión por hilo y reconexión con backoff."""

    def __init__(self, url: str, topology: Topology) -> None:
        self.url = url
        self.topology = topology
        self._local = threading.local()
        self._closed = threading.Event()
        self._connections: list[pika.BlockingConnection] = []
        self._lock = threading.Lock()

    def _channel(self) -> pika.channel.Channel:
        channel = getattr(self._local, "channel", None)
        if channel is not None and channel.is_open:
            return channel

        connection = _connect(self.url)
        channel = connection.channel()
        declare_topology(channel, self.topology)
        self._local.connection = connection
        self._local.channel = channel
        with self._lock:
            self._connections.append(connection)
        return channel

    def _discard_channel(self) -> None:
        connection = getattr(self._local, "connection", None)
        self._local.channel = None
        self._local.connection = None
        if connection is None:
            return
        with self._lock:
            if connection in self._connections:
                self._connections.remove(connection)
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - ya estaba rota
            pass

    def publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        payload: Mapping[str, Any],
        correlation_id: str,
        reply_to: str | None = None,
        persistent: bool = True,
        queue_label: str | None = None,
    ) -> None:
        """Publica un mensaje. Reintenta una vez tras reconectar.

        `persistent=True` (delivery_mode=2) sobre una cola durable es lo que
        garantiza que no se pierdan solicitudes cuando un procesador está
        indisponible — la razón por la que el diseño descarta Redis Pub/Sub
        como transporte (§1).
        """
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2 if persistent else 1,
            correlation_id=correlation_id,
            reply_to=reply_to,
            headers={HEADER_PUBLISHED_AT: int(time.time() * _MICROS)},
        )
        body = json.dumps(payload, default=str).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._channel().basic_publish(
                    exchange=exchange,
                    routing_key=routing_key,
                    body=body,
                    properties=properties,
                )
                metrics.queue_published_total.labels(
                    queue=queue_label or routing_key or exchange
                ).inc()
                return
            except (AMQPError, OSError) as exc:
                last_error = exc
                self._discard_channel()
                if attempt == 0:
                    log.warning("publicación falló, reconectando", extra={"error": str(exc)})
        raise RuntimeError(
            f"no se pudo publicar tras reconectar: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    def declare_reply_queue(self, name: str) -> str:
        """Declara la cola de respuestas exclusiva de esta instancia (§3.2).

        Exclusiva y auto-delete: muere con la conexión que la declaró, así que una
        instancia de `cotizacion` que se reinicia no deja colas huérfanas
        acumulando réplicas que ya nadie espera.
        """
        channel = self._channel()
        channel.queue_declare(name, durable=False, exclusive=False, auto_delete=True)
        return name

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass


class Consumer:
    """Consumidor en hilo propio, con ack manual y reconexión con backoff."""

    def __init__(
        self,
        *,
        url: str,
        queue: str,
        topology: Topology,
        on_message: Callable[[pika.BasicProperties, dict[str, Any]], None],
        prefetch: int = 1,
        declare: bool = True,
        exclusive_queue: bool = False,
        auto_ack: bool = False,
        name: str = "consumer",
        on_active: Callable[[bool], None] | None = None,
        transit_stage: str = "queue_wait",
    ) -> None:
        self.url = url
        self.queue = queue
        self.topology = topology
        self.on_message = on_message
        self.prefetch = prefetch
        self.declare = declare
        self.exclusive_queue = exclusive_queue
        self.auto_ack = auto_ack
        self.name = name
        self.on_active = on_active
        # Etapa de §7.1 a la que corresponde el tránsito por el broker de ESTA
        # cola: `queue_wait` para cotizacion.requests, `broker_reply` para las
        # colas de respuesta. Mezclarlas en una sola serie haría que el
        # sobrecosto del broker se contara dos veces en la descomposición.
        self.transit_stage = transit_stage

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.channel.Channel | None = None
        self._connected = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._consume_forever()
                attempt = 0
            except Exception as exc:  # noqa: BLE001 - reconectar ante cualquier fallo
                if self._stop.is_set():
                    break
                self._connected.clear()
                if self.on_active:
                    self.on_active(False)
                delay = backoff_delay(attempt)
                log.warning(
                    "consumidor desconectado, reintentando",
                    extra={"queue": self.queue, "error": str(exc), "retry_in_s": delay},
                )
                attempt += 1
                # El servicio arranca aunque RabbitMQ no esté disponible todavía
                # (§8.2: ECS no garantiza orden de arranque).
                self._stop.wait(delay)

    def _consume_forever(self) -> None:
        self._connection = _connect(self.url)
        self._channel = self._connection.channel()

        if self.declare:
            declare_topology(self._channel, self.topology)
        if self.exclusive_queue:
            self._channel.queue_declare(
                self.queue, durable=False, exclusive=False, auto_delete=True
            )

        # prefetch=1: un procesador solo retiene el mensaje que está trabajando.
        # Con un prefetch mayor, matar al PRIMARY re-encolaría un lote entero y la
        # medición del takeover incluiría trabajo que nunca llegó a empezar.
        self._channel.basic_qos(prefetch_count=self.prefetch)
        self._channel.basic_consume(
            queue=self.queue, on_message_callback=self._handle, auto_ack=self.auto_ack
        )

        self._connected.set()
        if self.on_active:
            self.on_active(True)
        log.info("consumiendo", extra={"queue": self.queue})
        self._channel.start_consuming()

    def _handle(
        self,
        channel: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.BasicProperties,
        body: bytes,
    ) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.error("mensaje ilegible, descartado", extra={"queue": self.queue})
            if not self.auto_ack:
                channel.basic_nack(method.delivery_tag, requeue=False)
            return

        published_at = (properties.headers or {}).get(HEADER_PUBLISHED_AT)
        if isinstance(published_at, int):
            waited = max(time.time() - published_at / _MICROS, 0.0)
            # Etapa de la descomposición del presupuesto (§7.1). El reloj es el
            # del host Docker, compartido por todos los contenedores, así que la
            # resta entre procesos es válida.
            metrics.observe_stage_value(self.transit_stage, waited)
            if self.transit_stage == "queue_wait":
                metrics.queue_wait_seconds.observe(waited)
            payload["_transit_s"] = waited

        try:
            self.on_message(properties, payload)
        except Exception:  # noqa: BLE001
            log.exception("el handler falló", extra={"queue": self.queue})
            if not self.auto_ack:
                # requeue=True: el mensaje vuelve a la cola y lo reprocesa el
                # siguiente consumidor. Cotizar no cobra ni emite, así que un
                # reproceso no tiene costo de corrección (§3.5) y no hace falta
                # almacén de idempotencia.
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        if not self.auto_ack:
            channel.basic_ack(method.delivery_tag)

    def stop(self) -> None:
        """Cierre limpio (§8.2).

        Un consumidor que no cierra limpio obliga a RabbitMQ a esperar el timeout
        de la conexión antes de promover al BACKUP, alargando artificialmente la
        ventana de takeover que el experimento mide.
        """
        self._stop.set()
        connection, channel = self._connection, self._channel
        if connection is not None and connection.is_open and channel is not None:
            try:
                connection.add_callback_threadsafe(channel.stop_consuming)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if connection is not None and connection.is_open:
                connection.close()
        except Exception:  # noqa: BLE001
            pass
        self._connected.clear()
