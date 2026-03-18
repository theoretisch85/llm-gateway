import logging
from html import escape
from textwrap import dedent

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.auth import get_admin_session_username, require_admin_api_auth
from app.config import get_settings
from app.services.backend_control import (
    gateway_logs,
    gateway_status,
    kai_logs,
    kai_status,
    restart_gateway,
    restart_mi50_backend,
    run_ops_command,
)
from app.services.config_store import read_runtime_config, write_runtime_config


router = APIRouter(tags=["admin"])
logger = logging.getLogger("llm_gateway")


class AdminConfigUpdate(BaseModel):
    LLAMACPP_BASE_URL: str
    LLAMACPP_TIMEOUT_SECONDS: str
    PUBLIC_MODEL_NAME: str
    BACKEND_MODEL_NAME: str
    FAST_MODEL_PUBLIC_NAME: str
    FAST_MODEL_BACKEND_NAME: str
    FAST_MODEL_BASE_URL: str
    DEEP_MODEL_PUBLIC_NAME: str
    DEEP_MODEL_BACKEND_NAME: str
    DEEP_MODEL_BASE_URL: str
    BACKEND_CONTEXT_WINDOW: str
    CONTEXT_RESPONSE_RESERVE: str
    CONTEXT_CHARS_PER_TOKEN: str
    DEFAULT_MAX_TOKENS: str
    ADMIN_DEFAULT_MODE: str
    ROUTING_DEEP_KEYWORDS: str
    ROUTING_LENGTH_THRESHOLD: str
    ROUTING_HISTORY_THRESHOLD: str
    DATABASE_URL: str
    MI50_SSH_HOST: str
    MI50_SSH_USER: str
    MI50_SSH_PORT: str
    MI50_RESTART_COMMAND: str
    MI50_STATUS_COMMAND: str
    MI50_LOGS_COMMAND: str
    MI50_ROCM_SMI_COMMAND: str


@router.get("/internal/admin", response_class=HTMLResponse, response_model=None)
async def admin_page(request: Request) -> HTMLResponse | RedirectResponse:
    username = get_admin_session_username(request)
    if not username:
        return RedirectResponse(url="/admin/login?next=/internal/admin", status_code=303)
    return HTMLResponse(_admin_html(username=username))


@router.get("/internal/admin/config", dependencies=[Depends(require_admin_api_auth)])
async def get_admin_config() -> dict[str, str]:
    settings = get_settings()
    current = read_runtime_config()
    current.setdefault("LLAMACPP_BASE_URL", settings.llamacpp_base_url)
    current.setdefault("LLAMACPP_TIMEOUT_SECONDS", str(settings.llamacpp_timeout_seconds))
    current.setdefault("PUBLIC_MODEL_NAME", settings.public_model_name)
    current.setdefault("BACKEND_MODEL_NAME", settings.backend_model_name)
    current.setdefault("FAST_MODEL_PUBLIC_NAME", settings.fast_model_public_name or settings.public_model_name)
    current.setdefault("FAST_MODEL_BACKEND_NAME", settings.fast_model_backend_name or settings.backend_model_name)
    current.setdefault("FAST_MODEL_BASE_URL", settings.fast_model_base_url or settings.llamacpp_base_url)
    current.setdefault("DEEP_MODEL_PUBLIC_NAME", settings.deep_model_public_name or "")
    current.setdefault("DEEP_MODEL_BACKEND_NAME", settings.deep_model_backend_name or "")
    current.setdefault("DEEP_MODEL_BASE_URL", settings.deep_model_base_url or "")
    current.setdefault("BACKEND_CONTEXT_WINDOW", str(settings.backend_context_window))
    current.setdefault("CONTEXT_RESPONSE_RESERVE", str(settings.context_response_reserve))
    current.setdefault("CONTEXT_CHARS_PER_TOKEN", str(settings.context_chars_per_token))
    current.setdefault("DEFAULT_MAX_TOKENS", str(settings.default_max_tokens))
    current.setdefault("ADMIN_DEFAULT_MODE", settings.admin_default_mode)
    current.setdefault("ROUTING_DEEP_KEYWORDS", settings.routing_deep_keywords)
    current.setdefault("ROUTING_LENGTH_THRESHOLD", str(settings.routing_length_threshold))
    current.setdefault("ROUTING_HISTORY_THRESHOLD", str(settings.routing_history_threshold))
    current.setdefault("DATABASE_URL", settings.database_url or "")
    current.setdefault("MI50_SSH_HOST", settings.mi50_ssh_host or "")
    current.setdefault("MI50_SSH_USER", settings.mi50_ssh_user or "")
    current.setdefault("MI50_SSH_PORT", str(settings.mi50_ssh_port))
    current.setdefault("MI50_RESTART_COMMAND", settings.mi50_restart_command or "sudo systemctl restart kai")
    current.setdefault("MI50_STATUS_COMMAND", settings.mi50_status_command or "systemctl status kai --no-pager")
    current.setdefault("MI50_LOGS_COMMAND", settings.mi50_logs_command or "journalctl -u kai -n 80 --no-pager")
    current.setdefault("MI50_ROCM_SMI_COMMAND", settings.mi50_rocm_smi_command or "rocm-smi --showtemp --showpower --showmemuse --json")
    return current


