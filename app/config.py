from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Secrets are read at runtime and never serialized."""

    model_config = SettingsConfigDict(
        env_prefix="ASSESSMENT_AI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "LibreTexts Assessment AI"
    database_url: str = "sqlite:///./data/assessment-ai.db"
    allowed_origin: str = "http://localhost:8000"
    max_source_chars: int = Field(default=60_000, ge=1_000, le=250_000)

    sandbox_root: str = "Sandboxes/johnnyphung"
    cxone_host: str = "dev.libretexts.org"
    cxone_env_file: Path = Path("/run/secrets/cxone.env")
    server_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ASSESSMENT_AI_SERVER_KEY", "SERVER_KEY"),
    )
    server_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ASSESSMENT_AI_SERVER_SECRET", "SERVER_SECRET"),
    )
    server_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ASSESSMENT_AI_SERVER_USER", "SERVER_USER"),
    )

    ollama_base_url: str = "https://ollama.com"
    ollama_model: str = "gpt-oss:120b"
    ollama_api_key: SecretStr | None = None
    ollama_timeout_seconds: float = Field(default=180.0, ge=5, le=600)
    ollama_max_retries: int = Field(default=2, ge=0, le=5)

    adapt_publishing_enabled: bool = False

    @field_validator("sandbox_root")
    @classmethod
    def validate_sandbox_root(cls, value: str) -> str:
        value = value.strip("/")
        if value.casefold() != "sandboxes/johnnyphung":
            raise ValueError("sandbox_root is pinned to Sandboxes/johnnyphung")
        return "Sandboxes/johnnyphung"

    @field_validator("cxone_host")
    @classmethod
    def validate_cxone_host(cls, value: str) -> str:
        host = value.strip().lower().removeprefix("https://").rstrip("/")
        if host != "dev.libretexts.org":
            raise ValueError("cxone_host is pinned to dev.libretexts.org")
        return host

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ollama_base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @property
    def ollama_is_cloud(self) -> bool:
        return urlparse(self.ollama_base_url).hostname == "ollama.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
