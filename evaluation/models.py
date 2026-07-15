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


class OutageBoundary(StrEnum):
    PROVIDER = "provider"
    ASSESSMENT_AI = "assessment_ai"
    ADAPT_PUBLICATION = "adapt_publication"
    WEBWORK = "webwork"
    RENDERER = "renderer"
    IMATHAS_BRIDGE = "imathas_bridge"
    QTI_STORAGE = "qti_storage"


class OutageOutcome(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


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


class ProviderCallReceipt(BaseModel):
    schema_version: Literal["build08-provider-call-v1"] = "build08-provider-call-v1"
    qualification_run_id: str = Field(min_length=1, max_length=100)
    call_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sequence: int = Field(ge=1)
    case_id: str = Field(pattern=r"^build08-draft-[0-9]{3}$")
    stage: Literal[
        "concept_extraction",
        "initial_draft",
        "critique",
        "revision",
        "hint_ladder",
    ]
    provider: Literal["gemini"] = "gemini"
    model: Literal["gemini-2.5-flash"] = "gemini-2.5-flash"
    prompt_version: str = Field(min_length=1, max_length=100)
    attempt_count: int = Field(ge=1, le=3)
    prompt_token_count: int = Field(ge=1)
    output_token_count: int = Field(ge=1)
    total_token_count: int = Field(ge=2)
    estimated_cost_microusd: int = Field(ge=1)
    input_rate_microusd_per_million: Literal[300_000] = 300_000
    output_rate_microusd_per_million: Literal[2_500_000] = 2_500_000
    rate_card_id: Literal["gemini-2.5-flash-paid-2026-07-14"] = (
        "gemini-2.5-flash-paid-2026-07-14"
    )
    max_output_tokens: Literal[8_192] = 8_192
    per_call_reserve_microusd: Literal[1_000_000] = 1_000_000
    budget_ceiling_microusd: Literal[100_000_000] = 100_000_000
    usage_complete: bool = True

    @model_validator(mode="after")
    def validate_usage(self) -> "ProviderCallReceipt":
        if self.total_token_count != (
            self.prompt_token_count + self.output_token_count
        ):
            raise ValueError("total tokens must equal prompt plus output tokens")
        return self


class BudgetReservation(BaseModel):
    case_id: str = Field(pattern=r"^build08-draft-[0-9]{3}$")
    stage: Literal[
        "concept_extraction",
        "initial_draft",
        "critique",
        "revision",
        "hint_ladder",
    ]
    reserved_microusd: Literal[1_000_000] = 1_000_000


class ProviderBudgetState(BaseModel):
    schema_version: Literal["build08-provider-budget-v1"] = (
        "build08-provider-budget-v1"
    )
    qualification_run_id: str = Field(min_length=1, max_length=100)
    budget_ceiling_microusd: Literal[100_000_000] = 100_000_000
    per_call_reserve_microusd: Literal[1_000_000] = 1_000_000
    settled_microusd: int = Field(ge=0)
    open_reservations: dict[str, BudgetReservation] = Field(default_factory=dict)


class DraftPlanCase(BaseModel):
    sequence: int = Field(ge=1, le=380)
    case_id: str = Field(pattern=r"^build08-draft-[0-9]{3}$")
    pilot_case: bool
    page_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    stratum: DomainStratum
    item_type: AssessmentItemType

    @model_validator(mode="after")
    def validate_identity(self) -> "DraftPlanCase":
        if self.case_id != f"build08-draft-{self.sequence:03d}":
            raise ValueError("case_id must match the plan sequence")
        if self.pilot_case != (self.sequence <= 19):
            raise ValueError("only the first 19 plan cases belong to the pilot")
        return self


class DraftQualificationPlan(BaseModel):
    schema_version: Literal["build08-draft-plan-v1"] = "build08-draft-plan-v1"
    corpus_sha256: str = Field(pattern=SHA256_PATTERN)
    cases: list[DraftPlanCase] = Field(min_length=380, max_length=380)


class DraftQualificationReceipt(BaseModel):
    schema_version: Literal["build08-draft-qualification-v1"] = (
        "build08-draft-qualification-v1"
    )
    qualification_run_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=1, le=380)
    case_id: str = Field(pattern=r"^build08-draft-[0-9]{3}$")
    pilot_case: bool
    generation_run_id: str = Field(min_length=1, max_length=100)
    draft_id: int = Field(gt=0)
    page_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    stratum: DomainStratum
    source_identity: str = Field(min_length=1, max_length=4_096)
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    license: str = Field(min_length=2, max_length=200)
    item_type: AssessmentItemType
    context_type: ItemContextType
    provider_call_ids: list[str] = Field(min_length=5, max_length=5)
    schema_valid: bool
    citation_valid: bool
    source_hash_valid: bool
    license_valid: bool
    critique_executed: bool
    revision_executed: bool
    hint_ladder_executed: bool
    hint_rung_count: int = Field(ge=0, le=3)
    hint_leak_detected: bool
    qti_valid: bool
    qti_sha256: str = Field(pattern=SHA256_PATTERN)
    engine_validation_passed: bool
    unsafe_executable_source_detected: bool
    detected_critical_defect: bool
    advanced_flags_false: bool
    adapt_publishing_disabled: bool
    publication_attempt_count: int = Field(ge=0)
    lifecycle_status: Literal["draft"] = "draft"
    review_status: Literal["ready_for_review"] = "ready_for_review"
    artifact_label: Literal["ai_generated_unreviewed_dev_demo"] = (
        "ai_generated_unreviewed_dev_demo"
    )

    @model_validator(mode="after")
    def validate_identity(self) -> "DraftQualificationReceipt":
        if self.case_id != f"build08-draft-{self.sequence:03d}":
            raise ValueError("case_id must match the receipt sequence")
        if self.pilot_case != (self.sequence <= 19):
            raise ValueError("only the first 19 cases belong to the pilot")
        if len(self.provider_call_ids) != len(set(self.provider_call_ids)):
            raise ValueError("provider call IDs must be unique within a draft")
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


