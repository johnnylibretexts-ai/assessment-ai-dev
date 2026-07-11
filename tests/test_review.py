from __future__ import annotations

from pathlib import Path

import pytest

from app.db import (
    DraftGroundingError,
    DraftRepository,
    DraftWrite,
    ReviewGateError,
    ReviewTransitionError,
    init_database,
)
from app.pipeline import ReviewService
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
)


def source_page() -> NormalizedPage:
    text = "Total energy is conserved."
    return NormalizedPage(
        title="Energy",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            canonical_url=(
                "https://dev.libretexts.org/Sandboxes/johnnyphung/Demo/Energy"
            ),
            path="Sandboxes/johnnyphung/Demo/Energy",
            page_id="123",
        ),
    )


def item(stem: str = "Which statement best describes total energy?") -> QuestionDraft:
    return QuestionDraft(
        concept_label="Conservation of energy",
        stem=stem,
        choices=[
            Choice(id="A", text="It remains constant.", correct=True),
            Choice(id="B", text="It disappears.", correct=False),
            Choice(id="C", text="It becomes matter.", correct=False),
            Choice(id="D", text="It has no measurable value.", correct=False),
        ],
        explanation="The cited source says that total energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


@pytest.fixture
def reviewed_store(tmp_path: Path):
    database = init_database(f"sqlite:///{tmp_path / 'review.db'}")
    repository = DraftRepository(database)
    original = item()
    stored = repository.replace_generated_drafts(
        page=source_page(),
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Conservation of energy",
                    description="Total energy remains constant.",
                    source_paragraphs=[0],
                ),
                raw=item("What happens to total energy?"),
                critique=Critique(
                    issues=["The stem is vague."],
                    revision_instructions=["Name total energy."],
                    revision_required=True,
                ),
                revised=original,
            )
        ],
        llm_calls=[],
    )
    yield database, repository, ReviewService(repository), stored.draft_ids[0]
    database.dispose()


def test_ready_to_publish_requires_two_independent_human_confirmations(
    reviewed_store,
) -> None:
    _database, repository, service, draft_id = reviewed_store

    with pytest.raises(ReviewGateError):
        repository.transition_status(
            draft_id,
            ReviewStatus.READY_TO_PUBLISH,
            reviewer="faculty@example.edu",
        )

    after_bloom = service.confirm_bloom(draft_id, reviewer="bloom-reviewer@example.edu")
    assert after_bloom.bloom_confirmed is True
    assert after_bloom.difficulty_confirmed is False
    with pytest.raises(ReviewGateError):
        repository.transition_status(
            draft_id,
            ReviewStatus.READY_TO_PUBLISH,
            reviewer="faculty@example.edu",
        )

    after_difficulty = service.confirm_difficulty(
        draft_id, reviewer="difficulty-reviewer@example.edu"
    )
    assert after_difficulty.bloom_confirmed_by == "bloom-reviewer@example.edu"
    assert after_difficulty.difficulty_confirmed_by == "difficulty-reviewer@example.edu"
    published = repository.transition_status(
        draft_id,
        ReviewStatus.READY_TO_PUBLISH,
        reviewer="faculty@example.edu",
        notes="Both labels checked against the source.",
    )
    assert published.status == ReviewStatus.READY_TO_PUBLISH
    assert published.reviewer_notes == "Both labels checked against the source."
    assert published.lifecycle_status == "draft"


def test_api_shaped_decision_records_each_gate_separately(reviewed_store) -> None:
    _database, _repository, service, draft_id = reviewed_store
    decision = ReviewDecision(
        status=ReviewStatus.READY_TO_PUBLISH,
        bloom_confirmed=True,
        difficulty_confirmed=True,
        reviewer_notes="Ready for a later publishing integration.",
    )

    draft = service.decide(draft_id, decision, reviewer="faculty@example.edu")

    events = [event["event"] for event in draft.review_history_json]
    assert events[-3:] == [
        "bloom_confirmation",
        "difficulty_confirmation",
        "status_changed",
    ]
    assert draft.status == ReviewStatus.READY_TO_PUBLISH


def test_rejection_is_allowed_without_taxonomy_confirmations(reviewed_store) -> None:
    _database, _repository, service, draft_id = reviewed_store

    rejected = service.reject(
        draft_id,
        reviewer="faculty@example.edu",
        notes="The distractors are not instructionally useful.",
    )

    assert rejected.status == ReviewStatus.REJECTED
    assert rejected.bloom_confirmed is False
    assert rejected.difficulty_confirmed is False
    assert rejected.reviewer_notes.startswith("The distractors")
    with pytest.raises(ReviewTransitionError, match="reopen"):
        service.confirm_bloom(draft_id, reviewer="faculty@example.edu")


