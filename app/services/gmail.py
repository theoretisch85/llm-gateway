from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.llamacpp_client import LlamaCppClient


logger = logging.getLogger(__name__)

GMAIL_OAUTH_STATE_FILE = Path("/opt/llm-gateway/.runtime/gmail_oauth_state.json")
DEFAULT_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_GMAIL_HEADERS = [
    "Subject",
    "From",
    "To",
    "Date",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "List-Id",
    "Precedence",
]


class GmailConfigError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503, code: str = "gmail_config_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GmailRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "gmail_request_failed",
        upstream_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.upstream_status_code = upstream_status_code


class GmailClient:
    upstream_name = "gmail"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get_status(self, *, fallback_redirect_uri: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_status_sync, fallback_redirect_uri)

    async def start_oauth(self, *, fallback_redirect_uri: str, next_url: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._start_oauth_sync, fallback_redirect_uri, next_url)

    async def finish_oauth(self, *, authorization_response: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._finish_oauth_sync, authorization_response)

    async def disconnect(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._disconnect_sync)

    async def list_recent(
        self,
        *,
        limit: int = 20,
        query: str | None = None,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_recent_sync, limit, query, label_ids or [], include_spam_trash)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_message_sync, message_id)

    def _get_status_sync(self, fallback_redirect_uri: str | None = None) -> dict[str, Any]:
        client_secret_path = Path(self.settings.gmail_client_secret_file).expanduser()
        token_path = Path(self.settings.gmail_token_file).expanduser()
        redirect_uri = self._resolve_redirect_uri(fallback_redirect_uri, require=False)

        status: dict[str, Any] = {
            "enabled": bool(self.settings.gmail_enabled),
            "configured": client_secret_path.exists(),
            "connected": False,
            "client_secret_file": str(client_secret_path),
            "token_file": str(token_path),
            "redirect_uri": redirect_uri or "",
            "scopes": list(DEFAULT_GMAIL_SCOPES),
            "email_address": "",
            "message": "",
        }

        if not self.settings.gmail_enabled:
            status["message"] = "Gmail ist deaktiviert. Setze GMAIL_ENABLED=true."
            return status

        if not client_secret_path.exists():
            status["message"] = f"GMAIL_CLIENT_SECRET_FILE fehlt: {client_secret_path}"
            return status

        if not token_path.exists():
            status["message"] = "Gmail ist noch nicht verbunden. Starte den OAuth-Flow."
            return status

        try:
            service = self._build_service_sync()
            profile = service.users().getProfile(userId="me").execute()
        except (GmailConfigError, GmailRequestError) as exc:
            status["message"] = str(exc)
            return status

        status["connected"] = True
        status["email_address"] = str(profile.get("emailAddress") or "")
        status["message"] = f"Verbunden als {status['email_address'] or 'Gmail-Nutzer'}."
        return status

    def _start_oauth_sync(self, fallback_redirect_uri: str, next_url: str | None = None) -> dict[str, Any]:
        Flow, _Credentials, _Request, _build, _HttpError = _load_google_dependencies()
        redirect_uri = self._resolve_redirect_uri(fallback_redirect_uri, require=True)
        client_secret_path = self._require_client_secret_file()

        flow = Flow.from_client_secrets_file(
            str(client_secret_path),
            scopes=DEFAULT_GMAIL_SCOPES,
            redirect_uri=redirect_uri,
        )
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        _write_json_file(
            GMAIL_OAUTH_STATE_FILE,
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "next_url": str(next_url or "/internal/admin?tab=settings"),
                "created_at": int(time.time()),
            },
        )
        return {
            "authorization_url": authorization_url,
            "redirect_uri": redirect_uri,
            "next_url": str(next_url or "/internal/admin?tab=settings"),
            "scopes": list(DEFAULT_GMAIL_SCOPES),
        }

    def _finish_oauth_sync(self, authorization_response: str) -> dict[str, Any]:
        Flow, _Credentials, _Request, _build, _HttpError = _load_google_dependencies()
        state_payload = _read_json_file(GMAIL_OAUTH_STATE_FILE)
        if not state_payload:
            raise GmailConfigError(
                "Kein laufender Gmail-OAuth-Start gefunden. Bitte den OAuth-Flow neu starten.",
                status_code=400,
                code="gmail_oauth_state_missing",
            )

        redirect_uri = str(state_payload.get("redirect_uri") or "").strip()
        state = str(state_payload.get("state") or "").strip()
        if not redirect_uri or not state:
            raise GmailConfigError(
                "Gmail-OAuth-State ist unvollstaendig. Bitte den OAuth-Flow neu starten.",
                status_code=400,
                code="gmail_oauth_state_invalid",
            )

        next_url = str(state_payload.get("next_url") or "/internal/admin?tab=settings")
        flow = Flow.from_client_secrets_file(
            str(self._require_client_secret_file()),
            scopes=DEFAULT_GMAIL_SCOPES,
            state=state,
            redirect_uri=redirect_uri,
        )
        try:
            flow.fetch_token(authorization_response=authorization_response)
        except Exception as exc:
            raise GmailRequestError(
                f"Gmail-OAuth konnte nicht abgeschlossen werden: {exc}",
                status_code=400,
                code="gmail_oauth_exchange_failed",
            ) from exc

        credentials = flow.credentials
        token_path = Path(self.settings.gmail_token_file).expanduser()
        _write_text_file(token_path, credentials.to_json())
        _remove_file(GMAIL_OAUTH_STATE_FILE)

        service = self._build_service_from_credentials(credentials)
        profile = service.users().getProfile(userId="me").execute()
        return {
            "connected": True,
            "email_address": str(profile.get("emailAddress") or ""),
            "message": "Gmail erfolgreich verbunden.",
            "next_url": next_url,
        }

    def _disconnect_sync(self) -> dict[str, Any]:
        token_path = Path(self.settings.gmail_token_file).expanduser()
        state_path = GMAIL_OAUTH_STATE_FILE
        removed = False
        for path in (token_path, state_path):
            if path.exists():
                path.unlink()
                removed = True
        return {
            "disconnected": True,
            "removed": removed,
            "message": "Gespeicherte Gmail-Anmeldung wurde entfernt.",
        }

    def _list_recent_sync(
        self,
        limit: int,
        query: str | None,
        label_ids: list[str],
        include_spam_trash: bool,
    ) -> dict[str, Any]:
        service = self._build_service_sync()
        limit = max(1, min(int(limit or 20), 50))
        clean_query = (query or "").strip()
        clean_labels = [str(item).strip() for item in label_ids if str(item).strip()]

        request = service.users().messages().list(
            userId="me",
            maxResults=limit,
            q=clean_query or None,
            labelIds=clean_labels or None,
            includeSpamTrash=bool(include_spam_trash),
        )
        response = self._execute_google_request(request)
        rows: list[dict[str, Any]] = []
        for item in response.get("messages") or []:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            metadata = self._execute_google_request(
                service.users().messages().get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=DEFAULT_GMAIL_HEADERS,
                )
            )
            rows.append(_normalize_gmail_message(metadata, include_body=False))

        return {
            "success": True,
            "upstream": self.upstream_name,
            "messages": rows,
            "query": clean_query,
            "limit": limit,
            "include_spam_trash": bool(include_spam_trash),
            "result_size_estimate": int(response.get("resultSizeEstimate") or len(rows)),
        }

    def _get_message_sync(self, message_id: str) -> dict[str, Any]:
        clean_id = str(message_id or "").strip()
        if not clean_id:
            raise ValueError("message_id ist erforderlich.")
        service = self._build_service_sync()
        payload = self._execute_google_request(
            service.users().messages().get(
                userId="me",
                id=clean_id,
                format="full",
            )
        )
        return {
            "success": True,
            "upstream": self.upstream_name,
            "message": _normalize_gmail_message(payload, include_body=True),
        }

    def _build_service_sync(self):
        credentials = self._load_credentials_sync()
        return self._build_service_from_credentials(credentials)

    def _build_service_from_credentials(self, credentials):
        _Flow, _Credentials, _Request, build, _HttpError = _load_google_dependencies()
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def _load_credentials_sync(self):
        _Flow, Credentials, Request, _build, _HttpError = _load_google_dependencies()
        self._require_enabled()
        token_path = Path(self.settings.gmail_token_file).expanduser()
        if not token_path.exists():
            raise GmailConfigError(
                "Gmail ist noch nicht verbunden. Bitte zuerst den OAuth-Flow starten.",
                status_code=401,
                code="gmail_auth_required",
            )

        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), DEFAULT_GMAIL_SCOPES)
        except Exception as exc:
            raise GmailConfigError(
                f"GMAIL_TOKEN_FILE ist ungueltig: {token_path}",
                status_code=400,
                code="gmail_token_invalid",
            ) from exc

        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                    _write_text_file(token_path, credentials.to_json())
                except Exception as exc:
                    raise GmailRequestError(
                        f"Gmail-Token konnte nicht aktualisiert werden: {exc}",
                        status_code=401,
                        code="gmail_token_refresh_failed",
                    ) from exc
            else:
                raise GmailConfigError(
                    "Gmail-Anmeldung ist ungueltig oder abgelaufen. Bitte neu verbinden.",
                    status_code=401,
                    code="gmail_token_missing_refresh",
                )
        return credentials

    def _require_enabled(self) -> None:
        if not self.settings.gmail_enabled:
            raise GmailConfigError(
                "Gmail ist im Gateway deaktiviert. Setze GMAIL_ENABLED=true.",
                status_code=503,
                code="gmail_disabled",
            )
        self._require_client_secret_file()

    def _require_client_secret_file(self) -> Path:
        client_secret_path = Path(self.settings.gmail_client_secret_file).expanduser()
        if not client_secret_path.exists():
            raise GmailConfigError(
                f"GMAIL_CLIENT_SECRET_FILE fehlt: {client_secret_path}",
                status_code=503,
                code="gmail_client_secret_missing",
            )
        return client_secret_path

    def _resolve_redirect_uri(self, fallback_redirect_uri: str | None, *, require: bool) -> str:
        redirect_uri = (self.settings.gmail_oauth_redirect_uri or fallback_redirect_uri or "").strip()
        if require and not redirect_uri:
            raise GmailConfigError(
                "GMAIL_OAUTH_REDIRECT_URI ist nicht gesetzt.",
                status_code=400,
                code="gmail_redirect_uri_missing",
            )
        return redirect_uri

    def _execute_google_request(self, request):
        _Flow, _Credentials, _Request, _build, HttpError = _load_google_dependencies()
        try:
            return request.execute()
        except HttpError as exc:
            status_code = int(getattr(getattr(exc, "resp", None), "status", 502) or 502)
            raise GmailRequestError(
                f"Gmail API Fehler: {exc}",
                status_code=status_code,
                code=f"gmail_upstream_{status_code}",
                upstream_status_code=status_code,
            ) from exc
        except Exception as exc:
            raise GmailRequestError(
                f"Gmail API Anfrage fehlgeschlagen: {exc}",
                status_code=502,
                code="gmail_upstream_unreachable",
            ) from exc


