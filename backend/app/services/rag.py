"""RAG: troceado, ingesta y búsqueda híbrida (vector + texto completo).

Estrategia híbrida: los embeddings capturan paráfrasis ("cómo lo limpio" vs
"instrucciones de limpieza") y la búsqueda léxica en español acierta con
nombres de modelo o códigos exactos, donde los vectores fallan. Se combinan
con Reciprocal Rank Fusion, que no requiere calibrar escalas de score.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging import get_logger
from app.models import Document, DocumentChunk, Product
from app.services.embeddings import embed_passages, embed_query

log = get_logger("rag")

PRODUCT_DOC_CHECKSUM = "catalog-products-v1"
SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf"}


@dataclass(slots=True)
class SearchHit:
    content: str
    title: str
    score: float
    product_id: int | None


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Trocea texto por párrafos con solape para no perder contexto.

    Se corta preferentemente en párrafos y, si uno excede el tamaño, en el
    último espacio antes del límite (evita cortar palabras a la mitad).
    """
    size = size or settings.rag_chunk_chars
    overlap = overlap if overlap is not None else settings.rag_chunk_overlap

    normalized = re.sub(r"\r\n?", "\n", text)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]

    raw: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > size:
            if current:
                raw.append(current)
                current = ""
            cut = paragraph.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            raw.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].lstrip()
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= size:
            current = candidate
        else:
            raw.append(current)
            current = paragraph
    if current:
        raw.append(current)

    # Al cortar un párrafo largo, el resto final suele ser un fragmento corto
    # ("útil"). Se fusiona con el bloque anterior en lugar de descartarlo: perder
    # esa frase al final de un manual dejaría el RAG con información incompleta.
    merged: list[str] = []
    for block in raw:
        if merged and len(block) < 30:
            merged[-1] = f"{merged[-1]}\n{block}"
        else:
            merged.append(block)

    chunks: list[str] = []
    for index, block in enumerate(merged):
        if len(block) < 30:
            continue
        if index > 0 and overlap > 0:
            tail = merged[index - 1][-overlap:]
            block = f"{tail}\n{block}"
        chunks.append(block.strip())
    return chunks


def extract_text(path: Path) -> str:
    """Extrae texto de Markdown/TXT/PDF.

    pypdf solo lee la capa de texto; un PDF escaneado sin OCR devolverá vacío y
    se marca como error para que el admin lo sepa.
    """
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    raise ValueError(f"Formato no soportado: {suffix}")


