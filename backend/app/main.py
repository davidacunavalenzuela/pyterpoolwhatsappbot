"""Aplicación FastAPI: API administrativa + webhook de WhatsApp."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import text

from app.api import admin_auth, admin_conversations, admin_docs, admin_misc, admin_orders, admin_products
from app.api.webhook import router as webhook_router
from app.config import settings
from app.db import get_session
from app.logging import configure_logging, get_logger
from app.services.llm import llm
from app.services.whatsapp import whatsapp

log = get_logger("main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Arranque/cierre ordenado de clientes HTTP y avisos de configuración."""
    configure_logging()
    log.info("api_starting", env=settings.app_env)
    if not settings.wa_access_token:
        log.warning("wa_access_token_missing")
    if not settings.groq_api_key:
        log.warning("groq_api_key_missing")
    if not settings.gemini_api_key:
        log.info("gemini_missing_vision_disabled")
    yield
    # Cerrar keep-alives evita conexiones colgadas al reiniciar el contenedor.
    await whatsapp.aclose()
    await llm.aclose()
    log.info("api_stopped")


app = FastAPI(
    title="WhatsApp Sales Assistant",
    version="0.1.0",
    lifespan=lifespan,
    # OpenAPI solo en desarrollo: en producción no se expone superficie extra.
    docs_url="/api/docs" if not settings.is_prod else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if not settings.is_prod else None,
)

app.include_router(webhook_router)
app.include_router(admin_auth.router)
app.include_router(admin_products.router)
app.include_router(admin_products.categories_router)
app.include_router(admin_orders.router)
app.include_router(admin_conversations.router)
app.include_router(admin_docs.router)
app.include_router(admin_misc.router)


@app.get("/health")
async def health(session=Depends(get_session)) -> dict[str, str]:
    """Healthcheck real: verifica que la base responda, no solo el proceso."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
