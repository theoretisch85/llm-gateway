from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class RouteDecision:
    requested_mode: str
    resolved_model: str
    target_base_url: str
    reason: str


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def decide(self, mode: str, user_text: str, message_count: int) -> RouteDecision:
        fast_model = self._settings.effective_fast_model
        deep_model = self._settings.effective_deep_model
        deep_available = bool(deep_model.public_name and deep_model.backend_name)
        normalized_mode = mode if mode in {"auto", "fast", "deep"} else self._settings.admin_default_mode
        lowered = user_text.lower()

        if normalized_mode == "fast":
            return RouteDecision(
                requested_mode="fast",
                resolved_model=fast_model.public_name,
                target_base_url=fast_model.base_url,
                reason="explicit_fast",
            )

        if normalized_mode == "deep":
            if not deep_available:
                return RouteDecision(
                    requested_mode="deep",
                    resolved_model=fast_model.public_name,
                    target_base_url=fast_model.base_url,
                    reason="deep_unavailable_fallback",
                )
            return RouteDecision(
                requested_mode="deep",
                resolved_model=deep_model.public_name,
                target_base_url=deep_model.base_url,
                reason="explicit_deep",
            )

        if deep_available and any(keyword in lowered for keyword in self._settings.parsed_routing_deep_keywords):
            return RouteDecision(
                requested_mode="auto",
                resolved_model=deep_model.public_name,
                target_base_url=deep_model.base_url,
                reason="keyword_rule",
            )

        if deep_available and len(user_text) >= self._settings.routing_length_threshold:
            return RouteDecision(
                requested_mode="auto",
                resolved_model=deep_model.public_name,
                target_base_url=deep_model.base_url,
                reason="length_rule",
            )

        if deep_available and message_count >= self._settings.routing_history_threshold:
            return RouteDecision(
                requested_mode="auto",
                resolved_model=deep_model.public_name,
                target_base_url=deep_model.base_url,
                reason="history_rule",
            )

        return RouteDecision(
            requested_mode="auto",
            resolved_model=fast_model.public_name,
            target_base_url=fast_model.base_url,
            reason="default_fast",
        )
