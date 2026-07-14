from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings


class EnginePublishingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IMathASQuestion:
    question_id: int
    created: bool


class IMathASBridgeClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create_question(
        self,
        *,
        publication_key: str,
        description: str,
        author: str,
        source: str,
        source_url: str,
    ) -> IMathASQuestion:
        if self._settings.imathas_publishing_status != "configured":
            raise EnginePublishingError(
                "imathas_not_configured", "The local IMathAS bridge is not configured."
            )
        token = self._settings.imathas_bridge_token
        assert token is not None
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.imathas_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._settings.resolved_imathas_bridge_questions_url,
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                    json={
                        "publication_key": publication_key,
                        "description": description,
                        "author": author,
                        "source": source,
                        "source_url": source_url,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EnginePublishingError(
                "imathas_unavailable", "The local IMathAS bridge is unavailable."
            ) from exc
        if response.status_code == 401:
            raise EnginePublishingError(
                "imathas_auth_failed",
                "The local IMathAS bridge rejected its service credentials.",
            )
        if response.status_code >= 400:
            raise EnginePublishingError(
                "imathas_create_failed",
                "The local IMathAS question could not be created.",
            )
        try:
            body = response.json()
            return IMathASQuestion(
                question_id=int(body["question_id"]), created=bool(body["created"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EnginePublishingError(
                "imathas_invalid_response",
                "The local IMathAS bridge returned an invalid response.",
            ) from exc
