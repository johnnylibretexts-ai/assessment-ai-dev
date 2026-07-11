from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Paragraph(BaseModel):
    index: int = Field(ge=0)
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SourceInfo(BaseModel):
    backend: str = "cxone_sandbox"
    canonical_url: str
    path: str
    page_id: str | None = None


class NormalizedPage(BaseModel):
    title: str = Field(min_length=1)
    plaintext: str = Field(min_length=1)
    html_body: str = Field(alias="htmlBody")
    paragraphs: list[Paragraph] = Field(min_length=1)
    source: SourceInfo

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_offsets(self) -> "NormalizedPage":
        previous_end = 0
        for paragraph in self.paragraphs:
            if paragraph.start < previous_end or paragraph.end > len(self.plaintext):
                raise ValueError("paragraph offsets are not monotonic and in bounds")
            if self.plaintext[paragraph.start : paragraph.end] != paragraph.text:
                raise ValueError(
                    "paragraph offsets do not slice back to paragraph text"
                )
            previous_end = paragraph.end
        return self


class TocNode(BaseModel):
    title: str
    path: str
    page_id: str | None = None
    children: list["TocNode"] = Field(default_factory=list)


class Concept(BaseModel):
    label: str = Field(min_length=2, max_length=160)
    description: str = Field(min_length=2, max_length=500)
    source_paragraphs: list[int] = Field(min_length=1)


class ConceptBatch(BaseModel):
    concepts: list[Concept] = Field(min_length=1, max_length=12)


class BloomLevel(StrEnum):
    REMEMBER = "remember"
    UNDERSTAND = "understand"
    APPLY = "apply"
    ANALYZE = "analyze"
    EVALUATE = "evaluate"
    CREATE = "create"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Choice(BaseModel):
    id: str = Field(pattern=r"^[A-H]$")
    text: str = Field(min_length=1, max_length=1_000)
    correct: bool = False
    feedback: str | None = Field(default=None, max_length=1_000)


class QuestionDraft(BaseModel):
    concept_label: str = Field(min_length=2, max_length=160)
    stem: str = Field(min_length=5, max_length=2_000)
    choices: list[Choice] = Field(min_length=4, max_length=4)
    explanation: str = Field(min_length=5, max_length=4_000)
    bloom: BloomLevel
    difficulty: Difficulty
    citation_paragraphs: list[int] = Field(min_length=1)
    needs_human_verification: bool = False

    @model_validator(mode="after")
    def validate_choices(self) -> "QuestionDraft":
        ids = [choice.id for choice in self.choices]
        if len(ids) != len(set(ids)):
            raise ValueError("choice ids must be unique")
        if sum(choice.correct for choice in self.choices) != 1:
            raise ValueError(
                "multiple-choice drafts require exactly one correct choice"
            )
        return self


class Critique(BaseModel):
    issues: list[str] = Field(default_factory=list, max_length=12)
    distractor_flags: list[str] = Field(default_factory=list, max_length=12)
    revision_instructions: list[str] = Field(default_factory=list, max_length=12)
    revision_required: bool


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
    READY_TO_PUBLISH = "ready_to_publish"
    REJECTED = "rejected"


class SourceType(StrEnum):
    PUBLIC = "public"
    SANDBOX = "sandbox"


class GenerateRequest(BaseModel):
    source_type: SourceType
    source_locator: str


class ReviewDecision(BaseModel):
    status: ReviewStatus
    bloom_confirmed: bool = False
    difficulty_confirmed: bool = False
    reviewer_notes: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def enforce_human_gates(self) -> "ReviewDecision":
        if self.status == ReviewStatus.READY_TO_PUBLISH:
            if not self.bloom_confirmed or not self.difficulty_confirmed:
                raise ValueError(
                    "Bloom and difficulty require separate human confirmation"
                )
        return self
