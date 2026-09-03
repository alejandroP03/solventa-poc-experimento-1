# Prompt para Claude Code — POC Solventa / Experimento 1

> **Cómo usar este documento:** pégalo completo como primer mensaje en una sesión de Claude Code, dentro de un directorio vacío. Está escrito como instrucción directa al agente.
>
> **Versión 3.** Implementa la arquitectura **tal como está definida en los modelos del Cuaderno IV** (vistas funcional, de despliegue, de información y de concurrencia). El objetivo del POC es **validar esa arquitectura as-is**, no proponer una alternativa. Broker: RabbitMQ.

---

## 0. Contexto y objetivo

Vas a construir un **prototipo ejecutable (POC) de arquitectura de microservicios** para un proyecto académico de la Universidad de los Andes (MISW4202 — Arquitecturas Ágiles de Software). El sistema ficticio se llama **Solventa**: una aseguradora digital que vende seguros embebidos en canales de terceros.

**Este NO es un producto.** Es un instrumento de medición. Su propósito es **resolver cinco puntos de sensibilidad de diseño con evidencia cuantitativa** sobre una arquitectura ya diseñada. Optimiza por: fidelidad al diseño, reproducibilidad, observabilidad y parametrización. NO optimices por completitud funcional ni realismo de negocio.

**Restricción de fidelidad:** el diseño de §3 está fijado por los modelos de arquitectura ya entregados por el equipo. **No lo rediseñes.** Si detectas una debilidad en él, no la corrijas en el código: repórtala como observación y, cuando sea medible, exponla como métrica. Un hallazgo negativo medido es un resultado válido y valioso del experimento; una corrección silenciosa destruye la validez de la medición.

### El experimento a soportar

**Título:** Graceful degradation de la cotización embebida ante indisponibilidad del proveedor de Open Finance.

**ASR asociado (ASR-Disp-09):** *Como socio de distribución embebido, cuando solicito una cotización de póliza por medio del API, dado que el sistema externo de Open Finance está indisponible, quiero que el sistema me siga devolviendo una cotización aunque sea con información degradada, para que mis clientes puedan ver el precio completo sin interrupción. Esto debe suceder con 0 % de solicitudes con error 5xx durante el evento de indisponibilidad.*

**Punto de sensibilidad raíz:** el acoplamiento temporal del perfilamiento con una dependencia externa no controlable. La decisión de diseño es **dónde se corta la propagación del fallo**, y pequeñas variaciones en los parámetros que gobiernan ese corte producen efectos desproporcionados sobre disponibilidad, latencia y precisión del pricing.

De ahí se derivan cinco puntos de sensibilidad concretos, que son **la razón de ser del código que vas a escribir**:

| ID | Punto de sensibilidad |
|---|---|
| **SP-1** | Valor del timeout de Perfilamiento hacia Open Finance |
| **SP-2** | Umbral de apertura y ventana half-open del circuit breaker |
| **SP-3** | TTL de la caché de perfil y comportamiento ante caché fría |
| **SP-4** | Fuente de verdad de la detección: señal del Monitor vs. conteo de fallos del breaker |
| **SP-5** | Aislamiento de recursos: que la saturación del perfilamiento no degrade la ruta Provider |

**Regla rectora:** todo parámetro configurable debe ser la variable independiente de algún SP, o una variable de control que ese SP necesita fijar. **Si un parámetro no sirve a ningún SP, hardcodéalo con un valor razonable.** La proliferación de flags sin propósito experimental es ruido, no flexibilidad.

---

## 1. Restricciones no negociables

| Restricción | Detalle |
|---|---|
| Lenguaje | Python 3.11 exclusivamente |
| Framework web | Flask, servido con Gunicorn |
| Estilo | Microservicios — un contenedor y un `Dockerfile` por servicio, desplegables de forma independiente |
| **Broker** | **RabbitMQ 3.13** con `pika`. Puertos 5672 (AMQP), 15672 (management), 15692 (Prometheus) |
| Caché | Redis (`redis-py`) — **exclusivamente como caché de perfil, nunca como broker** |
| Persistencia | PostgreSQL 16 (`SQLAlchemy`) |
| Circuit breaker | `pybreaker` |
| Métricas | `prometheus-client` + Prometheus + Grafana |
| Carga | k6 (contenedor `grafana/k6`) |
| Orquestación local | Docker Compose |
| Despliegue | AWS — el diseño debe facilitarlo (§8) |

**Sobre la elección del broker.** Redis queda descartado como transporte por dos razones que afectan la validez del experimento: Redis Pub/Sub no ofrece durabilidad y el diseño exige explícitamente no perder solicitudes cuando un procesador está indisponible; y compartir la instancia de Redis entre la caché de perfil y la mensajería crea un dominio de fallo común que **confunde la medición de SP-5**, cuyo objeto es precisamente el aislamiento de recursos. Redis se mantiene, con un único rol: caché.

Mantén `libs/solventa_common/messaging.py` como una abstracción delgada sobre `pika`, de modo que un cambio futuro de transporte quede contenido. **Implementa solo RabbitMQ.**

---

## 2. Alcance

### 2.1 Tier 1 — imprescindible