@router.post("/internal/admin/config", dependencies=[Depends(require_admin_api_auth)])
async def update_admin_config(payload: AdminConfigUpdate) -> dict[str, str]:
    return write_runtime_config(payload.model_dump())


@router.get("/internal/admin/continue-config", dependencies=[Depends(require_admin_api_auth)])
async def get_continue_config(request: Request) -> JSONResponse:
    settings = get_settings()
    host_base = str(request.base_url).rstrip("/")
    rendered = dedent(
        f"""
        name: llm-gateway
        version: 0.0.1
        schema: v1

        models:
          - name: llm-gateway-local
            provider: openai
            model: {settings.public_model_name}
            apiBase: {host_base}/v1
            apiKey: CHANGE_ME
        """
    ).strip()
    return JSONResponse({"yaml": rendered})


@router.post("/internal/admin/restart-backend", dependencies=[Depends(require_admin_api_auth)])
async def restart_backend(request: Request) -> JSONResponse:
    request.state.backend_called = True
    logger.info("admin requested mi50 backend restart")
    try:
        return JSONResponse(restart_mi50_backend())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/ops/{target}/status", dependencies=[Depends(require_admin_api_auth)])
async def ops_status(target: str) -> JSONResponse:
    try:
        if target == "gateway":
            return JSONResponse(gateway_status())
        if target == "kai":
            return JSONResponse(kai_status())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Unknown ops target.")


@router.get("/api/admin/ops/{target}/logs", dependencies=[Depends(require_admin_api_auth)])
async def ops_logs(target: str) -> JSONResponse:
    try:
        if target == "gateway":
            return JSONResponse(gateway_logs())
        if target == "kai":
            return JSONResponse(kai_logs())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Unknown ops target.")


@router.post("/api/admin/ops/{target}/restart", dependencies=[Depends(require_admin_api_auth)])
async def ops_restart(request: Request, target: str) -> JSONResponse:
    request.state.backend_called = target == "kai"
    try:
        if target == "gateway":
            return JSONResponse(restart_gateway())
        if target == "kai":
            return JSONResponse(restart_mi50_backend())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Unknown ops target.")


