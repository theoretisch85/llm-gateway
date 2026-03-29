from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from fastapi import UploadFile

from app.config import Settings
from app.services.llamacpp_client import LlamaCppError, LlamaCppTimeoutError
from app.services.vision import VisionConfigError, analyze_image_bytes, vision_is_configured


STORAGE_PROFILE_FILE = Path("/opt/llm-gateway/.runtime/storage_locations.json")
ALLOWED_STORAGE_TYPES = {"local", "smb_mount"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yaml", ".yml"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD_SIZE_BYTES = 25 * 1024 * 1024
DOCUMENT_SCHEMA_SQL = """
create table if not exists document_assets (
  id uuid primary key default gen_random_uuid(),
  storage_location_id text not null,
  storage_location_name text not null,
  title text,
  file_name text not null,
  media_type text,
  size_bytes bigint not null,
  relative_path text not null,
  extracted_text text,
  text_excerpt text,
  asset_kind text not null default 'document',
  tags text,
  created_at timestamptz not null default now()
);

alter table document_assets add column if not exists asset_kind text not null default 'document';

create index if not exists idx_document_assets_created
  on document_assets(created_at desc);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_profile_parent() -> None:
    STORAGE_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_profile_state() -> dict[str, object]:
    _ensure_profile_parent()
    if not STORAGE_PROFILE_FILE.exists():
        return {"active_profile_id": None, "profiles": []}
    try:
        data = json.loads(STORAGE_PROFILE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active_profile_id": None, "profiles": []}
    if not isinstance(data, dict):
        return {"active_profile_id": None, "profiles": []}
    if not isinstance(data.get("profiles"), list):
        data["profiles"] = []
    return data


def _save_profile_state(state: dict[str, object]) -> None:
    _ensure_profile_parent()
    STORAGE_PROFILE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sanitize_filename(file_name: str) -> str:
    base_name = Path(file_name or "document").name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._")
    return safe or "document"


def _is_image_upload(file_name: str, media_type: str | None) -> bool:
    suffix = Path(file_name).suffix.lower()
    return suffix in IMAGE_EXTENSIONS or (media_type or "").startswith("image/")


def _default_profile_name(backend_type: str, base_path: str) -> str:
    suffix = Path(base_path).name or base_path
    return f"{backend_type}:{suffix}"


def _ensure_writable_path(base_path: str) -> Path:
    if not base_path:
        raise RuntimeError("Base path ist leer.")
    path = Path(base_path).expanduser()
    if not path.is_absolute():
        raise RuntimeError("Base path muss absolut sein, z. B. /srv/llm-storage oder /mnt/smb/ki.")
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".llm-gateway-write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)
    return path


def list_storage_profiles() -> list[dict[str, object]]:
    state = _load_profile_state()
    active_profile_id = state.get("active_profile_id")
    items: list[dict[str, object]] = []
    for item in state.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "storage"),
                "backend_type": str(item.get("backend_type") or "local"),
                "base_path": str(item.get("base_path") or ""),
                "is_active": item.get("id") == active_profile_id,
            }
        )
    return items


def get_active_storage_profile() -> dict[str, object] | None:
    for profile in list_storage_profiles():
        if profile.get("is_active"):
            return profile
    return None


def save_storage_profile(name: str, backend_type: str, base_path: str, make_active: bool = True) -> dict[str, object]:
    clean_type = (backend_type or "local").strip().lower()
    if clean_type not in ALLOWED_STORAGE_TYPES:
        raise RuntimeError("Unbekannter Storage-Typ. Erlaubt sind local oder smb_mount.")

    checked_path = _ensure_writable_path(base_path)
    clean_path = str(checked_path)
    clean_name = (name or "").strip() or _default_profile_name(clean_type, clean_path)

    state = _load_profile_state()
    profiles = state.get("profiles") or []
    existing: dict[str, object] | None = None

    for item in profiles:
        if not isinstance(item, dict):
            continue
        if item.get("name") == clean_name:
            existing = item
            break

    if existing is None:
        existing = {
            "id": uuid4().hex,
            "name": clean_name,
            "backend_type": clean_type,
            "base_path": clean_path,
        }
        profiles.append(existing)
    else:
        existing["name"] = clean_name
        existing["backend_type"] = clean_type
        existing["base_path"] = clean_path

    state["profiles"] = profiles
    if make_active:
        state["active_profile_id"] = existing["id"]
    _save_profile_state(state)
    return {
        "id": existing["id"],
        "name": clean_name,
        "backend_type": clean_type,
        "base_path": clean_path,
        "is_active": make_active,
    }


def activate_storage_profile(profile_id: str) -> dict[str, object]:
    state = _load_profile_state()
    for item in state.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") == profile_id:
            state["active_profile_id"] = profile_id
            _save_profile_state(state)
            return {
                "id": item["id"],
                "name": item["name"],
                "backend_type": item["backend_type"],
                "base_path": item["base_path"],
                "is_active": True,
            }
    raise RuntimeError("Storage-Profil wurde nicht gefunden.")


def delete_storage_profile(profile_id: str) -> dict[str, object]:
    state = _load_profile_state()
    profiles = state.get("profiles") or []
    kept: list[dict[str, object]] = []
    deleted = False
    deleted_was_active = state.get("active_profile_id") == profile_id

    for item in profiles:
        if not isinstance(item, dict):
            continue
        if item.get("id") == profile_id:
            deleted = True
            continue
        kept.append(item)

    if not deleted:
        raise RuntimeError("Storage-Profil wurde nicht gefunden.")

    state["profiles"] = kept
    if deleted_was_active:
        state["active_profile_id"] = None
    _save_profile_state(state)
    return {"deleted": True, "deleted_was_active": deleted_was_active}


async def _connect(settings: Settings):
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL ist leer. Fuer Dokumente wird PostgreSQL benoetigt.")
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError("asyncpg ist nicht installiert.") from exc

    conn = await asyncpg.connect(settings.database_url, timeout=5.0)
    await conn.execute(DOCUMENT_SCHEMA_SQL)
    return conn


def _extract_text(file_name: str, media_type: str | None, raw_bytes: bytes) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in TEXT_EXTENSIONS or (media_type or "").startswith("text/"):
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return raw_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise RuntimeError("Textdatei konnte nicht dekodiert werden.")

    if suffix == ".pdf" or media_type == "application/pdf":
        try:
            from pypdf import PdfReader
        except ModuleNotFoundError as exc:
            raise RuntimeError("pypdf ist nicht installiert. PDF-Import ist so nicht verfuegbar.") from exc

        reader = PdfReader(io.BytesIO(raw_bytes))
        pages: list[str] = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                pages.append(text.strip())
        return "\n\n".join(pages).strip()

    raise RuntimeError("Dateityp nicht unterstuetzt. Erlaubt sind aktuell Text, PDF und gaengige Bilddateien.")


async def _extract_image_analysis(settings: Settings, file_name: str, media_type: str | None, raw_bytes: bytes) -> tuple[str, str]:
    if not vision_is_configured(settings):
        return "", "Bild gespeichert. Vision-Modell ist noch nicht konfiguriert."
    try:
        summary = await analyze_image_bytes(
            settings,
            file_name=file_name,
            media_type=media_type,
            raw_bytes=raw_bytes,
        )
    except VisionConfigError as exc:
        return "", f"Bild gespeichert. Vision-Konfiguration fehlt: {exc}"
    except (LlamaCppError, LlamaCppTimeoutError) as exc:
        return "", f"Bild gespeichert. Vision-Analyse fehlgeschlagen: {exc}"

    summary = summary.strip()
    if not summary:
        return "", "Bild gespeichert. Vision-Modell hat keine Beschreibung geliefert."
    return summary, _build_excerpt(summary)


def _build_excerpt(text: str, limit: int = 400) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _jsonify_document_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _serialize_document_row(row) -> dict[str, object]:
    return {key: _jsonify_document_value(value) for key, value in dict(row).items()}


async def upload_document(
    settings: Settings,
    file: UploadFile,
    storage_profile_id: str | None,
    title: str | None = None,
    tags: str | None = None,
) -> dict[str, object]:
    profile = None
    if storage_profile_id:
        for item in list_storage_profiles():
            if item["id"] == storage_profile_id:
                profile = item
                break
    else:
        profile = get_active_storage_profile()

    if profile is None:
        raise RuntimeError("Kein aktives Storage-Profil vorhanden. Bitte zuerst einen Speicherpfad anlegen und aktivieren.")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise RuntimeError("Leere Datei kann nicht gespeichert werden.")
    if len(raw_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise RuntimeError("Datei ist zu gross. Aktuell sind maximal 25 MB pro Upload vorgesehen.")

    safe_name = _sanitize_filename(file.filename or "document")
    asset_kind = "image" if _is_image_upload(safe_name, file.content_type) else "document"
    created_at = _utcnow()
    subdir = Path(str(created_at.year)) / f"{created_at.month:02d}"
    base_path = _ensure_writable_path(str(profile["base_path"]))
    target_dir = base_path / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    relative_path = str(subdir / f"{uuid4().hex}_{safe_name}")
    target_path = base_path / relative_path
    try:
        target_path.write_bytes(raw_bytes)

        extracted_text = ""
        text_excerpt = ""
        analysis_status = ""
        if asset_kind == "image":
            extracted_text, text_excerpt = await _extract_image_analysis(settings, safe_name, file.content_type, raw_bytes)
            analysis_status = "analyzed" if extracted_text else "stored_without_analysis"
        else:
            extracted_text = _extract_text(safe_name, file.content_type, raw_bytes)
            if not extracted_text.strip():
                extracted_text = ""
            text_excerpt = _build_excerpt(extracted_text) if extracted_text else ""
            analysis_status = "extracted" if extracted_text else "stored_without_text"

        conn = await _connect(settings)
        try:
            row = await conn.fetchrow(
                """
                insert into document_assets (
                  storage_location_id,
                  storage_location_name,
                  title,
                  file_name,
                  media_type,
                  size_bytes,
                  relative_path,
                  extracted_text,
                  text_excerpt,
                  asset_kind,
                  tags
                )
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                returning id, storage_location_id, storage_location_name, title, file_name, media_type,
                          size_bytes, relative_path, extracted_text, text_excerpt, asset_kind, tags, created_at
                """,
                str(profile["id"]),
                str(profile["name"]),
                (title or "").strip() or safe_name,
                safe_name,
                file.content_type or "",
                len(raw_bytes),
                relative_path,
                extracted_text,
                text_excerpt,
                asset_kind,
                (tags or "").strip(),
            )
        finally:
            await conn.close()
    except Exception:
        target_path.unlink(missing_ok=True)
        raise

    document = _serialize_document_row(row)
    document["analysis_status"] = analysis_status
    return document


async def list_documents(settings: Settings, limit: int = 30) -> list[dict[str, object]]:
    conn = await _connect(settings)
    try:
        rows = await conn.fetch(
            """
            select id, storage_location_id, storage_location_name, title, file_name, media_type,
                   size_bytes, relative_path, text_excerpt, asset_kind, tags, created_at
            from document_assets
            order by created_at desc
            limit $1
            """,
            limit,
        )
        return [_serialize_document_row(row) for row in rows]
    finally:
        await conn.close()


async def get_document_contexts(settings: Settings, document_ids: list[str]) -> list[dict[str, object]]:
    clean_ids = [item for item in document_ids if item]
    if not clean_ids:
        return []
    conn = await _connect(settings)
    try:
        rows = await conn.fetch(
            """
            select id, title, file_name, media_type, asset_kind, extracted_text, text_excerpt
            from document_assets
            where id = any($1::uuid[])
            order by created_at asc
            """,
            clean_ids,
        )
        return [_serialize_document_row(row) for row in rows]
    finally:
        await conn.close()


async def storage_overview(settings: Settings) -> dict[str, object]:
    profiles = list_storage_profiles()
    active_profile = get_active_storage_profile()
    documents: list[dict[str, object]] = []
    documents_error = ""

    if settings.database_url:
        try:
            documents = await list_documents(settings, limit=20)
        except Exception as exc:
            documents_error = str(exc)

    return {
        "active_profile": active_profile,
        "profiles": profiles,
        "profiles_count": len(profiles),
        "documents": documents,
        "documents_count": len(documents),
        "documents_error": documents_error,
    }