| Servicio | Puerto | Responsabilidad |
|---|---|---|
| `api-gateway` | 8080 | Punto de entrada del socio. Genera el `X-Correlation-Id` y lo propaga. **Frontera de medición del 5xx.** Sin auth real. |
| `socio-distribucion` | 8081 | Simula el canal del socio embebido. Expone la interfaz `Provider` (precio del servicio propio del socio). Es la **ruta no afectada** que mide SP-5. |
| `cotizacion` | 8082 | Orquestador del journey. Publica `ProfileRequest` en la cola y espera la respuesta correlacionada con presupuesto acotado. Compone el precio final. |
| `procesador-cotizacion` | 8083 / 8084 | Consumidor de la cola de cotizaciones. Dos instancias, `PRIMARY` y `BACKUP` (mismo código, `ROLE` por env), en modo *single active consumer*. Invoca a `financial-profiler` y publica la respuesta. |
| `financial-profiler` | 8085 | **Componente bajo prueba.** Envuelve la llamada a Open Finance con timeout + circuit breaker. Ante circuito abierto o fallo, resuelve desde la caché de perfil. Expone el endpoint de señalización que usa el Monitor. |
| `monitor` | 8086 | Ping–Echo periódico contra `mock-openfinance`. Al detectar N fallos consecutivos, **señaliza al circuit breaker** vía HTTP. Es una de las dos fuentes de verdad de SP-4. |
| `mock-openfinance` | 8090 | Proveedor externo simulado con inyección de fallos en caliente (§4). |
| `redis` | 6379 | Caché de perfil con TTL y ventana de gracia. |
| `rabbitmq` | 5672 / 15672 / 15692 | `CotizacionQueue`. Habilita los plugins `rabbitmq_management` y `rabbitmq_prometheus`. |
| `prometheus` + `grafana` | 9090 / 3000 | Observabilidad. Dashboards provisionados como código. |

### 2.2 Tier 2 — versión mínima

| Servicio | Puerto | Responsabilidad |
|---|---|---|
| `health-manager` | 8087 | Reconfigurador. Observa los latidos de los procesadores, registra los eventos de takeover PRIMARY→BACKUP y los publica en el exchange de eventos. |
| `notificador` | 8088 | Suscriptor del exchange de eventos. No envía nada real: log estructurado, contadores y `GET /events`. |
| `postgres` | 5432 | Catálogo de productos y tarifas base + bitácora append-only de cotizaciones (trazabilidad actuarial). |

### 2.3 Explícitamente FUERA del alcance

No lo construyas; stubéalo y documéntalo:

- Los otros 5 journeys y los demás microservicios del catálogo.
- Autenticación, autorización, mTLS, gestión de consentimiento. El gateway acepta `X-Partner-Id` y lo propaga; nada más.
- **Idempotencia y almacén de deduplicación.** Ver §3.5: el propio registro de correlación descarta las réplicas duplicadas, y cotizar no tiene efectos secundarios que deduplicar. No implementes un almacén de idempotencia.
- Clientes web y móvil. Ninguna UI salvo Grafana.
- Lógica actuarial real.
- Multi-región, multi-AZ, RTO/RPO, failover de infraestructura.
- CI/CD, Terraform, CDK.

---

## 3. Diseño del flujo (as-is, según los modelos del equipo)

### 3.1 Journey de cotización — modo `treatment`

```
Socio → api-gateway   [genera X-Correlation-Id]
      → cotizacion
         │
         ├─ HTTP sync → socio-distribucion (Provider)        [pool aislado A — SP-5]
         │
         └─ publica ProfileRequest en cotizacion.requests    [slot de espera — pool C, SP-5]
            (correlation_id + reply_to)
                      ↓
            procesador-cotizacion (PRIMARY, o BACKUP tras takeover)
                      ↓
            HTTP → financial-profiler                        [pool aislado B — SP-5]
                   │
                   ├─ breaker CERRADO → HTTP a mock-openfinance con OPENFINANCE_TIMEOUT_MS
                   │        ├─ éxito         → escribe Redis → perfil FRESH
                   │        └─ fallo/timeout → registra el fallo en el breaker → camino de caché
                   │
                   └─ breaker ABIERTO → lee Redis directamente, sin llamada externa
                            ├─ hit dentro de TTL + grace → perfil DEGRADED
                            └─ miss                      → perfil DEFAULT
                      ↓
            publica ProfileResponse en la reply_to con el mismo correlation_id
                      ↓
         cotizacion despierta la espera correlacionada, o vence REPLY_TIMEOUT_MS
         → precio = tarifa_base × f(perfil) + precio_socio
         → 200 { quote, profile_quality: FRESH|DEGRADED|DEFAULT, correlation_id }
```

**Invariante duro:** `cotizacion` **nunca** devuelve 5xx por causa de Open Finance ni por vencimiento del presupuesto de espera. Todo fallo aguas abajo se traduce en una degradación del campo `profile_quality`, jamás en un código de error. Si vence `REPLY_TIMEOUT_MS`, responde 200 con `profile_quality: DEFAULT` e incrementa `solventa_reply_timeout_total`.

### 3.2 Topología de RabbitMQ

| Recurso | Tipo | Configuración |
|---|---|---|
| `solventa.quotes` | Exchange direct | Durable |
| `cotizacion.requests` | Cola | Durable, mensajes persistentes, `x-single-active-consumer: true`, ack manual |
| `cotizacion.replies.<instance_id>` | Cola | Exclusiva, auto-delete, una por instancia de `cotizacion`, declarada al arrancar |
| `solventa.events` | Exchange fanout | Durable. Productores: `health-manager`. Consumidores: `notificador` y cualquier futuro |

**Request-reply.** Cada instancia de `cotizacion` declara su propia cola de respuestas exclusiva al arrancar y la consume en un hilo dedicado. Al publicar un `ProfileRequest` fija las propiedades AMQP `correlation_id` y `reply_to`. El procesador responde publicando a esa `reply_to` con el mismo `correlation_id`.

No uses `amq.rabbitmq.reply-to` (direct reply-to): obliga a publicar y consumir sobre el mismo canal, lo que se lleva mal con Gunicorn multihilo. La cola exclusiva por instancia es más simple de razonar y más robusta aquí.

**Registro de espera en `cotizacion`.** Un diccionario en memoria `{correlation_id: (threading.Event, slot_resultado)}` protegido por lock, con purga por TTL de las entradas vencidas. Las respuestas que lleguen con un `correlation_id` que ya no esté en el registro **se descartan silenciosamente** e incrementan `solventa_orphan_reply_total`.

**Single active consumer.** RabbitMQ entrega todos los mensajes a un único consumidor de `cotizacion.requests` y **promueve automáticamente al siguiente** cuando el activo se desconecta. Esto implementa la táctica de reconfiguración/takeover PRIMARY→BACKUP del modelo de despliegue de forma nativa, sin escribir un coordinador. Combinado con ack manual, un procesador que muera a mitad de trabajo provoca el re-encolado del mensaje y su reprocesamiento por el BACKUP, sin pérdida.

