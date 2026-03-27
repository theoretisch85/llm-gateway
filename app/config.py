from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ModelTarget:
    role: str
    public_name: str
    backend_name: str
    base_url: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # The admin UI writes runtime config back to .env.
        # Prioritize the current file over the process environment so
        # changes apply immediately without a full service restart.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    llamacpp_base_url: str = Field(alias="LLAMACPP_BASE_URL")
    llamacpp_timeout_seconds: float = Field(default=60.0, alias="LLAMACPP_TIMEOUT_SECONDS")
    llamacpp_api_key: str | None = Field(default=None, alias="LLAMACPP_API_KEY")
    api_bearer_token: str = Field(alias="API_BEARER_TOKEN")
    backend_context_window: int = Field(default=8192, alias="BACKEND_CONTEXT_WINDOW")
    context_response_reserve: int = Field(default=1024, alias="CONTEXT_RESPONSE_RESERVE")
    context_chars_per_token: float = Field(default=4.0, alias="CONTEXT_CHARS_PER_TOKEN")
    default_max_tokens: int = Field(default=512, alias="DEFAULT_MAX_TOKENS")

    public_model_name: str = Field(default="qwen2.5-coder", alias="PUBLIC_MODEL_NAME")
    backend_model_name: str = Field(default="qwen2.5-coder", alias="BACKEND_MODEL_NAME")
    fast_model_public_name: str = Field(default="", alias="FAST_MODEL_PUBLIC_NAME")
    fast_model_backend_name: str = Field(default="", alias="FAST_MODEL_BACKEND_NAME")
    fast_model_base_url: str | None = Field(default=None, alias="FAST_MODEL_BASE_URL")
    deep_model_public_name: str = Field(default="", alias="DEEP_MODEL_PUBLIC_NAME")
    deep_model_backend_name: str = Field(default="", alias="DEEP_MODEL_BACKEND_NAME")
    deep_model_base_url: str | None = Field(default=None, alias="DEEP_MODEL_BASE_URL")
    admin_default_mode: str = Field(default="auto", alias="ADMIN_DEFAULT_MODE")
    routing_deep_keywords: str = Field(
        default="architektur,analyse,refactor,refactoring,debug,design,plan,root cause,komplex",
        alias="ROUTING_DEEP_KEYWORDS",
    )
    routing_length_threshold: int = Field(default=1200, alias="ROUTING_LENGTH_THRESHOLD")
    routing_history_threshold: int = Field(default=8, alias="ROUTING_HISTORY_THRESHOLD")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str | None = Field(default=None, alias="ADMIN_PASSWORD")
    admin_session_secret: str | None = Field(default=None, alias="ADMIN_SESSION_SECRET")
    admin_session_ttl_hours: int = Field(default=24, alias="ADMIN_SESSION_TTL_HOURS")
    admin_cookie_secure: bool = Field(default=False, alias="ADMIN_COOKIE_SECURE")
    device_shared_token: str | None = Field(default=None, alias="DEVICE_SHARED_TOKEN")
    gateway_local_root_prefix: str = Field(default="sudo -n", alias="GATEWAY_LOCAL_ROOT_PREFIX")
    home_assistant_base_url: str | None = Field(default=None, alias="HOME_ASSISTANT_BASE_URL")
    home_assistant_token: str | None = Field(default=None, alias="HOME_ASSISTANT_TOKEN")
    home_assistant_timeout_seconds: float = Field(default=10.0, alias="HOME_ASSISTANT_TIMEOUT_SECONDS")
    home_assistant_allowed_services: str = Field(
        default="light.turn_on,light.turn_off,switch.turn_on,switch.turn_off,climate.set_temperature,script.turn_on",
        alias="HOME_ASSISTANT_ALLOWED_SERVICES",
    )
    home_assistant_allowed_entity_prefixes: str = Field(
        default="light.,switch.,climate.,script.",
        alias="HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES",
    )
    vision_base_url: str | None = Field(default=None, alias="VISION_BASE_URL")
    vision_model_name: str | None = Field(default=None, alias="VISION_MODEL_NAME")
    vision_prompt: str = Field(
        default="Beschreibe das Bild knapp und technisch brauchbar auf Deutsch. Nenne sichtbare Personen, Objekte, Text im Bild und auffaellige Ereignisse.",
        alias="VISION_PROMPT",
    )
    vision_max_tokens: int = Field(default=256, alias="VISION_MAX_TOKENS")
    mi50_ssh_host: str | None = Field(default=None, alias="MI50_SSH_HOST")
    mi50_ssh_user: str | None = Field(default=None, alias="MI50_SSH_USER")
    mi50_ssh_port: int = Field(default=22, alias="MI50_SSH_PORT")
    mi50_restart_command: str | None = Field(default=None, alias="MI50_RESTART_COMMAND")
    mi50_status_command: str | None = Field(default=None, alias="MI50_STATUS_COMMAND")
    mi50_logs_command: str | None = Field(default=None, alias="MI50_LOGS_COMMAND")
    mi50_rocm_smi_command: str | None = Field(default=None, alias="MI50_ROCM_SMI_COMMAND")

    @property
    def backend_chat_completions_url(self) -> str:
        return f"{self.llamacpp_base_url.rstrip('/')}/v1/chat/completions"

    @property
    def backend_models_url(self) -> str:
        return f"{self.llamacpp_base_url.rstrip('/')}/v1/models"

    @property
    def effective_fast_model(self) -> ModelTarget:
        return ModelTarget(
            role="fast",
            public_name=self.fast_model_public_name or self.public_model_name,
            backend_name=self.fast_model_backend_name or self.backend_model_name,
            base_url=(self.fast_model_base_url or self.llamacpp_base_url).rstrip("/"),
        )

    @property
    def effective_deep_model(self) -> ModelTarget:
        return ModelTarget(
            role="deep",
            public_name=self.deep_model_public_name,
            backend_name=self.deep_model_backend_name,
            base_url=(self.deep_model_base_url or self.llamacpp_base_url).rstrip("/"),
        )

    @property
    def listed_models(self) -> list[ModelTarget]:
        models = [self.effective_fast_model]
        deep_model = self.effective_deep_model
        if deep_model.public_name and deep_model.backend_name and deep_model.public_name != self.effective_fast_model.public_name:
            models.append(deep_model)
        return models

    @property
    def parsed_routing_deep_keywords(self) -> list[str]:
        return [item.strip().lower() for item in self.routing_deep_keywords.split(",") if item.strip()]

    @property
    def parsed_home_assistant_allowed_services(self) -> list[str]:
        return [item.strip().lower() for item in self.home_assistant_allowed_services.split(",") if item.strip()]

    @property
    def parsed_home_assistant_allowed_entity_prefixes(self) -> list[str]:
        return [item.strip().lower() for item in self.home_assistant_allowed_entity_prefixes.split(",") if item.strip()]

    def models_url_for(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/v1/models"

    def chat_completions_url_for(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/v1/chat/completions"

    @property
    def effective_vision_base_url(self) -> str:
        return (self.vision_base_url or self.llamacpp_base_url).rstrip("/")

    def resolve_target_for_public_model(self, model_name: str) -> ModelTarget:
        for target in self.listed_models:
            if target.public_name == model_name:
                return target
        return ModelTarget(
            role="custom",
            public_name=model_name,
            backend_name=model_name,
            base_url=self.llamacpp_base_url.rstrip("/"),
        )

    def map_public_to_backend_model(self, model_name: str) -> str:
        return self.resolve_target_for_public_model(model_name).backend_name

    def map_backend_to_public_model(self, model_name: str) -> str:
        for target in self.listed_models:
            if model_name == target.backend_name:
                return target.public_name
        return model_name


@lru_cache
def get_settings() -> Settings:
    return Settings()
