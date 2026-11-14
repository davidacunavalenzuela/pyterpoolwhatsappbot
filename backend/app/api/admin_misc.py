"""Dashboard de estadísticas y ajustes del negocio."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.deps import SessionDep, require_admin, require_csrf
from app.models import Conversation, Document, InboundEvent, Order, Product
from app.schemas import OrderOut, SettingsOut, SettingsPatch, StatsOut
from app.services.settings_service import load_settings, save_settings

router = APIRouter(
    prefix="/api",
    tags=["panel"],
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)

LOW_STOCK_THRESHOLD = 5


@router.get("/stats", response_model=StatsOut)
async def get_stats(session: SessionDep) -> StatsOut:
    """Métricas del día calculadas en la zona horaria del negocio.

    Se evita `CURRENT_DATE` de Postgres (UTC) y se construyen los límites en
    Python: de otro modo, a las 21:00 de Chile las ventas aparecerían como del
    día siguiente.
    """
    app_settings = await load_settings(session)
    tz_name = app_settings.get("business", {}).get("timezone", "America/Santiago")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Santiago")
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)

    revenue_stmt = select(func.coalesce(func.sum(Order.total), 0)).where(
        Order.status.in_(("confirmed", "shipped", "delivered")),
        Order.confirmed_at.is_not(None),
        Order.confirmed_at >= start_local,
    )
    revenue_today = float((await session.execute(revenue_stmt)).scalar_one() or 0)

    orders_today = (
        await session.execute(
            select(func.count(Order.id)).where(
                Order.confirmed_at.is_not(None), Order.confirmed_at >= start_local
            )
        )
    ).scalar_one()
    orders_draft = (
        await session.execute(select(func.count(Order.id)).where(Order.status == "draft"))
    ).scalar_one()
    conversations_open = (
        await session.execute(
            select(func.count(Conversation.id)).where(Conversation.status != "closed")
        )
    ).scalar_one()
    low_stock = (
        await session.execute(
            select(func.count(Product.id)).where(
                Product.active.is_(True), Product.stock <= LOW_STOCK_THRESHOLD
            )
        )
    ).scalar_one()
    documents_ready = (
        await session.execute(select(func.count(Document.id)).where(Document.status == "ready"))
    ).scalar_one()
    queue_pending = (
        await session.execute(
            select(func.count(InboundEvent.id)).where(InboundEvent.status == "pending")
        )
    ).scalar_one()

    recent_orders = [
        OrderOut.model_validate(order)
        for order in (await session.execute(select(Order).order_by(Order.id.desc()).limit(5))).scalars().all()
    ]

    return StatsOut(
        orders_today=orders_today,
        revenue_today=revenue_today,
        orders_draft=orders_draft,
        conversations_open=conversations_open,
        low_stock=low_stock,
        documents_ready=documents_ready,
        recent_orders=recent_orders,
        queue_pending=queue_pending,
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings(session: SessionDep) -> SettingsOut:
    return SettingsOut(settings=await load_settings(session))


@router.put("/settings", response_model=SettingsOut)
async def update_settings(payload: SettingsPatch, session: SessionDep) -> SettingsOut:
    """Guarda solo las secciones enviadas (merge por sección)."""
    patch = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    updated = await save_settings(session, patch)
    await session.commit()
    return SettingsOut(settings=updated)
