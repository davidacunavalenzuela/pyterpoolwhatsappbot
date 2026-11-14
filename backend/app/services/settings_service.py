"""Ajustes del negocio editables desde el panel (tabla `app_settings`)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

# Valores por defecto. La fusión es por sección: si el panel guarda solo
# `agent.tone`, el resto de claves se conserva.
DEFAULT_SETTINGS: dict[str, Any] = {
    "business": {
        "name": "Mi Negocio",
        "description": "Tienda de productos de uso cotidiano.",
        "hours": "Lunes a viernes, 10:00 a 19:00",
        "address": "",
        "phone": "",
        "currency": "CLP",
        "timezone": "America/Santiago",
        "delivery": "Despacho a todo el país. El costo se confirma al cerrar el pedido.",
        "payment": "Transferencia bancaria o pago contra entrega.",
    },
    "agent": {
        "tone": "cercano, claro y breve",
        "language": "español de Chile",
        "handoff_message": (
            "Te derivo con una persona del equipo para que te ayude mejor. "
            "En breve te escribimos por aquí."
        ),
        "max_products_in_reply": 5,
    },
    "sales": {
        # Interruptor de emergencia: apaga la creación de pedidos sin tocar código.
        "orders_enabled": True,
        # El bot debe pedir confirmación explícita antes de cerrar el pedido.
        "require_confirmation": True,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Fusión superficial por sección (los valores son diccionarios planos)."""
    result = deepcopy(base)
    for section, values in override.items():
        if isinstance(values, dict) and isinstance(result.get(section), dict):
            result[section].update(values)
        else:
            result[section] = values
    return result


async def load_settings(session: AsyncSession) -> dict[str, Any]:
    """Devuelve los ajustes actuales fusionados con los valores por defecto."""
    rows = (await session.execute(select(AppSetting))).scalars().all()
    stored = {row.key: row.value for row in rows}
    merged = deepcopy(DEFAULT_SETTINGS)
    for section, values in stored.items():
        if isinstance(values, dict) and section in merged and isinstance(merged[section], dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


async def save_settings(session: AsyncSession, patch: dict[str, Any]) -> dict[str, Any]:
    """Aplica un parche por secciones y persiste solo lo enviado.

    Se guarda por sección (no un único JSON) para que dos administradores
    editando áreas distintas no se pisen.
    """
    for section, values in patch.items():
        if not isinstance(values, dict):
            continue
        row = await session.get(AppSetting, section)
        if row is None:
            merged = deepcopy(DEFAULT_SETTINGS.get(section, {}))
            if isinstance(merged, dict):
                merged.update(values)
            else:
                merged = dict(values)
            session.add(AppSetting(key=section, value=merged))
        else:
            # Se asigna un dict NUEVO: mutar el JSONB en sitio (`current.update`)
            # deja el objeto viejo y el nuevo idénticos, y SQLAlchemy no emite
            # el UPDATE (el cambio se pierde al cerrar la sesión).
            current = row.value if isinstance(row.value, dict) else {}
            row.value = {**current, **values}
    await session.flush()
    return await load_settings(session)
