from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .adapt import AdaptClient, AdaptPublishingError
from .assistant import (
    AssistantService,
    AssistantStore,
    assistant_status,
    build_assistant_router,
    create_schema as create_assistant_schema,
)
from .catalog import SourceLicense, source_license
from .computation import AssessmentComputationBlueprint, ComputationProfile
from .computation_client import (
    AssessmentComputationClient,
    ComputationClientError,
)
from .computation_policy import (
    ComputationPolicyError,
    computation_runtime_is_qualified,
    configured_computation_runtime,
    require_computation_gate,
)
from .computation_workflow import (
    ComputationWorkflowError,
    blueprint_from_record,
    is_computational_draft,
    item_type_for_delivery,
    report_view,
    revalidate_draft_from_blueprint,
    revalidate_edited_draft,
    validate_requested_profile,
)
from .config import (
    COMPUTATION_PROXY_TOKEN_HEADER,
    Settings,
    get_settings,
)
from .content import (
    ContentAdapterError,
    PublicLibreTextsContentAdapter,
    build_content_adapter,
)
from .db import (
    ComputationAttestationWrite,
    ComputationEvidenceError,
    ConcurrentDraftUpdateError,
    Draft,
    DraftNotFoundError,
    DraftRepository,
    PublicationState,
    ReviewTransitionError,
    draft_content_sha256,
    inspect_hint_grounding,
    init_database,
)
from .http_guards import (
    require_same_origin as _require_same_origin,
    reviewer_identity as _reviewer,
)
from .jobs import GenerationWorker, validate_computation_profile_settings
from .llm import LLMError, build_llm_client, generation_status
from .media import HotspotMediaStore
from .math_text import SourceMathReferences, canonicalize_server_owned_preview
from .native_engine_runner import UnixSocketNativeEngineRunner
from .pipeline import AssessmentPipeline, PipelineError, ReviewService
from .publishing import (
    LicenseSelection,
    PublicationService,
    PublicationValidationError,
)
from .report_view import attach_native_seed_observations
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

# Choose types is the default mode, so one format must ship ticked or the
# first Generate click fails GenerateRequest.validate_generation_selection.
DEFAULT_ITEM_TYPE = AssessmentItemType.MULTIPLE_CHOICE.value


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def revision_label(edit_count: Any) -> str:
    """Render an internal ``edit_count`` as the user-facing revision number.

    ``edit_count`` is 0-based and is the number of times a draft has been
    edited, but reviewers were shown it raw, so a brand new draft read as
    "revision 0". Every user-facing revision number goes through here so the
    presentation stays v1/v2/v3 while the stored value keeps its own meaning.
    Never used for optimistic-concurrency values such as ``expected_edit_count``,
    which must stay the raw integer.
    """

    try:
        count = int(edit_count)
    except (TypeError, ValueError):
        return "v1"
    return f"v{max(count, 0) + 1}"


PUBLICATION_STATE_HEADLINES: dict[PublicationState, str] = {
    PublicationState.PENDING: "Publication pending",
    PublicationState.UNKNOWN: "Awaiting ADAPT reconciliation",
    PublicationState.ADAPT_CREATED: "ADAPT item created — QTI pending",
    PublicationState.HINTS_SYNCED: (
        "ADAPT item created, hint rungs synced — QTI pending"
    ),
    PublicationState.SUCCEEDED: "Published to ADAPT",
    PublicationState.FAILED: "Publication failed",
}


def publication_state_headline(state_value: Any) -> str:
    """Name what a publication record's state means, one state at a time.

    The history card used to choose with an ``{% else %}`` written for
    ``pending``, which silently absorbed every state the chain above it did not
    name. A mapping keyed by the enum is the point: an unnamed state is a
    missing key rather than a wrong headline, and
    ``test_every_publication_state_has_its_own_headline`` fails before the
    reviewer ever sees one.

    Membership is asked of ``PublicationState`` rather than of a set kept here,
    the same way ``Publisher._refuse_unreadable_publication_state`` asks it, so
    the refusal and this headline cannot disagree about what is readable. A
    state outside the enum was written by something else -- a successor build or
    a hand edit -- so it is named as unreadable and shown raw, because the
    refusal that sends an operator to this page names it too. ADR 0003.

    A recognised state with no entry is *unrecognised*, which is a different
    sentence from *unreadable*: this build declares the state and simply has no
    headline for it, so blaming a successor build would send an operator hunting
    a rollback that never happened. It does not raise, because a ``KeyError``
    inside a template render takes out the whole of ``GET /drafts/{id}`` -- one
    forgotten entry would make every draft that ever reached that state
    unviewable, which is worse than the mislabelled card this replaces and lands
    on the page ADR 0003 sends an operator to. The test is what forces the entry
    to exist; this is only what the page does if that guard is bypassed.
    """

    try:
        state = PublicationState(state_value)
    except ValueError:
        return f"Unreadable publication state: {state_value}"
    return PUBLICATION_STATE_HEADLINES.get(
        state, f"Unrecognised publication state: {state.value}"
    )