**Variante medible.** `CONSUMER_MODE` ∈ {`single_active`, `competing`}. El default es `single_active` porque es lo que fija el modelo. `competing` desactiva el flag y deja que ambos procesadores consuman en paralelo; sirve para contrastar capacidad de absorción de picos contra semántica de failover. Documenta la diferencia; no cambies el default.

**Reconexión.** Todo productor y consumidor debe reconectarse con backoff exponencial y arrancar aunque RabbitMQ no esté disponible todavía.

### 3.3 Señalización del Monitor

Fiel al modelo de conectores: el `monitor` hace health checks Ping–Echo por HTTP contra `mock-openfinance` y, al cruzar el umbral de fallos, **notifica al circuit breaker por HTTP** con `POST /internal/dependency-health` sobre `financial-profiler`:

```
POST /internal/dependency-health
  { "dependency": "openfinance", "state": "down" | "up", "observed_at": "<iso8601>" }
```

Regla de diseño a respetar: **la señal del Monitor solo puede forzar la apertura del circuito, nunca su cierre.** El cierre queda siempre en manos de la lógica half-open del breaker, que se apoya en tráfico real. Un Monitor que cerrara el circuito reabriría la propagación del fallo basándose en un endpoint que puede mentir (§4).

Con `MONITOR_SIGNAL_ENABLED=false` el Monitor sigue midiendo y exportando métricas pero no envía la señal, dejando solo la detección reactiva del breaker. Esa es la variable independiente de SP-4.

### 3.4 Modos de ejecución (`QUOTE_MODE`)

| Modo | Descripción |
|---|---|
| `baseline` | Sin caché, sin breaker, sin cola, sin bulkhead. `cotizacion` → `financial-profiler` → `mock-openfinance` en cadena bloqueante con timeout de 30 s. El fallo del proveedor se propaga como 502/504 al socio. **Este modo debe fallar** — es el control que demuestra que el problema existe. |
| `treatment` | Flujo completo de §3.1. **Default.** |

Los flags de ablación (`BREAKER_ENABLED`, `CACHE_ENABLED`, `MONITOR_SIGNAL_ENABLED`, `BULKHEAD_ENABLED`) solo aplican en `treatment` y existen exclusivamente porque son variables independientes de SP-2 a SP-5. No agregues flags adicionales.

### 3.5 Correlation ID — alcance de su uso

El `X-Correlation-Id` (UUID4, generado en el gateway) cumple **dos funciones y ninguna más**:

1. **Mecanismo del patrón request-reply**: casar cada `ProfileResponse` con la espera activa que la aguarda en `cotizacion`.
2. **Trazabilidad diagnóstica**: aparecer en cada línea de log estructurado de todos los servicios, para poder reconstruir por qué una cotización concreta salió `DEGRADED` durante la ventana de fallo. Es evidencia para el informe.

**No lo uses** como clave de idempotencia, ni para deduplicar, ni para ninguna decisión de negocio. Si un mensaje se reprocesa tras un re-encolado y llega una segunda respuesta, el registro de correlación ya no tiene la espera activa y la descarta: la deduplicación es un efecto del patrón, no una responsabilidad que haya que implementar. Cotizar no cobra, no emite y no compromete capital; duplicar el cálculo no tiene costo de corrección.

---

## 4. Mock de Open Finance — el instrumento del experimento

Controlable **en caliente**, sin reiniciar contenedores.

**Endpoint de negocio:** `GET /openfinance/v1/profiles/{client_id}` → `{ client_id, income_band, debt_ratio, payment_behavior_score, stability_index, generated_at }`

**Endpoints de control:**
```
POST /admin/mode
  { "mode": "normal" | "slow" | "error_5xx" | "timeout" | "flaky",
    "latency_ms": 1500,      # para slow
    "failure_rate": 0.5,     # para flaky
    "duration_s": 120 }      # opcional: auto-revierte a normal
GET  /admin/state
POST /admin/reset
GET  /health
```

| Modo | `/openfinance/v1/profiles` | `/health` |
|---|---|---|
| `normal` | 200 en 40–90 ms con jitter | 200 |
| `slow` | 200 en `latency_ms` ± 15 % | **200 rápido** |
| `error_5xx` | 503 inmediato | 503 |
| `timeout` | nunca responde (sleep 60 s) | no responde |
| `flaky` | mezcla `normal` / 503 según `failure_rate` | 200 |

**El comportamiento de `/health` en `slow` y `flaky` es deliberado y central para SP-4.** El health check declara sano al proveedor mientras el endpoint de negocio se arrastra o falla de forma intermitente. Esto expone el punto ciego estructural de la detección proactiva por Ping–Echo frente a la detección reactiva del breaker, que observa tráfico real. Ese contraste es el hallazgo que SP-4 debe producir. **No lo "arregles".**

Expón en `/metrics` un gauge `mock_openfinance_mode` mapeado a entero, para anotar la ventana de fallo en Grafana.

---

## 5. Parametrización

Cada parámetro se declara junto al SP que lo justifica. Genera `.env.example` con esta misma agrupación y comentarios.

### 5.1 Variables independientes

```bash
# --- SP-1: timeout hacia Open Finance ---
OPENFINANCE_TIMEOUT_MS=700              # contrastar: 400 | 700 | 1000

# --- SP-2: umbral de apertura y ventana half-open ---
BREAKER_FAIL_MAX=5                      # 3 | 5 | 10
BREAKER_RESET_TIMEOUT_S=30              # 10 | 30 | 60
BREAKER_ENABLED=true                    # ablación

# --- SP-3: TTL de caché y caché fría ---
PROFILE_CACHE_TTL_S=300                 # 60 | 300 | 900
PROFILE_CACHE_STALE_GRACE_S=1800        # 0 | 600 | 1800
CACHE_ENABLED=true                      # ablación
CACHE_PRELOAD_RATIO=0.5                 # 0.0 (caché fría) | 0.5 | 1.0

# --- SP-4: fuente de verdad de la detección ---
MONITOR_SIGNAL_ENABLED=true             # false = solo detección reactiva del breaker
MONITOR_INTERVAL_MS=2000                # 1000 | 2000 | 5000
MONITOR_UNHEALTHY_THRESHOLD=2           # 1 | 2 | 3

# --- SP-5: aislamiento de recursos ---
BULKHEAD_ENABLED=true                   # ablación
POOL_OPENFINANCE_MAX=8                  # 4 | 8 | 16   (en financial-profiler)
POOL_PENDING_REPLIES_MAX=16             # 8 | 16 | 32  (esperas concurrentes en cotizacion)
POOL_PROVIDER_MAX=8                     # fijo: es el control de SP-5, no una variable
```

