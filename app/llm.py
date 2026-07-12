from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Generic, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings


ModelT = TypeVar("ModelT", bound=BaseModel)
JsonScalar = str | int | float | bool | None

_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    flags=re.IGNORECASE | re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")
_API_KEY_RE = re.compile(r"(?i)((?:api[_-]?key|access[_-]?token)\s*[:=]\s*)[^\s,;]+")
_MAX_STORED_RAW_CHARS = 100_000
_RESPONSE_METADATA_FIELDS = (
    "model",
    "created_at",
    "done",
    "done_reason",
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
)


class LLMError(RuntimeError):
    """Base exception for safe-to-display LLM client failures."""


class LLMConfigurationError(LLMError):
    """Raised when the selected provider cannot be used safely."""


class LLMTransportError(LLMError):
    """Raised when Ollama cannot complete the HTTP request."""


class LLMStructuredOutputError(LLMError):
    """Raised after all structured-output validation attempts fail."""


class LLMAttemptMetadata(BaseModel):
    """Safe, serializable provenance for one model response."""

    model_config = ConfigDict(frozen=True)

    attempt: int = Field(ge=1)
    raw_response: str
    response_metadata: dict[str, JsonScalar] = Field(default_factory=dict)
    validation_error: str | None = None


class LLMCallMetadata(BaseModel):
    """Provenance retained with a successfully validated completion."""

    model_config = ConfigDict(frozen=True)

    provider: str = "unknown"
    model: str
    prompt_version: str
    attempt: int = Field(ge=1)
    raw_response: str
    response_metadata: dict[str, JsonScalar] = Field(default_factory=dict)
    attempts: tuple[LLMAttemptMetadata, ...]


class LLMResult(BaseModel, Generic[ModelT]):
    """A validated model value paired with audit-safe call metadata."""

    model_config = ConfigDict(frozen=True)

    value: ModelT
    metadata: LLMCallMetadata


