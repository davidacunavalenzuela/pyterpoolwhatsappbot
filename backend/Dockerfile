# =============================================================================
# Imagen única de backend, usada tanto por la API como por el worker.
#
# Decisiones:
#  - python:3.12-slim: wheels estables para asyncpg/onnxruntime/Pillow. El host
#    tiene 3.14, pero dentro del contenedor mandamos nosotros la versión.
#  - Se copian primero las dependencias para aprovechar la cache de capas.
#  - Usuario no root (uid 10001) y directorios de datos con permisos previos.
#  - libgomp1 es requerido por onnxruntime (fastembed) en Debian slim.
# =============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Los modelos de embeddings se cachean en /models (volumen) y los manuales en
# /app/docs (bind mount), así que ambos deben ser escribibles por el usuario
# sin privilegios.
RUN useradd --uid 10001 --create-home appuser \
    && mkdir -p /models /app/docs \
    && chown -R appuser:appuser /app /models \
    && chmod +x /app/entrypoint.sh

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
