"""Esquemas Pydantic v2 de la API administrativa.

Se usan `float` para dinero en las respuestas JSON (comodidad en el panel) y
`Decimal` en la base de datos (precisión). La conversión ocurre en la capa API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=200)


class MeOut(ORMModel):
    id: int
    email: str
    role: str


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class CategoryOut(ORMModel):
    id: int
    name: str
    slug: str
    sort_order: int


class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    category_id: int | None = None
    description: str = ""
    usage_summary: str = ""
    price: float = Field(ge=0)
    currency: str = Field(default="CLP", min_length=3, max_length=3)
    stock: int = Field(default=0, ge=0)
    active: bool = True
    image_url: str | None = None

    @field_validator("sku")
    @classmethod
    def _normalize_sku(cls, value: str) -> str:
        # El SKU se usa como identificador en las tools del LLM: sin espacios ni
        # mayúsculas ambiguas.
        return value.strip().upper().replace(" ", "-")


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: int | None = None
    description: str | None = None
    usage_summary: str | None = None
    price: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    stock: int | None = Field(default=None, ge=0)
    active: bool | None = None
    image_url: str | None = None


class ProductOut(ORMModel):
    id: int
    sku: str
    name: str
    category_id: int | None
    description: str
    usage_summary: str
    price: float
    currency: str
    stock: int
    active: bool
    image_url: str | None
    created_at: datetime
    updated_at: datetime


class ProductListOut(BaseModel):
    items: list[ProductOut]
    total: int


class StockAdjustIn(BaseModel):
    delta: int = Field(description="Positivo para ingresar stock, negativo para descontar.")
    reason: str = Field(default="ajuste manual", max_length=120)


# ---------------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------------
class OrderItemOut(ORMModel):
    id: int
    product_id: int | None
    name_snapshot: str
    sku_snapshot: str
    quantity: int
    unit_price: float
    line_total: float


class OrderOut(ORMModel):
    id: int
    code: str
    customer_id: int
    conversation_id: int | None
    status: str
    notes: str
    shipping_address: str
    total: float
    created_at: datetime
    confirmed_at: datetime | None
    items: list[OrderItemOut] = []


class OrderListOut(BaseModel):
    items: list[OrderOut]
    total: int


class OrderPatch(BaseModel):
    status: Literal["draft", "confirmed", "shipped", "delivered", "cancelled"] | None = None
    notes: str | None = None
    shipping_address: str | None = None


# ---------------------------------------------------------------------------
# Conversaciones
# ---------------------------------------------------------------------------
class MessageOut(ORMModel):
    id: int
    direction: str
    msg_type: str
    body: str | None
    transcription: str | None
    status: str | None
    created_at: datetime


class ConversationOut(ORMModel):
    id: int
    customer_id: int
    customer_name: str | None = None
    customer_wa_id: str | None = None
    status: str
    bot_enabled: bool
    last_message_at: datetime | None
    unread: int = 0


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    total: int


class ToggleBotIn(BaseModel):
    bot_enabled: bool


class ManualMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Documentos (RAG)
# ---------------------------------------------------------------------------
class DocumentOut(ORMModel):
    id: int
    title: str
    source_type: str
    filename: str | None
    status: str
    chunk_count: int
    error: str | None
    created_at: datetime


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    total: int


# ---------------------------------------------------------------------------
# Ajustes / estadísticas
# ---------------------------------------------------------------------------
class SettingsOut(BaseModel):
    settings: dict[str, Any]


class SettingsPatch(BaseModel):
    business: dict[str, Any] | None = None
    agent: dict[str, Any] | None = None
    sales: dict[str, Any] | None = None


class StatsOut(BaseModel):
    orders_today: int
    revenue_today: float
    orders_draft: int
    conversations_open: int
    low_stock: int
    documents_ready: int
    recent_orders: list[OrderOut]
    queue_pending: int