async def classify_gmail_messages(settings: Settings, messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not messages:
        return {"messages": [], "model": settings.effective_fast_model.public_name}

    target = settings.effective_fast_model
    client = LlamaCppClient(settings)
    payload_messages = [
        {
            "id": item.get("id"),
            "from": item.get("from"),
            "subject": item.get("subject"),
            "snippet": item.get("snippet"),
            "labels": item.get("label_ids"),
            "unsubscribe_available": item.get("unsubscribe_available"),
            "list_id": item.get("list_id"),
        }
        for item in messages[:20]
    ]
    response = await client.create_chat_completion(
        {
            "model": target.backend_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Du klassifizierst Gmail-Nachrichten fuer einen privaten Nutzer. "
                        "Antworte ausschliesslich mit JSON im Format "
                        '{"messages":[{"id":"string","category":"important|personal|finance|newsletter|promotion|social|spam_like|unknown",'
                        '"importance":"high|medium|low","summary":"string","recommended_action":"keep|review|archive_later|trash_candidate",'
                        '"unsubscribe_possible":true}]}. '
                        "Kein Markdown. Keine Erklaerungen ausserhalb des JSON. "
                        "Stufe nur dann als newsletter/promotion/spam_like ein, wenn die Hinweise in den Metadaten klar dafuer sprechen."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"messages": payload_messages}, ensure_ascii=False),
                },
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": min(1600, settings.default_max_tokens + 600),
        },
        base_url=target.base_url,
    )
    content = _extract_chat_content(response)
    parsed = _parse_json_object(content)
    rows = parsed.get("messages") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise ValueError("LLM-Klassifizierung lieferte kein gueltiges Nachrichten-Array.")

    by_id: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "").strip()
        if not key:
            continue
        by_id[key] = {
            "category": str(item.get("category") or "unknown"),
            "importance": str(item.get("importance") or "low"),
            "summary": str(item.get("summary") or "").strip(),
            "recommended_action": str(item.get("recommended_action") or "review"),
            "unsubscribe_possible": bool(item.get("unsubscribe_possible")),
        }

    classified_messages: list[dict[str, Any]] = []
    for message in messages:
        classification = by_id.get(str(message.get("id") or "").strip(), None)
        classified_messages.append(
            {
                **message,
                "classification": classification
                or {
                    "category": "unknown",
                    "importance": "low",
                    "summary": "",
                    "recommended_action": "review",
                    "unsubscribe_possible": bool(message.get("unsubscribe_available")),
                },
            }
        )

    return {
        "messages": classified_messages,
        "model": target.public_name,
    }


