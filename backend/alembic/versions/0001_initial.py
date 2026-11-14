"""Esquema inicial.

Se crea a partir de `Base.metadata` para que la migración y los modelos no
puedan divergir en el arranque. A partir de la segunda migración se usarán
operaciones explícitas (`alembic revision --autogenerate`) sobre este baseline.
"""

from __future__ import annotations

from alembic import op
from app.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # La extensión debe existir ANTES de crear columnas de tipo vector.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=op.get_bind())

    # Índice HNSW para búsqueda por similitud coseno: mejor recall que ivfflat
    # sin necesidad de entrenar con datos existentes (importante al arrancar).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )
    # Índice léxico en español para la rama keyword del RAG híbrido.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_fts "
        "ON document_chunks USING gin (to_tsvector('spanish', content))"
    )


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
    op.execute("DROP EXTENSION IF EXISTS vector")
