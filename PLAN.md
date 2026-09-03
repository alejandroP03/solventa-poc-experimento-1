# Plan de construcción — POC Solventa / Experimento 1

## Contexto

`kickoff.md` especifica un POC ejecutable de microservicios que **no es un producto sino un instrumento de medición**: existe para resolver cinco puntos de sensibilidad (SP-1..SP-5) sobre una arquitectura ya diseñada (Cuaderno IV, MISW4202) con evidencia cuantitativa. El ASR bajo prueba (ASR-Disp-09) exige 0 % de 5xx durante una caída del proveedor de Open Finance, degradando el `profile_quality` en lugar de fallar.

El directorio está vacío salvo `kickoff.md`, así que no hay código previo que reutilizar: el documento **es** la especificación de diseño y §3 está congelado. La restricción rectora es **fidelidad, no calidad de producto**: si el diseño tiene una debilidad, se mide y se reporta en `OBSERVACIONES.md`, no se corrige en el código.

**Resultado esperado:** un stack de 9 servicios + 5 piezas de infraestructura que arranca con `docker compose up`, seis dashboards de Grafana provisionados como código, y `scripts/run_experiment.sh SP-N` que produce las matrices comparativas listas para el informe.

### Decisiones ya cerradas

| Decisión | Valor |
|---|---|
| Raíz del repo | `exp_1/` directamente; `kickoff.md` queda como documento fuente |
| Matriz experimental | OFAT por defecto (~40 corridas ≈ 3 h); `--full` habilita el factorial completo de §7 |
| Checkpoints | **Al cerrar cada bloque de fases: 4 hitos** (Bloque A: F0–F4 · B: F5 · C: F6–F9 · D: F10–F15). Dentro de un bloque verifico cada compuerta con curl pero no espero confirmación |
| Git | `git init` en `exp_1/`, un commit por servicio o capacidad (§13.4) |

### Entorno verificado

- Docker 29.2.1 activo — 10 vCPU, 8 GB. Suficiente para ~15 contenedores con `scrape_interval: 1s`.
- **Host arm64.** El compose local construye **nativo arm64**; solo `deploy/aws/ecr/build-and-push.sh` fuerza `--platform linux/amd64` (§8.1). Forzar amd64 en local pasaría por QEMU y **contaminaría las mediciones de latencia de SP-1 y SP-5** — dejaría de medirse la arquitectura y pasaría a medirse la emulación.
- 10 vCPU no invalidan SP-5: la saturación se provoca por agotamiento de **hilos** (`WORKERS=2 × THREADS=4`), no de CPU.

---

## Bloques y fases

### Bloque A — Cadena síncrona end-to-end en `baseline`

#### Fase 0 · Andamiaje
- Árbol de §9 bajo `exp_1/`, `git init`, `.gitignore` (`results/`, `.env`, `__pycache__/`, `*.pyc`).
- `.env.example` con la agrupación **exacta** por SP de §5.1/§5.2/§5.3 y los comentarios de rangos.
- `.dockerignore` en la raíz, `Makefile` (`up`, `down`, `smoke`, `logs`, `exp SP=N`, `build-one`), `PLAN.md` (copia de este plan, es la evidencia que pide §13.1), `OBSERVACIONES.md` vacío con su encabezado.
- **Compuerta:** el árbol coincide con §9.

#### Fase 1 · `libs/solventa_common`
El único código compartido. Cada módulo lleva el comentario `# TÁCTICA: <nombre> — SP-N` de §13.7.