### 5.2 Variables de control

Fijas durante todo el experimento salvo instrucción explícita.

```bash
QUOTE_MODE=treatment
CONSUMER_MODE=single_active             # competing solo para la variante documentada de §3.2
MONITOR_TIMEOUT_MS=500
MONITOR_HEALTHY_THRESHOLD=2

JOURNEY_LATENCY_BUDGET_MS=250
REPLY_TIMEOUT_MS=900                    # derivado del presupuesto, no se sintoniza de forma
                                        # independiente; ver la observación transversal de §7

# Concurrencia — deliberadamente baja para que la saturación sea observable en SP-5
GUNICORN_WORKERS=2
GUNICORN_THREADS=4
GUNICORN_WORKER_CLASS=gthread
GUNICORN_TIMEOUT=60

K6_RPS=50
K6_DURATION=240s
```

### 5.3 Infraestructura y endpoints

```bash
REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql+psycopg://solventa:solventa@postgres:5432/solventa

RABBITMQ_URL=amqp://solventa:solventa@rabbitmq:5672/
RABBITMQ_MGMT_URL=http://rabbitmq:15672
EXCHANGE_QUOTES=solventa.quotes
QUEUE_REQUESTS=cotizacion.requests
EXCHANGE_EVENTS=solventa.events

COTIZACION_URL=http://cotizacion:8082
SOCIO_URL=http://socio-distribucion:8081
PROFILER_URL=http://financial-profiler:8085
OPENFINANCE_URL=http://mock-openfinance:8090

LOG_LEVEL=INFO
LOG_FORMAT=json
SERVICE_NAME=
PORT=
ROLE=                                   # PRIMARY | BACKUP en procesador-cotizacion
```

Ningún hostname, puerto ni parámetro de táctica puede estar hardcodeado.

---

## 6. Observabilidad — el entregable real

Cada métrica existe porque algún SP la necesita para decidir.

**Transversales (journey):**
```
solventa_http_requests_total{service,route,status,quote_mode}        Counter
solventa_http_request_duration_seconds{service,route,quote_mode}     Histogram
    buckets: [.025,.05,.1,.15,.2,.25,.3,.4,.5,.7,1,1.5,2,3,5,10,30]
solventa_quotes_total{profile_quality}                               Counter
solventa_journey_stage_duration_seconds{stage}                       Histogram
    # stages: gateway | provider_call | broker_publish | queue_wait |
    #         processor_handling | profiler_call | broker_reply | compose
```

`solventa_journey_stage_duration_seconds` es obligatoria y no opcional: es la que permite la **descomposición del presupuesto de latencia** descrita en §7.1.

**SP-1 — timeout:**
```
solventa_openfinance_duration_seconds                                Histogram
solventa_openfinance_calls_total{outcome}                            Counter   # success|timeout|error|rejected_open
solventa_openfinance_timeout_exhausted_total                         Counter
```

**SP-2 — breaker:**
```
solventa_circuit_breaker_state{dependency}                           Gauge     # 0=closed 1=open 2=half_open
solventa_circuit_breaker_transitions_total{from_state,to_state}      Counter
solventa_circuit_breaker_calls_total{outcome}                        Counter   # success|failure|rejected
```

**SP-3 — caché:**
```
solventa_profile_cache_operations_total{result}                      Counter   # hit_fresh|hit_stale|miss|write
solventa_profile_cache_age_seconds                                   Histogram
```

**SP-4 — detección:**
```
solventa_monitor_dependency_up{dependency}                           Gauge
solventa_detection_source_total{source}                              Counter   # monitor_signal|breaker_count
solventa_detection_latency_seconds{source}                           Histogram
solventa_health_signal_received_total{state}                         Counter
```

**SP-5 — aislamiento:**
```
solventa_pool_inflight{pool}                                         Gauge     # provider|openfinance|pending_replies
solventa_pool_rejected_total{pool}                                   Counter
solventa_pool_wait_seconds{pool}                                     Histogram
solventa_gunicorn_busy_workers{service}                              Gauge
```

**Broker y reconfiguración:**
```
solventa_queue_published_total{queue}                                Counter
solventa_queue_consumed_total{queue,processor_role}                  Counter
solventa_queue_depth{queue}                                          Gauge     # del API de management de RabbitMQ
solventa_queue_wait_seconds                                          Histogram
solventa_reply_timeout_total                                         Counter
solventa_orphan_reply_total                                          Counter
solventa_processor_active{role}                                      Gauge
solventa_takeover_events_total                                       Counter
```

**Prometheus:** `scrape_interval: 1s`. El default de 15 s daría 8 puntos en una ventana de fallo de 120 s, insuficiente para medir la ventana de detección de SP-2. Incluye como target el exporter nativo de RabbitMQ en `rabbitmq:15692`.

**Grafana:** datasource y dashboards provisionados como código en `infra/grafana/provisioning/`. Un dashboard general más uno por SP:

