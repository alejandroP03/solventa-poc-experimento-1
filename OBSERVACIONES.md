# Observaciones sobre la arquitectura

> **Propósito.** El kickoff (§13.5) fija que el diseño de §3 proviene de los modelos de
> arquitectura ya entregados y **no se rediseña**. Cuando la implementación revela una
> debilidad, se registra aquí junto con la métrica que la expondría — nunca se corrige en
> silencio en el código, porque una corrección silenciosa destruye la validez de la medición.
>
> Un hallazgo negativo medido es un resultado válido y valioso del experimento.

Cada entrada sigue el formato:

- **Qué se observó** — el hecho, sin interpretación.
- **Por qué importa** — el efecto sobre el ASR, sobre un SP o sobre la validez de la medición.
- **Cómo se expone** — la métrica, el panel o el procedimiento que lo hace visible con datos.
- **Estado** — `abierta` | `medida` | `descartada`.

---

## OBS-01 — El presupuesto de 250 ms incluye un round-trip completo por broker

- **Qué se observó.** El diseño de §3.1 coloca un patrón request-reply sobre RabbitMQ
  (publicación + espera en cola + consumo + respuesta) dentro de un presupuesto de journey
  de `JOURNEY_LATENCY_BUDGET_MS=250`. El `REPLY_TIMEOUT_MS=900` es casi cuatro veces ese
  presupuesto, lo que indica que el propio diseño anticipa que la espera puede excederlo.
- **Por qué importa.** Si el sobrecosto del broker consume una fracción grande del
  presupuesto, el margen disponible para `OPENFINANCE_TIMEOUT_MS` (SP-1) se estrecha, y la
  decisión de SP-1 deja de ser independiente: queda acotada por una constante arquitectónica.
- **Cómo se expone.** `solventa_journey_stage_duration_seconds` con una fila propia para
  `broker_publish + queue_wait + broker_reply` en la descomposición de §7.1, presente en
  **toda** corrida incluida la sana. El número se reporta sin interpretarlo ni ajustar el
  diseño para favorecerlo.
- **Estado.** abierta — se resuelve con el primer barrido de SP-0.

---

## OBS-02 — La caché fría es un límite honesto de la táctica, no un defecto a corregir

- **Qué se observó.** Con `CACHE_PRELOAD_RATIO=0.0`, la táctica cumple el ASR **literalmente**
  (0 % de 5xx) y al mismo tiempo el 100 % de las cotizaciones sale con perfil `DEFAULT`.
- **Por qué importa.** La disponibilidad se sostiene y la precisión del pricing colapsa. El
  ASR-Disp-09 mide una sola dimensión (5xx) y es insensible a esta pérdida, así que un
  sistema que la incumple del todo puede pasar el criterio de aceptación.
- **Cómo se expone.** `solventa_quotes_total{profile_quality}` y la **cobertura de
  degradación** de SP-3 (% de cotizaciones con perfil real en lugar de valores por defecto).
- **Estado.** abierta — es el hallazgo esperado de SP-3 y debe quedar en el informe.

---

## OBS-03 — Riesgo de validez: el tráfico sano previo anula el escenario de caché fría

- **Qué se observó.** La línea de tiempo estándar de §7 corre 60 s de tráfico sano antes de
  inyectar el fallo. A `K6_RPS=50` son ~3.000 peticiones sobre un dataset de 1.000
  `client_id`: para cuando llega la ventana de indisponibilidad, **la caché está poblada por
  completo**, sin importar el valor de `CACHE_PRELOAD_RATIO`.
- **Por qué importa.** El escenario de caché fría (`CACHE_PRELOAD_RATIO=0.0`) sería
  inobservable, y con él el hallazgo que §7 SP-3 exige documentar (OBS-02).
- **Cómo se expone.** Mitigación en el instrumento, no en la arquitectura: para las corridas
  con preload `0.0`, `scripts/run_experiment.sh` purga las claves de perfil de Redis
  inmediatamente antes de la inyección y registra la purga con su timestamp en el
  `metadata.json` de la corrida, de modo que quede auditable qué se hizo y cuándo.
