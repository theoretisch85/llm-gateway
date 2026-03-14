from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

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

    @property
    def backend_chat_completions_url(self) -> str:
        return f"{self.llamacpp_base_url.rstrip('/')}/v1/chat/completions"

    @property
    def backend_models_url(self) -> str:
        return f"{self.llamacpp_base_url.rstrip('/')}/v1/models"

    def map_public_to_backend_model(self, model_name: str) -> str:
        if model_name == self.public_model_name:
            return self.backend_model_name
        return model_name

    def map_backend_to_public_model(self, model_name: str) -> str:
        if model_name == self.backend_model_name:
            return self.public_model_name
        return model_name


@lru_cache
def get_settings() -> Settings:
    return Settings()
