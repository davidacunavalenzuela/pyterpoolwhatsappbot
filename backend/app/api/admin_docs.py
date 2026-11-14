"""Gestión de la base de conocimiento y prueba de búsqueda RAG."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep, require_admin, require_csrf
from app.config import settings
from app.logging import get_logger
from app.models import Document, InboundEvent
from app.schemas import DocumentListOut, DocumentOut
from app.services.rag import SUPPORTED_SUFFIXES, checksum_of, extract_text, search

router = APIRouter(
    prefix="/api/knowledge",
    tags=["documentos"],
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)
log = get_logger("admin.docs")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """Elimina rutas y caracteres peligrosos del nombre subido."""
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-") or "documento"


async def _enqueue_ingest(session: AsyncSession, payload: dict) -> None:
    """Encola la ingesta para el worker (no bloquea la subida)."""
    stmt = insert(InboundEvent).values(kind="ingest", payload=payload, wa_message_id=None)
    await session.execute(stmt)
    await session.commit()


@router.get("", response_model=DocumentListOut)
async def list_documents(session: SessionDep) -> DocumentListOut:
    rows = (await session.execute(select(Document).order_by(Document.id.desc()))).scalars().all()
    return DocumentListOut(items=[DocumentOut.model_validate(d) for d in rows], total=len(rows))


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    session: SessionDep,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    source_type: str = Form(default="manual"),
) -> Document:
    """Sube un manual y encola su indexación."""
    original = _safe_filename(file.filename or "documento")
    suffix = Path(original).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Formato no soportado. Usa: {', '.join(sorted(SUPPORTED_SUFFIXES))}.",
        )

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Archivo mayor a 20 MB.")

    checksum = checksum_of(data)
    existing = (
        await session.execute(select(Document).where(Document.checksum == checksum))
    ).scalar_one_or_none()
    if existing is not None:
        # Si el mismo archivo se subió antes pero falló la indexación, se
        # reencola en lugar de devolver un documento atascado en "error".
        if existing.status != "ready":
            session.add(
                InboundEvent(kind="ingest", payload={"document_id": existing.id}, wa_message_id=None)
            )
            await session.commit()
        return existing

    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.docs_dir / f"{uuid4().hex[:8]}-{original}"
    # Escritura en hilo aparte: no bloquear el event loop con IO de disco.
    await asyncio.to_thread(destination.write_bytes, data)

    document = Document(
        title=(title.strip() or Path(original).stem)[:200],
        source_type=source_type[:20],
        filename=str(destination),
        checksum=checksum,
        status="pending",
    )
    session.add(document)
    await session.flush()
    session.add(
        InboundEvent(kind="ingest", payload={"document_id": document.id}, wa_message_id=None)
    )
    await session.commit()
    await session.refresh(document)
    log.info("document_uploaded", document_id=document.id, filename=original)
    return document


@router.delete("/{document_id}")
async def delete_document(document_id: int, session: SessionDep) -> dict[str, bool]:
    """Borra el documento, sus chunks y el archivo si está dentro de docs/."""
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado.")

    if document.filename:
        path = Path(document.filename)
        try:
            # Comprobar que la ruta resuelta sigue dentro de docs/ (anti path traversal).
            # `resolve/exists` son IO de disco: se ejecutan fuera del event loop.
            resolved = await asyncio.to_thread(path.resolve)
            root = await asyncio.to_thread(settings.docs_dir.resolve)
            resolved.relative_to(root)
            if await asyncio.to_thread(path.exists):
                await asyncio.to_thread(path.unlink)
        except ValueError:
            log.warning("document_path_outside_docs", document_id=document.id)

    await session.delete(document)
    await session.commit()
    return {"ok": True}


@router.post("/reindex")
async def reindex_all(session: SessionDep) -> dict[str, str]:
    """Encola una reindexación completa (manuales + catálogo)."""
    await _enqueue_ingest(session, {"all": True})
    return {"status": "queued"}


@router.get("/search")
async def search_docs(session: SessionDep, q: str = Query(min_length=2, max_length=200)) -> dict:
    """Prueba la recuperación tal como la verá el agente."""
    hits = await search(session, q, k=5)
    return {
        "query": q,
        "hits": [
            {"titulo": hit.title, "score": round(hit.score, 4), "fragmento": hit.content[:600]}
            for hit in hits
        ],
    }


@router.post("/{document_id}/reindex", response_model=DocumentOut)
async def reindex_document(document_id: int, session: SessionDep) -> Document:
    """Reindexa un documento concreto en el acto (útil al corregir un PDF)."""
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado.")
    filename = Path(document.filename) if document.filename else None
    # `Path.exists` es IO bloqueante: en un endpoint async se delega a un hilo.
    if filename is None or not await asyncio.to_thread(filename.exists):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "El archivo original no está disponible.")
    try:
        text = await asyncio.to_thread(extract_text, filename)
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    from app.services.rag import ingest_document

    document = await ingest_document(session, document, text)
    await session.commit()
    await session.refresh(document)
    return document
