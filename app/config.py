from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
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
    public_sources_enabled: bool = False
    sandbox_sources_enabled: bool = False
    advanced_items_enabled: bool = False
    parameterized_items_enabled: bool = False
    hint_generation_enabled: bool = False
    webwork_enabled: bool = False
    imathas_enabled: bool = False

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

    # Providers are tried in order. Keeping Ollama first preserves the deployed
    # behavior while allowing Gemini to serve as an explicit fallback.
    llm_provider_order: str = "ollama"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASSESSMENT_AI_GEMINI_API_KEY",
            "GEMINI_API_KEY",
        ),
    )
    gemini_timeout_seconds: float = Field(default=180.0, ge=5, le=600)
    gemini_max_retries: int = Field(default=2, ge=0, le=5)
    gemini_max_output_tokens: int = Field(default=8_192, ge=1_024, le=8_192)

    adapt_publishing_enabled: bool = False
    adapt_base_url: str = "https://adapt.libretexts.dev/api"
    qualification_canary_marker: str = ""
    adapt_email: str = "assessment-ai@libretexts.dev"
    adapt_password: SecretStr | None = None
    adapt_folder_id: int | None = Field(default=None, gt=0)
    adapt_folder_name: str = "Assessment AI — Approved"
    adapt_author: str = "LibreTexts Assessment AI"
    adapt_public: bool = True
    adapt_timeout_seconds: float = Field(default=30.0, ge=5, le=120)
    qti_storage_dir: Path = Path("/data/qti")
    hotspot_media_dir: Path = Path("./data/media")
    hotspot_media_public_base: str = "https://assess-ai.libretexts.dev/media"

    webwork_base_url: str = "https://webwork.libretexts.dev"
    webwork_renderer_url: str = "https://wwrenderer.libretexts.dev"
    webwork_timeout_seconds: float = Field(default=30.0, ge=5, le=120)
    imathas_base_url: str = "https://imathas.libretexts.dev"
    imathas_bridge_api_url: str | None = None
    imathas_bridge_token: SecretStr | None = None
    imathas_timeout_seconds: float = Field(default=30.0, ge=5, le=120)

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

    @field_validator("gemini_base_url")
    @classmethod
    def validate_gemini_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("gemini_base_url must be an absolute HTTPS URL")
        return value.rstrip("/")

    @field_validator("llm_provider_order")
    @classmethod
    def validate_llm_provider_order(cls, value: str) -> str:
        providers = [item.strip().casefold() for item in value.split(",")]
        providers = [item for item in providers if item]
        if not providers:
            raise ValueError("llm_provider_order must select at least one provider")
        unknown = sorted(set(providers) - {"ollama", "gemini"})
        if unknown:
            raise ValueError(f"unsupported LLM provider(s): {', '.join(unknown)}")
        if len(providers) != len(set(providers)):
            raise ValueError("llm_provider_order must not contain duplicates")
        return ",".join(providers)

    @field_validator("adapt_base_url")
    @classmethod
    def validate_adapt_base_url(cls, value: str) -> str:
        if value.rstrip("/") == "http://adapt-browser/api":
            return "http://adapt-browser/api"
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "adapt.libretexts.dev"
            or parsed.port is not None
            or parsed.path.rstrip("/") != "/api"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "adapt_base_url is pinned to https://adapt.libretexts.dev/api"
            )
        return "https://adapt.libretexts.dev/api"

    @field_validator("adapt_email")
    @classmethod
    def validate_adapt_email(cls, value: str) -> str:
        if value.strip().casefold() != "assessment-ai@libretexts.dev":
            raise ValueError("adapt_email is pinned to assessment-ai@libretexts.dev")
        return "assessment-ai@libretexts.dev"

    @field_validator("adapt_author")
    @classmethod
    def validate_adapt_author(cls, value: str) -> str:
        if value.strip() != "LibreTexts Assessment AI":
            raise ValueError("adapt_author is pinned to LibreTexts Assessment AI")
        return "LibreTexts Assessment AI"

    @field_validator("webwork_base_url")
    @classmethod
    def validate_webwork_base_url(cls, value: str) -> str:
        return _pinned_dev_url(value, "webwork.libretexts.dev")

    @field_validator("webwork_renderer_url")
    @classmethod
    def validate_webwork_renderer_url(cls, value: str) -> str:
        return _pinned_dev_url(value, "wwrenderer.libretexts.dev")

    @field_validator("imathas_base_url")
    @classmethod
    def validate_imathas_base_url(cls, value: str) -> str:
        return _pinned_dev_url(value, "imathas.libretexts.dev")

    @field_validator("imathas_bridge_api_url")
    @classmethod
    def validate_imathas_bridge_api_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.rstrip("/")
        if normalized in {
            "https://imathas.libretexts.dev",
            "http://build08-imathas-bridge-browser:8000",
        }:
            return normalized
        raise ValueError(
            "imathas_bridge_api_url must be the pinned public bridge or the exact "
            "BUILD-08 qualification-canary alias"
        )

    @model_validator(mode="after")
    def validate_qualification_targets(self) -> "Settings":
        marker = self.qualification_canary_marker.strip()
        expected = "build08-assessment-publication-canary"
        internal_target = (
            self.adapt_base_url == "http://adapt-browser/api"
            or self.imathas_bridge_api_url
            == "http://build08-imathas-bridge-browser:8000"
        )
        if internal_target and marker != expected:
            raise ValueError(
                "internal publication targets require the exact BUILD-08 "
                "qualification-canary marker"
            )
        if marker and marker != expected:
            raise ValueError("unknown qualification-canary marker")
        return self

    @field_validator("hotspot_media_public_base")
    @classmethod
    def validate_hotspot_media_base(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "assess-ai.libretexts.dev"
            or parsed.port is not None
            or parsed.path.rstrip("/") != "/media"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "hotspot_media_public_base is pinned to the Assessment AI media route"
            )
        return "https://assess-ai.libretexts.dev/media"

    @property
    def ollama_is_cloud(self) -> bool:
        return urlparse(self.ollama_base_url).hostname == "ollama.com"

    @property
    def llm_providers(self) -> tuple[str, ...]:
        return tuple(self.llm_provider_order.split(","))

    @property
    def adapt_publishing_status(self) -> str:
        if not self.adapt_publishing_enabled:
            return "disabled"
        password = (
            self.adapt_password.get_secret_value().strip()
            if self.adapt_password is not None
            else ""
        )
        if (
            not password
            or self.adapt_folder_id is None
            or not self.adapt_folder_name.strip()
            or not self.adapt_author.strip()
            or not self.adapt_public
        ):
            return "misconfigured"
        return "configured"

    @property
    def webwork_status(self) -> str:
        return "configured" if self.webwork_enabled else "disabled"

    @property
    def imathas_status(self) -> str:
        if not self.imathas_enabled:
            return "disabled"
        token = (
            self.imathas_bridge_token.get_secret_value().strip()
            if self.imathas_bridge_token is not None
            else ""
        )
        return "configured" if token else "misconfigured"

    @property
    def imathas_publishing_status(self) -> str:
        if self.qualification_canary_marker == (
            "build08-assessment-publication-canary"
        ):
            token = (
                self.imathas_bridge_token.get_secret_value().strip()
                if self.imathas_bridge_token is not None
                else ""
            )
            return "configured" if token else "misconfigured"
        return self.imathas_status

    @property
    def hint_publication_enabled(self) -> bool:
        """Allow sealed approved hints through the exact qualification canary."""

        return self.hint_generation_enabled or self.qualification_canary_marker == (
            "build08-assessment-publication-canary"
        )

    @property
    def resolved_imathas_bridge_api_url(self) -> str:
        return self.imathas_bridge_api_url or self.imathas_base_url

    @property
    def resolved_imathas_bridge_questions_url(self) -> str:
        base_url = self.resolved_imathas_bridge_api_url
        if base_url == "http://build08-imathas-bridge-browser:8000":
            return f"{base_url}/v1/questions"
        return f"{base_url}/bridge/v1/questions"


def _pinned_dev_url(value: str, hostname: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
        or parsed.port is not None
        or parsed.path.rstrip("/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"URL is pinned to https://{hostname}")
    return f"https://{hostname}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