| Dashboard | Paneles clave |
|---|---|
| `Solventa — General` | Tasa de 5xx (umbral visual en 0 %), p50/p95/p99 con línea en 250 ms, **descomposición del presupuesto por etapa (barras apiladas)**, distribución de `profile_quality`, modo del mock como overlay |
| `SP-1 — Timeout` | Duración de llamadas a Open Finance, timeouts agotados vs. éxitos, pérdida de FRESH por corte prematuro |
| `SP-2 — Circuit breaker` | State timeline, ventana de detección, tiempo hasta CLOSED tras recuperación, transiciones (flapping) |
| `SP-3 — Caché` | Hit ratio por tipo, edad del dato servido, cobertura de degradación |
| `SP-4 — Detección` | Latencia de detección monitor vs. breaker, señal de salud contra tasa de error real (evidencia del desacuerdo en modo `slow`) |
| `SP-5 — Aislamiento` | p95 de la ruta Provider vs. ruta de perfilamiento, inflight por pool, profundidad de cola, workers ocupados, rechazos |

---

## 7. Diseño experimental

**Los modos de fallo del mock no son el experimento: son el fixture bajo el cual se mide cada SP.** Cada sub-experimento se ejecuta con `scripts/run_experiment.sh <SP-ID>`.

**Línea de tiempo estándar:**
```
[0–60 s]     mock en normal, carga estable       → línea base sana
[60–180 s]   inyección del fallo del fixture     → ventana de indisponibilidad
[180–240 s]  mock vuelve a normal                → medición de recuperación
```

### 7.1 Observación transversal: descomposición del presupuesto de latencia

En todas las corridas, incluida `SP-0` en condiciones sanas, `collect_results.py` debe producir la descomposición del p95 del journey por etapa a partir de `solventa_journey_stage_duration_seconds`, con una fila explícita para **el sobrecosto atribuible al broker** (`broker_publish + queue_wait + broker_reply`).

Esto no es telemetría accesoria. El diseño coloca un patrón request-reply sobre un broker dentro de un presupuesto de 250 ms, y el POC existe para determinar **con datos** si ese sobrecosto cabe. El resultado puede ser que sí, que cabe con margen estrecho, o que no cabe. Cualquiera de los tres es un hallazgo legítimo del informe. Reporta el número; no lo interpretes ni ajustes el diseño para favorecerlo.

---

### SP-0 — Control: ¿existe el problema?

| | |
|---|---|
| **Pregunta** | ¿El acoplamiento con Open Finance propaga efectivamente el fallo al socio? |
| **Variable independiente** | `QUOTE_MODE` ∈ {`baseline`, `treatment`} |
| **Control** | Parámetros en default; caché al 50 % |
| **Fixture** | `error_5xx` y `timeout` |
| **Métrica de decisión** | Tasa de 5xx en el gateway; p95; workers ocupados máximos; descomposición de latencia (§7.1) |
| **Resultado esperado** | `baseline`: ~100 % de 5xx y saturación de workers. `treatment`: 0 % de 5xx |

**Si el baseline no falla, el experimento completo carece de premisa.** Revisa la configuración de concurrencia antes de continuar.

---

### SP-1 — Valor del timeout hacia Open Finance

| | |
|---|---|
| **Pregunta de decisión** | ¿Qué valor de timeout minimiza el daño a la latencia sin sacrificar perfiles legítimos? |
| **Variable independiente** | `OPENFINANCE_TIMEOUT_MS` ∈ {400, 700, 1000} |
| **Control** | `BREAKER_FAIL_MAX=5`, `BREAKER_RESET_TIMEOUT_S=30`, `PROFILE_CACHE_TTL_S=300`, caché al 50 % |
| **Fixture** | `timeout` (costo en latencia) y `slow` con `latency_ms` ∈ {300, 600, 900} (falsos positivos) |
| **Métrica de decisión** | p95/p99 durante la ventana; nº de peticiones que agotan el timeout completo antes de que abra el breaker; **% de perfiles FRESH perdidos por cortar a un proveedor que sí iba a responder** |
| **Trade-off a exponer** | Un timeout bajo protege la latencia pero descarta respuestas válidas y degrada el pricing. Uno alto conserva precisión pero consume el presupuesto del journey y satura workers. |
| **Consideración adicional** | El presupuesto disponible para el timeout está acotado por el sobrecosto del broker medido en §7.1. Repórtalos juntos. |
| **Criterio** | El menor timeout que mantenga la pérdida de FRESH bajo el umbral acordado, en modo `slow` con latencia igual al p99 sano del proveedor |

---

### SP-2 — Umbral de apertura y ventana half-open

| | |
|---|---|
| **Pregunta de decisión** | ¿Cuántas peticiones pagan el costo completo del fallo antes de que el circuito abra, y con qué rapidez recupera precisión sin oscilar? |
| **Variable independiente** | `BREAKER_FAIL_MAX` ∈ {3, 5, 10} × `BREAKER_RESET_TIMEOUT_S` ∈ {10, 30, 60} |
| **Control** | `OPENFINANCE_TIMEOUT_MS=700`, caché al 50 %, `MONITOR_SIGNAL_ENABLED=false` para aislar la detección reactiva |
| **Fixture** | `error_5xx`, `timeout`, y `flaky` con `failure_rate` ∈ {0.2, 0.5, 0.8} |
| **Métrica de decisión** | **Ventana de detección**: nº de peticiones y latencia acumulada entre la inyección y la transición a OPEN. Tiempo desde la recuperación real hasta CLOSED. Nº de transiciones OPEN↔CLOSED bajo `flaky` (flapping). |
| **Trade-off a exponer** | Umbral bajo abre rápido pero es sensible a fallos transitorios. Ventana de reset corta recupera precisión antes pero reabre en falso bajo fallo intermitente. |
| **Criterio** | Ventana de detección acotada; recuperación ≤ 60 s sin reinicio manual; mínimo flapping bajo `flaky(0.5)` conservando apertura garantizada bajo `error_5xx` |

---

### SP-3 — TTL de la caché y caché fría

