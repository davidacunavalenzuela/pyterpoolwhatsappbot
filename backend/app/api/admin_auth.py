"""Autenticación del panel (cookie firmada + CSRF)."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import AdminDep, SessionDep, require_csrf
from app.config import settings
from app.logging import get_logger
from app.models import AdminUser
from app.schemas import LoginIn, MeOut
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_session_token,
    hash_password,
    needs_rehash,
    new_csrf_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger("auth")

# Límite de intentos en memoria: suficiente para un único proceso de API.
# Si algún día se escala la API, esto debería pasar a Postgres o Redis.
_attempts: dict[str, deque[float]] = defaultdict(deque)
_WINDOW_SECONDS = 15 * 60
_MAX_ATTEMPTS = 10


def _check_rate_limit(key: str) -> None:
    """Ventana deslizante simple por IP."""
    now = time.monotonic()
    bucket = _attempts[key]
    while bucket and now - bucket[0] > _WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= _MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos. Espera unos minutos.")
    bucket.append(now)


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    """Cookie de sesión HttpOnly + cookie CSRF legible por el panel."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=SESSION_MAX_AGE,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(payload: LoginIn, request: Request, response: Response, session: SessionDep) -> dict:
    """Valida credenciales y emite la sesión.

    El mensaje de error es genérico para no filtrar qué correos existen. El
    hash se verifica siempre que el usuario exista; si no existe se ejecuta un
    hash dummy para igualar tiempos de respuesta.
    """
    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    _check_rate_limit(f"{client_ip}:{payload.email.lower()}")

    email = payload.email.strip().lower()
    user = (
        await session.execute(select(AdminUser).where(AdminUser.email == email))
    ).scalar_one_or_none()

    if user is None:
        verify_password(payload.password, hash_password("dummy-password-para-timing"))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Correo o clave incorrectos.")

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Correo o clave incorrectos.")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Usuario desactivado.")

    # Rehash transparente si se endurecieron los parámetros de argon2.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        await session.commit()

    _attempts.pop(f"{client_ip}:{email}", None)
    _set_session_cookies(response, create_session_token(user.id, user.email), new_csrf_token())
    log.info("login_ok", user_id=user.id)
    return {"ok": True, "email": user.email}


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(response: Response) -> dict:
    """Elimina ambas cookies.

    Exige CSRF como el resto de POST del panel: SameSite=Lax ya bloquea el
    envío de cookies en POST cross-site, pero mantener la comprobación evita
    que un formulario de otro origen fuerce el cierre de sesión (y deja una
    única política de mutaciones fácil de auditar).
    """
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeOut)
async def me(user: AdminDep) -> AdminUser:
    """Devuelve el usuario de la sesión (usado como guard del panel)."""
    return user
