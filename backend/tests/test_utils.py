"""Pruebas unitarias de utilidades puras (sin base de datos ni red)."""

from __future__ import annotations

import hashlib
import hmac

from app.config import settings
from app.security import verify_verify_token, verify_webhook_signature
from app.services.orders import format_money
from app.services.rag import chunk_text
from app.services.whatsapp import split_text


def test_verify_webhook_signature_ok(monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", "secreto-de-prueba")
    body = b'{"entry":[]}'
    digest = hmac.new(b"secreto-de-prueba", body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, f"sha256={digest}")


def test_verify_webhook_signature_rejects_tampering(monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", "secreto-de-prueba")
    body = b'{"entry":[]}'
    digest = hmac.new(b"secreto-de-prueba", body, hashlib.sha256).hexdigest()

    assert not verify_webhook_signature(b'{"entry":[1]}', f"sha256={digest}")
    assert not verify_webhook_signature(body, None)
    assert not verify_webhook_signature(body, "sha1=abc")


def test_verify_webhook_signature_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", "")
    body = b"{}"
    # Sin App Secret no se puede validar: se rechaza, nunca se acepta a ciegas.
    assert not verify_webhook_signature(body, "sha256=" + "0" * 64)


def test_verify_token_constant_compare(monkeypatch):
    monkeypatch.setattr(settings, "wa_verify_token", "mi_token_secreto_123")
    assert verify_verify_token("mi_token_secreto_123")
    assert not verify_verify_token("otro")
    assert not verify_verify_token(None)


def test_split_text_respects_limit():
    text = "\n\n".join(["fragmento " * 40 for _ in range(40)])
    chunks = split_text(text, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks)  # no se pierde todo el contenido


def test_split_text_short_message_is_untouched():
    assert split_text("Hola, ¿tienen stock?") == ["Hola, ¿tienen stock?"]
    assert split_text("   ") == []


def test_chunk_text_prefers_paragraphs_and_overlaps():
    # Párrafos de 135 caracteres: dos caben en 300, tres no. Así el troceado
    # agrupa párrafos completos y el solape es determinista.
    paragraphs = [f"Párrafo {index} " + "contenido útil " * 9 for index in range(6)]
    chunks = chunk_text("\n\n".join(paragraphs), size=300, overlap=50)

    assert len(chunks) >= 2
    # El solape aporta contexto del bloque anterior a cada chunk siguiente.
    # Se compara con lstrip porque el chunk final se normaliza con strip().
    assert chunks[1].startswith(chunks[0][-50:].lstrip())
    assert all(len(chunk) <= 300 + 50 + 1 for chunk in chunks)


def test_chunk_text_drops_tiny_fragments():
    assert chunk_text("corto", size=300, overlap=0) == []


def test_format_money_clp_has_no_decimals():
    assert format_money(12990) == "$12.990"
    assert format_money(12990.4, "CLP") == "$12.990"


def test_format_money_other_currency_keeps_cents():
    assert format_money(19.9, "USD") == "$19.90"