@router.get("/api/admin/ops/{target}/run/{command_name}", dependencies=[Depends(require_admin_api_auth)])
async def ops_run(request: Request, target: str, command_name: str) -> JSONResponse:
    request.state.backend_called = target == "kai"
    try:
        return JSONResponse(run_ops_command(target, command_name))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _admin_html(username: str) -> str:
    html = dedent(
        """\
        <!doctype html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>llm-gateway admin hub</title>
          <style>
            :root {
              --bg:#0b0f14;
              --bg-deep:#06080c;
              --card:#11161d;
              --card-2:#161c24;
              --ink:#d8e4ef;
              --muted:#7f92a3;
              --line:#2b3744;
              --accent:#8be28b;
              --accent-2:#67c1ff;
              --accent-soft:rgba(139,226,139,.10);
              --warn-soft:rgba(255,196,94,.12);
              --err-soft:rgba(255,107,107,.12);
              --chrome:#1b232d;
            }
            * { box-sizing:border-box; }
            body {
              margin:0;
              font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
              color:var(--ink);
              background:
                linear-gradient(180deg, #10151c 0%, var(--bg) 45%, var(--bg-deep) 100%);
              min-height:100vh;
            }
            body::before {
              content:"";
              position:fixed;
              inset:0;
              pointer-events:none;
              background-image:
                linear-gradient(rgba(67,81,96,.10) 1px, transparent 1px),
                linear-gradient(90deg, rgba(67,81,96,.10) 1px, transparent 1px);
              background-size:24px 24px;
              mask-image:linear-gradient(180deg, rgba(0,0,0,.7), rgba(0,0,0,.15));
            }
            header {
              position:sticky;
              top:0;
              z-index:10;
              background:rgba(9,12,16,.92);
              backdrop-filter:blur(10px);
              border-bottom:1px solid var(--line);
            }
            .nav {
              max-width:1320px;
              margin:0 auto;
              padding:14px 20px;
              display:flex;
              gap:14px;
              align-items:center;
              justify-content:space-between;
            }
            .brand {
              font-size:1.05rem;
              font-weight:700;
              letter-spacing:.14em;
              text-transform:uppercase;
              color:var(--accent);
            }
            .nav-buttons { display:flex; gap:10px; flex-wrap:wrap; }
            .nav-buttons button, .nav form button, button, select, input, textarea {
              font:inherit;
            }
            .nav-buttons button, .nav form button, button.primary, button.secondary {
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 14px;
              cursor:pointer;
              text-transform:uppercase;
              letter-spacing:.06em;
            }
            .nav-buttons button, .nav form button, button.secondary {
              background:var(--chrome);
              color:var(--ink);
            }
            .nav-buttons button.active, button.primary {
              background:#1b2a1f;
              color:var(--accent);
              border-color:#2e5131;
            }
            .userbox { display:flex; gap:10px; align-items:center; color:var(--muted); }
            main { max-width:1320px; margin:0 auto; padding:24px 20px 56px; }
            .panel { display:none; }
            .panel.active { display:block; }
            .hero, .card {
              position:relative;
              background:linear-gradient(180deg, var(--card-2), var(--card));
              border:1px solid var(--line);
              border-radius:10px;
              box-shadow:none;
            }
            .hero::before, .card::before {
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
            .hero { padding:44px 22px 22px; margin-bottom:20px; }
            .hero h1 {
              margin:0 0 8px;
              font-size:1.75rem;
              letter-spacing:.08em;
              text-transform:uppercase;
            }
            .hero p, .muted { color:var(--muted); line-height:1.45; }
            .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; margin-top:18px; }
            .card { padding:44px 18px 18px; }
            .stat strong {
              display:block;
              font-size:1.4rem;
              color:var(--accent);
            }
            .two-col { display:grid; grid-template-columns:1.1fr .9fr; gap:18px; }
            .status {
              margin-top:12px;
              padding:10px 12px;
              border-radius:8px;
              background:var(--accent-soft);
              border:1px solid #284131;
            }
            label { display:block; margin-bottom:12px; }
            label span {
              display:block;
              margin-bottom:6px;
              font-weight:700;
              font-size:.92rem;
              letter-spacing:.06em;
              text-transform:uppercase;
              color:#a4b5c5;
            }
            input, select, textarea {
              width:100%;
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 12px;
              background:#0a0f14;
              color:var(--ink);
            }
            input:focus, select:focus, textarea:focus {
              outline:none;
              border-color:#4b8d4b;
              box-shadow:0 0 0 2px rgba(139,226,139,.10);
            }
            textarea { min-height:140px; resize:vertical; }
            .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
            iframe {
              width:100%;
              min-height:78vh;
              border:1px solid var(--line);
              border-radius:10px;
              background:#0a0f14;
              box-shadow:none;
            }
            pre {
              margin:0;
              white-space:pre-wrap;
              word-break:break-word;
              font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
            }
            ul { margin:8px 0 0 18px; padding:0; }
            code {
              padding:2px 8px;
              border-radius:6px;
              background:#0d141b;
              color:var(--accent);
              border:1px solid var(--line);
            }
            @media (max-width:1000px) {
              .two-col { grid-template-columns:1fr; }
              .nav { flex-direction:column; align-items:flex-start; }
            }
          </style>
        </head>
        <body>
          <header>
            <div class="nav">
              <div class="brand">llm-gateway Admin Hub</div>
              <div class="nav-buttons">
                <button class="active" data-tab="dashboard" onclick="switchTab('dashboard', this)">Dashboard</button>
                <button data-tab="chat" onclick="switchTab('chat', this)">Chat</button>
                <button data-tab="ops" onclick="switchTab('ops', this)">Ops</button>
                <button data-tab="devices" onclick="switchTab('devices', this)">Pi / Devices</button>
              </div>
              <div class="userbox">
                <span>eingeloggt als __USERNAME__</span>
                <form method="post" action="/admin/logout"><button type="submit">Logout</button></form>
              </div>
            </div>
          </header>
          <main>
            <section id="dashboard" class="panel active">
              <div class="hero">
                <h1>Betrieb, Routing und Plattform-Konfig</h1>
                <p>Ein Hub fuer Gateway, Kai, Modellrouting, Continue-Anbindung, Browser-Login und die naechsten Schritte Richtung Pi- und Upload-Plattform.</p>
                <div id="dashboardStatus" class="status">Lade Admin-Daten...</div>
              </div>
              <div class="grid">
                <div class="card stat">
                  <div class="muted">Gateway</div>
                  <strong id="gatewayState">-</strong>
                  <div id="gatewayInfo" class="muted">-</div>
                </div>
                <div class="card stat">
                  <div class="muted">Kai / MI50</div>
                  <strong id="backendState">-</strong>
                  <div id="backendInfo" class="muted">-</div>
                </div>
                <div class="card stat">
                  <div class="muted">Requests</div>
                  <strong id="requestsValue">-</strong>
                  <div class="muted">seit Prozessstart</div>
                </div>
                <div class="card stat">
                  <div class="muted">Backend Calls</div>
                  <strong id="backendCallsValue">-</strong>
                  <div class="muted">inkl. Health Checks</div>
                </div>
                <div class="card stat">
                  <div class="muted">GPU Temp</div>
                  <strong id="gpuTempValue">-</strong>
                  <div class="muted">MI50 edge temp</div>
                </div>
                <div class="card stat">
                  <div class="muted">Board Power</div>
                  <strong id="gpuPowerValue">-</strong>
                  <div class="muted">rocm-smi watt</div>
                </div>
                <div class="card stat">
                  <div class="muted">VRAM</div>
                  <strong id="gpuVramValue">-</strong>
                  <div class="muted">belegt / gesamt</div>
                </div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>Gateway / Routing</h2>
                  <form onsubmit="saveConfig(event)">
                    <div class="grid">
                      <label><span>LLAMACPP_BASE_URL</span><input id="LLAMACPP_BASE_URL"></label>
                      <label><span>PUBLIC_MODEL_NAME</span><input id="PUBLIC_MODEL_NAME"></label>
                      <label><span>BACKEND_MODEL_NAME</span><input id="BACKEND_MODEL_NAME"></label>
                      <label><span>FAST_MODEL_PUBLIC_NAME</span><input id="FAST_MODEL_PUBLIC_NAME"></label>
                      <label><span>FAST_MODEL_BACKEND_NAME</span><input id="FAST_MODEL_BACKEND_NAME"></label>
                      <label><span>FAST_MODEL_BASE_URL</span><input id="FAST_MODEL_BASE_URL"></label>
                      <label><span>DEEP_MODEL_PUBLIC_NAME</span><input id="DEEP_MODEL_PUBLIC_NAME"></label>
                      <label><span>DEEP_MODEL_BACKEND_NAME</span><input id="DEEP_MODEL_BACKEND_NAME"></label>
                      <label><span>DEEP_MODEL_BASE_URL</span><input id="DEEP_MODEL_BASE_URL"></label>
                      <label><span>BACKEND_CONTEXT_WINDOW</span><input id="BACKEND_CONTEXT_WINDOW"></label>
                      <label><span>CONTEXT_RESPONSE_RESERVE</span><input id="CONTEXT_RESPONSE_RESERVE"></label>
                      <label><span>DEFAULT_MAX_TOKENS</span><input id="DEFAULT_MAX_TOKENS"></label>
                      <label><span>ADMIN_DEFAULT_MODE</span><input id="ADMIN_DEFAULT_MODE"></label>
                      <label><span>ROUTING_LENGTH_THRESHOLD</span><input id="ROUTING_LENGTH_THRESHOLD"></label>
                      <label><span>ROUTING_HISTORY_THRESHOLD</span><input id="ROUTING_HISTORY_THRESHOLD"></label>
                      <label><span>MI50_SSH_HOST</span><input id="MI50_SSH_HOST"></label>
                      <label><span>MI50_SSH_USER</span><input id="MI50_SSH_USER"></label>
                      <label><span>MI50_SSH_PORT</span><input id="MI50_SSH_PORT"></label>
                      <label style="grid-column:1/-1;"><span>ROUTING_DEEP_KEYWORDS</span><input id="ROUTING_DEEP_KEYWORDS"></label>
                      <label style="grid-column:1/-1;"><span>MI50_RESTART_COMMAND</span><input id="MI50_RESTART_COMMAND"></label>
                      <label style="grid-column:1/-1;"><span>MI50_STATUS_COMMAND</span><input id="MI50_STATUS_COMMAND"></label>
                      <label style="grid-column:1/-1;"><span>MI50_LOGS_COMMAND</span><input id="MI50_LOGS_COMMAND"></label>
                      <label style="grid-column:1/-1;"><span>MI50_ROCM_SMI_COMMAND</span><input id="MI50_ROCM_SMI_COMMAND"></label>
                    </div>
                    <div class="actions">
                      <button class="primary" type="submit">Speichern</button>
                      <button class="secondary" type="button" onclick="loadContinueConfig()">Continue YAML</button>
                    </div>
                  </form>
                </div>
                <div class="card">
                  <h2>Continue / Persistenz</h2>
                  <label><span>DATABASE_URL</span><input id="DATABASE_URL"></label>
                  <label><span>Continue YAML</span><textarea id="continueYaml" readonly></textarea></label>
                </div>
              </div>
            </section>

            <section id="chat" class="panel">
              <div class="hero">
                <h1>Admin Chat</h1>
                <p>Session-Chat, Auto-Routing und Streaming bleiben im Hub. Fuer jetzt wird die bestehende Chat-Oberflaeche direkt eingebettet.</p>
              </div>
              <div class="card">
                <iframe src="/internal/chat?embedded=1" title="Admin Chat"></iframe>
              </div>
            </section>

            <section id="ops" class="panel">
              <div class="hero">
                <h1>Ops Konsole</h1>
                <p>Kein freies Root-Webterminal. Stattdessen eine sichere Terminal-V1 mit Status, Logs, Restart und freigegebenen Preset-Befehlen fuer Gateway und Kai.</p>
              </div>
              <div class="card">
                <div class="actions">
                  <select id="opsTarget">
                    <option value="gateway">gateway</option>
                    <option value="kai">kai</option>
                  </select>
                  <select id="opsCommand">
                    <option value="status">status</option>
                    <option value="logs">logs</option>
                    <option value="restart">restart</option>
                    <option value="health">health</option>
                    <option value="uptime">uptime (gateway)</option>
                    <option value="models">models (kai)</option>
                    <option value="telemetry">telemetry (kai)</option>
                  </select>
                  <button class="secondary" type="button" onclick="opsAction('status')">Status</button>
                  <button class="secondary" type="button" onclick="opsAction('logs')">Logs</button>
                  <button class="primary" type="button" onclick="opsAction('restart')">Restart</button>
                  <button class="secondary" type="button" onclick="runPresetCommand()">Preset ausfuehren</button>
                </div>
                <div id="opsStatus" class="status">Waehle ein Ziel und eine Aktion.</div>
                <div class="card" style="margin-top:14px; background:#fcfaf4;">
                  <pre id="opsOutput">Noch keine Ausgabe.</pre>
                </div>
              </div>
            </section>

            <section id="devices" class="panel">
              <div class="hero">
                <h1>Pi / Device Vorbereitung</h1>
                <p>Der Raspberry Pi soll spaeter lokal TTS/STT machen und den Gateway nur fuer Chat, Routing und Session-Memory ansprechen.</p>
              </div>
              <div class="two-col">
                <div class="card">
                  <h2>Device API</h2>
                  <p class="muted">Der vorbereitete Pfad ist <code>/api/device/ask</code>. Das ist die Grundlage fuer einen Pi-Client mit Mikrofon, Lautsprecher und lokalem TTS.</p>
                  <label style="margin-top:12px;">
                    <span>Beispiel-Request</span>
                    <textarea readonly>curl -s http://GATEWAY:8000/api/device/ask \
  -H "Authorization: Bearer DEVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Wie ist der Status von Kai?","mode":"auto"}'</textarea>
                  </label>
                </div>
                <div class="card">
                  <h2>Naechste sinnvolle Schritte</h2>
                  <ul>
                    <li>echtes Browser-Login statt Token-Eingabefeld</li>
                    <li>Pi mit eigenem Device-Token</li>
                    <li>Upload fuer Text und PDF</li>
                    <li>spaeter ein kleines Voice-Frontend auf dem Pi</li>
                    <li>persistentes Memory ueber PostgreSQL</li>
                  </ul>
                </div>
              </div>
            </section>
          </main>
          <script>
            window.currentConfig = {};
            const configFields = [
              "LLAMACPP_BASE_URL",
              "PUBLIC_MODEL_NAME",
              "BACKEND_MODEL_NAME",
              "FAST_MODEL_PUBLIC_NAME",
              "FAST_MODEL_BACKEND_NAME",
              "FAST_MODEL_BASE_URL",
              "DEEP_MODEL_PUBLIC_NAME",
              "DEEP_MODEL_BACKEND_NAME",
              "DEEP_MODEL_BASE_URL",
              "BACKEND_CONTEXT_WINDOW",
              "CONTEXT_RESPONSE_RESERVE",
              "DEFAULT_MAX_TOKENS",
              "ADMIN_DEFAULT_MODE",
              "ROUTING_DEEP_KEYWORDS",
              "ROUTING_LENGTH_THRESHOLD",
              "ROUTING_HISTORY_THRESHOLD",
              "DATABASE_URL",
              "MI50_SSH_HOST",
              "MI50_SSH_USER",
              "MI50_SSH_PORT",
              "MI50_RESTART_COMMAND",
              "MI50_STATUS_COMMAND",
              "MI50_LOGS_COMMAND",
              "MI50_ROCM_SMI_COMMAND"
            ];

            function switchTab(id, button) {
              document.querySelectorAll(".panel").forEach((panel) => panel.classList.remove("active"));
              document.querySelectorAll(".nav-buttons button").forEach((node) => node.classList.remove("active"));
              document.getElementById(id).classList.add("active");
              button.classList.add("active");
              const url = new URL(window.location.href);
              url.searchParams.set("tab", id);
              history.replaceState(null, "", url);
            }

            function setDashboardStatus(message, error = false) {
              const node = document.getElementById("dashboardStatus");
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            async function loadDashboard() {
              try {
                const [healthRes, metricsRes, configRes] = await Promise.all([
                  fetch("/internal/health"),
                  fetch("/internal/metrics"),
                  fetch("/internal/admin/config"),
                ]);
                if (!healthRes.ok || !metricsRes.ok || !configRes.ok) {
                  throw new Error("Dashboard-Daten konnten nicht geladen werden.");
                }

                const health = await healthRes.json();
                const metrics = await metricsRes.json();
                const config = await configRes.json();
                window.currentConfig = config;
                let telemetry = null;
                try {
                  const telemetryRes = await fetch("/api/admin/ops/kai/run/telemetry");
                  if (telemetryRes.ok) {
                    telemetry = await telemetryRes.json();
                  }
                } catch (error) {
                  telemetry = null;
                }

                document.getElementById("gatewayState").textContent = health.gateway?.status || "-";
                document.getElementById("gatewayInfo").textContent = "Gateway antwortet";
                document.getElementById("backendState").textContent = health.backend?.status || "-";
                document.getElementById("backendInfo").textContent = `${health.backend?.model || "-"} @ ${health.backend?.base_url || "-"}`;
                document.getElementById("requestsValue").textContent = metrics.total_requests ?? "-";
                document.getElementById("backendCallsValue").textContent = metrics.backend_calls ?? "-";
                document.getElementById("gpuTempValue").textContent = telemetry?.temperature_c != null ? `${telemetry.temperature_c} C` : "n/a";
                document.getElementById("gpuPowerValue").textContent = telemetry?.power_w != null ? `${telemetry.power_w} W` : "n/a";
                if (telemetry?.vram_used_gib != null && telemetry?.vram_total_gib != null) {
                  document.getElementById("gpuVramValue").textContent =
                    `${telemetry.vram_used_gib} / ${telemetry.vram_total_gib} GiB (${telemetry.vram_percent ?? "?"}%)`;
                } else if (telemetry?.vram_percent != null) {
                  document.getElementById("gpuVramValue").textContent = `${telemetry.vram_percent}%`;
                } else {
                  document.getElementById("gpuVramValue").textContent = "n/a";
                }

                for (const key of configFields) {
                  const node = document.getElementById(key);
                  if (node) node.value = config[key] || "";
                }
                setDashboardStatus("Dashboard und Konfiguration geladen.");
              } catch (error) {
                setDashboardStatus(error.message, true);
              }
            }

            async function saveConfig(event) {
              event.preventDefault();
              const payload = { ...window.currentConfig };
              for (const key of configFields) {
                payload[key] = document.getElementById(key).value.trim();
              }
              payload.LLAMACPP_TIMEOUT_SECONDS = payload.LLAMACPP_TIMEOUT_SECONDS || "60.0";
              payload.CONTEXT_CHARS_PER_TOKEN = payload.CONTEXT_CHARS_PER_TOKEN || "4.0";
              payload.MI50_SSH_PORT = payload.MI50_SSH_PORT || "22";
              payload.MI50_RESTART_COMMAND = payload.MI50_RESTART_COMMAND || "sudo systemctl restart kai";
              payload.MI50_STATUS_COMMAND = payload.MI50_STATUS_COMMAND || "systemctl status kai --no-pager";
              payload.MI50_LOGS_COMMAND = payload.MI50_LOGS_COMMAND || "journalctl -u kai -n 80 --no-pager";
              payload.MI50_ROCM_SMI_COMMAND = payload.MI50_ROCM_SMI_COMMAND || "rocm-smi --showtemp --showpower --showmemuse --json";

              const res = await fetch("/internal/admin/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
              });
              if (!res.ok) {
                setDashboardStatus(`Speichern fehlgeschlagen: ${res.status}`, true);
                return;
              }
              await loadDashboard();
              setDashboardStatus("Gespeichert. Neue Werte gelten fuer neue Requests.");
            }

            async function loadContinueConfig() {
              const res = await fetch("/internal/admin/continue-config");
              if (!res.ok) {
                setDashboardStatus(`Continue YAML fehlgeschlagen: ${res.status}`, true);
                return;
              }
              const data = await res.json();
              document.getElementById("continueYaml").value = data.yaml;
              setDashboardStatus("Continue YAML geladen.");
            }

            async function opsAction(action) {
              const target = document.getElementById("opsTarget").value;
              const statusNode = document.getElementById("opsStatus");
              const outputNode = document.getElementById("opsOutput");
              statusNode.textContent = `${target}: ${action} laeuft...`;
              statusNode.style.background = "#e8dcc7";

              let res;
              if (action === "status" || action === "logs") {
                res = await fetch(`/api/admin/ops/${target}/${action}`);
              } else {
                res = await fetch(`/api/admin/ops/${target}/restart`, { method: "POST" });
              }
              const data = await res.json();
              if (!res.ok) {
                statusNode.textContent = data?.error?.message || `Ops-Fehler (${res.status})`;
                statusNode.style.background = "#f9dddd";
                outputNode.textContent = JSON.stringify(data, null, 2);
                return;
              }
              statusNode.textContent = `${target}: ${action} erfolgreich`;
              statusNode.style.background = "#dff5e7";
              outputNode.textContent = data.output || JSON.stringify(data, null, 2);
            }

            async function runPresetCommand() {
              const target = document.getElementById("opsTarget").value;
              const command = document.getElementById("opsCommand").value;
              const statusNode = document.getElementById("opsStatus");
              const outputNode = document.getElementById("opsOutput");
              statusNode.textContent = `${target}: ${command} laeuft...`;
              statusNode.style.background = "#e8dcc7";

              const res = await fetch(`/api/admin/ops/${target}/run/${command}`);
              const data = await res.json();
              if (!res.ok) {
                statusNode.textContent = data?.error?.message || `Ops-Fehler (${res.status})`;
                statusNode.style.background = "#f9dddd";
                outputNode.textContent = JSON.stringify(data, null, 2);
                return;
              }
              statusNode.textContent = `${target}: ${command} erfolgreich`;
              statusNode.style.background = "#dff5e7";
              outputNode.textContent = data.output || JSON.stringify(data, null, 2);
            }

            const requestedTab = new URL(window.location.href).searchParams.get("tab");
            if (requestedTab) {
              const button = document.querySelector(`.nav-buttons button[data-tab="${requestedTab}"]`);
              if (button) switchTab(requestedTab, button);
            }

            loadDashboard();
            loadContinueConfig();
          </script>
        </body>
        </html>
        """
    )
    return html.replace("__USERNAME__", escape(username))
