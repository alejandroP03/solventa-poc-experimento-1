# Solventa POC - Experimento 1

POC para medir la degradacion elegante de la cotizacion embebida cuando el
proveedor simulado de Open Finance falla. El objetivo del experimento es validar
que, en modo `treatment`, el journey no devuelve 5xx por indisponibilidad de Open
Finance y degrada `profile_quality` a `DEGRADED` o `DEFAULT`.

## Requisitos

- Docker Desktop corriendo.
- Docker Compose v2 (`docker compose`).
- Git Bash, WSL o una terminal compatible con `bash`.
- Python 3 disponible como `python3` para los scripts.
- `curl`.

En Windows, se recomienda ejecutar los comandos desde Git Bash en la raiz del
repositorio:

```bash
cd /c/GIT/Arquitectura/solventa-poc-experimento-1
```

## Servicios

El compose levanta el stack principal:

| Servicio | Puerto host | Uso |
|---|---:|---|
| Kong | 8080 | Entrada del socio: `/v1/quotes` y `/v1/provider-quote` |
| Kong status | 8100 | Estado y metricas de Kong |
| socio-distribucion | 8081 | Provider simulado |
| cotizacion | 8082 | Orquestador de cotizacion |
| procesador-cotizacion primary | 8083 | Consumidor principal |
| procesador-cotizacion backup | 8084 | Consumidor de respaldo |
| financial-profiler | 8085 | Perfilamiento, breaker y cache |
| monitor | 8086 | Ping-Echo hacia Open Finance |
| mock-openfinance | 8090 | Proveedor externo simulado e inyector de fallos |
| Redis | 6379 | Cache de perfiles |
| RabbitMQ | 5672 | Broker AMQP |
| RabbitMQ Management | 15672 | Consola de RabbitMQ |
| RabbitMQ Prometheus | 15692 | Exporter de RabbitMQ |
| Prometheus | 9090 | Series del experimento |

## Levantar Todo

Crear `.env` desde la plantilla:

```bash
make env
```

Levantar el stack en modo `treatment`:

```bash
make up
```

Equivalente sin Make:

```bash
cp .env.example .env
docker compose up -d --build
docker compose restart kong
```

Verificar contenedores:

```bash
make ps
```

Probar el journey end-to-end:

```bash
make smoke
```

La prueba debe devolver una cotizacion con `profile_quality` normalmente en
`FRESH` y status HTTP menor a 500.

## Modo Baseline

El baseline es el control de SP-0. En este modo se desactivan las tacticas:
sin cache, sin breaker, sin cola y sin bulkhead. Debe propagar fallos cuando
Open Finance esta caido.

```bash
make baseline
```

Equivalente:

```bash
docker compose -f docker-compose.yml -f docker-compose.baseline.yml up -d --build
docker compose restart kong
```

## Comandos Utiles

Ver logs de todo:

```bash
make logs
```

Ver logs de un servicio:

```bash
make logs S=cotizacion
```

Reiniciar limpio:

```bash
make restart
```

Bajar contenedores y volumenes:

```bash
make down
```

Limpiar resultados locales:

```bash
make clean
```

Ejecutar tests unitarios:

```bash
make test
```

## Inyectar Fallos Manualmente

El proveedor simulado se controla por `scripts/inject_fault.sh`.

Volver a modo normal:

```bash
make reset
```

Inyectar 5xx:

```bash
make fault MODE=error_5xx
```

Inyectar lentitud:

```bash
make fault MODE=slow MS=1500
```

Inyectar timeout:

```bash
make fault MODE=timeout
```

Inyectar fallos intermitentes:

```bash
make fault MODE=flaky RATE=0.5
```

Con duracion automatica:

```bash
make fault MODE=error_5xx DUR=120
```

Modos disponibles:

| Modo | Comportamiento |
|---|---|
| `normal` | Open Finance responde sano |
| `slow` | Endpoint de negocio lento, `/health` responde sano |
| `error_5xx` | Endpoint de negocio y `/health` devuelven error |
| `timeout` | Endpoint no responde a tiempo |
| `flaky` | Fallo intermitente segun `RATE`, `/health` responde sano |

## Datos de Carga

Generar el dataset deterministico de 1.000 cotizaciones:

```bash
python3 scripts/seed_data.py
```

Generar dataset y precargar Redis segun `CACHE_PRELOAD_RATIO`:

```bash
make seed
```

El archivo generado queda en:

