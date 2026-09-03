# mock-openfinance — proveedor externo simulado (kickoff §4).
#
# El contexto de build es la raíz del repositorio, para poder copiar
# libs/solventa_common, pero solo se copia lo que este servicio necesita (§8.1).
#
#   docker build -f services/mock-openfinance/Dockerfile -t solventa/mock-openfinance .
#
# Para desplegar en ECS x86 hay que construir con --platform=linux/amd64; lo hace
# deploy/aws/ecr/build-and-push.sh. En local se construye nativo: emular amd64
# sobre Mac ARM pasaría por QEMU y contaminaría las mediciones de latencia.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    # Un solo worker: el estado de fallo inyectado vive en memoria y debe ser
    # ÚNICO. Con dos workers, POST /admin/mode alcanzaría a uno solo y la mitad
    # del tráfico seguiría viendo al proveedor sano — el fixture del experimento
    # sería aleatorio. La concurrencia del mock no es variable de ningún SP, así
    # que se fija (§0, regla rectora).
    #
    # Muchos hilos: en modo `timeout` cada petición retiene su hilo 60 s. El
    # tráfico que llega aquí está acotado aguas arriba por POOL_OPENFINANCE_MAX
    # (máx. 16) más los pings del Monitor, pero /admin/reset debe seguir
    # respondiendo para poder cerrar la ventana de indisponibilidad.
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=128

WORKDIR /app

# La librería compartida primero: cambia menos que el código del servicio, así
# que su capa se reaprovecha entre builds.
COPY libs/solventa_common /app/libs/solventa_common
RUN pip install --no-cache-dir /app/libs/solventa_common

COPY services/mock-openfinance/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/mock-openfinance/app /app/app

# Usuario no-root (§8.1). El directorio de métricas multiproceso vive en el
# sistema de archivos efímero del contenedor, no en un volumen: es estado de
# proceso, no de aplicación.
RUN useradd --create-home --uid 10001 solventa \
    && mkdir -p /tmp/prometheus \
    && chown -R solventa:solventa /tmp/prometheus /app
USER solventa

EXPOSE 8090

# Forma exec para que SIGTERM llegue a Gunicorn y el apagado sea limpio (§8.2).
CMD ["gunicorn", "-c", "python:solventa_common.gunicorn_conf", "app.main:app"]
