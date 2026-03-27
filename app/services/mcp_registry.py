from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.config import Settings
from app.services.backend_control import run_ops_command
from app.services.home_assistant import HomeAssistantClient
from app.services.storage_library import get_document_contexts, list_documents


MCPHandler = Callable[[Settings, dict[str, Any]], Awaitable[Any]]


def _tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    handler: MCPHandler,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "handler": handler,
    }


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
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("command ist erforderlich.")
    return run_ops_command(command, settings)


def get_mcp_tools() -> list[dict[str, Any]]:
    # MCP registry is intentionally small in v1 and backed by existing services.
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
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            output_schema={"type": "object"},
            handler=_gateway_ops,
        ),
    ]


def find_mcp_tool(tool_name: str) -> dict[str, Any] | None:
    for tool in get_mcp_tools():
        if tool["name"] == tool_name:
            return tool
    return None
