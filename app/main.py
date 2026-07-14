from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .adapt import AdaptClient, AdaptPublishingError
from .catalog import chemistry_seed, source_license, suggested_topic
from .config import Settings, get_settings
from .content import ContentAdapterError, build_content_adapter
from .db import (
    Draft,
    DraftNotFoundError,
    DraftRepository,
    PublicationState,
    ReviewTransitionError,
    init_database,
)
from .jobs import GenerationWorker
from .llm import LLMError, build_llm_client, generation_status
from .media import HotspotMediaStore
from .pipeline import AssessmentPipeline, PipelineError, ReviewService
from .publishing import (
    LicenseSelection,
    PublicationService,
    PublicationValidationError,
)
from .schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    Difficulty,
    QuestionDraft,
    GenerateRequest,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    ReviewDecision,
    ReviewStatus,
    SourceType,
)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
REVIEW_CONFIRMATION_ERROR = (
    "Confirm both the Bloom level and difficulty before approving this draft."
)
REVIEW_VALIDATION_ERROR = "Review could not be saved. Check the form and try again."


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    _ensure_sqlite_parent(resolved_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = init_database(resolved_settings.database_url)
        repository = DraftRepository(database)
        adapt_client = AdaptClient(resolved_settings)
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.repository = repository
        app.state.review = ReviewService(repository)
        app.state.adapt_client = adapt_client
        app.state.publisher = PublicationService(
            resolved_settings, repository, adapt_client
        )
        app.state.content_factory = lambda source_type: build_content_adapter(
            resolved_settings, source_type
        )
        app.state.llm_factory = lambda: build_llm_client(resolved_settings)
        worker = GenerationWorker(
            resolved_settings,
            repository,
            content_factory=app.state.content_factory,
            llm_factory=app.state.llm_factory,
        )
        app.state.generation_worker = worker
        worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await adapt_client.aclose()
            database.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    resolved_settings.hotspot_media_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/media",
        StaticFiles(directory=resolved_settings.hotspot_media_dir),
        name="hotspot-media",
    )

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
                "sandbox_sources": "disabled",
                "adapt_publishing": resolved_settings.adapt_publishing_status,
                "advanced_items": "enabled"
                if resolved_settings.advanced_items_enabled
                else "disabled",
                "parameterized_items": "enabled"
                if resolved_settings.parameterized_items_enabled
                else "disabled",
                "hint_generation": "enabled"
                if resolved_settings.hint_generation_enabled
                else "disabled",
                "webwork": resolved_settings.webwork_status,
                "imathas": resolved_settings.imathas_status,
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
        drafts = [
            draft
            for draft in request.app.state.repository.list_drafts()
            if draft.source.backend == "libretexts_public"
        ]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "drafts": [_draft_summary(draft) for draft in drafts],
                "notice": request.query_params.get("notice"),
                "error": request.query_params.get("error"),
                "public_sources_enabled": resolved_settings.public_sources_enabled,
                "advanced_items_enabled": resolved_settings.advanced_items_enabled,
                "hint_generation_enabled": resolved_settings.hint_generation_enabled,
                "item_type_options": [item.value for item in AssessmentItemType],
            },
        )

    @app.post("/generate")
    async def generate(
        request: Request,
        source_type: str = Form(SourceType.PUBLIC.value),
        source_locator: str | None = Form(None),
        sandbox_path: str | None = Form(None),
        generation_mode: str = Form("auto"),
        item_types: list[str] = Form(default_factory=list),
        item_count: int = Form(4),
        include_hint_ladder: bool = Form(False),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        _reviewer(request)
        try:
            locator = (source_locator or "").strip()
            selected_type = SourceType(source_type)
            if selected_type is not SourceType.PUBLIC or sandbox_path is not None:
                raise ValueError("Dev sandbox sources are disabled for this service.")
            if not locator:
                raise ValueError("Choose a public LibreTexts page to generate from.")
            if resolved_settings.advanced_items_enabled:
                generation_request = GenerateRequest(
                    source_type=selected_type,
                    source_locator=locator,
                    generation_mode=generation_mode,
                    item_types=[AssessmentItemType(item) for item in item_types],
                    item_count=item_count,
                    include_hint_ladder=include_hint_ladder,
                )
                job = request.app.state.repository.create_generation_job(
                    source_type=selected_type.value,
                    source_locator=locator,
                    request=generation_request.model_dump(mode="json"),
                    reviewer=_reviewer(request),
                )
                return RedirectResponse(
                    f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
                )
            content = request.app.state.content_factory(selected_type)
            llm = request.app.state.llm_factory()
            async with content, llm:
                pipeline = AssessmentPipeline(
                    content,
                    llm,
                    request.app.state.repository,
                    max_source_chars=resolved_settings.max_source_chars,
                    hotspot_media=HotspotMediaStore(resolved_settings),
                )
                outcome = await pipeline.generate(locator)
        except (ContentAdapterError, LLMError, PipelineError, ValueError) as exc:
            return _redirect_with_message("/", "error", str(exc))
        return RedirectResponse(
            f"/drafts/{outcome.draft_id}?notice=Draft+generated+and+revised",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}")
    async def generation_job(request: Request, job_id: str):
        reviewer = _reviewer(request)
        job = request.app.state.repository.get_generation_job(job_id)
        if job is None or job.reviewer_identity != reviewer:
            raise HTTPException(status_code=404, detail="Generation job not found")
        payload = {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "draft_ids": job.draft_ids_json,
            "error": job.error_message,
            "draft_url": (
                f"/drafts/{job.draft_ids_json[0]}" if job.draft_ids_json else None
            ),
            "queue_url": "/",
        }
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(payload)
        return templates.TemplateResponse(
            request,
            "job.html",
            {"job": payload},
        )

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    async def draft_detail(request: Request, draft_id: int) -> HTMLResponse:
        draft = _require_public_draft(request.app.state.repository, draft_id)
        seed = chemistry_seed()
        topics_by_chapter = [
            {
                "title": chapter["title"],
                "topics": chapter["topics"],
            }
            for chapter in seed["chapters"]
        ]
        mapped_license = source_license(draft.source.canonical_url)
        suggestion = suggested_topic(draft.source.canonical_url)
        return templates.TemplateResponse(
            request,
            "draft.html",
            {
                "draft": _draft_detail(draft),
                "notice": request.query_params.get("notice"),
                "error": request.query_params.get("error"),
                "bloom_options": [item.value for item in BloomLevel],
                "difficulty_options": [item.value for item in Difficulty],
                "adapt_publishing_status": resolved_settings.adapt_publishing_status,
                "adapt_folder_name": resolved_settings.adapt_folder_name,
                "adapt_public": resolved_settings.adapt_public,
                "mapped_license": mapped_license,
                "topic_groups": topics_by_chapter,
                "suggested_topic_id": suggestion.stable_id if suggestion else None,
                "manual_license_options": [
                    ("publicdomain", "Public domain"),
                    ("ccby", "CC BY"),
                    ("ccbync", "CC BY-NC"),
                    ("ccbyncsa", "CC BY-NC-SA"),
                    ("ccbysa", "CC BY-SA"),
                    ("arr", "All rights reserved"),
                ],
            },
        )

    @app.post("/drafts/{draft_id}/edit")
    async def edit_draft(
        request: Request,
        draft_id: int,
        item_json: str | None = Form(None),
        stem: str | None = Form(None),
        choice_a: str | None = Form(None),
        choice_b: str | None = Form(None),
        choice_c: str | None = Form(None),
        choice_d: str | None = Form(None),
        correct_choice: str | None = Form(None),
        explanation: str | None = Form(None),
        bloom: str | None = Form(None),
        difficulty: str | None = Form(None),
        reviewer_notes: str = Form(""),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        repository: DraftRepository = request.app.state.repository
        _require_public_draft(repository, draft_id)
        try:
            stored = repository.require_draft(draft_id)
            current = stored.current
            if item_json is not None:
                updated = QuestionDraft.model_validate(json.loads(item_json))
            else:
                if None in {
                    stem,
                    choice_a,
                    choice_b,
                    choice_c,
                    choice_d,
                    correct_choice,
                    explanation,
                    bloom,
                    difficulty,
                }:
                    raise ValueError("The edited multiple-choice item is incomplete.")
                submitted = [choice_a, choice_b, choice_c, choice_d]
                choices = []
                for index, text in enumerate(submitted):
                    identifier = chr(ord("A") + index)
                    prior = next(
                        (item for item in current.choices if item.id == identifier),
                        None,
                    )
                    choices.append(
                        Choice(
                            id=identifier,
                            text=str(text),
                            correct=identifier == correct_choice,
                            feedback=prior.feedback if prior else None,
                        )
                    )
                updated = QuestionDraft(
                    concept_label=current.concept_label,
                    stem=str(stem),
                    choices=choices,
                    explanation=str(explanation),
                    bloom=BloomLevel(str(bloom)),
                    difficulty=Difficulty(str(difficulty)),
                    citation_paragraphs=current.citation_paragraphs,
                    needs_human_verification=current.needs_human_verification,
                )
            repository.edit_draft(
                draft_id,
                updated,
                editor=_reviewer(request),
                notes=reviewer_notes,
            )
        except (DraftNotFoundError, ValidationError, ValueError, json.JSONDecodeError) as exc:
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
        specialist_confirmed: bool = Form(False),
        reviewer_notes: str = Form(""),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        stored_draft = _require_public_draft(request.app.state.repository, draft_id)
        if decision == ReviewStatus.READY_TO_PUBLISH.value and (
            not bloom_confirmed or not difficulty_confirmed
        ):
            return _redirect_with_message(
                f"/drafts/{draft_id}",
                "error",
                REVIEW_CONFIRMATION_ERROR,
            )
        if (
            decision == ReviewStatus.READY_TO_PUBLISH.value
            and stored_draft.current.specialist_review_required
            and not specialist_confirmed
        ):
            return _redirect_with_message(
                f"/drafts/{draft_id}",
                "error",
                "A qualified specialist must confirm this item before approval.",
            )
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
        except ValidationError:
            return _redirect_with_message(
                f"/drafts/{draft_id}",
                "error",
                REVIEW_VALIDATION_ERROR,
            )
        except (
            DraftNotFoundError,
            ReviewTransitionError,
            ValueError,
        ) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        label = (
            "Draft+approved%3B+not+yet+published"
            if decision == "ready_to_publish"
            else "Draft+rejected"
        )
        return RedirectResponse(
            f"/drafts/{draft_id}?notice={label}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/drafts/{draft_id}/hints/edit")
    async def edit_hint_ladder(
        request: Request,
        draft_id: int,
        conceptual_text: str = Form(...),
        conceptual_citations: str = Form(...),
        strategic_text: str = Form(...),
        strategic_citations: str = Form(...),
        specific_text: str = Form(...),
        specific_citations: str = Form(...),
        reviewer_notes: str = Form(""),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        draft = _require_public_draft(request.app.state.repository, draft_id)
        try:
            ladder = HintLadderDraft(
                concept_label=draft.current.concept_label,
                rungs=[
                    HintRungDraft(
                        rung=HintRungType.CONCEPTUAL,
                        text=conceptual_text,
                        citation_paragraphs=_parse_citations(conceptual_citations),
                    ),
                    HintRungDraft(
                        rung=HintRungType.STRATEGIC,
                        text=strategic_text,
                        citation_paragraphs=_parse_citations(strategic_citations),
                    ),
                    HintRungDraft(
                        rung=HintRungType.SPECIFIC,
                        text=specific_text,
                        citation_paragraphs=_parse_citations(specific_citations),
                    ),
                ],
            )
            request.app.state.repository.save_hint_ladder(
                draft_id,
                ladder,
                editor=_reviewer(request),
                notes=reviewer_notes,
            )
        except (ValidationError, ValueError) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        return _redirect_with_message(
            f"/drafts/{draft_id}",
            "notice",
            "Hint ladder saved; rung confirmations were reset",
        )

    @app.post("/drafts/{draft_id}/hints/review")
    async def review_hint_ladder(
        request: Request,
        draft_id: int,
        conceptual_confirmed: bool = Form(False),
        strategic_confirmed: bool = Form(False),
        specific_confirmed: bool = Form(False),
        reviewer_notes: str = Form(""),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        _require_public_draft(request.app.state.repository, draft_id)
        confirmed = [
            rung
            for rung, value in (
                ("conceptual", conceptual_confirmed),
                ("strategic", strategic_confirmed),
                ("specific", specific_confirmed),
            )
            if value
        ]
        try:
            request.app.state.repository.review_hint_ladder(
                draft_id,
                reviewer=_reviewer(request),
                confirmed_rungs=confirmed,
                approved=True,
                notes=reviewer_notes,
            )
        except (DraftNotFoundError, ReviewTransitionError, ValueError) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        return _redirect_with_message(
            f"/drafts/{draft_id}", "notice", "Hint ladder approved"
        )

    @app.post("/drafts/{draft_id}/publish")
    async def publish_draft(
        request: Request,
        draft_id: int,
        topic_stable_id: str = Form(...),
        alignment_confirmed: bool = Form(False),
        license_code: str | None = Form(None),
        license_version: str | None = Form(None),
        license_label: str | None = Form(None),
        license_evidence_url: str | None = Form(None),
        license_confirmed: bool = Form(False),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        reviewer = _reviewer(request)
        _require_public_draft(request.app.state.repository, draft_id)
        selected_license = None
        if license_code:
            selected_license = LicenseSelection(
                code=license_code.strip(),
                version=(license_version or "").strip() or None,
                label=(license_label or "").strip(),
                evidence_url=(license_evidence_url or "").strip(),
            )
        try:
            publication = await request.app.state.publisher.publish(
                draft_id,
                publisher=reviewer,
                topic_stable_id=topic_stable_id,
                alignment_confirmed=alignment_confirmed,
                selected_license=selected_license,
                license_confirmed=license_confirmed,
            )
        except (PublicationValidationError, AdaptPublishingError, ValueError) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        if publication.state == PublicationState.SUCCEEDED.value:
            return _redirect_with_message(
                f"/drafts/{draft_id}", "notice", "Published to ADAPT"
            )
        return _redirect_with_message(
            f"/drafts/{draft_id}",
            "error",
            publication.error_message
            or "Publishing did not complete. Review the publication status below.",
        )

    @app.get("/drafts/{draft_id}/publications/{publication_id}/qti")
    async def download_qti(
        request: Request, draft_id: int, publication_id: int
    ) -> FileResponse:
        _require_same_origin(request, resolved_settings)
        _reviewer(request)
        _require_public_draft(request.app.state.repository, draft_id)
        publication = request.app.state.repository.get_publication(publication_id)
        if (
            publication is None
            or publication.draft_id != draft_id
            or publication.state != PublicationState.SUCCEEDED.value
            or not publication.qti_path
        ):
            raise HTTPException(status_code=404, detail="QTI package not found")
        storage_root = Path(resolved_settings.qti_storage_dir).resolve()
        artifact = Path(publication.qti_path).resolve()
        if (
            artifact.parent != storage_root
            or artifact.name != f"{publication.publication_key}.zip"
            or not artifact.is_file()
        ):
            raise HTTPException(status_code=404, detail="QTI package not found")
        return FileResponse(
            artifact,
            media_type="application/zip",
            filename=f"assessment-ai-{publication.publication_key}.zip",
        )

    return app


def _draft_summary(draft: Draft) -> dict[str, Any]:
    current = draft.current
    return {
        "id": draft.id,
        "status": draft.status.value,
        "status_label": _status_label(draft.status.value),
        "source_title": draft.source.title,
        "source_type": _source_type(draft.source.backend),
        "stem": current.stem,
        "item_type": current.item_type.value,
        "context_type": current.context_type.value,
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
    publications = sorted(draft.publications, key=lambda item: item.id, reverse=True)
    hint_record = draft.current_hint_ladder
    hint_ladder = hint_record.ladder if hint_record is not None else None
    engine_validation = draft.current_engine_validation
    return {
        "id": draft.id,
        "status": draft.status.value,
        "status_label": _status_label(draft.status.value),
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
        "item_type": current.item_type.value,
        "item_type_label": current.item_type.value.replace("_", " "),
        "context_type": current.context_type.value,
        "stimulus": current.stimulus,
        "set_key": current.set_key,
        "choices": current.choices,
        "response": current.response.model_dump(mode="json"),
        "item_json": json.dumps(
            current.model_dump(mode="json"), indent=2, ensure_ascii=False
        ),
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
        "edit_count": draft.edit_count,
        "specialist_review_required": current.specialist_review_required,
        "engine_validation": {
            "engine": engine_validation.engine,
            "compiler_version": engine_validation.compiler_version,
            "source_sha256": engine_validation.source_sha256,
            "seed_count": engine_validation.seed_count,
            "status": engine_validation.status,
            "previews": engine_validation.previews_json[:5],
        }
        if engine_validation is not None
        else None,
        "hint_ladder": {
            "id": hint_record.id,
            "status": hint_record.status,
            "confirmations": hint_record.confirmations_json,
            "reviewer_notes": hint_record.reviewer_notes,
            "rungs": [
                {
                    "rung": rung.rung.value,
                    "text": rung.text,
                    "citation_paragraphs": rung.citation_paragraphs,
                    "citations_text": ", ".join(
                        str(item) for item in rung.citation_paragraphs
                    ),
                    "answer_leak_detected": rung.answer_leak_detected,
                }
                for rung in hint_ladder.rungs
            ],
        }
        if hint_record is not None and hint_ladder is not None
        else None,
        "publications": [
            {
                "id": publication.id,
                "edit_count": publication.edit_count,
                "state": publication.state,
                "adapt_question_id": publication.adapt_question_id,
                "adapt_page_id": publication.adapt_page_id,
                "framework_title": publication.framework_title,
                "topic": publication.alignment_json.get("topic", {}).get("text"),
                "license_label": publication.license_label,
                "finalized_at": publication.finalized_at,
                "error_message": publication.error_message,
                "is_current_edit": publication.edit_count == draft.edit_count,
            }
            for publication in publications
        ],
    }


def _status_label(status_value: str) -> str:
    if status_value == ReviewStatus.READY_TO_PUBLISH.value:
        return "Approved — not yet published"
    return status_value.replace("_", " ")


def _source_type(backend: str) -> str:
    return "public" if backend == "libretexts_public" else "sandbox"


def _parse_citations(value: str) -> list[int]:
    try:
        citations = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError:
        raise ValueError("Hint citations must be comma-separated paragraph numbers.") from None
    if not citations or any(item < 0 for item in citations):
        raise ValueError("Each hint requires at least one valid paragraph citation.")
    if len(citations) != len(set(citations)):
        raise ValueError("Hint paragraph citations must not contain duplicates.")
    return citations


def _require_public_draft(repository: DraftRepository, draft_id: int) -> Draft:
    draft = repository.get_draft(draft_id)
    if draft is None or draft.source.backend != "libretexts_public":
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


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