| Módulo | Contenido |
|---|---|
| `config.py` | Carga tipada y validada de env vars, agrupada por SP. Falla al arrancar ante valores inválidos, nunca con un default silencioso — un default silencioso invalidaría una corrida entera sin avisar |
| `logging.py` | JSON de una línea a stdout con `timestamp`, `level`, `service`, `correlation_id` (§8.2) |
| `metrics.py` | Registro Prometheus compartido; **declara todas las métricas de §6** con los buckets exactos. Soporta modo multiproceso |
| `correlation.py` | Middleware Flask (lee o genera UUID4), `contextvar`, propagación a headers HTTP y a properties AMQP |
| `health.py` | Blueprint `/health/live` (sin tocar dependencias) y `/health/ready` (dependencias críticas) |
| `http_client.py` | `requests.Session` + `HTTPAdapter(pool_maxsize=N)` + **semáforo acotado** (bulkhead SP-5), timeout obligatorio, instrumenta `solventa_pool_{inflight,rejected_total,wait_seconds}` |
| `messaging.py` | Abstracción delgada sobre `pika`: publish con confirms, consume con ack manual, `x-single-active-consumer`, reply queues exclusivas, backoff exponencial, cierre limpio en SIGTERM |
| `app_factory.py` | Ensambla Flask + blueprints `/metrics` y `/health` + middleware; config de Gunicorn desde env |

**Punto crítico — métricas bajo Gunicorn multiproceso.** Con `GUNICORN_WORKERS=2`, un `/metrics` ingenuo devuelve solo el proceso que atendió el scrape y **la mitad de los datos del experimento se pierde en silencio**. `metrics.py` usa `prometheus_client.multiprocess` con `PROMETHEUS_MULTIPROC_DIR` sobre un `tmpfs` interno del contenedor (no es un volumen de estado, respeta §8.1), con `multiprocess_mode` explícito en cada Gauge (`livesum` para inflight, `max` para el estado del breaker).

- **Compuerta:** `pytest` sobre config, correlation y el semáforo de `http_client`.

#### Fase 2 · `mock-openfinance` (8090) — el instrumento
- `GET /openfinance/v1/profiles/{client_id}` + `POST /admin/mode|reset`, `GET /admin/state`, `GET /health`.
- Los cinco modos de §4 con su tabla de comportamiento; `duration_s` auto-revierte vía hilo temporizador.
- **La asimetría de `/health` en `slow` y `flaky` es deliberada** (§4): responde 200 mientras el endpoint de negocio se arrastra o falla. Es el punto ciego que SP-4 debe exponer; no se "arregla".
- Gauge `mock_openfinance_mode` mapeado a entero para el overlay de Grafana.
- **Compuerta:** curl a los cinco modos; confirmar que `/health` miente en `slow` y `flaky`.

#### Fase 3 · `financial-profiler` (8085), sin tácticas
- `POST /profile {client_id}` → llamada cruda a Open Finance con timeout. Sin breaker ni caché todavía.
- `POST /internal/dependency-health` presente pero inerte (se activa en F8).
- Métricas de SP-1: `solventa_openfinance_{duration_seconds,calls_total,timeout_exhausted_total}`.
- **Compuerta:** perfil correcto con mock `normal`; con `error_5xx` el fallo se propaga (aún esperado).

#### Fase 4 · `socio-distribucion` (8081) + `cotizacion` (8082) + Kong (8080)
- `socio-distribucion`: precio determinista, latencia baja fija. Es la **ruta no afectada** de SP-5.
- `cotizacion`: `POST /quotes`. En `baseline` llama al profiler en cadena bloqueante (timeout 30 s) y al provider; compone `prima = tarifa_base × f_edad × f_riesgo × f_monto`. Instrumenta las etapas del journey. Expone además `GET /provider-quote`, la **serie de control de SP-5**: solo ruta Provider, pero compartiendo los hilos de Gunicorn con la ruta de perfilamiento.
- **Kong 3.7 DB-less** en lugar del `api-gateway` en Flask (decisión del equipo, ver `OBSERVACIONES.md` OBS-06): plugin `correlation-id` para generar el `X-Correlation-Id` y plugin `prometheus` para la **frontera de medición del 5xx**, con `retries: 0` explícito para no multiplicar la carga durante la ventana de indisponibilidad.
- `docker-compose.yml` + `docker-compose.baseline.yml` (override de `QUOTE_MODE` y timeout).
- **Compuerta — hito del Bloque A:** `docker compose up` + curl al gateway → 200 con perfil real; mock a `error_5xx` → **el gateway devuelve 5xx**. Si el baseline no falla, el experimento no tiene premisa (§7 SP-0) y hay que revisar la concurrencia antes de seguir.

