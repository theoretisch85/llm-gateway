from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings


SCHEMA_FILE = Path("/opt/llm-gateway/deploy/postgres_schema.sql")


def _redact_database_url(database_url: str) -> str:
    if not database_url:
        return ""
    parts = urlsplit(database_url)
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}:***@{hostname}"
    else:
        netloc = hostname
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def database_status(settings: Settings) -> dict[str, object]:
    if not settings.database_url:
        return {
            "store_mode": "memory",
            "configured": False,
            "connected": False,
            "schema_ready": False,
            "database_url_redacted": "",
            "message": "DATABASE_URL ist leer. Sessions und Summaries liegen nur im RAM.",
            "sessions_count": None,
            "messages_count": None,
        }

    try:
        import asyncpg
    except ModuleNotFoundError:
        return {
            "store_mode": "postgres",
            "configured": True,
            "connected": False,
            "schema_ready": False,
            "database_url_redacted": _redact_database_url(settings.database_url),
            "message": "asyncpg ist nicht installiert. PostgreSQL kann so nicht genutzt werden.",
            "sessions_count": None,
            "messages_count": None,
        }

    conn = await asyncpg.connect(settings.database_url, timeout=5.0)
    try:
        schema_ready = bool(
            await conn.fetchval("select to_regclass('public.chat_sessions') is not null")
        )
        sessions_count = None
        messages_count = None
        if schema_ready:
            sessions_count = await conn.fetchval("select count(*) from chat_sessions")
            messages_count = await conn.fetchval("select count(*) from chat_messages")
        return {
            "store_mode": "postgres",
            "configured": True,
            "connected": True,
            "schema_ready": schema_ready,
            "database_url_redacted": _redact_database_url(settings.database_url),
            "message": "PostgreSQL erreichbar." if schema_ready else "PostgreSQL erreichbar, Schema fehlt noch.",
            "sessions_count": sessions_count,
            "messages_count": messages_count,
        }
    finally:
        await conn.close()


async def initialize_database_schema(settings: Settings) -> dict[str, object]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL ist leer. Ohne PostgreSQL-Ziel kann kein Schema initialisiert werden.")
    if not SCHEMA_FILE.exists():
        raise RuntimeError("postgres_schema.sql wurde nicht gefunden.")

    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError("asyncpg ist nicht installiert. Bitte requirements neu installieren.") from exc

    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn = await asyncpg.connect(settings.database_url, timeout=5.0)
    try:
        await conn.execute(schema_sql)
    finally:
        await conn.close()

    return await database_status(settings)
