from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


SCHEMA_FILE = Path("/opt/llm-gateway/deploy/postgres_schema.sql")


@dataclass
class SessionMessage:
    id: str
    role: str
    content: str
    model_used: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    tokens_per_second: float | None
    created_at: datetime


@dataclass
class ChatSession:
    id: str
    title: str
    mode: str
    resolved_model: str | None
    route_reason: str | None
    summary: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[SessionMessage] = field(default_factory=list)


@dataclass
class MemorySummaryRecord:
    id: str
    session_id: str
    session_title: str
    summary_kind: str
    content: str
    source_message_count: int
    resolved_model: str | None
    created_at: datetime


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._lock = Lock()

    async def list_sessions(self, limit: int | None = None) -> list[ChatSession]:
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
            return sessions[:limit] if limit is not None else sessions

    async def create_session(self, title: str | None, mode: str) -> ChatSession:
        with self._lock:
            session_id = str(uuid4())
            now = utcnow()
            session = ChatSession(
                id=session_id,
                title=title or f"Session {len(self._sessions) + 1}",
                mode=mode,
                resolved_model=None,
                route_reason=None,
                summary=None,
                created_at=now,
                updated_at=now,
            )
            self._sessions[session_id] = session
            return session

    async def get_session(self, session_id: str) -> ChatSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    async def delete_session(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def reset_session(self, session_id: str) -> ChatSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.messages.clear()
            session.summary = None
            session.resolved_model = None
            session.route_reason = None
            session.updated_at = utcnow()
            return session

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        model_used: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        tokens_per_second: float | None = None,
    ) -> SessionMessage:
        with self._lock:
            session = self._sessions[session_id]
            message = SessionMessage(
                id=str(uuid4()),
                role=role,
                content=content,
                model_used=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                tokens_per_second=tokens_per_second,
                created_at=utcnow(),
            )
            session.messages.append(message)
            session.updated_at = utcnow()
            session.summary = _build_rolling_summary(session.messages)
            return message

    async def update_route(self, session_id: str, resolved_model: str, reason: str, mode: str) -> ChatSession:
        with self._lock:
            session = self._sessions[session_id]
            session.resolved_model = resolved_model
            session.route_reason = reason
            session.mode = mode
            session.updated_at = utcnow()
            return session

    async def list_memory_summaries(self, session_id: str | None = None, limit: int = 20) -> list[MemorySummaryRecord]:
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
            records: list[MemorySummaryRecord] = []
            for session in sessions:
                if session_id and session.id != session_id:
                    continue
                if not session.summary:
                    continue
                records.append(
                    MemorySummaryRecord(
                        id=f"inmemory-{session.id}",
                        session_id=session.id,
                        session_title=session.title,
                        summary_kind="rolling",
                        content=session.summary,
                        source_message_count=max(0, len(session.messages) - 6),
                        resolved_model=session.resolved_model,
                        created_at=session.updated_at,
                    )
                )
            return records[:limit]

    async def get_memory_stats(self) -> dict[str, int | str | bool]:
        with self._lock:
            sessions = list(self._sessions.values())
            return {
                "store_mode": "memory",
                "persistent": False,
                "sessions_count": len(sessions),
                "messages_count": sum(len(item.messages) for item in sessions),
                "summaries_count": sum(1 for item in sessions if item.summary),
            }


