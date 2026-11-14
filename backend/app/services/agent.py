"""Orquestador del agente: prompt, historial y bucle de tool calling."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging import get_logger
from app.models import Conversation, Customer, Message
from app.services.llm import LLMError, llm
from app.services.tools import HANDLERS, TOOL_SCHEMAS, AgentContext

log = get_logger("agent")

SYSTEM_TEMPLATE = """Eres {bot_name}, el asistente de ventas de {business_name}.
Hoy es {date} ({timezone}). Cliente: {customer} (WhatsApp {wa_id}).

Negocio: {description}
Horario: {hours}. Despacho: {delivery}. Pago: {payment}.

Reglas:
1. Responde en {language}, tono {tone}. Máximo 4 frases cortas, estilo WhatsApp, sin markdown.
2. Precios, stock y plazos: SOLO desde las herramientas. Si no hay dato, dilo y ofrece derivar.
3. Para cerrar una compra: confirma el carrito con el cliente y espera un "sí"
   explícito antes de llamar a confirmar_pedido.
4. Usa buscar_documentacion cuando pregunten cómo usar, limpiar o cuidar un producto.
5. Deriva a humano con derivar_a_humano si hay reclamo, devolución, problema de pago o el cliente lo pide.
6. Nunca pidas datos de tarjeta, claves ni documentos. El pago lo coordina una persona.
7. Si el mensaje es ambiguo, haz una sola pregunta. No inventes productos ni promociones.
8. No repitas el saludo en cada mensaje ni menciones que eres una IA salvo que lo pregunten."""


def _system_prompt(customer: Customer, app_settings: dict, wa_id: str) -> str:
    business = app_settings.get("business", {})
    agent = app_settings.get("agent", {})
    timezone = business.get("timezone", "America/Santiago")
    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:  # zona inválida configurada por el admin
        now = datetime.now()
    return SYSTEM_TEMPLATE.format(
        bot_name=business.get("name", "el asistente"),
        business_name=business.get("name", "el negocio"),
        date=now.strftime("%d-%m-%Y %H:%M"),
        timezone=timezone,
        customer=customer.name or "sin nombre registrado",
        wa_id=wa_id,
        description=business.get("description", ""),
        hours=business.get("hours", "no informado"),
        delivery=business.get("delivery", "no informado"),
        payment=business.get("payment", "no informado"),
        language=agent.get("language", "español"),
        tone=agent.get("tone", "amable y breve"),
    )


async def _history(session: AsyncSession, conversation_id: int, budget: int) -> list[dict]:
    """Últimos mensajes como turnos user/assistant, recortados por presupuesto.

    Se reserva ~40 % del presupuesto al prompt de sistema y a las tools; el
    resto al historial, porque 8.000 tokens/min es el límite más ajustado de
    Groq y una conversación larga lo agotaría en dos turnos.
    """
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.body.is_not(None))
            .order_by(Message.id.desc())
            .limit(settings.llm_history_messages)
        )
    ).scalars().all()

    history: list[dict] = []
    used = 0
    for message in reversed(rows):
        body = (message.body or "").strip()
        if not body:
            continue
        if used + len(body) > budget:
            break
        used += len(body)
        history.append(
            {"role": "user" if message.direction == "in" else "assistant", "content": body}
        )
    return history


async def run_agent(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    app_settings: dict,
) -> str | None:
    """Ejecuta el bucle de conversación y devuelve la respuesta final.

    Devuelve None si el modelo no produjo texto utilizable; el worker decide el
    mensaje de cortesía. No envía nada por WhatsApp: eso lo hace el worker, para
    que el agente sea testeable sin red.
    """
    system = _system_prompt(customer, app_settings, customer.wa_id)
    history_budget = max(settings.llm_max_input_chars - len(system) - 800, 1200)
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(await _history(session, conversation.id, history_budget))

    ctx = AgentContext(
        session=session,
        conversation=conversation,
        customer=customer,
        settings=app_settings,
    )

    for _step in range(settings.llm_max_steps):
        response = await llm.complete(messages, tools=TOOL_SCHEMAS)

        if not response.tool_calls:
            content = response.content.strip()
            return content or None

        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in response.tool_calls
                ],
            }
        )

        for call in response.tool_calls:
            handler = HANDLERS.get(call.name)
            if handler is None:
                result = json.dumps({"error": f"Herramienta desconocida: {call.name}"})
            else:
                try:
                    result = await handler(ctx, **call.arguments)
                except TypeError as exc:
                    # Argumentos que no calzan con la firma. Se devuelve el error
                    # al modelo para que corrija en el siguiente paso.
                    log.warning("tool_bad_arguments", tool=call.name, error=str(exc))
                    result = json.dumps({"error": "Argumentos inválidos."}, ensure_ascii=False)
                except Exception as exc:
                    log.exception("tool_failed", tool=call.name)
                    result = json.dumps(
                        {"error": f"No se pudo ejecutar {call.name}: {exc}"}, ensure_ascii=False
                    )

            # El handoff debe cortar el bucle: seguir conversando después de
            # derivar confundiría al cliente.
            if call.name == "derivar_a_humano":
                try:
                    payload = json.loads(result)
                    if payload.get("ok") and payload.get("mensaje_al_cliente"):
                        return str(payload["mensaje_al_cliente"])
                except json.JSONDecodeError:
                    pass

            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

        await session.flush()

    log.warning("agent_max_steps", conversation_id=conversation.id)
    return None


__all__ = ["LLMError", "run_agent"]
