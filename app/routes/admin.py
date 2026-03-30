import logging
from html import escape
from pathlib import Path
from textwrap import dedent
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import get_admin_session_username, require_admin_api_auth
from app.config import get_settings
from app.core.roles import ActorContext, ROLE_ADMIN
from app.metrics import metrics
from app.orchestrator import ToolOrchestrator
from app.services.home_assistant import HomeAssistantClient, HomeAssistantConfigError, HomeAssistantRequestError
from app.services.backend_control import (
    ops_command_catalog,
    gateway_system_telemetry,
    run_remote_backend_activation,
    stop_mi50_service,
    switch_mi50_service,
)
from app.services.backend_profiles import (
    activate_backend_profile,
    build_runtime_updates_for_backend_profile,
    clear_active_backend_profile,
    delete_backend_profile,
    get_active_backend_profile,
    get_backend_profile,
    known_backend_service_names,
    list_backend_profiles,
    save_backend_profile,
)
from app.services.mcp_custom_tools import delete_custom_mcp_tool, list_custom_mcp_tools, save_custom_mcp_tool
from app.services.mcp_registry import get_builtin_mcp_tool_names, get_mcp_tools
from app.services.config_store import read_runtime_config, write_runtime_config
from app.services.database_admin import database_status, initialize_database_schema
from app.services.database_profiles import (
    activate_database_profile,
    delete_database_profile,
    list_database_profiles,
    save_database_profile,
)
from app.services.device_bootstrap import (
    build_device_install_script,
    run_device_bootstrap_over_ssh,
    run_device_env_sync_over_ssh,
    run_device_face_apply_over_ssh,
    run_device_install_over_ssh,
    run_device_probe_over_ssh,
)
from app.services.device_profiles import (
    activate_device_profile,
    delete_device_profile,
    get_active_device_profile,
    get_device_profile,
    list_device_profiles,
    save_device_profile,
)
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError
from app.services.session_memory import get_session_store
from app.services.storage_library import (
    activate_storage_profile,
    delete_storage_profile,
    save_storage_profile,
    storage_overview,
    upload_document,
)
from app.tools.executor import ToolExecutionError


router = APIRouter(tags=["admin"])
logger = logging.getLogger("llm_gateway")
tool_orchestrator = ToolOrchestrator()