---

### Bloque B — Cola, request-reply y takeover

#### Fase 5 · RabbitMQ + `procesador-cotizacion` (8083/8084) + modo `treatment`
- `infra/rabbitmq/`: `enabled_plugins` (management + prometheus), `definitions.json` con la topología de §3.2 — exchange `solventa.quotes` (direct, durable), cola `cotizacion.requests` (durable, persistente, `x-single-active-consumer: true`, ack manual), exchange `solventa.events` (fanout, durable).
- `cotizacion`: cola de respuestas **exclusiva por instancia** declarada al arrancar (no `amq.rabbitmq.reply-to`, §3.2), consumida en hilo dedicado; registro de esperas `{correlation_id: (Event, slot)}` con lock y purga por TTL; al vencer `REPLY_TIMEOUT_MS` → **200 con `DEFAULT`** e incrementa `solventa_reply_timeout_total`; réplicas huérfanas descartadas en silencio → `solventa_orphan_reply_total`.
- `procesador-cotizacion`: consumidor con ack manual, `ROLE` por env, soporta `CONSUMER_MODE ∈ {single_active, competing}` (default `single_active`, no se cambia).
- **`GUNICORN_WORKERS=1` fijo solo para el procesador.** Con 2 workers habría dos consumidores AMQP registrados por instancia y la semántica PRIMARY/BACKUP de single-active-consumer dejaría de significar lo que el modelo dice. Va documentado en el README §10.
- Métricas de broker: `queue_published_total`, `queue_consumed_total`, `queue_wait_seconds`.
- **Descomposición del presupuesto (§7.1):** el publicador estampa el timestamp de publicación en el mensaje; el procesador calcula `queue_wait`, y `cotizacion` calcula `broker_reply` al recibir. Todos los contenedores comparten el reloj del host Docker, así que la resta entre servicios es válida. Con `stage ∈ {gateway, provider_call, broker_publish, queue_wait, processor_handling, profiler_call, broker_reply, compose}`.
- **Compuerta — hito del Bloque B:** quote en `treatment` → 200; `docker kill` al PRIMARY → el BACKUP toma el relevo **sin pérdida** y el mensaje en vuelo se re-encola; una respuesta que llega tras el `REPLY_TIMEOUT_MS` incrementa `orphan_reply_total` sin alterar la respuesta ya entregada.

---

### Bloque C — Las cuatro tácticas bajo prueba

#### Fase 6 · Timeout y circuit breaker (SP-1, SP-2)
- `pybreaker` en el profiler con listener que actualiza `circuit_breaker_state`, `..._transitions_total{from_state,to_state}` y `..._calls_total{outcome}`; `outcome=rejected_open` cuando el circuito corta sin llamar.
- `BREAKER_ENABLED` como ablación (solo aplica en `treatment`).
- **Compuerta:** con `error_5xx`, el gauge pasa a OPEN tras `BREAKER_FAIL_MAX` fallos; tras la recuperación, `open → half_open → closed` en ≤ `RESET_TIMEOUT_S` sin reinicio manual.

#### Fase 7 · Caché de perfil y degradación (SP-3)
- Redis solo como caché (§1). Escritura con TTL en cada éxito; lectura con clasificación **FRESH** (llamada viva OK) / **DEGRADED** (hit dentro de TTL+grace) / **DEFAULT** (miss).
- `solventa_profile_cache_operations_total{hit_fresh|hit_stale|miss|write}`, `..._age_seconds`; `CACHE_ENABLED` como ablación.
- **Compuerta:** con breaker abierto y preload 0.5, la distribución de `profile_quality` refleja la proporción; con 0.0 → 0 % de 5xx y ~100 % `DEFAULT`.

