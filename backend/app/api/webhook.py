"""Webhook de WhatsApp Cloud API.

Requisitos de Meta que moldean este endpoint:
  - El GET de verificación debe devolver el `hub.challenge` en texto plano.
  - Cada POST debe responderse 200 en <5 s o Meta lo reintenta.
  - Los reintentos pueden duplicar mensajes: la idempotencia se resuelve con el
    índice UNIQUE de `inbound_events.wa_message_id`.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import SessionDep
from app.config import settings
from app.logging import get_logger
from app.models import InboundEvent
from app.security import verify_verify_token, verify_webhook_signature

router = APIRouter(tags=["webhook"])
log = get_logger("webhook")

MAX_BODY_BYTES = 2 * 1024 * 1024  # Meta no envía payloads grandes; corta abusos.


@router.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> str:
    """Handshake inicial que Meta ejecuta al guardar la URL del webhook."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and verify_verify_token(token):
        return challenge
    log.warning("webhook_verification_failed", mode=mode)
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Verificación fallida.")


def _extract_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convierte el payload anidado de Meta en filas de cola.

    Se guarda el objeto crudo de cada mensaje/estado para poder reprocesar sin
    volver a pedírselo a Meta.
    """
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_id = str(metadata.get("phone_number_id") or "")
            # Se descarta tráfico de otras cuentas (si alguna vez se comparte app).
            if settings.wa_phone_number_id and phone_id and phone_id != settings.wa_phone_number_id:
                continue

            for message in value.get("messages") or []:
                message_id = message.get("id")
                if not message_id:
                    continue
                events.append(
                    {
                        "kind": "message",
                        "wa_message_id": message_id,
                        "payload": {
                            "message": message,
                            "contacts": value.get("contacts") or [],
                            "metadata": metadata,
                        },
                    }
                )

            for change_status in value.get("statuses") or []:
                status_id = change_status.get("id")
                state = change_status.get("status", "unknown")
                if not status_id:
                    continue
                # Se guarda un evento por (mensaje, estado): "sent" y "read" del
                # mismo wamid son eventos distintos, no duplicados.
                events.append(
                    {
                        "kind": "status",
                        "wa_message_id": f"{status_id}:{state}",
                        "payload": {"status": change_status},
                    }
                )
    return events


@router.post("/webhook")
async def receive_webhook(request: Request, session: SessionDep) -> dict[str, str]:
    """Valida la firma, encola los eventos y responde de inmediato."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Payload demasiado grande.")

    if settings.wa_app_secret:
        valid = verify_webhook_signature(raw, request.headers.get("x-hub-signature-256"))
    else:
        # Sin App Secret no se puede validar; en dev se permite explícitamente
        # para poder probar con curl, nunca en producción (config lo bloquea).
        valid = not settings.is_prod
        if valid:
            log.warning("webhook_signature_skipped_dev")

    if not valid:
        # Sin firma válida no se procesa nada: evita pedidos falsos.
        log.warning("webhook_signature_invalid")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Firma inválida.")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON inválido.") from None

    events = _extract_events(payload)
    if not events:
        # Meta envía también notificaciones sin mensajes (p. ej. cambios de
        # plantilla); se acepta con 200 para que no reintente.
        return {"status": "ignored"}

    stmt = insert(InboundEvent).values(events).on_conflict_do_nothing(
        index_elements=["wa_message_id"]
    )
    await session.execute(stmt)
    await session.commit()

    log.info("webhook_enqueued", events=len(events))
    return {"status": "ok"}