class LLMClient(Protocol):
    """Provider-independent structured completion surface."""

    async def complete(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[ModelT]: ...


class OllamaClient:
    """Structured-output client for direct Ollama Cloud or local Ollama."""

    provider_name = "ollama"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")

        parsed_base_url = urlparse(settings.ollama_base_url)
        self._is_direct_cloud = parsed_base_url.hostname == "ollama.com"
        self._secret = (
            settings.ollama_api_key.get_secret_value()
            if settings.ollama_api_key is not None
            else None
        )

        if self._is_direct_cloud:
            if parsed_base_url.scheme != "https":
                raise LLMConfigurationError("direct Ollama Cloud requires HTTPS")
            if not self._secret:
                raise LLMConfigurationError(
                    "direct Ollama Cloud requires ASSESSMENT_AI_OLLAMA_API_KEY"
                )

        if not settings.ollama_model.strip():
            raise LLMConfigurationError("an Ollama model must be configured")

        self._url = f"{settings.ollama_base_url}/api/chat"
        self._model = settings.ollama_model
        normalized_model = self._model.casefold()
        self._supports_native_schema = not (
            self._is_direct_cloud
            or normalized_model.endswith(":cloud")
            or normalized_model.endswith("-cloud")
        )
        self._max_attempts = settings.ollama_max_retries + 1
        self._headers = (
            {"Authorization": f"Bearer {self._secret}"} if self._is_direct_cloud else {}
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.ollama_timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[ModelT]:
        """Return a Pydantic-validated completion or fail after bounded retries."""

        if not prompt_version.strip():
            raise ValueError("prompt_version must not be blank")

        json_schema = schema.model_json_schema()
        validation_feedback: str | None = None
        attempts: list[LLMAttemptMetadata] = []

        for attempt_number in range(1, self._max_attempts + 1):
            request_prompt = _render_prompt(
                prompt,
                json_schema,
                validation_feedback=validation_feedback,
            )
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": [{"role": "user", "content": request_prompt}],
                "stream": False,
                "options": {"temperature": 0},
            }
            if self._supports_native_schema:
                payload["format"] = json_schema

            response = await self._post(payload)
            raw_response, response_metadata, envelope_error = self._read_response(
                response
            )

            if envelope_error is None:
                value, validation_feedback = _parse_and_validate(raw_response, schema)
            else:
                value = None
                validation_feedback = envelope_error

            safe_raw_response = self._safe_text(raw_response)
            safe_feedback = self._safe_text(validation_feedback)
            validation_feedback = safe_feedback
            safe_response_metadata = {
                key: self._safe_scalar(value)
                for key, value in response_metadata.items()
            }
            attempts.append(
                LLMAttemptMetadata(
                    attempt=attempt_number,
                    raw_response=safe_raw_response,
                    response_metadata=safe_response_metadata,
                    validation_error=None if value is not None else safe_feedback,
                )
            )

            if value is not None:
                metadata = LLMCallMetadata(
                    provider=self.provider_name,
                    model=self._model,
                    prompt_version=prompt_version,
                    attempt=attempt_number,
                    raw_response=safe_raw_response,
                    response_metadata=safe_response_metadata,
                    attempts=tuple(attempts),
                )
                return LLMResult[ModelT](value=value, metadata=metadata)

        failure = self._safe_text(validation_feedback or "invalid structured output")
        raise LLMStructuredOutputError(
            f"Ollama returned invalid structured output after "
            f"{self._max_attempts} attempt(s): {failure}"
        ) from None

    async def _post(self, payload: Mapping[str, Any]) -> httpx.Response:
        try:
            response = await self._client.post(
                self._url,
                headers=self._headers,
                json=payload,
            )
        except httpx.RequestError:
            # Request exceptions may contain caller-controlled URLs or headers.
            raise LLMTransportError("Ollama request failed") from None

        if response.is_error:
            # Deliberately exclude response bodies and headers: providers sometimes
            # echo credentials in diagnostics.
            raise LLMTransportError(
                f"Ollama request failed with HTTP {response.status_code}"
            ) from None
        return response

    def _read_response(
        self,
        response: httpx.Response,
    ) -> tuple[str, dict[str, JsonScalar], str | None]:
        try:
            body = response.json()
        except ValueError:
            return response.text, {}, "response envelope was not valid JSON"

        if not isinstance(body, dict):
            return _json_for_audit(body), {}, "response envelope must be a JSON object"

        response_metadata = {
            key: value
            for key in _RESPONSE_METADATA_FIELDS
            if isinstance((value := body.get(key)), (str, int, float, bool))
            or value is None
            and key in body
        }
        message = body.get("message")
        if not isinstance(message, dict):
            return (
                _json_for_audit(body),
                response_metadata,
                "response message was missing",
            )

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return (
                _json_for_audit(body),
                response_metadata,
                "response content was missing",
            )
        return content, response_metadata, None

    def _safe_text(self, value: str) -> str:
        redacted = value
        if self._secret:
            redacted = redacted.replace(self._secret, "[REDACTED]")
        redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
        redacted = _API_KEY_RE.sub(r"\1[REDACTED]", redacted)
        if len(redacted) > _MAX_STORED_RAW_CHARS:
            return redacted[:_MAX_STORED_RAW_CHARS] + "\n[TRUNCATED]"
        return redacted

    def _safe_scalar(self, value: JsonScalar) -> JsonScalar:
        return self._safe_text(value) if isinstance(value, str) else value


class GeminiClient:
    """Structured-output client for Google's Gemini generateContent API."""

    provider_name = "gemini"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")

        parsed_base_url = urlparse(settings.gemini_base_url)
        if parsed_base_url.scheme != "https":
            raise LLMConfigurationError("Gemini API requires HTTPS")

        self._secret = (
            settings.gemini_api_key.get_secret_value()
            if settings.gemini_api_key is not None
            else None
        )
        if not self._secret or not self._secret.strip():
            raise LLMConfigurationError("Gemini requires ASSESSMENT_AI_GEMINI_API_KEY")
        if not settings.gemini_model.strip():
            raise LLMConfigurationError("a Gemini model must be configured")

        self._model = settings.gemini_model.strip()
        self._url = f"{settings.gemini_base_url}/models/{self._model}:generateContent"
        self._max_attempts = settings.gemini_max_retries + 1
        self._sleep = sleep
        self._headers = {"x-goog-api-key": self._secret}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.gemini_timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> GeminiClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[ModelT]:
        if not prompt_version.strip():
            raise ValueError("prompt_version must not be blank")

        json_schema = schema.model_json_schema()
        validation_feedback: str | None = None
        attempts: list[LLMAttemptMetadata] = []

        for attempt_number in range(1, self._max_attempts + 1):
            request_prompt = _render_prompt(
                prompt,
                json_schema,
                validation_feedback=validation_feedback,
            )
            payload: dict[str, Any] = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": request_prompt}],
                    }
                ],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": json_schema,
                },
            }

            response = await self._post(payload)
            raw_response, response_metadata, envelope_error = self._read_response(
                response
            )
            if envelope_error is None:
                value, validation_feedback = _parse_and_validate(raw_response, schema)
            else:
                value = None
                validation_feedback = envelope_error

            safe_raw_response = self._safe_text(raw_response)
            safe_feedback = self._safe_text(validation_feedback)
            validation_feedback = safe_feedback
            safe_response_metadata = {
                key: self._safe_scalar(value)
                for key, value in response_metadata.items()
            }
            attempts.append(
                LLMAttemptMetadata(
                    attempt=attempt_number,
                    raw_response=safe_raw_response,
                    response_metadata=safe_response_metadata,
                    validation_error=None if value is not None else safe_feedback,
                )
            )

            if value is not None:
                metadata = LLMCallMetadata(
                    provider=self.provider_name,
                    model=self._model,
                    prompt_version=prompt_version,
                    attempt=attempt_number,
                    raw_response=safe_raw_response,
                    response_metadata=safe_response_metadata,
                    attempts=tuple(attempts),
                )
                return LLMResult[ModelT](value=value, metadata=metadata)

        failure = self._safe_text(validation_feedback or "invalid structured output")
        raise LLMStructuredOutputError(
            f"Gemini returned invalid structured output after "
            f"{self._max_attempts} attempt(s): {failure}"
        ) from None

    async def _post(self, payload: Mapping[str, Any]) -> httpx.Response:
        for request_attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(
                    self._url,
                    headers=self._headers,
                    json=payload,
                )
            except httpx.RequestError:
                if request_attempt >= self._max_attempts:
                    raise LLMTransportError(
                        "Gemini network request failed after "
                        f"{request_attempt} attempt(s)"
                    ) from None
                await self._sleep(min(0.5 * request_attempt, 2.0))
                continue

            retryable_status = response.status_code == 429 or (
                500 <= response.status_code <= 599
            )
            if retryable_status and request_attempt < self._max_attempts:
                await self._sleep(min(0.5 * request_attempt, 2.0))
                continue
            if response.is_error:
                raise LLMTransportError(
                    f"Gemini request failed with HTTP {response.status_code}"
                ) from None
            return response

        raise LLMTransportError("Gemini request failed")

    def _read_response(
        self,
        response: httpx.Response,
    ) -> tuple[str, dict[str, JsonScalar], str | None]:
        try:
            body = response.json()
        except ValueError:
            return response.text, {}, "response envelope was not valid JSON"

        if not isinstance(body, dict):
            return _json_for_audit(body), {}, "response envelope must be a JSON object"

        metadata: dict[str, JsonScalar] = {}
        for key in ("modelVersion", "responseId"):
            value = body.get(key)
            if (
                isinstance(value, (str, int, float, bool))
                or value is None
                and key in body
            ):
                metadata[key] = value
        usage = body.get("usageMetadata")
        if isinstance(usage, dict):
            for key in (
                "promptTokenCount",
                "candidatesTokenCount",
                "totalTokenCount",
                "cachedContentTokenCount",
                "thoughtsTokenCount",
            ):
                value = usage.get(key)
                if isinstance(value, (str, int, float, bool)) or (
                    value is None and key in usage
                ):
                    metadata[key] = value

        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            prompt_feedback = body.get("promptFeedback")
            error = (
                "response was blocked by the provider safety policy"
                if isinstance(prompt_feedback, dict)
                and prompt_feedback.get("blockReason")
                else "response candidates were missing"
            )
            return _json_for_audit(body), metadata, error

        candidate = candidates[0]
        if not isinstance(candidate, dict):
            return _json_for_audit(body), metadata, "response candidate was invalid"
        finish_reason = candidate.get("finishReason")
        if isinstance(finish_reason, str):
            metadata["finishReason"] = finish_reason

        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            return _json_for_audit(body), metadata, "response content was missing"
        text_parts = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and not part.get("thought", False)
            and isinstance(part.get("text"), str)
        ]
        raw_response = "".join(text_parts)
        if not raw_response.strip():
            return _json_for_audit(body), metadata, "response text was missing"
        return raw_response, metadata, None

    def _safe_text(self, value: str) -> str:
        redacted = value.replace(self._secret, "[REDACTED]")
        redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
        redacted = _API_KEY_RE.sub(r"\1[REDACTED]", redacted)
        if len(redacted) > _MAX_STORED_RAW_CHARS:
            return redacted[:_MAX_STORED_RAW_CHARS] + "\n[TRUNCATED]"
        return redacted

    def _safe_scalar(self, value: JsonScalar) -> JsonScalar:
        return self._safe_text(value) if isinstance(value, str) else value