def checksum_of(data: str | bytes) -> str:
    """SHA-256 para ingesta idempotente."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


async def _embed_vectors(contents: list[str]) -> list[list[float] | None]:
    """Calcula embeddings por lotes; devuelve None por chunk si no hay modelo."""
    vectors: list[list[float] | None] = [None] * len(contents)
    batch_size = 32
    for start in range(0, len(contents), batch_size):
        batch_vectors = await embed_passages(contents[start : start + batch_size])
        if batch_vectors is None:
            return [None] * len(contents)
        vectors[start : start + len(batch_vectors)] = batch_vectors
    return vectors


async def _replace_chunks(
    session: AsyncSession,
    document: Document,
    payloads: list[tuple[str, int | None]],
) -> int:
    """Regenera los chunks de un documento y calcula sus embeddings por lotes.

    La transacción se cierra ANTES de vectorizar: la primera carga del modelo
    descarga ~120 MB y un lote puede tardar más que
    `idle_in_transaction_session_timeout` (30 s), haciendo que Postgres cierre
    la conexión a mitad de la ingesta.
    """
    document_id = document.id
    contents = [content for content, _ in payloads]
    await session.commit()
    vectors = await _embed_vectors(contents)

    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    session.add_all(
        [
            DocumentChunk(document_id=document_id, product_id=product_id, content=content)
            for content, product_id in payloads
        ]
    )
    await session.flush()

    # Los vectores se asignan después del INSERT inicial para no cargar todos
    # en memoria antes de tiempo; se recorre solo lo recién creado.
    if any(v is not None for v in vectors):
        chunks = (
            await session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.id)
            )
        ).scalars().all()
        for chunk, vector in zip(chunks, vectors, strict=False):
            if vector is not None:
                chunk.embedding = vector
    return len(payloads)


async def ingest_document(session: AsyncSession, document: Document, raw_text: str) -> Document:
    """Ingesta completa de un documento de texto."""
    document_id = document.id
    document.status = "processing"
    document.error = None
    await session.flush()
    try:
        chunks = chunk_text(raw_text)
        if not chunks:
            raise ValueError("El documento no contiene texto extraíble.")
        document.chunk_count = await _replace_chunks(
            session, document, [(chunk, None) for chunk in chunks]
        )
        document.status = "ready"
        log.info("document_ingested", document_id=document_id, chunks=document.chunk_count)
    except Exception as exc:
        # Tras un fallo (p. ej. conexión cerrada por timeout) la sesión queda
        # inutilizable: se hace rollback y se recarga el documento antes de
        # marcarlo como error.
        await session.rollback()
        fresh = await session.get(Document, document_id)
        if fresh is not None:
            fresh.status = "error"
            fresh.error = str(exc)[:500]
            document = fresh
        log.error("document_ingest_failed", document_id=document_id, error=str(exc))
    await session.flush()
    return document


async def ingest_file(
    session: AsyncSession,
    path: Path,
    title: str | None = None,
    source_type: str = "manual",
    force: bool = False,
) -> Document:
    """Ingesta un archivo del disco de forma idempotente por checksum.

    `force=True` ignora el checksum y regenera los chunks (necesario al cambiar
    de modelo de embeddings o tras corregir un PDF).
    """
    raw = await asyncio.to_thread(path.read_bytes)
    checksum = checksum_of(raw)
    existing = (
        await session.execute(select(Document).where(Document.checksum == checksum))
    ).scalar_one_or_none()
    if existing is not None and existing.status == "ready" and not force:
        return existing

    document = existing or Document(
        title=title or path.stem,
        source_type=source_type,
        filename=str(path),
        checksum=checksum,
    )
    document.filename = str(path)
    document.title = title or document.title
    if existing is None:
        session.add(document)
    await session.flush()
    text_content = await asyncio.to_thread(extract_text, path)
    return await ingest_document(session, document, text_content)


async def ingest_product_catalog(session: AsyncSession) -> Document:
    """Indexa el uso de todos los productos activos como un único documento.

    Así el catálogo no compite con los manuales en la tabla de documentos y se
    puede reconstruir entero cuando el admin edita un producto.
    """
    document = (
        await session.execute(select(Document).where(Document.checksum == PRODUCT_DOC_CHECKSUM))
    ).scalar_one_or_none()
    if document is None:
        document = Document(
            title="Catálogo: uso de productos",
            source_type="product",
            filename=None,
            checksum=PRODUCT_DOC_CHECKSUM,
        )
        session.add(document)
        await session.flush()

    products = (
        await session.execute(select(Product).where(Product.active.is_(True)).order_by(Product.name))
    ).scalars().all()

    payloads: list[tuple[str, int | None]] = []
    for product in products:
        body = (
            f"Producto: {product.name} (SKU {product.sku}). "
            f"Precio: {product.price} {product.currency}. Stock: {product.stock}.\n"
            f"Descripción: {product.description or 'sin descripción'}.\n"
            f"Modo de uso: {product.usage_summary or 'no especificado'}."
        )
        for chunk in chunk_text(body):
            payloads.append((chunk, product.id))

    document.status = "processing"
    await session.flush()
    document.chunk_count = await _replace_chunks(session, document, payloads)
    document.status = "ready"
    await session.flush()
    return document


async def ingest_all(
    session: AsyncSession, docs_dir: Path | None = None, force: bool = False
) -> list[Document]:
    """Ingesta la carpeta de manuales más el catálogo de productos."""
    directory = docs_dir or settings.docs_dir
    results: list[Document] = []
    if directory.exists():
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                results.append(await ingest_file(session, path, force=force))
    results.append(await ingest_product_catalog(session))
    return results


async def search(session: AsyncSession, query: str, k: int | None = None) -> list[SearchHit]:
    """Búsqueda híbrida con RRF sobre chunks y documentos activos."""
    k = k or settings.rag_top_k
    query = query.strip()
    if not query:
        return []

    ranked: dict[int, float] = {}
    contents: dict[int, tuple[str, str, int | None]] = {}
    pool = max(k * 3, 10)

    # --- Rama vectorial ----------------------------------------------------
    vector = await embed_query(query)
    if vector is not None:
        distance = DocumentChunk.embedding.cosine_distance(vector).label("distance")
        rows = (
            await session.execute(
                select(
                    DocumentChunk.id,
                    DocumentChunk.content,
                    Document.title,
                    DocumentChunk.product_id,
                    distance,
                )
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.embedding.is_not(None), Document.status == "ready")
                .order_by(distance)
                .limit(pool)
            )
        ).all()
        for rank, (chunk_id, content, title, product_id, dist) in enumerate(rows, start=1):
            score = 1.0 - float(dist)
            if score < settings.rag_min_score:
                continue
            ranked[chunk_id] = ranked.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            contents.setdefault(chunk_id, (content, title, product_id))

    # --- Rama léxica en español -------------------------------------------
    # `websearch_to_tsquery` tolera frases y comillas sin explotar con errores
    # de sintaxis si el cliente escribe signos raros.
    rows = (
        await session.execute(
            text(
                """
                SELECT c.id, c.content, d.title, c.product_id,
                       ts_rank(to_tsvector('spanish', c.content),
                               websearch_to_tsquery('spanish', :q)) AS rank
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.status = 'ready'
                  AND to_tsvector('spanish', c.content) @@ websearch_to_tsquery('spanish', :q)
                ORDER BY rank DESC
                LIMIT :lim
                """
            ),
            {"q": query, "lim": pool},
        )
    ).all()
    for rank, (chunk_id, content, title, product_id, _rank) in enumerate(rows, start=1):
        ranked[chunk_id] = ranked.get(chunk_id, 0.0) + 1.0 / (60 + rank)
        contents.setdefault(chunk_id, (content, title, product_id))

    ordered = sorted(ranked.items(), key=lambda item: item[1], reverse=True)[:k]
    return [
        SearchHit(content=contents[cid][0], title=contents[cid][1], score=score, product_id=contents[cid][2])
        for cid, score in ordered
    ]


async def reindex_all(session: AsyncSession, docs_dir: Path | None = None) -> list[Document]:
    """Reindexa todo: vuelve a ingerir cada documento y el catálogo.

    Se fuerza el reprocesado porque el checksum no cambia al cambiar el modelo
    de embeddings; sin `force` los vectores viejos se quedarían intactos.
    """
    return await ingest_all(session, docs_dir, force=True)