- **Estado.** abierta — pendiente de implementar en la Fase 12.

---

## OBS-08 — El bulkhead de esperas de réplica es inoperante con los valores de §5.1

- **Qué se observó.** Medido en la Fase 9 con el fixture `timeout` y
  `BREAKER_ENABLED=false`, comparando el p95 de la ruta Provider contra su línea base sana:

  | Configuración | p95 Provider | vs. línea base | Rechazos del pool |
  |---|---|---|---|
  | Línea base sana | 33 ms | — | — |
  | `BULKHEAD_ENABLED=true`, `POOL_PENDING_REPLIES_MAX=16` | **838 ms** | **×25** | **0** |

  Con el valor por defecto del kickoff, el bulkhead **nunca rechazó una sola petición** y
  la ruta Provider se degradó 25 veces.

- **Por qué importa.** El semáforo vive en memoria de proceso, igual que los hilos que
  pretende particionar. `cotizacion` corre con `GUNICORN_WORKERS=2` y
  `GUNICORN_THREADS=4`, así que **cada worker tiene 4 hilos y su propio pool de 16**: cuatro
  hilos nunca pueden agotar dieciséis permisos. El semáforo no puede bloquear, luego no
  puede rechazar, luego no puede liberar hilos para la ruta Provider.

  La consecuencia alcanza al diseño experimental: los tres valores que §5.1 propone para
  esta variable —`8 | 16 | 32`— son **todos** mayores que `GUNICORN_THREADS=4`. Es decir,
  **la variable independiente de SP-5 no tiene ningún efecto observable en ninguno de sus
  tres niveles**. La condición necesaria para que el bulkhead actúe es
  `POOL_PENDING_REPLIES_MAX < GUNICORN_THREADS`, que ningún valor propuesto cumple.

  Esto explica también por qué el daño viaja: la degradación de la ruta Provider no la
  produce el pool, la produce el agotamiento de los hilos de Gunicorn, y el pool tal como
  está dimensionado no interviene en absoluto.

- **Cómo se expone.** `solventa_pool_rejected_total{pool="pending_replies"}` constante en 0
  durante toda la ventana de saturación, junto al p95 de la ruta Provider degradándose. Las
  dos series a la vez son la prueba: hay daño y el mecanismo que debía contenerlo no se
  activó. La corrida de SP-5 debe incluir al menos un nivel con
  `POOL_PENDING_REPLIES_MAX < GUNICORN_THREADS` para poder contrastar contra el diseño
  as-is; se propone añadir `2` a la rejilla **como nivel de contraste**, dejando `8|16|32`
  para documentar que el diseño entregado no aísla.

- **No es un defecto de implementación.** `libs/solventa_common/tests/test_http_client.py`
  demuestra a nivel unitario que `BoundedPool` acepta hasta su máximo, rechaza el siguiente
  de forma inmediata y libera el slot incluso si el cuerpo lanza. El pool funciona; lo que
  no funciona es su **dimensionamiento** frente al recurso que pretende particionar.

- **No se corrige en el código.** El dimensionamiento viene de §5.1 y se respeta (§13.5). Lo
  que se reporta es que, con esos valores, la táctica de bulkhead sobre las esperas de
  réplica está declarada pero no operativa — y que el aislamiento observado entre la ruta
  Provider y la de perfilamiento proviene de la separación en contenedores distintos, no del
  bulkhead. Relacionado con [[OBS-05]]: en ambos casos, el estado por proceso hace que el
  valor efectivo de un parámetro difiera del que declara la configuración.

- **Pendiente.** La medición definitiva, con la línea de tiempo de §7 y la serie de control
  en paralelo, corresponde a k6 en las Fases 12–13. Lo establecido aquí es el mecanismo y su
  dirección; la rejilla completa de SP-5 aportará las cifras del informe.

- **Estado.** medida (el mecanismo) / pendiente (la rejilla completa con k6).

---

## OBS-07 — Kong cachea la IP del upstream: riesgo de invalidar toda la matriz

