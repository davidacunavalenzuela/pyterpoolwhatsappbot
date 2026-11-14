"""Dependencias compartidas de la API administrativa."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import AdminUser
from app.security import CSRF_COOKIE, SESSION_COOKIE, read_session_token

SessionDep = Annotated[AsyncSession, Depends(get_session)]

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def require_admin(request: Request, session: SessionDep) -> AdminUser:
    """Valida la cookie de sesión firmada y devuelve el usuario activo.

    La sesión no usa JWT: la cookie firmada con itsdangerous es suficiente y
    permite revocar usuarios desactivándolos en la base de datos.
    """
    token = request.cookies.get(SESSION_COOKIE)
    payload = read_session_token(token) if token else None
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión inválida o expirada.")

    user = await session.get(AdminUser, payload.get("uid"))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario no autorizado.")
    return user


async def require_csrf(request: Request) -> None:
    """Double-submit cookie para métodos que mutan estado.

    El frontend lee la cookie `wa_csrf` (legible por JS) y la envía en
    `X-CSRF-Token`. Un sitio externo no puede leerla por la política de origen,
    así que no puede falsificar el header.
    """
    if request.method in SAFE_METHODS:
        return
    header = request.headers.get("x-csrf-token")
    cookie = request.cookies.get(CSRF_COOKIE)
    if not header or not cookie or not hmac.compare_digest(header, cookie):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token CSRF inválido.")


AdminDep = Annotated[AdminUser, Depends(require_admin)]
