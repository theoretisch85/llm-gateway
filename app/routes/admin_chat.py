import json
import logging
from textwrap import dedent

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.api_errors import error_response
from app.auth import get_admin_session_username, require_admin_api_auth
from app.config import get_settings
from app.context_guard import ContextGuardError, fit_messages_to_budget
from app.schemas.chat import ChatMessage
from app.schemas.admin_chat import AdminChatRequest, AdminChatResponse, AdminSessionCreateRequest, AdminSessionResponse
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError
from app.services.model_router import ModelRouter
from app.services.session_memory import ChatSession, get_session_store


logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-chat"])


@router.get("/internal/chat", response_class=HTMLResponse, response_model=None)
async def admin_chat_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dchat", status_code=303)
    return HTMLResponse(_admin_chat_html())


@router.get("/api/admin/sessions", dependencies=[Depends(require_admin_api_auth)])
async def list_sessions() -> list[AdminSessionResponse]:
    settings = get_settings()
    store = get_session_store(settings)
    return [_serialize_session(item) for item in await store.list_sessions()]


@router.post("/api/admin/sessions", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def create_session(payload: AdminSessionCreateRequest) -> AdminSessionResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await store.create_session(title=payload.title, mode=payload.mode)
    return _serialize_session(session)


@router.get("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def get_session(session_id: str) -> AdminSessionResponse:
    settings = get_settings()
    session = await _require_session(get_session_store(settings), session_id)
    return _serialize_session(session)


@router.delete("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin_api_auth)])
async def delete_session(session_id: str) -> dict[str, bool]:
    settings = get_settings()
    store = get_session_store(settings)
    deleted = await store.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"deleted": True}


@router.post("/api/admin/sessions/{session_id}/reset", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def reset_session(session_id: str) -> AdminSessionResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await store.reset_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _serialize_session(session)


@router.post(
    "/api/admin/sessions/{session_id}/chat",
    dependencies=[Depends(require_admin_api_auth)],
    response_model=None,
)
async def admin_chat(payload: AdminChatRequest, session_id: str, request: Request) -> AdminChatResponse | JSONResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await _require_session(store, session_id)
    client = LlamaCppClient(settings)

    try:
        decision, backend_payload = _prepare_admin_backend_payload(settings, session, payload)
        await store.add_message(session_id, "user", payload.message)
        await store.update_route(session_id, decision.resolved_model, decision.reason, payload.mode or session.mode)
        request.state.backend_called = True
        response_payload = await client.create_chat_completion(backend_payload, base_url=decision.target_base_url)
        assistant_text = _extract_assistant_text(response_payload)
        assistant_message = await store.add_message(
            session_id,
            "assistant",
            assistant_text,
            model_used=decision.resolved_model,
        )
        return AdminChatResponse(
            session=_serialize_session(await _require_session(store, session_id)),
            assistant_message=_serialize_message(assistant_message),
            resolved_model=decision.resolved_model,
            route_reason=decision.reason,
        )
    except ContextGuardError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=exc.message,
            error_type="context_length_error",
            code=exc.code,
        )
    except LlamaCppTimeoutError:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            message="Upstream llama.cpp request timed out.",
            error_type="gateway_timeout",
            code="upstream_timeout",
        )
    except LlamaCppError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
        )


