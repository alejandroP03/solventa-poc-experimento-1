# monitor — detección proactiva por Ping-Echo (kickoff §2.1, §3.3).
#
#   docker build -f services/monitor/Dockerfile -t solventa/monitor .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    # Un solo worker: con dos habría DOS lazos de Ping-Echo sondeando en
    # paralelo, el proveedor recibiría el doble de pings de los que
    # MONITOR_INTERVAL_MS declara, y la latencia de detección que SP-4 mide
    # dependería del número de procesos en lugar de la variable configurada.
    #
    # .env no define esta variable, así que el `env_file:` de compose no puede
    # pisarla como sí pisa a GUNICORN_WORKERS. Ver OBSERVACIONES.md, OBS-05.
    SOLVENTA_SINGLE_PROCESS=true \
    GUNICORN_THREADS=4

WORKDIR /app

COPY libs/solventa_common /app/libs/solventa_common
RUN pip install --no-cache-dir /app/libs/solventa_common

COPY services/monitor/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/monitor/app /app/app

RUN useradd --create-home --uid 10001 solventa \
    && mkdir -p /tmp/prometheus \
    && chown -R solventa:solventa /tmp/prometheus /app
USER solventa

EXPOSE 8086

CMD ["gunicorn", "-c", "python:solventa_common.gunicorn_conf", "app.main:app"]
