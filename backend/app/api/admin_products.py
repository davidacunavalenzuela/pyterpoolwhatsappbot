"""CRUD de catálogo, categorías y stock (panel administrativo)."""

from __future__ import annotations

import re
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep, require_admin, require_csrf
from app.models import Category, InboundEvent, Product, StockMovement
from app.schemas import (
    CategoryIn,
    CategoryOut,
    ProductIn,
    ProductListOut,
    ProductOut,
    ProductPatch,
    StockAdjustIn,
)

# Todos los endpoints requieren sesión; los métodos mutantes exigen además CSRF.
router = APIRouter(
    prefix="/api/products",
    tags=["productos"],
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)
categories_router = APIRouter(
    prefix="/api/categories",
    tags=["categorías"],
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)


def _money(value: float) -> Decimal:
    """Convierte float de la API a Decimal con 2 decimales, sin artefactos binarios."""
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "categoria"


async def _get_or_404(session: AsyncSession, product_id: int) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Producto no encontrado.")
    return product


async def _enqueue_catalog_reindex(session: AsyncSession) -> None:
    """Encola la reconstrucción del documento RAG del catálogo.

    El catálogo se indexa como un único documento con checksum fijo; si no se
    reconstruye tras crear, editar o desactivar un producto, la búsqueda
    documental seguiría citando precios o modos de uso viejos. Se hace vía
    worker para no bloquear la petición cargando el modelo de embeddings.
    """
    session.add(
        InboundEvent(kind="ingest", payload={"product_catalog": True}, wa_message_id=None)
    )


@router.get("", response_model=ProductListOut)
async def list_products(
    session: SessionDep,
    q: str = Query(default="", max_length=100),
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ProductListOut:
    """Lista paginada con búsqueda por nombre/SKU."""
    stmt = select(Product)
    count_stmt = select(func.count(Product.id))
    if q:
        pattern = f"%{q.strip()}%"
        condition = Product.name.ilike(pattern) | Product.sku.ilike(pattern)
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    if active is not None:
        stmt = stmt.where(Product.active.is_(active))
        count_stmt = count_stmt.where(Product.active.is_(active))

    total = (await session.execute(count_stmt)).scalar_one()
    items = (
        await session.execute(stmt.order_by(Product.name).limit(limit).offset(offset))
    ).scalars().all()
    return ProductListOut(items=[ProductOut.model_validate(p) for p in items], total=total)


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductIn, session: SessionDep) -> Product:
    """Crea un producto. El SKU se normaliza y debe ser único."""
    product = Product(
        sku=payload.sku,
        name=payload.name.strip(),
        category_id=payload.category_id,
        description=payload.description.strip(),
        usage_summary=payload.usage_summary.strip(),
        price=_money(payload.price),
        currency=payload.currency.upper(),
        stock=payload.stock,
        active=payload.active,
        image_url=payload.image_url,
    )
    session.add(product)
    await _enqueue_catalog_reindex(session)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe un producto con ese SKU.") from None
    await session.refresh(product)
    return product


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: int, session: SessionDep) -> Product:
    return await _get_or_404(session, product_id)


@router.patch("/{product_id}", response_model=ProductOut)
async def update_product(product_id: int, payload: ProductPatch, session: SessionDep) -> Product:
    """Actualiza solo los campos enviados (PATCH real, no reemplazo)."""
    product = await _get_or_404(session, product_id)
    data = payload.model_dump(exclude_unset=True)
    if "price" in data and data["price"] is not None:
        data["price"] = _money(data["price"])
    if "currency" in data and data["currency"]:
        data["currency"] = data["currency"].upper()
    for key, value in data.items():
        setattr(product, key, value)
    await _enqueue_catalog_reindex(session)
    await session.commit()
    await session.refresh(product)
    return product


@router.delete("/{product_id}")
async def deactivate_product(product_id: int, session: SessionDep) -> dict[str, bool]:
    """Baja lógica: preserva pedidos históricos y evita romper FKs."""
    product = await _get_or_404(session, product_id)
    product.active = False
    await _enqueue_catalog_reindex(session)
    await session.commit()
    return {"ok": True}


@router.post("/{product_id}/stock", response_model=ProductOut)
async def adjust_stock(product_id: int, payload: StockAdjustIn, session: SessionDep) -> Product:
    """Ajuste manual de inventario con trazabilidad en `stock_movements`."""
    product = await _get_or_404(session, product_id)
    if product.stock + payload.delta < 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"El ajuste dejaría el stock negativo (actual: {product.stock}).",
        )
    product.stock += payload.delta
    session.add(
        StockMovement(
            product_id=product.id,
            delta=payload.delta,
            reason=payload.reason[:120] or "ajuste manual",
        )
    )
    await session.commit()
    await session.refresh(product)
    return product


# ---------------------------------------------------------------------------
# Categorías
# ---------------------------------------------------------------------------
@categories_router.get("", response_model=list[CategoryOut])
async def list_categories(session: SessionDep) -> list[Category]:
    return list(
        (await session.execute(select(Category).order_by(Category.sort_order, Category.name)))
        .scalars()
        .all()
    )


@categories_router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryIn, session: SessionDep) -> Category:
    category = Category(name=payload.name.strip(), slug=_slugify(payload.name), sort_order=payload.sort_order)
    session.add(category)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Esa categoría ya existe.") from None
    await session.refresh(category)
    return category
