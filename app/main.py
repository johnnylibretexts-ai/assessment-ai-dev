from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .config import Settings, get_settings
from .content import ContentAdapterError, build_content_adapter
from .db import (
    Draft,
    DraftNotFoundError,
    DraftRepository,
    ReviewTransitionError,
    init_database,
)
from .llm import LLMError, build_llm_client, generation_status
from .pipeline import AssessmentPipeline, PipelineError, ReviewService
from .schemas import (
    BloomLevel,
    Choice,
    Difficulty,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceType,
)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    _ensure_sqlite_parent(resolved_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = init_database(resolved_settings.database_url)
        repository = DraftRepository(database)
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.repository = repository
        app.state.review = ReviewService(repository)
        app.state.content_factory = lambda source_type: build_content_adapter(
            resolved_settings, source_type
        )
        app.state.llm_factory = lambda: build_llm_client(resolved_settings)
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        current_generation_status = generation_status(resolved_settings)
        return JSONResponse(
            {
                "status": "ok",
                "generation": current_generation_status,
                "public_sources": "enabled"
                if resolved_settings.public_sources_enabled
                else "disabled",
                "adapt_publishing": "disabled",
            }
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        current_generation_status = generation_status(resolved_settings)
        ready = current_generation_status == "configured"
        return JSONResponse(
            {"status": "ready" if ready else current_generation_status},
            status_code=200 if ready else 503,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        drafts = request.app.state.repository.list_drafts()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "drafts": [_draft_summary(draft) for draft in drafts],
                "notice": request.query_params.get("notice"),
                "error": request.query_params.get("error"),
                "public_sources_enabled": resolved_settings.public_sources_enabled,
            },
        )

    @app.post("/generate")
    async def generate(
        request: Request,
        source_type: str = Form(SourceType.PUBLIC.value),
        source_locator: str | None = Form(None),
        sandbox_path: str | None = Form(None),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        _reviewer(request)
        try:
            locator = (source_locator or "").strip()
            selected_type = SourceType(source_type)
            if not locator and sandbox_path is not None:
                locator = sandbox_path.strip()
                selected_type = SourceType.SANDBOX
            if not locator:
                raise ValueError("Choose a source page to generate from.")
            content = request.app.state.content_factory(selected_type)
            llm = request.app.state.llm_factory()
            async with content, llm:
                pipeline = AssessmentPipeline(
                    content,
                    llm,
                    request.app.state.repository,
                    max_source_chars=resolved_settings.max_source_chars,
                )
                outcome = await pipeline.generate(locator)
        except (ContentAdapterError, LLMError, PipelineError, ValueError) as exc:
            return _redirect_with_message("/", "error", str(exc))
        return RedirectResponse(
            f"/drafts/{outcome.draft_id}?notice=Draft+generated+and+revised",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    async def draft_detail(request: Request, draft_id: int) -> HTMLResponse:
        draft = request.app.state.repository.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="Draft not found")
        return templates.TemplateResponse(
            request,
            "draft.html",
            {
                "draft": _draft_detail(draft),
                "notice": request.query_params.get("notice"),
                "error": request.query_params.get("error"),
                "bloom_options": [item.value for item in BloomLevel],
                "difficulty_options": [item.value for item in Difficulty],
            },
        )

    @app.post("/drafts/{draft_id}/edit")
    async def edit_draft(
        request: Request,
        draft_id: int,
        stem: str = Form(...),
        choice_a: str = Form(...),
        choice_b: str = Form(...),
        choice_c: str = Form(...),
        choice_d: str = Form(...),
        correct_choice: str = Form(...),
        explanation: str = Form(...),
        bloom: str = Form(...),
        difficulty: str = Form(...),
        reviewer_notes: str = Form(""),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        repository: DraftRepository = request.app.state.repository
        try:
            stored = repository.require_draft(draft_id)
            current = stored.current
            submitted = [choice_a, choice_b, choice_c, choice_d]
            choices = []
            for index, text in enumerate(submitted):
                identifier = chr(ord("A") + index)
                prior = next(
                    (item for item in current.choices if item.id == identifier), None
                )
                choices.append(
                    Choice(
                        id=identifier,
                        text=text,
                        correct=identifier == correct_choice,
                        feedback=prior.feedback if prior else None,
                    )
                )
            updated = QuestionDraft(
                concept_label=current.concept_label,
                stem=stem,
                choices=choices,
                explanation=explanation,
                bloom=BloomLevel(bloom),
                difficulty=Difficulty(difficulty),
                citation_paragraphs=current.citation_paragraphs,
                needs_human_verification=current.needs_human_verification,
            )
            repository.edit_draft(
                draft_id,
                updated,
                editor=_reviewer(request),
                notes=reviewer_notes,
            )
        except (DraftNotFoundError, ValidationError, ValueError) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        return RedirectResponse(
            f"/drafts/{draft_id}?notice=Draft+saved%3B+review+checks+were+reset",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/drafts/{draft_id}/review")
    async def review_draft(
        request: Request,
        draft_id: int,
        decision: str = Form(...),
        bloom_confirmed: bool = Form(False),
        difficulty_confirmed: bool = Form(False),
        reviewer_notes: str = Form(""),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        try:
            review_decision = ReviewDecision(
                status=ReviewStatus(decision),
                bloom_confirmed=bloom_confirmed,
                difficulty_confirmed=difficulty_confirmed,
                reviewer_notes=reviewer_notes,
            )
            request.app.state.review.decide(
                draft_id,
                review_decision,
                reviewer=_reviewer(request),
            )
        except (
            DraftNotFoundError,
            ReviewTransitionError,
            ValidationError,
            ValueError,
        ) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        label = (
            "Ready+to+publish+locally"
            if decision == "ready_to_publish"
            else "Draft+rejected"
        )
        return RedirectResponse(
            f"/drafts/{draft_id}?notice={label}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return app


def _draft_summary(draft: Draft) -> dict[str, Any]:
    current = draft.current
    return {
        "id": draft.id,
        "status": draft.status.value,
        "source_title": draft.source.title,
        "source_type": _source_type(draft.source.backend),
        "stem": current.stem,
        "bloom": current.bloom.value,
        "difficulty": current.difficulty.value,
    }


def _draft_detail(draft: Draft) -> dict[str, Any]:
    current = draft.current
    paragraphs_by_index = {
        int(paragraph["index"]): paragraph for paragraph in draft.source.paragraphs_json
    }
    cited = [
        paragraphs_by_index[index]
        for index in current.citation_paragraphs
        if index in paragraphs_by_index
    ]
    revision_call = next(
        (
            call
            for call in sorted(draft.llm_calls, key=lambda item: item.id, reverse=True)
            if call.stage == "revision"
        ),
        None,
    )
    critique = draft.critique_json
    return {
        "id": draft.id,
        "status": draft.status.value,
        "source_title": draft.source.title,
        "source_path": draft.source.canonical_path,
        "source_type": _source_type(draft.source.backend),
        "source_backend": draft.source.backend,
        "source_url": draft.source.canonical_url,
        "source_page_id": draft.source.page_id,
        "source_library": draft.source.canonical_path.split("/", 1)[0]
        if draft.source.backend == "libretexts_public"
        else "dev.libretexts.org",
        "cited_paragraphs": cited,
        "concept_label": current.concept_label,
        "stem": current.stem,
        "choices": current.choices,
        "explanation": current.explanation,
        "bloom": current.bloom.value,
        "difficulty": current.difficulty.value,
        "critique_issues": critique.get("issues", []),
        "model_id": revision_call.model_id if revision_call else "unknown model",
        "prompt_version": revision_call.prompt_version
        if revision_call
        else draft.tool_version,
        "bloom_confirmed": draft.bloom_confirmed,
        "difficulty_confirmed": draft.difficulty_confirmed,
        "reviewer_notes": draft.reviewer_notes,
    }


def _source_type(backend: str) -> str:
    return "public" if backend == "libretexts_public" else "sandbox"


def _reviewer(request: Request) -> str:
    value = request.headers.get("x-reviewer", "").strip()
    if not value:
        raise HTTPException(
            status_code=403, detail="Trusted reviewer identity required"
        )
    return value[:255]


def _require_same_origin(request: Request, settings: Settings) -> None:
    expected = settings.allowed_origin.rstrip("/")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != expected:
        raise HTTPException(
            status_code=403, detail="Cross-origin form submission refused"
        )
    referer = request.headers.get("referer")
    if not origin and not referer:
        raise HTTPException(status_code=403, detail="Form origin required")
    if not origin and referer:
        parsed = urlparse(referer)
        supplied = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if supplied != expected:
            raise HTTPException(
                status_code=403, detail="Cross-origin form submission refused"
            )


def _redirect_with_message(path: str, field: str, message: str) -> RedirectResponse:
    from urllib.parse import quote_plus

    safe_message = " ".join(message.split())[:500]
    separator = "&" if "?" in path else "?"
    return RedirectResponse(
        f"{path}{separator}{field}={quote_plus(safe_message)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url in {
        "sqlite://",
        "sqlite:///:memory:",
    }:
        return
    path_text = database_url.removeprefix(prefix)
    if not path_text or path_text == ":memory:":
        return
    Path(path_text).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


app = create_app()