def _load_google_dependencies():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise GmailConfigError(
            "Gmail-Python-Abhaengigkeiten fehlen. Installiere google-api-python-client, google-auth-httplib2 und google-auth-oauthlib.",
            status_code=503,
            code="gmail_dependencies_missing",
        ) from exc
    return Flow, Credentials, Request, build, HttpError


def _normalize_gmail_message(payload: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    headers = _header_map(payload)
    normalized: dict[str, Any] = {
        "id": str(payload.get("id") or ""),
        "thread_id": str(payload.get("threadId") or ""),
        "label_ids": payload.get("labelIds") if isinstance(payload.get("labelIds"), list) else [],
        "snippet": str(payload.get("snippet") or "").strip(),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "received_at": _internal_date_to_iso(payload.get("internalDate")),
        "list_id": headers.get("list-id", ""),
        "list_unsubscribe": headers.get("list-unsubscribe", ""),
        "list_unsubscribe_post": headers.get("list-unsubscribe-post", ""),
        "precedence": headers.get("precedence", ""),
        "unsubscribe_available": bool(headers.get("list-unsubscribe")),
    }
    if include_body:
        normalized["body_text"] = _extract_body_text(payload.get("payload"))
    return normalized


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    message_payload = payload.get("payload")
    if not isinstance(message_payload, dict):
        return result
    raw_headers = message_payload.get("headers")
    if not isinstance(raw_headers, list):
        return result
    for item in raw_headers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name and value:
            result[name] = value
    return result


def _internal_date_to_iso(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        ts = int(text) / 1000.0
    except ValueError:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _extract_body_text(payload: Any) -> str:
    text = _extract_mime_text(payload, preferred_mime="text/plain")
    if text:
        return text
    html_text = _extract_mime_text(payload, preferred_mime="text/html")
    if html_text:
        return _strip_html(html_text)
    return ""


def _extract_mime_text(payload: Any, *, preferred_mime: str) -> str:
    if not isinstance(payload, dict):
        return ""

    mime_type = str(payload.get("mimeType") or "").strip().lower()
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    data = body.get("data")
    if mime_type == preferred_mime and data:
        return _decode_gmail_body(str(data))

    parts = payload.get("parts")
    if isinstance(parts, list):
        for item in parts:
            text = _extract_mime_text(item, preferred_mime=preferred_mime)
            if text:
                return text
    return ""


def _decode_gmail_body(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return json.dumps(content or {}, ensure_ascii=False)


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Kein JSON-Objekt gefunden.")
        parsed = json.loads(raw_text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON-Antwort ist kein Objekt.")
    return parsed


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _remove_file(path: Path) -> None:
    if path.exists():
        path.unlink()
