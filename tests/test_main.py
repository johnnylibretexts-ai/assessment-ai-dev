from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main as main_module
from app.config import Settings
from app.content import PublicLibreTextsContentAdapter
from app.db import (
    DraftRepository,
    DraftWrite,
    HintGroundingIssue,
    PublicationState,
)
from app.main import create_app
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
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


def seed(repository: DraftRepository, source_page: NormalizedPage | None = None) -> int:
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
                revised=question(),
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


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

        blocked = client.post(
            f"/drafts/{draft_id}/review",
            data={"decision": "ready_to_publish", "reviewer_notes": "Looks good."},
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )
        assert blocked.status_code == 422
        blocked_page = blocked
        assert (
            "Confirm both the Bloom level and difficulty before approving this draft."
        ) in blocked_page.text
        assert "Looks good." in blocked_page.text
        assert "input_value" not in blocked_page.text
        assert "pydantic.dev" not in blocked_page.text
        assert (
            app.state.repository.require_draft(draft_id).status
            == ReviewStatus.READY_FOR_REVIEW
        )

        approved = client.post(
            f"/drafts/{draft_id}/review",
            data={
                "decision": "ready_to_publish",
                "bloom_confirmed": "true",
                "difficulty_confirmed": "true",
                "reviewer_notes": "Both labels checked.",
            },
            headers={
                "X-Reviewer": "reviewer@example.org",
                "Origin": "http://testserver",
            },
            follow_redirects=False,
        )
        assert approved.status_code == 303
        stored = app.state.repository.require_draft(draft_id)
        assert stored.status == ReviewStatus.READY_TO_PUBLISH
        assert stored.bloom_confirmed is True
        assert stored.difficulty_confirmed is True


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


def test_generate_request_defaults_to_choose_types() -> None:
    """The shipped default mode must be 'selected'."""

    from app.schemas import GenerateRequest, SourceType

    request = GenerateRequest(
        source_type=SourceType.PUBLIC,
        source_locator="https://chem.libretexts.org/x",
        item_types=["multiple_choice"],
    )
    assert request.generation_mode == "selected"


def test_choose_types_default_is_unusable_without_a_ticked_format() -> None:
    """Guards the reason main.py ships DEFAULT_ITEM_TYPE pre-ticked.

    If someone later removes the pre-ticked checkbox from the template, this
    records why the form would break: the default mode cannot be submitted with
    an empty item_types list.
    """

    import pytest
    from pydantic import ValidationError

    from app.schemas import GenerateRequest, SourceType

    with pytest.raises(ValidationError, match="choose at least one item type"):
        GenerateRequest(
            source_type=SourceType.PUBLIC,
            source_locator="https://chem.libretexts.org/x",
        )
