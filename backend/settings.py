from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the local voice-agent PoC."""

    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepgram_api_key: str | None = Field(default=None, alias="DEEPGRAM_API_KEY")
    elevenlabs_api_key: str | None = Field(default=None, alias="ELEVENLABS_API_KEY")
    elevenlabs_voice_id: str | None = Field(default=None, alias="ELEVENLABS_VOICE_ID")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="qwen/qwen-2.5-7b-instruct", alias="OPENAI_MODEL")
    backend_port: int = Field(default=8000, alias="BACKEND_PORT")
    frontend_port: int = Field(default=5173, alias="FRONTEND_PORT")

    @property
    def frontend_origin(self) -> str:
        return f"http://localhost:{self.frontend_port}"

    @property
    def llm_api_key(self) -> str | None:
        return self.openrouter_api_key or self.openai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
