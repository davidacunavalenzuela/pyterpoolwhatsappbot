"""Cliente LLM sobre Groq (API compatible con OpenAI).

Restricciones del free tier que condicionan el diseño:
  - 30 req/min y 8.000 tokens/min: se espacian las llamadas y se recorta el
    contexto antes de enviarlo.
  - Sin visión en los modelos de texto: las imágenes van por Gemini (media.py).
  - Groq no entrena con los datos y no los retiene por defecto.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger("llm")


class LLMError(RuntimeError):
    """Fallo definitivo del proveedor (cuota agotada, credenciales, etc.)."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class GroqLLM:
    """Cliente mínimo con espaciado de tasa y fallback de modelo."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._pace_lock = asyncio.Lock()
        self._last_call = 0.0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.groq_base_url,
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pace(self) -> None:
        """Serializa las llamadas para no superar el límite por minuto.

        Con un solo worker y espaciado de 2,2 s el techo es ~27 req/min, por
        debajo de los 30 RPM del free tier. El lock evita que dos tareas del
        mismo proceso disparen a la vez.
        """
        async with self._pace_lock:
            elapsed = time.monotonic() - self._last_call
            wait = settings.llm_min_interval_seconds - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _request(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST con reintentos y respeto de `Retry-After`."""
        last_error: str = "sin detalle"
        for attempt in range(3):
            await self._pace()
            try:
                response = await self.client.post("/chat/completions", json={**payload, "model": model})
            except httpx.HTTPError as exc:
                last_error = f"red: {exc}"
            else:
                if response.status_code < 400:
                    return response.json()
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        await asyncio.sleep(min(float(retry_after), 20.0))
                        continue
                    except ValueError:
                        pass
            await asyncio.sleep(min(2.0 * (2**attempt), 15.0))
        raise LLMError(last_error)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Una llamada al modelo, con fallback si el principal falla.

        El fallback solo se usa ante error del primario; no se duplica el gasto
        de tokens en el camino feliz.
        """
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or settings.llm_max_output_tokens,
            "top_p": 1,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        models = [settings.llm_model]
        if settings.llm_fallback_model and settings.llm_fallback_model != settings.llm_model:
            models.append(settings.llm_fallback_model)

        error: Exception | None = None
        for model in models:
            try:
                data = await self._request(model, payload)
            except LLMError as exc:
                error = exc
                log.warning("llm_model_failed", model=model, error=str(exc))
                continue
            return self._parse(data)

        raise LLMError(f"Todos los modelos fallaron: {error}")

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResponse:
        """Normaliza la respuesta de Groq (formato OpenAI)."""
        choices = data.get("choices") or []
        if not choices:
            return LLMResponse(content="")
        message = choices[0].get("message") or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                # Argumentos inválidos: se registra y se pasa vacío para que la
                # tool devuelva un error de validación en vez de romper el flujo.
                log.warning("tool_arguments_invalid", tool=function.get("name"))
                arguments = {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(calls)}",
                    name=function.get("name") or "",
                    arguments=arguments,
                )
            )
        return LLMResponse(content=(message.get("content") or "").strip(), tool_calls=calls)


llm = GroqLLM()
