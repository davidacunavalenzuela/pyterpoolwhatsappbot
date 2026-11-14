"""Procesamiento de notas de voz e imágenes entrantes.

- Audio -> Groq Whisper (free tier, sin entrenamiento con los datos).
- Imagen -> Gemini free. Mitigaciones de privacidad: se envía **solo la imagen**
  (sin historial, nombre, teléfono ni datos del pedido) y se reduce el tamaño
  antes de subirla. Si Gemini falla, el bot pide al cliente que lo escriba.
"""

from __future__ import annotations

import asyncio
import base64
import io

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger("media")

VISION_PROMPT = (
    "Eres un asistente de una tienda. Describe en español, en máximo 60 palabras, "
    "lo que muestra esta imagen enviada por un cliente: producto, estado o daño "
    "visible, y transcribe cualquier texto legible. No supongas datos que no se vean."
)


def downscale_image(data: bytes, max_side: int | None = None) -> tuple[bytes, str]:
    """Reduce la imagen a JPEG de lado máximo `max_side`.

    Se hace en un hilo aparte porque Pillow es CPU-bound y bloquearía el event
    loop. Con 1024 px se conserva legibilidad del texto de un envase y se
    reduce el costo en tokens frente a una foto de 12 MP.
    """
    from PIL import Image, UnidentifiedImageError

    max_side = max_side or settings.vision_max_pixels
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            return output.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("La imagen no se pudo procesar.") from exc


async def transcribe_audio(data: bytes, filename: str = "audio.ogg", mime: str = "audio/ogg") -> str | None:
    """Transcribe una nota de voz con Whisper en Groq.

    Nota: Meta entrega las notas de voz como OGG/Opus, formato que Whisper
    acepta directamente. No se guarda el audio en disco.
    """
    if not settings.groq_api_key:
        return None
    files = {"file": (filename, data, mime or "audio/ogg")}
    payload = {
        "model": settings.whisper_model,
        "language": "es",
        "response_format": "json",
        "temperature": "0",
    }
    async with httpx.AsyncClient(
        base_url=settings.groq_base_url,
        timeout=httpx.Timeout(90.0, connect=10.0),
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
    ) as client:
        for attempt in range(3):
            try:
                response = await client.post("/audio/transcriptions", data=payload, files=files)
            except httpx.HTTPError as exc:
                log.warning("whisper_network_error", error=str(exc), attempt=attempt)
            else:
                if response.status_code < 400:
                    return (response.json().get("text") or "").strip() or None
                log.warning("whisper_failed", status=response.status_code, body=response.text[:200])
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return None
            await asyncio.sleep(1.5 * (2**attempt))
    return None


async def describe_image(data: bytes, mime: str = "image/jpeg") -> str | None:
    """Describe una imagen con Gemini free (multimodal).

    Si no hay `GEMINI_API_KEY` o la cuota se agota, devuelve None y el agente
    pide al cliente que describa el producto por texto.
    """
    if not settings.gemini_api_key:
        return None
    try:
        processed, processed_mime = await asyncio.to_thread(downscale_image, data)
    except ValueError as exc:
        log.warning("image_invalid", error=str(exc))
        return None

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": VISION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": processed_mime,
                            "data": base64.b64encode(processed).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": settings.vision_max_output_tokens,
        },
    }
    url = f"{settings.gemini_base_url}/models/{settings.vision_model}:generateContent"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        headers={"x-goog-api-key": settings.gemini_api_key},
    ) as client:
        for attempt in range(2):
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("vision_network_error", error=str(exc), attempt=attempt)
            else:
                if response.status_code < 400:
                    return _extract_gemini_text(response.json())
                log.warning("vision_failed", status=response.status_code, body=response.text[:200])
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return None
            await asyncio.sleep(2.0 * (2**attempt))
    return None


def _extract_gemini_text(data: dict) -> str | None:
    """Extrae el texto de la respuesta de Gemini ignorando partes vacías."""
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts = [part.get("text", "") for part in parts if part.get("text")]
    result = "\n".join(t for t in texts if t).strip()
    return result or None
