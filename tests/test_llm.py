from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from app.config import Settings
from app.llm import (
    LLMConfigurationError,
    LLMStructuredOutputError,
    LLMTransportError,
    OllamaClient,
)


class Answer(BaseModel):
    answer: str
    confidence: int = Field(ge=0, le=1)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ollama_base_url": "https://ollama.com",
        "ollama_model": "gpt-oss:120b",
        "ollama_api_key": "test-cloud-key",
        "ollama_max_retries": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_cloud_chat_uses_bearer_exact_model_and_omits_format() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "gpt-oss:120b",
                "created_at": "2026-07-10T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": '```json\n{"answer":"four","confidence":1}\n```',
                },
                "done": True,
                "eval_count": 9,
            },
        )

    client = OllamaClient(
        settings(),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.complete(
            "What is two plus two?",
            Answer,
            prompt_version="question-v3",
        )
    finally:
        await client.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("https://ollama.com/api/chat")
    assert request.headers["Authorization"] == "Bearer test-cloud-key"

    payload = json.loads(request.content)
    assert payload["model"] == "gpt-oss:120b"
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0}
    assert "format" not in payload
    assert "What is two plus two?" in payload["messages"][0]["content"]
    assert '"confidence"' in payload["messages"][0]["content"]
    assert '"type": "object"' in payload["messages"][0]["content"]

    assert result.value == Answer(answer="four", confidence=1)
    assert result.metadata.model == "gpt-oss:120b"
    assert result.metadata.prompt_version == "question-v3"
    assert result.metadata.attempt == 1
    assert result.metadata.raw_response.startswith("```json")
    assert result.metadata.response_metadata["eval_count"] == 9


@pytest.mark.asyncio
async def test_local_chat_uses_native_json_schema_without_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": '{"answer":"local","confidence":0}',
                },
                "done": True,
            },
        )

    client = OllamaClient(
        settings(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="qwen3:8b",
            ollama_api_key=None,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.complete("Answer locally.", Answer)
    finally:
        await client.aclose()

    request = requests[0]
    payload = json.loads(request.content)
    assert "Authorization" not in request.headers
    assert payload["model"] == "qwen3:8b"
    assert payload["format"] == Answer.model_json_schema()
    assert Answer.model_json_schema()["title"] in payload["messages"][0]["content"]
    assert result.value.answer == "local"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-oss:120b-cloud", "glm-5.2:cloud"])
async def test_cloud_model_through_signed_in_local_daemon_omits_format(
    model: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": '{"answer":"cloud","confidence":1}',
                }
            },
        )

    client = OllamaClient(
        settings(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model=model,
            ollama_api_key=None,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.complete("Answer through the local proxy.", Answer)
    finally:
        await client.aclose()

    payload = json.loads(requests[0].content)
    assert payload["model"] == model
    assert "format" not in payload
    assert "Authorization" not in requests[0].headers
    assert result.value.answer == "cloud"


@pytest.mark.asyncio
async def test_validation_failure_retries_with_concise_feedback() -> None:
    request_payloads: list[dict[str, object]] = []
    responses = iter(
        (
            '{"answer":"four","confidence":7}',
            '{"answer":"four","confidence":1}',
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        request_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": next(responses)}},
        )

    client = OllamaClient(
        settings(ollama_max_retries=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.complete("Calculate.", Answer)
    finally:
        await client.aclose()

    assert len(request_payloads) == 2
    retry_prompt = request_payloads[1]["messages"][0]["content"]  # type: ignore[index]
    assert "previous response was invalid" in retry_prompt.lower()
    assert "confidence" in retry_prompt
    assert '"type": "object"' in retry_prompt
    assert result.metadata.attempt == 2
    assert len(result.metadata.attempts) == 2
    assert result.metadata.attempts[0].validation_error is not None
    assert result.metadata.attempts[1].validation_error is None


@pytest.mark.asyncio
async def test_malformed_response_is_bounded_and_does_not_expose_body() -> None:
    calls = 0
    sensitive_body = "not-json server diagnostic should-not-escape"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=sensitive_body)

    client = OllamaClient(
        settings(ollama_max_retries=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LLMStructuredOutputError) as exc_info:
            await client.complete("Calculate.", Answer)
    finally:
        await client.aclose()

    assert calls == 2
    assert "after 2 attempt(s)" in str(exc_info.value)
    assert "response envelope was not valid JSON" in str(exc_info.value)
    assert sensitive_body not in str(exc_info.value)


@pytest.mark.asyncio
async def test_attempt_metadata_and_transport_errors_redact_secret() -> None:
    secret = "super-secret-cloud-token"
    responses = iter(
        (
            {
                "done_reason": f"Authorization: Bearer {secret}",
                "message": {
                    "role": "assistant",
                    "content": ('{"answer":"api_key=' + secret + '","confidence":9}'),
                },
            },
            {
                "message": {
                    "role": "assistant",
                    "content": '{"answer":"safe","confidence":1}',
                }
            },
        )
    )

    def retry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    client = OllamaClient(
        settings(ollama_api_key=secret, ollama_max_retries=1),
        transport=httpx.MockTransport(retry_handler),
    )
    try:
        result = await client.complete("Calculate.", Answer)
    finally:
        await client.aclose()

    serialized_metadata = result.metadata.model_dump_json()
    assert secret not in serialized_metadata
    assert "[REDACTED]" in serialized_metadata

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed with {secret}", request=request)

    failing_client = OllamaClient(
        settings(ollama_api_key=secret),
        transport=httpx.MockTransport(error_handler),
    )
    try:
        with pytest.raises(LLMTransportError) as exc_info:
            await failing_client.complete("Calculate.", Answer)
    finally:
        await failing_client.aclose()

    assert secret not in str(exc_info.value)
    assert str(exc_info.value) == "Ollama request failed"


def test_direct_cloud_fails_closed_without_key() -> None:
    with pytest.raises(LLMConfigurationError, match="requires"):
        OllamaClient(settings(ollama_api_key=None))


def test_direct_cloud_rejects_plain_http_even_with_key() -> None:
    with pytest.raises(LLMConfigurationError, match="HTTPS"):
        OllamaClient(settings(ollama_base_url="http://ollama.com"))
