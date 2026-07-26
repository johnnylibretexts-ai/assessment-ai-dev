"""Streaming chat client for the demo assistant.

Deliberately separate from ``app.llm``. That module's ``LLMClient`` protocol is
structured-output only -- ``complete(prompt, schema)`` returning a validated
Pydantic value -- because everything it serves has to be machine-checkable
before it can enter the review queue. Chat is the opposite shape: free text,
streamed incrementally, never parsed. Bolting a second mode onto the qualified
generation client would put demo-support code inside the path that drafts
assessment items, so it lives here instead.

Provider selection follows the same ``llm_provider_order`` setting generation
uses, so a self-hosted Ollama model stays a first-class option and the assistant
never pins the deployment to one vendor.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..config import Settings


class AssistantLLMError(RuntimeError):
    """Safe-to-display failure from the assistant's provider."""


@dataclass(frozen=True)
class ChatUsage:
    """Token accounting for one answer, so cost is observable rather than guessed.

    ``cached`` is the slice of the prompt the provider served from its context
    cache. ``thoughts`` bills as output, which is why it is tracked separately:
    an assistant that quietly starts reasoning is an assistant that quietly
    gets expensive.
    """

    prompt: int = 0
    cached: int = 0
    thoughts: int = 0
    output: int = 0
    total: int = 0

    @property
    def billed_prompt(self) -> int:
        """Prompt tokens charged at full rate, i.e. everything not cached."""

        return max(self.prompt - self.cached, 0)


@dataclass(frozen=True)
class ChatTurn:
    """One prior exchange, replayed to give the model conversation memory."""

    role: str
    content: str


class ChatClient(Protocol):
    provider_name: str
    model: str
    # Populated once a stream completes; None until then.
    last_usage: ChatUsage | None

    def stream(
        self,
        *,
        system: str,
        history: Sequence[ChatTurn],
        question: str,
    ) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


def _secret(value: Any) -> str:
    if value is None:
        return ""
    return value.get_secret_value().strip()


class GeminiChatClient:
    """Streams plain text from Gemini's ``streamGenerateContent`` endpoint."""

    provider_name = "gemini"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = _secret(settings.gemini_api_key)
        if not key:
            raise AssistantLLMError("Gemini requires ASSESSMENT_AI_GEMINI_API_KEY")
        self.model = (settings.assistant_model or settings.gemini_model).strip()
        if not self.model:
            raise AssistantLLMError("a Gemini model must be configured")
        self.last_usage: ChatUsage | None = None
        self._thinking_level = settings.assistant_thinking_level
        self._max_output_tokens = settings.assistant_max_output_tokens
        base = settings.gemini_base_url.rstrip("/")
        self._url = (
            f"{base}/models/{quote(self.model, safe='')}:streamGenerateContent?alt=sse"
        )
        self._headers = {"x-goog-api-key": key}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.assistant_timeout_seconds, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(
        self,
        *,
        system: str,
        history: Sequence[ChatTurn],
        question: str,
    ) -> AsyncIterator[str]:
        contents: list[dict[str, Any]] = []
        for turn in history:
            role = "model" if turn.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": turn.content}]})
        contents.append({"role": "user", "parts": [{"text": question}]})

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": self._max_output_tokens,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
            },
        }
        self.last_usage = None

        try:
            async with self._client.stream(
                "POST", self._url, headers=self._headers, json=payload
            ) as response:
                if response.is_error:
                    # Drain before reading; the body is not streamed on error.
                    await response.aread()
                    raise AssistantLLMError(
                        f"Gemini request failed with HTTP {response.status_code}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[len("data:") :].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    for piece in _gemini_text_parts(chunk):
                        yield piece
                    # Usage arrives on the trailing chunks; keep the last one.
                    usage = _gemini_usage(chunk)
                    if usage is not None:
                        self.last_usage = usage
        except httpx.RequestError as exc:
            raise AssistantLLMError("Gemini network request failed") from exc


def _gemini_text_parts(chunk: str) -> list[str]:
    try:
        body = json.loads(chunk)
    except json.JSONDecodeError:
        return []
    if not isinstance(body, dict):
        return []
    texts: list[str] = []
    for candidate in body.get("candidates") or ():
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or ():
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return texts


def _gemini_usage(chunk: str) -> ChatUsage | None:
    try:
        body = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    meta = body.get("usageMetadata")
    if not isinstance(meta, dict):
        return None

    def count(key: str) -> int:
        value = meta.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    return ChatUsage(
        prompt=count("promptTokenCount"),
        cached=count("cachedContentTokenCount"),
        thoughts=count("thoughtsTokenCount"),
        output=count("candidatesTokenCount"),
        total=count("totalTokenCount"),
    )


class OllamaChatClient:
    """Streams plain text from an Ollama ``/api/chat`` endpoint (NDJSON)."""

    provider_name = "ollama"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = (settings.assistant_model or settings.ollama_model).strip()
        if not self.model:
            raise AssistantLLMError("an Ollama model must be configured")
        self.last_usage: ChatUsage | None = None
        self._max_output_tokens = settings.assistant_max_output_tokens
        key = _secret(settings.ollama_api_key)
        if settings.ollama_is_cloud and not key:
            raise AssistantLLMError(
                "direct Ollama Cloud requires ASSESSMENT_AI_OLLAMA_API_KEY"
            )
        self._url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.assistant_timeout_seconds, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(
        self,
        *,
        system: str,
        history: Sequence[ChatTurn],
        question: str,
    ) -> AsyncIterator[str]:
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for turn in history:
            role = "assistant" if turn.role == "assistant" else "user"
            messages.append({"role": role, "content": turn.content})
        messages.append({"role": "user", "content": question})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.4,
                "num_predict": self._max_output_tokens,
            },
        }
        self.last_usage = None

        try:
            async with self._client.stream(
                "POST", self._url, headers=self._headers, json=payload
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise AssistantLLMError(
                        f"Ollama request failed with HTTP {response.status_code}"
                    )
                async for line in response.aiter_lines():
                    text = _ollama_text(line)
                    if text:
                        yield text
                    usage = _ollama_usage(line)
                    if usage is not None:
                        self.last_usage = usage
        except httpx.RequestError as exc:
            raise AssistantLLMError("Ollama network request failed") from exc


def _ollama_text(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        body = json.loads(stripped)
    except json.JSONDecodeError:
        return ""
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def _ollama_usage(line: str) -> ChatUsage | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        body = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or not body.get("done"):
        return None
    prompt = body.get("prompt_eval_count")
    output = body.get("eval_count")
    prompt = prompt if isinstance(prompt, int) else 0
    output = output if isinstance(output, int) else 0
    return ChatUsage(prompt=prompt, output=output, total=prompt + output)


def build_chat_client(settings: Settings) -> ChatClient:
    """Return a client for the first ordered provider that is actually ready.

    Mirrors ``app.llm.build_llm_client``'s ordering so the assistant speaks to
    whatever generation is already configured for, rather than introducing a
    second, separately-configured provider for testers to get confused by.
    """

    failures: list[str] = []
    for name in settings.llm_providers:
        try:
            if name == "gemini":
                return GeminiChatClient(settings)
            if name == "ollama":
                return OllamaChatClient(settings)
        except AssistantLLMError as exc:
            failures.append(f"{name}: {exc}")
    detail = "; ".join(failures) if failures else "no provider configured"
    raise AssistantLLMError(f"no assistant provider is ready ({detail})")
