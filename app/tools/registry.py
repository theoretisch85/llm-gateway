from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.core.roles import ROLE_ADMIN, ROLE_DEVICE, ROLE_USER
from app.services.backend_control import run_ops_command
from app.services.calm_news import CalmNewsClient
from app.services.gmail import GmailClient, classify_gmail_messages
from app.services.home_assistant import HomeAssistantClient
from app.services.math_service import calculate_expression
from app.services.mcp_custom_tools import delete_custom_mcp_tool, list_custom_mcp_tools, save_custom_mcp_tool
from app.services.storage_library import get_document_contexts, list_documents


ToolHandler = Callable[[Settings, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler
    allowed_roles: tuple[str, ...] = (ROLE_ADMIN, ROLE_DEVICE)

    @property
    def requires_admin(self) -> bool:
        return ROLE_DEVICE not in self.allowed_roles


def _tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    handler: ToolHandler,
    allowed_roles: tuple[str, ...] = (ROLE_ADMIN, ROLE_DEVICE),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        handler=handler,
        allowed_roles=allowed_roles,
    )


async def _ha_list_entities(settings: Settings, args: dict[str, Any]) -> Any:
    domain = str(args.get("domain") or "").strip() or None
    limit = int(args.get("limit") or 50)
    client = HomeAssistantClient(settings)
    return await client.list_entities(domain=domain, limit=limit)


async def _ha_call(settings: Settings, args: dict[str, Any]) -> Any:
    domain = str(args.get("domain") or "").strip()
    service = str(args.get("service") or "").strip()
    if not domain or not service:
        raise ValueError("domain und service sind erforderlich.")

    entity_id = args.get("entity_id")
    service_data = args.get("service_data")

    client = HomeAssistantClient(settings)
    return await client.call_service(
        domain=domain,
        service=service,
        entity_id=str(entity_id) if entity_id else None,
        service_data=service_data if isinstance(service_data, dict) else None,
    )


async def _news_get_latest(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.get_latest(params=_news_query_params(args))


async def _news_get_calm(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.get_calm(params=_news_query_params(args))


async def _news_get_positive(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.get_positive(params=_news_query_params(args))


async def _news_get_relevant(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.get_relevant(params=_news_query_params(args))


async def _news_get_article(settings: Settings, args: dict[str, Any]) -> Any:
    article_id = str(args.get("id") or args.get("article_id") or "").strip()
    client = CalmNewsClient(settings)
    return await client.get_article(article_id, params=_news_query_params(args))


async def _news_get_system_status(settings: Settings, args: dict[str, Any]) -> Any:
    _ = args
    client = CalmNewsClient(settings)
    return await client.get_system_status()


async def _news_get_sources(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.get_sources(params=_news_query_params(args, include_filters=False, include_source=True))


async def _news_trigger_ingest(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.trigger_ingest(params=_news_query_params(args, include_filters=False, include_source=True))


async def _news_get_last_ingest(settings: Settings, args: dict[str, Any]) -> Any:
    client = CalmNewsClient(settings)
    return await client.get_last_ingest(params=_news_query_params(args, include_filters=False, include_source=True))


async def _news_set_source_status(settings: Settings, args: dict[str, Any]) -> Any:
    source_id = str(args.get("id") or args.get("source_id") or "").strip()
    client = CalmNewsClient(settings)
    return await client.set_source_status(source_id, payload=_news_source_payload(args))


async def _storage_list(settings: Settings, args: dict[str, Any]) -> Any:
    limit = int(args.get("limit") or 30)
    return await list_documents(settings, limit=limit)


async def _storage_get(settings: Settings, args: dict[str, Any]) -> Any:
    raw_ids = args.get("document_ids") or args.get("document_id") or []
    if isinstance(raw_ids, str):
        ids = [raw_ids]
    elif isinstance(raw_ids, list):
        ids = [str(item) for item in raw_ids if item]
    else:
        ids = []
    return await get_document_contexts(settings, ids)


async def _gateway_ops(settings: Settings, args: dict[str, Any]) -> Any:
    _ = settings
    command = str(args.get("command") or "").strip().lower()
    target = str(args.get("target") or "").strip().lower() or "gateway"
    if "." in command and "target" not in args:
        split_target, split_command = command.split(".", 1)
        if split_target in {"gateway", "kai"} and split_command:
            target = split_target
            command = split_command

    if not command:
        raise ValueError("command ist erforderlich.")

    try:
        return run_ops_command(target, command)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


async def _mail_list_recent(settings: Settings, args: dict[str, Any]) -> Any:
    client = GmailClient(settings)
    label_ids = args.get("label_ids") or []
    if isinstance(label_ids, str):
        parsed_labels = [item.strip() for item in label_ids.split(",") if item.strip()]
    elif isinstance(label_ids, list):
        parsed_labels = [str(item).strip() for item in label_ids if str(item).strip()]
    else:
        parsed_labels = []
    return await client.list_recent(
        limit=int(args.get("limit") or 20),
        query=str(args.get("q") or "").strip() or None,
        label_ids=parsed_labels,
        include_spam_trash=bool(args.get("include_spam_trash")),
    )


async def _mail_get_message(settings: Settings, args: dict[str, Any]) -> Any:
    client = GmailClient(settings)
    return await client.get_message(str(args.get("message_id") or args.get("id") or "").strip())


async def _mail_classify_recent(settings: Settings, args: dict[str, Any]) -> Any:
    client = GmailClient(settings)
    label_ids = args.get("label_ids") or []
    if isinstance(label_ids, str):
        parsed_labels = [item.strip() for item in label_ids.split(",") if item.strip()]
    elif isinstance(label_ids, list):
        parsed_labels = [str(item).strip() for item in label_ids if str(item).strip()]
    else:
        parsed_labels = []
    recent = await client.list_recent(
        limit=int(args.get("limit") or 12),
        query=str(args.get("q") or "").strip() or None,
        label_ids=parsed_labels,
        include_spam_trash=bool(args.get("include_spam_trash")),
    )
    classified = await classify_gmail_messages(settings, recent.get("messages") or [])
    return {
        **recent,
        "messages": classified.get("messages") or [],
        "classification_model": classified.get("model") or settings.effective_fast_model.public_name,
    }


async def _math_calculate(settings: Settings, args: dict[str, Any]) -> Any:
    _ = settings
    expression = str(args.get("expression") or "").strip()
    return calculate_expression(expression)


async def _custom_tool_list(settings: Settings, args: dict[str, Any]) -> Any:
    _ = settings, args
    return list_custom_mcp_tools()


async def _custom_tool_save(settings: Settings, args: dict[str, Any]) -> Any:
    _ = settings
    name = str(args.get("name") or "").strip().lower()
    if name in get_builtin_tool_names():
        raise ValueError("Name ist reserviert (builtin MCP-Tool).")

    try:
        return save_custom_mcp_tool(
            name=name,
            description=str(args.get("description") or ""),
            target=str(args.get("target") or ""),
            command=str(args.get("command") or ""),
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


async def _custom_tool_delete(settings: Settings, args: dict[str, Any]) -> Any:
    _ = settings
    name = str(args.get("name") or "").strip().lower()
    if not name:
        raise ValueError("name ist erforderlich.")
    if name in get_builtin_tool_names():
        raise ValueError("Builtin MCP-Tools koennen nicht geloescht werden.")

    try:
        return delete_custom_mcp_tool(name)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def _custom_ops_tool_handler(target: str, command: str) -> ToolHandler:
    async def _handler(settings: Settings, args: dict[str, Any]) -> Any:
        _ = settings, args
        try:
            return run_ops_command(target, command)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    return _handler


def _news_query_params(
    args: dict[str, Any],
    *,
    include_filters: bool = True,
    include_source: bool = True,
) -> dict[str, Any]:
    params: dict[str, Any] = {}

    if args.get("limit") is not None:
        params["limit"] = int(args["limit"])
    if args.get("tone") is not None:
        params["tone"] = str(args["tone"]).strip()
    if include_source and args.get("source") is not None:
        params["source"] = str(args["source"]).strip()
    if include_filters and args.get("min_relevance") is not None:
        params["min_relevance"] = int(args["min_relevance"])
    if include_filters and args.get("max_stress") is not None:
        params["max_stress"] = int(args["max_stress"])

    return {key: value for key, value in params.items() if value != ""}


def _news_source_payload(args: dict[str, Any]) -> dict[str, Any]:
    raw_payload = args.get("payload")
    if isinstance(raw_payload, dict) and raw_payload:
        return raw_payload

    payload: dict[str, Any] = {}
    if "enabled" in args and args.get("enabled") is not None:
        payload["enabled"] = _coerce_bool(args.get("enabled"))
    if "status" in args and args.get("status") is not None:
        payload["status"] = str(args.get("status")).strip()

    payload = {key: value for key, value in payload.items() if value != ""}
    if not payload:
        raise ValueError("payload oder enabled/status ist erforderlich.")
    return payload


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError("enabled muss true oder false sein.")


def _builtin_tools() -> list[ToolDefinition]:
    return [
        _tool(
            name="ha.entities",
            description="Liste erlaubter Home-Assistant-Entities.",
            input_schema={"type": "object", "properties": {"domain": {"type": "string"}, "limit": {"type": "integer"}}},
            output_schema={"type": "array"},
            handler=_ha_list_entities,
        ),
        _tool(
            name="ha.call",
            description="Fuehrt einen freigegebenen Home-Assistant-Service aus.",
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "service": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "service_data": {"type": "object"},
                },
                "required": ["domain", "service"],
            },
            output_schema={"type": "object"},
            handler=_ha_call,
        ),
        _tool(
            name="news.get_latest",
            description="Laedt die neuesten calm_news-Eintraege ueber den Gateway.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "tone": {"type": "string"},
                    "source": {"type": "string"},
                    "min_relevance": {"type": "integer"},
                    "max_stress": {"type": "integer"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_get_latest,
        ),
        _tool(
            name="news.get_calm",
            description="Laedt calm kuratierte Nachrichten aus calm_news ueber den Gateway.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "tone": {"type": "string"},
                    "source": {"type": "string"},
                    "min_relevance": {"type": "integer"},
                    "max_stress": {"type": "integer"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_get_calm,
        ),
        _tool(
            name="news.get_positive",
            description="Laedt positive Nachrichten aus calm_news ueber den Gateway.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "tone": {"type": "string"},
                    "source": {"type": "string"},
                    "min_relevance": {"type": "integer"},
                    "max_stress": {"type": "integer"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_get_positive,
        ),
        _tool(
            name="news.get_relevant",
            description="Laedt relevante Nachrichten aus calm_news ueber den Gateway.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "tone": {"type": "string"},
                    "source": {"type": "string"},
                    "min_relevance": {"type": "integer"},
                    "max_stress": {"type": "integer"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_get_relevant,
        ),
        _tool(
            name="news.get_article",
            description="Laedt einen einzelnen calm_news-Artikel ueber seine ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tone": {"type": "string"},
                },
                "required": ["id"],
            },
            output_schema={"type": "object"},
            handler=_news_get_article,
        ),
        _tool(
            name="news.get_system_status",
            description="Laedt den Systemstatus der calm_news-App.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
            handler=_news_get_system_status,
        ),
        _tool(
            name="news.get_sources",
            description="Listet konfigurierte calm_news-Quellen.",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_get_sources,
        ),
        _tool(
            name="news.trigger_ingest",
            description="Startet einen calm_news-Ingest ueber den Gateway.",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_trigger_ingest,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="news.get_last_ingest",
            description="Laedt den letzten bekannten calm_news-Ingest-Status.",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            handler=_news_get_last_ingest,
        ),
        _tool(
            name="news.set_source_status",
            description="Aendert den Status einer calm_news-Quelle.",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "status": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["id"],
            },
            output_schema={"type": "object"},
            handler=_news_set_source_status,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="mail.list_recent",
            description="Listet aktuelle Gmail-Nachrichten inkl. Header-Hinweisen wie List-Unsubscribe.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "q": {"type": "string"},
                    "label_ids": {"type": ["array", "string"], "items": {"type": "string"}},
                    "include_spam_trash": {"type": "boolean"},
                },
            },
            output_schema={"type": "object"},
            handler=_mail_list_recent,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="mail.get_message",
            description="Laedt eine einzelne Gmail-Nachricht inkl. Textinhalt.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "id": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            handler=_mail_get_message,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="mail.classify_recent",
            description="Laedt aktuelle Gmail-Nachrichten und klassifiziert sie ueber das bestehende LLM.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "q": {"type": "string"},
                    "label_ids": {"type": ["array", "string"], "items": {"type": "string"}},
                    "include_spam_trash": {"type": "boolean"},
                },
            },
            output_schema={"type": "object"},
            handler=_mail_classify_recent,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="math.calculate",
            description="Berechnet deterministisch mathematische Ausdruecke.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                },
                "required": ["expression"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "result": {"type": ["number", "integer"]},
                },
                "required": ["expression", "result"],
            },
            handler=_math_calculate,
            allowed_roles=(ROLE_ADMIN, ROLE_DEVICE, ROLE_USER),
        ),
        _tool(
            name="storage.list",
            description="Listet die letzten gespeicherten Dokumente.",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            output_schema={"type": "array"},
            handler=_storage_list,
        ),
        _tool(
            name="storage.get",
            description="Laedt gespeicherte Dokumente inkl. extrahiertem Text.",
            input_schema={"type": "object", "properties": {"document_ids": {"type": "array", "items": {"type": "string"}}}},
            output_schema={"type": "array"},
            handler=_storage_get,
        ),
        _tool(
            name="gateway.ops",
            description="Fuehrt freigegebene Gateway-Ops-Presets aus.",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "gateway oder kai"},
                    "command": {"type": "string", "description": "z. B. status, skills, install_htop"},
                },
                "required": ["command"],
            },
            output_schema={"type": "object"},
            handler=_gateway_ops,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="gateway.custom_tool.list",
            description="Listet gespeicherte Custom-MCP-Tools.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "array"},
            handler=_custom_tool_list,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="gateway.custom_tool.save",
            description="Speichert ein Custom-MCP-Tool (Name -> target/command).",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "target": {"type": "string"},
                    "command": {"type": "string"},
                },
                "required": ["name", "target", "command"],
            },
            output_schema={"type": "object"},
            handler=_custom_tool_save,
            allowed_roles=(ROLE_ADMIN,),
        ),
        _tool(
            name="gateway.custom_tool.delete",
            description="Loescht ein gespeichertes Custom-MCP-Tool per Name.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
            output_schema={"type": "object"},
            handler=_custom_tool_delete,
            allowed_roles=(ROLE_ADMIN,),
        ),
    ]


