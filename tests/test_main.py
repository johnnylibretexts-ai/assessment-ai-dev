import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from app import main as main_module
from app.config import Settings
from app.content import PublicLibreTextsContentAdapter
from app.db import (
    Draft,
    DraftRepository,
    DraftWrite,
    HintGroundingIssue,
    PublicationState,
)
from app.main import create_app
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    ItemResponse,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
    SourceLicenseMetadata,
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        allowed_origin="http://testserver",
        server_key="key",
        server_secret="secret",
        server_user="user",
        ollama_api_key=None,
    )


def page() -> NormalizedPage:
    text = "Energy changes form but is conserved."
    return NormalizedPage(
        title="Energy",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url="https://chem.libretexts.org/Bookshelves/Test/Page",
            path="chem.libretexts.org/Bookshelves/Test/Page",
            page_id="86187",
        ),
    )


def question(stem: str = "Which statement about energy is accurate?") -> QuestionDraft:
    return QuestionDraft(
        concept_label="Energy conservation",
        stem=stem,
        choices=[
            Choice(id="A", text="It is conserved.", correct=True),
            Choice(id="B", text="It disappears.", correct=False),
            Choice(id="C", text="It is matter.", correct=False),
            Choice(id="D", text="It has no units.", correct=False),
        ],
        explanation="The source says energy changes form while remaining conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def seed(
    repository: DraftRepository,
    source_page: NormalizedPage | None = None,
    *,
    specialist: bool = False,
) -> int:
    """One reviewable draft. `specialist` arms the specialist-review guard."""

    revised = question()
    if specialist:
        revised = revised.model_copy(update={"specialist_review_required": True})
    stored = repository.replace_generated_drafts(
        page=source_page or page(),
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Energy conservation",
                    description="Energy changes form without disappearing.",
                    source_paragraphs=[0],
                ),
                raw=question("Initial question about energy?"),
                critique=Critique(
                    issues=["The initial stem was vague."],
                    revision_required=True,
                ),
                revised=revised,
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


SPECIALIST_REFUSAL = "Confirm you have seen the specialist-review flag before approving."
CONFIRMATION_REFUSAL = (
    "Confirm both the Bloom level and difficulty before approving this draft."
)


def approve(client: TestClient, draft_id: int, **fields: str) -> Response:
    """One approval submission, carrying only the fields a test cares about."""

    return client.post(
        f"/drafts/{draft_id}/review",
        data={"decision": "ready_to_publish", **fields},
        headers={
            "X-Reviewer": "reviewer@example.org",
            "Origin": "http://testserver",
        },
        follow_redirects=False,
    )


def mark_published(
    repository: DraftRepository,
    draft_id: int,
    *,
    adapt_question_id: int = 125,
) -> None:
    draft = repository.require_draft(draft_id)
    publication, created = repository.create_or_get_publication(
        {
            "draft_id": draft.id,
            "edit_count": draft.edit_count,
            "question_snapshot_json": draft.current_json,
            "source_snapshot_json": {"source_id": draft.source_snapshot_id},
            "reviewer_identity": "reviewer@example.org",
            "approved_at": datetime.now(UTC),
            "destination_folder_id": 42,
            "destination_folder_name": "Assessment AI — Approved",
            "author": "LibreTexts",
            "public": True,
            "license": "CC BY",
            "license_version": "4.0",
            "license_label": "CC BY 4.0",
            "license_evidence_url": "https://example.invalid/license",
            "framework_id": 7,
            "framework_title": "Test framework",
            "alignment_json": {
                "topic": {
                    "text": "Energy conservation",
                    "stable_id": "energy",
                }
            },
            "stable_topic_ids_json": ["energy"],
            "hint_ladder_snapshot_json": None,
            "publication_key": f"{draft.id:064x}",
            "payload_hash": f"{adapt_question_id:064x}",
            "payload_mapper_version": "test-1",
            "qti_exporter_version": "test-1",
        }
    )
    assert created is True
    repository.update_publication(
        publication.id,
        state=PublicationState.SUCCEEDED,
        adapt_question_id=adapt_question_id,
        adapt_page_id=adapt_question_id,
        finalized_at=datetime.now(UTC),
    )


def public_page() -> NormalizedPage:
    return page()


def sandbox_page() -> NormalizedPage:
    source_page = page()
    source_page.source = SourceInfo(
        canonical_url="https://dev.libretexts.org/Sandboxes/johnnyphung/Energy",
        path="Sandboxes/johnnyphung/Energy",
        page_id="7",
    )
    return source_page


def test_health_and_empty_queue_work_without_cloud_key(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "generation": "needs_ollama_api_key",
            "public_sources": "disabled",
            "sandbox_sources": "disabled",
            "adapt_publishing": "disabled",
            "advanced_items": "disabled",
            "parameterized_items": "disabled",
            "hint_generation": "disabled",
            "webwork": "disabled",
            "imathas": "disabled",
            "assistant": "disabled",
        }
        readiness = client.get("/readyz")
        assert readiness.status_code == 503
        assert readiness.json() == {"status": "needs_ollama_api_key"}
        queue = client.get("/")
        assert queue.status_code == 200
        assert "No drafts yet" in queue.text


def test_blank_cloud_key_is_not_reported_ready(tmp_path: Path) -> None:
    blank = settings(tmp_path).model_copy(update={"ollama_api_key": SecretStr("")})
    with TestClient(create_app(blank)) as client:
        assert client.get("/healthz").json()["generation"] == "needs_ollama_api_key"
        assert client.get("/readyz").status_code == 503