class PostgresSessionStore:
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

        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=5)
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

    async def list_sessions(self, limit: int | None = None) -> list[ChatSession]:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            if limit is not None:
                rows = await conn.fetch(
                    """
                    select id, title, selected_mode, resolved_model, route_reason, rolling_summary, created_at, updated_at
                    from chat_sessions
                    order by updated_at desc
                    limit $1
                    """,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    select id, title, selected_mode, resolved_model, route_reason, rolling_summary, created_at, updated_at
                    from chat_sessions
                    order by updated_at desc
                    """
                )
            sessions = []
            for row in rows:
                messages = await self._fetch_messages(conn, row["id"])
                sessions.append(self._session_from_row(row, messages))
            return sessions

    async def create_session(self, title: str | None, mode: str) -> ChatSession:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into chat_sessions (title, selected_mode)
                values ($1, $2)
                returning id, title, selected_mode, resolved_model, route_reason, rolling_summary, created_at, updated_at
                """,
                title or "Session",
                mode,
            )
            return self._session_from_row(row, [])

    async def get_session(self, session_id: str) -> ChatSession | None:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select id, title, selected_mode, resolved_model, route_reason, rolling_summary, created_at, updated_at
                from chat_sessions
                where id = $1::uuid
                """,
                session_id,
            )
            if row is None:
                return None
            messages = await self._fetch_messages(conn, session_id)
            return self._session_from_row(row, messages)

    async def delete_session(self, session_id: str) -> bool:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            result = await conn.execute("delete from chat_sessions where id = $1::uuid", session_id)
            return result.endswith("1")

    async def reset_session(self, session_id: str) -> ChatSession | None:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("select 1 from chat_sessions where id = $1::uuid", session_id)
            if exists is None:
                return None
            await conn.execute("delete from chat_messages where session_id = $1::uuid", session_id)
            await conn.execute("delete from memory_summaries where session_id = $1::uuid", session_id)
            row = await conn.fetchrow(
                """
                update chat_sessions
                set resolved_model = null,
                    route_reason = null,
                    rolling_summary = null,
                    updated_at = now()
                where id = $1::uuid
                returning id, title, selected_mode, resolved_model, route_reason, rolling_summary, created_at, updated_at
                """,
                session_id,
            )
            return self._session_from_row(row, [])

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        model_used: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        tokens_per_second: float | None = None,
    ) -> SessionMessage:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into chat_messages (
                  session_id, role, content, model_used, token_estimate,
                  prompt_tokens, completion_tokens, total_tokens, tokens_per_second
                )
                values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
                returning id, role, content, model_used, prompt_tokens, completion_tokens, total_tokens, tokens_per_second, created_at
                """,
                session_id,
                role,
                content,
                model_used,
                max(1, len(content) // 4),
                prompt_tokens,
                completion_tokens,
                total_tokens,
                tokens_per_second,
            )
            messages = await self._fetch_messages(conn, session_id)
            summary = _build_rolling_summary(messages)
            await conn.execute(
                """
                update chat_sessions
                set rolling_summary = $2, updated_at = now()
                where id = $1::uuid
                """,
                session_id,
                summary,
            )
            if summary:
                await conn.execute(
                    """
                    insert into memory_summaries (session_id, summary_kind, content, source_message_count)
                    values ($1::uuid, 'rolling', $2, $3)
                    """,
                    session_id,
                    summary,
                    max(0, len(messages) - 6),
                )
            return SessionMessage(
                id=str(row["id"]),
                role=row["role"],
                content=row["content"],
                model_used=row["model_used"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
                tokens_per_second=row["tokens_per_second"],
                created_at=row["created_at"],
            )

    async def update_route(self, session_id: str, resolved_model: str, reason: str, mode: str) -> ChatSession:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                update chat_sessions
                set selected_mode = $2,
                    resolved_model = $3,
                    route_reason = $4,
                    updated_at = now()
                where id = $1::uuid
                returning id, title, selected_mode, resolved_model, route_reason, rolling_summary, created_at, updated_at
                """,
                session_id,
                mode,
                resolved_model,
                reason,
            )
            await conn.execute(
                """
                insert into routing_events (session_id, requested_mode, resolved_model, reason)
                values ($1::uuid, $2, $3, $4)
                """,
                session_id,
                mode,
                resolved_model,
                reason,
            )
            messages = await self._fetch_messages(conn, session_id)
            return self._session_from_row(row, messages)

    async def list_memory_summaries(self, session_id: str | None = None, limit: int = 20) -> list[MemorySummaryRecord]:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            if session_id:
                rows = await conn.fetch(
                    """
                    select
                      ms.id,
                      ms.session_id,
                      cs.title as session_title,
                      ms.summary_kind,
                      ms.content,
                      ms.source_message_count,
                      cs.resolved_model,
                      ms.created_at
                    from memory_summaries ms
                    join chat_sessions cs on cs.id = ms.session_id
                    where ms.session_id = $1::uuid
                    order by ms.created_at desc
                    limit $2
                    """,
                    session_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    select
                      ms.id,
                      ms.session_id,
                      cs.title as session_title,
                      ms.summary_kind,
                      ms.content,
                      ms.source_message_count,
                      cs.resolved_model,
                      ms.created_at
                    from memory_summaries ms
                    join chat_sessions cs on cs.id = ms.session_id
                    order by ms.created_at desc
                    limit $1
                    """,
                    limit,
                )
            return [
                MemorySummaryRecord(
                    id=str(row["id"]),
                    session_id=str(row["session_id"]),
                    session_title=row["session_title"],
                    summary_kind=row["summary_kind"],
                    content=row["content"],
                    source_message_count=row["source_message_count"],
                    resolved_model=row["resolved_model"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_memory_stats(self) -> dict[str, int | str | bool]:
        pool = await self._pool_instance()
        async with pool.acquire() as conn:
            sessions_count = await conn.fetchval("select count(*) from chat_sessions")
            messages_count = await conn.fetchval("select count(*) from chat_messages")
            summaries_count = await conn.fetchval("select count(*) from memory_summaries")
            return {
                "store_mode": "postgres",
                "persistent": True,
                "sessions_count": int(sessions_count or 0),
                "messages_count": int(messages_count or 0),
                "summaries_count": int(summaries_count or 0),
            }

    async def _fetch_messages(self, conn, session_id: str) -> list[SessionMessage]:
        rows = await conn.fetch(
            """
            select id, role, content, model_used, prompt_tokens, completion_tokens, total_tokens, tokens_per_second, created_at
            from chat_messages
            where session_id = $1::uuid
            order by created_at asc
            """,
            session_id,
        )
        return [
            SessionMessage(
                id=str(row["id"]),
                role=row["role"],
                content=row["content"],
                model_used=row["model_used"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
                tokens_per_second=row["tokens_per_second"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _session_from_row(self, row, messages: list[SessionMessage]) -> ChatSession:
        return ChatSession(
            id=str(row["id"]),
            title=row["title"],
            mode=row["selected_mode"],
            resolved_model=row["resolved_model"],
            route_reason=row["route_reason"],
            summary=row["rolling_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            messages=messages,
        )


_in_memory_store = InMemorySessionStore()
_postgres_store: PostgresSessionStore | None = None


def get_session_store(settings: Settings):
    global _postgres_store
    if settings.database_url:
        if _postgres_store is None or _postgres_store._database_url != settings.database_url:
            _postgres_store = PostgresSessionStore(settings.database_url)
        return _postgres_store
    return _in_memory_store


def _build_rolling_summary(messages: list[SessionMessage]) -> str | None:
    if len(messages) <= 10:
        return None

    older_messages = messages[:-6]
    lines: list[str] = []
    for message in older_messages[-8:]:
        snippet = " ".join(message.content.strip().split())
        if len(snippet) > 180:
            snippet = f"{snippet[:177]}..."
        lines.append(f"{message.role}: {snippet}")

    if not lines:
        return None

    return "Zusammenfassung aelterer Nachrichten:\n" + "\n".join(lines)