| | |
|---|---|
| **Pregunta de decisión** | ¿Qué TTL y ventana de gracia dan cobertura suficiente de degradación, y cuál es el peor caso real? |
| **Variable independiente** | `PROFILE_CACHE_TTL_S` ∈ {60, 300, 900} × `PROFILE_CACHE_STALE_GRACE_S` ∈ {0, 600, 1800} × `CACHE_PRELOAD_RATIO` ∈ {0.0, 0.5, 1.0} |
| **Control** | `OPENFINANCE_TIMEOUT_MS=700`, breaker en default |
| **Fixture** | `error_5xx` durante 120 s |
| **Métrica de decisión** | Distribución FRESH/DEGRADED/DEFAULT; hit ratio fresco vs. stale; edad del dato servido; **cobertura de degradación** = % de cotizaciones con perfil real en lugar de valores por defecto |
| **Trade-off a exponer** | TTL corto mantiene precisión en operación normal pero deja la caché vacía justo cuando se necesita. TTL largo maximiza cobertura ante fallo a costa de servir datos financieros viejos, con implicaciones de pricing y de auditoría. |
| **Criterio** | TTL mínimo con cobertura aceptable; **cuantificar explícitamente el escenario de caché fría** (`CACHE_PRELOAD_RATIO=0.0`) |
| **Hallazgo esperado a documentar** | Con caché fría se cumple el ASR literalmente (0 % de 5xx) pero **el 100 % de las cotizaciones sale con perfil DEFAULT**. La disponibilidad se sostiene y la precisión del pricing colapsa. Es el límite honesto de la táctica y debe quedar en el informe. |

---

### SP-4 — Fuente de verdad de la detección

| | |
|---|---|
| **Pregunta de decisión** | ¿Detectar proactivamente con el Monitor (Ping–Echo) o reactivamente con el conteo del breaker sobre tráfico real? |
| **Variable independiente** | `MONITOR_SIGNAL_ENABLED` ∈ {true, false} × `MONITOR_INTERVAL_MS` ∈ {1000, 2000, 5000} |
| **Control** | `BREAKER_FAIL_MAX=5`, `OPENFINANCE_TIMEOUT_MS=700`, caché al 50 % |
| **Fixture** | `error_5xx` y `timeout` (ambas fuentes detectan); **`slow(1500)` y `flaky(0.5)`**, donde `/health` responde 200 y solo el tráfico real revela el problema |
| **Métrica de decisión** | Latencia de detección de cada fuente (desde la inyección hasta el corte efectivo); nº de peticiones afectadas en ese intervalo; **casos de desacuerdo entre fuentes** |
| **Trade-off a exponer** | El Monitor detecta antes y sin gastar peticiones de usuario, pero observa un endpoint que no es el que importa: puede declarar sano un proveedor que se arrastra. El breaker observa la verdad operativa, pero solo la descubre gastando peticiones reales. |
| **Criterio** | Cuantificar la ventaja del Monitor en caídas duras y **su punto ciego en degradaciones parciales**. La regla de §3.3 (el Monitor solo abre, nunca cierra) debe quedar validada con datos, no asumida. |

---

### SP-5 — Aislamiento de recursos (bulkhead)

| | |
|---|---|
| **Pregunta de decisión** | ¿La saturación del perfilamiento degrada la ruta Provider, que no depende de Open Finance? |
| **Variable independiente** | `BULKHEAD_ENABLED` ∈ {true, false} × `POOL_OPENFINANCE_MAX` ∈ {4, 8, 16} × `POOL_PENDING_REPLIES_MAX` ∈ {8, 16, 32} |
| **Control** | `POOL_PROVIDER_MAX=8` fijo, `GUNICORN_WORKERS=2`, `GUNICORN_THREADS=4`, `BREAKER_ENABLED=false` para forzar saturación sostenida y no enmascararla |
| **Fixture** | `timeout` — el único modo que retiene recursos ocupados de forma sostenida |
| **Métrica de decisión** | **p95 de la ruta Provider durante la saturación, contra su p95 en línea base sana**; inflight y rechazos por pool; profundidad de `cotizacion.requests`; workers ocupados |
| **Nota de diseño** | En esta arquitectura hay dos frentes de saturación distintos y ambos deben medirse: el pool de salida hacia Open Finance en `financial-profiler`, y **las esperas de réplica acumuladas en `cotizacion`**, que retienen hilos de Gunicorn mientras la cola no responde. El segundo es el que amenaza directamente a la ruta Provider. |
| **Trade-off a exponer** | Pools pequeños contienen el daño pero rechazan trabajo legítimo bajo pico normal. Pools grandes absorben picos pero permiten que una dependencia colgada consuma toda la capacidad del servicio. |
| **Criterio** | Con bulkhead activo, la desviación del p95 de Provider frente a su línea base debe mantenerse bajo el umbral acordado. Con bulkhead desactivado debe degradarse de forma observable; si no lo hace, la configuración de concurrencia es demasiado holgada y hay que reducir workers. |

---

### 7.2 Guion de carga k6

- `constant-arrival-rate` para que las corridas sean comparables. Tasa y duración por env.
- `X-Partner-Id` rotativo entre 3 socios; `client_id` del dataset sintético.
- La mezcla de `client_id` debe respetar `CACHE_PRELOAD_RATIO` para que el hit/miss sea **conocido y controlado**, no accidental. Es la variable que SP-3 necesita gobernar.
- Umbrales explícitos que hagan fallar la corrida si no se cumple el ASR:
  ```js
  thresholds: {
    'http_req_failed': ['rate==0'],
    'http_req_duration': ['p(95)<250', 'p(99)<500'],
  }
  ```
  En `baseline` se espera que fallen. Ese es el resultado, no un error del guion.
- Escenario secundario en paralelo que golpea **solo** la ruta Provider a tasa constante, para tener la serie de control de SP-5.
- Resumen JSON a `results/<run_id>/k6_summary.json`.

### 7.3 Recolección de resultados

`collect_results.py` consulta la API de Prometheus por el rango de la corrida y produce:

- `results/<run_id>/metrics.csv` — series crudas.
- `results/<run_id>/summary.md` — una sección por SP con variable independiente, valores de control, métrica de decisión y valor observado; más la **descomposición del presupuesto de latencia** de §7.1.
- `results/sp_<N>_matrix.md` — tabla comparativa de todas las combinaciones de ese SP, lista para el informe, con una columna final de **recomendación de valor**.

