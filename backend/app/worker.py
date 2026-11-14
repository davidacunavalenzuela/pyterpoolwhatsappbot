"""Worker: consume la cola de Postgres y ejecuta el agente.

Por qué un worker separado y una cola en Postgres:
  - El webhook debe responder en <5 s; procesar el LLM en la request haría que
    Meta reintente y duplique mensajes.
  - No se añade Redis: `FOR UPDATE SKIP LOCKED` da la misma garantía de
    "exactamente un consumidor" con la base que ya tenemos, ahorrando RAM en un
    servidor de 4 GB.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from app.config import settings
from app.db import SessionLocal
from app.logging import configure_logging, get_logger
from app.models import Conversation, Customer, Document, Message
from app.services.agent import run_agent
from app.services.llm import LLMError
from app.services.media import describe_image, transcribe_audio
from app.services.rag import (
    extract_text,
    ingest_document,
    ingest_product_catalog,
    reindex_all,
)
from app.services.settings_service import load_settings
from app.services.whatsapp import WhatsAppError, whatsapp

log = get_logger("worker")

FALLBACK_REPLY = (
    "Perdona, no pude procesar bien tu mensaje. ¿Me lo puedes escribir de otra forma?"
)
LLM_DOWN_REPLY = (
    "Estamos con mucha demanda en este momento. Te derivo con una persona del equipo "
    "para que te ayude en breve."
)


# ---------------------------------------------------------------------------
# Cola
# ---------------------------------------------------------------------------
async def claim_event() -> dict[str, Any] | None:
    """Toma un evento pendiente con SKIP LOCKED (atómico y sin bloqueos)."""
    statement = text(
        """
        UPDATE inbound_events
        SET status = 'processing', locked_at = now(), attempts = attempts + 1
        WHERE id = (
            SELECT id FROM inbound_events
            WHERE status = 'pending' AND available_at <= now()
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload, attempts
        """
    )
    async with SessionLocal() as session:
        row = (await session.execute(statement)).mappings().first()
        await session.commit()
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {"id": row["id"], "kind": row["kind"], "payload": payload, "attempts": row["attempts"]}


async def mark_done(event_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE inbound_events SET status='done', processed_at=now(), last_error=NULL "
                "WHERE id=:id"
            ),
            {"id": event_id},
        )
        await session.commit()


async def mark_failed(event_id: int, attempts: int, error: str) -> None:
    """Reintenta con backoff cuadrático; al agotar intentos queda en 'failed'."""
    retry = attempts < settings.worker_max_attempts
    delay = min(10 * attempts * attempts, 300)
    async with SessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE inbound_events
                SET status = :status,
                    last_error = :error,
                    locked_at = NULL,
                    available_at = CASE WHEN :retry
                        THEN now() + make_interval(secs => CAST(:delay AS double precision))
                        ELSE available_at END
                WHERE id = :id
                """
            ),
            {
                "status": "pending" if retry else "failed",
                "error": error[:1000],
                "retry": retry,
                "delay": delay,
                "id": event_id,
            },
        )
        await session.commit()
    log.warning("event_failed", event_id=event_id, attempts=attempts, retry=retry)