def test_gemini_can_make_generation_ready_without_ollama_key(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path).model_copy(
        update={
            "llm_provider_order": "ollama,gemini",
            "gemini_api_key": SecretStr("gemini-key"),
        }
    )
    with TestClient(create_app(configured)) as client:
        assert client.get("/healthz").json()["generation"] == "configured"
        assert client.get("/readyz").status_code == 200
        assert client.get("/readyz").json() == {"status": "ready"}


def test_enforce_readiness_fails_closed_without_promoted_runtime(
    tmp_path: Path,
) -> None:
    configured = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'unqualified.db'}",
        allowed_origin="http://testserver",
        llm_provider_order="gemini",
        gemini_api_key=SecretStr("gemini-key"),
        computation_mode="enforce",
        computation_family_allowlist="numeric",
        computation_image_reference=(
            f"registry.example/assessment-computation@sha256:{'a' * 64}"
        ),
    )

    with TestClient(create_app(configured)) as client:
        health = client.get("/healthz").json()
        assert health["assessment_computation"]["runtime_qualification"] == (
            "unqualified"
        )
        readiness = client.get("/readyz")
        assert readiness.status_code == 503
        assert readiness.json() == {
            "status": "computation_unqualified",
            "assessment_computation": "unqualified",
        }


def test_generation_form_rejects_cross_origin_before_provider_calls(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/generate",
            data={"sandbox_path": "Sandboxes/johnnyphung/Energy"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        missing_origin = client.post(
            "/generate",
            data={"sandbox_path": "Sandboxes/johnnyphung/Energy"},
            headers={"X-Reviewer": "reviewer@example.org"},
        )
        assert missing_origin.status_code == 403
        missing_identity = client.post(
            "/generate",
            data={"sandbox_path": "Sandboxes/johnnyphung/Energy"},
            headers={"Origin": "http://testserver"},
        )
        assert missing_identity.status_code == 403


def test_public_only_form_and_public_feature_disabled_behavior(tmp_path: Path) -> None:
    disabled_app = create_app(settings(tmp_path))
    with TestClient(disabled_app) as client:
        form = client.get("/")
        assert 'type="hidden" name="source_type" value="public"' in form.text
        assert 'name="source_locator"' in form.text
        assert 'id="generate-submit"' in form.text
        assert 'id="generation-status"' in form.text
        assert 'role="status"' in form.text
        assert 'aria-live="polite"' in form.text
        assert "This can take about a minute. Keep this page open." in form.text
        assert 'src="/static/generation.js"' in form.text
        assert "sandbox" not in form.text.casefold()
        assert "dev.libretexts.org" not in form.text
        generation_script = client.get("/static/generation.js")
        assert generation_script.status_code == 200
        assert "Finding the clearest teachable concepts" in generation_script.text
        assert 'form.dataset.submitting === "true"' in generation_script.text
        assert "sourceInput.readOnly = true" in generation_script.text
        response = client.post(
            "/generate",
            data={
                "source_type": "public",
                "source_locator": "https://chem.libretexts.org/Bookshelves/Test/Page",
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "not+enabled" in response.headers["location"]

    enabled_settings = settings(tmp_path).model_copy(
        update={
            "public_sources_enabled": True,
            "advanced_items_enabled": True,
        }
    )
    with TestClient(create_app(enabled_settings)) as client:
        form = client.get("/")
        assert "Select All That Apply" in form.text
        assert "not a control that checks every format" in form.text
        assert "Select Exactly N" in form.text
        assert 'type="hidden" name="source_type" value="public"' in form.text
        assert "sandbox" not in form.text.casefold()


def test_invalid_choose_types_requests_return_friendly_errors(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path).model_copy(
        update={
            "advanced_items_enabled": True,
            "public_sources_enabled": True,
        }
    )
    with TestClient(create_app(configured)) as client:
        too_many = client.post(
            "/generate",
            data={
                "source_type": "public",
                "source_locator": ("https://chem.libretexts.org/Bookshelves/Test/Page"),
                "generation_mode": "selected",
                "item_count": "8",
                "item_types": [
                    "multiple_choice",
                    "true_false",
                    "numerical",
                    "multiple_response",
                    "select_all",
                    "select_n",
                    "fill_in_blank",
                    "matching",
                    "ordering",
                ],
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
        assert too_many.status_code == 303
        assert "Choose+no+more+than+8+item+types" in too_many.headers["location"]
        assert "pydantic" not in too_many.headers["location"]

        total_too_small = client.post(
            "/generate",
            data={
                "source_type": "public",
                "source_locator": ("https://chem.libretexts.org/Bookshelves/Test/Page"),
                "generation_mode": "selected",
                "item_count": "1",
                "item_types": [
                    "multiple_choice",
                    "true_false",
                    "numerical",
                    "ordering",
                ],
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
        assert total_too_small.status_code == 303
        assert "Set+Total+number+of+items" in total_too_small.headers["location"]
        assert "pydantic" not in total_too_small.headers["location"]


def test_sandbox_and_legacy_requests_are_blocked_before_adapter_creation(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path))
    adapter_calls = 0

    def fail_if_called(_source_type: object) -> None:
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("sandbox request reached the adapter factory")

    with TestClient(app) as client:
        app.state.content_factory = fail_if_called
        legacy = client.post(
            "/generate",
            data={"sandbox_path": "Sandboxes/johnnyphung/Energy"},
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
        explicit = client.post(
            "/generate",
            data={
                "source_type": "sandbox",
                "source_locator": "Sandboxes/johnnyphung/Energy",
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
    assert legacy.status_code == 303
    assert explicit.status_code == 303
    assert "sandbox+sources+are+disabled" in legacy.headers["location"].casefold()
    assert "sandbox+sources+are+disabled" in explicit.headers["location"].casefold()
    assert adapter_calls == 0


def test_stored_sandbox_drafts_are_hidden_and_inaccessible(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        public_id = seed(app.state.repository, public_page())
        sandbox_id = seed(app.state.repository, sandbox_page())

        queue = client.get("/")
        assert queue.status_code == 200
        assert f'href="/drafts/{public_id}"' in queue.text
        assert f'href="/drafts/{sandbox_id}"' not in queue.text
        assert "dev.libretexts.org" not in queue.text
        assert client.get(f"/drafts/{sandbox_id}").status_code == 404

        review = client.post(
            f"/drafts/{sandbox_id}/review",
            data={"decision": "rejected"},
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
        )
        assert review.status_code == 404

        edit = client.post(
            f"/drafts/{sandbox_id}/edit",
            data={
                "stem": "What happens to total energy?",
                "choice_a": "It remains conserved.",
                "choice_b": "It vanishes.",
                "choice_c": "It becomes matter.",
                "choice_d": "It loses all units.",
                "correct_choice": "A",
                "explanation": "The source states that energy is conserved.",
                "bloom": "understand",
                "difficulty": "easy",
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
        )
        assert edit.status_code == 404


def test_revision_label_presents_edit_count_as_a_one_based_version() -> None:
    # edit_count is 0-based, so showing it raw made an unedited draft read as
    # "revision 0". Presentation is 1-based v-numbers everywhere.
    assert main_module.revision_label(0) == "v1"
    assert main_module.revision_label(1) == "v2"
    assert main_module.revision_label(2) == "v3"
    # Never render a nonsensical revision for missing or malformed values.
    assert main_module.revision_label(None) == "v1"
    assert main_module.revision_label("not a number") == "v1"
    assert main_module.revision_label(-1) == "v1"


def test_queue_shows_a_one_based_revision_number(tmp_path: Path) -> None:
    # The queue always renders a revision number; the detail page only does so
    # once there is a publication or an approval form, so assert against the
    # queue for the unedited case.
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        seed(app.state.repository, public_page())
        queue = client.get("/")
    assert queue.status_code == 200
    assert "Current revision v1" in queue.text
    assert "Current revision 0" not in queue.text


def test_concurrency_token_stays_a_raw_edit_count() -> None:
    # expected_edit_count is posted back for optimistic concurrency. Rendering
    # it through revision_label would submit "v1" instead of 0 and silently
    # break conflict detection, so pin the raw binding.
    template = (
        Path(main_module.__file__).resolve().parent / "templates" / "draft.html"
    ).read_text()
    assert 'value="{{ draft.edit_count }}"' in template
    assert 'value="{{ revision_label(draft.edit_count) }}"' not in template


def test_public_source_provenance_is_clickable_on_review_page(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository, public_page())
        detail = client.get(f"/drafts/{draft_id}")
    assert detail.status_code == 200
    assert "public source" in detail.text
    assert "chem.libretexts.org" in detail.text
    assert "Page ID: 86187" in detail.text
    assert 'target="_blank" rel="noopener noreferrer"' in detail.text
    assert 'class="source-link"' in detail.text
    assert ">Open original LibreTexts page</a>" in detail.text
    assert 'title="https://chem.libretexts.org/Bookshelves/Test/Page"' in detail.text
    assert "Paragraph 0" in detail.text


def test_verified_page_license_is_stored_and_rendered_without_manual_fields(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path))
    source_page = public_page()
    source_page.source.license = SourceLicenseMetadata(
        code="ccbyncsa",
        version="4.0",
        label="CC BY-NC-SA 4.0",
        evidence_url=source_page.source.canonical_url,
    )

    with TestClient(app) as client:
        draft_id = seed(app.state.repository, source_page)
        approved = client.post(
            f"/drafts/{draft_id}/review",
            data={
                "decision": "ready_to_publish",
                "bloom_confirmed": "true",
                "difficulty_confirmed": "true",
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
        assert approved.status_code == 303
        detail = client.get(f"/drafts/{draft_id}")

    stored = app.state.repository.require_draft(draft_id)
    assert stored.source.license_metadata == source_page.source.license
    assert "Source license: CC BY-NC-SA 4.0" in detail.text
    assert "filled automatically" in detail.text
    assert 'name="license_code"' not in detail.text
    assert "outside the currently curated framework" in detail.text


def test_existing_draft_license_is_backfilled_from_page_tags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = settings(tmp_path).model_copy(update={"public_sources_enabled": True})
    app = create_app(config)

    async def fake_fetch_license(
        _adapter: PublicLibreTextsContentAdapter,
        source_url: str,
    ) -> SourceLicenseMetadata:
        return SourceLicenseMetadata(
            code="ccbyncsa",
            version="4.0",
            label="CC BY-NC-SA 4.0",
            evidence_url=source_url,
        )

    monkeypatch.setattr(
        PublicLibreTextsContentAdapter,
        "fetch_license",
        fake_fetch_license,
    )

    with TestClient(app) as client:
        draft_id = seed(app.state.repository, public_page())
        detail = client.get(f"/drafts/{draft_id}")

    stored = app.state.repository.require_draft(draft_id)
    assert detail.status_code == 200
    assert stored.source.license_metadata is not None
    assert stored.source.license_metadata.label == "CC BY-NC-SA 4.0"


def test_edit_and_independent_review_gates(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        detail = client.get(f"/drafts/{draft_id}")
        assert detail.status_code == 200
        assert "Which statement about energy is accurate?" in detail.text
        assert "standard context" in detail.text
        assert "The initial stem was vague." in detail.text

        edited = client.post(
            f"/drafts/{draft_id}/edit",
            data={
                "stem": "What happens to total energy?",
                "choice_a": "It remains conserved.",
                "choice_b": "It vanishes.",
                "choice_c": "It becomes matter.",
                "choice_d": "It loses all units.",
                "correct_choice": "A",
                "explanation": "The source explicitly states that energy is conserved.",
                "bloom": "understand",
                "difficulty": "easy",
                "reviewer_notes": "Tightened the stem.",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )
        assert edited.status_code == 303
        stored = app.state.repository.require_draft(draft_id)
        assert stored.current.stem == "What happens to total energy?"
        assert stored.bloom_confirmed is False
        assert stored.difficulty_confirmed is False

        blocked = approve(client, draft_id, reviewer_notes="Looks good.")
        assert blocked.status_code == 422
        blocked_page = blocked
        assert CONFIRMATION_REFUSAL in blocked_page.text
        assert "Looks good." in blocked_page.text
        assert "input_value" not in blocked_page.text
        assert "pydantic.dev" not in blocked_page.text
        assert (
            app.state.repository.require_draft(draft_id).status
            == ReviewStatus.READY_FOR_REVIEW
        )

        approved = approve(
            client,
            draft_id,
            bloom_confirmed="true",
            difficulty_confirmed="true",
            reviewer_notes="Both labels checked.",
        )
        assert approved.status_code == 303
        stored = app.state.repository.require_draft(draft_id)
        assert stored.status == ReviewStatus.READY_TO_PUBLISH
        assert stored.bloom_confirmed is True
        assert stored.difficulty_confirmed is True


def test_specialist_review_refuses_approval_until_the_box_is_ticked(
    tmp_path: Path,
) -> None:
    """The guard as it ships, which nothing else in the suite exercises.

    Characterization, not a new requirement: this pins the refusal so that
    changing it later -- see the question of whether specialist review should be
    persisted at all -- is a deliberate edit to a failing test rather than a
    silent change to behaviour no test describes.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository, specialist=True)

        refused = approve(
            client,
            draft_id,
            bloom_confirmed="true",
            difficulty_confirmed="true",
            reviewer_notes="Both labels checked.",
        )

        assert refused.status_code == 422
        assert SPECIALIST_REFUSAL in refused.text
        assert "Both labels checked." in refused.text
        stored = app.state.repository.require_draft(draft_id)
        assert stored.status == ReviewStatus.READY_FOR_REVIEW
        # The refusal is whole: the two confirmations it did receive are not
        # banked on the way past.
        assert stored.bloom_confirmed is False
        assert stored.difficulty_confirmed is False


def test_specialist_review_approves_once_the_box_is_ticked(tmp_path: Path) -> None:
    """The other half of the guard: ticked, the draft approves like any other."""

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository, specialist=True)

        approved = approve(
            client,
            draft_id,
            bloom_confirmed="true",
            difficulty_confirmed="true",
            specialist_confirmed="true",
            reviewer_notes="Specialist checked.",
        )

        assert approved.status_code == 303
        stored = app.state.repository.require_draft(draft_id)
        assert stored.status == ReviewStatus.READY_TO_PUBLISH
        assert stored.bloom_confirmed is True
        assert stored.difficulty_confirmed is True


def test_a_draft_not_requiring_specialist_review_is_unaffected_by_the_box(
    tmp_path: Path,
) -> None:
    """The flag arms the guard, so without it the field decides nothing.

    Both submissions approve. The point is that the second one -- which ticks a
    box the draft never asked for -- is neither refused nor treated as meaning
    anything.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        repository = app.state.repository
        without = seed(repository)
        approved = approve(
            client,
            without,
            bloom_confirmed="true",
            difficulty_confirmed="true",
        )
        assert approved.status_code == 303
        assert repository.require_draft(without).status == ReviewStatus.READY_TO_PUBLISH

        # A second page, so seeding does not replace the draft just approved.
        second_page = page()
        second_page.source.canonical_url += "/Second"
        second_page.source.path += "/Second"
        second_page.source.page_id = "86188"
        volunteered = seed(repository, second_page)
        approved = approve(
            client,
            volunteered,
            bloom_confirmed="true",
            difficulty_confirmed="true",
            specialist_confirmed="true",
        )
        assert approved.status_code == 303
        assert (
            repository.require_draft(volunteered).status
            == ReviewStatus.READY_TO_PUBLISH
        )


def test_missing_everything_reports_the_bloom_and_difficulty_refusal_first(
    tmp_path: Path,
) -> None:
    """Which of two refusals a reviewer sees when both apply.

    Pinned as found rather than chosen: the confirmation check simply runs
    first. A reviewer who ticks nothing is sent to fix the labels, and only sees
    the specialist requirement on the next attempt. Worth a test because the
    order is invisible at the call site and easy to swap while tidying.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository, specialist=True)

        refused = approve(client, draft_id, reviewer_notes="Nothing ticked.")

        assert refused.status_code == 422
        assert CONFIRMATION_REFUSAL in refused.text
        assert SPECIALIST_REFUSAL not in refused.text


def test_the_specialist_notice_says_it_is_not_recorded(tmp_path: Path) -> None:
    """The whole of ADR 0005's fix is one sentence on the page.

    The decision not to persist is defensible only because the reviewer is told
    so where they would otherwise assume otherwise. An ADR cannot do that job;
    the ADR explains why, the sentence is what prevents the misreading. Losing
    it in a template tidy would restore the original defect in full while every
    other test still passed, so it is pinned rather than trusted.

    The fieldset assertion is the second half: the control is outside `Required
    human checks` deliberately, and sliding back in would make it look like a
    peer of two constraint-backed confirmations again.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        detail = client.get(f"/drafts/{seed(app.state.repository, specialist=True)}")

        assert detail.status_code == 200
        assert "This acknowledgement is not recorded." in detail.text
        assert "flagged for specialist review" in detail.text
        # Not a claim about the reviewer's credentials -- the thing this
        # control was rewritten to stop asserting.
        assert "I am qualified" not in detail.text

        page_html = detail.text
        notice = page_html.index('class="specialist-flag"')
        fieldset = page_html.index("Required human checks")
        assert notice < fieldset, "the notice must sit outside the checks fieldset"

    with TestClient(create_app(settings(tmp_path / "b"))) as client:
        unflagged = client.get(f"/drafts/{seed(client.app.state.repository)}")

        assert "flagged for specialist review" not in unflagged.text


def test_a_specialist_confirmation_is_recorded_in_no_column(tmp_path: Path) -> None:
    """The tripwire for a decision that has now been made: ADR 0005.

    Bloom and difficulty each persist three columns and are enforced by a
    database constraint. The specialist acknowledgement persists nothing -- so an
    approval that carried the flag is indistinguishable afterwards from one that
    did not. That was filed as a defect, and the answer is that it is correct:
    the flag is a model self-assessment, and storing a reviewer's self-declared
    qualification against it would produce a record that reads like specialist
    sign-off without being one.

    So this test no longer pins a gap awaiting a fix. It pins the decision. The
    day someone adds the column, it fails and points at the ADR, instead of the
    change landing unremarked.

    One thing it deliberately does not cover: `ReviewService.decide` accepts no
    specialist signal at all -- the check lives only in the HTTP handler, unlike
    bloom and difficulty which are also enforced in the review decision, in the
    transition, and by the database. That asymmetry is a consequence of the same
    decision, not an oversight beside it.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository, specialist=True)

        approved = approve(
            client,
            draft_id,
            bloom_confirmed="true",
            difficulty_confirmed="true",
            specialist_confirmed="true",
        )
        assert approved.status_code == 303

        stored = app.state.repository.require_draft(draft_id)
        assert stored.current.specialist_review_required is True
        assert not hasattr(stored, "specialist_confirmed")

    # Named for the one event this test exists to catch, so that an unrelated
    # future column cannot fail it and send the reader to the wrong change.
    assert not [
        column.name for column in Draft.__table__.columns if "specialist" in column.name
    ]


def edit(client: TestClient, draft_id: int, **fields: str) -> Response:
    """One edit submission, carrying only the fields a test cares about."""

    return client.post(
        f"/drafts/{draft_id}/edit",
        data={"reviewer_notes": "", **fields},
        headers={
            "X-Reviewer": "reviewer@example.org",
            "Origin": "http://testserver",
        },
        follow_redirects=False,
    )


def refused(response: Response, because: str) -> bool:
    """This route reports a refusal by redirecting with an `error`, not a 422.

    `because` is required rather than optional: any refusal on this route looks
    the same in the status line -- the same-origin check, a JSON decode failure,
    a fixture that stopped validating -- so a test that asserted only "an error
    came back" could stay green while the guard it names had gone.
    """

    if response.status_code != 303:
        return False
    location = response.headers["location"]
    return "error=" in location and quote_plus(because) in location


def test_item_json_is_refused_for_a_multiple_choice_draft(tmp_path: Path) -> None:
    """The edit route picks its branch from the draft, not from the caller.

    The review page offers the JSON editor only for typed items, and the
    field-by-field form only for multiple choice. The route used to decide by
    asking which field arrived, so a caller could reach the typed branch for a
    multiple-choice draft -- a branch that rebuilds the question from the payload
    alone and carries nothing forward. `specialist_review_required` defaults to
    false, so that path could clear it and let the draft approve with no
    specialist confirmation.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        repository = app.state.repository
        draft_id = seed(repository, specialist=True)
        before = repository.require_draft(draft_id)
        payload = json.loads(before.current.model_dump_json())
        payload["specialist_review_required"] = False

        response = edit(client, draft_id, item_json=json.dumps(payload))

        assert refused(response, "edit it with the question form")
        after = repository.require_draft(draft_id)
        assert after.edit_count == before.edit_count
        assert after.current.specialist_review_required is True


def ordering_question() -> QuestionDraft:
    """A typed item, so the review page would offer it the JSON editor."""

    return QuestionDraft(
        concept_label="Energy conservation",
        stem="Order these by increasing energy.",
        choices=[
            Choice(id="A", text="Potential energy", correct=False),
            Choice(id="B", text="Kinetic energy", correct=False),
            Choice(id="C", text="Thermal energy", correct=False),
        ],
        explanation="The source orders these forms by the energy each carries.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
        item_type=AssessmentItemType.ORDERING,
        response=ItemResponse(correct_order=["A", "B", "C"]),
        specialist_review_required=True,
    )


def seed_typed(repository: DraftRepository) -> int:
    stored = repository.replace_generated_drafts(
        page=page(),
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Energy conservation",
                    description="Energy changes form without disappearing.",
                    source_paragraphs=[0],
                ),
                raw=ordering_question(),
                critique=Critique(
                    issues=["Order was unclear."], revision_required=True
                ),
                revised=ordering_question(),
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


def test_the_question_form_is_refused_for_a_typed_draft(tmp_path: Path) -> None:
    """The same mis-dispatch running the other way, which costs more.

    The form branch builds a question without naming an item type, and that
    field defaults to multiple choice -- so reaching it for a typed draft
    rewrites the item as multiple choice and drops its response specification
    entirely. Nothing about the submission looks wrong on the way in.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        repository = app.state.repository
        draft_id = seed_typed(repository)
        before = repository.require_draft(draft_id)

        response = edit(
            client,
            draft_id,
            stem="Order these again.",
            choice_a="Potential energy",
            choice_b="Kinetic energy",
            choice_c="Thermal energy",
            choice_d="Chemical energy",
            correct_choice="A",
            explanation="A rewritten explanation of the ordering.",
            bloom="understand",
            difficulty="easy",
        )

        assert refused(response, "edit it as JSON")
        after = repository.require_draft(draft_id)
        assert after.edit_count == before.edit_count
        assert after.current.item_type == AssessmentItemType.ORDERING
        assert after.current.response.correct_order == ["A", "B", "C"]


def typed_payload(repository: DraftRepository, draft_id: int) -> dict[str, object]:
    """The draft's own JSON, which is exactly what the textarea is prefilled with."""

    return json.loads(repository.require_draft(draft_id).current.model_dump_json())


def test_a_typed_edit_cannot_retract_the_specialist_requirement(
    tmp_path: Path,
) -> None:
    """The JSON editor edits content; it does not edit what review the item needs.

    `specialist_review_required` is an assertion about the item, and the approval
    guard reads it. A reviewer who did not want to claim specialist
    qualification could otherwise clear it here and approve unchallenged.

    Refused rather than quietly restored. Overwriting the submitted value would
    return the same "Draft saved" notice as a real edit, which is the failure
    this whole issue is about -- a caller told it succeeded while the thing it
    asked for did not happen.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        repository = app.state.repository
        draft_id = seed_typed(repository)
        before = repository.require_draft(draft_id)
        payload = typed_payload(repository, draft_id)
        payload["specialist_review_required"] = False
        payload["stem"] = "Order these by increasing energy, carefully."

        response = edit(client, draft_id, item_json=json.dumps(payload))

        assert refused(response, "specialist_review_required")
        after = repository.require_draft(draft_id)
        assert after.edit_count == before.edit_count
        assert after.current.specialist_review_required is True
        assert after.current.stem == before.current.stem


def seed_convertible(repository: DraftRepository) -> int:
    """A typed draft whose shape is *also* valid as multiple choice.

    Four choices, one correct -- so relabelling it `multiple_choice` passes the
    schema's own shape rules and reaches the route's guard. The ordering fixture
    cannot: it has three choices and none correct, so the model rejects the
    relabelled payload before any of this route's code runs, and a test built on
    it would pass while proving nothing.
    """

    item = QuestionDraft(
        concept_label="Energy conservation",
        stem="Select the statement that holds.",
        choices=[
            Choice(id="A", text="Energy is conserved.", correct=True),
            Choice(id="B", text="Energy disappears.", correct=False),
            Choice(id="C", text="Energy is matter.", correct=False),
            Choice(id="D", text="Energy has no units.", correct=False),
        ],
        explanation="The source states that energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
        item_type=AssessmentItemType.SELECT_CHOICE,
    )
    stored = repository.replace_generated_drafts(
        page=page(),
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Energy conservation",
                    description="Energy changes form without disappearing.",
                    source_paragraphs=[0],
                ),
                raw=item,
                critique=Critique(
                    issues=["Wording was loose."], revision_required=True
                ),
                revised=item,
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


def test_a_typed_edit_cannot_change_the_item_type(tmp_path: Path) -> None:
    """The field the dispatch reads must not be editable through the dispatch.

    The branch is chosen from the stored item type, and the payload carries an
    item type too -- so without this, one edit converts a typed item to multiple
    choice, and the guard then refuses the JSON editor that would convert it
    back while the form branch cannot set the field at all. The item strands,
    holding a response specification nothing reads.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        repository = app.state.repository
        draft_id = seed_convertible(repository)
        before = repository.require_draft(draft_id)
        payload = typed_payload(repository, draft_id)
        payload["item_type"] = AssessmentItemType.MULTIPLE_CHOICE.value

        response = edit(client, draft_id, item_json=json.dumps(payload))

        assert refused(response, "item_type")
        after = repository.require_draft(draft_id)
        assert after.edit_count == before.edit_count
        assert after.current.item_type == AssessmentItemType.SELECT_CHOICE


def test_a_typed_edit_that_changes_only_content_still_lands(tmp_path: Path) -> None:
    """The guards refuse two fields, not the edit. Everything else still saves."""

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        repository = app.state.repository
        draft_id = seed_typed(repository)
        payload = typed_payload(repository, draft_id)
        payload["stem"] = "Order these by increasing energy, carefully."

        response = edit(client, draft_id, item_json=json.dumps(payload))

        assert response.status_code == 303
        assert "error=" not in response.headers["location"]
        after = repository.require_draft(draft_id)
        assert after.current.stem == "Order these by increasing energy, carefully."
        assert after.current.specialist_review_required is True
        assert after.current.item_type == AssessmentItemType.ORDERING


def test_hint_validation_preserves_posted_edits_and_persisted_version(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        ladder = HintLadderDraft(
            concept_label="Energy conservation",
            rungs=[
                HintRungDraft(
                    rung=rung,
                    text=f"Saved {rung.value} hint.",
                    citation_paragraphs=[0],
                )
                for rung in HintRungType
            ],
        )
        original = app.state.repository.save_hint_ladder(
            draft_id,
            ladder,
            editor="reviewer@example.org",
        )

        response = client.post(
            f"/drafts/{draft_id}/hints/edit",
            data={
                "conceptual_text": "Unsaved conceptual wording.",
                "conceptual_citations": "47",
                "strategic_text": "Unsaved strategic wording.",
                "strategic_citations": "0",
                "specific_text": "Unsaved specific wording.",
                "specific_citations": "0",
                "reviewer_notes": "Keep these edits visible.",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )

        assert response.status_code == 422
        assert "Unsaved conceptual wording." in response.text
        assert 'value="47"' in response.text
        assert "Keep these edits visible." in response.text
        assert "Allowed paragraphs: 0" in response.text
        assert 'aria-invalid="true"' in response.text
        current = app.state.repository.require_draft(draft_id).current_hint_ladder
        assert current is not None
        assert current.id == original.id
        assert current.ladder.rungs[0].text == "Saved conceptual hint."


def test_failed_hint_edit_keeps_the_approved_disclosure_open(tmp_path: Path) -> None:
    """An approved ladder wraps its edit form in a collapsed <details>.

    A rejected edit must reopen it, or the preserved values, inline errors, and
    aria-invalid markers are rendered where the reviewer cannot see them.
    """

    configured = settings(tmp_path).model_copy(
        update={
            "hint_generation_enabled": True,
            "hint_publication_canary_enabled": True,
        }
    )
    app = create_app(configured)
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.save_hint_ladder(
            draft_id,
            HintLadderDraft(
                concept_label="Energy conservation",
                rungs=[
                    HintRungDraft(
                        rung=rung,
                        text=f"Review {rung.value} evidence.",
                        citation_paragraphs=[0],
                    )
                    for rung in HintRungType
                ],
            ),
            editor="reviewer@example.org",
        )
        approval = client.post(
            f"/drafts/{draft_id}/hints/review",
            data={
                "conceptual_confirmed": "true",
                "strategic_confirmed": "true",
                "specific_confirmed": "true",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )
        assert approval.status_code == 303

        rejected = client.post(
            f"/drafts/{draft_id}/hints/edit",
            data={
                "conceptual_text": "Unsaved conceptual wording.",
                "conceptual_citations": "47",
                "strategic_text": "Unsaved strategic wording.",
                "strategic_citations": "0",
                "specific_text": "Unsaved specific wording.",
                "specific_citations": "0",
                "reviewer_notes": "Keep these edits visible.",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )

        assert rejected.status_code == 422
        assert 'aria-invalid="true"' in rejected.text
        assert "Unsaved conceptual wording." in rejected.text
        assert '<details class="edit-box" open>' in rejected.text


def test_ladder_wide_grounding_issue_is_shown_on_the_hint_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grounding issue with no rung must still reach the reviewer.

    `inspect_hint_grounding` reports a concept mismatch against the ladder as a
    whole, so it carries no rung. The per-rung error map drops those, which
    would block publication with nothing on the page naming the cause. Persisted
    ladders cannot reach that state today — `edit_draft` refuses to change a
    concept and ladders are pinned to an edit count — so the issue is injected
    here to cover the display path that would otherwise fail silently.
    """

    configured = settings(tmp_path).model_copy(
        update={
            "hint_generation_enabled": True,
            "hint_publication_canary_enabled": True,
        }
    )
    app = create_app(configured)
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.save_hint_ladder(
            draft_id,
            HintLadderDraft(
                concept_label="Energy conservation",
                rungs=[
                    HintRungDraft(
                        rung=rung,
                        text=f"Review {rung.value} evidence.",
                        citation_paragraphs=[0],
                    )
                    for rung in HintRungType
                ],
            ),
            editor="reviewer@example.org",
        )

        message = "A hint ladder cannot change the selected concept."
        monkeypatch.setattr(
            main_module,
            "inspect_hint_grounding",
            lambda question, ladder: (
                HintGroundingIssue(code="concept_mismatch", message=message),
            ),
        )
        page = client.get(f"/drafts/{draft_id}")

        assert page.status_code == 200
        assert message in page.text


def test_hint_approval_button_is_not_statically_disabled(tmp_path: Path) -> None:
    """The approve button is gated by JavaScript.

    Shipping `disabled` in the markup makes hint approval impossible whenever
    the script fails to load. The server independently rejects an incomplete
    confirmation set, so the button itself must start enabled.
    """

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.save_hint_ladder(
            draft_id,
            HintLadderDraft(
                concept_label="Energy conservation",
                rungs=[
                    HintRungDraft(
                        rung=rung,
                        text=f"Review {rung.value} evidence.",
                        citation_paragraphs=[0],
                    )
                    for rung in HintRungType
                ],
            ),
            editor="reviewer@example.org",
        )

        detail = client.get(f"/drafts/{draft_id}")

        assert "data-hint-approve>" in detail.text
        assert "data-hint-approve disabled" not in detail.text


def test_incomplete_hint_confirmation_is_rejected_without_javascript(
    tmp_path: Path,
) -> None:
    """Removing the static `disabled` must not weaken the server gate."""

    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.save_hint_ladder(
            draft_id,
            HintLadderDraft(
                concept_label="Energy conservation",
                rungs=[
                    HintRungDraft(
                        rung=rung,
                        text=f"Review {rung.value} evidence.",
                        citation_paragraphs=[0],
                    )
                    for rung in HintRungType
                ],
            ),
            editor="reviewer@example.org",
        )

        response = client.post(
            f"/drafts/{draft_id}/hints/review",
            data={"conceptual_confirmed": "true"},
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )

        assert response.status_code == 422
        assert "confirm all three hint rungs" in response.text
        ladder = app.state.repository.require_draft(draft_id).current_hint_ladder
        assert ladder is not None
        assert ladder.status != "approved"


def test_saved_hint_and_question_approvals_render_as_summaries(tmp_path: Path) -> None:
    configured = settings(tmp_path).model_copy(
        update={
            "hint_generation_enabled": True,
            "hint_publication_canary_enabled": True,
        }
    )
    app = create_app(configured)
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.save_hint_ladder(
            draft_id,
            HintLadderDraft(
                concept_label="Energy conservation",
                rungs=[
                    HintRungDraft(
                        rung=rung,
                        text=f"Review {rung.value} evidence.",
                        citation_paragraphs=[0],
                    )
                    for rung in HintRungType
                ],
            ),
            editor="reviewer@example.org",
        )
        hint_approval = client.post(
            f"/drafts/{draft_id}/hints/review",
            data={
                "conceptual_confirmed": "true",
                "strategic_confirmed": "true",
                "specific_confirmed": "true",
                "reviewer_notes": "All hint checks complete.",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )
        assert hint_approval.status_code == 303
        question_approval = client.post(
            f"/drafts/{draft_id}/review",
            data={
                "decision": "ready_to_publish",
                "bloom_confirmed": "true",
                "difficulty_confirmed": "true",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )
        assert question_approval.status_code == 303

        detail = client.get(f"/drafts/{draft_id}")

        assert "All three hints approved" in detail.text
        assert "Question approved" in detail.text
        assert "Approve saved hint version" not in detail.text
        assert "Approve question revision" not in detail.text


def test_current_successful_publication_drives_queue_and_detail_state(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.apply_review_decision(
            draft_id,
            ReviewDecision(
                status=ReviewStatus.READY_TO_PUBLISH,
                bloom_confirmed=True,
                difficulty_confirmed=True,
            ),
            reviewer="reviewer@example.org",
        )
        mark_published(app.state.repository, draft_id)

        queue = client.get("/")
        assert queue.status_code == 200
        assert "Published to ADAPT" in queue.text
        assert "Approved — not yet published" not in queue.text
        assert "Current revision v1" in queue.text
        assert "Published to ADAPT as question 125" in queue.text

        detail = client.get(f"/drafts/{draft_id}")
        assert detail.status_code == 200
        assert "Approved — not yet published" not in detail.text
        assert "Publication status" in detail.text
        assert "ADAPT question ID: 125" in detail.text
        assert "This publication is bound to the current draft revision." in detail.text
        assert f'action="/drafts/{draft_id}/publish"' not in detail.text


def test_earlier_publication_does_not_mark_a_new_revision_as_published(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        app.state.repository.apply_review_decision(
            draft_id,
            ReviewDecision(
                status=ReviewStatus.READY_TO_PUBLISH,
                bloom_confirmed=True,
                difficulty_confirmed=True,
            ),
            reviewer="reviewer@example.org",
        )
        mark_published(app.state.repository, draft_id)
        app.state.repository.edit_draft(
            draft_id,
            question("Which revised statement about energy is accurate?"),
            editor="editor@example.org",
        )
        app.state.repository.apply_review_decision(
            draft_id,
            ReviewDecision(
                status=ReviewStatus.READY_TO_PUBLISH,
                bloom_confirmed=True,
                difficulty_confirmed=True,
            ),
            reviewer="reviewer@example.org",
        )

        queue = client.get("/")
        assert queue.status_code == 200
        assert "Approved — not yet published" in queue.text
        assert "Current revision v2" in queue.text
        assert "Revision v1 remains published to ADAPT as question 125." in queue.text

        detail = client.get(f"/drafts/{draft_id}")
        assert detail.status_code == 200
        assert "Approved — not yet published" in detail.text
        assert "Current revision v2 is not yet published." in detail.text
        assert "Revision v1 remains published to ADAPT as question 125." in detail.text
        assert "Earlier revision" in detail.text


def test_api_default_mode_is_unchanged() -> None:
    """The form defaults to Choose types; the API contract must not.

    test_computation_pipeline pins a sha256 of the default GenerateRequest from
    the BUILD-08 qualification. Defaulting generation_mode in the schema moves
    that digest and makes the default request unconstructable (selected mode
    requires item_types), so the default lives in the template only. The
    rendered form is covered by tests/browser/test_generation_options_browser.
    """

    from app.schemas import GenerateRequest, SourceType

    request = GenerateRequest(
        source_type=SourceType.SANDBOX,
        source_locator="x",
    )
    assert request.generation_mode == "auto"