Debe registrar en un archivo de metadata los timestamps exactos de inyección y recuperación, sin los cuales la latencia de detección de SP-2 y SP-4 no es calculable.

---

## 8. Contenerización y despliegue en AWS

### 8.1 Reglas de contenerización

- **Un `Dockerfile` por servicio** en `services/<nombre>/Dockerfile`. Construcción y versionado independientes.
- Base `python:3.11-slim`. Usuario no-root.
- **Build explícito para `linux/amd64`** (`--platform=linux/amd64`). Es el fallo clásico al construir en Mac ARM y desplegar en ECS x86.
- Contexto de build en la raíz (para copiar `libs/solventa_common`), pero cada Dockerfile copia solo lo que necesita. `.dockerignore` en la raíz.
- Sin volúmenes para estado de aplicación. El estado vive en Redis, Postgres y RabbitMQ.
- Puerto único leído de `PORT`; Gunicorn con `--bind 0.0.0.0:$PORT`.

### 8.2 Requisitos para AWS

- **Health checks:** `GET /health/live` (proceso vivo, sin tocar dependencias) y `GET /health/ready` (dependencias críticas responden). ALB y ECS usan `/health/live`.
- **Logs solo a stdout**, JSON de una línea, con `correlation_id`, `service`, `level`, `timestamp`. Nunca a archivo.
- **Configuración 100 % por env vars.**
- **Sin dependencias de orden de arranque.** Cada servicio arranca aunque Redis, Postgres o RabbitMQ no estén listos, y reintenta con backoff. ECS no garantiza orden.
- **Shutdown limpio ante `SIGTERM`**: cerrar canales y conexiones AMQP, dejar de consumir, drenar peticiones en vuelo. Un consumidor que no cierra limpio retrasa la promoción del BACKUP por single-active-consumer.
- Hostnames por env var, de modo que `amqp://rabbitmq:5672` se convierta en el endpoint de Amazon MQ cambiando una variable.

### 8.3 Artefactos en `deploy/aws/`

**`ecr/`** — `create-repos.sh` (un repositorio por servicio) y `build-and-push.sh`, que construye con `--platform linux/amd64`, etiqueta con git SHA + `latest` y hace push. **Debe aceptar un nombre de servicio como argumento** para construir uno solo: es la prueba de despliegue independiente.

**`ec2-compose/` — ruta recomendada.** `docker-compose.aws.yml` con `image:` apuntando a ECR; `user-data.sh` que instala Docker, hace login en ECR y levanta el stack; `README.md` con tipo de instancia (`t3.large` — 2 vCPU / 8 GB cumple el requisito del experimento), security group mínimo restringido a la IP del equipo (22, 3000, 8080, 15672), IAM instance profile con `AmazonEC2ContainerRegistryReadOnly` y estimación de costo mensual.

**`ecs-fargate/` — camino de evolución.** Una task definition JSON **por servicio**, con placeholders de cuenta y región; definiciones de servicio con health check; `README.md` documentando el reemplazo de dependencias (Redis → ElastiCache, Postgres → RDS, **RabbitMQ → Amazon MQ for RabbitMQ**, que soporta AMQP 0-9-1 nativamente) y la resolución entre servicios vía AWS Cloud Map. **No escribas Terraform ni CDK**: JSON parametrizado y AWS CLI es suficiente y más auditable para el informe.

---

## 9. Estructura del repositorio

```
solventa-poc/
├── README.md
├── Makefile
├── .env.example
├── .dockerignore
├── docker-compose.yml                 # stack completo, modo treatment
├── docker-compose.baseline.yml        # override para SP-0
├── docker-compose.load.yml            # override que añade k6
│
├── libs/solventa_common/
│   ├── pyproject.toml
│   └── solventa_common/
│       ├── config.py                  # carga y validación de env vars
│       ├── logging.py                 # logger JSON con correlation_id
│       ├── metrics.py                 # registro Prometheus compartido
│       ├── correlation.py             # middleware Flask + propagación a AMQP
│       ├── health.py                  # blueprint /health/live y /health/ready
│       ├── http_client.py             # cliente con timeout, pool acotado y semáforo (bulkhead)
│       ├── messaging.py               # abstracción sobre pika: publish, consume,
│       │                              # single-active-consumer, reply queues, backoff
│       └── app_factory.py
│
├── services/
│   ├── api-gateway/          {Dockerfile, requirements.txt, app/, tests/}
│   ├── socio-distribucion/
│   ├── cotizacion/
│   ├── procesador-cotizacion/
│   ├── financial-profiler/
│   ├── monitor/
│   ├── health-manager/
│   ├── notificador/
│   └── mock-openfinance/
│
├── infra/
│   ├── prometheus/prometheus.yml
│   ├── grafana/provisioning/{datasources,dashboards}/   # general + 5 por SP
│   ├── rabbitmq/{rabbitmq.conf,enabled_plugins,definitions.json}
│   └── postgres/init.sql
│
├── load/k6/
│   ├── quote_load.js
│   ├── provider_control.js            # serie de control de SP-5
│   └── data/quotes.json
│
├── scripts/
│   ├── seed_data.py                   # respeta CACHE_PRELOAD_RATIO
│   ├── run_experiment.sh              # argumento: SP-0 … SP-5
│   ├── inject_fault.sh
│   ├── collect_results.py
│   └── smoke_test.sh
│
├── results/
└── deploy/aws/{ecr,ec2-compose,ecs-fargate}/
```

---

## 10. Datos sintéticos

`scripts/seed_data.py`, determinista con semilla fija:

- **1.000 solicitudes** en `load/k6/data/quotes.json`: `{client_id, product_code, insured_amount, age, city, partner_id}`. Productos `VIAJE`, `DISPOSITIVO`, `VIDA_MICRO`.
- **Perfiles precargados en Redis según `CACHE_PRELOAD_RATIO`**, tomados de los mismos `client_id` del dataset. Con `0.5` se obtiene ~50 % de hits y ~50 % de misses de forma controlada; con `0.0` se reproduce la caché fría de SP-3. La proporción debe ser exacta y reproducible entre corridas, no aleatoria.
- **Catálogo en Postgres**: productos, tarifas base y rangos de factor de riesgo.