@router.post(
    "/api/admin/sessions/{session_id}/chat/stream",
    dependencies=[Depends(require_admin_api_auth)],
    response_model=None,
)
async def admin_chat_stream(payload: AdminChatRequest, session_id: str, request: Request) -> StreamingResponse | JSONResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await _require_session(store, session_id)
    client = LlamaCppClient(settings)

    try:
        decision, backend_payload = _prepare_admin_backend_payload(settings, session, payload)
        backend_payload["stream"] = True
    except ContextGuardError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=exc.message,
            error_type="context_length_error",
            code=exc.code,
        )

    await store.add_message(session_id, "user", payload.message)
    await store.update_route(session_id, decision.resolved_model, decision.reason, payload.mode or session.mode)
    request.state.backend_called = True

    async def event_stream():
        chunks: list[str] = []
        try:
            async for chunk in client.stream_chat_completion(
                backend_payload=backend_payload,
                public_model_name=decision.resolved_model,
                backend_model_name=settings.resolve_target_for_public_model(decision.resolved_model).backend_name,
                request_id=request.state.request_id,
                base_url=decision.target_base_url,
            ):
                text_part = _extract_content_from_sse(chunk)
                if text_part:
                    chunks.append(text_part)
                yield chunk
        except (LlamaCppError, LlamaCppTimeoutError) as exc:
            payload = {
                "error": {
                    "message": getattr(exc, "message", "Streaming request failed."),
                    "type": "upstream_error",
                    "code": getattr(exc, "code", "upstream_error"),
                    "request_id": request.state.request_id,
                }
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            final_text = "".join(chunks).strip()
            if final_text:
                await store.add_message(
                    session_id,
                    "assistant",
                    final_text,
                    model_used=decision.resolved_model,
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _prepare_admin_backend_payload(settings, session: ChatSession, payload: AdminChatRequest):
    model_router = ModelRouter(settings)
    decision = model_router.decide(payload.mode or session.mode, payload.message, len(session.messages))
    history_messages = [
        {"role": item.role, "content": item.content}
        for item in session.messages[-12:]
    ]
    prompt_messages: list[ChatMessage] = []
    prompt_messages.append(
        ChatMessage(
            role="system",
            content="You are the built-in admin assistant for this llm-gateway platform. Be precise and implementation-oriented.",
        )
    )
    if session.summary:
        prompt_messages.append(ChatMessage(role="system", content=session.summary))
    prompt_messages.extend(ChatMessage(role=item["role"], content=item["content"]) for item in history_messages)
    prompt_messages.append(ChatMessage(role="user", content=payload.message))

    guard_result = fit_messages_to_budget(
        messages=prompt_messages,
        max_context_tokens=settings.backend_context_window,
        response_reserve_tokens=payload.max_tokens or settings.context_response_reserve,
        chars_per_token=settings.context_chars_per_token,
    )
    target = settings.resolve_target_for_public_model(decision.resolved_model)
    backend_payload = {
        "model": target.backend_name,
        "messages": guard_result.messages,
        "stream": False,
        "temperature": payload.temperature,
        "max_tokens": payload.max_tokens or settings.default_max_tokens,
    }
    return decision, backend_payload


async def _require_session(store, session_id: str) -> ChatSession:
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


def _serialize_message(message):
    from app.schemas.admin_chat import AdminChatMessageResponse

    return AdminChatMessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        model_used=message.model_used,
        created_at=message.created_at,
    )


def _serialize_session(session: ChatSession) -> AdminSessionResponse:
    return AdminSessionResponse(
        id=session.id,
        title=session.title,
        mode=session.mode,
        resolved_model=session.resolved_model,
        route_reason=session.route_reason,
        summary=session.summary,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[_serialize_message(item) for item in session.messages],
    )


def _extract_assistant_text(response_payload: dict) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _extract_content_from_sse(chunk: bytes) -> str:
    if not chunk.startswith(b"data: "):
        return ""
    payload = chunk[6:].strip()
    if payload == b"[DONE]":
        return ""
    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _admin_chat_html() -> str:
    return dedent(
        """\
        <!doctype html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>llm-gateway chat</title>
          <style>
            :root {
              --bg:#0b0f14;
              --card:#11161d;
              --card-2:#161c24;
              --ink:#d8e4ef;
              --muted:#7f92a3;
              --line:#2b3744;
              --accent:#8be28b;
              --accent-2:#67c1ff;
              --warn:#ffcc6a;
            }
            * { box-sizing:border-box; }
            body {
              margin:0;
              font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
              background:
                linear-gradient(180deg,#10151c 0%, var(--bg) 100%);
              color:var(--ink);
            }
            main { padding:20px; display:grid; grid-template-columns:300px 1fr; gap:18px; min-height:100vh; }
            .panel {
              position:relative;
              background:linear-gradient(180deg, var(--card-2), var(--card));
              border:1px solid var(--line);
              border-radius:10px;
              padding:44px 18px 18px;
            }
            .panel::before {
              content:"";
              position:absolute;
              top:0;
              left:0;
              right:0;
              height:28px;
              border-bottom:1px solid var(--line);
              border-radius:10px 10px 0 0;
              background:
                radial-gradient(circle at 16px 14px, #ff5f56 0 4px, transparent 5px),
                radial-gradient(circle at 34px 14px, #ffbd2e 0 4px, transparent 5px),
                radial-gradient(circle at 52px 14px, #27c93f 0 4px, transparent 5px),
                linear-gradient(180deg, #1a222b, #161d26);
            }
            .muted { color:var(--muted); }
            button, select, textarea { font:inherit; }
            button {
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 14px;
              cursor:pointer;
              background:#1b2a1f;
              color:var(--accent);
              font-weight:700;
              text-transform:uppercase;
              letter-spacing:.05em;
            }
            button.secondary { background:#1a222b; color:var(--ink); }
            select, textarea {
              width:100%;
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 12px;
              background:#0a0f14;
              color:var(--ink);
            }
            select:focus, textarea:focus {
              outline:none;
              border-color:#4b8d4b;
              box-shadow:0 0 0 2px rgba(139,226,139,.10);
            }
            textarea { min-height:110px; resize:vertical; }
            .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
            .session-list { display:flex; flex-direction:column; gap:10px; margin-top:16px; }
            .session-item {
              padding:12px;
              border:1px solid var(--line);
              border-radius:8px;
              background:#0c1117;
              cursor:pointer;
              color:var(--ink);
              text-align:left;
            }
            .session-item.active {
              border-color:#4b8d4b;
              background:#151d17;
            }
            .messages { min-height:420px; max-height:62vh; overflow:auto; display:flex; flex-direction:column; gap:12px; margin:14px 0; }
            .message {
              border:1px solid var(--line);
              border-radius:8px;
              padding:12px;
              background:#0c1117;
            }
            .message.user { border-left:5px solid var(--accent); }
            .message.assistant { border-left:5px solid var(--warn); }
            .row { display:grid; grid-template-columns:1fr 180px; gap:12px; }
            .topbar { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }
            .status {
              margin-top:10px;
              padding:10px 12px;
              border-radius:8px;
              background:rgba(139,226,139,.10);
              border:1px solid #284131;
            }
            h1, h2 {
              letter-spacing:.10em;
              text-transform:uppercase;
            }
            pre { white-space:pre-wrap; word-break:break-word; margin:0; }
            @media (max-width: 980px) { main { grid-template-columns:1fr; } .row { grid-template-columns:1fr; } }
          </style>
        </head>
        <body>
          <main>
            <section class="panel">
              <h1>Admin Chat</h1>
              <p class="muted">Direkter Chat gegen Fast oder Deep Model mit Session-Verlauf, Streaming und Auto-Routing.</p>
              <div class="actions">
                <button type="button" onclick="createSession()">Neue Session</button>
                <button type="button" class="secondary" onclick="loadSessions()">Sessions laden</button>
              </div>
              <div id="sessionList" class="session-list"></div>
            </section>
            <section class="panel">
              <div class="topbar">
                <div>
                  <h2 id="sessionTitle">Keine Session</h2>
                  <div class="muted" id="sessionMeta">Lege links eine Session an oder lade eine bestehende.</div>
                </div>
                <div style="min-width:220px;">
                  <label>
                    <span>Modus</span>
                    <select id="mode">
                      <option value="auto">Auto-Routing</option>
                      <option value="fast">Fast Model</option>
                      <option value="deep">Deep Model</option>
                    </select>
                  </label>
                </div>
              </div>
              <div id="messages" class="messages"></div>
              <div class="row">
                <label>
                  <span>Nachricht</span>
                  <textarea id="prompt" placeholder="Schreibe hier direkt an die AI-Plattform..."></textarea>
                </label>
                <div>
                  <label>
                    <span>Streaming</span>
                    <select id="streaming">
                      <option value="false">Nein</option>
                      <option value="true">Ja</option>
                    </select>
                  </label>
                  <div class="actions">
                    <button type="button" onclick="sendMessage()">Senden</button>
                    <button type="button" class="secondary" onclick="resetSession()">Reset</button>
                    <button type="button" class="secondary" onclick="deleteSession()">Loeschen</button>
                  </div>
                </div>
              </div>
              <div id="status" class="status">Session anlegen und loschatten.</div>
            </section>
          </main>
          <script>
            const sessionList = document.getElementById("sessionList");
            const messagesNode = document.getElementById("messages");
            const sessionTitle = document.getElementById("sessionTitle");
            const sessionMeta = document.getElementById("sessionMeta");
            const promptInput = document.getElementById("prompt");
            const modeInput = document.getElementById("mode");
            const statusNode = document.getElementById("status");
            const streamingInput = document.getElementById("streaming");
            let currentSessionId = null;

            function setStatus(text, error = false) {
              statusNode.textContent = text;
              statusNode.style.background = error ? "#f9dddd" : "#e4f3e8";
              statusNode.style.color = error ? "#942f2f" : "#16231b";
            }

            function headers() {
              return { "Content-Type": "application/json" };
            }

            function renderMessages(items) {
              messagesNode.innerHTML = "";
              for (const item of items) {
                const node = document.createElement("div");
                node.className = `message ${item.role}`;
                node.innerHTML = `<strong>${item.role}</strong><pre>${item.content}</pre>`;
                messagesNode.appendChild(node);
              }
              messagesNode.scrollTop = messagesNode.scrollHeight;
            }

            function renderSession(session) {
              currentSessionId = session.id;
              sessionTitle.textContent = session.title;
              modeInput.value = session.mode || "auto";
              sessionMeta.textContent = `Modus: ${session.mode} | Modell: ${session.resolved_model || "-"} | Regel: ${session.route_reason || "-"}`;
              renderMessages(session.messages || []);
            }

            async function loadSessions() {
              try {
                const res = await fetch("/api/admin/sessions", { headers: headers() });
                if (!res.ok) throw new Error(`Sessions fehlgeschlagen: ${res.status}`);
                const sessions = await res.json();
                sessionList.innerHTML = "";
                for (const session of sessions) {
                  const button = document.createElement("button");
                  button.type = "button";
                  button.className = `session-item ${session.id === currentSessionId ? "active" : ""}`;
                  button.innerHTML = `<strong>${session.title}</strong><div class="muted">${session.mode} | ${session.resolved_model || "noch kein Modell"}</div>`;
                  button.onclick = () => openSession(session.id);
                  sessionList.appendChild(button);
                }
                if (!currentSessionId && sessions.length) {
                  renderSession(sessions[0]);
                }
                setStatus("Sessions geladen.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function createSession() {
              try {
                const res = await fetch("/api/admin/sessions", {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({ mode: modeInput.value }),
                });
                if (!res.ok) throw new Error(`Session anlegen fehlgeschlagen: ${res.status}`);
                const session = await res.json();
                renderSession(session);
                await loadSessions();
                setStatus("Neue Session angelegt.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function openSession(sessionId) {
              try {
                const res = await fetch(`/api/admin/sessions/${sessionId}`, { headers: headers() });
                if (!res.ok) throw new Error(`Session laden fehlgeschlagen: ${res.status}`);
                const session = await res.json();
                renderSession(session);
                await loadSessions();
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function resetSession() {
              if (!currentSessionId) return;
              try {
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/reset`, {
                  method: "POST",
                  headers: headers(),
                });
                if (!res.ok) throw new Error(`Reset fehlgeschlagen: ${res.status}`);
                const session = await res.json();
                renderSession(session);
                await loadSessions();
                setStatus("Session zurueckgesetzt.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function deleteSession() {
              if (!currentSessionId) return;
              try {
                const res = await fetch(`/api/admin/sessions/${currentSessionId}`, {
                  method: "DELETE",
                  headers: headers(),
                });
                if (!res.ok) throw new Error(`Loeschen fehlgeschlagen: ${res.status}`);
                currentSessionId = null;
                sessionTitle.textContent = "Keine Session";
                sessionMeta.textContent = "Lege links eine Session an oder lade eine bestehende.";
                renderMessages([]);
                await loadSessions();
                setStatus("Session geloescht.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function sendMessage() {
              if (!currentSessionId) {
                await createSession();
                if (!currentSessionId) return;
              }
              const message = promptInput.value.trim();
              if (!message) return;
              const mode = modeInput.value;
              promptInput.value = "";

              if (streamingInput.value === "true") {
                await streamMessage(message, mode);
                return;
              }

              try {
                setStatus("Request laeuft...");
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/chat`, {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({ message, mode }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data?.error?.message || `Chat fehlgeschlagen: ${res.status}`);
                renderSession(data.session);
                await loadSessions();
                setStatus(`Antwort erhalten via ${data.resolved_model} (${data.route_reason}).`);
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function streamMessage(message, mode) {
              const userNode = document.createElement("div");
              userNode.className = "message user";
              userNode.innerHTML = `<strong>user</strong><pre>${message}</pre>`;
              messagesNode.appendChild(userNode);

              const assistantNode = document.createElement("div");
              assistantNode.className = "message assistant";
              const assistantPre = document.createElement("pre");
              assistantNode.innerHTML = "<strong>assistant</strong>";
              assistantNode.appendChild(assistantPre);
              messagesNode.appendChild(assistantNode);
              messagesNode.scrollTop = messagesNode.scrollHeight;

              try {
                setStatus("Streaming laeuft...");
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/chat/stream`, {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({ message, mode }),
                });
                if (!res.ok) {
                  const data = await res.json();
                  throw new Error(data?.error?.message || `Streaming fehlgeschlagen: ${res.status}`);
                }

                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";

                while (true) {
                  const { value, done } = await reader.read();
                  if (done) break;
                  buffer += decoder.decode(value, { stream: true });
                  const parts = buffer.split("\\n\\n");
                  buffer = parts.pop() || "";
                  for (const part of parts) {
                    if (!part.startsWith("data: ")) continue;
                    const payload = part.slice(6);
                    if (payload === "[DONE]") continue;
                    const data = JSON.parse(payload);
                    const delta = data.choices?.[0]?.delta?.content || "";
                    if (delta) assistantPre.textContent += delta;
                  }
                }

                await openSession(currentSessionId);
                await loadSessions();
                setStatus("Streaming abgeschlossen.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            loadSessions();
          </script>
        </body>
        </html>
        """
    )
