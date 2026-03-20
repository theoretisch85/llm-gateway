from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SCHEMA_FILE = Path("/opt/llm-gateway/deploy/postgres_schema.sql")
NOTE_PATTERNS = [
    re.compile(
        r"^\s*(?:merke(?:\s+ha)?|ha[- ]note)\s+(?P<entity_id>[a-z0-9_]+\.[a-z0-9_]+)\s*:\s*(?P<note>.+?)\s*$",
        re.IGNORECASE | re.DOTALL,
    ),
]


@dataclass
class HomeAssistantEntityNote:
    entity_id: str
    note: str
    created_at: object
    updated_at: object


@dataclass
class HomeAssistantAlias:
    alias: str
    domain: str
    entity_ids: list[str]
    learned_from: str | None
    created_at: object
    updated_at: object


class HomeAssistantEntityNoteStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool = None

    async def _pool_instance(self):
        if self._pool is not None:
            return self._pool

        try:
            import asyncpg
        except ModuleNotFoundError as exc:
            raise RuntimeError("DATABASE_URL ist gesetzt, aber asyncpg ist nicht installiert.") from exc

        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=3)
        await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        if self._pool is None:
            return
        if not SCHEMA_FILE.exists():
            raise RuntimeError("postgres_schema.sql wurde nicht gefunden.")
        schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(schema_sql)

    async def list_notes(self, limit: int = 200) -> list[HomeAssistantEntityNote]:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select entity_id, note, created_at, updated_at
                from home_assistant_entity_notes
                order by updated_at desc
                limit $1
                """,
                limit,
            )
        return [
            HomeAssistantEntityNote(
                entity_id=row["entity_id"],
                note=row["note"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def get_notes_by_entity_ids(self, entity_ids: list[str]) -> list[HomeAssistantEntityNote]:
        if not entity_ids:
            return []
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select entity_id, note, created_at, updated_at
                from home_assistant_entity_notes
                where entity_id = any($1::text[])
                order by updated_at desc
                """,
                entity_ids,
            )
        return [
            HomeAssistantEntityNote(
                entity_id=row["entity_id"],
                note=row["note"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def upsert_note(self, entity_id: str, note: str) -> HomeAssistantEntityNote:
        clean_entity_id = entity_id.strip().lower()
        clean_note = note.strip()
        if not clean_entity_id or "." not in clean_entity_id:
            raise RuntimeError("Entity-ID fuer Home Assistant ist ungueltig.")
        if not clean_note:
            raise RuntimeError("Notiz fuer Home Assistant darf nicht leer sein.")

        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into home_assistant_entity_notes (entity_id, note)
                values ($1, $2)
                on conflict (entity_id)
                do update set note = excluded.note, updated_at = now()
                returning entity_id, note, created_at, updated_at
                """,
                clean_entity_id,
                clean_note,
            )
        return HomeAssistantEntityNote(
            entity_id=row["entity_id"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class HomeAssistantAliasStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool = None

    async def _pool_instance(self):
        if self._pool is not None:
            return self._pool

        try:
            import asyncpg
        except ModuleNotFoundError as exc:
            raise RuntimeError("DATABASE_URL ist gesetzt, aber asyncpg ist nicht installiert.") from exc

        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=3)
        await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        if self._pool is None:
            return
        if not SCHEMA_FILE.exists():
            raise RuntimeError("postgres_schema.sql wurde nicht gefunden.")
        schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(schema_sql)

    async def find_alias(self, alias: str, domain: str | None = None) -> HomeAssistantAlias | None:
        clean_alias = normalize_home_assistant_alias(alias)
        if not clean_alias:
            return None
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select alias, domain, entity_ids, learned_from, created_at, updated_at
                from home_assistant_aliases
                where alias = $1
                  and ($2::text is null or domain = $2)
                limit 1
                """,
                clean_alias,
                domain,
            )
        return _alias_from_row(row) if row else None

    async def upsert_alias(
        self,
        *,
        alias: str,
        domain: str,
        entity_ids: list[str],
        learned_from: str | None = None,
    ) -> HomeAssistantAlias:
        clean_alias = normalize_home_assistant_alias(alias)
        clean_domain = (domain or "").strip().lower()
        clean_entity_ids = [item.strip().lower() for item in entity_ids if item and item.strip()]
        if not clean_alias:
            raise RuntimeError("Alias fuer Home Assistant darf nicht leer sein.")
        if not clean_domain:
            raise RuntimeError("Domain fuer Home Assistant Alias darf nicht leer sein.")
        if not clean_entity_ids:
            raise RuntimeError("Alias braucht mindestens eine Entity-ID.")

        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into home_assistant_aliases (alias, domain, entity_ids, learned_from)
                values ($1, $2, $3::jsonb, $4)
                on conflict (alias)
                do update set
                  domain = excluded.domain,
                  entity_ids = excluded.entity_ids,
                  learned_from = excluded.learned_from,
                  updated_at = now()
                returning alias, domain, entity_ids, learned_from, created_at, updated_at
                """,
                clean_alias,
                clean_domain,
                _json_entity_ids(clean_entity_ids),
                (learned_from or "").strip() or None,
            )
        if row is None:
            raise RuntimeError("Home-Assistant-Alias konnte nicht gespeichert werden.")
        return _alias_from_row(row)

    async def list_aliases(self, limit: int = 200) -> list[HomeAssistantAlias]:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select alias, domain, entity_ids, learned_from, created_at, updated_at
                from home_assistant_aliases
                order by updated_at desc
                limit $1
                """,
                limit,
            )
        return [_alias_from_row(row) for row in rows]


_note_store: HomeAssistantEntityNoteStore | None = None
_alias_store: HomeAssistantAliasStore | None = None


def get_home_assistant_note_store(settings):
    global _note_store
    if not settings.database_url:
        return None
    if _note_store is None or _note_store._database_url != settings.database_url:
        _note_store = HomeAssistantEntityNoteStore(settings.database_url)
    return _note_store


def get_home_assistant_alias_store(settings):
    global _alias_store
    if not settings.database_url:
        return None
    if _alias_store is None or _alias_store._database_url != settings.database_url:
        _alias_store = HomeAssistantAliasStore(settings.database_url)
    return _alias_store


def parse_home_assistant_note_instruction(message: str) -> tuple[str, str] | None:
    text = (message or "").strip()
    for pattern in NOTE_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group("entity_id").strip().lower(), match.group("note").strip()
    return None


def normalize_home_assistant_alias(alias: str) -> str:
    text = (alias or "").strip().lower()
    text = re.sub(r"[^\w\säöüß.-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or "." in text:
        return text
    tokens = [token for token in text.split(" ") if token]
    if len(tokens) <= 1:
        return text
    return " ".join(sorted(dict.fromkeys(tokens)))


def _json_entity_ids(entity_ids: list[str]) -> str:
    import json

    return json.dumps(entity_ids, ensure_ascii=False)


def _alias_from_row(row) -> HomeAssistantAlias:
    entity_ids = row["entity_ids"]
    if isinstance(entity_ids, str):
        import json

        entity_ids = json.loads(entity_ids)
    return HomeAssistantAlias(
        alias=row["alias"],
        domain=row["domain"],
        entity_ids=[str(item).strip().lower() for item in (entity_ids or []) if str(item).strip()],
        learned_from=row["learned_from"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
