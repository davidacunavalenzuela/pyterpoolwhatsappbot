#!/bin/sh
# =============================================================================
# Punto de entrada del backend.
#
# Siempre aplica migraciones antes de arrancar (idempotente con Alembic). Con
# un solo contenedor de API no hay carrera de migraciones; si algún día se
# escala la API, habría que mover `alembic upgrade head` a un job aparte.
# =============================================================================
set -e

case "$1" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
    ;;
  api-dev)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app/app
    ;;
  worker)
    alembic upgrade head
    exec python -m app.worker
    ;;
  cli)
    shift
    alembic upgrade head
    exec python -m app.cli "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
