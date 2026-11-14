"""Lógica de ventas: carrito borrador, confirmación y control de stock.

Regla de negocio clave: el stock no se reserva al agregar al carrito (evita
bloquear inventario por conversaciones abandonadas). Se descuenta de forma
atómica al confirmar, con `SELECT ... FOR UPDATE` para que dos clientes no
compren la última unidad al mismo tiempo.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderItem, Product, StockMovement


class SalesError(RuntimeError):
    """Error de negocio mostrable al cliente (sin trazas internas)."""


class ProductNotFound(SalesError):
    pass


class OutOfStock(SalesError):
    pass


class EmptyCart(SalesError):
    pass


def format_money(amount: Decimal | float, currency: str = "CLP") -> str:
    """Formatea montos para hablar por WhatsApp.

    CLP no usa decimales en la práctica; para el resto se muestran dos.
    """
    value = float(amount)
    if currency.upper() == "CLP":
        return f"${value:,.0f}".replace(",", ".")
    return f"${value:,.2f}"


def _new_code() -> str:
    """Código público corto y no secuencial (no revela volumen de ventas)."""
    return f"PED-{datetime.now(UTC):%y%m%d}-{secrets.token_hex(2).upper()}"


async def get_draft_order(session: AsyncSession, customer_id: int) -> Order | None:
    """Último pedido en borrador del cliente (el carrito activo)."""
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id, Order.status == "draft")
        .order_by(Order.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_draft(
    session: AsyncSession, customer_id: int, conversation_id: int | None
) -> Order:
    """Devuelve el borrador existente o crea uno nuevo con código único."""
    order = await get_draft_order(session, customer_id)
    if order is not None:
        if conversation_id and order.conversation_id is None:
            order.conversation_id = conversation_id
        return order

    for _ in range(5):
        order = Order(
            code=_new_code(),
            customer_id=customer_id,
            conversation_id=conversation_id,
            status="draft",
            total=Decimal("0.00"),
        )
        # SAVEPOINT: si el código colisiona, se deshace solo este INSERT y no la
        # transacción completa del mensaje que se está procesando.
        async with session.begin_nested():
            session.add(order)
            await session.flush()
        return order
    raise SalesError("No se pudo generar el código del pedido.")


async def _find_product(session: AsyncSession, sku_or_name: str) -> Product:
    """Busca por SKU exacto y, si no, por nombre aproximado."""
    needle = sku_or_name.strip().upper()
    product = (
        await session.execute(select(Product).where(Product.sku == needle))
    ).scalar_one_or_none()
    if product is not None:
        return product

    product = (
        await session.execute(
            select(Product)
            .where(Product.active.is_(True), Product.name.ilike(f"%{sku_or_name.strip()}%"))
            .order_by(Product.name)
            .limit(1)
        )
    ).scalar_one_or_none()
    if product is None:
        raise ProductNotFound(f"No encontré el producto «{sku_or_name}».")
    return product


def _recalculate(order: Order) -> None:
    order.total = sum((item.line_total for item in order.items), Decimal("0.00"))


async def add_item(session: AsyncSession, order: Order, sku_or_name: str, quantity: int) -> OrderItem:
    """Agrega (o incrementa) un producto en el carrito."""
    if quantity <= 0:
        raise SalesError("La cantidad debe ser mayor a cero.")
    product = await _find_product(session, sku_or_name)
    if not product.active:
        raise SalesError(f"«{product.name}» no está disponible por ahora.")

    # Consulta directa en lugar de `order.items`: en un carrito recién creado la
    # relación no está cargada y el acceso perezoso falla en contexto async.
    existing = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id, OrderItem.product_id == product.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.quantity += quantity
        existing.line_total = existing.unit_price * existing.quantity
        item = existing
    else:
        item = OrderItem(
            order=order,
            product_id=product.id,
            name_snapshot=product.name,
            sku_snapshot=product.sku,
            quantity=quantity,
            unit_price=product.price,
            line_total=product.price * quantity,
        )
        session.add(item)

    await session.flush()
    # `order.items` puede no reflejar aún el nuevo item; se recarga la relación.
    await session.refresh(order, attribute_names=["items"])
    _recalculate(order)
    await session.flush()
    return item


async def remove_item(session: AsyncSession, order: Order, sku: str) -> bool:
    """Quita una línea del carrito. Devuelve True si existía."""
    target = sku.strip().upper()
    removed = False
    for item in list(order.items):
        if item.sku_snapshot == target or item.name_snapshot.upper() == target:
            await session.delete(item)
            removed = True
    if removed:
        await session.flush()
        await session.refresh(order, attribute_names=["items"])
        _recalculate(order)
        await session.flush()
    return removed


async def confirm_order(
    session: AsyncSession, order_id: int, shipping_address: str = "", notes: str = ""
) -> Order:
    """Confirma el pedido descontando stock de forma atómica.

    Se bloquean las filas de producto con FOR UPDATE dentro de la misma
    transacción: si otro worker intenta confirmar en paralelo, espera y luego
    ve el stock ya descontado, evitando sobreventa.
    """
    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise SalesError("El pedido ya no existe.")
    if order.status != "draft":
        raise SalesError("Este pedido ya fue confirmado.")

    await session.refresh(order, attribute_names=["items"])
    if not order.items:
        raise EmptyCart("El carrito está vacío.")

    for item in order.items:
        if item.product_id is None:
            raise SalesError(f"«{item.name_snapshot}» ya no está en el catálogo.")
        product = (
            await session.execute(
                select(Product).where(Product.id == item.product_id).with_for_update()
            )
        ).scalar_one_or_none()
        if product is None or not product.active:
            raise SalesError(f"«{item.name_snapshot}» ya no está disponible.")
        if product.stock < item.quantity:
            raise OutOfStock(
                f"Solo quedan {product.stock} unidades de «{product.name}»."
                if product.stock > 0
                else f"«{product.name}» está agotado."
            )
        # Se congela el precio vigente al momento de confirmar.
        item.unit_price = product.price
        item.line_total = product.price * item.quantity
        product.stock -= item.quantity
        session.add(
            StockMovement(
                product_id=product.id,
                delta=-item.quantity,
                reason=f"Venta {order.code}",
                order_id=order.id,
            )
        )

    _recalculate(order)
    order.status = "confirmed"
    order.confirmed_at = datetime.now(UTC)
    if shipping_address:
        order.shipping_address = shipping_address
    if notes:
        order.notes = notes
    await session.flush()
    return order


async def set_order_status(session: AsyncSession, order_id: int, status: str) -> Order:
    """Cambia el estado desde el panel, reponiendo stock si se cancela una venta."""
    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise SalesError("Pedido inexistente.")

    if status == "cancelled" and order.status in {"confirmed", "shipped"}:
        await session.refresh(order, attribute_names=["items"])
        for item in order.items:
            if item.product_id is None:
                continue
            product = (
                await session.execute(
                    select(Product).where(Product.id == item.product_id).with_for_update()
                )
            ).scalar_one_or_none()
            if product is None:
                continue
            product.stock += item.quantity
            session.add(
                StockMovement(
                    product_id=product.id,
                    delta=item.quantity,
                    reason=f"Cancelación {order.code}",
                    order_id=order.id,
                )
            )
    order.status = status
    await session.flush()
    return order


def cart_summary(order: Order) -> dict:
    """Representación compacta del carrito para las tools del agente."""
    return {
        "codigo": order.code,
        "items": [
            {
                "sku": item.sku_snapshot,
                "nombre": item.name_snapshot,
                "cantidad": item.quantity,
                "precio_unitario": float(item.unit_price),
                "subtotal": float(item.line_total),
            }
            for item in order.items
        ],
        "total": float(order.total),
    }


def order_summary(order: Order, currency: str = "CLP") -> dict:
    """Resumen de pedido con totales ya formateados para el cliente."""
    return {
        "codigo": order.code,
        "estado": order.status,
        "total": format_money(order.total, currency),
        "fecha": order.created_at.isoformat() if order.created_at else None,
        "items": [
            {
                "nombre": i.name_snapshot,
                "cantidad": i.quantity,
                "subtotal": format_money(i.line_total, currency),
            }
            for i in order.items
        ],
        "notas": order.notes,
        "direccion": order.shipping_address,
    }


__all__ = [
    "EmptyCart",
    "OutOfStock",
    "ProductNotFound",
    "SalesError",
    "add_item",
    "cart_summary",
    "confirm_order",
    "format_money",
    "get_draft_order",
    "get_or_create_draft",
    "order_summary",
    "remove_item",
    "set_order_status",
]