@router.get("/internal/admin", response_class=HTMLResponse, response_model=None)
async def admin_page(request: Request, tab: str = "dashboard") -> HTMLResponse | RedirectResponse:
    username = get_admin_session_username(request)
    if not username:
        return RedirectResponse(url="/admin/login?next=/internal/admin", status_code=303)
    active_tab = tab if tab in {"dashboard", "settings", "skills", "chat", "memory", "database", "home-assistant", "storage", "ops", "devices"} else "dashboard"
    initial_data = await _build_initial_admin_data(base_url=str(request.base_url).rstrip("/"))
    db_message = request.query_params.get("db_message")
    if db_message:
        initial_data["database_status"] = db_message
    settings_message = request.query_params.get("settings_message")
    if settings_message:
        initial_data["settings_status"] = settings_message
    edit_profile_id = request.query_params.get("edit_profile")
    if active_tab == "settings" and edit_profile_id:
        try:
            profile = get_backend_profile(edit_profile_id)
            initial_data["backend_profile_form_id"] = str(profile.get("id") or "")
            initial_data["backend_profile_form_name"] = str(profile.get("name") or "")
            initial_data["backend_profile_form_public_model_name"] = str(profile.get("public_model_name") or "")
            initial_data["backend_profile_form_backend_model_name"] = str(profile.get("backend_model_name") or "")
            initial_data["backend_profile_form_base_url"] = str(profile.get("base_url") or "")
            initial_data["backend_profile_form_context_window"] = str(profile.get("context_window") or initial_data.get("cfg_BACKEND_CONTEXT_WINDOW", ""))
            initial_data["backend_profile_form_response_reserve"] = str(profile.get("response_reserve") or initial_data.get("cfg_CONTEXT_RESPONSE_RESERVE", ""))
            initial_data["backend_profile_form_default_max_tokens"] = str(profile.get("default_max_tokens") or initial_data.get("cfg_DEFAULT_MAX_TOKENS", ""))
            initial_data["backend_profile_form_ngl_layers"] = str(profile.get("ngl_layers") or "")
            initial_data["backend_profile_form_service_name"] = str(profile.get("service_name") or "")
            initial_data["backend_profile_form_activate_command"] = str(profile.get("activate_command") or "")
            initial_data["backend_profile_form_status_command"] = str(profile.get("status_command") or "")
            initial_data["backend_profile_form_logs_command"] = str(profile.get("logs_command") or "")
            initial_data["backend_profile_preview"] = _render_backend_profile_preview(profile)
            initial_data["settings_status"] = f"Profil '{profile.get('name')}' zum Bearbeiten geladen."
        except Exception as exc:
            initial_data["settings_status"] = str(exc)
    storage_message = request.query_params.get("storage_message")
    if storage_message:
        initial_data["storage_status"] = storage_message
    device_message = request.query_params.get("device_message")
    if device_message:
        initial_data["device_status"] = device_message
    edit_device_id = request.query_params.get("edit_device")
    if active_tab == "devices" and edit_device_id:
        try:
            profile = get_device_profile(edit_device_id)
            initial_data["device_profile_form_id"] = str(profile.get("id") or "")
            initial_data["device_profile_form_name"] = str(profile.get("name") or "")
            initial_data["device_profile_form_gateway_base_url"] = str(profile.get("gateway_base_url") or "")
            initial_data["device_profile_form_device_token"] = str(profile.get("device_token") or "")
            initial_data["device_profile_form_ssh_host"] = str(profile.get("ssh_host") or "")
            initial_data["device_profile_form_ssh_user"] = str(profile.get("ssh_user") or "")
            initial_data["device_profile_form_ssh_port"] = str(profile.get("ssh_port") or "22")
            initial_data["device_profile_form_ssh_password"] = ""
            initial_data["device_profile_form_remote_dir"] = str(profile.get("remote_dir") or "~/kai-pi")
            initial_data["device_profile_form_ssh_root_prefix"] = str(profile.get("ssh_root_prefix") or "sudo -n")
            initial_data["device_profile_form_notes"] = str(profile.get("notes") or "")
            initial_data["device_bootstrap_preview"] = build_device_install_script(profile)
            initial_data["device_status"] = f"Device-Profil '{profile.get('name')}' zum Bearbeiten geladen."
        except Exception as exc:
            initial_data["device_status"] = str(exc)
    return HTMLResponse(
        _admin_html(username=username, active_tab=active_tab, initial_data=initial_data),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/internal/admin/config", dependencies=[Depends(require_admin_api_auth)])
async def get_admin_config() -> dict[str, str]:
    settings = get_settings()
    return _build_admin_config_values(settings)


@router.get("/api/admin/system/summary", dependencies=[Depends(require_admin_api_auth)])
async def get_admin_system_summary() -> JSONResponse:
    return JSONResponse(gateway_system_telemetry(), headers={"Cache-Control": "no-store, max-age=0"})


def _build_admin_config_values(settings) -> dict[str, str]:
    current = read_runtime_config()
    current.setdefault("LLAMACPP_BASE_URL", settings.llamacpp_base_url)
    current.setdefault("LLAMACPP_TIMEOUT_SECONDS", str(settings.llamacpp_timeout_seconds))
    current.setdefault("GATEWAY_LOCAL_ROOT_PREFIX", settings.gateway_local_root_prefix or "sudo -n")
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
    current.pop("DATABASE_URL", None)
    current.setdefault("HOME_ASSISTANT_BASE_URL", settings.home_assistant_base_url or "")
    current.setdefault("HOME_ASSISTANT_TOKEN", settings.home_assistant_token or "")
    current.setdefault("HOME_ASSISTANT_TIMEOUT_SECONDS", str(settings.home_assistant_timeout_seconds))
    current.setdefault("HOME_ASSISTANT_ALLOWED_SERVICES", settings.home_assistant_allowed_services)
    current.setdefault("HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES", settings.home_assistant_allowed_entity_prefixes)
    current.setdefault("VISION_BASE_URL", settings.vision_base_url or "")
    current.setdefault("VISION_MODEL_NAME", settings.vision_model_name or "")
    current.setdefault("VISION_PROMPT", settings.vision_prompt)
    current.setdefault("VISION_MAX_TOKENS", str(settings.vision_max_tokens))
    current.setdefault("MI50_SSH_HOST", settings.mi50_ssh_host or "")
    current.setdefault("MI50_SSH_USER", settings.mi50_ssh_user or "")
    current.setdefault("MI50_SSH_PORT", str(settings.mi50_ssh_port))
    current.setdefault("MI50_RESTART_COMMAND", settings.mi50_restart_command or "sudo systemctl restart kai")
    current.setdefault("MI50_STATUS_COMMAND", settings.mi50_status_command or "systemctl status kai --no-pager")
    current.setdefault("MI50_LOGS_COMMAND", settings.mi50_logs_command or "journalctl -u kai -n 80 --no-pager")
    current.setdefault("MI50_ROCM_SMI_COMMAND", settings.mi50_rocm_smi_command or "rocm-smi --showtemp --showpower --showmemuse --json")
    return current


@router.post("/internal/admin/config", dependencies=[Depends(require_admin_api_auth)])
async def update_admin_config(payload: dict[str, str | None]) -> dict[str, str]:
    settings = get_settings()
    merged = _build_admin_config_values(settings)
    for key, value in payload.items():
        if value is None:
            continue
        merged[key] = str(value)
    updated = write_runtime_config(merged)
    updated.pop("DATABASE_URL", None)
    return updated


@router.post("/internal/admin/backend-profile/save-form")
async def save_backend_profile_form(
    request: Request,
    BACKEND_PROFILE_ID: str = Form(default=""),
    BACKEND_PROFILE_NAME: str = Form(default=""),
    PROFILE_PUBLIC_MODEL_NAME: str = Form(default=""),
    PROFILE_BACKEND_MODEL_NAME: str = Form(default=""),
    PROFILE_BASE_URL: str = Form(default=""),
    PROFILE_CONTEXT_WINDOW: str = Form(default=""),
    PROFILE_RESPONSE_RESERVE: str = Form(default=""),
    PROFILE_DEFAULT_MAX_TOKENS: str = Form(default=""),
    PROFILE_MI50_NGL: str = Form(default=""),
    PROFILE_MI50_SERVICE_NAME: str = Form(default=""),
    PROFILE_MI50_ACTIVATE_COMMAND: str = Form(default=""),
    PROFILE_MI50_STATUS_COMMAND: str = Form(default=""),
    PROFILE_MI50_LOGS_COMMAND: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dsettings", status_code=303)
    try:
        profile = save_backend_profile(
            profile_id=BACKEND_PROFILE_ID or None,
            name=BACKEND_PROFILE_NAME,
            public_model_name=PROFILE_PUBLIC_MODEL_NAME,
            backend_model_name=PROFILE_BACKEND_MODEL_NAME,
            base_url=PROFILE_BASE_URL,
            context_window=PROFILE_CONTEXT_WINDOW,
            response_reserve=PROFILE_RESPONSE_RESERVE,
            default_max_tokens=PROFILE_DEFAULT_MAX_TOKENS,
            ngl_layers=PROFILE_MI50_NGL,
            service_name=PROFILE_MI50_SERVICE_NAME,
            activate_command=PROFILE_MI50_ACTIVATE_COMMAND,
            status_command=PROFILE_MI50_STATUS_COMMAND,
            logs_command=PROFILE_MI50_LOGS_COMMAND,
            make_active=False,
        )
        if profile.get("is_active"):
            return _settings_redirect(
                f"Backend-Profil '{profile['name']}' gespeichert. Da es aktuell aktiv ist, werden die Aenderungen erst nach 'Reset' oder erneutem Aktivieren wirksam.",
                error=False,
            )
        return _settings_redirect(f"Backend-Profil '{profile['name']}' gespeichert.", error=False)
    except Exception as exc:
        return _settings_redirect(str(exc), error=True)


@router.post("/internal/admin/backend-profile/activate-form")
async def activate_backend_profile_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dsettings", status_code=303)
    try:
        profile = get_backend_profile(profile_id)
        service_name = str(profile.get("service_name") or "").strip()
        activate_command = str(profile.get("activate_command") or "").strip()
        ngl_layers = str(profile.get("ngl_layers") or "").strip()
        service_output = ""
        if activate_command:
            switch_result = run_remote_backend_activation(activate_command, ngl_layers=ngl_layers)
            service_output = str(switch_result.get("output") or "")
        elif service_name:
            switch_result = switch_mi50_service(service_name, known_backend_service_names())
            service_output = str(switch_result.get("output") or "")
        write_runtime_config(build_runtime_updates_for_backend_profile(profile))
        activate_backend_profile(profile_id)
        if service_output:
            return _settings_redirect(f"Backend-Profil '{profile['name']}' aktiviert und MI50-Service umgeschaltet.", error=False)
        return _settings_redirect(f"Backend-Profil '{profile['name']}' aktiviert.", error=False)
    except Exception as exc:
        return _settings_redirect(str(exc), error=True)


@router.post("/internal/admin/backend-profile/deactivate-form")
async def deactivate_backend_profile_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dsettings", status_code=303)
    try:
        profile = get_backend_profile(profile_id)
        service_name = str(profile.get("service_name") or "").strip()
        stop_output = ""
        if service_name:
            stop_result = stop_mi50_service(service_name)
            stop_output = str(stop_result.get("output") or "")
        cleared = clear_active_backend_profile(profile_id)
        name = cleared.get("previous_active_profile_name") or profile.get("name") or "Backend-Profil"
        if stop_output:
            return _settings_redirect(f"Backend-Profil '{name}' deaktiviert. MI50-Service gestoppt.", error=False)
        return _settings_redirect(f"Backend-Profil '{name}' deaktiviert.", error=False)
    except Exception as exc:
        return _settings_redirect(str(exc), error=True)


@router.post("/internal/admin/backend-profile/delete-form")
async def delete_backend_profile_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dsettings", status_code=303)
    try:
        result = delete_backend_profile(profile_id)
        if result.get("deleted_was_active"):
            return _settings_redirect("Aktives Backend-Profil geloescht. Gateway-Konfig bleibt bis zur naechsten Umschaltung unveraendert.", error=False)
        return _settings_redirect("Backend-Profil geloescht.", error=False)
    except Exception as exc:
        return _settings_redirect(str(exc), error=True)


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


@router.post("/internal/admin/restart-backend")
async def restart_backend(request: Request, auth_subject: str = Depends(require_admin_api_auth)) -> JSONResponse:
    logger.info("admin requested mi50 backend restart")
    try:
        result = await _run_admin_ops_tool(
            request=request,
            auth_subject=auth_subject,
            target="kai",
            command_name="restart",
            source="api.admin.restart_backend",
        )
        return JSONResponse(result)
    except ToolExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/internal/admin/database/status", dependencies=[Depends(require_admin_api_auth)])
async def get_database_status() -> JSONResponse:
    settings = get_settings()
    try:
        return JSONResponse(await database_status(settings))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/internal/admin/database/init", dependencies=[Depends(require_admin_api_auth)])
async def post_database_init() -> JSONResponse:
    settings = get_settings()
    try:
        return JSONResponse(await initialize_database_schema(settings))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/internal/admin/database/save-form")
async def save_database_form(
    request: Request,
    DATABASE_URL: str = Form(default=""),
    DATABASE_PROFILE_NAME: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddatabase", status_code=303)
    database_url = DATABASE_URL.strip()
    if not database_url:
        return _database_redirect("DATABASE_URL ist leer. Ohne URL kann kein Profil gespeichert werden.", error=True)
    profile = save_database_profile(DATABASE_PROFILE_NAME, database_url, make_active=True)
    write_runtime_config({"DATABASE_URL": database_url})
    return _database_redirect(f"Profil '{profile['name']}' gespeichert und aktiviert.", error=False)


@router.post("/internal/admin/database/test-form")
async def test_database_form(
    request: Request,
    DATABASE_URL: str = Form(default=""),
    DATABASE_PROFILE_NAME: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddatabase", status_code=303)
    current_settings = get_settings()
    database_url = DATABASE_URL.strip() or (current_settings.database_url or "")
    if not database_url:
        return _database_redirect("DATABASE_URL ist leer. Bitte URL eintragen oder ein bestehendes Profil aktivieren.", error=True)
    if DATABASE_URL.strip():
        save_database_profile(DATABASE_PROFILE_NAME, database_url, make_active=True)
    write_runtime_config({"DATABASE_URL": database_url})
    settings = get_settings()
    try:
        result = await database_status(settings)
        return _database_redirect(str(result.get("message") or "Datenbankstatus geladen."), error=not bool(result.get("connected")))
    except Exception as exc:
        return _database_redirect(str(exc), error=True)


@router.post("/internal/admin/database/init-form")
async def init_database_form(
    request: Request,
    DATABASE_URL: str = Form(default=""),
    DATABASE_PROFILE_NAME: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddatabase", status_code=303)
    current_settings = get_settings()
    database_url = DATABASE_URL.strip() or (current_settings.database_url or "")
    if not database_url:
        return _database_redirect("DATABASE_URL ist leer. Bitte URL eintragen oder ein bestehendes Profil aktivieren.", error=True)
    if DATABASE_URL.strip():
        save_database_profile(DATABASE_PROFILE_NAME, database_url, make_active=True)
    write_runtime_config({"DATABASE_URL": database_url})
    settings = get_settings()
    try:
        result = await initialize_database_schema(settings)
        return _database_redirect(str(result.get("message") or "Schema initialisiert."), error=False)
    except Exception as exc:
        return _database_redirect(str(exc), error=True)


@router.post("/internal/admin/database/activate-form")
async def activate_database_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddatabase", status_code=303)
    try:
        database_url = activate_database_profile(profile_id)
        write_runtime_config({"DATABASE_URL": database_url})
        return _database_redirect("Datenbank-Profil aktiviert.", error=False)
    except Exception as exc:
        return _database_redirect(str(exc), error=True)


@router.post("/internal/admin/database/delete-form")
async def delete_database_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddatabase", status_code=303)
    try:
        result = delete_database_profile(profile_id)
        if result.get("deleted_was_active"):
            write_runtime_config({"DATABASE_URL": ""})
            return _database_redirect("Aktives Datenbank-Profil geloescht. Gateway faellt auf RAM-Store zurueck.", error=False)
        return _database_redirect("Datenbank-Profil geloescht.", error=False)
    except Exception as exc:
        return _database_redirect(str(exc), error=True)


@router.get("/api/admin/storage/overview", dependencies=[Depends(require_admin_api_auth)])
async def get_storage_overview() -> JSONResponse:
    settings = get_settings()
    try:
        return JSONResponse(await storage_overview(settings))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/storage/upload", dependencies=[Depends(require_admin_api_auth)])
async def upload_storage_document_api(
    STORAGE_UPLOAD_PROFILE_ID: str = Form(default=""),
    DOCUMENT_TITLE: str = Form(default=""),
    DOCUMENT_TAGS: str = Form(default=""),
    DOCUMENT_FILE: UploadFile = File(...),
) -> JSONResponse:
    settings = get_settings()
    try:
        document = await upload_document(
            settings=settings,
            file=DOCUMENT_FILE,
            storage_profile_id=STORAGE_UPLOAD_PROFILE_ID or None,
            title=DOCUMENT_TITLE,
            tags=DOCUMENT_TAGS,
        )
        return JSONResponse({"document": document})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/internal/admin/storage/location/save-form")
async def save_storage_location_form(
    request: Request,
    STORAGE_PROFILE_NAME: str = Form(default=""),
    STORAGE_BACKEND_TYPE: str = Form(default="local"),
    STORAGE_BASE_PATH: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dstorage", status_code=303)
    try:
        profile = save_storage_profile(STORAGE_PROFILE_NAME, STORAGE_BACKEND_TYPE, STORAGE_BASE_PATH, make_active=True)
        return _storage_redirect(f"Storage-Profil '{profile['name']}' gespeichert und aktiviert.", error=False)
    except Exception as exc:
        return _storage_redirect(str(exc), error=True)


@router.post("/internal/admin/storage/location/activate-form")
async def activate_storage_location_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dstorage", status_code=303)
    try:
        activate_storage_profile(profile_id)
        return _storage_redirect("Storage-Profil aktiviert.", error=False)
    except Exception as exc:
        return _storage_redirect(str(exc), error=True)


@router.post("/internal/admin/storage/location/delete-form")
async def delete_storage_location_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dstorage", status_code=303)
    try:
        result = delete_storage_profile(profile_id)
        if result.get("deleted_was_active"):
            return _storage_redirect("Aktives Storage-Profil geloescht. Bitte ein neues Ziel waehlen.", error=False)
        return _storage_redirect("Storage-Profil geloescht.", error=False)
    except Exception as exc:
        return _storage_redirect(str(exc), error=True)


@router.post("/internal/admin/storage/upload-form")
async def upload_storage_document_form(
    request: Request,
    STORAGE_UPLOAD_PROFILE_ID: str = Form(default=""),
    DOCUMENT_TITLE: str = Form(default=""),
    DOCUMENT_TAGS: str = Form(default=""),
    DOCUMENT_FILE: UploadFile = File(...),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dstorage", status_code=303)
    settings = get_settings()
    try:
        document = await upload_document(
            settings=settings,
            file=DOCUMENT_FILE,
            storage_profile_id=STORAGE_UPLOAD_PROFILE_ID or None,
            title=DOCUMENT_TITLE,
            tags=DOCUMENT_TAGS,
        )
        title = str(document.get("title") or document.get("file_name") or "Dokument")
        return _storage_redirect(f"Dokument '{title}' gespeichert und extrahiert.", error=False)
    except Exception as exc:
        return _storage_redirect(str(exc), error=True)


@router.post("/internal/admin/device/save-form")
async def save_device_form(
    request: Request,
    DEVICE_PROFILE_ID: str = Form(default=""),
    DEVICE_PROFILE_NAME: str = Form(default=""),
    DEVICE_GATEWAY_BASE_URL: str = Form(default=""),
    DEVICE_TOKEN: str = Form(default=""),
    PI_SSH_HOST: str = Form(default=""),
    PI_SSH_USER: str = Form(default=""),
    PI_SSH_PORT: str = Form(default="22"),
    PI_SSH_PASSWORD: str = Form(default=""),
    PI_REMOTE_DIR: str = Form(default="~/kai-pi"),
    PI_SSH_ROOT_PREFIX: str = Form(default="sudo -n"),
    DEVICE_NOTES: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = save_device_profile(
            profile_id=DEVICE_PROFILE_ID or None,
            name=DEVICE_PROFILE_NAME,
            gateway_base_url=DEVICE_GATEWAY_BASE_URL,
            device_token=DEVICE_TOKEN,
            ssh_host=PI_SSH_HOST,
            ssh_user=PI_SSH_USER,
            ssh_port=PI_SSH_PORT,
            ssh_password=PI_SSH_PASSWORD,
            remote_dir=PI_REMOTE_DIR,
            ssh_root_prefix=PI_SSH_ROOT_PREFIX,
            notes=DEVICE_NOTES,
            make_active=False,
        )
        return _device_redirect(f"Device-Profil '{profile['name']}' gespeichert.", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/activate-form")
async def activate_device_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = activate_device_profile(profile_id)
        write_runtime_config({"DEVICE_SHARED_TOKEN": str(profile.get("device_token") or "")})
        return _device_redirect(f"Device-Profil '{profile['name']}' aktiviert und Device-Token uebernommen.", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/bootstrap-form")
async def bootstrap_device_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = get_device_profile(profile_id)
        write_runtime_config({"DEVICE_SHARED_TOKEN": str(profile.get("device_token") or "")})
        run_device_bootstrap_over_ssh(profile)
        activate_device_profile(profile_id)
        return _device_redirect(f"Pi-Bootstrap fuer '{profile['name']}' erfolgreich ueber SSH ausgefuehrt.", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/install-form")
async def install_device_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = get_device_profile(profile_id)
        write_runtime_config({"DEVICE_SHARED_TOKEN": str(profile.get("device_token") or "")})
        result = run_device_install_over_ssh(profile)
        activate_device_profile(profile_id)
        output = str(result.get("output") or "").strip()
        compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
        return _device_redirect(f"Pi-Installation fuer '{profile['name']}' erfolgreich: {compact or 'ok'}", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/connect-form")
async def connect_device_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = get_device_profile(profile_id)
        write_runtime_config({"DEVICE_SHARED_TOKEN": str(profile.get("device_token") or "")})
        result = run_device_env_sync_over_ssh(profile)
        activate_device_profile(profile_id)
        output = str(result.get("output") or "").strip()
        compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
        return _device_redirect(f"Kai-Pi '{profile['name']}' verbunden: {compact or 'ok'}", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/probe-form")
async def probe_device_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = get_device_profile(profile_id)
        result = run_device_probe_over_ssh(profile)
        output = str(result.get("output") or "").strip()
        compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
        return _device_redirect(f"Pi-Probe fuer '{profile['name']}': {compact or 'ok'}", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/face-apply-form")
async def apply_device_face_form(
    request: Request,
    profile_id: str = Form(...),
    FACE_STYLE_NAME: str = Form(default=""),
    FACE_STATE: str = Form(default="idle"),
    FACE_RENDER_MODE: str = Form(default="vector"),
    FACE_SPRITE_PACK: str = Form(default=""),
    FACE_VARIANT: str = Form(default="custom"),
    FACE_FACE_COLOR: str = Form(default="black"),
    FACE_EYE_SHAPE: str = Form(default="round"),
    FACE_EYE_SPACING: str = Form(default="normal"),
    FACE_IRIS_COLOR: str = Form(default="#59c7ff"),
    FACE_PUPILS: str = Form(default=""),
    FACE_IRIS: str = Form(default=""),
    FACE_MOUTH: str = Form(default=""),
    FACE_NOSE: str = Form(default=""),
    FACE_CHEEKS: str = Form(default=""),
    FACE_EARS: str = Form(default=""),
    FACE_EYEBROWS: str = Form(default=""),
    FACE_EYELIDS: str = Form(default=""),
    FACE_HAIR: str = Form(default=""),
    FACE_CLOSE_EYES: str = Form(default=""),
) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        profile = get_device_profile(profile_id)
        write_runtime_config({"DEVICE_SHARED_TOKEN": str(profile.get("device_token") or "")})
        activate_device_profile(profile_id)

        state_value = (FACE_STATE or "idle").strip().lower()
        if state_value not in {"idle", "listening", "thinking", "speaking", "happy", "sleepy", "error"}:
            state_value = "idle"
        render_mode = (FACE_RENDER_MODE or "vector").strip().lower()
        if render_mode not in {"vector", "sprite_pack"}:
            render_mode = "vector"
        sprite_pack = (FACE_SPRITE_PACK or "").strip()
        variant = (FACE_VARIANT or "custom").strip().upper()
        if variant not in {"CUSTOM", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "F13", "F14", "F15", "F16", "F17"}:
            variant = "CUSTOM"
        face_color = (FACE_FACE_COLOR or "black").strip().lower()
        if face_color not in {"black", "white"}:
            face_color = "black"
        eye_shape = (FACE_EYE_SHAPE or "round").strip().lower()
        if eye_shape not in {"round", "oval", "small"}:
            eye_shape = "round"
        eye_spacing = (FACE_EYE_SPACING or "normal").strip().lower()
        if eye_spacing not in {"normal", "far", "raised"}:
            eye_spacing = "normal"

        face_config = {
            "renderMode": render_mode,
            "spritePack": sprite_pack,
            "variant": variant,
            "faceColor": face_color,
            "eyeShape": eye_shape,
            "eyeSpacing": eye_spacing,
            "irisColor": (FACE_IRIS_COLOR or "#59c7ff").strip() or "#59c7ff",
            "pupils": _form_bool(FACE_PUPILS),
            "iris": _form_bool(FACE_IRIS),
            "mouth": _form_bool(FACE_MOUTH),
            "nose": _form_bool(FACE_NOSE),
            "cheeks": _form_bool(FACE_CHEEKS),
            "ears": _form_bool(FACE_EARS),
            "eyebrows": _form_bool(FACE_EYEBROWS),
            "eyelids": _form_bool(FACE_EYELIDS),
            "hair": _form_bool(FACE_HAIR),
            "closeEyes": _form_bool(FACE_CLOSE_EYES),
        }
        style_name = (FACE_STYLE_NAME or "").strip() or f"gateway_{state_value}"
        result = run_device_face_apply_over_ssh(
            profile,
            style_name=style_name,
            state=state_value,
            face_config=face_config,
        )
        output = str(result.get("output") or "").strip()
        compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
        return _device_redirect(f"Kai-Face auf '{profile['name']}' gesetzt: {compact or style_name}", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


@router.post("/internal/admin/device/delete-form")
async def delete_device_form(request: Request, profile_id: str = Form(...)) -> RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Ddevices", status_code=303)
    try:
        result = delete_device_profile(profile_id)
        if result.get("deleted_was_active"):
            write_runtime_config({"DEVICE_SHARED_TOKEN": ""})
            return _device_redirect("Aktives Device-Profil geloescht. Device-Token im Gateway wurde geleert.", error=False)
        return _device_redirect("Device-Profil geloescht.", error=False)
    except Exception as exc:
        return _device_redirect(str(exc), error=True)


def _database_redirect(message: str, error: bool) -> RedirectResponse:
    query = {"tab": "database", "db_message": message}
    if error:
        query["db_error"] = "1"
    return RedirectResponse(url=f"/internal/admin?{urlencode(query)}", status_code=303)


def _settings_redirect(message: str, error: bool) -> RedirectResponse:
    query = {"tab": "settings", "settings_message": message}
    if error:
        query["settings_error"] = "1"
    return RedirectResponse(url=f"/internal/admin?{urlencode(query)}", status_code=303)


def _storage_redirect(message: str, error: bool) -> RedirectResponse:
    query = {"tab": "storage", "storage_message": message}
    if error:
        query["storage_error"] = "1"
    return RedirectResponse(url=f"/internal/admin?{urlencode(query)}", status_code=303)


def _device_redirect(message: str, error: bool) -> RedirectResponse:
    query = {"tab": "devices", "device_message": message}
    if error:
        query["device_error"] = "1"
    return RedirectResponse(url=f"/internal/admin?{urlencode(query)}", status_code=303)


def _form_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


async def _run_admin_ops_tool(
    *,
    request: Request,
    auth_subject: str,
    target: str,
    command_name: str,
    source: str,
) -> dict[str, object]:
    settings = get_settings()
    normalized_target = (target or "").strip().lower()
    normalized_command = (command_name or "").strip().lower()
    result = await tool_orchestrator.execute_tool(
        settings=settings,
        actor=ActorContext(
            actor_id=auth_subject or "admin",
            role=ROLE_ADMIN,
            source=source,
        ),
        request_id=request.state.request_id,
        tool_name="gateway.ops",
        arguments={
            "target": normalized_target,
            "command": normalized_command,
        },
    )
    if normalized_target == "kai":
        request.state.backend_called = True
    return result if isinstance(result, dict) else {"result": result}


@router.get("/api/admin/ops/{target}/status")
async def ops_status(target: str, request: Request, auth_subject: str = Depends(require_admin_api_auth)) -> JSONResponse:
    try:
        if target not in {"gateway", "kai"}:
            raise HTTPException(status_code=404, detail="Unknown ops target.")
        result = await _run_admin_ops_tool(
            request=request,
            auth_subject=auth_subject,
            target=target,
            command_name="status",
            source="api.admin.ops_status",
        )
        return JSONResponse(result)
    except ToolExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/ops/{target}/logs")
async def ops_logs(target: str, request: Request, auth_subject: str = Depends(require_admin_api_auth)) -> JSONResponse:
    try:
        if target not in {"gateway", "kai"}:
            raise HTTPException(status_code=404, detail="Unknown ops target.")
        result = await _run_admin_ops_tool(
            request=request,
            auth_subject=auth_subject,
            target=target,
            command_name="logs",
            source="api.admin.ops_logs",
        )
        return JSONResponse(result)
    except ToolExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/ops/{target}/restart")
async def ops_restart(request: Request, target: str, auth_subject: str = Depends(require_admin_api_auth)) -> JSONResponse:
    try:
        if target not in {"gateway", "kai"}:
            raise HTTPException(status_code=404, detail="Unknown ops target.")
        result = await _run_admin_ops_tool(
            request=request,
            auth_subject=auth_subject,
            target=target,
            command_name="restart",
            source="api.admin.ops_restart",
        )
        return JSONResponse(result)
    except ToolExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/ops/{target}/run/{command_name}")
async def ops_run(
    request: Request,
    target: str,
    command_name: str,
    auth_subject: str = Depends(require_admin_api_auth),
) -> JSONResponse:
    try:
        result = await _run_admin_ops_tool(
            request=request,
            auth_subject=auth_subject,
            target=target,
            command_name=command_name,
            source="api.admin.ops_run",
        )
        return JSONResponse(result)
    except ToolExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/ops/catalog", dependencies=[Depends(require_admin_api_auth)])
async def get_ops_catalog() -> JSONResponse:
    return JSONResponse({"targets": ops_command_catalog()})


@router.get("/api/admin/mcp/tools", dependencies=[Depends(require_admin_api_auth)])
async def get_admin_mcp_tools() -> JSONResponse:
    custom_map = {item["name"]: item for item in list_custom_mcp_tools()}
    tools: list[dict[str, object]] = []
    for item in get_mcp_tools():
        name = str(item.get("name") or "")
        custom = custom_map.get(name)
        tool_row: dict[str, object] = {
            "name": name,
            "description": str(item.get("description") or ""),
            "input_schema": item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {},
            "output_schema": item.get("output_schema") if isinstance(item.get("output_schema"), dict) else {},
            "is_custom": bool(custom),
            "requires_admin": bool(item.get("requires_admin")),
        }
        if custom:
            tool_row["target"] = custom.get("target")
            tool_row["command"] = custom.get("command")
        tools.append(tool_row)
    return JSONResponse({"tools": tools})


@router.get("/api/admin/mcp/custom-tools", dependencies=[Depends(require_admin_api_auth)])
async def get_admin_custom_mcp_tools() -> JSONResponse:
    return JSONResponse(
        {
            "tools": list_custom_mcp_tools(),
            "ops_catalog": ops_command_catalog(),
            "reserved_tool_names": sorted(get_builtin_mcp_tool_names()),
        }
    )


@router.post("/api/admin/mcp/custom-tools", dependencies=[Depends(require_admin_api_auth)])
async def save_admin_custom_mcp_tool(payload: dict[str, str | None]) -> JSONResponse:
    name = str(payload.get("name") or "").strip().lower()
    if name in get_builtin_mcp_tool_names():
        raise HTTPException(status_code=400, detail="Name ist reserviert (builtin MCP-Tool).")

    try:
        saved = save_custom_mcp_tool(
            name=name,
            description=str(payload.get("description") or ""),
            target=str(payload.get("target") or ""),
            command=str(payload.get("command") or ""),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"saved": saved, "tools": list_custom_mcp_tools()})


@router.post("/api/admin/mcp/custom-tools/{tool_name}/delete", dependencies=[Depends(require_admin_api_auth)])
async def delete_admin_custom_mcp_tool(tool_name: str) -> JSONResponse:
    if tool_name.strip().lower() in get_builtin_mcp_tool_names():
        raise HTTPException(status_code=400, detail="Builtin MCP-Tools koennen nicht geloescht werden.")
    try:
        result = delete_custom_mcp_tool(tool_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"deleted": result, "tools": list_custom_mcp_tools()})


async def _build_initial_admin_data(base_url: str = "") -> dict[str, str]:
    settings = get_settings()
    config_values = _build_admin_config_values(settings)
    data = {
        "dashboard_status": "Dashboard-Daten vorgeladen.",
        "settings_status": "Settings vorgeladen.",
        "gateway_state": "ok",
        "gateway_info": "Gateway antwortet",
        "backend_state": "-",
        "backend_info": "-",
        "requests_value": str(metrics.snapshot().get("total_requests", "-")),
        "backend_calls_value": str(metrics.snapshot().get("backend_calls", "-")),
        "uptime_value": str(metrics.snapshot().get("uptime_seconds", "-")),
        "avg_request_value": str(metrics.snapshot().get("average_request_duration_ms", "-")),
        "gpu_temp_value": "-",
        "gpu_power_value": "-",
        "gpu_vram_value": "-",
        "header_cpu_usage_value": "-",
        "header_cpu_temp_value": "-",
        "header_gpu_usage_value": "-",
        "header_gpu_temp_value": "-",
        "header_gpu_power_value": "-",
        "header_gpu_vram_value": "-",
        "dashboard_public_model": settings.public_model_name,
        "dashboard_backend_model": settings.backend_model_name,
        "dashboard_backend_profile": "-",
        "dashboard_admin_mode": settings.admin_default_mode,
        "dashboard_backend_base_url": settings.llamacpp_base_url,
        "dashboard_context_window": str(settings.backend_context_window),
        "dashboard_response_reserve": str(settings.context_response_reserve),
        "dashboard_default_max_tokens": str(settings.default_max_tokens),
        "dashboard_routing_thresholds": f"{settings.routing_length_threshold} Zeichen / {settings.routing_history_threshold} Nachrichten",
        "dashboard_db_mode": "-",
        "dashboard_storage_active": "-",
        "dashboard_ha_summary": "-",
        "dashboard_mcp_skills_value": "0 / 0",
        "database_status": "Datenbankstatus wird geladen...",
        "db_store_mode": "-",
        "db_connected": "-",
        "db_schema_ready": "-",
        "db_sessions_count": "-",
        "db_messages_count": "-",
        "db_url_redacted": "-",
        "database_url_value": "",
        "database_profiles_html": _render_database_profiles_html(list_database_profiles(settings.database_url or "")),
        "memory_status": "Memory-Ueberblick wird geladen...",
        "memory_store_mode": "-",
        "memory_persistence": "-",
        "memory_sessions_count": "-",
        "memory_messages_count": "-",
        "memory_summaries_count": "-",
        "storage_status": "Speicherstatus wird geladen...",
        "storage_session_mode": "-",
        "storage_persistence": "-",
        "storage_target": "kein Storage-Profil",
        "storage_active_name": "-",
        "storage_profiles_count": "0",
        "storage_documents_count": "0",
        "storage_profiles_html": '<div class="muted">Noch keine Storage-Profile vorhanden.</div>',
        "storage_documents_html": '<div class="muted">Noch keine Dokumente vorhanden.</div>',
        "storage_upload_options_html": '<option value="">aktives Profil verwenden</option>',
        "ha_status": "Home-Assistant-Status wird geladen...",
        "ha_configured": "-",
        "ha_connected": "-",
        "ha_location": "-",
        "skills_status": "Skills/MCP-Verwaltung ist bereit.",
        "mcp_tools_count": "0",
        "mcp_custom_tools_count": "0",
        "mcp_custom_tools_html": '<div class="muted">Noch keine Custom-MCP-Tools gespeichert.</div>',
        "device_status": "Pi-/Device-Profilverwaltung ist bereit.",
        "device_profiles_html": '<div class="muted">Noch keine Device-Profile gespeichert.</div>',
        "device_profile_form_id": "",
        "device_profile_form_name": "",
        "device_profile_form_gateway_base_url": base_url or "http://127.0.0.1:8000",
        "device_profile_form_device_token": settings.device_shared_token or "",
        "device_profile_form_ssh_host": "",
        "device_profile_form_ssh_user": "pi",
        "device_profile_form_ssh_port": "22",
        "device_profile_form_ssh_password": "",
        "device_profile_form_remote_dir": "~/kai-pi",
        "device_profile_form_ssh_root_prefix": "sudo -n",
        "device_profile_form_notes": "",
        "device_active_token_redacted": _redact_device_token(settings.device_shared_token or ""),
        "device_bootstrap_preview": "Noch kein Device-Profil ausgewaehlt.",
        "device_face_profile_options_html": '<option value="">zuerst Device-Profil speichern</option>',
        "device_face_style_name": "gateway_idle",
        "device_face_state": "idle",
        "device_face_render_mode": "vector",
        "device_face_sprite_pack": "robot_v1",
        "device_face_variant": "custom",
        "device_face_face_color": "black",
        "device_face_eye_shape": "round",
        "device_face_eye_spacing": "normal",
        "device_face_iris_color": "#59c7ff",
        "backend_profiles_html": '<div class="muted">Noch keine KI-Profile gespeichert.</div>',
        "backend_profile_form_id": "",
        "backend_profile_form_name": "",
        "backend_profile_form_public_model_name": "",
        "backend_profile_form_backend_model_name": "",
        "backend_profile_form_base_url": "",
        "backend_profile_form_context_window": str(settings.backend_context_window),
        "backend_profile_form_response_reserve": str(settings.context_response_reserve),
        "backend_profile_form_default_max_tokens": str(settings.default_max_tokens),
        "backend_profile_form_ngl_layers": "",
        "backend_profile_form_service_name": "",
        "backend_profile_form_activate_command": "",
        "backend_profile_form_status_command": "",
        "backend_profile_form_logs_command": "",
        "backend_profile_preview": "Noch kein Profil ausgewaehlt.",
    }
    for key, value in config_values.items():
        data[f"cfg_{key}"] = str(value or "")
    active_backend_profile = get_active_backend_profile()
    if active_backend_profile:
        data["dashboard_backend_profile"] = str(active_backend_profile.get("name") or "-")
        data["backend_profile_form_id"] = str(active_backend_profile.get("id") or "")
        data["backend_profile_form_name"] = str(active_backend_profile.get("name") or "")
        data["backend_profile_form_public_model_name"] = str(active_backend_profile.get("public_model_name") or "")
        data["backend_profile_form_backend_model_name"] = str(active_backend_profile.get("backend_model_name") or "")
        data["backend_profile_form_base_url"] = str(active_backend_profile.get("base_url") or "")
        data["backend_profile_form_context_window"] = str(active_backend_profile.get("context_window") or settings.backend_context_window)
        data["backend_profile_form_response_reserve"] = str(active_backend_profile.get("response_reserve") or settings.context_response_reserve)
        data["backend_profile_form_default_max_tokens"] = str(active_backend_profile.get("default_max_tokens") or settings.default_max_tokens)
        data["backend_profile_form_ngl_layers"] = str(active_backend_profile.get("ngl_layers") or "")
        data["backend_profile_form_service_name"] = str(active_backend_profile.get("service_name") or "")
        data["backend_profile_form_activate_command"] = str(active_backend_profile.get("activate_command") or "")
        data["backend_profile_form_status_command"] = str(active_backend_profile.get("status_command") or "")
        data["backend_profile_form_logs_command"] = str(active_backend_profile.get("logs_command") or "")
        data["backend_profile_preview"] = _render_backend_profile_preview(active_backend_profile)
    data["backend_profiles_html"] = _render_backend_profiles_html(list_backend_profiles())

    active_device_profile = get_active_device_profile()
    if active_device_profile:
        data["device_profile_form_id"] = str(active_device_profile.get("id") or "")
        data["device_profile_form_name"] = str(active_device_profile.get("name") or "")
        data["device_profile_form_gateway_base_url"] = str(active_device_profile.get("gateway_base_url") or data["device_profile_form_gateway_base_url"])
        data["device_profile_form_device_token"] = str(active_device_profile.get("device_token") or "")
        data["device_profile_form_ssh_host"] = str(active_device_profile.get("ssh_host") or "")
        data["device_profile_form_ssh_user"] = str(active_device_profile.get("ssh_user") or "pi")
        data["device_profile_form_ssh_port"] = str(active_device_profile.get("ssh_port") or "22")
        data["device_profile_form_ssh_password"] = ""
        data["device_profile_form_remote_dir"] = str(active_device_profile.get("remote_dir") or "~/kai-pi")
        data["device_profile_form_ssh_root_prefix"] = str(active_device_profile.get("ssh_root_prefix") or "sudo -n")
        data["device_profile_form_notes"] = str(active_device_profile.get("notes") or "")
        data["device_active_token_redacted"] = _redact_device_token(str(active_device_profile.get("device_token") or settings.device_shared_token or ""))
        data["device_bootstrap_preview"] = build_device_install_script(active_device_profile)
        data["device_face_style_name"] = f"gateway_{str(active_device_profile.get('name') or 'kai')}"
    data["device_profiles_html"] = _render_device_profiles_html(list_device_profiles())
    data["device_face_profile_options_html"] = _render_device_profile_options_html(list_device_profiles(), str(active_device_profile.get("id") or "") if active_device_profile else "")
    data["mcp_custom_tools_html"] = _render_mcp_custom_tools_html(list_custom_mcp_tools())

    client = LlamaCppClient(settings)
    try:
        models_response, _latency_ms = await client.fetch_models()
        backend_ok = _backend_model_available(models_response, settings.backend_model_name)
        data["backend_state"] = "ok" if backend_ok else "error"
        data["backend_info"] = f"{settings.backend_model_name} @ {settings.llamacpp_base_url}"
        if not backend_ok:
            data["dashboard_status"] = "Backend erreichbar, aber Modell fehlt."
    except LlamaCppTimeoutError:
        data["backend_state"] = "timeout"
        data["backend_info"] = settings.llamacpp_base_url
        data["dashboard_status"] = "Backend-Check Timeout."
    except LlamaCppError as exc:
        data["backend_state"] = "error"
        data["backend_info"] = exc.message
        data["dashboard_status"] = "Backend-Check fehlgeschlagen."

    try:
        telemetry = gateway_system_telemetry()
        data["header_cpu_usage_value"] = _format_number(telemetry.get("cpu_usage_percent"), "%")
        data["header_cpu_temp_value"] = _format_number(telemetry.get("cpu_temp_c"), " C")
        data["header_gpu_usage_value"] = _format_number(telemetry.get("gpu_usage_percent"), "%")
        data["header_gpu_temp_value"] = _format_number(telemetry.get("temperature_c"), " C")
        data["header_gpu_power_value"] = _format_number(telemetry.get("power_w"), " W")
        data["gpu_temp_value"] = data["header_gpu_temp_value"]
        data["gpu_power_value"] = data["header_gpu_power_value"]
        if telemetry.get("vram_used_gib") is not None and telemetry.get("vram_total_gib") is not None:
            percent = telemetry.get("vram_percent")
            percent_text = f" ({percent}%)" if percent is not None else ""
            data["gpu_vram_value"] = f"{telemetry['vram_used_gib']} / {telemetry['vram_total_gib']} GiB{percent_text}"
            data["header_gpu_vram_value"] = f"{percent}%" if percent is not None else "-"
        elif telemetry.get("vram_percent") is not None:
            data["gpu_vram_value"] = f"{telemetry['vram_percent']}%"
            data["header_gpu_vram_value"] = data["gpu_vram_value"]
    except RuntimeError:
        pass

    try:
        db = await database_status(settings)
        data["database_status"] = str(db.get("message") or "Datenbankstatus geladen.")
        data["db_store_mode"] = str(db.get("store_mode") or "-")
        data["db_connected"] = "yes" if db.get("connected") else "no"
        data["db_schema_ready"] = "ready" if db.get("schema_ready") else "missing"
        data["db_sessions_count"] = str(db.get("sessions_count") if db.get("sessions_count") is not None else "-")
        data["db_messages_count"] = str(db.get("messages_count") if db.get("messages_count") is not None else "-")
        data["db_url_redacted"] = str(db.get("database_url_redacted") or "-")
        data["dashboard_db_mode"] = f"{db.get('store_mode') or '-'} / {'verbunden' if db.get('connected') else 'offline'}"
        data["storage_status"] = (
            "PostgreSQL ist der aktive Metadaten- und Session-Speicher. Dokumentdateien legst du zusaetzlich in Storage-Profilen ab."
            if db.get("store_mode") == "postgres" and db.get("connected")
            else "Aktuell nur RAM-Store. Nach Neustart gehen Sessions verloren."
        )
        data["storage_session_mode"] = str(db.get("store_mode") or "-")
        data["storage_persistence"] = "persistent" if db.get("store_mode") == "postgres" and db.get("connected") else "volatile"
    except Exception:
        pass

    try:
        storage = await storage_overview(settings)
        active_storage = storage.get("active_profile") or {}
        data["storage_active_name"] = str(active_storage.get("name") or "-")
        data["storage_profiles_count"] = str(storage.get("profiles_count") or 0)
        data["storage_documents_count"] = str(storage.get("documents_count") or 0)
        data["storage_profiles_html"] = _render_storage_profiles_html(storage.get("profiles") or [])
        data["storage_documents_html"] = _render_storage_documents_html(storage.get("documents") or [])
        data["storage_upload_options_html"] = _render_storage_profile_options_html(storage.get("profiles") or [])
        if active_storage:
            data["storage_status"] = f"Aktives Storage-Ziel: {active_storage.get('name')} ({active_storage.get('backend_type')})."
            data["storage_target"] = str(active_storage.get("base_path") or data["storage_target"])
            data["dashboard_storage_active"] = str(active_storage.get("name") or "-")
        if storage.get("documents_error"):
            data["storage_status"] = str(storage["documents_error"])
    except Exception as exc:
        data["storage_status"] = str(exc)

    try:
        memory_store = get_session_store(settings)
        memory_stats = await memory_store.get_memory_stats()
        data["memory_status"] = "Persistenter Memory aktiv." if memory_stats.get("persistent") else "Aktuell nur RAM-Memory aktiv."
        data["memory_store_mode"] = str(memory_stats.get("store_mode") or "-")
        data["memory_persistence"] = "persistent" if memory_stats.get("persistent") else "volatile"
        data["memory_sessions_count"] = str(memory_stats.get("sessions_count") or 0)
        data["memory_messages_count"] = str(memory_stats.get("messages_count") or 0)
        data["memory_summaries_count"] = str(memory_stats.get("summaries_count") or 0)
    except Exception:
        pass

    try:
        ha = await HomeAssistantClient(settings).status()
        data["ha_status"] = str(ha.get("message") or "Home Assistant bereit.")
        data["ha_configured"] = "yes"
        data["ha_connected"] = "yes"
        data["ha_location"] = str(ha.get("location_name") or "-")
    except HomeAssistantConfigError as exc:
        data["ha_status"] = str(exc)
        data["ha_configured"] = "no"
        data["ha_connected"] = "no"
    except HomeAssistantRequestError as exc:
        data["ha_status"] = exc.message
        data["ha_configured"] = "yes"
        data["ha_connected"] = "no"
    data["dashboard_ha_summary"] = f"{data['ha_configured']} / {data['ha_connected']}"
    mcp_tools_count = len(get_mcp_tools())
    skills_count = _count_installed_skills()
    data["mcp_tools_count"] = str(mcp_tools_count)
    data["mcp_custom_tools_count"] = str(len(list_custom_mcp_tools()))
    data["dashboard_mcp_skills_value"] = f"{mcp_tools_count} / {skills_count}"

    return data


def _format_number(value, suffix: str) -> str:
    if value is None:
        return "-"
    return f"{value}{suffix}"


def _backend_model_available(models_response: dict, expected_model: str) -> bool:
    data = models_response.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id") == expected_model:
                return True

    models = models_response.get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("model") == expected_model:
                return True
            if isinstance(item, dict) and item.get("name") == expected_model:
                return True
    return False


def _count_installed_skills() -> int:
    # The admin dashboard should display a stable, read-only count of currently
    # installed skills without requiring shell commands.
    skill_roots = [
        Path("/root/.codex/skills"),
        Path("~/.codex/skills").expanduser(),
        Path("/opt/llm-gateway/.codex/skills"),
    ]
    found: set[str] = set()
    for root in skill_roots:
        try:
            if not root.exists() or not root.is_dir():
                continue
            for skill_file in root.rglob("SKILL.md"):
                found.add(str(skill_file.parent))
        except OSError:
            continue
    return len(found)


def _render_database_profiles_html(profiles: list[dict[str, object]]) -> str:
    if not profiles:
        return '<div class="muted">Noch keine gespeicherten Datenbank-Profile.</div>'

    items: list[str] = []
    for profile in profiles:
        profile_id = escape(str(profile.get("id") or ""))
        name = escape(str(profile.get("name") or "DB-Profil"))
        redacted = escape(str(profile.get("database_url_redacted") or "-"))
        badge = '<span class="status" style="display:inline-block;padding:4px 8px;margin:0 0 0 8px;">aktiv</span>' if profile.get("is_active") else ""
        actions = []
        if not profile.get("is_active") and not profile.get("is_ephemeral"):
            actions.append(
                f"""
                <form method="post" action="/internal/admin/database/activate-form">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Aktivieren</button>
                </form>
                """
            )
        if not profile.get("is_ephemeral"):
            actions.append(
                f"""
                <form method="post" action="/internal/admin/database/delete-form" onsubmit="return confirm('Profil wirklich loeschen?');">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Loeschen</button>
                </form>
                """
            )
        items.append(
            f"""
            <div class="list-card">
              <h3>{name}{badge}</h3>
              <div class="list-meta"><code>{redacted}</code></div>
              <div class="actions">{''.join(actions)}</div>
            </div>
            """
        )
    return "".join(items)


def _render_backend_profiles_html(profiles: list[dict[str, object]]) -> str:
    if not profiles:
        return '<div class="muted">Noch keine KI-Profile gespeichert.</div>'

    items: list[str] = []
    for profile in profiles:
        profile_id = escape(str(profile.get("id") or ""))
        name = escape(str(profile.get("name") or "backend-profile"))
        public_model = escape(str(profile.get("public_model_name") or "-"))
        backend_model = escape(str(profile.get("backend_model_name") or "-"))
        base_url = escape(str(profile.get("base_url") or "-"))
        context_window = escape(str(profile.get("context_window") or "-"))
        response_reserve = escape(str(profile.get("response_reserve") or "-"))
        default_max_tokens = escape(str(profile.get("default_max_tokens") or "-"))
        ngl_layers = escape(str(profile.get("ngl_layers") or "-"))
        service_name = escape(str(profile.get("service_name") or "-"))
        activate_command = escape(str(profile.get("activate_command") or "-"))
        status_command = escape(str(profile.get("status_command") or "-"))
        logs_command = escape(str(profile.get("logs_command") or "-"))
        badge = '<span class="status" style="display:inline-block;padding:4px 8px;margin:0 0 0 8px;">aktiv</span>' if profile.get("is_active") else ""
        actions = []
        if not profile.get("is_active"):
            actions.append(
                f"""
                <form method="post" action="/internal/admin/backend-profile/activate-form">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Aktivieren</button>
                </form>
                """
            )
        else:
            actions.append(
                f"""
                <form method="post" action="/internal/admin/backend-profile/activate-form">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Reset</button>
                </form>
                """
            )
            actions.append(
                f"""
                <form method="post" action="/internal/admin/backend-profile/deactivate-form" onsubmit="return confirm('Aktives Backend-Profil wirklich deaktivieren und den MI50-Service stoppen?');">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Deaktivieren</button>
                </form>
                """
            )
        actions.append(
            f"""
            <a class="secondary" href="/internal/admin?tab=settings&edit_profile={profile_id}">Bearbeiten</a>
            """
        )
        actions.append(
            f"""
            <form method="post" action="/internal/admin/backend-profile/delete-form" onsubmit="return confirm('Backend-Profil wirklich loeschen?');">
              <input type="hidden" name="profile_id" value="{profile_id}">
              <button class="secondary" type="submit">Loeschen</button>
            </form>
            """
        )
        items.append(
            f"""
            <div class="list-card">
              <h3>{name}{badge}</h3>
              <div class="list-meta">Public: <code>{public_model}</code> | Backend: <code>{backend_model}</code></div>
              <div class="list-meta">Base URL: <code>{base_url}</code> | MI50-Service: <code>{service_name}</code></div>
              <div class="list-meta">Kontext: <code>{context_window}</code> | Reserve: <code>{response_reserve}</code> | Default max: <code>{default_max_tokens}</code> | NGL: <code>{ngl_layers}</code></div>
              <div class="list-meta">Aktivierung: <code>{activate_command}</code></div>
              <div class="list-meta">Status: <code>{status_command}</code> | Logs: <code>{logs_command}</code></div>
              <div class="actions">{''.join(actions)}</div>
            </div>
            """
        )
    return "".join(items)


def _render_backend_profile_preview(profile: dict[str, object]) -> str:
    name = str(profile.get("name") or "")
    public_model = str(profile.get("public_model_name") or "")
    backend_model = str(profile.get("backend_model_name") or "")
    base_url = str(profile.get("base_url") or "")
    context_window = str(profile.get("context_window") or "")
    response_reserve = str(profile.get("response_reserve") or "")
    default_max_tokens = str(profile.get("default_max_tokens") or "")
    ngl_layers = str(profile.get("ngl_layers") or "")
    service_name = str(profile.get("service_name") or "")
    activate_command = str(profile.get("activate_command") or "")
    status_command = str(profile.get("status_command") or "")
    logs_command = str(profile.get("logs_command") or "")
    return dedent(
        f"""\
        # KI-Profil
        name={name}
        public_model_name={public_model}
        backend_model_name={backend_model}
        base_url={base_url}
        context_window={context_window}
        response_reserve={response_reserve}
        default_max_tokens={default_max_tokens}
        ngl_layers={ngl_layers}
        service_name={service_name}
        activate_command={activate_command}
        status_command={status_command}
        logs_command={logs_command}
        """
    ).strip()


def _render_mcp_custom_tools_html(tools: list[dict[str, object]]) -> str:
    if not tools:
        return '<div class="muted">Noch keine Custom-MCP-Tools gespeichert.</div>'

    items: list[str] = []
    for tool in tools:
        name = escape(str(tool.get("name") or "custom.tool"))
        description = escape(str(tool.get("description") or "-"))
        target = escape(str(tool.get("target") or "gateway"))
        command = escape(str(tool.get("command") or "status"))
        name_js = str(tool.get("name") or "").replace("\\", "\\\\").replace("'", "\\'")
        items.append(
            f"""
            <div class="list-card">
              <h3>{name}</h3>
              <div class="list-meta">{description}</div>
              <div class="list-meta">Ops: <code>{target}.{command}</code></div>
              <div class="actions">
                <button class="secondary" type="button" onclick="deleteCustomMcpTool('{name_js}')">Loeschen</button>
              </div>
            </div>
            """
        )
    return "".join(items)


def _render_storage_profiles_html(profiles: list[dict[str, object]]) -> str:
    if not profiles:
        return '<div class="muted">Noch keine Storage-Profile vorhanden.</div>'

    items: list[str] = []
    for profile in profiles:
        profile_id = escape(str(profile.get("id") or ""))
        name = escape(str(profile.get("name") or "Storage"))
        backend_type = escape(str(profile.get("backend_type") or "local"))
        base_path = escape(str(profile.get("base_path") or "-"))
        badge = '<span class="status" style="display:inline-block;padding:4px 8px;margin:0 0 0 8px;">aktiv</span>' if profile.get("is_active") else ""
        actions = []
        if not profile.get("is_active"):
            actions.append(
                f"""
                <form method="post" action="/internal/admin/storage/location/activate-form">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Aktivieren</button>
                </form>
                """
            )
        actions.append(
            f"""
            <form method="post" action="/internal/admin/storage/location/delete-form" onsubmit="return confirm('Storage-Profil wirklich loeschen?');">
              <input type="hidden" name="profile_id" value="{profile_id}">
              <button class="secondary" type="submit">Loeschen</button>
            </form>
            """
        )
        items.append(
            f"""
            <div class="list-card">
              <h3>{name}{badge}</h3>
              <div class="list-meta">Typ: {backend_type} | Pfad: <code>{base_path}</code></div>
              <div class="list-meta">Hinweis: SMB wird hier als bereits gemounteter Pfad behandelt, nicht als direkter SMB-Login im Gateway.</div>
              <div class="actions">{''.join(actions)}</div>
            </div>
            """
        )
    return "".join(items)


def _render_storage_documents_html(documents: list[dict[str, object]]) -> str:
    if not documents:
        return '<div class="muted">Noch keine Dokumente gespeichert.</div>'

    items: list[str] = []
    for document in documents:
        title = escape(str(document.get("title") or document.get("file_name") or "Dokument"))
        file_name = escape(str(document.get("file_name") or "-"))
        location_name = escape(str(document.get("storage_location_name") or "-"))
        media_type = escape(str(document.get("media_type") or "-"))
        size_bytes = int(document.get("size_bytes") or 0)
        size_text = f"{round(size_bytes / 1024, 1)} KB" if size_bytes < 1024 * 1024 else f"{round(size_bytes / (1024 * 1024), 2)} MB"
        created_at = escape(str(document.get("created_at") or "-"))
        excerpt = escape(str(document.get("text_excerpt") or "Kein Textauszug verfuegbar."))
        tags = escape(str(document.get("tags") or "-"))
        items.append(
            f"""
            <div class="list-card">
              <h3>{title}</h3>
              <div class="list-meta">Datei: {file_name} | Storage: {location_name} | Typ: {media_type} | Groesse: {size_text}</div>
              <div class="list-meta">Tags: {tags} | Erfasst: {created_at}</div>
              <pre>{excerpt}</pre>
            </div>
            """
        )
    return "".join(items)


def _render_storage_profile_options_html(profiles: list[dict[str, object]]) -> str:
    options = ['<option value="">aktives Profil verwenden</option>']
    for profile in profiles:
        profile_id = escape(str(profile.get("id") or ""))
        label = escape(str(profile.get("name") or "Storage"))
        if profile.get("is_active"):
            label = f"{label} (aktiv)"
        options.append(f'<option value="{profile_id}">{label}</option>')
    return "".join(options)


def _redact_device_token(token: str) -> str:
    clean_token = (token or "").strip()
    if not clean_token:
        return "-"
    if len(clean_token) <= 8:
        return "*" * len(clean_token)
    return f"{clean_token[:4]}***{clean_token[-4:]}"


def _render_device_profiles_html(profiles: list[dict[str, object]]) -> str:
    if not profiles:
        return '<div class="muted">Noch keine Device-Profile gespeichert.</div>'

    items: list[str] = []
    for profile in profiles:
        profile_id = escape(str(profile.get("id") or ""))
        name = escape(str(profile.get("name") or "Pi Device"))
        gateway_base_url = escape(str(profile.get("gateway_base_url") or "-"))
        raw_ssh_host = str(profile.get("ssh_host") or "").strip()
        raw_ssh_user = str(profile.get("ssh_user") or "").strip()
        ssh_host = escape(raw_ssh_host or "-")
        ssh_user = escape(raw_ssh_user or "-")
        ssh_port = escape(str(profile.get("ssh_port") or "22"))
        remote_dir = escape(str(profile.get("remote_dir") or "~/kai-pi"))
        token_redacted = escape(str(profile.get("device_token_redacted") or "-"))
        ssh_auth_mode = escape(str(profile.get("ssh_auth_mode") or "key"))
        bootstrap_ready = bool(raw_ssh_host and raw_ssh_user)
        badge = '<span class="status" style="display:inline-block;padding:4px 8px;margin:0 0 0 8px;">aktiv</span>' if profile.get("is_active") else ""
        actions = []
        if not profile.get("is_active"):
            actions.append(
                f"""
                <form method="post" action="/internal/admin/device/activate-form">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Aktivieren</button>
                </form>
                """
            )
        actions.append(f'<a class="secondary" href="/internal/admin?tab=devices&edit_device={profile_id}">Bearbeiten</a>')
        if bootstrap_ready:
            actions.append(
                f"""
                <form method="post" action="/internal/admin/device/connect-form" onsubmit="return confirm('Gateway-URL und Device-Token jetzt per SSH auf den Kai-Pi schreiben und kai.service neu starten?');">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="primary" type="submit">Verbinden / .env sync</button>
                </form>
                """
            )
        if bootstrap_ready:
            actions.append(
                f"""
                <form method="post" action="/internal/admin/device/probe-form">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="secondary" type="submit">Pruefen</button>
                </form>
                """
            )
        if bootstrap_ready:
            actions.append(
                f"""
                <form method="post" action="/internal/admin/device/install-form" onsubmit="return confirm('Roher Pi fuer dieses Profil jetzt ueber SSH installieren?');">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  <button class="primary" type="submit">PI installieren</button>
                </form>
                """
            )
        actions.append(
            f"""
            <form method="post" action="/internal/admin/device/delete-form" onsubmit="return confirm('Device-Profil wirklich loeschen?');">
              <input type="hidden" name="profile_id" value="{profile_id}">
              <button class="secondary" type="submit">Loeschen</button>
            </form>
            """
        )
        items.append(
            f"""
            <div class="list-card">
              <h3>{name}{badge}</h3>
              <div class="list-meta">Gateway: <code>{gateway_base_url}</code> | Token: <code>{token_redacted}</code></div>
              <div class="list-meta">SSH: <code>{ssh_user}@{ssh_host}:{ssh_port}</code> | Auth: <code>{ssh_auth_mode}</code> | Ziel: <code>{remote_dir}</code>{'' if bootstrap_ready else ' | nur Direktverbindung, kein Bootstrap'}</div>
              <div class="actions">{''.join(actions)}</div>
            </div>
            """
        )
    return "".join(items)


def _render_device_profile_options_html(profiles: list[dict[str, object]], active_profile_id: str) -> str:
    if not profiles:
        return '<option value="">zuerst Device-Profil speichern</option>'
    options: list[str] = []
    for profile in profiles:
        profile_id = str(profile.get("id") or "")
        name = str(profile.get("name") or "Pi Device")
        active_badge = " (aktiv)" if profile.get("is_active") else ""
        selected = " selected" if profile_id and profile_id == active_profile_id else ""
        options.append(f'<option value="{escape(profile_id)}"{selected}>{escape(name + active_badge)}</option>')
    return "".join(options)


def _admin_html(username: str, active_tab: str, initial_data: dict[str, str]) -> str:
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
              display:grid;
              gap:12px;
            }
            .nav-main {
              display:flex;
              gap:14px;
              align-items:center;
              justify-content:space-between;
              flex-wrap:wrap;
            }
            .brand-stack {
              display:flex;
              flex-direction:column;
              gap:8px;
              min-width:260px;
            }
            .brand {
              font-size:1.05rem;
              font-weight:700;
              letter-spacing:.14em;
              text-transform:uppercase;
              color:var(--accent);
            }
            .header-telemetry {
              display:flex;
              gap:8px;
              flex-wrap:wrap;
            }
            .telemetry-chip {
              min-width:72px;
              padding:5px 8px 6px;
              border:1px solid var(--line);
              border-radius:8px;
              background:#10161f;
            }
            .chip-label {
              display:block;
              color:var(--muted);
              font-size:.58rem;
              letter-spacing:.10em;
              text-transform:uppercase;
              line-height:1.1;
            }
            .chip-value {
              display:block;
              margin-top:3px;
              color:var(--accent);
              font-size:.86rem;
              font-weight:700;
              line-height:1.05;
            }
            .nav-buttons { display:flex; gap:10px; flex-wrap:wrap; }
            .nav-buttons a, .nav-buttons button, .nav form button, button, select, input, textarea {
              font:inherit;
            }
            .nav-buttons a, .nav-buttons button, .nav form button, button.primary, button.secondary {
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 14px;
              cursor:pointer;
              text-transform:uppercase;
              letter-spacing:.06em;
              text-decoration:none;
            }
            .nav-buttons a, .nav-buttons button, .nav form button, button.secondary {
              background:var(--chrome);
              color:var(--ink);
            }
            .nav-buttons a.active, .nav-buttons button.active, button.primary {
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
              pointer-events:none;
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
            .actions a {
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 14px;
              text-decoration:none;
              text-transform:uppercase;
              letter-spacing:.06em;
              background:var(--chrome);
              color:var(--ink);
            }
            .list-stack {
              display:grid;
              gap:12px;
              margin-top:12px;
            }
            .list-card {
              border:1px solid var(--line);
              border-radius:8px;
              background:#0d131a;
              padding:12px;
            }
            .list-card h3 {
              margin:0 0 6px;
              font-size:1rem;
            }
            .list-meta {
              color:var(--muted);
              font-size:.9rem;
              margin-bottom:8px;
              line-height:1.45;
            }
            .list-card pre {
              margin-top:8px;
              padding:10px;
              border:1px solid var(--line);
              border-radius:8px;
              background:#090d12;
              color:#b8c8d8;
            }
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
              .nav-main { flex-direction:column; align-items:flex-start; }
            }
          </style>
        </head>
        <body>
          <header>
            <div class="nav">
              <div class="nav-main">
                <div class="brand-stack">
                  <div class="brand">llm-gateway Admin Hub</div>
                  <div class="header-telemetry">
                    <div class="telemetry-chip"><span class="chip-label">CPU</span><span id="headerCpuUsageValue" class="chip-value">__HEADER_CPU_USAGE_VALUE__</span></div>
                    <div class="telemetry-chip"><span class="chip-label">CPU Temp</span><span id="headerCpuTempValue" class="chip-value">__HEADER_CPU_TEMP_VALUE__</span></div>
                    <div class="telemetry-chip"><span class="chip-label">GPU Use</span><span id="headerGpuUsageValue" class="chip-value">__HEADER_GPU_USAGE_VALUE__</span></div>
                    <div class="telemetry-chip"><span class="chip-label">GPU Temp</span><span id="headerGpuTempValue" class="chip-value">__HEADER_GPU_TEMP_VALUE__</span></div>
                    <div class="telemetry-chip"><span class="chip-label">GPU Pwr</span><span id="headerGpuPowerValue" class="chip-value">__HEADER_GPU_POWER_VALUE__</span></div>
                    <div class="telemetry-chip"><span class="chip-label">VRAM</span><span id="headerGpuVramValue" class="chip-value">__HEADER_GPU_VRAM_VALUE__</span></div>
                  </div>
                </div>
                <div class="nav-buttons">
                  <a class="__NAV_DASHBOARD__" href="/internal/admin?tab=dashboard">Dashboard</a>
                  <a class="__NAV_SETTINGS__" href="/internal/admin?tab=settings">Settings</a>
                  <a class="__NAV_SKILLS__" href="/internal/admin?tab=skills">Skills / MCP</a>
                  <a class="__NAV_CHAT__" href="/internal/admin?tab=chat">Chat</a>
                  <a class="__NAV_MEMORY__" href="/internal/admin?tab=memory">Memory</a>
                  <a class="__NAV_DATABASE__" href="/internal/admin?tab=database">Database</a>
                  <a class="__NAV_HOME_ASSISTANT__" href="/internal/admin?tab=home-assistant">Home Assistant</a>
                  <a class="__NAV_STORAGE__" href="/internal/admin?tab=storage">Storage</a>
                  <a class="__NAV_OPS__" href="/internal/admin?tab=ops">Ops</a>
                  <a class="__NAV_DEVICES__" href="/internal/admin?tab=devices">Pi / Devices</a>
                </div>
                <div class="userbox">
                  <span>eingeloggt als __USERNAME__</span>
                  <form method="post" action="/admin/logout"><button type="submit">Logout</button></form>
                </div>
              </div>
            </div>
          </header>
          <main>
            <section id="dashboard" class="panel __PANEL_DASHBOARD__">
              <div class="hero">
                <h1>Betriebsueberblick</h1>
                <p>Hier siehst du nur den laufenden Zustand der Plattform: Gateway, Kai, GPU, Requests, aktive Modelle, Datenbank und Storage. Konfiguration liegt im neuen Settings-Tab.</p>
                <div id="dashboardStatus" class="status">__DASHBOARD_STATUS__</div>
              </div>
              <div class="grid">
                <div class="card stat">
                  <div class="muted">Gateway</div>
                  <strong id="gatewayState">__GATEWAY_STATE__</strong>
                  <div id="gatewayInfo" class="muted">__GATEWAY_INFO__</div>
                </div>
                <div class="card stat">
                  <div class="muted">Kai / MI50</div>
                  <strong id="backendState">__BACKEND_STATE__</strong>
                  <div id="backendInfo" class="muted">__BACKEND_INFO__</div>
                </div>
                <div class="card stat">
                  <div class="muted">Requests</div>
                  <strong id="requestsValue">__REQUESTS_VALUE__</strong>
                  <div class="muted">seit Prozessstart</div>
                </div>
                <div class="card stat">
                  <div class="muted">Backend Calls</div>
                  <strong id="backendCallsValue">__BACKEND_CALLS_VALUE__</strong>
                  <div class="muted">inkl. Health Checks</div>
                </div>
                <div class="card stat">
                  <div class="muted">Uptime</div>
                  <strong id="uptimeValue">__UPTIME_VALUE__</strong>
                  <div class="muted">seit Prozessstart</div>
                </div>
                <div class="card stat">
                  <div class="muted">Avg Request</div>
                  <strong id="avgRequestValue">__AVG_REQUEST_VALUE__</strong>
                  <div class="muted">mittlere Dauer</div>
                </div>
                <div class="card stat">
                  <div class="muted">GPU Temp</div>
                  <strong id="gpuTempValue">__GPU_TEMP_VALUE__</strong>
                  <div class="muted">MI50 edge temp</div>
                </div>
                <div class="card stat">
                  <div class="muted">GPU Power</div>
                  <strong id="gpuPowerValue">__GPU_POWER_VALUE__</strong>
                  <div class="muted">rocm-smi watt</div>
                </div>
                <div class="card stat">
                  <div class="muted">VRAM</div>
                  <strong id="gpuVramValue">__GPU_VRAM_VALUE__</strong>
                  <div class="muted">belegt / gesamt</div>
                </div>
                <div class="card stat">
                  <div class="muted">MCP / Skills aktiv</div>
                  <strong id="dashboardMcpSkillsValue">__DASHBOARD_MCP_SKILLS_VALUE__</strong>
                  <div class="muted">MCP-Tools / Skills</div>
                </div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>Aktiver Plattform-Stack</h2>
                  <div class="list-stack">
                    <div class="list-card">
                      <h3>Modelle und Routing</h3>
                      <div class="list-meta">Aktives KI-Profil: <code id="dashboardBackendProfile">__DASHBOARD_BACKEND_PROFILE__</code></div>
                      <div class="list-meta">Public Model: <code id="dashboardPublicModel">__DASHBOARD_PUBLIC_MODEL__</code></div>
                      <div class="list-meta">Backend Model: <code id="dashboardBackendModel">__DASHBOARD_BACKEND_MODEL__</code></div>
                      <div class="list-meta">Admin Default Mode: <code id="dashboardAdminMode">__DASHBOARD_ADMIN_MODE__</code></div>
                      <div class="list-meta">Routing Schwellwerte: <code id="dashboardRoutingThresholds">__DASHBOARD_ROUTING_THRESHOLDS__</code></div>
                    </div>
                    <div class="list-card">
                      <h3>Kontext und Limits</h3>
                      <div class="list-meta">Backend URL: <code id="dashboardBackendBaseUrl">__DASHBOARD_BACKEND_BASE_URL__</code></div>
                      <div class="list-meta">Context Window: <code id="dashboardContextWindow">__DASHBOARD_CONTEXT_WINDOW__</code></div>
                      <div class="list-meta">Response Reserve: <code id="dashboardResponseReserve">__DASHBOARD_RESPONSE_RESERVE__</code></div>
                      <div class="list-meta">Default max_tokens: <code id="dashboardDefaultMaxTokens">__DASHBOARD_DEFAULT_MAX_TOKENS__</code></div>
                    </div>
                    <div class="list-card">
                      <h3>Persistenz und Integrationen</h3>
                      <div class="list-meta">Database / Memory: <code id="dashboardDbMode">__DASHBOARD_DB_MODE__</code></div>
                      <div class="list-meta">Storage aktiv: <code id="dashboardStorageActive">__DASHBOARD_STORAGE_ACTIVE__</code></div>
                      <div class="list-meta">Home Assistant: <code id="dashboardHaSummary">__DASHBOARD_HA_SUMMARY__</code></div>
                    </div>
                  </div>
                </div>
                <div class="card">
                  <h2>Schnellzugriff</h2>
                  <p class="muted">Das Dashboard soll dich direkt zu den produktiven Bereichen bringen, nicht mit Konfig-Feldern erschlagen.</p>
                  <div class="actions">
                    <a class="secondary" href="/internal/admin?tab=chat">Zum Chat</a>
                    <a class="secondary" href="/internal/admin?tab=memory">Memory</a>
                    <a class="secondary" href="/internal/admin?tab=storage">Storage</a>
                    <a class="secondary" href="/internal/admin?tab=database">Database</a>
                    <a class="secondary" href="/internal/admin?tab=ops">Ops</a>
                    <a class="secondary" href="/internal/admin?tab=settings">Settings</a>
                  </div>
                  <div class="list-stack" style="margin-top:16px;">
                    <div class="list-card">
                      <h3>Wofuer das Dashboard jetzt da ist</h3>
                      <div class="list-meta">Betriebsueberblick, GPU-Telemetrie, Modellzustand, Request-Last und schneller Sprung in die Arbeitsbereiche.</div>
                    </div>
                    <div class="list-card">
                      <h3>Was bewusst ausgelagert wurde</h3>
                      <div class="list-meta">Gateway-/Routing-Settings, Continue YAML und technische Grundkonfiguration liegen jetzt im eigenen Settings-Tab.</div>
                    </div>
                  </div>
                </div>
              </div>
            </section>

            <section id="settings" class="panel __PANEL_SETTINGS__">
              <div class="hero">
                <h1>Settings / Gateway</h1>
                <p>Hier liegen die schreibbaren Plattform-Settings. Ganz oben stehen bewusst die KI-Profile, weil du darueber im Alltag Modell, Service und Umschaltung steuerst. Rohere Gateway-Felder und Legacy-Kompatibilitaet sind darunter kompakter einsortiert.</p>
                <div id="settingsStatus" class="status">__SETTINGS_STATUS__</div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>KI-Profile / MI50-Services</h2>
                  <form method="post" action="/internal/admin/backend-profile/save-form">
                    <input type="hidden" name="BACKEND_PROFILE_ID" value="__BACKEND_PROFILE_FORM_ID__">
                    <label><span>BACKEND_PROFILE_NAME</span><input name="BACKEND_PROFILE_NAME" value="__BACKEND_PROFILE_FORM_NAME__" placeholder="z. B. devstral, qwen-coder"></label>
                    <label><span>PROFILE_PUBLIC_MODEL_NAME</span><input name="PROFILE_PUBLIC_MODEL_NAME" value="__BACKEND_PROFILE_FORM_PUBLIC_MODEL_NAME__" placeholder="z. B. devstral-q3 oder qwen-coder"></label>
                    <label><span>PROFILE_BACKEND_MODEL_NAME</span><input name="PROFILE_BACKEND_MODEL_NAME" value="__BACKEND_PROFILE_FORM_BACKEND_MODEL_NAME__" placeholder="z. B. Devstral-Small-2-24B-Instruct-2512-Q3_K_M.gguf"></label>
                    <label><span>PROFILE_BASE_URL</span><input name="PROFILE_BASE_URL" value="__BACKEND_PROFILE_FORM_BASE_URL__" placeholder="z. B. http://192.168.40.111:8080"></label>
                    <label><span>PROFILE_CONTEXT_WINDOW</span><input name="PROFILE_CONTEXT_WINDOW" value="__BACKEND_PROFILE_FORM_CONTEXT_WINDOW__" placeholder="z. B. 8192 oder 16384"></label>
                    <label><span>PROFILE_RESPONSE_RESERVE</span><input name="PROFILE_RESPONSE_RESERVE" value="__BACKEND_PROFILE_FORM_RESPONSE_RESERVE__" placeholder="z. B. 2048"></label>
                    <label><span>PROFILE_DEFAULT_MAX_TOKENS</span><input name="PROFILE_DEFAULT_MAX_TOKENS" value="__BACKEND_PROFILE_FORM_DEFAULT_MAX_TOKENS__" placeholder="z. B. 1024"></label>
                    <label><span>PROFILE_MI50_NGL optional</span><input name="PROFILE_MI50_NGL" value="__BACKEND_PROFILE_FORM_NGL_LAYERS__" placeholder="z. B. 0, 20 oder 40"></label>
                    <label><span>PROFILE_MI50_SERVICE_NAME</span><input name="PROFILE_MI50_SERVICE_NAME" value="__BACKEND_PROFILE_FORM_SERVICE_NAME__" placeholder="z. B. kai-devstral oder kai-qwen"></label>
                    <label style="grid-column:1/-1;"><span>PROFILE_MI50_ACTIVATE_COMMAND optional</span><input name="PROFILE_MI50_ACTIVATE_COMMAND" value="__BACKEND_PROFILE_FORM_ACTIVATE_COMMAND__" placeholder="z. B. ~/switch-kai.sh devstral oder sudo -n systemctl restart kai-devstral"></label>
                    <label style="grid-column:1/-1;"><span>PROFILE_MI50_STATUS_COMMAND optional</span><input name="PROFILE_MI50_STATUS_COMMAND" value="__BACKEND_PROFILE_FORM_STATUS_COMMAND__" placeholder="z. B. systemctl status kai-devstral --no-pager"></label>
                    <label style="grid-column:1/-1;"><span>PROFILE_MI50_LOGS_COMMAND optional</span><input name="PROFILE_MI50_LOGS_COMMAND" value="__BACKEND_PROFILE_FORM_LOGS_COMMAND__" placeholder="z. B. journalctl -u kai-devstral -n 80 --no-pager"></label>
                    <div class="actions">
                      <button class="primary" type="submit">Profil speichern</button>
                      <a class="secondary" href="/internal/admin?tab=settings">Neu / Formular leeren</a>
                    </div>
                  </form>
                  <p class="muted" style="margin-top:12px;">Gedacht fuer deinen Ein-GPU-Alltag: mehrere KI-Services auf der MI50 anlegen, aber immer nur einen aktiv haben. Beim Aktivieren eines Profils stellt der Gateway das Modell-Mapping um und startet den passenden Service auf der MI50. Wenn `sudo systemctl ...` nicht ohne Passwort geht, hinterlegst du hier stattdessen ein eigenes Wrapper-Kommando wie `~/switch-kai.sh qwen`. Ein gesetztes <code>NGL</code> wird beim Aktivieren als <code>KAI_NGL</code> an den Remote-Befehl weitergereicht oder kann ueber <code>{ngl}</code> direkt in einem Kommando-Template benutzt werden.</p>
                </div>
                <div class="card">
                  <h2>Gespeicherte KI-Profile</h2>
                  <div id="backendProfilesList" class="list-stack">__BACKEND_PROFILES_HTML__</div>
                  <label style="margin-top:12px;"><span>Profil-Ansicht</span><textarea readonly>__BACKEND_PROFILE_PREVIEW__</textarea></label>
                </div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>Basis-Settings</h2>
                  <p class="muted">Das hier sind die Felder, die du im Alltag wirklich brauchst: aktuelles Backend, sichtbarer Modellname, Kontext-/Token-Grenzen und die SSH-Strecke zur MI50.</p>
                  <form onsubmit="saveConfig(event)">
                    <div class="grid">
                      <label><span>LLAMACPP_BASE_URL</span><input id="LLAMACPP_BASE_URL" value="__CFG_LLAMACPP_BASE_URL__"></label>
                      <label><span>PUBLIC_MODEL_NAME</span><input id="PUBLIC_MODEL_NAME" value="__CFG_PUBLIC_MODEL_NAME__"></label>
                      <label><span>BACKEND_MODEL_NAME</span><input id="BACKEND_MODEL_NAME" value="__CFG_BACKEND_MODEL_NAME__"></label>
                      <label><span>BACKEND_CONTEXT_WINDOW</span><input id="BACKEND_CONTEXT_WINDOW" value="__CFG_BACKEND_CONTEXT_WINDOW__"></label>
                      <label><span>CONTEXT_RESPONSE_RESERVE</span><input id="CONTEXT_RESPONSE_RESERVE" value="__CFG_CONTEXT_RESPONSE_RESERVE__"></label>
                      <label><span>DEFAULT_MAX_TOKENS</span><input id="DEFAULT_MAX_TOKENS" value="__CFG_DEFAULT_MAX_TOKENS__"></label>
                      <label><span>ADMIN_DEFAULT_MODE</span><input id="ADMIN_DEFAULT_MODE" value="__CFG_ADMIN_DEFAULT_MODE__"></label>
                      <label><span>MI50_SSH_HOST</span><input id="MI50_SSH_HOST" value="__CFG_MI50_SSH_HOST__"></label>
                      <label><span>MI50_SSH_USER</span><input id="MI50_SSH_USER" value="__CFG_MI50_SSH_USER__"></label>
                      <label><span>MI50_SSH_PORT</span><input id="MI50_SSH_PORT" value="__CFG_MI50_SSH_PORT__"></label>
                      <label><span>GATEWAY_LOCAL_ROOT_PREFIX</span><input id="GATEWAY_LOCAL_ROOT_PREFIX" value="__CFG_GATEWAY_LOCAL_ROOT_PREFIX__"></label>
                    </div>
                    <details style="margin-top:18px;">
                      <summary style="cursor:pointer;font-weight:700;">Erweiterte Modell- und Routing-Settings</summary>
                      <div class="grid" style="margin-top:14px;">
                        <label><span>FAST_MODEL_PUBLIC_NAME</span><input id="FAST_MODEL_PUBLIC_NAME" value="__CFG_FAST_MODEL_PUBLIC_NAME__"></label>
                        <label><span>FAST_MODEL_BACKEND_NAME</span><input id="FAST_MODEL_BACKEND_NAME" value="__CFG_FAST_MODEL_BACKEND_NAME__"></label>
                        <label><span>FAST_MODEL_BASE_URL</span><input id="FAST_MODEL_BASE_URL" value="__CFG_FAST_MODEL_BASE_URL__"></label>
                        <label><span>DEEP_MODEL_PUBLIC_NAME</span><input id="DEEP_MODEL_PUBLIC_NAME" value="__CFG_DEEP_MODEL_PUBLIC_NAME__"></label>
                        <label><span>DEEP_MODEL_BACKEND_NAME</span><input id="DEEP_MODEL_BACKEND_NAME" value="__CFG_DEEP_MODEL_BACKEND_NAME__"></label>
                        <label><span>DEEP_MODEL_BASE_URL</span><input id="DEEP_MODEL_BASE_URL" value="__CFG_DEEP_MODEL_BASE_URL__"></label>
                        <label><span>ROUTING_LENGTH_THRESHOLD</span><input id="ROUTING_LENGTH_THRESHOLD" value="__CFG_ROUTING_LENGTH_THRESHOLD__"></label>
                        <label><span>ROUTING_HISTORY_THRESHOLD</span><input id="ROUTING_HISTORY_THRESHOLD" value="__CFG_ROUTING_HISTORY_THRESHOLD__"></label>
                        <label style="grid-column:1/-1;"><span>ROUTING_DEEP_KEYWORDS</span><input id="ROUTING_DEEP_KEYWORDS" value="__CFG_ROUTING_DEEP_KEYWORDS__"></label>
                      </div>
                    </details>
                    <details style="margin-top:14px;">
                      <summary style="cursor:pointer;font-weight:700;">MI50 Remote-Kommandos und Telemetrie</summary>
                      <div class="grid" style="margin-top:14px;">
                        <label style="grid-column:1/-1;"><span>MI50_RESTART_COMMAND</span><input id="MI50_RESTART_COMMAND" value="__CFG_MI50_RESTART_COMMAND__"></label>
                        <label style="grid-column:1/-1;"><span>MI50_STATUS_COMMAND</span><input id="MI50_STATUS_COMMAND" value="__CFG_MI50_STATUS_COMMAND__"></label>
                        <label style="grid-column:1/-1;"><span>MI50_LOGS_COMMAND</span><input id="MI50_LOGS_COMMAND" value="__CFG_MI50_LOGS_COMMAND__"></label>
                        <label style="grid-column:1/-1;"><span>MI50_ROCM_SMI_COMMAND</span><input id="MI50_ROCM_SMI_COMMAND" value="__CFG_MI50_ROCM_SMI_COMMAND__"></label>
                      </div>
                    </details>
                    <details style="margin-top:14px;">
                      <summary style="cursor:pointer;font-weight:700;">Vision / Bildanalyse</summary>
                      <div class="grid" style="margin-top:14px;">
                        <label><span>VISION_BASE_URL</span><input id="VISION_BASE_URL" value="__CFG_VISION_BASE_URL__" placeholder="http://127.0.0.1:8081"></label>
                        <label><span>VISION_MODEL_NAME</span><input id="VISION_MODEL_NAME" value="__CFG_VISION_MODEL_NAME__" placeholder="qwen2.5-vl"></label>
                        <label><span>VISION_MAX_TOKENS</span><input id="VISION_MAX_TOKENS" value="__CFG_VISION_MAX_TOKENS__"></label>
                        <label style="grid-column:1/-1;"><span>VISION_PROMPT</span><input id="VISION_PROMPT" value="__CFG_VISION_PROMPT__"></label>
                      </div>
                    </details>
                    <div class="actions">
                      <button class="primary" type="button" onclick="saveConfig({ preventDefault() {} })">Basis-Settings speichern</button>
                    </div>
                  </form>
                </div>
                <div class="card">
                  <h2>Continue / Client-Anbindung</h2>
                  <p class="muted">Continue bleibt hier schnell erreichbar. Database, Storage und Home Assistant haben eigene Tabs, damit die Settings nicht wieder in einen chaotischen Alles-Editor kippen.</p>
                  <div class="actions">
                    <button class="secondary" type="button" onclick="loadContinueConfig()">Continue YAML laden</button>
                  </div>
                  <label style="margin-top:12px;"><span>Continue YAML</span><textarea id="continueYaml" readonly></textarea></label>
                  <div class="list-stack" style="margin-top:16px;">
                    <div class="list-card">
                      <h3>Was hier bewusst nicht mehr oben steht</h3>
                      <div class="list-meta">Fast/Deep-Kompatibilitaet, Remote-Kommandos und feinere Routing-Regeln sind nicht weg, aber in die erweiterten Bereiche geschoben.</div>
                    </div>
                    <div class="list-card">
                      <h3>Faustregel fuer den Alltag</h3>
                      <div class="list-meta">Meist reichen aktives KI-Profil, sichtbarer Modellname, Kontextfenster, Default max tokens und die SSH-Strecke zur MI50.</div>
                    </div>
                  </div>
                </div>
              </div>
            </section>

            <section id="memory" class="panel __PANEL_MEMORY__">
              <div class="hero">
                <h1>Memory / Sessions</h1>
                <p>Hier siehst du, was der Gateway aktuell wirklich als Chat-Memory haelt: persistierte Sessions, gespeicherte Nachrichten und Rolling Summaries. Das ist der operative Blick auf den aktuellen Speicherstand der KI, nicht bloss rohe Datenbanktechnik.</p>
                <div id="memoryStatus" class="status">__MEMORY_STATUS__</div>
              </div>
              <div class="grid">
                <div class="card stat">
                  <div class="muted">Store Mode</div>
                  <strong id="memoryStoreMode">__MEMORY_STORE_MODE__</strong>
                  <div class="muted">memory oder postgres</div>
                </div>
                <div class="card stat">
                  <div class="muted">Persistenz</div>
                  <strong id="memoryPersistence">__MEMORY_PERSISTENCE__</strong>
                  <div class="muted">volatile oder persistent</div>
                </div>
                <div class="card stat">
                  <div class="muted">Sessions</div>
                  <strong id="memorySessionsCount">__MEMORY_SESSIONS_COUNT__</strong>
                  <div class="muted">gespeicherte Chats</div>
                </div>
                <div class="card stat">
                  <div class="muted">Messages</div>
                  <strong id="memoryMessagesCount">__MEMORY_MESSAGES_COUNT__</strong>
                  <div class="muted">gespeicherte Nachrichten</div>
                </div>
                <div class="card stat">
                  <div class="muted">Summaries</div>
                  <strong id="memorySummariesCount">__MEMORY_SUMMARIES_COUNT__</strong>
                  <div class="muted">Rolling Summaries</div>
                </div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>Session-Historie</h2>
                  <div class="actions">
                    <button class="secondary" type="button" onclick="loadMemoryOverview()">Neu laden</button>
                  </div>
                  <div id="memorySessionsList" class="list-stack">
                    <div class="muted">Session-Daten werden geladen...</div>
                  </div>
                </div>
                <div class="card">
                  <h2>Rolling Summaries</h2>
                  <div class="actions">
                    <select id="memorySessionFilter" onchange="loadMemoryOverview()">
                      <option value="">alle Sessions</option>
                    </select>
                  </div>
                  <div id="memorySummariesList" class="list-stack">
                    <div class="muted">Summary-Daten werden geladen...</div>
                  </div>
                </div>
              </div>
            </section>

            <section id="database" class="panel __PANEL_DATABASE__">
              <div class="hero">
                <h1>PostgreSQL / Memory</h1>
                <p>Hier stellst du die persistente Session-Datenbank ein. Ohne <code>DATABASE_URL</code> bleibt der Gateway bewusst im RAM-Modus. Mit gesetzter URL nutzt der Gateway PostgreSQL fuer Sessions, Messages und Rolling Summaries.</p>
                <div id="databaseStatus" class="status">__DATABASE_STATUS__</div>
              </div>
              <div class="two-col">
                <div class="card">
                  <h2>Verbindung</h2>
                  <form method="post" action="/internal/admin/database/save-form">
                    <label><span>DATABASE_PROFILE_NAME</span><input name="DATABASE_PROFILE_NAME" placeholder="z. B. kai-db, staging, backup-lxc"></label>
                    <label><span>DATABASE_URL</span><input id="DATABASE_URL" name="DATABASE_URL" value="__DATABASE_URL_VALUE__" placeholder="postgresql://user:pass@host:5432/llm_gateway"></label>
                    <div class="actions">
                      <button class="primary" type="submit" formaction="/internal/admin/database/save-form" formmethod="post">Speichern</button>
                      <button class="secondary" type="submit" formaction="/internal/admin/database/test-form" formmethod="post">Verbindung testen</button>
                      <button class="secondary" type="submit" formaction="/internal/admin/database/init-form" formmethod="post">Schema initialisieren</button>
                    </div>
                  </form>
                  <p class="muted" style="margin-top:12px;">Dieser Pfad laeuft bewusst serverseitig. Speichern und Testen funktionieren damit auch dann, wenn Browser-JavaScript oder eingebettete WebViews zicken. Nach dem Speichern wird die volle URL nicht mehr im UI angezeigt.</p>
                  <div style="margin-top:18px;">
                    <h2>Gespeicherte Profile</h2>
                    <div id="databaseProfilesList" class="list-stack">__DATABASE_PROFILES_HTML__</div>
                  </div>
                </div>
                <div class="card">
                  <h2>Status</h2>
                  <div class="grid">
                    <div class="card stat">
                      <div class="muted">Store Mode</div>
                      <strong id="dbStoreMode">__DB_STORE_MODE__</strong>
                      <div class="muted">memory oder postgres</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Connected</div>
                      <strong id="dbConnected">__DB_CONNECTED__</strong>
                      <div class="muted">DB erreichbar</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Schema</div>
                      <strong id="dbSchemaReady">__DB_SCHEMA_READY__</strong>
                      <div class="muted">chat_sessions / chat_messages</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Sessions</div>
                      <strong id="dbSessionsCount">__DB_SESSIONS_COUNT__</strong>
                      <div class="muted">persistierte Chats</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Messages</div>
                      <strong id="dbMessagesCount">__DB_MESSAGES_COUNT__</strong>
                      <div class="muted">persistierte Nachrichten</div>
                    </div>
                  </div>
                  <div style="margin-top:12px;" class="muted">Aktuelles Ziel: <code id="dbUrlRedacted">__DB_URL_REDACTED__</code></div>
                </div>
              </div>
            </section>

            <section id="home-assistant" class="panel __PANEL_HOME_ASSISTANT__">
              <div class="hero">
                <h1>Home Assistant</h1>
                <p>Diese V1 bindet Home Assistant sicher ueber API-Token an. Der Gateway darf nur explizit freigegebene Services fuer erlaubte Entities ausfuehren. Das ist bewusst keine freie "KI darf alles im Haus schalten"-Loesung.</p>
                <div id="haStatus" class="status">__HA_STATUS__</div>
              </div>
              <div class="two-col">
                <div class="card">
                  <h2>Verbindung und Freigaben</h2>
                  <label><span>HOME_ASSISTANT_BASE_URL</span><input id="HOME_ASSISTANT_BASE_URL" placeholder="http://homeassistant.local:8123"></label>
                  <label><span>HOME_ASSISTANT_TOKEN</span><input id="HOME_ASSISTANT_TOKEN" type="password" placeholder="Long-Lived Access Token"></label>
                  <label><span>HOME_ASSISTANT_TIMEOUT_SECONDS</span><input id="HOME_ASSISTANT_TIMEOUT_SECONDS"></label>
                  <label><span>HOME_ASSISTANT_ALLOWED_SERVICES</span><input id="HOME_ASSISTANT_ALLOWED_SERVICES" placeholder="light.turn_on,light.turn_off,switch.turn_on"></label>
                  <label><span>HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES</span><input id="HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES" placeholder="light.,switch.,climate.,script."></label>
                  <div class="actions">
                    <button class="primary" type="button" onclick="saveHomeAssistantConfig()">Speichern</button>
                    <button class="secondary" type="button" onclick="testHomeAssistantConnection()">Verbindung testen</button>
                    <button class="secondary" type="button" onclick="loadHomeAssistantEntities()">Entities laden</button>
                  </div>
                </div>
                <div class="card">
                  <h2>Status und erlaubte Controls</h2>
                  <div class="grid">
                    <div class="card stat">
                      <div class="muted">Configured</div>
                      <strong id="haConfigured">__HA_CONFIGURED__</strong>
                      <div class="muted">URL und Token gesetzt</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Connected</div>
                      <strong id="haConnected">__HA_CONNECTED__</strong>
                      <div class="muted">Home Assistant erreichbar</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Location</div>
                      <strong id="haLocation">__HA_LOCATION__</strong>
                      <div class="muted">HA-Instanz</div>
                    </div>
                  </div>
                  <label style="margin-top:12px;"><span>Erlaubte Services</span><textarea id="haAllowedServicesView" readonly></textarea></label>
                  <label><span>Erlaubte Entity-Praefixe</span><textarea id="haAllowedPrefixesView" readonly></textarea></label>
                </div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>Entities</h2>
                  <div class="actions">
                    <select id="haEntityDomain">
                      <option value="">alle erlaubten Domains</option>
                      <option value="light">light</option>
                      <option value="switch">switch</option>
                      <option value="climate">climate</option>
                      <option value="script">script</option>
                    </select>
                    <button class="secondary" type="button" onclick="loadHomeAssistantEntities()">Neu laden</button>
                  </div>
                  <label style="margin-top:12px;"><span>Entity-Liste</span><textarea id="haEntities" readonly></textarea></label>
                </div>
                <div class="card">
                  <h2>Aktion testen</h2>
                  <label><span>Domain</span><input id="haActionDomain" value="light"></label>
                  <label><span>Service</span><input id="haActionService" value="turn_on"></label>
                  <label><span>Entity ID</span><input id="haActionEntityId" placeholder="light.wohnzimmer"></label>
                  <label><span>service_data (JSON optional)</span><textarea id="haActionData" placeholder='{"brightness_pct": 50}'></textarea></label>
                  <div class="actions">
                    <button class="primary" type="button" onclick="runHomeAssistantAction()">Aktion ausfuehren</button>
                  </div>
                  <label style="margin-top:12px;"><span>Antwort</span><textarea id="haActionResult" readonly></textarea></label>
                </div>
              </div>
            </section>

            <section id="storage" class="panel __PANEL_STORAGE__">
              <div class="hero">
                <h1>Storage / Dokumente</h1>
                <p>Hier legst du echte Speicherziele fuer Dokumente an: lokal oder als bereits gemounteten SMB-Pfad. Uploads fuer Text- und PDF-Dateien landen dort als Dateien, waehrend Metadaten und extrahierter Text in PostgreSQL landen. Das ist die Grundlage dafuer, diese Inhalte spaeter als KI-Kontext oder Aufgabenbasis zu nutzen.</p>
                <div id="storageStatus" class="status">__STORAGE_STATUS__</div>
              </div>
              <div class="two-col">
                <div class="card">
                  <h2>Storage-Profile</h2>
                  <form method="post" action="/internal/admin/storage/location/save-form">
                    <label><span>STORAGE_PROFILE_NAME</span><input name="STORAGE_PROFILE_NAME" placeholder="z. B. intern-ssd, smb-wissen, archiv-hdd"></label>
                    <label><span>STORAGE_BACKEND_TYPE</span>
                      <select name="STORAGE_BACKEND_TYPE">
                        <option value="local">local</option>
                        <option value="smb_mount">smb_mount</option>
                      </select>
                    </label>
                    <label><span>STORAGE_BASE_PATH</span><input name="STORAGE_BASE_PATH" placeholder="/srv/llm-dokumente oder /mnt/smb/ki-wissen"></label>
                    <div class="actions">
                      <button class="primary" type="submit">Profil speichern</button>
                    </div>
                  </form>
                  <p class="muted" style="margin-top:12px;">`local` ist ein interner Pfad auf dem Gateway oder im LXC. `smb_mount` bedeutet bewusst: Der SMB-Share ist bereits auf dem Gateway-Host gemountet. Der Gateway selbst verwaltet keine SMB-Zugangsdaten.</p>
                  <div style="margin-top:18px;">
                    <h2>Gespeicherte Storage-Ziele</h2>
                    <div id="storageProfilesList" class="list-stack">__STORAGE_PROFILES_HTML__</div>
                  </div>
                </div>
                <div class="card">
                  <h2>Aktueller Speicherzustand</h2>
                  <div class="grid">
                    <div class="card stat">
                      <div class="muted">Dokument-Metadaten</div>
                      <strong id="storageSessionMode">__STORAGE_SESSION_MODE__</strong>
                      <div class="muted">memory oder postgres</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Index-Persistenz</div>
                      <strong id="storagePersistence">__STORAGE_PERSISTENCE__</strong>
                      <div class="muted">Metadaten/Text ueberleben Neustarts</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Aktiver Dateipfad</div>
                      <strong id="storageTarget">__STORAGE_TARGET__</strong>
                      <div class="muted">lokal oder SMB-Mount</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Storage Profile</div>
                      <strong id="storageProfilesCount">__STORAGE_PROFILES_COUNT__</strong>
                      <div class="muted">angelegte Speicherorte</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Dokumente</div>
                      <strong id="storageDocumentsCount">__STORAGE_DOCUMENTS_COUNT__</strong>
                      <div class="muted">erfasste Dateien</div>
                    </div>
                  </div>
                  <div style="margin-top:12px;" class="muted">Aktive Quelle: <code id="storageActiveName">__STORAGE_ACTIVE_NAME__</code></div>
                </div>
              </div>
              <div class="two-col" style="margin-top:18px;">
                <div class="card">
                  <h2>Dokument hochladen</h2>
                  <form method="post" action="/internal/admin/storage/upload-form" enctype="multipart/form-data">
                    <label><span>Storage-Ziel</span>
                      <select name="STORAGE_UPLOAD_PROFILE_ID">__STORAGE_UPLOAD_OPTIONS_HTML__</select>
                    </label>
                    <label><span>Titel optional</span><input name="DOCUMENT_TITLE" placeholder="z. B. Wissensnotiz, Aufgabenliste, Handbuch"></label>
                    <label><span>Tags optional</span><input name="DOCUMENT_TAGS" placeholder="projekt,aufgaben,pdf"></label>
                    <label><span>Datei</span><input type="file" name="DOCUMENT_FILE" accept=".txt,.md,.markdown,.pdf,.log,.csv,.json,.yaml,.yml,.jpg,.jpeg,.png,.webp,.gif,image/*" required></label>
                    <div class="actions">
                      <button class="primary" type="submit">Dokument speichern</button>
                    </div>
                  </form>
                  <p class="muted" style="margin-top:12px;">Aktuell werden Textdateien, `.pdf` und gaengige Bilddateien verarbeitet. Die Datei bleibt im gewaehlten Storage, waehrend extrahierter Text, Bildanalyse und Metadaten in PostgreSQL landen. Im Admin-Chat kannst du gespeicherte Dokumente und Bilder danach direkt als Kontext auswaehlen.</p>
                </div>
                <div class="card">
                  <h2>Dokumente / Wissensbasis</h2>
                  <div id="storageDocumentsList" class="list-stack">__STORAGE_DOCUMENTS_HTML__</div>
                </div>
              </div>
            </section>

            <section id="skills" class="panel __PANEL_SKILLS__">
              <div class="hero">
                <h1>Skills / MCP</h1>
                <p>Hier verwaltest du den MCP-Tool-Broker. Builtin-Tools bleiben stabil, zusaetzliche Custom-Tools mappen auf freigegebene Ops-Befehle. So kann Kai kontrolliert neue Wartungs-/Installationsaktionen nutzen, ohne ein freies Root-Terminal zu bekommen.</p>
                <div id="skillsStatus" class="status">__SKILLS_STATUS__</div>
              </div>
              <div class="two-col">
                <div class="card">
                  <h2>MCP Tool-Broker</h2>
                  <div class="grid">
                    <div class="card stat">
                      <div class="muted">MCP Tools</div>
                      <strong id="mcpToolsCount">__MCP_TOOLS_COUNT__</strong>
                      <div class="muted">builtin + custom</div>
                    </div>
                    <div class="card stat">
                      <div class="muted">Custom Tools</div>
                      <strong id="mcpCustomToolsCount">__MCP_CUSTOM_TOOLS_COUNT__</strong>
                      <div class="muted">eigene Mappings</div>
                    </div>
                  </div>
                  <div class="actions">
                    <button class="secondary" type="button" onclick="loadMcpTools()">MCP-Tools laden</button>
                    <button class="secondary" type="button" onclick="loadOpsCatalog()">Ops-Katalog laden</button>
                  </div>
                  <label style="margin-top:12px;"><span>Aktive MCP-Tools</span><textarea id="mcpToolsView" readonly></textarea></label>
                  <label><span>Ops-Katalog (Allowlist)</span><textarea id="mcpOpsCatalogView" readonly></textarea></label>
                  <div class="muted">Letztes Skills/MCP-Refresh: <code id="skillsLastLoaded">-</code></div>
                  <p class="muted" style="margin-top:12px;">Custom-Tools rufen intern exakt einen freigegebenen Ops-Befehl auf, z. B. <code>gateway.install_htop</code>. Damit bleibt die Ausfuehrung nachvollziehbar und abgesichert.</p>
                </div>
                <div class="card">
                  <h2>Custom MCP-Tool hinzufuegen</h2>
                  <div class="grid">
                    <label><span>Tool Name</span><input id="customMcpName" placeholder="z. B. gateway.install_htop_alias"></label>
                    <label><span>Beschreibung</span><input id="customMcpDescription" placeholder="Kurze Beschreibung fuer Clients"></label>
                    <label><span>Ops Target</span>
                      <select id="customMcpTarget" onchange="refreshCustomMcpCommandOptions()">
                        <option value="gateway">gateway</option>
                        <option value="kai">kai</option>
                      </select>
                    </label>
                    <label><span>Ops Command</span>
                      <select id="customMcpCommand">
                        <option value="">zuerst Ops-Katalog laden</option>
                      </select>
                    </label>
                  </div>
                  <div class="actions">
                    <button class="primary" type="button" onclick="saveCustomMcpTool()">Tool speichern</button>
                  </div>
                  <p class="muted" style="margin-top:12px;">Der Tool-Name muss 3-64 Zeichen lang sein und darf nur <code>a-z</code>, <code>0-9</code>, <code>.</code>, <code>-</code> und <code>_</code> enthalten.</p>
                  <div style="margin-top:16px;">
                    <h2>Gespeicherte Custom-Tools</h2>
                    <div id="mcpCustomToolsList" class="list-stack">__MCP_CUSTOM_TOOLS_HTML__</div>
                  </div>
                </div>
              </div>
            </section>

            <section id="chat" class="panel __PANEL_CHAT__">
              <div class="hero">
                <h1>Admin Chat</h1>
                <p>Session-Chat, Auto-Routing und Streaming bleiben im Hub. Fuer jetzt wird die bestehende Chat-Oberflaeche direkt eingebettet.</p>
              </div>
              <div class="card">
                <iframe src="/internal/chat?embedded=1" title="Admin Chat"></iframe>
              </div>
            </section>

            <section id="ops" class="panel __PANEL_OPS__">
              <div class="hero">
                <h1>Ops Konsole</h1>
                <p>Kein freies Root-Webterminal. Stattdessen eine sichere Terminal-V1 mit Status, Logs, Restart und freigegebenen Preset-Befehlen fuer Gateway, Kai und kuratierte lokale Tool-Installationen.</p>
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
                    <option value="tools">tools (gateway)</option>
                    <option value="skills">skills (gateway)</option>
                    <option value="apt_update">apt update (gateway)</option>
                    <option value="install_git">install git (gateway)</option>
                    <option value="install_curl">install curl (gateway)</option>
                    <option value="install_gh">install gh (gateway)</option>
                    <option value="install_ripgrep">install ripgrep (gateway)</option>
                    <option value="install_htop">install htop (gateway)</option>
                    <option value="install_tmux">install tmux (gateway)</option>
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

            <section id="devices" class="panel __PANEL_DEVICES__">
              <div class="hero">
                <h1>Pi / Devices</h1>
                <p>Hier verbindest du fertige Kai-Pis schnell ueber Gateway-URL, Token und optional SSH. Fuer einen laufenden Kai-Pi reichen praktisch Host, User, Port und auf Wunsch ein Passwort.</p>
                <div id="deviceStatus" class="status">__DEVICE_STATUS__</div>
              </div>
              <div class="two-col">
                <div class="card">
                  <h2>Kai-Pi Schnell verbinden</h2>
                  <form method="post" action="/internal/admin/device/save-form">
                    <input type="hidden" name="DEVICE_PROFILE_ID" value="__DEVICE_PROFILE_FORM_ID__">
                    <div class="grid">
                      <label><span>PI_SSH_HOST</span><input name="PI_SSH_HOST" value="__DEVICE_PROFILE_FORM_SSH_HOST__" placeholder="192.168.x.x"></label>
                      <label><span>PI_SSH_USER</span><input name="PI_SSH_USER" value="__DEVICE_PROFILE_FORM_SSH_USER__" placeholder="pi"></label>
                      <label><span>PI_SSH_PORT</span><input name="PI_SSH_PORT" value="__DEVICE_PROFILE_FORM_SSH_PORT__" placeholder="22"></label>
                      <label><span>PI_SSH_PASSWORD</span><input type="password" name="PI_SSH_PASSWORD" value="__DEVICE_PROFILE_FORM_SSH_PASSWORD__" placeholder="optional, sonst SSH-Key"></label>
                      <label><span>DEVICE_PROFILE_NAME</span><input name="DEVICE_PROFILE_NAME" value="__DEVICE_PROFILE_FORM_NAME__" placeholder="z. B. kai-pi-wohnzimmer"></label>
                      <label><span>DEVICE_GATEWAY_BASE_URL</span><input name="DEVICE_GATEWAY_BASE_URL" value="__DEVICE_PROFILE_FORM_GATEWAY_BASE_URL__" placeholder="http://gateway:8000"></label>
                      <label><span>DEVICE_TOKEN</span><input name="DEVICE_TOKEN" value="__DEVICE_PROFILE_FORM_DEVICE_TOKEN__" placeholder="leer lassen = automatisch erzeugen"></label>
                      <label style="grid-column:1/-1;"><span>DEVICE_NOTES</span><input name="DEVICE_NOTES" value="__DEVICE_PROFILE_FORM_NOTES__" placeholder="z. B. Pi 5 mit 800x640 Display im Wohnzimmer"></label>
                    </div>
                    <details style="margin-top:14px;">
                      <summary style="cursor:pointer;font-weight:700;">Erweiterte SSH-/Bootstrap-Optionen</summary>
                      <div class="grid" style="margin-top:14px;">
                        <label><span>PI_REMOTE_DIR</span><input name="PI_REMOTE_DIR" value="__DEVICE_PROFILE_FORM_REMOTE_DIR__" placeholder="~/kai-pi"></label>
                        <label><span>PI_SSH_ROOT_PREFIX</span><input name="PI_SSH_ROOT_PREFIX" value="__DEVICE_PROFILE_FORM_SSH_ROOT_PREFIX__" placeholder="sudo -n"></label>
                      </div>
                    </details>
                    <div class="actions">
                      <button class="primary" type="submit">Device-Profil speichern</button>
                      <a class="secondary" href="/internal/admin?tab=devices">Neu / Formular leeren</a>
                    </div>
                  </form>
                  <div class="card" style="margin-top:16px;">
                    <h2>Kai Face zentral vom Gateway steuern</h2>
                    <p class="muted">Das Pi-Menue wird bewusst nicht mehr benoetigt. Style, State und Layer steuerst du hier im Gateway und drueckst dann nur <code>Apply</code>.</p>
                    <form method="post" action="/internal/admin/device/face-apply-form">
                      <div class="grid">
                        <label><span>Ziel-Pi Profil</span><select name="profile_id">__DEVICE_FACE_PROFILE_OPTIONS_HTML__</select></label>
                        <label><span>Style Name</span><input name="FACE_STYLE_NAME" value="__DEVICE_FACE_STYLE_NAME__" placeholder="z. B. gaming_mode"></label>
                        <label>
                          <span>State</span>
                          <select name="FACE_STATE">
                            <option value="idle">idle</option>
                            <option value="listening">listening</option>
                            <option value="thinking">thinking</option>
                            <option value="speaking">speaking</option>
                            <option value="happy">happy</option>
                            <option value="sleepy">sleepy</option>
                            <option value="error">error</option>
                          </select>
                        </label>
                        <label>
                          <span>Render Mode</span>
                          <select name="FACE_RENDER_MODE">
                            <option value="vector">vector</option>
                            <option value="sprite_pack">sprite_pack</option>
                          </select>
                        </label>
                        <label><span>Sprite Pack</span><input name="FACE_SPRITE_PACK" value="__DEVICE_FACE_SPRITE_PACK__" placeholder="z. B. robot_v1"></label>
                        <label>
                          <span>Variant (F1..F17)</span>
                          <select name="FACE_VARIANT">
                            <option value="custom">custom</option>
                            <option value="F1">F1 baseline</option>
                            <option value="F2">F2 blue eyes</option>
                            <option value="F3">F3 cheeks</option>
                            <option value="F4">F4 close eyes</option>
                            <option value="F5">F5 ears</option>
                            <option value="F6">F6 eyebrows</option>
                            <option value="F7">F7 eyelids</option>
                            <option value="F8">F8 hair</option>
                            <option value="F9">F9 iris</option>
                            <option value="F10">F10 no mouth</option>
                            <option value="F11">F11 no pupils</option>
                            <option value="F12">F12 nose</option>
                            <option value="F13">F13 oval eyes</option>
                            <option value="F14">F14 raised eyes</option>
                            <option value="F15">F15 small eyes</option>
                            <option value="F16">F16 white face</option>
                            <option value="F17">F17 far eyes</option>
                          </select>
                        </label>
                        <label>
                          <span>Face Color</span>
                          <select name="FACE_FACE_COLOR">
                            <option value="black">black</option>
                            <option value="white">white</option>
                          </select>
                        </label>
                        <label>
                          <span>Eye Shape</span>
                          <select name="FACE_EYE_SHAPE">
                            <option value="round">round</option>
                            <option value="oval">oval</option>
                            <option value="small">small</option>
                          </select>
                        </label>
                        <label>
                          <span>Eye Spacing</span>
                          <select name="FACE_EYE_SPACING">
                            <option value="normal">normal</option>
                            <option value="far">far</option>
                            <option value="raised">raised</option>
                          </select>
                        </label>
                        <label><span>Iris Color</span><input name="FACE_IRIS_COLOR" value="__DEVICE_FACE_IRIS_COLOR__" placeholder="#59c7ff"></label>
                      </div>
                      <p class="muted" style="margin-top:8px;">Eigene Designs: Lege auf dem Pi einen Pack-Ordner unter <code>/home/pi/kai/styles/packs/&lt;pack_name&gt;</code> mit <code>manifest.json</code> und PNG-Layern an. Dann hier <code>Render Mode = sprite_pack</code> und den Pack-Namen setzen.</p>
                      <div class="grid" style="margin-top:8px;">
                        <label><span><input type="checkbox" name="FACE_PUPILS" value="1" checked> pupils</span></label>
                        <label><span><input type="checkbox" name="FACE_IRIS" value="1"> iris</span></label>
                        <label><span><input type="checkbox" name="FACE_MOUTH" value="1" checked> mouth</span></label>
                        <label><span><input type="checkbox" name="FACE_NOSE" value="1"> nose</span></label>
                        <label><span><input type="checkbox" name="FACE_CHEEKS" value="1"> cheeks</span></label>
                        <label><span><input type="checkbox" name="FACE_EARS" value="1"> ears</span></label>
                        <label><span><input type="checkbox" name="FACE_EYEBROWS" value="1" checked> eyebrows</span></label>
                        <label><span><input type="checkbox" name="FACE_EYELIDS" value="1"> eyelids</span></label>
                        <label><span><input type="checkbox" name="FACE_HAIR" value="1"> hair</span></label>
                        <label><span><input type="checkbox" name="FACE_CLOSE_EYES" value="1"> close eyes</span></label>
                      </div>
                      <div class="actions">
                        <button class="primary" type="submit">Apply Face to Kai</button>
                      </div>
                    </form>
                  </div>
                  <div class="list-stack" style="margin-top:16px;">
                    <div class="list-card">
                      <h3>Aktiver Device-Token</h3>
                      <div class="list-meta"><code>__DEVICE_ACTIVE_TOKEN_REDACTED__</code></div>
                    </div>
                    <div class="list-card">
                      <h3>Was fuer fertige Kai-Pis reicht</h3>
                      <div class="list-meta">Nutze bei bestehenden Kai-Pis den Button <code>Verbinden / .env sync</code>: der Gateway schreibt URL+Token direkt auf den Pi und startet <code>kai.service</code> neu. Fuer rohe Pi-Installationen gibt es pro Profil den Button <code>PI installieren</code>.</div>
                    </div>
                  </div>
                  <label style="margin-top:12px;">
                    <span>Beispiel-Request</span>
                    <textarea readonly>curl -s http://GATEWAY:8000/api/device/ask \
  -H "Authorization: Bearer DEVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Wie ist der Status von Kai?","mode":"auto"}'</textarea>
                  </label>
                </div>
                <div class="card">
                  <h2>Gespeicherte Pi-Profile</h2>
                  <div class="list-stack">__DEVICE_PROFILES_HTML__</div>
                  <label style="margin-top:14px;"><span>Pi-Install-Skript</span><textarea readonly>__DEVICE_BOOTSTRAP_PREVIEW__</textarea></label>
                  <p class="muted" style="margin-top:12px;">Der Button <code>PI installieren</code> fuehrt dieses Script serverseitig ueber SSH aus. Damit kannst du auch einen rohen Pi ohne bestehendes Kai-Setup direkt vom Gateway aus vorbereiten.</p>
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
              "HOME_ASSISTANT_BASE_URL",
              "HOME_ASSISTANT_TOKEN",
              "HOME_ASSISTANT_TIMEOUT_SECONDS",
              "HOME_ASSISTANT_ALLOWED_SERVICES",
              "HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES",
              "VISION_BASE_URL",
              "VISION_MODEL_NAME",
              "VISION_PROMPT",
              "VISION_MAX_TOKENS",
              "MI50_SSH_HOST",
              "MI50_SSH_USER",
              "MI50_SSH_PORT",
              "GATEWAY_LOCAL_ROOT_PREFIX",
              "MI50_RESTART_COMMAND",
              "MI50_STATUS_COMMAND",
              "MI50_LOGS_COMMAND",
              "MI50_ROCM_SMI_COMMAND"
            ];

            function setText(id, value) {
              const node = document.getElementById(id);
              if (node) node.textContent = value;
            }

            function setValue(id, value) {
              const node = document.getElementById(id);
              if (node) node.value = value;
            }

            function valueOr(value, fallback) {
              return value === null || value === undefined ? fallback : value;
            }

            function nestedValue(obj, path, fallback) {
              let current = obj;
              for (const key of path) {
                if (current === null || current === undefined || !(key in current)) {
                  return fallback;
                }
                current = current[key];
              }
              return valueOr(current, fallback);
            }

            function errorMessage(data, fallback) {
              if (data && data.detail) return data.detail;
              if (data && data.error && data.error.message) return data.error.message;
              return fallback;
            }

            function escapeHtml(value) {
              return String(value)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/\"/g, "&quot;")
                .replace(/'/g, "&#39;");
            }

            function formatTimestamp(value) {
              if (!value) return "-";
              try {
                return new Date(value).toLocaleString("de-DE");
              } catch (_error) {
                return value;
              }
            }

            function setDashboardStatus(message, error = false) {
              const node = document.getElementById("dashboardStatus");
              if (!node) return;
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            function setSettingsStatus(message, error = false) {
              const node = document.getElementById("settingsStatus");
              if (!node) return;
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            function setSkillsStatus(message, error = false) {
              const node = document.getElementById("skillsStatus");
              if (!node) return;
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            function markSkillsLoadedNow() {
              const node = document.getElementById("skillsLastLoaded");
              if (!node) return;
              try {
                node.textContent = new Date().toLocaleTimeString("de-DE");
              } catch (_error) {
                node.textContent = String(Date.now());
              }
            }

            window.opsCatalog = { gateway: [], kai: [] };

            function refreshCustomMcpCommandOptions() {
              const targetNode = document.getElementById("customMcpTarget");
              const commandNode = document.getElementById("customMcpCommand");
              if (!targetNode || !commandNode) return;
              const target = targetNode.value || "gateway";
              const commands = (window.opsCatalog && window.opsCatalog[target]) || [];
              const currentValue = commandNode.value;
              if (!commands.length) {
                commandNode.innerHTML = '<option value="">keine Befehle geladen</option>';
                return;
              }
              commandNode.innerHTML = commands.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
              if (commands.includes(currentValue)) {
                commandNode.value = currentValue;
              }
            }

            function renderCustomMcpTools(items) {
              const node = document.getElementById("mcpCustomToolsList");
              if (!node) return;
              if (!items || items.length === 0) {
                node.innerHTML = '<div class="muted">Noch keine Custom-MCP-Tools gespeichert.</div>';
                return;
              }
              node.innerHTML = items.map((tool) => `
                <div class="list-card">
                  <h3>${escapeHtml(tool.name || "custom.tool")}</h3>
                  <div class="list-meta">${escapeHtml(tool.description || "-")}</div>
                  <div class="list-meta">Ops: <code>${escapeHtml(tool.target || "gateway")}.${escapeHtml(tool.command || "status")}</code></div>
                  <div class="actions">
                    <button class="secondary" type="button" onclick="deleteCustomMcpTool('${escapeHtml(tool.name || "")}')">Loeschen</button>
                  </div>
                </div>
              `).join("");
            }

            function renderOpsCatalogView(catalog) {
              const node = document.getElementById("mcpOpsCatalogView");
              if (!node) return;
              if (!catalog || typeof catalog !== "object") {
                node.value = "-";
                return;
              }
              const targets = Object.keys(catalog).sort();
              const lines = [];
              for (const target of targets) {
                const commands = Array.isArray(catalog[target]) ? catalog[target] : [];
                lines.push(`${target}:`);
                lines.push(...commands.map((item) => `  - ${item}`));
                lines.push("");
              }
              node.value = lines.join("\\n").trim() || "-";
            }

            async function loadOpsCatalog() {
              try {
                setSkillsStatus("Ops-Katalog wird geladen...");
                const res = await fetch("/api/admin/ops/catalog");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Ops-Katalog fehlgeschlagen: ${res.status}`));
                window.opsCatalog = data.targets || { gateway: [], kai: [] };
                refreshCustomMcpCommandOptions();
                renderOpsCatalogView(window.opsCatalog);
                const gatewayCount = Array.isArray(window.opsCatalog.gateway) ? window.opsCatalog.gateway.length : 0;
                const kaiCount = Array.isArray(window.opsCatalog.kai) ? window.opsCatalog.kai.length : 0;
                markSkillsLoadedNow();
                setSkillsStatus(`Ops-Katalog geladen (gateway: ${gatewayCount}, kai: ${kaiCount}).`);
              } catch (error) {
                setSkillsStatus(error.message, true);
              }
            }

            async function loadMcpTools() {
              try {
                setSkillsStatus("MCP-Tools werden geladen...");
                const res = await fetch("/api/admin/mcp/tools");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `MCP-Tools fehlgeschlagen: ${res.status}`));
                const tools = data.tools || [];
                const lines = tools.map((tool) => {
                  const prefix = tool.is_custom ? "[custom]" : "[builtin]";
                  const authPart = tool.requires_admin ? " [admin]" : " [device-ok]";
                  const opsPart = tool.target && tool.command ? ` -> ${tool.target}.${tool.command}` : "";
                  return `${prefix}${authPart} ${tool.name}${opsPart} | ${tool.description || "-"}`;
                });
                setValue("mcpToolsView", lines.join("\\n"));
                setText("mcpToolsCount", String(tools.length));
                markSkillsLoadedNow();
                setSkillsStatus(`MCP-Tools geladen (${tools.length}).`);
              } catch (error) {
                setSkillsStatus(error.message, true);
              }
            }

            async function loadCustomMcpTools() {
              try {
                const res = await fetch("/api/admin/mcp/custom-tools");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Custom-MCP-Tools fehlgeschlagen: ${res.status}`));
                const tools = data.tools || [];
                renderCustomMcpTools(tools);
                setText("mcpCustomToolsCount", String(tools.length));
                if (data.ops_catalog) {
                  window.opsCatalog = data.ops_catalog;
                  refreshCustomMcpCommandOptions();
                }
              } catch (error) {
                setSkillsStatus(error.message, true);
              }
            }

            async function saveCustomMcpTool() {
              try {
                const payload = {
                  name: document.getElementById("customMcpName").value.trim().toLowerCase(),
                  description: document.getElementById("customMcpDescription").value.trim(),
                  target: document.getElementById("customMcpTarget").value,
                  command: document.getElementById("customMcpCommand").value,
                };
                const res = await fetch("/api/admin/mcp/custom-tools", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Tool speichern fehlgeschlagen: ${res.status}`));
                document.getElementById("customMcpName").value = "";
                document.getElementById("customMcpDescription").value = "";
                await loadCustomMcpTools();
                await loadMcpTools();
                setSkillsStatus(`Custom-Tool gespeichert: ${payload.name}`);
              } catch (error) {
                setSkillsStatus(error.message, true);
              }
            }

            async function deleteCustomMcpTool(name) {
              const toolName = String(name || "").trim().toLowerCase();
              if (!toolName) return;
              if (!window.confirm(`Custom-Tool '${toolName}' wirklich loeschen?`)) return;
              try {
                const res = await fetch(`/api/admin/mcp/custom-tools/${encodeURIComponent(toolName)}/delete`, {
                  method: "POST",
                });
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Tool loeschen fehlgeschlagen: ${res.status}`));
                await loadCustomMcpTools();
                await loadMcpTools();
                setSkillsStatus(`Custom-Tool geloescht: ${toolName}`);
              } catch (error) {
                setSkillsStatus(error.message, true);
              }
            }

            function formatUptime(seconds) {
              if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "-";
              const total = Math.max(0, Math.round(Number(seconds)));
              const hours = Math.floor(total / 3600);
              const minutes = Math.floor((total % 3600) / 60);
              const secs = total % 60;
              if (hours > 0) return `${hours}h ${minutes}m`;
              if (minutes > 0) return `${minutes}m ${secs}s`;
              return `${secs}s`;
            }

            function updateDashboardConfigFacts(config) {
              if (!config) return;
              setText("dashboardPublicModel", valueOr(config.PUBLIC_MODEL_NAME, "-"));
              setText("dashboardBackendModel", valueOr(config.BACKEND_MODEL_NAME, "-"));
              setText("dashboardAdminMode", valueOr(config.ADMIN_DEFAULT_MODE, "-"));
              setText("dashboardBackendBaseUrl", valueOr(config.LLAMACPP_BASE_URL, "-"));
              setText("dashboardContextWindow", valueOr(config.BACKEND_CONTEXT_WINDOW, "-"));
              setText("dashboardResponseReserve", valueOr(config.CONTEXT_RESPONSE_RESERVE, "-"));
              setText("dashboardDefaultMaxTokens", valueOr(config.DEFAULT_MAX_TOKENS, "-"));
              setText(
                "dashboardRoutingThresholds",
                `${valueOr(config.ROUTING_LENGTH_THRESHOLD, "-")} Zeichen / ${valueOr(config.ROUTING_HISTORY_THRESHOLD, "-")} Nachrichten`,
              );
            }

            let headerTelemetryLoading = false;

            function updateHeaderTelemetryView(data) {
              setText("headerCpuUsageValue", data && data.cpu_usage_percent != null ? `${Number(data.cpu_usage_percent).toFixed(1)}%` : "n/a");
              setText("headerCpuTempValue", data && data.cpu_temp_c != null ? `${Number(data.cpu_temp_c).toFixed(1)} C` : "n/a");
              setText("headerGpuUsageValue", data && data.gpu_usage_percent != null ? `${Number(data.gpu_usage_percent).toFixed(1)}%` : "n/a");
              setText("headerGpuTempValue", data && data.temperature_c != null ? `${Number(data.temperature_c).toFixed(1)} C` : "n/a");
              setText("headerGpuPowerValue", data && data.power_w != null ? `${Number(data.power_w).toFixed(1)} W` : "n/a");

              let vramText = "n/a";
              if (data && data.vram_used_gib != null && data.vram_total_gib != null) {
                const percentText = data.vram_percent != null ? `${Number(data.vram_percent).toFixed(1)}%` : "?";
                vramText = percentText;
                setText("gpuVramValue", `${Number(data.vram_used_gib).toFixed(1)} / ${Number(data.vram_total_gib).toFixed(1)} GiB (${percentText})`);
              } else if (data && data.vram_percent != null) {
                vramText = `${Number(data.vram_percent).toFixed(1)}%`;
                setText("gpuVramValue", vramText);
              } else {
                setText("gpuVramValue", "n/a");
              }
              setText("headerGpuVramValue", vramText);
              setText("gpuTempValue", data && data.temperature_c != null ? `${Number(data.temperature_c).toFixed(1)} C` : "n/a");
              setText("gpuPowerValue", data && data.power_w != null ? `${Number(data.power_w).toFixed(1)} W` : "n/a");
            }

            async function loadHeaderTelemetry() {
              if (headerTelemetryLoading) return;
              headerTelemetryLoading = true;
              try {
                const res = await fetch("/api/admin/system/summary");
                if (!res.ok) throw new Error(`Systemstatus ${res.status}`);
                const data = await res.json();
                updateHeaderTelemetryView(data);
              } catch (_error) {
                setText("headerCpuUsageValue", "n/a");
                setText("headerCpuTempValue", "n/a");
                setText("headerGpuUsageValue", "n/a");
                setText("headerGpuTempValue", "n/a");
                setText("headerGpuPowerValue", "n/a");
                setText("headerGpuVramValue", "n/a");
              } finally {
                headerTelemetryLoading = false;
              }
            }

            async function loadDashboard() {
              const issues = [];
              try {
                const [healthResult, metricsResult] = await Promise.allSettled([
                  fetch("/internal/health"),
                  fetch("/internal/metrics"),
                ]);

                if (healthResult.status === "fulfilled") {
                  const healthRes = healthResult.value;
                  const health = await healthRes.json();
                  if (healthRes.ok) {
                    setText("gatewayState", nestedValue(health, ["gateway", "status"], "-"));
                    setText("gatewayInfo", "Gateway antwortet");
                    setText("backendState", nestedValue(health, ["backend", "status"], "-"));
                    setText(
                      "backendInfo",
                      valueOr(nestedValue(health, ["backend", "model"], "-"), "-") + " @ " + valueOr(nestedValue(health, ["backend", "base_url"], "-"), "-"),
                    );
                  } else {
                    issues.push(errorMessage(health, `Health ${healthRes.status}`));
                    setText("gatewayState", nestedValue(health, ["gateway", "status"], "error"));
                    setText("backendState", nestedValue(health, ["backend", "status"], "error"));
                    setText("backendInfo", valueOr(nestedValue(health, ["backend", "message"], "-"), "-"));
                  }
                } else {
                  issues.push("Health nicht erreichbar");
                }

                if (metricsResult.status === "fulfilled") {
                  const metricsRes = metricsResult.value;
                  const metrics = await metricsRes.json();
                  if (metricsRes.ok) {
                    setText("requestsValue", valueOr(metrics.total_requests, "-"));
                    setText("backendCallsValue", valueOr(metrics.backend_calls, "-"));
                    setText("uptimeValue", formatUptime(metrics.uptime_seconds));
                    setText("avgRequestValue", `${valueOr(metrics.average_request_duration_ms, "-")} ms`);
                  } else {
                    issues.push(errorMessage(metrics, `Metrics ${metricsRes.status}`));
                  }
                } else {
                  issues.push("Metrics nicht erreichbar");
                }

                updateDashboardConfigFacts(window.currentConfig || {});
                if (issues.length) {
                  setDashboardStatus(`Teilweise geladen: ${issues.join(" | ")}`, true);
                } else {
                  setDashboardStatus("Dashboard geladen.");
                }
              } catch (error) {
                setDashboardStatus(error.message, true);
              }
            }

            function setMemoryStatus(message, error = false) {
              const node = document.getElementById("memoryStatus");
              if (!node) return;
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            function renderMemorySessions(items) {
              const node = document.getElementById("memorySessionsList");
              if (!node) return;
              if (!items || items.length === 0) {
                node.innerHTML = '<div class="muted">Noch keine gespeicherten Sessions.</div>';
                return;
              }
              node.innerHTML = items.map((item) => `
                <div class="list-card">
                  <h3>${escapeHtml(item.title || "Session")}</h3>
                  <div class="list-meta">Mode: ${escapeHtml(item.mode || "-")} | Modell: ${escapeHtml(item.resolved_model || "-")} | Nachrichten: ${escapeHtml(item.message_count || 0)} | ca. Tokens: ${escapeHtml(item.token_estimate || 0)}</div>
                  <div class="list-meta">Aktualisiert: ${escapeHtml(formatTimestamp(item.updated_at))}</div>
                  ${item.summary ? `<pre>${escapeHtml(item.summary)}</pre>` : '<div class="muted">Noch keine Rolling Summary vorhanden.</div>'}
                </div>
              `).join("");
            }

            function renderMemorySummaries(items) {
              const node = document.getElementById("memorySummariesList");
              if (!node) return;
              if (!items || items.length === 0) {
                node.innerHTML = '<div class="muted">Noch keine gespeicherten Summaries.</div>';
                return;
              }
              node.innerHTML = items.map((item) => `
                <div class="list-card">
                  <h3>${escapeHtml(item.session_title || "Session")} (${escapeHtml(item.summary_kind || "rolling")})</h3>
                  <div class="list-meta">Session: ${escapeHtml(item.session_id)} | Modell: ${escapeHtml(item.resolved_model || "-")} | Quelle: ${escapeHtml(item.source_message_count || 0)} Nachrichten</div>
                  <div class="list-meta">Erstellt: ${escapeHtml(formatTimestamp(item.created_at))}</div>
                  <pre>${escapeHtml(item.content || "")}</pre>
                </div>
              `).join("");
            }

            function updateMemoryFilterOptions(sessions) {
              const node = document.getElementById("memorySessionFilter");
              if (!node) return;
              const currentValue = node.value;
              const options = ['<option value="">alle Sessions</option>'];
              for (const item of sessions || []) {
                options.push(`<option value="${escapeHtml(item.id)}">${escapeHtml(item.title || item.id)}</option>`);
              }
              node.innerHTML = options.join("");
              node.value = currentValue;
            }

            async function loadMemoryOverview() {
              try {
                const filterNode = document.getElementById("memorySessionFilter");
                const filterValue = filterNode && filterNode.value ? `&session_id=${encodeURIComponent(filterNode.value)}` : "";
                const res = await fetch(`/api/admin/memory/overview?limit_sessions=12&limit_summaries=16${filterValue}`);
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Memory-Status fehlgeschlagen: ${res.status}`));

                setText("memoryStoreMode", data.store_mode || "-");
                setText("memoryPersistence", data.persistent ? "persistent" : "volatile");
                setText("memorySessionsCount", valueOr(data.sessions_count, "-"));
                setText("memoryMessagesCount", valueOr(data.messages_count, "-"));
                setText("memorySummariesCount", valueOr(data.summaries_count, "-"));
                renderMemorySessions(data.sessions || []);
                renderMemorySummaries(data.summaries || []);
                updateMemoryFilterOptions(data.sessions || []);
                setMemoryStatus(data.persistent ? "Persistenter Memory aktiv." : "Aktuell nur RAM-Memory aktiv.");
              } catch (error) {
                setMemoryStatus(error.message, true);
              }
            }

            function setDatabaseStatus(message, error = false) {
              const node = document.getElementById("databaseStatus");
              if (!node) return;
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            function updateDatabaseView(data) {
              setText("dbStoreMode", data.store_mode || "-");
              setText("dbConnected", data.connected ? "yes" : "no");
              setText("dbSchemaReady", data.schema_ready ? "ready" : "missing");
              setText("dbSessionsCount", valueOr(data.sessions_count, "-"));
              setText("dbMessagesCount", valueOr(data.messages_count, "-"));
              setText("dbUrlRedacted", data.database_url_redacted || "-");

              setText("storageSessionMode", data.store_mode || "-");
              setText("storagePersistence", data.store_mode === "postgres" && data.connected ? "persistent" : "volatile");
            }

            async function loadDatabaseStatus() {
              try {
                const res = await fetch("/internal/admin/database/status");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `DB-Status fehlgeschlagen: ${res.status}`));
                updateDatabaseView(data);
                setDatabaseStatus(data.message || "Datenbankstatus geladen.");
              } catch (error) {
                setDatabaseStatus(error.message, true);
              }
            }

            function renderStorageProfiles(items) {
              const node = document.getElementById("storageProfilesList");
              if (!node) return;
              if (!items || items.length === 0) {
                node.innerHTML = '<div class="muted">Noch keine Storage-Profile vorhanden.</div>';
                return;
              }
              node.innerHTML = items.map((item) => {
                const badge = item.is_active ? '<span class="status" style="display:inline-block;padding:4px 8px;margin:0 0 0 8px;">aktiv</span>' : '';
                const activateForm = item.is_active ? '' : `
                  <form method="post" action="/internal/admin/storage/location/activate-form">
                    <input type="hidden" name="profile_id" value="${escapeHtml(item.id)}">
                    <button class="secondary" type="submit">Aktivieren</button>
                  </form>`;
                return `
                  <div class="list-card">
                    <h3>${escapeHtml(item.name || "Storage")}${badge}</h3>
                    <div class="list-meta">Typ: ${escapeHtml(item.backend_type || "local")} | Pfad: <code>${escapeHtml(item.base_path || "-")}</code></div>
                    <div class="actions">
                      ${activateForm}
                      <form method="post" action="/internal/admin/storage/location/delete-form" onsubmit="return confirm('Storage-Profil wirklich loeschen?');">
                        <input type="hidden" name="profile_id" value="${escapeHtml(item.id)}">
                        <button class="secondary" type="submit">Loeschen</button>
                      </form>
                    </div>
                  </div>`;
              }).join("");
            }

            function renderStorageDocuments(items) {
              const node = document.getElementById("storageDocumentsList");
              if (!node) return;
              if (!items || items.length === 0) {
                node.innerHTML = '<div class="muted">Noch keine Dokumente gespeichert.</div>';
                return;
              }
              node.innerHTML = items.map((item) => `
                <div class="list-card">
                  <h3>${escapeHtml(item.title || item.file_name || "Dokument")}</h3>
                  <div class="list-meta">Datei: ${escapeHtml(item.file_name || "-")} | Storage: ${escapeHtml(item.storage_location_name || "-")} | Typ: ${escapeHtml(item.media_type || "-")}</div>
                  <div class="list-meta">Erfasst: ${escapeHtml(formatTimestamp(item.created_at))}</div>
                  <pre>${escapeHtml(item.text_excerpt || "Kein Textauszug verfuegbar.")}</pre>
                </div>
              `).join("");
            }

            async function loadStorageOverview() {
              try {
                const res = await fetch("/api/admin/storage/overview");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Storage-Status fehlgeschlagen: ${res.status}`));
                const active = data.active_profile || null;
                setText("storageActiveName", active ? active.name : "-");
                setText("storageTarget", active ? active.base_path : "-");
                setText("storageProfilesCount", valueOr(data.profiles_count, 0));
                setText("storageDocumentsCount", valueOr(data.documents_count, 0));
                renderStorageProfiles(data.profiles || []);
                renderStorageDocuments(data.documents || []);

                const node = document.getElementById("storageStatus");
                if (node) {
                  const message = active
                    ? `Aktives Storage-Ziel: ${active.name} (${active.backend_type}). Metadaten und extrahierter Text liegen in PostgreSQL.`
                    : "Noch kein aktives Storage-Ziel vorhanden. Lege zuerst einen lokalen Pfad oder SMB-Mount an.";
                  node.textContent = data.documents_error ? data.documents_error : message;
                  node.style.background = data.documents_error ? "#f9dddd" : "#dff5e7";
                  node.style.color = data.documents_error ? "#942f2f" : "#16231b";
                }
              } catch (error) {
                const node = document.getElementById("storageStatus");
                if (!node) return;
                node.textContent = error.message;
                node.style.background = "#f9dddd";
                node.style.color = "#942f2f";
              }
            }

            function setHomeAssistantStatus(message, error = false) {
              const node = document.getElementById("haStatus");
              if (!node) return;
              node.textContent = message;
              node.style.background = error ? "#f9dddd" : "#dff5e7";
              node.style.color = error ? "#942f2f" : "#16231b";
            }

            function updateHomeAssistantView(data) {
              setText("haConfigured", data.configured ? "yes" : "no");
              setText("haConnected", data.configured && data.message && !String(data.message).toLowerCase().includes("nicht gesetzt") ? "yes" : "no");
              setText("haLocation", data.location_name || "-");
              setValue("haAllowedServicesView", (data.allowed_services || []).join("\\n"));
              setValue("haAllowedPrefixesView", (data.allowed_entity_prefixes || []).join("\\n"));
            }

            async function loadHomeAssistantStatus() {
              try {
                const res = await fetch("/api/admin/home-assistant/status");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `HA-Status fehlgeschlagen: ${res.status}`));
                updateHomeAssistantView(data);
                const connected = data.configured && !String(data.message || "").toLowerCase().includes("nicht gesetzt");
                setHomeAssistantStatus(data.message || "Home Assistant bereit.", !connected && data.configured);
              } catch (error) {
                setHomeAssistantStatus(error.message, true);
              }
            }

            async function testHomeAssistantConnection() {
              try {
                setHomeAssistantStatus("Home-Assistant-Verbindung wird getestet...");
                const payload = collectConfigPayload();
                await persistConfig(payload);
                await loadDashboard();
                await loadHomeAssistantStatus();
              } catch (error) {
                setHomeAssistantStatus(error.message, true);
              }
            }

            function collectConfigPayload() {
              const payload = {};
              const currentConfig = window.currentConfig || {};
              for (const key in currentConfig) {
                if (Object.prototype.hasOwnProperty.call(currentConfig, key)) {
                  payload[key] = currentConfig[key];
                }
              }
              for (const keyIndex in configFields) {
                const key = configFields[keyIndex];
                const node = document.getElementById(key);
                if (node) {
                  payload[key] = node.value.trim();
                }
              }
              payload.LLAMACPP_TIMEOUT_SECONDS = payload.LLAMACPP_TIMEOUT_SECONDS || "60.0";
              payload.CONTEXT_CHARS_PER_TOKEN = payload.CONTEXT_CHARS_PER_TOKEN || "4.0";
              payload.MI50_SSH_PORT = payload.MI50_SSH_PORT || "22";
              payload.MI50_RESTART_COMMAND = payload.MI50_RESTART_COMMAND || "sudo systemctl restart kai";
              payload.MI50_STATUS_COMMAND = payload.MI50_STATUS_COMMAND || "systemctl status kai --no-pager";
              payload.MI50_LOGS_COMMAND = payload.MI50_LOGS_COMMAND || "journalctl -u kai -n 80 --no-pager";
              payload.MI50_ROCM_SMI_COMMAND = payload.MI50_ROCM_SMI_COMMAND || "rocm-smi --showtemp --showpower --showmemuse --json";
              payload.HOME_ASSISTANT_TIMEOUT_SECONDS = payload.HOME_ASSISTANT_TIMEOUT_SECONDS || "10.0";
              return payload;
            }

            async function persistConfig(payload) {
              const res = await fetch("/internal/admin/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
              });
              let data = {};
              try {
                data = await res.json();
              } catch (_error) {
                data = {};
              }
              if (!res.ok) {
                throw new Error(errorMessage(data, `Speichern fehlgeschlagen: ${res.status}`));
              }
              window.currentConfig = data;
              return data;
            }

            async function saveHomeAssistantConfig() {
              try {
                setHomeAssistantStatus("Home-Assistant-Konfiguration wird gespeichert...");
                const payload = collectConfigPayload();
                await persistConfig(payload);
                await loadDashboard();
                await loadHomeAssistantStatus();
                setHomeAssistantStatus("Home-Assistant-Konfiguration gespeichert.");
              } catch (error) {
                setHomeAssistantStatus(error.message, true);
              }
            }

            async function loadHomeAssistantEntities() {
              try {
                const domain = document.getElementById("haEntityDomain").value;
                const query = domain ? `?domain=${encodeURIComponent(domain)}` : "";
                const res = await fetch(`/api/admin/home-assistant/entities${query}`);
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `HA-Entities fehlgeschlagen: ${res.status}`));
                const lines = (data.entities || []).map((item) => `${item.entity_id} | ${item.state} | ${item.friendly_name || ""}`);
                setValue("haEntities", lines.join("\\n"));
                setHomeAssistantStatus(`${data.count || 0} erlaubte Entities geladen.`);
              } catch (error) {
                setHomeAssistantStatus(error.message, true);
              }
            }

            async function runHomeAssistantAction() {
              try {
                const payload = {
                  domain: document.getElementById("haActionDomain").value.trim(),
                  service: document.getElementById("haActionService").value.trim(),
                  entity_id: document.getElementById("haActionEntityId").value.trim() || null,
                  service_data: {},
                };
                const rawData = document.getElementById("haActionData").value.trim();
                if (rawData) {
                  payload.service_data = JSON.parse(rawData);
                }
                const res = await fetch("/api/admin/home-assistant/action", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `HA-Aktion fehlgeschlagen: ${res.status}`));
                setValue("haActionResult", JSON.stringify(data, null, 2));
                setHomeAssistantStatus(`Aktion ${payload.domain}.${payload.service} erfolgreich.`);
              } catch (error) {
                setHomeAssistantStatus(error.message, true);
              }
            }

            async function saveConfig(event) {
              event.preventDefault();
              try {
                const payload = collectConfigPayload();
                await persistConfig(payload);
                updateDashboardConfigFacts(window.currentConfig || payload);
                await loadDashboard();
                await loadSettings();
                await loadDatabaseStatus();
                await loadHomeAssistantStatus();
                setSettingsStatus("Gespeichert. Neue Werte gelten fuer neue Requests.");
              } catch (error) {
                setSettingsStatus(error.message, true);
              }
            }

            async function saveDatabaseConfig(event) {
              if (event && event.preventDefault) event.preventDefault();
              try {
                setDatabaseStatus("Datenbank-Konfiguration wird gespeichert...");
                const payload = collectConfigPayload();
                await persistConfig(payload);
                await loadDashboard();
                await loadDatabaseStatus();
                setDatabaseStatus("Datenbank-Konfiguration gespeichert.");
              } catch (error) {
                setDatabaseStatus(error.message, true);
              }
            }

            async function testDatabaseConnection() {
              try {
                setDatabaseStatus("Datenbank-Verbindung wird getestet...");
                const payload = collectConfigPayload();
                await persistConfig(payload);
                await loadDashboard();
                await loadDatabaseStatus();
              } catch (error) {
                setDatabaseStatus(error.message, true);
              }
            }

            async function initializeDatabase() {
              setDatabaseStatus("Schema wird initialisiert...");
              const res = await fetch("/internal/admin/database/init", { method: "POST" });
              const data = await res.json();
              if (!res.ok) {
                setDatabaseStatus(errorMessage(data, `Schema-Init fehlgeschlagen: ${res.status}`), true);
                return;
              }
              updateDatabaseView(data);
              setDatabaseStatus("Schema initialisiert. PostgreSQL ist bereit.");
            }

            async function loadContinueConfig() {
              const res = await fetch("/internal/admin/continue-config");
              if (!res.ok) {
                setSettingsStatus(`Continue YAML fehlgeschlagen: ${res.status}`, true);
                return;
              }
              const data = await res.json();
              document.getElementById("continueYaml").value = data.yaml;
              setSettingsStatus("Continue YAML geladen.");
            }

            async function loadSettings() {
              try {
                const res = await fetch("/internal/admin/config");
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessage(data, `Settings fehlgeschlagen: ${res.status}`));
                window.currentConfig = data;
                for (const key of configFields) {
                  const node = document.getElementById(key);
                  if (node) node.value = data[key] || "";
                }
                updateDashboardConfigFacts(data);
                setSettingsStatus("Settings geladen.");
              } catch (error) {
                setSettingsStatus(error.message, true);
              }
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
                statusNode.textContent = errorMessage(data, `Ops-Fehler (${res.status})`);
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
                statusNode.textContent = errorMessage(data, `Ops-Fehler (${res.status})`);
                statusNode.style.background = "#f9dddd";
                outputNode.textContent = JSON.stringify(data, null, 2);
                return;
              }
              statusNode.textContent = `${target}: ${command} erfolgreich`;
              statusNode.style.background = "#dff5e7";
              outputNode.textContent = data.output || JSON.stringify(data, null, 2);
            }

            function applyFaceFormDefaults() {
              const form = document.querySelector('form[action="/internal/admin/device/face-apply-form"]');
              if (!form) return;
              const defaults = {
                FACE_STATE: "__DEVICE_FACE_STATE__",
                FACE_RENDER_MODE: "__DEVICE_FACE_RENDER_MODE__",
                FACE_VARIANT: "__DEVICE_FACE_VARIANT__",
                FACE_FACE_COLOR: "__DEVICE_FACE_FACE_COLOR__",
                FACE_EYE_SHAPE: "__DEVICE_FACE_EYE_SHAPE__",
                FACE_EYE_SPACING: "__DEVICE_FACE_EYE_SPACING__"
              };
              for (const [name, value] of Object.entries(defaults)) {
                const node = form.querySelector(`[name="${name}"]`);
                if (node && value) {
                  node.value = value;
                }
              }
            }

            loadDashboard();
            loadHeaderTelemetry();
            window.setInterval(loadHeaderTelemetry, 500);
            loadSettings();
            loadMemoryOverview();
            loadDatabaseStatus();
            loadStorageOverview();
            loadHomeAssistantStatus();
            loadContinueConfig();
            loadOpsCatalog();
            loadCustomMcpTools();
            loadMcpTools();
            applyFaceFormDefaults();
          </script>
        </body>
        </html>
        """
    )
    replacements = {
        "__USERNAME__": escape(username),
        "__NAV_DASHBOARD__": "active" if active_tab == "dashboard" else "",
        "__NAV_SETTINGS__": "active" if active_tab == "settings" else "",
        "__NAV_SKILLS__": "active" if active_tab == "skills" else "",
        "__NAV_CHAT__": "active" if active_tab == "chat" else "",
        "__NAV_MEMORY__": "active" if active_tab == "memory" else "",
        "__NAV_DATABASE__": "active" if active_tab == "database" else "",
        "__NAV_HOME_ASSISTANT__": "active" if active_tab == "home-assistant" else "",
        "__NAV_STORAGE__": "active" if active_tab == "storage" else "",
        "__NAV_OPS__": "active" if active_tab == "ops" else "",
        "__NAV_DEVICES__": "active" if active_tab == "devices" else "",
        "__PANEL_DASHBOARD__": "active" if active_tab == "dashboard" else "",
        "__PANEL_SETTINGS__": "active" if active_tab == "settings" else "",
        "__PANEL_SKILLS__": "active" if active_tab == "skills" else "",
        "__PANEL_CHAT__": "active" if active_tab == "chat" else "",
        "__PANEL_MEMORY__": "active" if active_tab == "memory" else "",
        "__PANEL_DATABASE__": "active" if active_tab == "database" else "",
        "__PANEL_HOME_ASSISTANT__": "active" if active_tab == "home-assistant" else "",
        "__PANEL_STORAGE__": "active" if active_tab == "storage" else "",
        "__PANEL_OPS__": "active" if active_tab == "ops" else "",
        "__PANEL_DEVICES__": "active" if active_tab == "devices" else "",
        "__DASHBOARD_STATUS__": escape(initial_data.get("dashboard_status", "-")),
        "__SETTINGS_STATUS__": escape(initial_data.get("settings_status", "-")),
        "__SKILLS_STATUS__": escape(initial_data.get("skills_status", "-")),
        "__GATEWAY_STATE__": escape(initial_data.get("gateway_state", "-")),
        "__GATEWAY_INFO__": escape(initial_data.get("gateway_info", "-")),
        "__BACKEND_STATE__": escape(initial_data.get("backend_state", "-")),
        "__BACKEND_INFO__": escape(initial_data.get("backend_info", "-")),
        "__REQUESTS_VALUE__": escape(initial_data.get("requests_value", "-")),
        "__BACKEND_CALLS_VALUE__": escape(initial_data.get("backend_calls_value", "-")),
        "__UPTIME_VALUE__": escape(initial_data.get("uptime_value", "-")),
        "__AVG_REQUEST_VALUE__": escape(initial_data.get("avg_request_value", "-")),
        "__GPU_TEMP_VALUE__": escape(initial_data.get("gpu_temp_value", "-")),
        "__GPU_POWER_VALUE__": escape(initial_data.get("gpu_power_value", "-")),
        "__GPU_VRAM_VALUE__": escape(initial_data.get("gpu_vram_value", "-")),
        "__HEADER_CPU_USAGE_VALUE__": escape(initial_data.get("header_cpu_usage_value", "-")),
        "__HEADER_CPU_TEMP_VALUE__": escape(initial_data.get("header_cpu_temp_value", "-")),
        "__HEADER_GPU_USAGE_VALUE__": escape(initial_data.get("header_gpu_usage_value", "-")),
        "__HEADER_GPU_TEMP_VALUE__": escape(initial_data.get("header_gpu_temp_value", "-")),
        "__HEADER_GPU_POWER_VALUE__": escape(initial_data.get("header_gpu_power_value", "-")),
        "__HEADER_GPU_VRAM_VALUE__": escape(initial_data.get("header_gpu_vram_value", "-")),
        "__DASHBOARD_PUBLIC_MODEL__": escape(initial_data.get("dashboard_public_model", "-")),
        "__DASHBOARD_BACKEND_MODEL__": escape(initial_data.get("dashboard_backend_model", "-")),
        "__DASHBOARD_BACKEND_PROFILE__": escape(initial_data.get("dashboard_backend_profile", "-")),
        "__DASHBOARD_ADMIN_MODE__": escape(initial_data.get("dashboard_admin_mode", "-")),
        "__DASHBOARD_BACKEND_BASE_URL__": escape(initial_data.get("dashboard_backend_base_url", "-")),
        "__DASHBOARD_CONTEXT_WINDOW__": escape(initial_data.get("dashboard_context_window", "-")),
        "__DASHBOARD_RESPONSE_RESERVE__": escape(initial_data.get("dashboard_response_reserve", "-")),
        "__DASHBOARD_DEFAULT_MAX_TOKENS__": escape(initial_data.get("dashboard_default_max_tokens", "-")),
        "__DASHBOARD_ROUTING_THRESHOLDS__": escape(initial_data.get("dashboard_routing_thresholds", "-")),
        "__DASHBOARD_DB_MODE__": escape(initial_data.get("dashboard_db_mode", "-")),
        "__DASHBOARD_STORAGE_ACTIVE__": escape(initial_data.get("dashboard_storage_active", "-")),
        "__DASHBOARD_HA_SUMMARY__": escape(initial_data.get("dashboard_ha_summary", "-")),
        "__DASHBOARD_MCP_SKILLS_VALUE__": escape(initial_data.get("dashboard_mcp_skills_value", "0 / 0")),
        "__DATABASE_STATUS__": escape(initial_data.get("database_status", "-")),
        "__DB_STORE_MODE__": escape(initial_data.get("db_store_mode", "-")),
        "__DB_CONNECTED__": escape(initial_data.get("db_connected", "-")),
        "__DB_SCHEMA_READY__": escape(initial_data.get("db_schema_ready", "-")),
        "__DB_SESSIONS_COUNT__": escape(initial_data.get("db_sessions_count", "-")),
        "__DB_MESSAGES_COUNT__": escape(initial_data.get("db_messages_count", "-")),
        "__DB_URL_REDACTED__": escape(initial_data.get("db_url_redacted", "-")),
        "__DATABASE_URL_VALUE__": escape(initial_data.get("database_url_value", "")),
        "__DATABASE_PROFILES_HTML__": initial_data.get("database_profiles_html", ""),
        "__MEMORY_STATUS__": escape(initial_data.get("memory_status", "-")),
        "__MEMORY_STORE_MODE__": escape(initial_data.get("memory_store_mode", "-")),
        "__MEMORY_PERSISTENCE__": escape(initial_data.get("memory_persistence", "-")),
        "__MEMORY_SESSIONS_COUNT__": escape(initial_data.get("memory_sessions_count", "-")),
        "__MEMORY_MESSAGES_COUNT__": escape(initial_data.get("memory_messages_count", "-")),
        "__MEMORY_SUMMARIES_COUNT__": escape(initial_data.get("memory_summaries_count", "-")),
        "__STORAGE_STATUS__": escape(initial_data.get("storage_status", "-")),
        "__STORAGE_SESSION_MODE__": escape(initial_data.get("storage_session_mode", "-")),
        "__STORAGE_PERSISTENCE__": escape(initial_data.get("storage_persistence", "-")),
        "__STORAGE_TARGET__": escape(initial_data.get("storage_target", "-")),
        "__STORAGE_ACTIVE_NAME__": escape(initial_data.get("storage_active_name", "-")),
        "__STORAGE_PROFILES_COUNT__": escape(initial_data.get("storage_profiles_count", "0")),
        "__STORAGE_DOCUMENTS_COUNT__": escape(initial_data.get("storage_documents_count", "0")),
        "__STORAGE_PROFILES_HTML__": initial_data.get("storage_profiles_html", ""),
        "__STORAGE_DOCUMENTS_HTML__": initial_data.get("storage_documents_html", ""),
        "__STORAGE_UPLOAD_OPTIONS_HTML__": initial_data.get("storage_upload_options_html", '<option value="">aktives Profil verwenden</option>'),
        "__MCP_TOOLS_COUNT__": escape(initial_data.get("mcp_tools_count", "0")),
        "__MCP_CUSTOM_TOOLS_COUNT__": escape(initial_data.get("mcp_custom_tools_count", "0")),
        "__MCP_CUSTOM_TOOLS_HTML__": initial_data.get("mcp_custom_tools_html", '<div class="muted">Noch keine Custom-MCP-Tools gespeichert.</div>'),
        "__DEVICE_STATUS__": escape(initial_data.get("device_status", "-")),
        "__DEVICE_PROFILES_HTML__": initial_data.get("device_profiles_html", ""),
        "__DEVICE_PROFILE_FORM_ID__": escape(initial_data.get("device_profile_form_id", "")),
        "__DEVICE_PROFILE_FORM_NAME__": escape(initial_data.get("device_profile_form_name", "")),
        "__DEVICE_PROFILE_FORM_GATEWAY_BASE_URL__": escape(initial_data.get("device_profile_form_gateway_base_url", "")),
        "__DEVICE_PROFILE_FORM_DEVICE_TOKEN__": escape(initial_data.get("device_profile_form_device_token", "")),
        "__DEVICE_PROFILE_FORM_SSH_HOST__": escape(initial_data.get("device_profile_form_ssh_host", "")),
        "__DEVICE_PROFILE_FORM_SSH_USER__": escape(initial_data.get("device_profile_form_ssh_user", "")),
        "__DEVICE_PROFILE_FORM_SSH_PORT__": escape(initial_data.get("device_profile_form_ssh_port", "22")),
        "__DEVICE_PROFILE_FORM_SSH_PASSWORD__": escape(initial_data.get("device_profile_form_ssh_password", "")),
        "__DEVICE_PROFILE_FORM_REMOTE_DIR__": escape(initial_data.get("device_profile_form_remote_dir", "~/kai-pi")),
        "__DEVICE_PROFILE_FORM_SSH_ROOT_PREFIX__": escape(initial_data.get("device_profile_form_ssh_root_prefix", "sudo -n")),
        "__DEVICE_PROFILE_FORM_NOTES__": escape(initial_data.get("device_profile_form_notes", "")),
        "__DEVICE_ACTIVE_TOKEN_REDACTED__": escape(initial_data.get("device_active_token_redacted", "-")),
        "__DEVICE_BOOTSTRAP_PREVIEW__": escape(initial_data.get("device_bootstrap_preview", "Noch kein Device-Profil ausgewaehlt.")),
        "__DEVICE_FACE_PROFILE_OPTIONS_HTML__": initial_data.get("device_face_profile_options_html", '<option value="">zuerst Device-Profil speichern</option>'),
        "__DEVICE_FACE_STYLE_NAME__": escape(initial_data.get("device_face_style_name", "gateway_idle")),
        "__DEVICE_FACE_STATE__": escape(initial_data.get("device_face_state", "idle")),
        "__DEVICE_FACE_RENDER_MODE__": escape(initial_data.get("device_face_render_mode", "vector")),
        "__DEVICE_FACE_SPRITE_PACK__": escape(initial_data.get("device_face_sprite_pack", "robot_v1")),
        "__DEVICE_FACE_VARIANT__": escape(initial_data.get("device_face_variant", "custom")),
        "__DEVICE_FACE_FACE_COLOR__": escape(initial_data.get("device_face_face_color", "black")),
        "__DEVICE_FACE_EYE_SHAPE__": escape(initial_data.get("device_face_eye_shape", "round")),
        "__DEVICE_FACE_EYE_SPACING__": escape(initial_data.get("device_face_eye_spacing", "normal")),
        "__DEVICE_FACE_IRIS_COLOR__": escape(initial_data.get("device_face_iris_color", "#59c7ff")),
        "__HA_STATUS__": escape(initial_data.get("ha_status", "-")),
        "__HA_CONFIGURED__": escape(initial_data.get("ha_configured", "-")),
        "__HA_CONNECTED__": escape(initial_data.get("ha_connected", "-")),
        "__HA_LOCATION__": escape(initial_data.get("ha_location", "-")),
        "__BACKEND_PROFILES_HTML__": initial_data.get("backend_profiles_html", ""),
        "__BACKEND_PROFILE_FORM_ID__": escape(initial_data.get("backend_profile_form_id", "")),
        "__BACKEND_PROFILE_FORM_NAME__": escape(initial_data.get("backend_profile_form_name", "")),
        "__BACKEND_PROFILE_FORM_PUBLIC_MODEL_NAME__": escape(initial_data.get("backend_profile_form_public_model_name", "")),
        "__BACKEND_PROFILE_FORM_BACKEND_MODEL_NAME__": escape(initial_data.get("backend_profile_form_backend_model_name", "")),
        "__BACKEND_PROFILE_FORM_BASE_URL__": escape(initial_data.get("backend_profile_form_base_url", "")),
        "__BACKEND_PROFILE_FORM_CONTEXT_WINDOW__": escape(initial_data.get("backend_profile_form_context_window", "")),
        "__BACKEND_PROFILE_FORM_RESPONSE_RESERVE__": escape(initial_data.get("backend_profile_form_response_reserve", "")),
        "__BACKEND_PROFILE_FORM_DEFAULT_MAX_TOKENS__": escape(initial_data.get("backend_profile_form_default_max_tokens", "")),
        "__BACKEND_PROFILE_FORM_NGL_LAYERS__": escape(initial_data.get("backend_profile_form_ngl_layers", "")),
        "__BACKEND_PROFILE_FORM_SERVICE_NAME__": escape(initial_data.get("backend_profile_form_service_name", "")),
        "__BACKEND_PROFILE_FORM_ACTIVATE_COMMAND__": escape(initial_data.get("backend_profile_form_activate_command", "")),
        "__BACKEND_PROFILE_FORM_STATUS_COMMAND__": escape(initial_data.get("backend_profile_form_status_command", "")),
        "__BACKEND_PROFILE_FORM_LOGS_COMMAND__": escape(initial_data.get("backend_profile_form_logs_command", "")),
        "__BACKEND_PROFILE_PREVIEW__": escape(initial_data.get("backend_profile_preview", "")),
    }
    for key, value in initial_data.items():
        if key.startswith("cfg_"):
            replacements[f"__CFG_{key[4:]}__"] = escape(str(value))
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html
