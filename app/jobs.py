from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from .config import Settings
from .db import DraftRepository, GenerationJob, GenerationJobStatus
from .pipeline import AssessmentPipeline
from .media import HotspotMediaStore
from .schemas import AssessmentItemType, GenerateRequest, SourceType


class GenerationWorker:
    """Single durable SQLite-backed worker for long-running model calls."""

    def __init__(
        self,
        settings: Settings,
        repository: DraftRepository,
        *,
        content_factory: Callable[[SourceType], AbstractAsyncContextManager[Any]],
        llm_factory: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._content_factory = content_factory
        self._llm_factory = llm_factory
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._repository.requeue_interrupted_generation_jobs()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="assessment-ai-jobs")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await asyncio.to_thread(self._repository.claim_next_generation_job)
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.25)
                except TimeoutError:
                    continue
                continue
            await self._process(job)

    async def _process(self, job: GenerationJob) -> None:
        try:
            request = GenerateRequest.model_validate(job.request_json)
            selected = _validated_item_types(self._settings, request)
            await asyncio.to_thread(
                self._repository.update_generation_job,
                job.id,
                stage="generating_and_revising",
                progress=15,
            )
            content = self._content_factory(request.source_type)
            llm = self._llm_factory()
            highest_progress = 15

            async def report(stage: str, progress: int) -> None:
                nonlocal highest_progress
                highest_progress = max(highest_progress, progress)
                await asyncio.to_thread(
                    self._repository.update_generation_job,
                    job.id,
                    stage=stage,
                    progress=highest_progress,
                )

            async with content, llm:
                pipeline = AssessmentPipeline(
                    content,
                    llm,
                    self._repository,
                    max_source_chars=self._settings.max_source_chars,
                    hotspot_media=HotspotMediaStore(self._settings),
                    progress_callback=report,
                )
                outcome = await pipeline.generate(
                    request.source_locator,
                    item_types=selected,
                    item_count=request.item_count,
                    include_hint_ladder=(
                        request.include_hint_ladder
                        and self._settings.hint_generation_enabled
                    ),
                )
            await asyncio.to_thread(
                self._repository.update_generation_job,
                job.id,
                status=GenerationJobStatus.SUCCEEDED,
                stage="complete",
                progress=100,
                draft_ids=outcome.draft_ids,
            )
        except Exception as exc:  # worker boundary must retain the durable failure
            await asyncio.to_thread(
                self._repository.update_generation_job,
                job.id,
                status=GenerationJobStatus.FAILED,
                stage="failed",
                progress=100,
                error_code=_safe_error_code(exc),
                error_message=str(exc) or "Generation failed.",
            )


def _validated_item_types(
    settings: Settings, request: GenerateRequest
) -> list[AssessmentItemType] | None:
    selected = list(request.item_types) if request.generation_mode == "selected" else None
    parameterized = {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}
    requested = set(selected or ())
    if requested & parameterized and not settings.parameterized_items_enabled:
        raise ValueError("Parameterized item generation is disabled.")
    if AssessmentItemType.WEBWORK in requested and settings.webwork_status != "configured":
        raise ValueError("The local WeBWorK engine is not configured.")
    if AssessmentItemType.IMATHAS in requested and settings.imathas_status != "configured":
        raise ValueError("The local IMathAS engine is not configured.")
    return selected


def _safe_error_code(exc: Exception) -> str:
    name = type(exc).__name__.casefold()
    return "".join(character for character in name if character.isalnum() or character == "_")[:100]
