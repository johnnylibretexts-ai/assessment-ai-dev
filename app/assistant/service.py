"""Orchestration for one assistant exchange.

Validate, rate-limit, load history, build the prompt, stream the answer while
accumulating it, then persist. A failure mid-stream persists a marker rather
than a half-written answer, so the stored transcript never reads as though the
model said something it did not finish saying.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import AsyncIterator, Callable

from ..config import Settings
from ..db import DraftRepository
from .context import page_context
from .llm import AssistantLLMError, ChatClient, ChatTurn, build_chat_client
from .prompt import build_system_prompt
from .store import AssistantStore


# A turn is a question and its answer: two rows. Kept explicit so the setting
# named "turns" cannot silently drift back into meaning "messages".
ROWS_PER_TURN = 2


class AssistantRateLimited(RuntimeError):
    """Raised when one reviewer asks faster than the configured allowance."""


class RateLimiter:
    """Fixed-window-per-reviewer allowance.

    In-memory on purpose: the app runs as a single container, so a shared store
    would add a dependency without adding correctness.
    """

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic):
        self._per_minute = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, reviewer: str) -> None:
        now = self._clock()
        window = self._hits.setdefault(reviewer, deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self._per_minute:
            raise AssistantRateLimited(
                "Too many questions in the last minute. Give it a moment."
            )
        window.append(now)


class AssistantService:
    def __init__(
        self,
        settings: Settings,
        store: AssistantStore,
        repository: DraftRepository | None,
        *,
        client_factory: Callable[[], ChatClient] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._repository = repository
        self._client_factory = client_factory or (lambda: build_chat_client(settings))
        self._limiter = rate_limiter or RateLimiter(
            settings.assistant_rate_limit_per_minute
        )

    def _history_row_limit(self) -> int:
        return self._settings.assistant_max_turns * ROWS_PER_TURN

    def history(self, reviewer: str) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in self._store.messages(
                reviewer, limit=self._history_row_limit()
            )
            if message.role in {"user", "assistant"}
        ]

    def reset(self, reviewer: str) -> None:
        self._store.start_conversation(reviewer)

    async def answer(
        self,
        *,
        reviewer: str,
        question: str,
        route: str,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(event, payload)`` pairs: ``delta``*, then ``done`` or ``error``."""

        self._limiter.check(reviewer)

        conversation_id = self._store.ensure_conversation(reviewer, title=question)
        prior = [
            ChatTurn(role=message.role, content=message.content)
            for message in self._store.messages(
                reviewer, limit=self._history_row_limit()
            )
            if message.role in {"user", "assistant"}
        ]
        self._store.append(
            reviewer=reviewer,
            conversation_id=conversation_id,
            role="user",
            content=question,
            page_route=route,
        )

        context = (
            page_context(route, self._repository)
            if self._repository is not None
            else ""
        )

        client: ChatClient | None = None
        answered = ""
        try:
            client = self._client_factory()
            system = build_system_prompt(
                self._settings,
                self._repository,
                provider=client.provider_name,
                model=client.model,
                page_context=context,
            )
            async for piece in client.stream(
                system=system, history=prior, question=question
            ):
                if not piece:
                    continue
                answered += piece
                yield "delta", piece
        except AssistantLLMError as exc:
            self._store.append(
                reviewer=reviewer,
                conversation_id=conversation_id,
                role="error",
                content=str(exc),
                page_route=route,
                error_code="provider_error",
            )
            yield "error", "The assistant could not reach its model. Try again."
            return
        except Exception:
            self._store.append(
                reviewer=reviewer,
                conversation_id=conversation_id,
                role="error",
                content="unexpected assistant failure",
                page_route=route,
                error_code="unexpected_error",
            )
            yield "error", "The assistant hit an unexpected problem. Try again."
            return
        finally:
            if client is not None:
                await client.aclose()

        if not answered.strip():
            self._store.append(
                reviewer=reviewer,
                conversation_id=conversation_id,
                role="error",
                content="empty completion",
                page_route=route,
                error_code="empty_response",
            )
            yield "error", "The assistant returned an empty answer. Try rephrasing."
            return

        self._store.append(
            reviewer=reviewer,
            conversation_id=conversation_id,
            role="assistant",
            content=answered,
            model=client.model if client is not None else "",
            page_route=route,
        )
        yield "done", ""
