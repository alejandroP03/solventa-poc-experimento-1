# cotizacion — orquestador del journey (§2.1).
#
#   docker build -f services/cotizacion/Dockerfile -t solventa/cotizacion .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

WORKDIR /app

# La librería compartida primero: cambia menos que el código del servicio, así
# que su capa se reaprovecha entre builds.
COPY libs/solventa_common /app/libs/solventa_common
RUN pip install --no-cache-dir /app/libs/solventa_common

COPY services/cotizacion/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/cotizacion/app /app/app

# Usuario no-root (§8.1).
RUN useradd --create-home --uid 10001 solventa \
    && mkdir -p /tmp/prometheus \
    && chown -R solventa:solventa /tmp/prometheus /app
USER solventa

EXPOSE 8082

# Forma exec para que SIGTERM llegue a Gunicorn y el apagado sea limpio (§8.2).
CMD ["gunicorn", "-c", "python:solventa_common.gunicorn_conf", "app.main:app"]
