"""Conversaciones: historial, control del bot y respuesta manual."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import SessionDep, require_admin, require_csrf
from app.logging import get_logger
from app.models import Conversation, Customer, Message
from app.schemas import (
    ConversationListOut,
    ConversationOut,
    ManualMessageIn,
    MessageOut,
    ToggleBotIn,
)
from app.services.whatsapp import WhatsAppError, whatsapp

router = APIRouter(
    prefix="/api/conversations",
    tags=["conversaciones"],
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)
log = get_logger("admin.conversations")

# Ventana de servicio de Meta: fuera de 24 h desde el último mensaje del cliente
# solo se pueden enviar plantillas aprobadas, que este proyecto no usa.
SERVICE_WINDOW = timedelta(hours=24)


async def _get_conversation(session, conversation_id: int) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversación no encontrada.")
    return conversation


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    session: SessionDep,
    only_open: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConversationListOut:
    """Lista conversaciones ordenadas por actividad reciente."""
    stmt = select(Conversation, Customer).join(Customer, Customer.id == Conversation.customer_id)
    count_stmt = select(func.count(Conversation.id))
    if only_open:
        stmt = stmt.where(Conversation.status != "closed")
        count_stmt = count_stmt.where(Conversation.status != "closed")

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        ConversationOut(
            id=conv.id,
            customer_id=customer.id,
            customer_name=customer.name,
            customer_wa_id=customer.wa_id,
            status=conv.status,
            bot_enabled=conv.bot_enabled,
            last_message_at=conv.last_message_at,
            unread=0,
        )
        for conv, customer in rows
    ]
    return ConversationListOut(items=items, total=total)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: int,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=300),
) -> list[Message]:
    """Historial en orden cronológico ascendente."""
    await _get_conversation(session, conversation_id)
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


@router.patch("/{conversation_id}/bot", response_model=ConversationOut)
async def toggle_bot(conversation_id: int, payload: ToggleBotIn, session: SessionDep) -> ConversationOut:
    """Activa o pausa el bot en una conversación concreta."""
    conversation = await _get_conversation(session, conversation_id)
    conversation.bot_enabled = payload.bot_enabled
    conversation.status = "bot" if payload.bot_enabled else "human"
    await session.commit()
    customer = await session.get(Customer, conversation.customer_id)
    return ConversationOut(
        id=conversation.id,
        customer_id=conversation.customer_id,
        customer_name=customer.name if customer else None,
        customer_wa_id=customer.wa_id if customer else None,
        status=conversation.status,
        bot_enabled=conversation.bot_enabled,
        last_message_at=conversation.last_message_at,
    )


@router.post("/{conversation_id}/messages", response_model=MessageOut)
async def send_manual_message(
    conversation_id: int, payload: ManualMessageIn, session: SessionDep
) -> Message:
    """Envía un mensaje desde el panel y toma control humano del hilo.

    Se valida la ventana de 24 h para no provocar un error opaco de Meta; en ese
    caso la API explica que hace falta una plantilla aprobada.
    """
    conversation = await _get_conversation(session, conversation_id)
    customer = await session.get(Customer, conversation.customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cliente no encontrado.")

    last_inbound = (
        await session.execute(
            select(func.max(Message.created_at)).where(
                Message.conversation_id == conversation_id, Message.direction == "in"
            )
        )
    ).scalar_one()
    if last_inbound is not None:
        last_inbound = last_inbound.replace(tzinfo=UTC) if last_inbound.tzinfo is None else last_inbound
        if datetime.now(UTC) - last_inbound > SERVICE_WINDOW:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Pasaron más de 24 h desde el último mensaje del cliente. "
                "Meta solo permite plantillas aprobadas fuera de esa ventana.",
            )

    try:
        wa_message_id = await whatsapp.send_text(customer.wa_id, payload.text)
    except WhatsAppError as exc:
        log.error("manual_send_failed", conversation_id=conversation_id, error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "WhatsApp rechazó el envío.") from None

    message = Message(
        conversation_id=conversation.id,
        wa_message_id=wa_message_id,
        direction="out",
        msg_type="text",
        body=payload.text,
        status="sent",
    )
    session.add(message)
    conversation.status = "human"
    conversation.bot_enabled = False
    conversation.last_message_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(message)
    return message


@router.post("/{conversation_id}/close", response_model=ConversationOut)
async def close_conversation(conversation_id: int, session: SessionDep) -> ConversationOut:
    """Cierra el hilo; el próximo mensaje del cliente abrirá uno nuevo."""
    conversation = await _get_conversation(session, conversation_id)
    conversation.status = "closed"
    conversation.bot_enabled = False
    await session.commit()
    customer = await session.get(Customer, conversation.customer_id)
    return ConversationOut(
        id=conversation.id,
        customer_id=conversation.customer_id,
        customer_name=customer.name if customer else None,
        customer_wa_id=customer.wa_id if customer else None,
        status=conversation.status,
        bot_enabled=conversation.bot_enabled,
        last_message_at=conversation.last_message_at,
    )


@router.get("/{conversation_id}/debug", include_in_schema=False)
async def conversation_debug(conversation_id: int, session: SessionDep) -> dict:
    """Ayuda a soporte: últimos eventos de la cola relacionados al hilo."""
    await _get_conversation(session, conversation_id)
    messages = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(5)
        )
    ).scalars().all()
    return {
        "messages": json.loads(
            json.dumps([MessageOut.model_validate(m).model_dump() for m in messages], default=str)
        )
    }
