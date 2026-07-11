import json

from app.adapt import (
    AdaptDestination,
    FrameworkAlignment,
    FrameworkItem,
    build_mcq_payload,
)
from app.schemas import BloomLevel, Choice, Difficulty, QuestionDraft


def draft() -> QuestionDraft:
    return QuestionDraft(
        concept_label="Conservation of energy",
        stem="Which statement best describes <energy>?",
        choices=[
            Choice(id="A", text="It is conserved.", correct=True, feedback="Correct."),
            Choice(id="B", text="It disappears.", correct=False),
            Choice(id="C", text="It is matter.", correct=False),
            Choice(id="D", text="It has no units.", correct=False),
        ],
        explanation="The cited passage states that total energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[2],
    )


def test_builds_verified_adapt_mcq_shape_and_embeds_alignment() -> None:
    payload = build_mcq_payload(
        draft(),
        destination=AdaptDestination(
            folder_id=42, author="Assessment Reviewer", license="ccby", public=False
        ),
        source_url="https://dev.libretexts.org/Sandboxes/johnnyphung/book/page",
        title="Energy draft",
        alignment=FrameworkAlignment(
            levels=[FrameworkItem(id=10, text="Physics")],
            descriptors=[FrameworkItem(id=11, text="Explain energy conservation")],
        ),
    )

    assert payload["technology"] == "qti"
    assert payload["folder_id"] == 42
    assert (
        payload["qti_prompt"] == "<p>Which statement best describes &lt;energy&gt;?</p>"
    )
    assert [payload[f"qti_simple_choice_{i}"] for i in range(4)] == [
        "It is conserved.",
        "It disappears.",
        "It is matter.",
        "It has no units.",
    ]
    qti = json.loads(payload["qti_json"])
    assert qti["questionType"] == "multiple_choice"
    assert sum(choice["correctResponse"] for choice in qti["simpleChoice"]) == 1
    assert payload["framework_item_sync_question"] == {
        "levels": [{"id": 10, "text": "Physics"}],
        "descriptors": [{"id": 11, "text": "Explain energy conservation"}],
    }


def test_alignment_is_omitted_when_not_selected() -> None:
    payload = build_mcq_payload(
        draft(),
        destination=AdaptDestination(
            folder_id=42, author="Assessment Reviewer", license="ccby"
        ),
        source_url="https://dev.libretexts.org/Sandboxes/johnnyphung/book/page",
        title="Energy draft",
    )
    assert "framework_item_sync_question" not in payload