async def recover_stuck_events() -> None:
    """Devuelve a 'pending' eventos que quedaron 'processing' por un crash."""
    async with SessionLocal() as session:
        await session.execute(
            text(
                """
                UPDATE inbound_events
                SET status='pending', locked_at=NULL
                WHERE status='processing'
                  AND locked_at < now() - make_interval(secs => CAST(:secs AS double precision))
                """
            ),
            {"secs": settings.worker_stuck_after_seconds},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Procesamiento
# ---------------------------------------------------------------------------
def _message_text(message: dict[str, Any]) -> str | None:
    """Texto plano del mensaje (incluye respuestas de botones/listas)."""
    msg_type = message.get("type")
    if msg_type == "text":
        return (message.get("text") or {}).get("body")
    if msg_type == "button":
        return (message.get("button") or {}).get("text")
    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title")
    if msg_type == "image":
        return (message.get("image") or {}).get("caption")
    return None


async def _resolve_media(message: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Descarga y enriquece audio/imagen. Devuelve (texto, media_id, mime)."""
    msg_type = message.get("type")
    if msg_type in {"audio", "voice"}:
        media = message.get("audio") or message.get("voice") or {}
        media_id = media.get("id")
        if not media_id:
            return None, None, None
        try:
            data, mime = await whatsapp.download_media(media_id)
            transcript = await transcribe_audio(data, mime=mime)
        except Exception as exc:  # descarga o transcripción
            log.warning("audio_processing_failed", error=str(exc))
            return "[Nota de voz que no se pudo transcribir]", media_id, media.get("mime_type")
        if not transcript:
            return "[Nota de voz sin transcribir]", media_id, media.get("mime_type")
        return transcript, media_id, media.get("mime_type")

    if msg_type == "image":
        media = message.get("image") or {}
        media_id = media.get("id")
        caption = media.get("caption") or ""
        if not media_id:
            return None, None, None
        try:
            data, mime = await whatsapp.download_media(media_id)
            description = await describe_image(data, mime)
        except Exception as exc:
            log.warning("image_processing_failed", error=str(exc))
            return caption or "[Imagen que no se pudo analizar]", media_id, media.get("mime_type")
        if description is None:
            return (
                (caption + " " if caption else "")
                + "[Imagen recibida; no se pudo analizar. Pide que la describa por texto.]",
                media_id,
                media.get("mime_type"),
            )
        body = f"{caption}\n[Descripción de la imagen: {description}]".strip()
        return body, media_id, media.get("mime_type")

    return None, None, None


async def _get_or_create_customer(session, wa_id: str, name: str | None) -> Customer:
    customer = (
        await session.execute(select(Customer).where(Customer.wa_id == wa_id))
    ).scalar_one_or_none()
    if customer is None:
        customer = Customer(wa_id=wa_id, name=name, phone=wa_id)
        session.add(customer)
        await session.flush()
    elif name and not customer.name:
        customer.name = name
    return customer


async def _get_open_conversation(session, customer: Customer) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.customer_id == customer.id, Conversation.status != "closed")
            .order_by(Conversation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversation is None:
        conversation = Conversation(customer_id=customer.id, status="bot", bot_enabled=True)
        session.add(conversation)
        await session.flush()
    return conversation


async def handle_message(payload: dict[str, Any]) -> None:
    """Flujo completo de un mensaje entrante.

    Las transacciones se cierran antes de cada llamada externa (Meta, Groq,
    Gemini): `idle_in_transaction_session_timeout` mataría la conexión si
    mantuviéramos una transacción abierta durante 30-60 s de generación.
    """
    message = payload.get("message") or {}
    contacts = payload.get("contacts") or []
    wa_id = message.get("from")
    wa_message_id = message.get("id")
    if not wa_id or not wa_message_id:
        return

    profile_name = None
    if contacts:
        profile_name = ((contacts[0].get("profile") or {}).get("name") or "").strip() or None

    msg_type = message.get("type") or "unknown"
    body = _message_text(message)

    async with SessionLocal() as session:
        existing = (
            await session.execute(select(Message).where(Message.wa_message_id == wa_message_id))
        ).scalar_one_or_none()
        if existing is not None:
            return  # idempotencia adicional ante reintentos

        customer = await _get_or_create_customer(session, wa_id, profile_name)
        conversation = await _get_open_conversation(session, customer)
        inbound = Message(
            conversation_id=conversation.id,
            wa_message_id=wa_message_id,
            direction="in",
            msg_type=msg_type,
            body=body,
        )
        session.add(inbound)
        conversation.last_message_at = datetime.now(UTC)
        await session.commit()
        conversation_id = conversation.id
        customer_id = customer.id
        inbound_id = inbound.id
        await session.refresh(conversation)
        await session.refresh(customer, attribute_names=["id", "wa_id", "name", "notes"])
        bot_enabled = conversation.bot_enabled and conversation.status == "bot"

    await whatsapp.mark_read(wa_message_id)

    # --- Media (llamadas externas, sin transacción abierta) ---------------
    if msg_type in {"audio", "voice", "image"}:
        enriched, media_id, media_mime = await _resolve_media(message)
        async with SessionLocal() as session:
            row = await session.get(Message, inbound_id)
            if row is not None:
                if enriched:
                    row.body = enriched
                row.media_id = media_id
                row.media_mime = media_mime
                row.transcription = enriched if msg_type in {"audio", "voice"} else None
                await session.commit()

    if not bot_enabled:
        log.info("bot_disabled_message_stored", conversation_id=conversation_id)
        return

    # --- Agente ------------------------------------------------------------
    try:
        async with SessionLocal() as session:
            conversation = await session.get(Conversation, conversation_id)
            customer = await session.get(Customer, customer_id)
            if conversation is None or customer is None:
                return
            app_settings = await load_settings(session)
            await session.commit()  # cerrar lectura antes de llamar al LLM

            reply = await run_agent(session, conversation, customer, app_settings)
            await session.commit()
    except LLMError as exc:
        log.error("llm_unavailable", conversation_id=conversation_id, error=str(exc))
        async with SessionLocal() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.status = "human"
                conversation.bot_enabled = False
                await session.commit()
        await _send_and_store(conversation_id, customer_id, LLM_DOWN_REPLY)
        return

    if not reply:
        reply = FALLBACK_REPLY
    await _send_and_store(conversation_id, customer_id, reply)


async def _send_and_store(conversation_id: int, customer_id: int, body: str) -> None:
    """Envía la respuesta por WhatsApp y la guarda como mensaje saliente."""
    async with SessionLocal() as session:
        customer = await session.get(Customer, customer_id)
        if customer is None:
            return
        wa_id = customer.wa_id

    try:
        wa_message_id = await whatsapp.send_text(wa_id, body)
    except WhatsAppError as exc:
        log.error("send_failed", conversation_id=conversation_id, error=str(exc))
        async with SessionLocal() as session:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction="out",
                    msg_type="text",
                    body=body,
                    status="failed",
                    error=str(exc)[:500],
                )
            )
            await session.commit()
        return

    async with SessionLocal() as session:
        session.add(
            Message(
                conversation_id=conversation_id,
                wa_message_id=wa_message_id,
                direction="out",
                msg_type="text",
                body=body,
                status="sent",
            )
        )
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.last_message_at = datetime.now(UTC)
        await session.commit()


async def handle_status(payload: dict[str, Any]) -> None:
    """Actualiza el estado de entrega/lectura de un mensaje saliente."""
    status_data = payload.get("status") or {}
    wa_message_id = status_data.get("id")
    state = status_data.get("status")
    if not wa_message_id or not state:
        return
    async with SessionLocal() as session:
        message = (
            await session.execute(select(Message).where(Message.wa_message_id == wa_message_id))
        ).scalar_one_or_none()
        if message is None:
            return
        message.status = state
        errors = status_data.get("errors") or []
        if errors:
            message.error = json.dumps(errors)[:1000]
        await session.commit()


async def handle_ingest(payload: dict[str, Any]) -> None:
    """Indexa un documento puntual, el catálogo o todo, según el payload."""
    async with SessionLocal() as session:
        if payload.get("all"):
            documents = await reindex_all(session)
            await session.commit()
            log.info("reindex_all_done", documents=len(documents))
            return
        if payload.get("product_catalog"):
            await ingest_product_catalog(session)
            await session.commit()
            return
        document_id = payload.get("document_id")
        if not document_id:
            return
        document = await session.get(Document, document_id)
        if document is None:
            return
        filename = Path(document.filename) if document.filename else None
        if filename is None or not await asyncio.to_thread(filename.exists):
            document.status = "error"
            document.error = "Archivo no encontrado."
            await session.commit()
            return
        try:
            raw_text = await asyncio.to_thread(extract_text, filename)
        except Exception as exc:
            document.status = "error"
            document.error = str(exc)[:500]
            await session.commit()
            return
        await ingest_document(session, document, raw_text)
        await session.commit()


async def dispatch(event: dict[str, Any]) -> None:
    kind = event["kind"]
    if kind == "message":
        await handle_message(event["payload"])
    elif kind == "status":
        await handle_status(event["payload"])
    elif kind == "ingest":
        await handle_ingest(event["payload"])
    else:
        log.warning("unknown_event_kind", kind=kind)


async def main() -> None:
    """Bucle principal con cierre ordenado al recibir SIGTERM."""
    configure_logging()
    log.info("worker_started", poll=settings.worker_poll_seconds)
    await recover_stuck_events()
    try:
        while True:
            event = await claim_event()
            if event is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            try:
                await dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("event_processing_error", event_id=event["id"])
                await mark_failed(event["id"], event["attempts"], str(exc))
            else:
                await mark_done(event["id"])
    finally:
        await whatsapp.aclose()
        log.info("worker_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