```text
load/k6/data/quotes.json
```

Ese archivo esta ignorado por Git porque se puede regenerar.

## Ejecutar Experimentos

Cada sub-experimento se ejecuta con:

```bash
make exp SP=SP-2
```

O directamente:

```bash
scripts/run_experiment.sh SP-2
```

Para ejecutar la variante ampliada de un SP:

```bash
make exp SP=SP-2 FULL=1
```

O:

```bash
scripts/run_experiment.sh SP-2 --full
```

SP disponibles:

| SP | Que mide |
|---|---|
| `SP-0` | Control: treatment vs baseline ante 5xx |
| `SP-1` | Timeout hacia Open Finance |
| `SP-2` | Circuit breaker: umbral y ventana half-open |
| `SP-3` | Cache de perfil, TTL y cache fria |
| `SP-4` | Monitor proactivo vs conteo reactivo del breaker |
| `SP-5` | Bulkhead y aislamiento de la ruta Provider |

Cada caso sigue esta linea de tiempo:

| Ventana | Duracion | Estado |
|---|---:|---|
| Sano | 60 s | Open Finance en `normal` |
| Fallo | 120 s | Se inyecta el modo de fallo del caso |
| Recuperacion | 60 s aprox. | Open Finance vuelve a `normal` mientras termina k6 |

El script recrea el stack por cada combinacion, reinicia Kong para refrescar las
IPs de upstreams, ejecuta k6 y guarda metadata con timestamps exactos de
inyeccion y recuperacion.

## Resultados

Cada corrida crea un directorio:

```text
results/<run_id>/
```

Archivos principales:

| Archivo | Contenido |
|---|---|
| `metadata.json` | SP, caso, parametros, timestamps, codigo de salida de k6 |
| `seed.json` | Dataset y cantidad de perfiles precargados |
| `fault_injected.json` | Estado devuelto por el mock al inyectar fallo |
| `fault_recovered.json` | Estado devuelto al volver a normal |
| `k6.log` | Salida completa de k6 |
| `k6_summary.json` | Resumen JSON exportado por k6 |
| `metrics.csv` | Consultas relevantes extraidas de Prometheus |
| `summary.md` | Resumen legible de la corrida |

Ademas, el recolector actualiza una matriz por SP:

```text
results/sp_2_matrix.md
```

Para recolectar nuevamente una corrida existente:

```bash
make collect RUN=<run_id>
```

## Carga k6 Manual

Con el stack arriba, se puede ejecutar k6 manualmente:

```bash
docker compose -f docker-compose.yml -f docker-compose.load.yml --profile load run --rm k6
```

Tambien se puede correr una micro-carga de prueba:

```bash
docker run --rm --network solventa_default \
  -v "$PWD/load/k6:/scripts:ro" \
  -v "$PWD/results:/results" \
  -e BASE_URL=http://kong:8000 \
  -e LOAD_RPS=1 \
  -e PROVIDER_RPS=1 \
  -e LOAD_DURATION=5s \
  grafana/k6:0.54.0 run /scripts/quote_load.js \
  --summary-export /results/k6_summary.json
```

## URLs de Verificacion

Kong:

```bash
curl http://localhost:8100/status
```

Prometheus:

```text
http://localhost:9090
```

RabbitMQ Management:

```text
http://localhost:15672
```

Credenciales:

```text
usuario: solventa
password: solventa
```

Mock Open Finance:

```bash
curl http://localhost:8090/admin/state
```

Cotizacion por gateway:

```bash
curl -X POST http://localhost:8080/v1/quotes \
  -H "Content-Type: application/json" \
  -H "X-Partner-Id: partner-a" \
  -d '{"client_id":"CLI-0001","product_code":"VIAJE","insured_amount":1000000,"age":35,"city":"Bogota","partner_id":"partner-a"}'
```

Ruta Provider de control:

```bash
curl "http://localhost:8080/v1/provider-quote?product_code=VIAJE" \
  -H "X-Partner-Id: partner-a"
```

## Notas Operativas

- `run_experiment.sh` modifica `.env` durante la corrida y lo restaura al final.
- En baseline, un exit code distinto de cero en k6 puede ser el resultado
  esperado: demuestra que el control propaga el fallo.
- Si se recrea `cotizacion`, reinicia Kong con `make reload-gateway`; Kong puede
  conservar la IP anterior del upstream.
- `results/` se regenera con los scripts y no se versiona, salvo `.gitkeep`.
