from __future__ import annotations

import json
from html import escape

from pydantic import BaseModel, Field

from app.schemas import QuestionDraft


class FrameworkItem(BaseModel):
    id: int = Field(gt=0)
    text: str = Field(min_length=1)


class FrameworkAlignment(BaseModel):
    levels: list[FrameworkItem] = Field(default_factory=list)
    descriptors: list[FrameworkItem] = Field(default_factory=list)


class AdaptDestination(BaseModel):
    folder_id: int = Field(gt=0)
    author: str = Field(min_length=1)
    license: str = Field(min_length=1)
    license_version: str | None = None
    public: bool = False


def build_mcq_payload(
    draft: QuestionDraft,
    *,
    destination: AdaptDestination,
    source_url: str,
    title: str,
    alignment: FrameworkAlignment | None = None,
) -> dict[str, object]:
    """Build the verified ADAPT QTI-MCQ create payload without sending it.

    ADAPT validates the duplicated ``qti_prompt`` and ``qti_simple_choice_*`` fields in
    addition to ``qti_json``. Framework links are part of this same request; there is no
    separate framework-sync POST endpoint.
    """

    prompt_html = f"<p>{escape(draft.stem)}</p>"
    simple_choices: list[dict[str, object]] = []
    feedback: dict[str, str] = {}
    payload: dict[str, object] = {
        "question_type": "assessment",
        "folder_id": destination.folder_id,
        "public": int(destination.public),
        "title": title,
        "author": destination.author,
        "tags": [],
        "technology": "qti",
        "technology_id": None,
        "non_technology_text": None,
        "text_question": None,
        "a11y_technology": None,
        "a11y_technology_id": None,
        "answer_html": None,
        "solution_html": draft.explanation,
        "notes": "Generated as a human-reviewed draft by LibreTexts Assessment AI.",
        "hint": None,
        "license": destination.license,
        "license_version": destination.license_version,
        "source_url": source_url,
        "qti_prompt": prompt_html,
    }

    for index, choice in enumerate(draft.choices):
        identifier = f"assessment-ai-{choice.id.lower()}"
        simple_choices.append(
            {
                "identifier": identifier,
                "value": escape(choice.text),
                "correctResponse": choice.correct,
            }
        )
        payload[f"qti_simple_choice_{index}"] = choice.text
        if choice.feedback:
            feedback[identifier] = escape(choice.feedback)

    qti_json: dict[str, object] = {
        "questionType": "multiple_choice",
        "prompt": prompt_html,
        "simpleChoice": simple_choices,
    }
    if feedback:
        qti_json["feedback"] = feedback
    payload["qti_json"] = json.dumps(
        qti_json, separators=(",", ":"), ensure_ascii=False
    )

    if alignment is not None:
        payload["framework_item_sync_question"] = alignment.model_dump()

    return payload
