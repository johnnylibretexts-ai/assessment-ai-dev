"""HTTP surface for the demo assistant.

Mounted only when the feature flag is on, so with the flag off these paths do
not exist at all rather than returning a disabled error.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..http_guards import require_same_origin, reviewer_identity
from .prompt import DISCLAIMER
from .service import AssistantRateLimited, AssistantService


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    route: str = Field(default="/", max_length=512)


def _sse(event: str, payload: str) -> str:
    """Encode one SSE frame.

    ``data`` is JSON-encoded so newlines inside an answer survive the transport
    -- a bare multi-line payload would be split across frames by the protocol.
    """

    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def build_assistant_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/assistant", tags=["assistant"])

    def _service(request: Request) -> AssistantService:
        service = getattr(request.app.state, "assistant_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Assistant is unavailable")
        return service

    @router.get("/conversation")
    async def conversation(request: Request) -> JSONResponse:
        reviewer = reviewer_identity(request)
        return JSONResponse(
            {
                "disclaimer": DISCLAIMER,
                "messages": _service(request).history(reviewer),
            }
        )

    @router.post("/reset")
    async def reset(request: Request) -> JSONResponse:
        require_same_origin(request, settings)
        reviewer = reviewer_identity(request)
        _service(request).reset(reviewer)
        return JSONResponse({"status": "reset"})

    @router.post("/message")
    async def message(request: Request) -> StreamingResponse:
        require_same_origin(request, settings)
        reviewer = reviewer_identity(request)
        service = _service(request)

        try:
            payload = AskRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            raise HTTPException(
                status_code=422, detail="A question is required"
            ) from None

        question = " ".join(payload.question.split())
        if not question:
            raise HTTPException(status_code=422, detail="A question is required")
        if len(question) > settings.assistant_max_message_chars:
            raise HTTPException(
                status_code=422,
                detail=(
                    "That question is too long "
                    f"(limit {settings.assistant_max_message_chars} characters)."
                ),
            )

        async def body() -> AsyncIterator[str]:
            try:
                async for event, chunk in service.answer(
                    reviewer=reviewer, question=question, route=payload.route
                ):
                    yield _sse(event, chunk)
            except AssistantRateLimited as exc:
                yield _sse("error", str(exc))

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                # Belt and braces for any proxy that buffers by default.
                "X-Accel-Buffering": "no",
            },
        )

    return router
