"""Herramientas que el LLM puede invocar (function calling).

Se mantienen en 10 como máximo: Groq recomienda no superar ese número de tools
porque el modelo empieza a elegir mal. Los resultados son JSON compacto en
español; los errores de negocio se devuelven como `{"error": ...}` para que el
modelo los explique al cliente sin exponer trazas.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import Category, Conversation, Customer, Order, Product
from app.services import orders as orders_service
from app.services.rag import search as rag_search

log = get_logger("tools")


@dataclass(slots=True)
class AgentContext:
    """Dependencias que reciben todas las tools."""

    session: AsyncSession
    conversation: Conversation
    customer: Customer
    settings: dict[str, Any]

    @property
    def currency(self) -> str:
        return self.settings.get("business", {}).get("currency", "CLP")


def _product_payload(product: Product, currency: str) -> dict[str, Any]:
    return {
        "sku": product.sku,
        "nombre": product.name,
        "precio": orders_service.format_money(product.price, currency),
        "disponible": product.stock > 0,
        "stock": product.stock,
        "descripcion": (product.description or "")[:160],
    }


async def search_products(
    ctx: AgentContext, query: str = "", category: str = "", max_price: float | None = None
) -> str:
    """Busca productos activos por texto, categoría y precio máximo."""
    stmt = select(Product).where(Product.active.is_(True))
    if query:
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(
            Product.name.ilike(pattern) | Product.description.ilike(pattern) | Product.sku.ilike(pattern)
        )
    if max_price is not None:
        stmt = stmt.where(Product.price <= max_price)
    if category:
        # El filtro va en SQL (no en Python) para que el LIMIT devuelva los
        # productos correctos de esa categoría.
        stmt = stmt.join(Category).where(Category.name.ilike(f"%{category.strip()}%"))
    products = (await ctx.session.execute(stmt.order_by(Product.name).limit(6))).scalars().all()
    return json.dumps(
        [_product_payload(p, ctx.currency) for p in products],
        ensure_ascii=False,
    )


async def get_product(ctx: AgentContext, sku_o_nombre: str) -> str:
    """Devuelve detalle y stock de un producto concreto."""
    needle = sku_o_nombre.strip().upper()
    product = (
        await ctx.session.execute(select(Product).where(Product.sku == needle))
    ).scalar_one_or_none()
    if product is None:
        product = (
            await ctx.session.execute(
                select(Product)
                .where(Product.active.is_(True), Product.name.ilike(f"%{sku_o_nombre.strip()}%"))
                .limit(1)
            )
        ).scalar_one_or_none()
    if product is None:
        return json.dumps({"error": f"No encontré «{sku_o_nombre}»."}, ensure_ascii=False)
    data = _product_payload(product, ctx.currency)
    data["modo_de_uso"] = (product.usage_summary or "")[:400]
    return json.dumps(data, ensure_ascii=False)


async def search_docs(ctx: AgentContext, consulta: str) -> str:
    """Busca en manuales y fichas de uso (RAG híbrido)."""
    hits = await rag_search(ctx.session, consulta, k=3)
    if not hits:
        return json.dumps({"resultados": [], "nota": "Sin documentación relevante."}, ensure_ascii=False)
    return json.dumps(
        {
            "resultados": [
                {"titulo": hit.title, "fragmento": hit.content[:500]} for hit in hits
            ]
        },
        ensure_ascii=False,
    )


async def add_to_cart(ctx: AgentContext, sku: str, cantidad: int = 1) -> str:
    """Agrega un producto al carrito borrador del cliente."""
    if not ctx.settings.get("sales", {}).get("orders_enabled", True):
        return json.dumps({"error": "Los pedidos están temporalmente deshabilitados."})
    try:
        order = await orders_service.get_or_create_draft(
            ctx.session, ctx.customer.id, ctx.conversation.id
        )
        await orders_service.add_item(ctx.session, order, sku, cantidad)
    except orders_service.SalesError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {"ok": True, "carrito": orders_service.cart_summary(order)},
        ensure_ascii=False,
    )


async def remove_from_cart(ctx: AgentContext, sku: str) -> str:
    """Quita un producto del carrito."""
    order = await orders_service.get_draft_order(ctx.session, ctx.customer.id)
    if order is None:
        return json.dumps({"error": "No hay un carrito activo."}, ensure_ascii=False)
    removed = await orders_service.remove_item(ctx.session, order, sku)
    return json.dumps(
        {
            "ok": removed,
            "carrito": orders_service.cart_summary(order),
        },
        ensure_ascii=False,
    )


async def view_cart(ctx: AgentContext) -> str:
    """Muestra el contenido actual del carrito."""
    order = await orders_service.get_draft_order(ctx.session, ctx.customer.id)
    if order is None:
        return json.dumps({"items": [], "total": 0, "nota": "Carrito vacío."}, ensure_ascii=False)
    summary = orders_service.cart_summary(order)
    summary["total_formateado"] = orders_service.format_money(order.total, ctx.currency)
    return json.dumps(summary, ensure_ascii=False)


async def confirm_order(
    ctx: AgentContext,
    nombre_cliente: str = "",
    direccion: str = "",
    notas: str = "",
) -> str:
    """Confirma el pedido y descuenta stock (solo tras confirmación explícita)."""
    if not ctx.settings.get("sales", {}).get("orders_enabled", True):
        return json.dumps({"error": "Los pedidos están temporalmente deshabilitados."})
    order = await orders_service.get_draft_order(ctx.session, ctx.customer.id)
    if order is None:
        return json.dumps({"error": "No hay un carrito activo para confirmar."}, ensure_ascii=False)

    if nombre_cliente and not ctx.customer.name:
        ctx.customer.name = nombre_cliente.strip()[:200]
    try:
        confirmed = await orders_service.confirm_order(
            ctx.session, order.id, shipping_address=direccion, notes=notas
        )
    except orders_service.OutOfStock as exc:
        return json.dumps({"error": str(exc), "stock_insuficiente": True}, ensure_ascii=False)
    except orders_service.SalesError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    await ctx.session.refresh(confirmed, attribute_names=["items"])
    return json.dumps(
        {
            "ok": True,
            "pedido": orders_service.order_summary(confirmed, ctx.currency),
            "siguiente_paso": "El equipo coordinará el pago y la entrega.",
        },
        ensure_ascii=False,
    )


async def get_order(ctx: AgentContext, codigo: str) -> str:
    """Consulta un pedido por código, solo si pertenece al cliente actual."""
    order = (
        await ctx.session.execute(
            select(Order).where(
                Order.code == codigo.strip().upper(), Order.customer_id == ctx.customer.id
            )
        )
    ).scalar_one_or_none()
    if order is None:
        return json.dumps({"error": "No encontré ese pedido en tu cuenta."}, ensure_ascii=False)
    await ctx.session.refresh(order, attribute_names=["items"])
    return json.dumps(orders_service.order_summary(order, ctx.currency), ensure_ascii=False)


async def list_my_orders(ctx: AgentContext) -> str:
    """Lista los últimos pedidos del cliente."""
    rows = (
        await ctx.session.execute(
            select(Order)
            .where(Order.customer_id == ctx.customer.id, Order.status != "draft")
            .order_by(Order.id.desc())
            .limit(5)
        )
    ).scalars().all()
    return json.dumps(
        [
            {
                "codigo": o.code,
                "estado": o.status,
                "total": orders_service.format_money(o.total, ctx.currency),
                "fecha": o.created_at.date().isoformat() if o.created_at else None,
            }
            for o in rows
        ],
        ensure_ascii=False,
    )


async def handoff_to_human(ctx: AgentContext, motivo: str = "") -> str:
    """Pausa el bot y deja la conversación a un humano."""
    ctx.conversation.status = "human"
    ctx.conversation.bot_enabled = False
    await ctx.session.flush()
    log.info(
        "handoff",
        conversation_id=ctx.conversation.id,
        customer_id=ctx.customer.id,
        reason=motivo[:200],
    )
    handoff_message = ctx.settings.get("agent", {}).get(
        "handoff_message",
        "Te derivo con una persona del equipo. En breve te escribimos por aquí.",
    )
    return json.dumps({"ok": True, "mensaje_al_cliente": handoff_message}, ensure_ascii=False)


ToolHandler = Callable[..., Awaitable[str]]

HANDLERS: dict[str, ToolHandler] = {
    "buscar_productos": search_products,
    "ver_producto": get_product,
    "buscar_documentacion": search_docs,
    "agregar_al_carrito": add_to_cart,
    "quitar_del_carrito": remove_from_cart,
    "ver_carrito": view_cart,
    "confirmar_pedido": confirm_order,
    "consultar_pedido": get_order,
    "mis_pedidos": list_my_orders,
    "derivar_a_humano": handoff_to_human,
}

# Esquemas en formato OpenAI/Groq. Las descripciones son cortas a propósito:
# cada token del esquema consume presupuesto de los 8K TPM del free tier.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_productos",
            "description": "Busca productos del catálogo por texto o precio máximo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto a buscar."},
                    "category": {"type": "string"},
                    "max_price": {"type": "number"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ver_producto",
            "description": "Ver precio, stock y modo de uso de un producto por SKU o nombre.",
            "parameters": {
                "type": "object",
                "properties": {"sku_o_nombre": {"type": "string"}},
                "required": ["sku_o_nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_documentacion",
            "description": "Busca en manuales y fichas de uso cuando preguntan cómo usar algo.",
            "parameters": {
                "type": "object",
                "properties": {"consulta": {"type": "string"}},
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agregar_al_carrito",
            "description": "Agrega unidades de un producto al carrito.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "cantidad": {"type": "integer", "minimum": 1},
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quitar_del_carrito",
            "description": "Quita un producto del carrito por SKU.",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ver_carrito",
            "description": "Muestra el carrito actual con totales.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_pedido",
            "description": "Confirma el pedido. Usar SOLO cuando el cliente ya confirmó explícitamente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_cliente": {"type": "string"},
                    "direccion": {"type": "string"},
                    "notas": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_pedido",
            "description": "Consulta el estado de un pedido por su código.",
            "parameters": {
                "type": "object",
                "properties": {"codigo": {"type": "string"}},
                "required": ["codigo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mis_pedidos",
            "description": "Lista los últimos pedidos del cliente.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "derivar_a_humano",
            "description": "Deriva a una persona cuando hay reclamos o el bot no puede resolver.",
            "parameters": {
                "type": "object",
                "properties": {"motivo": {"type": "string"}},
                "required": [],
            },
        },
    },
]