#### Fase 8 · `monitor` (8086) y señalización (SP-4)
- Ping–Echo por HTTP contra `/health` del mock cada `MONITOR_INTERVAL_MS`; al cruzar `MONITOR_UNHEALTHY_THRESHOLD` → `POST /internal/dependency-health` al profiler.
- **La señal solo puede forzar la apertura, nunca el cierre** (§3.3). El cierre queda siempre en la lógica half-open del breaker, que se apoya en tráfico real.
- `MONITOR_SIGNAL_ENABLED=false` → el monitor sigue midiendo y exportando pero no señaliza. Es la variable independiente de SP-4.
- Métricas: `monitor_dependency_up`, `detection_source_total{monitor_signal|breaker_count}`, `health_signal_received_total{state}`.
- **Compuerta:** con `error_5xx` el monitor corta antes que el conteo del breaker; con `slow(1500)` el monitor reporta **up** mientras el breaker acumula fallos — el desacuerdo queda registrado.

#### Fase 9 · Bulkhead (SP-5)
Los **dos frentes de saturación** de §7 SP-5, ambos medidos:
1. `POOL_OPENFINANCE_MAX` — semáforo de salida hacia Open Finance en `financial-profiler`.
2. `POOL_PENDING_REPLIES_MAX` — **esperas de réplica concurrentes en `cotizacion`**, que retienen hilos de Gunicorn mientras la cola no responde. Es el que amenaza directamente a la ruta Provider.
3. `POOL_PROVIDER_MAX=8` fijo — el control, nunca una variable.

- **El rechazo por bulkhead jamás produce 5xx** (invariante duro de §3.1): al no haber slot, `cotizacion` responde 200 con `DEFAULT` de inmediato e incrementa `pool_rejected_total{pool="pending_replies"}`. Ese fail-fast **es** el mecanismo que protege la ruta Provider, y es exactamente lo que SP-5 mide.
- `solventa_gunicorn_busy_workers{service}` vía hook de Gunicorn.
- **Compuerta — hito del Bloque C:** mock en `timeout` con `BREAKER_ENABLED=false`; con bulkhead el p95 de Provider se mantiene cerca de su línea base, sin bulkhead se degrada de forma observable. (Para la profundidad de cola en esta compuerta se usa el exporter nativo `rabbitmq:15692`; `solventa_queue_depth` desde el API de management llega en F10.)

---

### Bloque D — Tier 2, observabilidad y experimentos

#### Fase 10 · `postgres` + `health-manager` (8087) + `notificador` (8088)
- `infra/postgres/init.sql`: catálogo de productos (`VIAJE`, `DISPOSITIVO`, `VIDA_MICRO`), tarifas base, rangos de factor de riesgo, y bitácora append-only de cotizaciones.
- **La bitácora se escribe fuera del camino crítico**, en un hilo con cola en memoria. Una escritura síncrona a Postgres dentro de un presupuesto de 250 ms contaminaría la descomposición de latencia de §7.1 con una etapa que el modelo no contempla.
- `health-manager`: observa los latidos de los procesadores (`/health/live` de 8083/8084) **y** consulta el API de management de RabbitMQ para saber cuál es el consumidor activo de `cotizacion.requests`. Emite el evento de takeover cuando la identidad del consumidor activo cambia; publica en `solventa.events`. Expone `processor_active{role}`, `takeover_events_total` y `solventa_queue_depth{queue}`.
- `notificador`: consume el fanout, log estructurado, contadores y `GET /events`. No envía nada real.
- **Compuerta:** matar el PRIMARY hace aparecer el evento de takeover en `GET /events` y `takeover_events_total` incrementa.

