"""Configuración central tipada.

Todas las variables se leen del entorno (el compose inyecta el `.env`). En
`APP_ENV=prod` se validan los secretos críticos al arrancar para fallar rápido
en lugar de descubrir un webhook inválido en producción.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dimensión fija del vector: la columna `document_chunks.embedding` es
# `vector(384)` en la migración. Cambiar de modelo de embeddings exige migración.
EMBEDDING_DIM = 384


class Settings(BaseSettings):
    """Variables de entorno del servicio."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Entorno ----------------------------------------------------------
    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8080"

    # --- Base de datos ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://wa:wa@localhost:5432/wa"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --- Panel / sesiones ------------------------------------------------
    session_secret: str = "dev-only-secret-cambiar-en-produccion"
    admin_email: str = "admin@example.com"
    admin_password: str = "admin1234"
    cookie_secure: bool = False

    # --- WhatsApp Cloud API ----------------------------------------------
    wa_phone_number_id: str = ""
    wa_waba_id: str = ""
    wa_access_token: str = ""
    wa_verify_token: str = "mi_token_secreto_123"
    wa_app_secret: str = ""
    meta_graph_version: str = "v22.0"

    # --- LLM (Groq, API compatible con OpenAI) ---------------------------
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_fallback_model: str = "openai/gpt-oss-20b"
    llm_temperature: float = 0.3
    llm_max_output_tokens: int = 400
    # Presupuesto de entrada en caracteres (~4 chars/token) para respetar los
    # 8.000 tokens por minuto del free tier de Groq.
    llm_max_input_chars: int = 6000
    # Espaciado mínimo entre llamadas: 2,2 s => ~27 req/min.
    llm_min_interval_seconds: float = 2.2
    llm_max_steps: int = 5
    llm_history_messages: int = 8
    whisper_model: str = "whisper-large-v3-turbo"

    # --- Visión (Gemini free, solo imágenes) -----------------------------
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    vision_model: str = "gemini-3.5-flash-lite"
    vision_max_pixels: int = 1024
    vision_max_output_tokens: int = 250

    # --- RAG --------------------------------------------------------------
    # Modelo multilingüe de 384 dim compatible con fastembed 0.7.x. El e5-small
    # ya no aparece en `TextEmbedding.list_supported_models()`; este sí, y
    # mantiene la dimensión de la columna `vector(384)`.
    embeddings_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embeddings_dim: int = EMBEDDING_DIM
    embeddings_cache_dir: str = "/models"
    rag_top_k: int = 5
    rag_min_score: float = 0.25
    rag_chunk_chars: int = 900
    rag_chunk_overlap: int = 150

    # --- Media ------------------------------------------------------------
    max_media_bytes: int = 20 * 1024 * 1024
    docs_dir: Path = Field(default=Path("/app/docs"))

    # --- Worker -----------------------------------------------------------
    worker_poll_seconds: float = 1.0
    worker_max_attempts: int = 5
    worker_stuck_after_seconds: int = 300

    # --- Cloudflare (no lo usa la app, pero vive en el mismo .env) --------
    tunnel_token: str = ""

    @property
    def graph_url(self) -> str:
        """Base de la Graph API, con la versión configurable."""
        return f"https://graph.facebook.com/{self.meta_graph_version}"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.embeddings_dim != EMBEDDING_DIM:
            raise ValueError(
                f"EMBEDDINGS_DIM debe ser {EMBEDDING_DIM}: la columna es vector({EMBEDDING_DIM})."
            )
        if self.is_prod:
            missing = [
                name
                for name, value in {
                    "WA_ACCESS_TOKEN": self.wa_access_token,
                    "WA_APP_SECRET": self.wa_app_secret,
                    "WA_PHONE_NUMBER_ID": self.wa_phone_number_id,
                    "GROQ_API_KEY": self.groq_api_key,
                    "SESSION_SECRET": self.session_secret,
                }.items()
                if not value or value.lower().startswith(("completar", "cambia", "rotar"))
            ]
            if missing:
                raise ValueError(f"Variables obligatorias sin configurar en prod: {', '.join(missing)}")
            if len(self.session_secret) < 32:
                raise ValueError("SESSION_SECRET debe tener al menos 32 caracteres en producción.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la configuración cacheada (una lectura de entorno por proceso)."""
    return Settings()


settings = get_settings()