def get_builtin_tool_names() -> set[str]:
    return {item.name for item in _builtin_tools()}


def get_registered_tools() -> list[ToolDefinition]:
    tools = _builtin_tools()
    reserved_names = {item.name for item in tools}

    for item in list_custom_mcp_tools():
        name = str(item.get("name") or "").strip().lower()
        description = str(item.get("description") or "").strip() or f"Custom Ops Tool: {item.get('target')}.{item.get('command')}"
        target = str(item.get("target") or "").strip().lower()
        command = str(item.get("command") or "").strip().lower()
        if not name or name in reserved_names or not target or not command:
            continue

        tools.append(
            _tool(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object"},
                handler=_custom_ops_tool_handler(target, command),
                allowed_roles=(ROLE_ADMIN,),
            )
        )

    return tools


def find_registered_tool(tool_name: str) -> ToolDefinition | None:
    normalized = (tool_name or "").strip()
    for tool in get_registered_tools():
        if tool.name == normalized:
            return tool
    return None


def is_role_allowed(tool: ToolDefinition, role: str) -> bool:
    return role in tool.allowed_roles


def list_tool_rows(role: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in get_registered_tools():
        if role and not is_role_allowed(item, role):
            continue
        rows.append(
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
                "output_schema": item.output_schema,
                "handler": item.handler,
                "requires_admin": item.requires_admin,
                "allowed_roles": list(item.allowed_roles),
            }
        )
    return rows
