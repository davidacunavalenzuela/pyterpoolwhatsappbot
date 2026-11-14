"""Motor y sesiones de base de datos (SQLAlchemy 2.0 async)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# `pool_pre_ping` evita reutilizar conexiones muertas tras un reinicio de
# Postgres (habitual en un servidor pequeño que reinicia servicios).
# El pool se mantiene pequeño: la API y el worker suman como máximo ~20
# conexiones frente a max_connections=50.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=1800,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia FastAPI: una sesión por request, con cierre garantizado."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transacción explícita para worker/CLI: commit al salir, rollback si falla."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
