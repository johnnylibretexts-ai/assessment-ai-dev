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


def seed(repository: DraftRepository) -> int:
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


def test_health_and_empty_queue_work_without_cloud_key(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "generation": "needs_ollama_api_key",
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
