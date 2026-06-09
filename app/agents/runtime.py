from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.agents.policy import AgentPolicy, PolicyDecision
from app.agents.prompts import build_agent_system_prompt
from app.agents.registry import AgentTool, get_tool, list_tools
from app.agents.schemas import (
    ActionRequest,
    ActionResult,
    AgentDecision,
    AgentInput,
    AgentRun,
    DecisionType,
)
from app.config import Settings, get_settings
from app.core.request_context import RequestContext
from app.orchestrator import ToolOrchestrator
from app.services.llamacpp_client import LlamaCppClient
from app.services.model_router import ModelRouter, RouteDecision
from app.services.session_memory import get_session_store


logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        *,
        request_context: RequestContext,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._request_context = request_context
        self._llm = LlamaCppClient(self._settings)
        self._model_router = ModelRouter(self._settings)
        self._policy = AgentPolicy()
        self._tool_orchestrator = ToolOrchestrator()

    async def run(self, agent_input: AgentInput) -> AgentRun:
        route_decision = self._model_router.decide(agent_input.model_preference, agent_input.message, 0)
        agent_decision = await self._decide(agent_input, route_decision)
        action_result: ActionResult | None = None

        if agent_decision.decision == DecisionType.ACTION and agent_decision.action is not None:
            policy_decision = self._policy.evaluate(agent_decision.action, allow_actions=agent_input.allow_actions)
            logger.info(
                "agent policy status=%s tool_name=%s request_id=%s",
                policy_decision.status,
                agent_decision.action.tool_name,
                self._request_context.request_id,
            )
            if policy_decision.status == "allowed" and policy_decision.tool is not None:
                action_result = await self._execute_action(agent_input, agent_decision.action, policy_decision.tool)
            else:
                action_result = self._policy_action_result(agent_decision.action, policy_decision)

        final_reply = self._build_final_reply(agent_decision, action_result)
        return AgentRun(
            id=str(uuid4()),
            resolved_model=route_decision.resolved_model,
            route_reason=route_decision.reason,
            decision=agent_decision,
            action_result=action_result,
            final_reply=final_reply,
        )

    async def _decide(self, agent_input: AgentInput, route_decision: RouteDecision) -> AgentDecision:
        system_prompt = build_agent_system_prompt(list_tools())
        user_prompt = json.dumps(
            {
                "request": agent_input.message,
                "allow_actions": agent_input.allow_actions,
                "session_id": agent_input.session_id,
            },
            ensure_ascii=True,
        )
        target = self._settings.resolve_target_for_public_model(route_decision.resolved_model)
        response_payload = await self._llm.create_chat_completion(
            {
                "model": target.backend_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "temperature": 0,
                "max_tokens": min(300, self._settings.default_max_tokens),
            },
            base_url=route_decision.target_base_url,
        )
        raw_text = self._extract_assistant_text(response_payload)
        logger.info(
            "agent llm decision model=%s route_reason=%s request_id=%s",
            route_decision.resolved_model,
            route_decision.reason,
            self._request_context.request_id,
        )
        return self._parse_decision(raw_text)

    async def _execute_action(
        self,
        agent_input: AgentInput,
        action: ActionRequest,
        tool: AgentTool,
    ) -> ActionResult:
        logger.info(
            "agent action executing tool_name=%s request_id=%s",
            tool.name,
            self._request_context.request_id,
        )

        try:
            if tool.name == "home.read_state":
                result = await self._execute_home_read_state(action.arguments)
            elif tool.name == "home.call_service":
                result = await self._execute_home_call_service(action.arguments)
            elif tool.name == "memory.store_fact":
                result = await self._execute_memory_store_fact(agent_input, action.arguments)
            else:
                raise ValueError(f"Unknown tool: {tool.name}")
        except Exception as exc:
            logger.warning(
                "agent action failed tool_name=%s request_id=%s error=%s",
                tool.name,
                self._request_context.request_id,
                str(exc),
            )
            return ActionResult(
                tool_name=tool.name,
                status="failed",
                ok=False,
                executed=False,
                risk=tool.risk_level,
                requires_confirmation=tool.requires_confirmation,
                error=str(exc),
            )

        return ActionResult(
            tool_name=tool.name,
            status="executed",
            ok=True,
            executed=True,
            risk=tool.risk_level,
            requires_confirmation=tool.requires_confirmation,
            result=result,
        )

    def _build_final_reply(self, decision: AgentDecision, action_result: ActionResult | None) -> str:
        if action_result is None:
            return decision.reply.strip() or self._default_reply_for_decision(decision.decision)

        if action_result.status == "denied":
            return action_result.error or "I cannot run that action."

        if action_result.status == "requires_confirmation":
            return action_result.error or "Confirmation is required before this action can run."

        if action_result.status == "failed":
            return action_result.error or "The requested action failed."

        if action_result.tool_name == "home.read_state":
            return self._reply_for_home_read_state(action_result.result)
        if action_result.tool_name == "home.call_service":
            return "The Home Assistant service call completed."
        if action_result.tool_name == "memory.store_fact":
            return "I stored that fact in memory."
        return decision.reply.strip() or "Action completed."

    def _policy_action_result(self, action: ActionRequest, policy_decision: PolicyDecision) -> ActionResult:
        tool = policy_decision.tool or get_tool(action.tool_name)
        requires_confirmation = policy_decision.status == "requires_confirmation" or (
            tool.requires_confirmation if tool is not None else False
        )
        return ActionResult(
            tool_name=action.tool_name,
            status="requires_confirmation" if policy_decision.status == "requires_confirmation" else "denied",
            ok=False,
            executed=False,
            risk=tool.risk_level if tool is not None else action.risk,
            requires_confirmation=requires_confirmation,
            error=policy_decision.reason,
        )

    async def _execute_home_read_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = str(arguments.get("entity_id") or "").strip().lower()
        domain = str(arguments.get("domain") or "").strip().lower()
        limit = int(arguments.get("limit") or 20)
        if entity_id and "." in entity_id and not domain:
            domain = entity_id.split(".", 1)[0]

        entities = await self._tool_orchestrator.execute_tool(
            settings=self._settings,
            context=self._request_context,
            tool_name="ha.entities",
            arguments={"domain": domain or None, "limit": limit},
        )
        if entity_id:
            entities = [item for item in entities if str(item.get("entity_id") or "").lower() == entity_id]
        return {
            "count": len(entities),
            "entities": entities,
        }

    async def _execute_home_call_service(self, arguments: dict[str, Any]) -> Any:
        domain = str(arguments.get("domain") or "").strip().lower()
        service = str(arguments.get("service") or "").strip().lower()
        allowed = {
            "light.turn_on",
            "light.turn_off",
            "switch.turn_on",
            "switch.turn_off",
            "scene.turn_on",
        }
        key = f"{domain}.{service}"
        if key not in allowed:
            raise ValueError("Service not allowed")

        return await self._tool_orchestrator.execute_tool(
            settings=self._settings,
            context=self._request_context,
            tool_name="ha.call",
            arguments={
                "domain": domain,
                "service": service,
                "entity_id": arguments.get("entity_id"),
                "service_data": arguments.get("service_data") or {},
            },
        )

    async def _execute_memory_store_fact(self, agent_input: AgentInput, arguments: dict[str, Any]) -> dict[str, Any]:
        fact = str(arguments.get("fact") or arguments.get("content") or "").strip()
        if not fact:
            raise ValueError("memory.store_fact requires a non-empty fact.")

        store = get_session_store(self._settings)
        session_id = str(arguments.get("session_id") or agent_input.session_id or "").strip()
        source = str(arguments.get("source") or "kai.agent").strip()

        if session_id:
            session = await store.get_session(session_id)
            if session is None:
                raise ValueError("Memory session not found.")
        else:
            session = await store.create_session(title="Kai Agent Memory", mode=agent_input.model_preference)

        message = await store.add_message(
            session.id,
            "system",
            f"AGENT FACT ({source}): {fact}",
            model_used="kai.agent",
        )
        return {
            "session_id": session.id,
            "message_id": message.id,
            "stored_fact": fact,
            "source": source,
        }

    def _parse_decision(self, raw_text: str) -> AgentDecision:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            try:
                payload = self._extract_json_object(raw_text)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "agent decision json parse failed request_id=%s error=%s raw=%s",
                    self._request_context.request_id,
                    str(exc),
                    raw_text,
                )
                return AgentDecision(
                    decision=DecisionType.DENY,
                    reply="I could not produce a valid structured decision for this request.",
                    action=None,
                )

        try:
            decision = AgentDecision.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "agent decision parse failed request_id=%s error=%s raw=%s",
                self._request_context.request_id,
                str(exc),
                raw_text,
            )
            return AgentDecision(
                decision=DecisionType.DENY,
                reply="I could not produce a valid structured decision for this request.",
                action=None,
            )

        if decision.action is not None:
            tool = get_tool(decision.action.tool_name)
            if tool is not None:
                decision = decision.model_copy(
                    update={
                        "action": decision.action.model_copy(
                            update={
                                "risk": tool.risk_level,
                            }
                        )
                    }
                )

        return decision

    def _extract_assistant_text(self, response_payload: dict[str, Any]) -> str:
        choices = response_payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        return json.dumps(content or {}, ensure_ascii=True)

    def _extract_json_object(self, raw_text: str) -> dict[str, Any]:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start < 0 or end < start:
            raise json.JSONDecodeError("No JSON object found.", raw_text, 0)
        return json.loads(raw_text[start : end + 1])

    def _reply_for_home_read_state(self, result: Any) -> str:
        if not isinstance(result, dict):
            return "I read the Home Assistant state."

        entities = result.get("entities")
        if not isinstance(entities, list) or not entities:
            return "I could not find a matching Home Assistant entity."

        if len(entities) == 1:
            item = entities[0]
            name = str(item.get("friendly_name") or item.get("entity_id") or "entity")
            state = item.get("state")
            return f"{name} is {state}."

        rendered = ", ".join(
            f"{item.get('entity_id')}={item.get('state')}"
            for item in entities[:5]
            if isinstance(item, dict)
        )
        return f"I found these states: {rendered}."

    def _default_reply_for_decision(self, decision_type: DecisionType) -> str:
        if decision_type == DecisionType.CLARIFY:
            return "I need a bit more information before I can continue."
        if decision_type == DecisionType.DENY:
            return "I cannot help with that request."
        if decision_type == DecisionType.RESPOND:
            return "Done."
        return "Action prepared."