templates.env.globals["revision_label"] = revision_label
templates.env.globals["publication_state_headline"] = publication_state_headline
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
        runtime_binding = configured_computation_runtime(resolved_settings)
        computation_client = (
            AssessmentComputationClient(
                resolved_settings.computation_socket_path,
                expected_runtime_manifest_sha256=(
                    runtime_binding.runtime_manifest_sha256
                    if runtime_binding is not None
                    else None
                ),
            )
            if resolved_settings.computation_mode != "off"
            else None
        )
        native_engine_runner = (
            UnixSocketNativeEngineRunner(
                resolved_settings.computation_native_runner_socket_path,
                runner_id=resolved_settings.computation_native_runner_id,
            )
            if resolved_settings.computation_mode != "off"
            and resolved_settings.computation_native_runner_configured
            else None
        )
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.repository = repository
        app.state.review = ReviewService(repository, resolved_settings)
        app.state.adapt_client = adapt_client
        app.state.computation_client = computation_client
        app.state.native_engine_runner = native_engine_runner
        app.state.publisher = PublicationService(
            resolved_settings, repository, adapt_client
        )
        app.state.content_factory = lambda source_type: build_content_adapter(
            resolved_settings, source_type
        )
        app.state.llm_factory = lambda: build_llm_client(resolved_settings)
        if resolved_settings.assistant_enabled:
            # Schema and service exist only while the feature is on, so the
            # demo assistant adds no tables and no state to a normal deployment.
            create_assistant_schema(database.engine)
            app.state.assistant_service = AssistantService(
                resolved_settings,
                AssistantStore(database.session_factory),
                repository,
            )
        else:
            app.state.assistant_service = None
        worker = GenerationWorker(
            resolved_settings,
            repository,
            content_factory=app.state.content_factory,
            llm_factory=app.state.llm_factory,
            computation_client=computation_client,
            native_engine_runner=native_engine_runner,
        )
        app.state.generation_worker = worker
        worker.start()
        try:
            yield
        finally:
            await worker.stop()
            if computation_client is not None:
                await computation_client.aclose()
            if native_engine_runner is not None:
                await native_engine_runner.aclose()
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
    # Set outside the lifespan so templates can read it on any request, including
    # in tests that render without a started lifespan.
    app.state.assistant_enabled = resolved_settings.assistant_enabled
    app.state.assistant_max_message_chars = (
        resolved_settings.assistant_max_message_chars
    )
    if resolved_settings.assistant_enabled:
        app.include_router(build_assistant_router(resolved_settings))
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
                "sandbox_sources": "enabled"
                if resolved_settings.sandbox_sources_enabled
                else "disabled",
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
                "assistant": assistant_status(resolved_settings),
                **(
                    {
                        "assessment_computation": (
                            {
                                **resolved_settings.computation_health_config,
                                "runtime_qualification": (
                                    "promoted"
                                    if computation_runtime_is_qualified(
                                        resolved_settings
                                    )
                                    else "unqualified"
                                ),
                            }
                        )
                    }
                    if resolved_settings.computation_mode != "off"
                    else {}
                ),
            }
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        current_generation_status = generation_status(resolved_settings)
        ready = current_generation_status == "configured"
        computation_status = "disabled"
        if resolved_settings.computation_mode != "off":
            if (
                resolved_settings.computation_mode == "enforce"
                and not computation_runtime_is_qualified(resolved_settings)
            ):
                computation_status = "unqualified"
                ready = False
            else:
                computation_status = "unavailable"
                computation_client = getattr(app.state, "computation_client", None)
                if computation_client is not None:
                    try:
                        await computation_client.ready()
                        computation_status = "ready"
                    except ComputationClientError:
                        ready = False
        overall_status = (
            "ready"
            if ready
            else (
                f"computation_{computation_status}"
                if computation_status in {"unavailable", "unqualified"}
                else current_generation_status
            )
        )
        return JSONResponse(
            {
                "status": overall_status,
                **(
                    {"assessment_computation": computation_status}
                    if resolved_settings.computation_mode != "off"
                    else {}
                ),
            },
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
                "default_item_type": DEFAULT_ITEM_TYPE,
                "item_type_labels": {
                    item.value: _item_type_label(item.value)
                    for item in AssessmentItemType
                },
                "computation_mode": resolved_settings.computation_mode,
                "computation_families": resolved_settings.computation_families,
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
        computation_family: str | None = Form(None),
        computation_delivery: str | None = Form(None),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        _reviewer(request)
        try:
            locator = (source_locator or "").strip()
            selected_type = SourceType(source_type)
            # Honour the configured flag rather than rejecting unconditionally.
            # content.py already gates the sandbox adapter on
            # sandbox_sources_enabled, so hard-coding the refusal here left the
            # setting half-wired: enabling it changed the adapter layer while
            # this endpoint still refused. Default stays False, so the shipped
            # behaviour is unchanged.
            if not resolved_settings.sandbox_sources_enabled and (
                selected_type is not SourceType.PUBLIC or sandbox_path is not None
            ):
                raise ValueError("Dev sandbox sources are disabled for this service.")
            if not locator:
                raise ValueError("Choose a public LibreTexts page to generate from.")
            if generation_mode == "selected":
                if not item_types:
                    raise ValueError("Choose at least one item type.")
                if len(item_types) > 8:
                    raise ValueError("Choose no more than 8 item types.")
                if item_count < len(item_types):
                    raise ValueError(
                        "Set Total number of items to at least the number of "
                        "selected item types."
                    )
            family = (computation_family or "").strip()
            delivery = (computation_delivery or "").strip()
            if bool(family) != bool(delivery):
                raise ValueError(
                    "Select both a computation family and delivery format, or neither."
                )
            computation_profile = (
                ComputationProfile(family=family, delivery=delivery)
                if family and delivery
                else None
            )
            if computation_profile is not None:
                validate_computation_profile_settings(
                    resolved_settings,
                    computation_profile,
                )
            if resolved_settings.advanced_items_enabled:
                generation_request = GenerateRequest(
                    source_type=selected_type,
                    source_locator=locator,
                    generation_mode=generation_mode,
                    item_types=[AssessmentItemType(item) for item in item_types],
                    item_count=item_count,
                    include_hint_ladder=include_hint_ladder,
                    computation_profile=computation_profile,
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
                    computation_client=request.app.state.computation_client,
                    computation_mode=resolved_settings.computation_mode,
                    computation_families=resolved_settings.computation_families,
                    computation_container_digest=(
                        resolved_settings.computation_container_digest
                    ),
                    computation_image_reference=(
                        resolved_settings.computation_image_reference
                    ),
                    native_engine_runner=request.app.state.native_engine_runner,
                )
                outcome = await pipeline.generate(
                    locator,
                    computation_profile=computation_profile,
                )
        except ValidationError:
            return _redirect_with_message(
                "/",
                "error",
                "Check the generation selections and try again.",
            )
        except (
            ComputationClientError,
            ComputationWorkflowError,
            ContentAdapterError,
            LLMError,
            PipelineError,
            ValueError,
        ) as exc:
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

    async def render_draft_page(
        request: Request,
        draft_id: int,
        *,
        error: str | None = None,
        active_form: str | None = None,
        form_values: dict[str, Any] | None = None,
        form_errors: dict[str, str] | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        repository: DraftRepository = request.app.state.repository
        draft = _require_public_draft(repository, draft_id)
        computation = None
        if resolved_settings.computation_mode != "off":
            record = repository.get_current_computation_validation(draft.id)
            attestations = (
                repository.list_current_computation_attestations(
                    draft.id,
                    report_sha256=record.report_sha256,
                )
                if record is not None
                else ()
            )
            specialist_subject = _trusted_computation_specialist_subject(
                request,
                resolved_settings,
            )
            computation = report_view(
                record,
                legacy_computational=is_computational_draft(draft.current),
                attestations=attestations,
                specialist_allowed=(
                    record is not None
                    and record.is_current
                    and specialist_subject is not None
                ),
            )
            computation = attach_native_seed_observations(
                computation,
                record.engine_evidence_json if record is not None else {},
            )
        mapped_license = await _ensure_source_license(
            request,
            draft,
            resolved_settings,
        )
        publication_readiness = request.app.state.publisher.readiness(
            draft.id,
            license_resolved=mapped_license is not None,
        ).model_dump()
        current_publication = _current_successful_publication(draft)
        publication_can_submit = (
            current_publication is None
            and publication_readiness["alignment"] is not None
            and all(
                blocker["code"] == "license_unresolved"
                for blocker in publication_readiness["blockers"]
            )
        )
        draft_view = _draft_detail(draft, computation=computation)
        return templates.TemplateResponse(
            request,
            "draft.html",
            {
                "draft": draft_view,
                "notice": request.query_params.get("notice"),
                "error": error or request.query_params.get("error"),
                "active_form": active_form,
                "form_values": form_values or {},
                "form_errors": form_errors or {},
                "bloom_options": [item.value for item in BloomLevel],
                "difficulty_options": [item.value for item in Difficulty],
                "adapt_publishing_status": resolved_settings.adapt_publishing_status,
                "adapt_folder_name": resolved_settings.adapt_folder_name,
                "adapt_public": resolved_settings.adapt_public,
                "mapped_license": mapped_license,
                "publication_readiness": publication_readiness,
                "publication_can_submit": publication_can_submit,
                "manual_license_options": [
                    ("publicdomain", "Public domain"),
                    ("ccby", "CC BY"),
                    ("ccbync", "CC BY-NC"),
                    ("ccbyncsa", "CC BY-NC-SA"),
                    ("ccbysa", "CC BY-SA"),
                    ("arr", "All rights reserved"),
                ],
            },
            status_code=status_code,
        )

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    async def draft_detail(request: Request, draft_id: int) -> HTMLResponse:
        return await render_draft_page(request, draft_id)

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
                    context_type=current.context_type,
                    stem=str(stem),
                    stimulus=current.stimulus,
                    set_key=current.set_key,
                    choices=choices,
                    explanation=str(explanation),
                    bloom=BloomLevel(str(bloom)),
                    difficulty=Difficulty(str(difficulty)),
                    citation_paragraphs=current.citation_paragraphs,
                    needs_human_verification=current.needs_human_verification,
                    specialist_review_required=current.specialist_review_required,
                    targeted_misconception=current.targeted_misconception,
                )
            computation_validation = None
            expected_edit_count = None
            expected_draft_sha256 = None
            if resolved_settings.computation_mode != "off":
                expected_edit_count = stored.edit_count
                expected_draft_sha256 = draft_content_sha256(stored.current_json)
                computation_client = request.app.state.computation_client
                if computation_client is None:
                    raise ComputationWorkflowError(
                        "The isolated assessment computation service is unavailable."
                    )
                updated, computation_validation = await revalidate_edited_draft(
                    client=computation_client,
                    current_record=repository.get_current_computation_validation(
                        draft_id
                    ),
                    draft=updated,
                    container_digest=resolved_settings.computation_container_digest,
                    image_reference=resolved_settings.computation_image_reference,
                    native_engine_runner=request.app.state.native_engine_runner,
                )
            repository.edit_draft(
                draft_id,
                updated,
                editor=_reviewer(request),
                notes=reviewer_notes,
                computation_validation=computation_validation,
                expected_edit_count=expected_edit_count,
                expected_draft_sha256=expected_draft_sha256,
            )
        except (
            ComputationClientError,
            ComputationWorkflowError,
            DraftNotFoundError,
            ValidationError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
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
    ):
        _require_same_origin(request, resolved_settings)
        stored_draft = _require_public_draft(request.app.state.repository, draft_id)
        review_values = {
            "bloom_confirmed": bloom_confirmed,
            "difficulty_confirmed": difficulty_confirmed,
            "specialist_confirmed": specialist_confirmed,
            "reviewer_notes": reviewer_notes,
        }
        if decision == ReviewStatus.READY_TO_PUBLISH.value and (
            not bloom_confirmed or not difficulty_confirmed
        ):
            return await render_draft_page(
                request,
                draft_id,
                error=REVIEW_CONFIRMATION_ERROR,
                active_form="question_review",
                form_values=review_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        if (
            decision == ReviewStatus.READY_TO_PUBLISH.value
            and stored_draft.current.specialist_review_required
            and not specialist_confirmed
        ):
            return await render_draft_page(
                request,
                draft_id,
                error="A qualified specialist must confirm this item before approval.",
                active_form="question_review",
                form_values=review_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        computation_binding = None
        if decision == ReviewStatus.READY_TO_PUBLISH.value:
            try:
                computation_decision = require_computation_gate(
                    resolved_settings,
                    request.app.state.repository,
                    stored_draft,
                    action="approval",
                )
                if resolved_settings.computation_mode == "enforce":
                    computation_binding = computation_decision.atomic_binding(
                        resolved_settings
                    )
            except ComputationPolicyError as exc:
                return await render_draft_page(
                    request,
                    draft_id,
                    error=str(exc),
                    active_form="question_review",
                    form_values=review_values,
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
                computation_binding=computation_binding,
            )
        except ValidationError:
            return await render_draft_page(
                request,
                draft_id,
                error=REVIEW_VALIDATION_ERROR,
                active_form="question_review",
                form_values=review_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        except (
            DraftNotFoundError,
            ReviewTransitionError,
            ValueError,
        ) as exc:
            return await render_draft_page(
                request,
                draft_id,
                error=str(exc),
                active_form="question_review",
                form_values=review_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        label = (
            "Draft+approved%3B+not+yet+published"
            if decision == "ready_to_publish"
            else "Draft+rejected"
        )
        return RedirectResponse(
            f"/drafts/{draft_id}?notice={label}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/drafts/{draft_id}/computation/attest")
    async def attest_computation(
        request: Request,
        draft_id: int,
        rationale: str = Form(...),
    ) -> RedirectResponse:
        _require_same_origin(request, resolved_settings)
        repository: DraftRepository = request.app.state.repository
        _require_public_draft(repository, draft_id)
        try:
            if resolved_settings.computation_mode == "off":
                raise ValueError("Assessment computation is disabled.")
            subject = _trusted_computation_specialist_subject(
                request,
                resolved_settings,
            )
            if subject is None:
                raise ValueError(
                    "An authenticated, allowlisted computation-specialist "
                    "proxy subject is required."
                )
            normalized_rationale = rationale.strip()
            if not 20 <= len(normalized_rationale) <= 4_000:
                raise ValueError(
                    "The computation-specialist rationale must be 20–4,000 characters."
                )
            validation = repository.get_current_computation_validation(draft_id)
            if validation is None or validation.status not in {
                "partially_validated",
                "unsupported",
            }:
                raise ValueError(
                    "Only a current partially validated or unsupported report "
                    "can be attested."
                )
            repository.append_computation_attestation(
                draft_id,
                edit_count=validation.edit_count,
                report_sha256=validation.report_sha256,
                attestation=ComputationAttestationWrite(
                    specialist_identity=subject,
                    rationale=normalized_rationale,
                    qualification_json=json.dumps(
                        {
                            "policy": "assessment-computation-specialist-v0",
                            "trusted_proxy_subject": subject,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        except (DraftNotFoundError, ValueError) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        return _redirect_with_message(
            f"/drafts/{draft_id}",
            "notice",
            "Computation-specialist attestation recorded",
        )

    @app.post("/drafts/{draft_id}/computation/revalidate")
    async def revalidate_legacy_computation(
        request: Request,
        draft_id: int,
        blueprint_json: str = Form(...),
        expected_edit_count: int = Form(...),
    ) -> RedirectResponse:
        """Migrate one pre-v0 draft through the strict typed computation path."""

        _require_same_origin(request, resolved_settings)
        repository: DraftRepository = request.app.state.repository
        reviewer = _reviewer(request)
        try:
            if resolved_settings.computation_mode == "off":
                raise ValueError("Assessment computation is disabled.")
            stored = _require_public_draft(repository, draft_id)
            if not is_computational_draft(stored.current):
                raise ValueError(
                    "Only a legacy numerical, WeBWorK, or IMathAS draft can use "
                    "explicit computation revalidation."
                )
            current_record = repository.get_current_computation_validation(draft_id)
            if (
                current_record is not None
                and blueprint_from_record(current_record) is not None
            ):
                raise ValueError(
                    "This draft already has a typed computation blueprint. Edit the "
                    "draft to revalidate its current blueprint."
                )
            if expected_edit_count != stored.edit_count:
                raise ConcurrentDraftUpdateError(
                    "the draft changed before computation revalidation; reload it "
                    "before retrying"
                )
            if len(blueprint_json.encode("utf-8")) > 64 * 1024:
                raise ValueError(
                    "The typed computation blueprint exceeds the 64 KiB limit."
                )
            blueprint_payload = _strict_json_object(blueprint_json)
            blueprint = AssessmentComputationBlueprint.model_validate(blueprint_payload)
            if (
                blueprint.source_concept_label is not None
                and blueprint.source_concept_label.strip().casefold()
                != stored.current.concept_label.strip().casefold()
            ):
                raise ValueError(
                    "The typed blueprint source concept must match the current draft."
                )
            blueprint = blueprint.model_copy(
                update={"source_concept_label": stored.current.concept_label},
                deep=True,
            )
            validate_requested_profile(
                blueprint.profile,
                mode=resolved_settings.computation_mode,
                allowed_families=resolved_settings.computation_families,
            )
            validate_computation_profile_settings(
                resolved_settings,
                blueprint.profile,
            )
            if item_type_for_delivery(blueprint.profile.delivery) != (
                stored.current.item_type
            ):
                raise ValueError(
                    "The typed blueprint delivery must match the current draft type."
                )
            computation_client = request.app.state.computation_client
            if computation_client is None:
                raise ComputationWorkflowError(
                    "The isolated assessment computation service is unavailable."
                )
            expected_draft_hash = draft_content_sha256(stored.current_json)
            rebound, computation_validation = await revalidate_draft_from_blueprint(
                client=computation_client,
                blueprint=blueprint,
                draft=stored.current,
                container_digest=(resolved_settings.computation_container_digest),
                image_reference=resolved_settings.computation_image_reference,
                native_engine_runner=request.app.state.native_engine_runner,
            )
            repository.edit_draft(
                draft_id,
                rebound,
                editor=reviewer,
                notes="Explicit typed computation revalidation.",
                computation_validation=computation_validation,
                expected_edit_count=expected_edit_count,
                expected_draft_sha256=expected_draft_hash,
            )
            notice = (
                "Draft rebound to the typed computation blueprint; review checks "
                "were reset"
            )
        except (
            ComputationClientError,
            ComputationEvidenceError,
            ComputationWorkflowError,
            DraftNotFoundError,
            ReviewTransitionError,
            ValidationError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return _redirect_with_message(f"/drafts/{draft_id}", "error", str(exc))
        return _redirect_with_message(
            f"/drafts/{draft_id}",
            "notice",
            notice,
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
    ):
        _require_same_origin(request, resolved_settings)
        draft = _require_public_draft(request.app.state.repository, draft_id)
        hint_values = {
            "conceptual_text": conceptual_text,
            "conceptual_citations": conceptual_citations,
            "strategic_text": strategic_text,
            "strategic_citations": strategic_citations,
            "specific_text": specific_text,
            "specific_citations": specific_citations,
            "reviewer_notes": reviewer_notes,
        }
        citation_values: dict[str, list[int]] = {}
        citation_errors: dict[str, str] = {}
        for rung, raw_value in (
            ("conceptual", conceptual_citations),
            ("strategic", strategic_citations),
            ("specific", specific_citations),
        ):
            try:
                citation_values[rung] = _parse_citations(raw_value)
            except ValueError as exc:
                citation_errors[rung] = str(exc)
        if citation_errors:
            return await render_draft_page(
                request,
                draft_id,
                error="Hint edits were not saved. Correct the cited paragraphs below.",
                active_form="hint_edit",
                form_values=hint_values,
                form_errors=citation_errors,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        try:
            ladder = HintLadderDraft(
                concept_label=draft.current.concept_label,
                rungs=[
                    HintRungDraft(
                        rung=HintRungType.CONCEPTUAL,
                        text=conceptual_text,
                        citation_paragraphs=citation_values["conceptual"],
                    ),
                    HintRungDraft(
                        rung=HintRungType.STRATEGIC,
                        text=strategic_text,
                        citation_paragraphs=citation_values["strategic"],
                    ),
                    HintRungDraft(
                        rung=HintRungType.SPECIFIC,
                        text=specific_text,
                        citation_paragraphs=citation_values["specific"],
                    ),
                ],
            )
            grounding = inspect_hint_grounding(draft.current, ladder)
            if grounding:
                return await render_draft_page(
                    request,
                    draft_id,
                    error="Hint edits were not saved. Use only the question source paragraphs shown on this page.",
                    active_form="hint_edit",
                    form_values=hint_values,
                    form_errors={
                        issue.rung or "general": issue.message for issue in grounding
                    },
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                )
            request.app.state.repository.save_hint_ladder(
                draft_id,
                ladder,
                editor=_reviewer(request),
                notes=reviewer_notes,
            )
        # DraftNotFoundError subclasses LookupError, not ValueError, so it has
        # to be named explicitly or a draft deleted mid-request 500s instead of
        # re-rendering the form. Matches the sibling hint-review handler.
        except (DraftNotFoundError, ValidationError, ValueError) as exc:
            return await render_draft_page(
                request,
                draft_id,
                error=str(exc),
                active_form="hint_edit",
                form_values=hint_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
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
    ):
        _require_same_origin(request, resolved_settings)
        _require_public_draft(request.app.state.repository, draft_id)
        review_values = {
            "conceptual_confirmed": conceptual_confirmed,
            "strategic_confirmed": strategic_confirmed,
            "specific_confirmed": specific_confirmed,
            "reviewer_notes": reviewer_notes,
        }
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
            return await render_draft_page(
                request,
                draft_id,
                error=str(exc),
                active_form="hint_review",
                form_values=review_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
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
    ):
        _require_same_origin(request, resolved_settings)
        reviewer = _reviewer(request)
        stored_draft = _require_public_draft(
            request.app.state.repository,
            draft_id,
        )
        await _ensure_source_license(request, stored_draft, resolved_settings)
        selected_license = None
        if license_code:
            selected_license = LicenseSelection(
                code=license_code.strip(),
                version=(license_version or "").strip() or None,
                label=(license_label or "").strip(),
                evidence_url=(license_evidence_url or "").strip(),
            )
        publish_values = {
            "topic_stable_id": topic_stable_id,
            "alignment_confirmed": alignment_confirmed,
            "license_code": license_code or "",
            "license_version": license_version or "",
            "license_label": license_label or "",
            "license_evidence_url": license_evidence_url or "",
            "license_confirmed": license_confirmed,
        }
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
            return await render_draft_page(
                request,
                draft_id,
                error=str(exc),
                active_form="publish",
                form_values=publish_values,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        if publication.state == PublicationState.SUCCEEDED.value:
            return _redirect_with_message(
                f"/drafts/{draft_id}", "notice", "Published to ADAPT"
            )
        return await render_draft_page(
            request,
            draft_id,
            error=publication.error_message
            or "Publishing did not complete. Review the publication status below.",
            active_form="publish",
            form_values=publish_values,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
    references = SourceMathReferences.from_html(draft.source.html_body)
    current_publication = _current_successful_publication(draft)
    latest_publication = _latest_successful_publication(draft)
    display_status = (
        "published" if current_publication is not None else draft.status.value
    )
    return {
        "id": draft.id,
        "status": display_status,
        "status_label": (
            "Published to ADAPT"
            if current_publication is not None
            else _status_label(draft.status.value)
        ),
        "source_title": draft.source.title,
        "source_type": _source_type(draft.source.backend),
        "stem": references.present(current.stem),
        "item_type": current.item_type.value,
        "context_type": current.context_type.value,
        "bloom": current.bloom.value,
        "difficulty": current.difficulty.value,
        "edit_count": draft.edit_count,
        "current_publication": _publication_display(current_publication),
        "latest_publication": _publication_display(latest_publication),
    }


def _draft_detail(
    draft: Draft,
    *,
    computation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = draft.current
    references = SourceMathReferences.from_html(draft.source.html_body)
    display = _question_display(current, references)
    paragraphs_by_index = {
        int(paragraph["index"]): {
            **paragraph,
            "display_text": references.present(str(paragraph["text"])),
        }
        for paragraph in draft.source.paragraphs_json
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
    current_publication = _current_successful_publication(draft)
    latest_publication = _latest_successful_publication(draft)
    hint_record = draft.current_hint_ladder
    hint_ladder = hint_record.ladder if hint_record is not None else None
    hint_grounding = (
        inspect_hint_grounding(current, hint_ladder) if hint_ladder is not None else ()
    )
    hint_grounding_by_rung = {
        issue.rung: issue.message for issue in hint_grounding if issue.rung
    }
    # Ladder-wide issues (a concept mismatch) carry no rung, so the per-rung map
    # above drops them. Surface them separately or the reviewer sees a blocked
    # publication with nothing on the page explaining why.
    hint_grounding_general = next(
        (issue.message for issue in hint_grounding if issue.rung is None),
        None,
    )
    engine_validation = draft.current_engine_validation
    return {
        "id": draft.id,
        "status": draft.status.value,
        "display_status": (
            "published" if current_publication is not None else draft.status.value
        ),
        "status_label": (
            "Published to ADAPT"
            if current_publication is not None
            else _status_label(draft.status.value)
        ),
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
        "display": display,
        "item_type": current.item_type.value,
        "item_type_label": _item_type_label(current.item_type.value),
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
        "critique_issues": [
            references.present(str(issue)) for issue in critique.get("issues", [])
        ],
        "model_id": revision_call.model_id if revision_call else "unknown model",
        "prompt_version": revision_call.prompt_version
        if revision_call
        else draft.tool_version,
        "bloom_confirmed": draft.bloom_confirmed,
        "difficulty_confirmed": draft.difficulty_confirmed,
        "reviewer_notes": draft.reviewer_notes,
        "last_reviewed_by": draft.last_reviewed_by,
        "last_reviewed_at": draft.last_reviewed_at,
        "edit_count": draft.edit_count,
        "specialist_review_required": current.specialist_review_required,
        "computation": computation,
        "engine_validation": {
            "engine": engine_validation.engine,
            "compiler_version": engine_validation.compiler_version,
            "source_sha256": engine_validation.source_sha256,
            "seed_count": engine_validation.seed_count,
            "status": engine_validation.status,
            "previews": [
                {
                    **preview,
                    "display_prompt": references.present(
                        canonicalize_server_owned_preview(str(preview["prompt"]))
                    ),
                    "display_answer": references.present(
                        canonicalize_server_owned_preview(str(preview["answer"]))
                    ),
                    "display_explanation": references.present(
                        canonicalize_server_owned_preview(str(preview["explanation"]))
                    ),
                }
                for preview in engine_validation.previews_json[:5]
            ],
        }
        if engine_validation is not None
        else None,
        "hint_ladder": {
            "id": hint_record.id,
            "status": hint_record.status,
            "confirmations": hint_record.confirmations_json,
            "reviewer_notes": hint_record.reviewer_notes,
            "reviewed_by": hint_record.reviewed_by,
            "reviewed_at": hint_record.reviewed_at,
            "needs_repair": bool(hint_grounding)
            or any(rung.answer_leak_detected for rung in hint_ladder.rungs),
            "grounding_error": hint_grounding_general,
            "allowed_citations_text": ", ".join(
                str(item) for item in current.citation_paragraphs
            ),
            "rungs": [
                {
                    "rung": rung.rung.value,
                    "text": rung.text,
                    "display_text": references.present(rung.text),
                    "citation_paragraphs": rung.citation_paragraphs,
                    "citations_text": ", ".join(
                        str(item) for item in rung.citation_paragraphs
                    ),
                    "answer_leak_detected": rung.answer_leak_detected,
                    "grounding_error": hint_grounding_by_rung.get(rung.rung.value),
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
        "current_publication": _publication_display(current_publication),
        "latest_publication": _publication_display(latest_publication),
    }


def _question_display(
    question: QuestionDraft,
    references: SourceMathReferences,
) -> dict[str, Any]:
    display = question.model_dump(mode="json")
    for field in ("stem", "stimulus", "explanation", "targeted_misconception"):
        value = display.get(field)
        if isinstance(value, str):
            display[field] = references.present(value)
    for choice in display["choices"]:
        choice["text"] = references.present(choice["text"])
        if choice.get("feedback"):
            choice["feedback"] = references.present(choice["feedback"])

    response = display["response"]
    for pair in response["matching_pairs"]:
        pair["prompt"] = references.present(pair["prompt"])
        pair["target"] = references.present(pair["target"])
    if response.get("image_alt"):
        response["image_alt"] = references.present(response["image_alt"])
    for region in response["hotspot_regions"]:
        region["label"] = references.present(region["label"])
    for segment in response["highlight_segments"]:
        segment["text"] = references.present(segment["text"])
    for column in response["matrix_columns"]:
        column["text"] = references.present(column["text"])
        if column.get("feedback"):
            column["feedback"] = references.present(column["feedback"])
    for row in response["matrix_rows"]:
        row["text"] = references.present(row["text"])
    for field in (
        "bow_tie_actions",
        "bow_tie_condition",
        "bow_tie_parameters",
    ):
        group = response.get(field)
        if group:
            for choice in group["choices"]:
                choice["text"] = references.present(choice["text"])
                if choice.get("feedback"):
                    choice["feedback"] = references.present(choice["feedback"])
    return display


def _status_label(status_value: str) -> str:
    if status_value == ReviewStatus.READY_TO_PUBLISH.value:
        return "Approved — not yet published"
    return status_value.replace("_", " ")


def _current_successful_publication(draft: Draft):
    return max(
        (
            publication
            for publication in draft.publications
            if publication.edit_count == draft.edit_count
            and publication.state == PublicationState.SUCCEEDED.value
        ),
        key=lambda publication: publication.id,
        default=None,
    )


def _latest_successful_publication(draft: Draft):
    return max(
        (
            publication
            for publication in draft.publications
            if publication.state == PublicationState.SUCCEEDED.value
        ),
        key=lambda publication: (publication.edit_count, publication.id),
        default=None,
    )


def _publication_display(publication):
    if publication is None:
        return None
    return {
        "id": publication.id,
        "edit_count": publication.edit_count,
        "adapt_question_id": publication.adapt_question_id,
        "adapt_page_id": publication.adapt_page_id,
        "framework_title": publication.framework_title,
        "topic": publication.alignment_json.get("topic", {}).get("text"),
        "license_label": publication.license_label,
        "finalized_at": publication.finalized_at,
    }


def _item_type_label(item_type: str) -> str:
    labels = {
        AssessmentItemType.SELECT_ALL.value: "Select All That Apply",
        AssessmentItemType.SELECT_N.value: "Select Exactly N",
    }
    return labels.get(item_type, item_type.replace("_", " ").title())


def _source_type(backend: str) -> str:
    return "public" if backend == "libretexts_public" else "sandbox"


def _parse_citations(value: str) -> list[int]:
    try:
        citations = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError:
        raise ValueError(
            "Hint citations must be comma-separated paragraph numbers."
        ) from None
    if not citations or any(item < 0 for item in citations):
        raise ValueError("Each hint requires at least one valid paragraph citation.")
    if len(citations) != len(set(citations)):
        raise ValueError("Hint paragraph citations must not contain duplicates.")
    return citations


def _strict_json_object(value: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Typed blueprint JSON contains a duplicate key.")
            result[key] = item
        return result

    def reject_nonfinite_constant(_value: str) -> None:
        raise ValueError("Typed blueprint JSON contains a non-finite number.")

    parsed = json.loads(
        value,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("Typed blueprint JSON must contain one object.")
    return parsed


def _require_public_draft(repository: DraftRepository, draft_id: int) -> Draft:
    draft = repository.get_draft(draft_id)
    if draft is None or draft.source.backend != "libretexts_public":
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


async def _ensure_source_license(
    request: Request,
    draft: Draft,
    settings: Settings,
) -> SourceLicense | None:
    mapped = source_license(
        draft.source.canonical_url,
        draft.source.license_metadata,
    )
    if mapped is not None or draft.source.backend != "libretexts_public":
        return mapped

    try:
        async with PublicLibreTextsContentAdapter(settings) as content:
            metadata = await content.fetch_license(draft.source.canonical_url)
    except ContentAdapterError:
        return None
    if metadata is None:
        return None

    request.app.state.repository.update_source_license(
        draft.source.id,
        metadata,
    )
    return source_license(draft.source.canonical_url, metadata)


def _trusted_computation_specialist_subject(
    request: Request,
    settings: Settings,
) -> str | None:
    """Return a proxy-authenticated allowlisted subject, or fail closed.

    The proxy token is checked before the configurable subject header is read.
    The general ``X-Reviewer`` identity remains intentionally separate.
    """

    if not settings.computation_specialist_proxy_ready:
        return None
    configured_secret = settings.computation_trusted_proxy_token
    if configured_secret is None:
        return None
    expected = configured_secret.get_secret_value().encode("ascii")
    presented = request.headers.get(COMPUTATION_PROXY_TOKEN_HEADER, "").encode(
        "utf-8",
        errors="surrogatepass",
    )
    expected_digest = hashlib.sha256(expected).digest()
    presented_digest = hashlib.sha256(presented).digest()
    if not hmac.compare_digest(presented_digest, expected_digest):
        return None

    subject = request.headers.get(
        settings.computation_specialist_subject_header,
        "",
    ).strip()
    if (
        not subject
        or len(subject) > 255
        or any(character.isspace() or ord(character) < 32 for character in subject)
    ):
        return None
    if subject not in settings.computation_specialist_subjects:
        return None
    return subject


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
