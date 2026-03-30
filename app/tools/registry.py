from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.core.roles import ROLE_ADMIN, ROLE_DEVICE
from app.services.backend_control import run_ops_command
from app.services.home_assistant import HomeAssistantClient
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
