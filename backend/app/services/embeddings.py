"""Embeddings locales con fastembed (ONNX).

Por qué local: Groq no ofrece embeddings y depender de otra API gratuita
añadiría cuotas, latencia y envío de datos de clientes. El modelo
paraphrase-multilingual-MiniLM-L12-v2 (384 dim) corre en CPU con ~200 MB de
RAM y entiende español.
"""

from __future__ import annotations

import asyncio
import threading

from app.config import settings
from app.logging import get_logger

log = get_logger("embeddings")

_embedder = None
_lock = threading.Lock()
_disabled = False


def _load_embedder():
    """Carga perezosa y única del modelo (la primera vez descarga ~120 MB)."""
    global _embedder, _disabled
    if _embedder is not None or _disabled:
        return _embedder
    with _lock:
        if _embedder is not None or _disabled:
            return _embedder
        try:
            from fastembed import TextEmbedding

            _embedder = TextEmbedding(
                model_name=settings.embeddings_model,
                cache_dir=settings.embeddings_cache_dir,
            )
            log.info("embeddings_loaded", model=settings.embeddings_model)
        except Exception as exc:  # modelo no descargable / sin memoria
            # Modo degradado: el RAG sigue funcionando con búsqueda léxica.
            _disabled = True
            log.warning("embeddings_unavailable", error=str(exc))
    return _embedder


def embeddings_available() -> bool:
    """True si el modelo está cargado o puede cargarse."""
    return _load_embedder() is not None


async def embed_passages(texts: list[str]) -> list[list[float]] | None:
    """Vectoriza fragmentos de documentos.

    No se añaden prefijos manualmente: fastembed ya aplica los prefijos
    `passage:`/`query:` que exigen los modelos e5 según su configuración.
    """
    if not texts:
        return []
    embedder = await asyncio.to_thread(_load_embedder)
    if embedder is None:
        return None
    vectors = await asyncio.to_thread(lambda: list(embedder.embed(texts)))
    return [v.tolist() for v in vectors]


async def embed_query(text: str) -> list[float] | None:
    """Vectoriza la consulta del usuario con el prefijo de consulta del modelo."""
    embedder = await asyncio.to_thread(_load_embedder)
    if embedder is None:
        return None
    vectors = await asyncio.to_thread(lambda: list(embedder.query_embed(text)))
    return vectors[0].tolist() if vectors else None
