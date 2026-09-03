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
