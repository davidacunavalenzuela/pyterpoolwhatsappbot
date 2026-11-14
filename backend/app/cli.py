"""Comandos de administración (seed, usuarios, suscripción y RAG).

Uso dentro del contenedor:
    docker compose run --rm api cli seed
    docker compose run --rm api cli create-admin --email ... --password ...
    docker compose run --rm api cli subscribe
    docker compose run --rm api cli ingest
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select, text

from app.config import settings
from app.db import SessionLocal
from app.logging import configure_logging, get_logger
from app.models import AdminUser, AppSetting, Document, Product
from app.security import hash_password
from app.services.rag import ingest_all, reindex_all
from app.services.settings_service import DEFAULT_SETTINGS, save_settings
from app.services.whatsapp import WhatsAppError, whatsapp

log = get_logger("cli")


async def cmd_seed(_: argparse.Namespace) -> None:
    """Crea el usuario admin, los ajustes por defecto y verifica la conexión."""
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
        existing = (
            await session.execute(select(AdminUser).where(AdminUser.email == settings.admin_email.lower()))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                AdminUser(
                    email=settings.admin_email.lower(),
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                )
            )
            print(f"Admin creado: {settings.admin_email}")
        else:
            print(f"Admin ya existe: {settings.admin_email}")

        # `load_settings` fusiona los defaults en memoria; para saber si la tabla
        # está vacía hay que mirar las filas reales (si no, nunca se persistirían).
        stored = await session.execute(select(AppSetting.key))
        if not stored.scalars().all():
            await save_settings(session, DEFAULT_SETTINGS)
        await session.commit()
    print("Seed completado.")


async def cmd_create_admin(args: argparse.Namespace) -> None:
    """Crea o actualiza un usuario del panel con la clave indicada."""
    email = args.email.strip().lower()
    if len(args.password) < 10:
        raise SystemExit("La contraseña debe tener al menos 10 caracteres.")
    async with SessionLocal() as session:
        user = (
            await session.execute(select(AdminUser).where(AdminUser.email == email))
        ).scalar_one_or_none()
        if user is None:
            session.add(AdminUser(email=email, password_hash=hash_password(args.password), role="admin"))
            action = "creado"
        else:
            # Cambiar la clave revoca implícitamente sesiones viejas solo cuando
            # expiran (la cookie no guarda la contraseña); documentado en el runbook.
            user.password_hash = hash_password(args.password)
            user.is_active = True
            action = "actualizado"
        await session.commit()
    print(f"Usuario {email} {action}.")


async def cmd_subscribe(_: argparse.Namespace) -> None:
    """Suscribe la app al WABA para recibir mensajes por webhook."""
    try:
        result = await whatsapp.subscribe_app()
    except WhatsAppError as exc:
        raise SystemExit(f"No se pudo suscribir: {exc}") from None
    finally:
        await whatsapp.aclose()
    print(f"Respuesta de Meta: {result}")


async def cmd_ingest(args: argparse.Namespace) -> None:
    """Indexa manuales y catálogo para el RAG."""
    directory = Path(args.path) if args.path else settings.docs_dir
    async with SessionLocal() as session:
        # `--force` regenera chunks/embeddings aunque el checksum coincida
        # (obligatorio si se cambió de modelo de embeddings).
        documents = (
            await reindex_all(session, directory)
            if args.force
            else await ingest_all(session, directory)
        )
        await session.commit()
        for doc in documents:
            print(f"- {doc.title}: {doc.status} ({doc.chunk_count} chunks) {doc.error or ''}")


async def cmd_status(_: argparse.Namespace) -> None:
    """Muestra un resumen operativo rápido."""
    async with SessionLocal() as session:
        products = (await session.execute(select(Product))).scalars().all()
        documents = (await session.execute(select(Document))).scalars().all()
        queue = (
            await session.execute(
                text("SELECT status, count(*) FROM inbound_events GROUP BY status ORDER BY status")
            )
        ).all()
    print(f"Productos: {len(products)} ({sum(1 for p in products if p.active)} activos)")
    print(f"Documentos: {len(documents)} ({sum(1 for d in documents if d.status == 'ready')} listos)")
    print("Cola:", {status: count for status, count in queue} or "vacía")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Comandos del asistente de WhatsApp")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Crea admin y ajustes por defecto")
    seed.set_defaults(func=cmd_seed)

    create_admin = sub.add_parser("create-admin", help="Crea o resetea un usuario del panel")
    create_admin.add_argument("--email", required=True)
    create_admin.add_argument("--password", required=True)
    create_admin.set_defaults(func=cmd_create_admin)

    subscribe = sub.add_parser("subscribe", help="Suscribe la app al WABA en Meta")
    subscribe.set_defaults(func=cmd_subscribe)

    ingest = sub.add_parser("ingest", help="Indexa manuales y catálogo")
    ingest.add_argument("--path", default="")
    ingest.add_argument("--force", action="store_true", help="Reindexa aunque no cambie el checksum")
    ingest.set_defaults(func=cmd_ingest)

    status = sub.add_parser("status", help="Resumen de datos y cola")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
