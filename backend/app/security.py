"""Primitivas de seguridad: hashing de claves, sesiones, CSRF y firma de webhooks."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

# Parámetros por defecto de argon2id (recomendados por OWASP). El hash resultante
# incluye sal aleatoria, así que dos usuarios con la misma clave no colisionan.
_hasher = PasswordHasher()

SESSION_COOKIE = "wa_session"
CSRF_COOKIE = "wa_csrf"
SESSION_MAX_AGE = 12 * 60 * 60  # 12 horas

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="wa-session")


def hash_password(password: str) -> str:
    """Genera un hash argon2id de la contraseña."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica la contraseña contra el hash. Nunca propaga errores de formato."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Indica si el hash usa parámetros obsoletos y conviene regenerarlo."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_session_token(user_id: int, email: str) -> str:
    """Firma una cookie de sesión con expiración verificada al leer."""
    return _serializer.dumps({"uid": user_id, "email": email})


def read_session_token(token: str) -> dict[str, Any] | None:
    """Devuelve el payload de la sesión o None si expiró/está manipulada."""
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def new_csrf_token() -> str:
    """Token aleatorio para el patrón double-submit cookie."""
    return secrets.token_urlsafe(32)


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Valida `X-Hub-Signature-256` usando el App Secret de Meta.

    Se compara en tiempo constante para no filtrar información por timing.
    Si no hay App Secret configurado se rechaza: aceptar sin validar permitiría
    que cualquiera inyecte pedidos falsos.
    """
    if not settings.wa_app_secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.wa_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def verify_verify_token(candidate: str | None) -> bool:
    """Compara el `hub.verify_token` del handshake de Meta en tiempo constante."""
    if not candidate or not settings.wa_verify_token:
        return False
    return hmac.compare_digest(candidate, settings.wa_verify_token)