def test_revoking_a_gate_demotes_a_publish_ready_draft(reviewed_store) -> None:
    _database, repository, service, draft_id = reviewed_store
    service.confirm_bloom(draft_id, reviewer="faculty@example.edu")
    service.confirm_difficulty(draft_id, reviewer="faculty@example.edu")
    repository.transition_status(
        draft_id,
        ReviewStatus.READY_TO_PUBLISH,
        reviewer="faculty@example.edu",
    )

    demoted = service.confirm_bloom(
        draft_id,
        reviewer="faculty@example.edu",
        confirmed=False,
    )

    assert demoted.status == ReviewStatus.READY_FOR_REVIEW
    assert demoted.bloom_confirmed is False
    assert demoted.difficulty_confirmed is False
    assert demoted.review_history_json[-1]["event"] == "status_changed"


def test_human_edit_preserves_generated_versions_and_full_audit_fields(
    reviewed_store,
) -> None:
    _database, repository, service, draft_id = reviewed_store
    service.confirm_bloom(draft_id, reviewer="faculty@example.edu")
    service.confirm_difficulty(draft_id, reviewer="faculty@example.edu")
    repository.transition_status(
        draft_id,
        ReviewStatus.READY_TO_PUBLISH,
        reviewer="faculty@example.edu",
    )
    before = repository.require_draft(draft_id)
    generated_revision = before.revised_json.copy()
    edited_item = item("According to the passage, what happens to total energy?")

    edited = service.edit(
        draft_id,
        edited_item,
        editor="editor@example.edu",
        notes="Clarified that the question is source-bound.",
    )

    assert edited.raw_json["stem"] == "What happens to total energy?"
    assert edited.revised_json == generated_revision
    assert edited.current_json["stem"] == edited_item.stem
    assert edited.current_json != edited.revised_json
    assert edited.status == ReviewStatus.READY_FOR_REVIEW
    assert edited.bloom_confirmed is False
    assert edited.difficulty_confirmed is False
    assert edited.edit_count == 1
    assert edited.edited_by == "editor@example.edu"
    assert edited.edited_at is not None
    audit_event = edited.review_history_json[-1]
    assert audit_event["event"] == "edited"
    assert audit_event["before"] == generated_revision
    assert audit_event["after"] == edited.current_json
    assert audit_event["notes"] == "Clarified that the question is source-bound."


@pytest.mark.parametrize(
    "invalid",
    [
        item().model_copy(update={"citation_paragraphs": [99]}),
        item().model_copy(update={"concept_label": "A different concept"}),
    ],
)
def test_human_edit_cannot_break_source_or_concept_grounding(
    reviewed_store,
    invalid: QuestionDraft,
) -> None:
    _database, repository, service, draft_id = reviewed_store
    before = repository.require_draft(draft_id)

    with pytest.raises(DraftGroundingError):
        service.edit(draft_id, invalid, editor="editor@example.edu")

    unchanged = repository.require_draft(draft_id)
    assert unchanged.current_json == before.current_json
    assert unchanged.edit_count == 0


def test_failed_api_decision_rolls_back_both_gate_changes(reviewed_store) -> None:
    _database, repository, service, draft_id = reviewed_store
    invalid_transition = ReviewDecision(
        status=ReviewStatus.DRAFT,
        bloom_confirmed=True,
        difficulty_confirmed=True,
    )

    with pytest.raises(ReviewTransitionError):
        service.decide(
            draft_id,
            invalid_transition,
            reviewer="faculty@example.edu",
        )

    unchanged = repository.require_draft(draft_id)
    assert unchanged.status == ReviewStatus.READY_FOR_REVIEW
    assert unchanged.bloom_confirmed is False
    assert unchanged.difficulty_confirmed is False


def test_rejected_draft_can_only_be_reopened_for_review(reviewed_store) -> None:
    _database, repository, service, draft_id = reviewed_store
    service.reject(draft_id, reviewer="faculty@example.edu")

    with pytest.raises(ReviewTransitionError):
        repository.transition_status(
            draft_id,
            ReviewStatus.READY_TO_PUBLISH,
            reviewer="faculty@example.edu",
        )

    reopened = repository.transition_status(
        draft_id,
        ReviewStatus.READY_FOR_REVIEW,
        reviewer="faculty@example.edu",
        notes="Reopened for a substantive rewrite.",
    )
    assert reopened.status == ReviewStatus.READY_FOR_REVIEW
    assert reopened.bloom_confirmed is False
    assert reopened.difficulty_confirmed is False
