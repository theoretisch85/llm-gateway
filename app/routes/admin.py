import logging
from textwrap import dedent

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.auth import require_bearer_token
from app.config import get_settings
from app.services.backend_control import restart_mi50_backend
from app.services.config_store import read_runtime_config, write_runtime_config


router = APIRouter(tags=["admin"])
logger = logging.getLogger("llm_gateway")


class AdminConfigUpdate(BaseModel):
    LLAMACPP_BASE_URL: str
    LLAMACPP_TIMEOUT_SECONDS: str
    PUBLIC_MODEL_NAME: str
    BACKEND_MODEL_NAME: str
    BACKEND_CONTEXT_WINDOW: str
    CONTEXT_RESPONSE_RESERVE: str
    CONTEXT_CHARS_PER_TOKEN: str
    DEFAULT_MAX_TOKENS: str
    MI50_SSH_HOST: str
    MI50_SSH_USER: str
    MI50_SSH_PORT: str
    MI50_RESTART_COMMAND: str
    MI50_STATUS_COMMAND: str


@router.get("/internal/admin", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    return HTMLResponse(_admin_html())


@router.get("/internal/admin/config", dependencies=[Depends(require_bearer_token)])
async def get_admin_config() -> dict[str, str]:
    settings = get_settings()
    current = read_runtime_config()
    current.setdefault("LLAMACPP_BASE_URL", settings.llamacpp_base_url)
    current.setdefault("LLAMACPP_TIMEOUT_SECONDS", str(settings.llamacpp_timeout_seconds))
    current.setdefault("PUBLIC_MODEL_NAME", settings.public_model_name)
    current.setdefault("BACKEND_MODEL_NAME", settings.backend_model_name)
    current.setdefault("BACKEND_CONTEXT_WINDOW", str(settings.backend_context_window))
    current.setdefault("CONTEXT_RESPONSE_RESERVE", str(settings.context_response_reserve))
    current.setdefault("CONTEXT_CHARS_PER_TOKEN", str(settings.context_chars_per_token))
    current.setdefault("DEFAULT_MAX_TOKENS", str(settings.default_max_tokens))
    current.setdefault("MI50_SSH_HOST", "")
    current.setdefault("MI50_SSH_USER", "")
    current.setdefault("MI50_SSH_PORT", "22")
    current.setdefault("MI50_RESTART_COMMAND", "sudo systemctl restart llama.cpp")
    current.setdefault("MI50_STATUS_COMMAND", "sudo systemctl status llama.cpp --no-pager")
    return current


@router.post("/internal/admin/config", dependencies=[Depends(require_bearer_token)])
async def update_admin_config(payload: AdminConfigUpdate) -> dict[str, str]:
    return write_runtime_config(payload.model_dump())


@router.get("/internal/admin/continue-config", dependencies=[Depends(require_bearer_token)])
async def get_continue_config(request: Request) -> JSONResponse:
    settings = get_settings()
    host_base = str(request.base_url).rstrip("/")
    rendered = dedent(
        f"""
        name: llm-gateway
        version: 0.0.1
        schema: v1

        models:
          - name: qwen2.5-coder-local
            provider: openai
            model: {settings.public_model_name}
            apiBase: {host_base}/v1
            apiKey: CHANGE_ME
        """
    ).strip()
    return JSONResponse({"yaml": rendered})


@router.post("/internal/admin/restart-backend", dependencies=[Depends(require_bearer_token)])
async def restart_backend(request: Request) -> JSONResponse:
    request.state.backend_called = True
    logger.info("admin requested mi50 backend restart")
    try:
        return JSONResponse(restart_mi50_backend())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _admin_html() -> str:
    return dedent(
        """\
        <!doctype html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>llm-gateway admin</title>
          <style>
            :root {
              --bg: #f4efe6;
              --card: rgba(255, 252, 244, 0.92);
              --ink: #17231b;
              --muted: #5b675f;
              --line: #d8cfbf;
              --accent: #165f44;
              --accent-soft: #dff5e7;
              --ok: #1a7f4b;
              --ok-soft: #dff6e6;
              --warn: #a46117;
              --warn-soft: #fff0d7;
              --err: #942f2f;
              --err-soft: #f9dddd;
            }
            * { box-sizing: border-box; }
            body {
              margin: 0;
              font-family: Georgia, "Times New Roman", serif;
              color: var(--ink);
              background:
                radial-gradient(circle at top left, rgba(22,95,68,.14), transparent 28%),
                radial-gradient(circle at bottom right, rgba(164,97,23,.10), transparent 24%),
                linear-gradient(180deg, #faf7f1 0%, var(--bg) 100%);
            }
            main { max-width: 1180px; margin: 0 auto; padding: 28px 20px 64px; }
            .hero {
              display: grid;
              grid-template-columns: 1.5fr 1fr;
              gap: 18px;
              margin-bottom: 24px;
            }
            .hero-panel {
              background: linear-gradient(145deg, rgba(255,255,255,0.9), rgba(244,239,230,0.95));
              border: 1px solid var(--line);
              border-radius: 24px;
              padding: 22px;
              box-shadow: 0 16px 40px rgba(23, 35, 27, 0.08);
            }
            h1 { margin: 0 0 8px; font-size: 2.4rem; }
            p { margin: 0; color: var(--muted); line-height: 1.45; }
            .hero-badges {
              display: flex;
              gap: 10px;
              flex-wrap: wrap;
              margin-top: 16px;
            }
            .badge {
              border-radius: 999px;
              padding: 8px 12px;
              background: rgba(22,95,68,0.08);
              color: var(--ink);
              border: 1px solid rgba(22,95,68,0.12);
              font-size: .92rem;
            }
            .grid {
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
              gap: 20px;
              margin-top: 24px;
            }
            .card {
              background: var(--card);
              border: 1px solid var(--line);
              border-radius: 18px;
              padding: 18px;
              box-shadow: 0 12px 30px rgba(20, 34, 22, 0.06);
              backdrop-filter: blur(10px);
            }
            .wide { grid-column: 1 / -1; }
            .status-grid {
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 14px;
            }
            .stat {
              border-radius: 18px;
              padding: 16px;
              border: 1px solid var(--line);
              background: rgba(255,255,255,0.72);
            }
            .stat h3 {
              margin: 0 0 8px;
              font-size: .9rem;
              text-transform: uppercase;
              letter-spacing: .06em;
              color: var(--muted);
            }
            .stat strong {
              display: block;
              font-size: 1.5rem;
            }
            .health-card {
              display: flex;
              align-items: center;
              gap: 14px;
              min-height: 88px;
            }
            .lamp {
              width: 18px;
              height: 18px;
              border-radius: 50%;
              background: #bbb;
              box-shadow: 0 0 0 6px rgba(0,0,0,0.04);
              flex: 0 0 auto;
            }
            .lamp.neutral { background: #b7b7b7; box-shadow: 0 0 0 6px rgba(120,120,120,0.10); }
            .lamp.ok { background: var(--ok); box-shadow: 0 0 0 6px rgba(26,127,75,0.13); }
            .lamp.warn { background: var(--warn); box-shadow: 0 0 0 6px rgba(164,97,23,0.12); }
            .lamp.err { background: var(--err); box-shadow: 0 0 0 6px rgba(148,47,47,0.13); }
            .health-copy strong { font-size: 1.1rem; display: block; }
            .health-copy small { color: var(--muted); line-height: 1.4; display: block; margin-top: 4px; }
            .state-tag {
              display: inline-flex;
              align-items: center;
              gap: 6px;
              margin-top: 8px;
              padding: 5px 10px;
              border-radius: 999px;
              font-size: .82rem;
              font-weight: 700;
              letter-spacing: .02em;
              background: rgba(120,120,120,0.10);
              color: var(--muted);
            }
            .state-tag.ok { background: var(--ok-soft); color: var(--ok); }
            .state-tag.warn { background: var(--warn-soft); color: var(--warn); }
            .state-tag.err { background: var(--err-soft); color: var(--err); }
            .state-tag.neutral { background: rgba(120,120,120,0.10); color: var(--muted); }
            .legend {
              display: flex;
              gap: 10px;
              flex-wrap: wrap;
              margin-top: 12px;
              color: var(--muted);
              font-size: .88rem;
            }
            .legend span {
              display: inline-flex;
              align-items: center;
              gap: 6px;
              margin: 0;
              font-weight: 500;
            }
            .dot {
              width: 10px;
              height: 10px;
              border-radius: 50%;
              display: inline-block;
            }
            .dot.neutral { background: #b7b7b7; }
            .dot.ok { background: var(--ok); }
            .dot.warn { background: var(--warn); }
            .dot.err { background: var(--err); }
            label { display: block; font-size: .92rem; margin-bottom: 12px; }
            span { display: block; margin-bottom: 6px; font-weight: 600; }
            input, textarea {
              width: 100%;
              border: 1px solid var(--line);
              border-radius: 10px;
              padding: 10px 12px;
              font: inherit;
              background: white;
            }
            textarea { min-height: 220px; resize: vertical; }
            .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
            button {
              border: 0;
              border-radius: 999px;
              padding: 10px 16px;
              font: inherit;
              font-weight: 700;
              cursor: pointer;
              background: var(--accent);
              color: white;
            }
            button.secondary { background: #d8e2d8; color: var(--ink); }
            button.ghost { background: #efe7d6; color: var(--ink); }
            .status {
              margin-top: 12px;
              padding: 10px 12px;
              border-radius: 10px;
              background: var(--accent-soft);
              color: var(--ink);
              min-height: 44px;
            }
            .status.error { background: var(--err-soft); color: var(--err); }
            .status.warn { background: var(--warn-soft); color: var(--warn); }
            .muted { color: var(--muted); }
            code.inline {
              display: inline-block;
              padding: 3px 8px;
              border-radius: 999px;
              background: rgba(23,35,27,0.06);
              font-size: .92rem;
            }
            @media (max-width: 900px) {
              .hero { grid-template-columns: 1fr; }
            }
          </style>
        </head>
        <body>
          <main>
            <section class="hero">
              <div class="hero-panel">
                <h1>llm-gateway Admin</h1>
                <p>Interne Betriebsseite fuer Gateway, MI50-Backend, Kontextfenster und Continue-Konfiguration. Alles Wichtige auf einen Blick, ohne blind in YAML und Logs zu springen.</p>
                <div class="hero-badges">
                  <span class="badge">Gateway + Health</span>
                  <span class="badge">MI50 / llama.cpp Status</span>
                  <span class="badge">Kontext / Max Tokens</span>
                  <span class="badge">Continue YAML</span>
                </div>
                <div class="legend">
                  <span><i class="dot neutral"></i>Noch nicht geladen</span>
                  <span><i class="dot ok"></i>Läuft</span>
                  <span><i class="dot warn"></i>Lädt / Problem</span>
                  <span><i class="dot err"></i>Fehler / offline</span>
                </div>
              </div>
              <div class="hero-panel">
                <label>
                  <span>Bearer Token</span>
                  <input id="token" type="password" placeholder="API_BEARER_TOKEN">
                </label>
                <div class="actions">
                  <button type="button" onclick="refreshAll()">Alles aktualisieren</button>
                  <button type="button" class="ghost" onclick="loadContinueConfig()">Continue YAML</button>
                </div>
                <div id="status" class="status">Token eingeben und dann alles laden.</div>
              </div>
            </section>

            <section class="grid">
              <div class="card wide">
                <div class="status-grid">
                  <div class="stat health-card">
                    <div id="gatewayLamp" class="lamp neutral"></div>
                    <div class="health-copy">
                      <strong>Gateway</strong>
                      <small id="gatewayText">Noch nicht geladen</small>
                      <span id="gatewayState" class="state-tag neutral">Noch nicht geladen</span>
                    </div>
                  </div>
                  <div class="stat health-card">
                    <div id="backendLamp" class="lamp neutral"></div>
                    <div class="health-copy">
                      <strong>MI50 / llama.cpp</strong>
                      <small id="backendText">Noch nicht geladen</small>
                      <span id="backendState" class="state-tag neutral">Noch nicht geladen</span>
                    </div>
                  </div>
                  <div class="stat">
                    <h3>Backend-Latenz</h3>
                    <strong id="latencyValue">-</strong>
                    <span class="muted">ms fuer /v1/models Check</span>
                  </div>
                  <div class="stat">
                    <h3>Requests</h3>
                    <strong id="requestsValue">-</strong>
                    <span class="muted">seit Prozessstart</span>
                  </div>
                  <div class="stat">
                    <h3>Backend Calls</h3>
                    <strong id="backendCallsValue">-</strong>
                    <span class="muted">inkl. Health Checks</span>
                  </div>
                  <div class="stat">
                    <h3>Uptime</h3>
                    <strong id="uptimeValue">-</strong>
                    <span class="muted">Sekunden</span>
                  </div>
                </div>
              </div>

              <form class="card wide" onsubmit="saveConfig(event)">
                <p class="muted" style="margin-bottom:16px;">Hier stellst du Gateway-Konfiguration und MI50-SSH-Steuerung ein. Die Werte werden direkt in <code class="inline">.env</code> geschrieben. Wichtig: Ein groesseres Gateway-Kontextfenster allein macht das entfernte <code class="inline">llama.cpp</code> nicht automatisch zu 16K-faehig. Dafuer muss das Backend selbst mit passendem Kontext gestartet werden, zum Beispiel ueber einen angepassten MI50-Restart-Command.</p>
                <div class="grid">
                  <label><span>LLAMACPP_BASE_URL</span><input id="LLAMACPP_BASE_URL"></label>
                  <label><span>LLAMACPP_TIMEOUT_SECONDS</span><input id="LLAMACPP_TIMEOUT_SECONDS"></label>
                  <label><span>PUBLIC_MODEL_NAME</span><input id="PUBLIC_MODEL_NAME"></label>
                  <label><span>BACKEND_MODEL_NAME</span><input id="BACKEND_MODEL_NAME"></label>
                  <label><span>BACKEND_CONTEXT_WINDOW</span><input id="BACKEND_CONTEXT_WINDOW"></label>
                  <label><span>CONTEXT_RESPONSE_RESERVE</span><input id="CONTEXT_RESPONSE_RESERVE"></label>
                  <label><span>CONTEXT_CHARS_PER_TOKEN</span><input id="CONTEXT_CHARS_PER_TOKEN"></label>
                  <label><span>DEFAULT_MAX_TOKENS</span><input id="DEFAULT_MAX_TOKENS"></label>
                  <label><span>MI50_SSH_HOST</span><input id="MI50_SSH_HOST" placeholder="192.168.40.111"></label>
                  <label><span>MI50_SSH_USER</span><input id="MI50_SSH_USER" placeholder="llmadmin"></label>
                  <label><span>MI50_SSH_PORT</span><input id="MI50_SSH_PORT" placeholder="22"></label>
                  <label><span>MI50_RESTART_COMMAND</span><input id="MI50_RESTART_COMMAND" placeholder="sudo systemctl restart llama.cpp"></label>
                  <label><span>MI50_STATUS_COMMAND</span><input id="MI50_STATUS_COMMAND" placeholder="sudo systemctl status llama.cpp --no-pager"></label>
                </div>
                <div class="actions">
                  <button type="submit">Speichern</button>
                  <button type="button" class="secondary" onclick="restartBackend()">MI50 neu starten</button>
                </div>
              </form>

              <div class="card wide">
                <label>
                  <span>Continue YAML</span>
                  <textarea id="continueYaml" readonly></textarea>
                </label>
              </div>
            </section>
          </main>

          <script>
            const fields = [
              "LLAMACPP_BASE_URL",
              "LLAMACPP_TIMEOUT_SECONDS",
              "PUBLIC_MODEL_NAME",
              "BACKEND_MODEL_NAME",
              "BACKEND_CONTEXT_WINDOW",
              "CONTEXT_RESPONSE_RESERVE",
              "CONTEXT_CHARS_PER_TOKEN",
              "DEFAULT_MAX_TOKENS",
              "MI50_SSH_HOST",
              "MI50_SSH_USER",
              "MI50_SSH_PORT",
              "MI50_RESTART_COMMAND",
              "MI50_STATUS_COMMAND",
            ];

            const tokenInput = document.getElementById("token");
            tokenInput.value = localStorage.getItem("llmGatewayToken") || "";

            function setStatus(message, level = "ok") {
              const node = document.getElementById("status");
              node.textContent = message;
              node.className = level === "error" ? "status error" : level === "warn" ? "status warn" : "status";
            }

            function authHeaders() {
              const token = tokenInput.value.trim();
              localStorage.setItem("llmGatewayToken", token);
              return {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json",
              };
            }

            function setLamp(id, state) {
              const lamp = document.getElementById(id);
              lamp.className = `lamp ${state}`;
            }

            function setStateTag(id, state, text) {
              const node = document.getElementById(id);
              node.className = `state-tag ${state}`;
              node.textContent = text;
            }

            function formatSeconds(seconds) {
              if (!Number.isFinite(seconds)) return "-";
              if (seconds < 60) return `${seconds.toFixed(0)} s`;
              const minutes = Math.floor(seconds / 60);
              const rest = Math.floor(seconds % 60);
              return `${minutes} m ${rest} s`;
            }

            async function loadConfig() {
              try {
                const res = await fetch("/internal/admin/config", {
                  headers: authHeaders(),
                });
                if (!res.ok) {
                  throw new Error(`Laden fehlgeschlagen: ${res.status}`);
                }
                const data = await res.json();
                for (const key of fields) {
                  document.getElementById(key).value = data[key] || "";
                }
                setStatus("Konfiguration geladen.");
              } catch (error) {
                setStatus(error.message, "error");
              }
            }

            async function saveConfig(event) {
              event.preventDefault();
              const payload = {};
              for (const key of fields) {
                payload[key] = document.getElementById(key).value.trim();
              }
              try {
                const res = await fetch("/internal/admin/config", {
                  method: "POST",
                  headers: authHeaders(),
                  body: JSON.stringify(payload),
                });
                if (!res.ok) {
                  throw new Error(`Speichern fehlgeschlagen: ${res.status}`);
                }
                await res.json();
                setStatus("Gespeichert. Neue Werte gelten fuer neue Requests.");
                await loadContinueConfig();
                await loadConfig();
                await loadMetrics();
                await loadHealth();
              } catch (error) {
                setStatus(error.message, "error");
              }
            }

            async function restartBackend() {
              if (!tokenInput.value.trim()) {
                setStatus("Fuer den MI50-Neustart wird ein Bearer-Token benoetigt.", "warn");
                return;
              }
              if (!confirm("MI50-Backend wirklich per SSH neu starten?")) {
                return;
              }
              try {
                setStatus("MI50-Backend wird neu gestartet ...", "warn");
                const res = await fetch("/internal/admin/restart-backend", {
                  method: "POST",
                  headers: authHeaders(),
                });
                const data = await res.json();
                if (!res.ok) {
                  throw new Error(data?.error?.message || `Restart fehlgeschlagen: ${res.status}`);
                }
                setStatus("MI50-Neustart angestossen. Health wird neu geladen.");
                await loadHealth();
                await loadMetrics();
              } catch (error) {
                setStatus(`MI50-Restart fehlgeschlagen: ${error.message}`, "error");
              }
            }

            async function loadContinueConfig() {
              try {
                const res = await fetch("/internal/admin/continue-config", {
                  headers: authHeaders(),
                });
                if (!res.ok) {
                  throw new Error(`Continue YAML fehlgeschlagen: ${res.status}`);
                }
                const data = await res.json();
                document.getElementById("continueYaml").value = data.yaml;
                setStatus("Continue YAML aktualisiert.");
              } catch (error) {
                setStatus(error.message, "error");
              }
            }

            async function loadHealth() {
              try {
                setLamp("gatewayLamp", "neutral");
                setLamp("backendLamp", "neutral");
                setStateTag("gatewayState", "neutral", "Wird geladen");
                setStateTag("backendState", "neutral", "Wird geladen");
                const res = await fetch("/internal/health", {
                  headers: authHeaders(),
                });
                const data = await res.json();
                const gatewayOk = data.gateway?.status === "ok";
                const backendOk = data.backend?.status === "ok";

                setLamp("gatewayLamp", gatewayOk ? "ok" : "err");
                setLamp("backendLamp", backendOk ? "ok" : data.status === "degraded" ? "warn" : "err");
                setStateTag("gatewayState", gatewayOk ? "ok" : "err", gatewayOk ? "Laeuft" : "Fehler");
                setStateTag(
                  "backendState",
                  backendOk ? "ok" : data.status === "degraded" ? "warn" : "err",
                  backendOk ? "Laeuft" : "Nicht bereit"
                );

                document.getElementById("gatewayText").textContent = gatewayOk
                  ? "Gateway laeuft"
                  : "Gateway meldet Fehler";

                const backendMessage = backendOk
                  ? `${data.backend.model} @ ${data.backend.base_url}`
                  : (data.backend?.message || "Backend nicht bereit");

                document.getElementById("backendText").textContent = backendMessage;
                document.getElementById("latencyValue").textContent =
                  data.backend?.latency_ms != null ? `${data.backend.latency_ms}` : "-";

                if (!res.ok) {
                  setStatus("Health zeigt ein Problem am Backend.", "warn");
                }
              } catch (error) {
                setLamp("gatewayLamp", "warn");
                setLamp("backendLamp", "warn");
                setStateTag("gatewayState", "warn", "Keine Daten");
                setStateTag("backendState", "warn", "Keine Daten");
                document.getElementById("gatewayText").textContent = "Nicht erreichbar";
                document.getElementById("backendText").textContent = "Keine Health-Daten";
                document.getElementById("latencyValue").textContent = "-";
                setStatus(`Health fehlgeschlagen: ${error.message}`, "error");
              }
            }

            async function loadPublicHealth() {
              try {
                const res = await fetch("/health");
                if (!res.ok) {
                  throw new Error(`Public health fehlgeschlagen: ${res.status}`);
                }
                await res.json();
                setLamp("gatewayLamp", "ok");
                setStateTag("gatewayState", "ok", "Laeuft");
                document.getElementById("gatewayText").textContent = "Gateway antwortet auf /health";

                setLamp("backendLamp", "warn");
                setStateTag("backendState", "warn", "Token fehlt");
                document.getElementById("backendText").textContent = "Fuer MI50-Status erst Bearer-Token eingeben";
                document.getElementById("latencyValue").textContent = "-";
                setStatus("Gateway laeuft. Fuer MI50-Status bitte Bearer-Token eingeben.", "warn");
              } catch (error) {
                setLamp("gatewayLamp", "err");
                setStateTag("gatewayState", "err", "Fehler");
                document.getElementById("gatewayText").textContent = "Gateway antwortet nicht auf /health";
                setLamp("backendLamp", "neutral");
                setStateTag("backendState", "neutral", "Noch nicht geladen");
                document.getElementById("backendText").textContent = "Noch nicht geladen";
                setStatus(`Public health fehlgeschlagen: ${error.message}`, "error");
              }
            }

            async function loadMetrics() {
              try {
                const res = await fetch("/internal/metrics", {
                  headers: authHeaders(),
                });
                if (!res.ok) {
                  throw new Error(`Metrics fehlgeschlagen: ${res.status}`);
                }
                const data = await res.json();
                document.getElementById("requestsValue").textContent = data.total_requests ?? "-";
                document.getElementById("backendCallsValue").textContent = data.backend_calls ?? "-";
                document.getElementById("uptimeValue").textContent = formatSeconds(data.uptime_seconds);
              } catch (error) {
                document.getElementById("requestsValue").textContent = "-";
                document.getElementById("backendCallsValue").textContent = "-";
                document.getElementById("uptimeValue").textContent = "-";
                setStatus(error.message, "error");
              }
            }

            async function refreshAll() {
              if (!tokenInput.value.trim()) {
                await loadPublicHealth();
                return;
              }
              await loadConfig();
              await loadHealth();
              await loadMetrics();
              await loadContinueConfig();
            }

            if (tokenInput.value.trim()) {
              refreshAll();
            } else {
              loadPublicHealth();
            }
          </script>
        </body>
        </html>
        """
    )
