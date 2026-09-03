# procesador-cotizacion — consumidor de la cola de cotizaciones (kickoff §2.1).
#
# Se despliega dos veces con el mismo Dockerfile y distinto ROLE (PRIMARY /
# BACKUP), en modo single active consumer.
#
#   docker build -f services/procesador-cotizacion/Dockerfile -t solventa/procesador-cotizacion .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    # Un solo worker. Con dos habría DOS consumidores AMQP registrados por
    # instancia y la semántica PRIMARY/BACKUP de single-active-consumer dejaría
    # de significar lo que dice el modelo de despliegue: el "backup" ya estaría
    # consumiendo desde el principio en la mitad de los casos.
    #
    # El nombre de la variable importa: .env no la define, así que el `env_file:`
    # de compose no puede pisarla como sí pisa a GUNICORN_WORKERS.
    SOLVENTA_SINGLE_PROCESS=true \
    GUNICORN_THREADS=4

WORKDIR /app

COPY libs/solventa_common /app/libs/solventa_common
RUN pip install --no-cache-dir /app/libs/solventa_common

COPY services/procesador-cotizacion/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/procesador-cotizacion/app /app/app

RUN useradd --create-home --uid 10001 solventa \
    && mkdir -p /tmp/prometheus \
    && chown -R solventa:solventa /tmp/prometheus /app
USER solventa

EXPOSE 8083

# Forma exec para que SIGTERM llegue a Gunicorn: un consumidor que no cierra
# limpio retrasa la promoción del BACKUP por single-active-consumer (§8.2).
CMD ["gunicorn", "-c", "python:solventa_common.gunicorn_conf", "app.main:app"]