Cálculo de prima, determinista y sin realismo actuarial:
```
prima = tarifa_base[producto] × f_edad(age) × f_riesgo(perfil) × f_monto(insured_amount)
```
Con perfil `DEFAULT` se aplican factores conservadores fijos y la respuesta lo marca, para que el socio sepa que el precio es preliminar.

---

## 11. README

1. Qué es esto y qué experimento soporta (3 párrafos máximo).
2. Diagrama de componentes en Mermaid, fiel a §3.1 y §3.2, distinguiendo el tramo síncrono del tramo por cola.
3. Arranque en 3 comandos.
4. Tabla de servicios: nombre, puerto, responsabilidad, endpoints.
5. **Cómo ejecutar cada SP** y qué pregunta responde.
6. Cómo inyectar fallos manualmente (curl de ejemplo).
7. Dónde ver resultados: Grafana, Prometheus, consola de RabbitMQ y ruta de `results/`.
8. Tabla de variables de entorno **agrupada por SP**, con default y efecto.
9. Sección **"Qué está mockeado y por qué"** — stubs y simplificaciones con justificación de alcance. Va directo al informe.
10. Sección **"Decisiones de implementación"**, cubriendo al menos: por qué cola exclusiva por instancia en lugar de direct reply-to; por qué single-active-consumer implementa el takeover; por qué el Monitor solo abre y nunca cierra el circuito; por qué no hay almacén de idempotencia; por qué Redis es solo caché y no transporte.
11. Cómo desplegar en AWS.

---

## 12. Criterios de aceptación

- [ ] `docker compose up` levanta el stack y `scripts/smoke_test.sh` pasa sin intervención manual.
- [ ] Con el mock en `normal`, una petición al gateway devuelve 200 con `profile_quality: FRESH`.
- [ ] **SP-0:** con `error_5xx`, `treatment` da 0 % de 5xx y `baseline` propaga 5xx con saturación de workers. *Si el baseline no falla, el experimento no tiene premisa.*
- [ ] La descomposición del presupuesto de latencia (§7.1) se produce en toda corrida, con el sobrecosto del broker como fila propia.
- [ ] **SP-1:** las tres configuraciones de timeout producen valores medibles y distintos de latencia y de pérdida de FRESH.
- [ ] **SP-2:** el gauge del breaker transiciona `closed → open` durante el fallo y `open → half_open → closed` tras la recuperación en ≤ 60 s, sin reinicio manual. La ventana de detección es calculable desde las métricas.
- [ ] **SP-3:** con `CACHE_PRELOAD_RATIO=0.5` la distribución de `profile_quality` refleja esa proporción. Con `0.0` se obtiene 0 % de 5xx y ~100 % de `DEFAULT`, y queda registrado.
- [ ] **SP-4:** en modo `slow`, el Monitor reporta el proveedor sano mientras el breaker registra fallos. El desacuerdo es visible en el dashboard.
- [ ] **SP-5:** con bulkhead activo y Open Finance colgado, el p95 de la ruta Provider se mantiene cerca de su línea base; sin bulkhead, se degrada de forma observable.
- [ ] Matar `procesador-cotizacion-primary` no pierde mensajes: RabbitMQ promueve al BACKUP por single-active-consumer, el mensaje en vuelo se re-encola y `solventa_takeover_events_total` incrementa.
- [ ] Una respuesta que llega tras vencer `REPLY_TIMEOUT_MS` se descarta e incrementa `solventa_orphan_reply_total`, sin alterar la respuesta ya entregada al socio.
- [ ] Los seis dashboards de Grafana cargan automáticamente con datos.
- [ ] `scripts/run_experiment.sh SP-2` corre de punta a punta y deja `results/sp_2_matrix.md` poblado.
- [ ] Los nueve servicios se construyen de forma independiente: `docker build -f services/<x>/Dockerfile .` funciona para cada uno, y `deploy/aws/ecr/build-and-push.sh cotizacion` construye solo ese.
- [ ] Todos responden `/health/live` y `/health/ready` y arrancan aunque sus dependencias no estén listas.
- [ ] Ningún hostname, puerto ni parámetro de táctica está hardcodeado.
- [ ] **No existe almacén de idempotencia ni lógica de deduplicación.** El `correlation_id` se usa solo para casar réplicas y para trazabilidad en logs.

---

## 13. Cómo trabajar

1. **Empieza por un plan.** Antes de escribir código, escribe `PLAN.md` con el orden de construcción y muéstramelo. Espera confirmación.
2. **Orden de construcción:** `libs/solventa_common` → `mock-openfinance` → `financial-profiler` (sin tácticas) → `cotizacion` → `api-gateway` → `socio-distribucion` → compose mínimo end-to-end en `baseline` → RabbitMQ, cola y `procesador-cotizacion` con request-reply → timeout y breaker → caché y degradación → `monitor` y señalización → bulkhead → `health-manager` y `notificador` → observabilidad y dashboards → k6 y scripts de experimento → despliegue AWS.
3. **Verifica cada capa antes de seguir.** Levanta el compose y prueba con curl. No acumules tres servicios sin ejecutar nada.
4. **Commits pequeños**, uno por servicio o capacidad.
5. **No rediseñes.** El diseño de §3 viene de modelos de arquitectura ya entregados. Si detectas una debilidad, escríbela en `OBSERVACIONES.md` y, si es medible, propón la métrica que la expondría. No la corrijas en el código sin autorización.
6. **Pregunta antes de expandir el alcance.** Si crees que falta algo que no está en §2.1 o §2.2, propónlo; no lo construyas.
7. **Cada táctica lleva un comentario que la nombra** (`# TÁCTICA: circuit breaker — SP-2`), para poder rastrearla desde el documento de arquitectura hasta el código. Este repositorio es evidencia de un informe académico.
8. **Sin dependencias innecesarias.** Si algo se resuelve con la librería estándar, úsala.
