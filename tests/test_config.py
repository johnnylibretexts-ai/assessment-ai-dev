import pytest
from pydantic import ValidationError

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