#### Fase 11 · Observabilidad
- `infra/prometheus/prometheus.yml` con **`scrape_interval: 1s`** (§6: el default de 15 s daría 8 puntos en una ventana de 120 s, insuficiente para SP-2), los 9 servicios y `rabbitmq:15692`.
- `infra/grafana/provisioning/`: datasource + **6 dashboards** como código (General + uno por SP) con los paneles de la tabla de §6.
- **Compuerta:** los seis dashboards cargan solos y con datos.

#### Fase 12 · Datos sintéticos y k6
- `scripts/seed_data.py` determinista con semilla fija: 1.000 solicitudes en `load/k6/data/quotes.json`, precarga **exacta** de Redis según `CACHE_PRELOAD_RATIO` (los primeros N×ratio del dataset barajado con semilla), catálogo en Postgres.
- `load/k6/quote_load.js`: `constant-arrival-rate`, `X-Partner-Id` rotativo entre 3 socios, recorrido determinista del dataset para que el hit/miss sea conocido y no accidental, thresholds `http_req_failed: rate==0` y `p(95)<250 / p(99)<500`.
- `load/k6/provider_control.js`: escenario paralelo que golpea **solo** la ruta Provider — la serie de control de SP-5.
- **Riesgo de validez detectado — caché fría (SP-3).** Los 60 s de tráfico sano previos a la inyección son 3.000 peticiones sobre 1.000 client_id: **poblarían la caché por completo y anularían el escenario `CACHE_PRELOAD_RATIO=0.0`**, que es precisamente el hallazgo que §7 SP-3 exige documentar. Mitigación: para las corridas de preload 0.0, `run_experiment.sh` purga las claves de perfil de Redis inmediatamente antes de la inyección y registra la purga en el metadata. Queda anotado en `OBSERVACIONES.md`.

#### Fase 13 · Scripts de experimento y recolección
- `run_experiment.sh SP-0..SP-5` — barrido OFAT por defecto, `--full` para el factorial de §7. Aplica la línea de tiempo estándar (0–60 s sano · 60–180 s fallo · 180–240 s recuperación), levanta el stack con el env de cada combinación y **registra en `metadata.json` los timestamps exactos de inyección y recuperación**, sin los cuales la latencia de detección de SP-2 y SP-4 no es calculable.
- En `baseline` el exit code ≠ 0 de k6 **es el resultado esperado**, no un fallo del guion; el script no lo trata como error.
- `inject_fault.sh`, `smoke_test.sh`.
- `collect_results.py` — consulta el API de rango de Prometheus y produce `results/<run_id>/{metrics.csv,summary.md,k6_summary.json}` y `results/sp_<N>_matrix.md` con columna final de **recomendación de valor**. Toda corrida incluye la **descomposición del p95 por etapa con el sobrecosto del broker (`broker_publish + queue_wait + broker_reply`) como fila propia** (§7.1). El número se reporta; no se interpreta ni se ajusta el diseño para favorecerlo.
- **Nota sobre `detection_latency_seconds`:** el profiler solo puede medir *primer fallo observado → corte efectivo*. La latencia desde la **inyección real** se calcula offline en `collect_results.py` cruzando `metadata.json` con las series. Ambas se reportan por separado y la distinción va a `OBSERVACIONES.md`.
- **Compuerta:** `run_experiment.sh SP-2` corre de punta a punta y deja `results/sp_2_matrix.md` poblado.

#### Fase 14 · AWS (`deploy/aws/`)
- `ecr/create-repos.sh` + `build-and-push.sh`, que **acepta un nombre de servicio** (prueba de despliegue independiente), fuerza `--platform linux/amd64` y etiqueta con git SHA + `latest`.
- `ec2-compose/` (ruta recomendada): `docker-compose.aws.yml` con `image:` a ECR, `user-data.sh`, `README.md` con `t3.large`, security group mínimo (22, 3000, 8080, 15672) y estimación de costo.
- `ecs-fargate/` (evolución): una task definition JSON **por servicio** con placeholders, y README documentando Redis → ElastiCache, Postgres → RDS, RabbitMQ → **Amazon MQ for RabbitMQ**, y Cloud Map. **Sin Terraform ni CDK** (§8.3).