- **Qué se observó.** Tras recrear el contenedor de `cotizacion` (`docker compose up -d
  cotizacion`), Kong siguió enviando tráfico a la IP anterior y devolvió **502 de forma
  permanente**, mientras `cotizacion` respondía 200 correctamente en su puerto directo.
  Reiniciar Kong lo resolvió de inmediato.

- **Por qué importa.** Es el hallazgo más peligroso encontrado durante la construcción, y no
  es un problema de la arquitectura sino **del instrumento**. `run_experiment.sh` recrea
  servicios entre cada combinación de la matriz —del orden de 40 corridas—, así que toda
  corrida posterior a la primera habría reportado **100 % de 5xx en el gateway**. Ese es
  justo el número que decide si el ASR-Disp-09 se cumple. El informe habría concluido que la
  arquitectura falla catastróficamente cuando lo que fallaba era una caché de DNS.

  Un fallo del instrumento que produce exactamente la señal que el experimento busca es la
  peor clase de error posible: es indistinguible del resultado, y confirma la hipótesis
  contraria con evidencia fabricada.

- **Cómo se expone y se contiene.** `make reload-gateway` reinicia Kong y espera a que su
  `/status` responda; `make up` y `make baseline` lo invocan siempre al final, y
  `run_experiment.sh` debe invocarlo tras cada recreación de servicios. Además,
  `solventa:gateway_5xx_by_source:rate` (regla de registro sobre
  `kong_http_requests_total{source}`) separa los 5xx generados por Kong de los del upstream:
  si el problema reapareciera, aparecería como `source="kong"` y sería reconocible en el
  dashboard en lugar de contarse contra el ASR.

- **Estado.** medida y contenida. Es una consecuencia operativa de [[OBS-06]] que no existía
  con el gateway en Flask, y debe figurar en el informe como coste de esa decisión.

---

## OBS-06 — Desviación deliberada: Kong sustituye al `api-gateway` en Flask

