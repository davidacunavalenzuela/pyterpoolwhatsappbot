"""Cliente de WhatsApp Cloud API (Meta).

Solo se envían mensajes de texto: el bot es reactivo y responde dentro de la
ventana de servicio de 24 h, por lo que no se necesitan plantillas aprobadas.
Los textos largos se parten porque la API rechaza > 4096 caracteres.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger("whatsapp")

MAX_TEXT_LENGTH = 4000  # margen bajo el límite duro de 4096


class WhatsAppError(RuntimeError):
    """Error al comunicarse con la Graph API."""


def split_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Parte un texto en bloques respetando párrafos y oraciones.

    Partir por caracteres arbitrarios cortaría una URL o un precio; primero se
    intenta por párrafo, luego por oración y solo al final por corte duro.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue
        # Párrafo demasiado largo: se corta por oraciones.
        for sentence in paragraph.replace(". ", ".\n").split("\n"):
            piece = f"{current} {sentence}".strip() if current else sentence
            if len(piece) <= limit:
                current = piece
            else:
                if current:
                    chunks.append(current)
                while len(sentence) > limit:
                    chunks.append(sentence[:limit])
                    sentence = sentence[limit:]
                current = sentence
    if current:
        chunks.append(current)
    return chunks


class WhatsAppClient:
    """Wrapper con reintentos y timeouts explícitos."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Cliente HTTP reutilizado (keep-alive) creado bajo demanda."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.graph_url,
                timeout=httpx.Timeout(20.0, connect=5.0),
                headers={"Authorization": f"Bearer {settings.wa_access_token}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST con reintento exponencial ante 429/5xx.

        Meta devuelve 429 en picos; reintentar con jitter evita bucles
        sincronizados. No se reintenta un 4xx de validación (sería inútil).
        """
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.post(path, json=payload)
            except httpx.HTTPError as exc:  # red/timeout
                last_error = exc
            else:
                if response.status_code < 400:
                    return response.json() if response.content else {}
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = WhatsAppError(
                        f"HTTP {response.status_code}: {response.text[:300]}"
                    )
                else:
                    raise WhatsAppError(
                        f"HTTP {response.status_code}: {response.text[:500]}"
                    )
            await asyncio.sleep(1.5 * (2**attempt))
        raise WhatsAppError(f"No se pudo enviar a WhatsApp: {last_error}")

    async def send_text(self, to: str, body: str) -> str | None:
        """Envía texto (particionado si es necesario) y devuelve el último wamid."""
        last_id: str | None = None
        for chunk in split_text(body):
            data = await self._post(
                f"/{settings.wa_phone_number_id}/messages",
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to,
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                },
            )
            messages = data.get("messages") or []
            if messages:
                last_id = messages[0].get("id")
        return last_id

    async def mark_read(self, wa_message_id: str) -> None:
        """Marca el mensaje como leído (mejora la UX del cliente).

        Es best-effort: un fallo aquí no debe impedir la respuesta.
        """
        try:
            await self._post(
                f"/{settings.wa_phone_number_id}/messages",
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": wa_message_id,
                },
            )
        except WhatsAppError as exc:
            log.warning("mark_read_failed", error=str(exc))

    async def get_media_url(self, media_id: str) -> str:
        """Obtiene la URL temporal del binario a partir del media_id."""
        response = await self.client.get(f"/{media_id}")
        response.raise_for_status()
        return response.json()["url"]

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Descarga el media con límite de tamaño y devuelve (bytes, mime)."""
        url = await self.get_media_url(media_id)
        # La descarga NO va al host de Graph: se usa un cliente sin base_url.
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as raw:
            async with raw.stream(
                "GET", url, headers={"Authorization": f"Bearer {settings.wa_access_token}"}
            ) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "application/octet-stream")
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > settings.max_media_bytes:
                        raise WhatsAppError("El archivo excede el tamaño máximo permitido.")
                return bytes(buffer), mime

    async def subscribe_app(self) -> dict[str, Any]:
        """Suscribe la app al WABA para recibir webhooks (una vez por entorno)."""
        return await self._post(f"/{settings.wa_waba_id}/subscribed_apps", {})


whatsapp = WhatsAppClient()
