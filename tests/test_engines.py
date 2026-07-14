from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.engines import EnginePublishingError, IMathASBridgeClient


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'engine.db'}",
        imathas_enabled=True,
        imathas_bridge_token=SecretStr("bridge-secret"),
    )


@pytest.mark.asyncio
async def test_imathas_bridge_client_uses_pinned_host_token_and_idempotency_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_post(self, url, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(url=url, headers=kwargs["headers"], json=kwargs["json"])
        return httpx.Response(200, json={"question_id": 17, "created": False})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await IMathASBridgeClient(settings(tmp_path)).create_question(
        publication_key="a" * 64,
        description="Parameterized item",
        author="LibreTexts Assessment AI",
        source='{"engine":"imathas"}',
        source_url="https://math.libretexts.org/Bookshelves/Validation",
    )
    assert result.question_id == 17
    assert result.created is False
    assert seen["url"] == "https://imathas.libretexts.dev/bridge/v1/questions"
    assert seen["headers"] == {"Authorization": "Bearer bridge-secret"}
    assert seen["json"]["publication_key"] == "a" * 64  # type: ignore[index]
    assert seen["json"]["source_url"] == "https://math.libretexts.org/Bookshelves/Validation"  # type: ignore[index]


@pytest.mark.asyncio
async def test_imathas_bridge_client_redacts_remote_failure_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(self, url, **kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(500, text="database password leaked by upstream")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(EnginePublishingError) as caught:
        await IMathASBridgeClient(settings(tmp_path)).create_question(
            publication_key="b" * 64,
            description="Parameterized item",
            author="LibreTexts Assessment AI",
            source='{"engine":"imathas"}',
            source_url="https://math.libretexts.org/Bookshelves/Validation",
        )
    assert "password" not in str(caught.value)


@pytest.mark.asyncio
async def test_imathas_bridge_network_outage_is_retryable_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(self, url, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", url)
        raise httpx.ConnectError("bridge-secret must stay private", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", unavailable)
    with pytest.raises(EnginePublishingError) as caught:
        await IMathASBridgeClient(settings(tmp_path)).create_question(
            publication_key="c" * 64,
            description="Parameterized item",
            author="LibreTexts Assessment AI",
            source='{"engine":"imathas"}',
            source_url="https://math.libretexts.org/Bookshelves/Validation",
        )
    assert caught.value.code == "imathas_unavailable"
    assert "bridge-secret" not in str(caught.value)
