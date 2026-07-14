import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def test_reads_existing_cxone_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVER_KEY", "key")
    monkeypatch.setenv("SERVER_SECRET", "supersecretvalue")
    monkeypatch.setenv("SERVER_USER", "user")
    settings = Settings(_env_file=None)
    assert settings.server_key is not None
    assert settings.server_key.get_secret_value() == "key"
    assert settings.server_user == "user"
    assert "supersecretvalue" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sandbox_root", "Sandboxes/someone-else"),
        ("cxone_host", "libretexts.org"),
        ("cxone_host", "https://evil.example"),
    ],
)
def test_cxone_scope_is_not_runtime_expandable(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_detects_direct_ollama_cloud() -> None:
    settings = Settings(_env_file=None, ollama_base_url="https://ollama.com")
    assert settings.ollama_is_cloud is True
    local = Settings(_env_file=None, ollama_base_url="http://localhost:11434")
    assert local.ollama_is_cloud is False


def test_normalizes_provider_order_and_reads_gemini_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    settings = Settings(
        _env_file=None,
        llm_provider_order=" Gemini, OLLAMA ",
    )
    assert settings.llm_providers == ("gemini", "ollama")
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "gemini-secret"
    assert "gemini-secret" not in repr(settings)


@pytest.mark.parametrize("provider_order", ["", "openrouter", "ollama,ollama"])
def test_rejects_invalid_provider_order(provider_order: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider_order=provider_order)


def test_adapt_publishing_health_state_is_independent_and_requires_full_config() -> (
    None
):
    disabled = Settings(_env_file=None)
    assert disabled.adapt_publishing_status == "disabled"
    incomplete = Settings(_env_file=None, adapt_publishing_enabled=True)
    assert incomplete.adapt_publishing_status == "misconfigured"
    configured = Settings(
        _env_file=None,
        adapt_publishing_enabled=True,
        adapt_password=SecretStr("secret"),
        adapt_folder_id=42,
    )
    assert configured.adapt_publishing_status == "configured"


def test_only_exact_qualification_canary_can_publish_hints_while_flag_is_false() -> (
    None
):
    disabled = Settings(_env_file=None, hint_generation_enabled=False)
    assert disabled.hint_publication_enabled is False

    canary = Settings(
        _env_file=None,
        hint_generation_enabled=False,
        qualification_canary_marker="build08-assessment-publication-canary",
    )
    assert canary.hint_generation_enabled is False
    assert canary.hint_publication_enabled is True


@pytest.mark.parametrize(
    "url",
    [
        "https://adapt.libretexts.org/api",
        "http://adapt.libretexts.dev/api",
        "https://adapt.libretexts.dev:443/api",
        "https://adapt.libretexts.dev/not-api",
    ],
)
def test_adapt_target_is_pinned_to_the_dev_api(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, adapt_base_url=url)