- **Qué se observó.** Decisión del equipo durante la construcción, no un hallazgo de
  medición. Se registra aquí porque **contradice tres puntos declarados no negociables**:
  §1 (*"Python 3.11 exclusivamente"*, *"Flask, servido con Gunicorn"*), §2.1 (`api-gateway`
  como uno de los nueve servicios) y §12 (*"los nueve servicios se construyen de forma
  independiente"*). El repositorio es evidencia de un informe, así que la desviación debe
  ser rastreable y no descubrirse leyendo el compose.
- **Motivo.** Un gateway propio es un servicio más que mantener sin aportar nada al
  experimento: sus dos responsabilidades (§3.5) son generar el `X-Correlation-Id` y ser la
  frontera de medición del 5xx, y ambas son funciones estándar de un API gateway real. Kong
  3.7 en modo DB-less las cubre con configuración declarativa y sin código.
- **Qué se gana.**
  1. **`retries: 0` explícito.** El default de Kong son 5 reintentos. Dejarlo habría
     multiplicado por 5 la carga contra el upstream durante la ventana de indisponibilidad:
     el conteo del breaker cruzaría el umbral 5 veces antes y la ventana de detección
     medida en SP-2 sería artificialmente corta. La decisión queda ahora explícita en
     `infra/kong/kong.yml`, donde antes estaba implícita en no haberla escrito.
  2. **Buckets alineados con el experimento.** El histograma de Kong tiene bordes en
     25, 50, 80, 100, **250, 400, 700, 1000**, 2000 ms: coinciden con el presupuesto del
     journey (`JOURNEY_LATENCY_BUDGET_MS=250`) y con los tres valores contrastados de SP-1.
  3. **`source="kong"` frente a `source="service"`.** Kong distingue sus propios timeouts
     de los errores del upstream, lo que permite **demostrar** que un 5xx contado contra el
     ASR vino del sistema y no del gateway cortando antes de tiempo. Con el gateway en
     Flask esa distinción habría requerido instrumentación adicional.
  4. Un frente de saturación menos entre el socio y `cotizacion`: Kong es event-driven y no
     agota hilos, así que la degradación que SP-5 observe es atribuible a `cotizacion` y no
     al borde.
- **Qué se pierde.**
  1. La etapa `gateway` de `solventa_journey_stage_duration_seconds` (§6, §7.1) **cambia de
     definición**: pasa de ser el tramo medido dentro del proceso Flask a ser
     `kong_kong_latency_ms`, el tiempo de proceso propio de Kong sin el upstream. Es una
     definición más limpia, pero no es la misma serie y no comparte buckets con el resto de
     la descomposición. `collect_results.py` la reporta convertida a segundos y marcada
     como procedente de Kong.
  2. Las series del borde no nacen en el espacio de nombres `solventa_*`. Se reconcilian
     con reglas de registro de Prometheus (`infra/prometheus/rules/kong.yml`) para que
     dashboards y recolector consulten un único espacio de nombres, a costa de una
     indirección que hay que conocer al leer el dashboard.
  3. El criterio de aceptación de §12 pasa a leerse **ocho** servicios propios más Kong.
- **Estado.** medida — la desviación está implementada, documentada aquí y en la sección
  "Decisiones de implementación" del README.

---

## OBS-05 — El estado de las tácticas es estado de proceso, no de servicio

- **Qué se observó.** Detectado empíricamente al verificar el mock (Fase 2): con
  `GUNICORN_WORKERS=2`, `POST /admin/mode` solo alcanzaba a uno de los dos workers y la
  mitad del tráfico seguía viendo al proveedor sano. El mismo mecanismo afecta a **las
  tácticas bajo prueba**: el contador de fallos y el estado del `pybreaker` viven en la
  memoria de un proceso, igual que el registro de esperas de `cotizacion`.
- **Por qué importa.** Con N procesos hay N circuit breakers independientes:
  1. El umbral efectivo de apertura pasa a ser **N × `BREAKER_FAIL_MAX`**, así que la
     ventana de detección de SP-2 se multiplica por N sin que ninguna variable lo diga.
  2. La señal del Monitor (`POST /internal/dependency-health`, §3.3) llega a **un solo
     proceso**: los demás siguen propagando el fallo. SP-4 mediría una ventaja del Monitor
     artificialmente pequeña.
  3. La misma fragmentación ocurriría al escalar horizontalmente el servicio en ECS, que es
     el despliegue objetivo de §8 — no es un artefacto de Gunicorn.
- **Cómo se expone.** El modelo funcional del Cuaderno IV muestra **un** Circuit Breaker, no
  uno por proceso. Para ser fiel a esa vista, los servicios que albergan estado de táctica o
  de correlación corren con **un worker** y concurrencia por hilos: `financial-profiler`
  (breaker y señal), `monitor` (lazo periódico), `procesador-cotizacion` (consumidor AMQP),
  `health-manager`, `notificador` y `mock-openfinance` (estado del fixture).
  `api-gateway`, `socio-distribucion` y `cotizacion` conservan `GUNICORN_WORKERS=2`, que es
  donde SP-5 mide la saturación.
  La debilidad **no se corrige**: se documenta. La métrica que la expondría es
  `solventa_circuit_breaker_transitions_total` desagregada por instancia (`pod`/`pid`),
  comparando la ventana de detección con 1 y con N réplicas del profiler. Queda propuesta
  como extensión, fuera del alcance de §2.1.
- **Estado.** medida (el mecanismo) / abierta (su cuantificación con N réplicas).

---

## OBS-04 — `solventa_detection_latency_seconds` mide algo distinto de la latencia de detección real

- **Qué se observó.** El `financial-profiler` no conoce el instante en que se inyectó el
  fallo en el mock. In-process solo puede medir *primer fallo observado → corte efectivo*,
  que es una cota inferior de la latencia de detección.
- **Por qué importa.** SP-2 y SP-4 comparan fuentes de detección; usar la cota inferior como
  si fuera la latencia real subestimaría el número de peticiones afectadas en la ventana.
- **Cómo se expone.** Se reportan **dos** cantidades por separado: la métrica in-process
  (`solventa_detection_latency_seconds{source}`) y la latencia desde la inyección real,
  calculada offline por `collect_results.py` cruzando los timestamps de `metadata.json` con
  las series de Prometheus. El informe usa la segunda; la primera sirve de contraste.
- **Estado.** abierta — pendiente de implementar en la Fase 13.
