from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas import (
    AssessmentItemType,
    HintLadderDraft,
    ItemContextType,
    QuestionDraft,
)
from app.source_policy import PublicSourceValidationError, parse_public_source_url


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DomainStratum(StrEnum):
    CHEMISTRY = "chemistry"
    BIOLOGY = "biology"
    MATHEMATICS = "mathematics"
    MEDICINE_HEALTH = "medicine_health"
    HUMANITIES_SOCIAL = "humanities_social"
    SPANISH_FRENCH = "spanish_french"


class CorpusPage(BaseModel):
    page_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    stratum: DomainStratum
    title: str = Field(min_length=2, max_length=500)
    canonical_url: str = Field(min_length=1, max_length=4_096)
    source_identity: str = Field(min_length=1, max_length=4_096)
    source_page_id: str | None = Field(default=None, max_length=200)
    license: str = Field(min_length=2, max_length=200)
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    paragraph_sha256: list[str] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_source(self) -> "CorpusPage":
        try:
            location = parse_public_source_url(self.canonical_url)
        except PublicSourceValidationError as exc:
            raise ValueError("corpus URL is not an approved public source") from exc
        if location.identity != self.source_identity:
            raise ValueError("source_identity does not match canonical_url")
        if any(
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in self.paragraph_sha256
        ):
            raise ValueError("paragraph_sha256 entries must be lowercase SHA-256")
        if len(self.paragraph_sha256) != len(set(self.paragraph_sha256)):
            raise ValueError("paragraph hashes must be unique within a page")
        return self


class CorpusManifest(BaseModel):
    schema_version: Literal["build08-corpus-v1"] = "build08-corpus-v1"
    pages: list[CorpusPage] = Field(min_length=48, max_length=48)

    @model_validator(mode="after")
    def validate_distribution(self) -> "CorpusManifest":
        for field_name, values in {
            "page_key": [page.page_key for page in self.pages],
            "canonical_url": [page.canonical_url for page in self.pages],
            "source_identity": [page.source_identity for page in self.pages],
            "content_sha256": [page.content_sha256 for page in self.pages],
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"corpus {field_name} values must be unique")
        counts = {
            stratum: sum(page.stratum == stratum for page in self.pages)
            for stratum in DomainStratum
        }
        if any(count != 8 for count in counts.values()):
            raise ValueError("corpus requires exactly eight pages in each stratum")
        return self


class FixtureCase(BaseModel):
    fixture_id: str = Field(pattern=r"^build08-[a-z0-9_-]+$")
    draft: QuestionDraft
    hint_ladder: HintLadderDraft


class FixtureBundle(BaseModel):
    schema_version: Literal["build08-fixtures-v1"] = "build08-fixtures-v1"
    cases: list[FixtureCase] = Field(min_length=95, max_length=95)

    @model_validator(mode="after")
    def validate_matrix(self) -> "FixtureBundle":
        identifiers = [case.fixture_id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("fixture IDs must be unique")
        combinations = {
            (case.draft.item_type, case.draft.context_type) for case in self.cases
        }
        expected = {
            (item_type, context_type)
            for item_type in AssessmentItemType
            for context_type in ItemContextType
        }
        if combinations != expected:
            raise ValueError("fixture bundle must cover every item/context combination")
        return self


class Publishability(StrEnum):
    NO_EDIT = "no_edit"
    MINOR_EDIT = "minor_edit"
    MAJOR_EDIT = "major_edit"
    REJECT = "reject"


class ReviewRole(StrEnum):
    PRIMARY = "primary"
    INDEPENDENT = "independent"
    ADJUDICATION = "adjudication"


class ReviewRecord(BaseModel):
    schema_version: Literal["build08-review-v1"] = "build08-review-v1"
    run_id: str = Field(min_length=1, max_length=100)
    draft_id: str = Field(min_length=1, max_length=200)
    page_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    item_type: AssessmentItemType
    context_type: ItemContextType
    reviewer_id: str = Field(min_length=1, max_length=100)
    role: ReviewRole
    critical_defect: bool
    factual_correct: bool
    source_supported: bool
    answer_correct: bool
    interaction_quality: bool
    bloom_aligned: bool
    difficulty_aligned: bool
    accessible: bool
    publishability: Publishability
    citation_complete: bool
    license_complete: bool
    hint_ladder_reviewed: bool
    hints_non_leaking: bool
    hints_progressive: bool
    specialist_qualified: bool = False
    clinical_approved: bool | None = None
    notes: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def validate_specialist_fields(self) -> "ReviewRecord":
        if self.item_type == AssessmentItemType.BOW_TIE:
            if self.role == ReviewRole.PRIMARY and self.clinical_approved is None:
                raise ValueError("primary bow-tie review requires a clinical decision")
        elif self.clinical_approved is not None:
            raise ValueError("clinical decisions are reserved for bow-tie reviews")
        return self


class SeedPlanCase(BaseModel):
    schema_version: Literal["build08-seed-plan-v1"] = "build08-seed-plan-v1"
    run_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=200)
    item_type: Literal[AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS]
    seed: int = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_prompt: str = Field(min_length=1, max_length=4_000)
    expected_answer: float
    wrong_answer: float


class SeedReceipt(BaseModel):
    schema_version: Literal["build08-seed-receipt-v1"] = "build08-seed-receipt-v1"
    run_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=200)
    item_type: Literal[AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS]
    seed: int = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    deterministic: bool
    constraints_satisfied: bool
    rendered: bool
    render_duration_ms: int = Field(ge=0, le=120_000)
    warning_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    outbound_request_count: int = Field(ge=0)
    expected_answer_accepted: bool
    wrong_answer_rejected: bool
    persisted_grade_match: bool
    object_idempotent: bool
    cross_owner_access_blocked: bool


class ShadowMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"


class ShadowReceipt(BaseModel):
    schema_version: Literal["build08-shadow-v1"] = "build08-shadow-v1"
    run_id: str = Field(min_length=1, max_length=100)
    case_id: str = Field(min_length=1, max_length=200)
    item_type: AssessmentItemType
    mode: ShadowMode
    request_id: str = Field(min_length=1, max_length=200)
    event_ids: list[str] = Field(default_factory=list, max_length=20)
    expected_event_count: int = Field(ge=0, le=20)
    observed_event_count: int = Field(ge=0, le=20)
    student_dom_sha256: str = Field(pattern=SHA256_PATTERN)
    student_api_sha256: str = Field(pattern=SHA256_PATTERN)
    submission_sha256: str = Field(pattern=SHA256_PATTERN)
    grade: float
    penalty: float
    shown_hint_state: str = Field(max_length=500)
    gradebook_sha256: str = Field(pattern=SHA256_PATTERN)
    contains_pii_or_response_content: bool = False

    @model_validator(mode="after")
    def validate_events(self) -> "ShadowReceipt":
        if self.observed_event_count != len(self.event_ids):
            raise ValueError("observed_event_count must equal event_ids length")
        if len(self.event_ids) != len(set(self.event_ids)):
            raise ValueError("event IDs must be unique within a receipt")
        if self.mode == ShadowMode.OFF and (
            self.expected_event_count or self.observed_event_count
        ):
            raise ValueError("off receipts cannot contain shadow events")
        return self


class SectionResult(BaseModel):
    name: str
    passed: bool
    counts: dict[str, int | float | str | bool] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)


class QualificationReport(BaseModel):
    schema_version: Literal["build08-qualification-report-v1"] = (
        "build08-qualification-report-v1"
    )
    passed: bool
    sections: list[SectionResult]
