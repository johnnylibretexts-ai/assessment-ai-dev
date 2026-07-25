from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


COMPUTATION_PROXY_TOKEN_HEADER = "x-assessment-ai-proxy-token"
DEFAULT_COMPUTATION_SPECIALIST_SUBJECT_HEADER = "x-assessment-ai-authenticated-subject"


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

    computation_mode: Literal["off", "assist", "enforce"] = "off"
    computation_family_allowlist: str = ""
    computation_socket_path: Path = Path("/run/assessment-computation/compute.sock")
    # v0 qualification binds these exact wall clocks to the sidecar and client.
    # They are exposed for health reporting, not configurable runtime knobs.
    computation_numeric_timeout_seconds: Literal[2.0] = 2.0
    computation_unit_timeout_seconds: Literal[2.0] = 2.0
    computation_algebraic_timeout_seconds: Literal[5.0] = 5.0
    computation_specialist_subject_allowlist: str = ""
    computation_specialist_subject_header: str = (
        DEFAULT_COMPUTATION_SPECIALIST_SUBJECT_HEADER
    )
    computation_trusted_proxy_token: SecretStr | None = None
    computation_image_reference: str = Field(default="unavailable", max_length=512)
    # Native grading is a distinct, optional local-only trust boundary.  Both
    # values must be supplied together; the empty defaults make the runner
    # unreachable unless an operator explicitly configures a Unix socket and
    # a source-controlled qualified runner identity.
    computation_native_runner_socket_path: Path | None = None
    computation_native_runner_id: str = ""

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
    gemini_model: str = "gemini-3.5-flash"
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
    # Keep paid Gemini 3.x reasoning at the owner-approved minimum. A Literal
    # prevents an environment override from silently increasing its cost.
    gemini_thinking_level: Literal["minimal"] = "minimal"

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

    @field_validator("computation_family_allowlist")
    @classmethod
    def validate_computation_family_allowlist(cls, value: str) -> str:
        return _normalize_csv_allowlist(
            value,
            setting_name="computation_family_allowlist",
            allowed={"numeric", "algebraic", "unit"},
            casefold=True,
        )

    @field_validator("computation_specialist_subject_allowlist")
    @classmethod
    def validate_computation_specialist_subject_allowlist(cls, value: str) -> str:
        normalized = _normalize_csv_allowlist(
            value,
            setting_name="computation_specialist_subject_allowlist",
        )
        for subject in normalized.split(",") if normalized else ():
            if len(subject) > 255:
                raise ValueError(
                    "computation specialist subjects must be at most 255 characters"
                )
            if any(character.isspace() or ord(character) < 32 for character in subject):
                raise ValueError(
                    "computation specialist subjects must not contain whitespace "
                    "or control characters"
                )
        return normalized

    @field_validator("computation_specialist_subject_header")
    @classmethod
    def validate_computation_specialist_subject_header(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if (
            not re.fullmatch(
                r"x-assessment-ai-[a-z0-9]+(?:-[a-z0-9]+)*",
                normalized,
            )
            or normalized == COMPUTATION_PROXY_TOKEN_HEADER
        ):
            raise ValueError(
                "computation_specialist_subject_header must be a dedicated "
                "X-Assessment-AI-* header and must not be the proxy-token header"
            )
        return normalized

    @field_validator("computation_trusted_proxy_token")
    @classmethod
    def validate_computation_trusted_proxy_token(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is None:
            return None
        token = value.get_secret_value()
        if not token.strip():
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ValueError(
                "computation_trusted_proxy_token must be a 32–256 character "
                "base64url token"
            )
        return SecretStr(token)

    @field_validator("computation_socket_path")
    @classmethod
    def validate_computation_socket_path(cls, value: Path) -> Path:
        if not value.is_absolute() or value.suffix != ".sock" or ".." in value.parts:
            raise ValueError(
                "computation_socket_path must be an absolute .sock path "
                "without parent traversal"
            )
        return value

    @field_validator("computation_native_runner_socket_path", mode="before")
    @classmethod
    def normalize_computation_native_runner_socket_path(
        cls,
        value: object,
    ) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("computation_native_runner_socket_path")
    @classmethod
    def validate_computation_native_runner_socket_path(
        cls,
        value: Path | None,
    ) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute() or value.suffix != ".sock" or ".." in value.parts:
            raise ValueError(
                "computation_native_runner_socket_path must be an absolute "
                ".sock path without parent traversal"
            )
        return value

    @field_validator("computation_native_runner_id")
    @classmethod
    def validate_computation_native_runner_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        if re.fullmatch(r"[a-z][a-z0-9_.-]{2,63}", normalized) is None:
            raise ValueError(
                "computation_native_runner_id must be a safe source-controlled "
                "runner identity"
            )
        return normalized

    @field_validator("computation_image_reference")
    @classmethod
    def validate_computation_image_reference(cls, value: str) -> str:
        normalized = value.strip()
        if normalized == "unavailable":
            return normalized
        if (
            "://" in normalized
            or any(
                character.isspace() or ord(character) < 32 for character in normalized
            )
            or not re.fullmatch(r"[a-z0-9][A-Za-z0-9._/:@-]{0,510}", normalized)
        ):
            raise ValueError(
                "computation_image_reference must be unavailable or a safe OCI "
                "image reference"
            )
        if "@" in normalized:
            repository, separator, digest = normalized.rpartition("@")
            if (
                normalized.count("@") != 1
                or not separator
                or not re.fullmatch(r"[a-z0-9][a-z0-9._/:-]{0,400}", repository)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            ):
                raise ValueError(
                    "immutable computation_image_reference must use "
                    "lowercase-repository@sha256:<64 lowercase hex characters>"
                )
        return normalized

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
        native_socket_configured = (
            self.computation_native_runner_socket_path is not None
        )
        native_id_configured = bool(self.computation_native_runner_id)
        if native_socket_configured != native_id_configured:
            raise ValueError(
                "computation native runner socket path and runner id must be "
                "configured together"
            )
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
    def computation_families(self) -> tuple[str, ...]:
        if not self.computation_family_allowlist:
            return ()
        return tuple(self.computation_family_allowlist.split(","))

    @property
    def computation_container_digest(self) -> str:
        """Derive evidence digest from the single configured OCI reference."""

        _repository, separator, digest = self.computation_image_reference.rpartition(
            "@"
        )
        if separator and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return digest
        return "unavailable"

    @property
    def computation_image_is_immutable(self) -> bool:
        return self.computation_container_digest != "unavailable"

    @property
    def computation_native_runner_configured(self) -> bool:
        return self.computation_native_runner_socket_path is not None and bool(
            self.computation_native_runner_id
        )

    @property
    def computation_specialist_subjects(self) -> tuple[str, ...]:
        if not self.computation_specialist_subject_allowlist:
            return ()
        return tuple(self.computation_specialist_subject_allowlist.split(","))

    @property
    def computation_specialist_proxy_ready(self) -> bool:
        """Whether authenticated specialist identity can be accepted fail-closed."""

        token = (
            self.computation_trusted_proxy_token.get_secret_value()
            if self.computation_trusted_proxy_token is not None
            else ""
        )
        return (
            self.computation_mode != "off"
            and bool(self.computation_specialist_subjects)
            and bool(token)
        )

    @property
    def computation_health_config(self) -> dict[str, object]:
        """Return non-secret computation settings safe for status endpoints."""

        status: dict[str, object] = {
            "mode": self.computation_mode,
            "enabled": self.computation_mode != "off",
            "families": list(self.computation_families),
            "transport": "unix",
            "socket_path": str(self.computation_socket_path),
            "image_reference": self.computation_image_reference,
            "timeouts_seconds": {
                "numeric": self.computation_numeric_timeout_seconds,
                "unit": self.computation_unit_timeout_seconds,
                "algebraic": self.computation_algebraic_timeout_seconds,
            },
            "specialist_subject_count": len(self.computation_specialist_subjects),
        }
        if self.computation_native_runner_configured:
            status["native_runner"] = {
                "configured": True,
                "transport": "unix",
                "runner_id": self.computation_native_runner_id,
                "socket_path": str(self.computation_native_runner_socket_path),
            }
        return status

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


def _normalize_csv_allowlist(
    value: str,
    *,
    setting_name: str,
    allowed: set[str] | None = None,
    casefold: bool = False,
) -> str:
    if not value.strip():
        return ""
    entries = value.split(",")
    if any(not entry.strip() for entry in entries):
        raise ValueError(f"{setting_name} must not contain blank entries")
    normalized = [
        entry.strip().casefold() if casefold else entry.strip() for entry in entries
    ]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{setting_name} must not contain duplicates")
    if allowed is not None:
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            raise ValueError(
                f"unsupported computation family/families: {', '.join(unknown)}"
            )
    return ",".join(normalized)


@lru_cache
def get_settings() -> Settings:
    return Settings()
