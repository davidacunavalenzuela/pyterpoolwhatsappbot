"""Pedidos: listado, detalle y cambios de estado."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import SessionDep, require_admin, require_csrf
from app.models import Customer, Order
from app.schemas import OrderListOut, OrderOut, OrderPatch
from app.services.orders import SalesError, set_order_status

router = APIRouter(
    prefix="/api/orders",
    tags=["pedidos"],
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)


@router.get("", response_model=OrderListOut)
async def list_orders(
    session: SessionDep,
    status_filter: str | None = Query(default=None, alias="status", max_length=12),
    q: str = Query(default="", max_length=60),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> OrderListOut:
    """Lista pedidos, opcionalmente filtrados por estado o código."""
    stmt = select(Order)
    count_stmt = select(func.count(Order.id))
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)
        count_stmt = count_stmt.where(Order.status == status_filter)
    if q:
        stmt = stmt.where(Order.code.ilike(f"%{q.strip().upper()}%"))
        count_stmt = count_stmt.where(Order.code.ilike(f"%{q.strip().upper()}%"))

    total = (await session.execute(count_stmt)).scalar_one()
    orders = (
        await session.execute(stmt.order_by(Order.id.desc()).limit(limit).offset(offset))
    ).scalars().all()

    # `items` usa lazy="selectin": se carga en una sola consulta extra para todos
    # los pedidos, así que no hace falta un refresh por fila (evita N+1).
    result = [OrderOut.model_validate(order) for order in orders]
    return OrderListOut(items=result, total=total)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, session: SessionDep) -> Order:
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado.")
    await session.refresh(order, attribute_names=["items"])
    return order


@router.patch("/{order_id}", response_model=OrderOut)
async def update_order(order_id: int, payload: OrderPatch, session: SessionDep) -> Order:
    """Cambia estado/notas. Cancelar una venta confirmada repone el stock."""
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado.")

    try:
        if payload.status is not None and payload.status != order.status:
            order = await set_order_status(session, order_id, payload.status)
        if payload.notes is not None:
            order.notes = payload.notes
        if payload.shipping_address is not None:
            order.shipping_address = payload.shipping_address
        await session.commit()
    except SalesError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    await session.refresh(order, attribute_names=["items"])
    return order


@router.get("/{order_id}/customer")
async def order_customer(order_id: int, session: SessionDep) -> dict:
    """Datos mínimos del cliente para gestionar el despacho."""
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado.")
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cliente no encontrado.")
    return {
        "id": customer.id,
        "name": customer.name,
        "wa_id": customer.wa_id,
        "phone": customer.phone,
        "notes": customer.notes,
    }
