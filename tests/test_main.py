from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.db import DraftRepository, DraftWrite
from app.main import create_app
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewStatus,
    SourceInfo,
    SourceType,
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
            canonical_url="https://dev.libretexts.org/Sandboxes/johnnyphung/Energy",
            path="Sandboxes/johnnyphung/Energy",
            page_id="7",
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


def public_page() -> NormalizedPage:
    source_page = page()
    source_page.source = SourceInfo(
        backend="libretexts_public",
        canonical_url="https://chem.libretexts.org/Bookshelves/Test/Page",
        path="chem.libretexts.org/Bookshelves/Test/Page",
        page_id="86187",
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
            "adapt_publishing": "disabled",
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


def test_source_selector_and_public_feature_disabled_behavior(tmp_path: Path) -> None:
    disabled_app = create_app(settings(tmp_path))
    with TestClient(disabled_app) as client:
        form = client.get("/")
        assert 'name="source_type" value="public" disabled' in form.text
        assert 'name="source_type" value="sandbox" checked' in form.text
        assert 'name="source_locator"' in form.text
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
        update={"public_sources_enabled": True}
    )
    with TestClient(create_app(enabled_settings)) as client:
        form = client.get("/")
        assert 'name="source_type" value="public" checked' in form.text
        assert 'name="source_type" value="sandbox" checked' not in form.text


def test_legacy_sandbox_path_is_dispatched_as_sandbox(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    selected: list[SourceType] = []

    def record_source_type(source_type: SourceType) -> None:
        selected.append(source_type)
        raise ValueError("stop after dispatch")

    with TestClient(app) as client:
        app.state.content_factory = record_source_type
        response = client.post(
            "/generate",
            data={"sandbox_path": "Sandboxes/johnnyphung/Energy"},
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": "reviewer@example.org",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert selected == [SourceType.SANDBOX]


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
    assert "Paragraph 0" in detail.text


def test_edit_and_independent_review_gates(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        draft_id = seed(app.state.repository)
        detail = client.get(f"/drafts/{draft_id}")
        assert detail.status_code == 200
        assert "Which statement about energy is accurate?" in detail.text
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
        assert blocked.status_code == 303
        assert "error=" in blocked.headers["location"]
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