class AdaptSeedAttestation(BaseModel):
    schema_version: Literal["build08-adapt-seed-attestation-v1"] = (
        "build08-adapt-seed-attestation-v1"
    )
    run_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=200)
    item_type: Literal[AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS]
    seed: int = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    adapt_image_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    clone_backup_sha256: str = Field(pattern=SHA256_PATTERN)
    adapt_question_id: int = Field(gt=0)
    adapt_assignment_id: int = Field(gt=0)
    adapt_submission_id: int = Field(gt=0)
    expected_score: float = Field(ge=0, le=1)
    persisted_score: float = Field(ge=0)
    submission_count: int = Field(ge=1, le=100)
    grade_refreshed: bool
    object_idempotent: bool
    cross_owner_access_blocked: bool
    canary_network_internal: bool
    hint_mode_off: bool


class AdaptSeedItem(BaseModel):
    schema_version: Literal["build08-adapt-seed-item-v1"] = (
        "build08-adapt-seed-item-v1"
    )
    run_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=200)
    item_type: Literal[AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS]
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    engine_source: str = Field(min_length=1, max_length=100_000)
    technology_id: int | None = Field(default=None, gt=0)
    engine_object_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_engine_object(self) -> "AdaptSeedItem":
        if self.item_type == AssessmentItemType.IMATHAS:
            if self.technology_id is None or self.engine_object_sha256 is None:
                raise ValueError("IMathAS seed items require the linked engine object")
        elif self.technology_id is not None or self.engine_object_sha256 is not None:
            raise ValueError("WeBWorK seed items do not use an engine object ID")
        return self


class EngineProbeReceipt(BaseModel):
    schema_version: Literal["build08-engine-probe-v1"] = "build08-engine-probe-v1"
    run_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=200)
    item_type: Literal[AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS]
    seed: int = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    endpoint_host: str = Field(min_length=1, max_length=253)
    engine_image_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    network_isolation_attestation_sha256: str = Field(pattern=SHA256_PATTERN)
    adapter_image_sha256: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    engine_object_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    object_idempotent_observed: bool | None = None
    runtime_values_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    semantic_render_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    deterministic: bool
    constraints_satisfied: bool
    rendered: bool
    render_duration_ms: int = Field(ge=0, le=120_000)
    warning_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    expected_answer_accepted: bool
    wrong_answer_rejected: bool
    expected_score: float | None = Field(default=None, ge=0, le=1)
    wrong_score: float | None = Field(default=None, ge=0, le=1)
    failure_stage: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    final_receipt_ready: Literal[False] = False
    remaining_checks: list[
        Literal["persisted_grade_match", "object_idempotent", "cross_owner_access_blocked"]
    ] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_remaining_checks(self) -> "EngineProbeReceipt":
        required = {
            "persisted_grade_match",
            "object_idempotent",
            "cross_owner_access_blocked",
        }
        if set(self.remaining_checks) != required:
            raise ValueError("engine probes must retain all final receipt checks")
        return self


class OutageReceipt(BaseModel):
    schema_version: Literal["build08-outage-v1"] = "build08-outage-v1"
    run_id: str = Field(min_length=1, max_length=100)
    boundary: OutageBoundary
    injection_method: str = Field(pattern=r"^[a-z0-9_]{3,80}$")
    failure_code: str = Field(pattern=r"^[a-z0-9_]{3,100}$")
    outcome: OutageOutcome
    retry_safe: bool
    terminal_safe: bool
    no_partial_publication: bool
    adapt_core_ready: bool
    recovered: bool
    secrets_redacted: bool
    advanced_flags_false: bool
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    platform_state_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_outcome(self) -> "OutageReceipt":
        if self.retry_safe == self.terminal_safe:
            raise ValueError("exactly one safe outage outcome must be selected")
        expected = (
            OutageOutcome.RETRYABLE if self.retry_safe else OutageOutcome.TERMINAL
        )
        if self.outcome != expected:
            raise ValueError("outage outcome does not match its safety attestation")
        return self


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