class LLMProviderChain:
    """Try configured providers in order and retain the successful provenance."""

    provider_name = "provider-chain"

    def __init__(self, providers: Sequence[tuple[str, LLMClient]]) -> None:
        if not providers:
            raise LLMConfigurationError("no configured LLM provider is ready")
        self._providers = tuple(providers)

    async def __aenter__(self) -> LLMProviderChain:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for _name, client in reversed(self._providers):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    async def complete(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[ModelT]:
        failures: list[tuple[str, str]] = []
        for name, client in self._providers:
            try:
                result = await client.complete(
                    prompt,
                    schema,
                    prompt_version=prompt_version,
                )
            except LLMError as exc:
                failures.append((name, str(exc)))
                continue

            metadata = result.metadata
            response_metadata = dict(metadata.response_metadata)
            response_metadata["fallback_count"] = len(failures)
            if failures:
                response_metadata["fallback_from"] = ",".join(
                    provider for provider, _message in failures
                )
            metadata = metadata.model_copy(
                update={
                    "provider": name,
                    "response_metadata": response_metadata,
                }
            )
            return result.model_copy(update={"metadata": metadata})

        summary = "; ".join(f"{name}: {message}" for name, message in failures)
        raise LLMError(f"all configured LLM providers failed ({summary})") from None


def provider_is_ready(settings: Settings, provider: str) -> bool:
    if provider == "ollama":
        if not settings.ollama_is_cloud:
            return True
        return _secret_is_set(settings.ollama_api_key)
    if provider == "gemini":
        return _secret_is_set(settings.gemini_api_key)
    return False


def generation_status(settings: Settings) -> str:
    if any(provider_is_ready(settings, name) for name in settings.llm_providers):
        return "configured"
    if settings.llm_providers == ("ollama",):
        return "needs_ollama_api_key"
    if settings.llm_providers == ("gemini",):
        return "needs_gemini_api_key"
    return "needs_llm_api_key"


def build_llm_client(settings: Settings) -> LLMClient:
    providers: list[tuple[str, LLMClient]] = []
    for name in settings.llm_providers:
        if not provider_is_ready(settings, name):
            continue
        client: LLMClient
        if name == "ollama":
            client = OllamaClient(settings)
        else:
            client = GeminiClient(settings)
        providers.append((name, client))
    if not providers:
        raise LLMConfigurationError("no configured LLM provider is ready")
    if len(providers) == 1:
        return providers[0][1]
    return LLMProviderChain(providers)


def _secret_is_set(secret: Any) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


def _render_prompt(
    prompt: str,
    json_schema: dict[str, Any],
    *,
    validation_feedback: str | None,
) -> str:
    schema_text = json.dumps(json_schema, ensure_ascii=False, sort_keys=True)
    parts = [
        prompt,
        "Return exactly one JSON value and no explanatory text.",
        "The JSON must validate against this schema:",
        schema_text,
    ]
    if validation_feedback:
        parts.extend(
            (
                "The previous response was invalid. Correct this validation feedback:",
                validation_feedback,
            )
        )
    return "\n\n".join(parts)


def _parse_and_validate(
    raw_response: str,
    schema: type[ModelT],
) -> tuple[ModelT | None, str]:
    candidates = _decode_json_candidates(raw_response)
    if not candidates:
        return None, "response did not contain valid JSON"

    errors: list[str] = []
    for candidate in candidates:
        try:
            return schema.model_validate(candidate), ""
        except ValidationError as exc:
            errors.append(_concise_validation_error(exc))
    return None, errors[0] if errors else "response did not match the JSON schema"


def _decode_json_candidates(raw_response: str) -> list[Any]:
    stripped = raw_response.strip()
    if not stripped:
        return []

    try:
        return [json.loads(stripped)]
    except json.JSONDecodeError:
        pass

    decoded: list[Any] = []
    for fenced in _FENCED_JSON_RE.findall(stripped):
        try:
            decoded.append(json.loads(fenced.strip()))
        except json.JSONDecodeError:
            continue
    if decoded:
        return decoded

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", stripped):
        try:
            value, _end = decoder.raw_decode(stripped, match.start())
        except json.JSONDecodeError:
            continue
        return [value]
    return []


def _concise_validation_error(exc: ValidationError) -> str:
    summaries: list[str] = []
    for detail in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:3]:
        location = ".".join(str(part) for part in detail.get("loc", ())) or "root"
        message = " ".join(str(detail.get("msg", "invalid value")).split())
        summaries.append(f"{location}: {message}"[:240])
    remaining = exc.error_count() - len(summaries)
    if remaining > 0:
        summaries.append(f"and {remaining} more validation error(s)")
    return "; ".join(summaries) or "response did not match the JSON schema"


def _json_for_audit(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[unserializable response]"