#### Fase 15 · Cierre documental
- `README.md` con los 11 puntos de §11, incluido el diagrama Mermaid que distingue el tramo síncrono del tramo por cola, la tabla de env vars **agrupada por SP**, "Qué está mockeado y por qué" y "Decisiones de implementación".
- `OBSERVACIONES.md` consolidado (§13.5).
- **Compuerta final:** recorrer las 20 casillas de §12 una por una con evidencia ejecutada.

---

## Archivos críticos

| Ruta | Por qué es crítica |
|---|---|
| `libs/solventa_common/metrics.py` | Define todas las métricas de §6; el modo multiproceso decide si los datos del experimento son completos o la mitad |
| `libs/solventa_common/messaging.py` | Única abstracción sobre `pika`; contiene single-active-consumer, reply queues, backoff y cierre limpio |
| `libs/solventa_common/http_client.py` | Sede del bulkhead (SP-5) y del timeout (SP-1) |
| `infra/kong/kong.yml` | Frontera de medición del 5xx y origen del correlation_id; `retries: 0` es crítico para la validez de SP-2 |
| `services/financial-profiler/app/` | Componente bajo prueba: timeout + breaker + caché + señal del monitor. SP-1..SP-4 viven aquí |
| `services/cotizacion/app/` | Invariante duro (nunca 5xx), registro de correlación, segundo frente de bulkhead |
| `services/mock-openfinance/app/` | El instrumento; su `/health` mentiroso es la base de SP-4 |
| `scripts/collect_results.py` | Convierte series en las tablas del informe; §7.1 es obligatoria en toda corrida |
| `infra/prometheus/prometheus.yml` | `scrape_interval: 1s` — sin esto SP-2 no es medible |

## Verificación end-to-end

1. `docker compose up -d && scripts/smoke_test.sh` — pasa sin intervención manual.
2. Mock en `normal`: un curl al gateway devuelve 200 con `profile_quality: FRESH`.
3. **Premisa:** `docker compose -f docker-compose.yml -f docker-compose.baseline.yml` con `error_5xx` → ~100 % de 5xx y saturación de workers; el mismo fixture en `treatment` → 0 % de 5xx.
4. Los seis dashboards de Grafana (`localhost:3000`) cargan con datos.
5. `docker kill procesador-cotizacion-primary` → sin pérdida de mensajes, `takeover_events_total` incrementa.
6. `docker build -f services/<x>/Dockerfile .` funciona para los ocho servicios propios de forma independiente (Kong es imagen oficial, sin build).
7. `scripts/run_experiment.sh SP-2` → `results/sp_2_matrix.md` poblado.
8. Barrido completo `SP-0 … SP-5` y revisión de las 20 casillas de §12.

## Reglas permanentes durante la ejecución

- **No rediseñar** (§13.5). Debilidad detectada → `OBSERVACIONES.md` + la métrica que la expondría. Nunca una corrección silenciosa: destruye la validez de la medición.
- **Cada táctica lleva su comentario** `# TÁCTICA: <nombre> — SP-N` (§13.7). El repo es evidencia de un informe académico.
- **Ningún hostname, puerto ni parámetro de táctica hardcodeado** (§5.3).
- **Ningún flag que no sea variable independiente o de control de algún SP** (§0). Si no sirve a un SP, valor razonable en duro.
- **Sin almacén de idempotencia ni deduplicación** (§2.3, §3.5). El registro de correlación descarta las réplicas como efecto del patrón.
- Preguntar antes de expandir el alcance más allá de §2.1/§2.2 (§13.6).
