# financial-profiler — el componente bajo prueba (kickoff §2.1).
#
#   docker build -f services/financial-profiler/Dockerfile -t solventa/financial-profiler .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    # Un worker y ocho hilos, en lugar de dos workers de cuatro hilos: la
    # capacidad total es la misma, pero el estado del circuit breaker y la señal
    # del Monitor viven en memoria de proceso. Con dos workers habría dos
    # breakers independientes, el umbral efectivo de apertura sería el doble de
    # BREAKER_FAIL_MAX y la señal del Monitor alcanzaría solo a la mitad del
    # tráfico. El modelo funcional muestra UN circuit breaker.
    # Ver OBSERVACIONES.md, OBS-05.
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=8

WORKDIR /app

COPY libs/solventa_common /app/libs/solventa_common
RUN pip install --no-cache-dir /app/libs/solventa_common

COPY services/financial-profiler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/financial-profiler/app /app/app

RUN useradd --create-home --uid 10001 solventa \
    && mkdir -p /tmp/prometheus \
    && chown -R solventa:solventa /tmp/prometheus /app
USER solventa

EXPOSE 8085

CMD ["gunicorn", "-c", "python:solventa_common.gunicorn_conf", "app.main:app"]
