from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from .computation import ComputationProfile
from .math_text import (
    validate_critique_math,
    validate_hint_math,
    validate_question_math,
)


class Paragraph(BaseModel):
    index: int = Field(ge=0)
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SourceLicenseMetadata(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    version: str | None = Field(default=None, max_length=20)
    label: str = Field(min_length=1, max_length=120)
    evidence_url: str = Field(min_length=1, max_length=4_096)


class SourceInfo(BaseModel):
    backend: str = "cxone_sandbox"
    canonical_url: str
    path: str
    page_id: str | None = None
    license: SourceLicenseMetadata | None = None


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


class AssessmentItemType(StrEnum):
    MULTIPLE_CHOICE = "multiple_choice"
    TRUE_FALSE = "true_false"
    NUMERICAL = "numerical"
    MULTIPLE_RESPONSE = "multiple_response"
    SELECT_ALL = "select_all"
    SELECT_N = "select_n"
    FILL_IN_BLANK = "fill_in_blank"
    SELECT_CHOICE = "select_choice"
    MATCHING = "matching"
    ORDERING = "ordering"
    DRAG_DROP_CLOZE = "drag_drop_cloze"
    IMAGE_HOTSPOT = "image_hotspot"
    HIGHLIGHT_TEXT = "highlight_text"
    HIGHLIGHT_TABLE = "highlight_table"
    MATRIX = "matrix"
    DROPDOWN = "dropdown"
    BOW_TIE = "bow_tie"
    WEBWORK = "webwork"
    IMATHAS = "imathas"


class ItemContextType(StrEnum):
    STANDARD = "standard"
    SCENARIO = "scenario"
    CASE = "case"
    SHARED_STIMULUS = "shared_stimulus"
    DIFFICULTY_VARIANT = "difficulty_variant"


class Choice(BaseModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    text: str = Field(min_length=1, max_length=1_000)
    correct: bool = False
    feedback: str | None = Field(default=None, max_length=1_000)

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class MatchingPair(BaseModel):
    prompt_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    prompt: str = Field(min_length=1, max_length=1_000)
    target_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    target: str = Field(min_length=1, max_length=1_000)

    @field_validator("prompt_id", "target_id", mode="before")
    @classmethod
    def normalize_ids(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class ClozeBlank(BaseModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    correct: list[str] = Field(min_length=1, max_length=12)
    options: list[str] = Field(default_factory=list, max_length=20)
    case_sensitive: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class HotspotRegion(BaseModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    label: str = Field(min_length=1, max_length=500)
    shape: Literal["rectangle", "polygon"]
    coordinates: list[Annotated[float, Field(ge=0, le=1)]] = Field(
        min_length=4, max_length=40
    )
    correct: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_coordinates(self) -> "HotspotRegion":
        if any(value < 0 or value > 1 for value in self.coordinates):
            raise ValueError("hotspot coordinates must be normalized from 0 to 1")
        if self.shape == "rectangle" and len(self.coordinates) != 4:
            raise ValueError("rectangle hotspots require x, y, width, and height")
        if self.shape == "polygon" and (
            len(self.coordinates) < 6 or len(self.coordinates) % 2
        ):
            raise ValueError("polygon hotspots require at least three x/y points")
        return self


class HighlightSegment(BaseModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    text: str = Field(min_length=1, max_length=2_000)
    correct: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class MatrixRow(BaseModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    text: str = Field(min_length=1, max_length=1_000)
    correct_column_ids: list[str] = Field(min_length=1, max_length=12)

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("correct_column_ids", mode="before")
    @classmethod
    def normalize_column_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [item.upper() if isinstance(item, str) else item for item in value]


class BowTieGroup(BaseModel):
    choices: list[Choice] = Field(min_length=2, max_length=12)
    required_selections: int = Field(default=1, ge=1, le=6)

    @model_validator(mode="after")
    def validate_group(self) -> "BowTieGroup":
        ids = [choice.id for choice in self.choices]
        if len(ids) != len(set(ids)):
            raise ValueError("bow-tie choice ids must be unique within a group")
        if sum(choice.correct for choice in self.choices) != self.required_selections:
            raise ValueError("bow-tie correct choices must match required selections")
        return self


class ParameterVariable(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    minimum: float
    maximum: float
    step: float = Field(default=1, gt=0)
    integer: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> "ParameterVariable":
        if self.maximum <= self.minimum:
            raise ValueError("parameter maximum must be greater than minimum")
        return self


class ParameterizedItemSpec(BaseModel):
    engine: Literal["webwork", "imathas"]
    # Keep the accepted provider-facing schema at minItems=1 while permitting
    # the server-owned formula profile to contain only a learner response
    # symbol. The validator below still rejects empty numeric parameter lists.
    variables: (
        Annotated[
            list[ParameterVariable],
            Field(min_length=1, max_length=12),
        ]
        | SkipJsonSchema[
            Annotated[
                list[ParameterVariable],
                Field(max_length=12),
            ]
        ]
    )
    prompt_template: str = Field(min_length=5, max_length=4_000)
    answer_expression: str = Field(min_length=1, max_length=1_000)
    answer_kind: SkipJsonSchema[Literal["numeric", "formula"]] = Field(
        default="numeric",
        exclude_if=lambda value: value == "numeric",
    )
    compiler_profile: SkipJsonSchema[Literal["legacy", "assessment_computation_v0"]] = (
        Field(default="legacy", exclude_if=lambda value: value == "legacy")
    )
    response_symbols: SkipJsonSchema[
        list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]]
    ] = Field(
        default_factory=list,
        max_length=4,
        exclude_if=lambda value: not value,
    )
    explanation_template: str = Field(min_length=5, max_length=4_000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    tolerance: float = Field(default=0, ge=0)
    units: str | None = Field(default=None, max_length=100)
    seed_policy: Literal["per_student", "per_attempt"] = "per_student"

    @model_validator(mode="after")
    def validate_response_symbols(self) -> "ParameterizedItemSpec":
        response_symbols = set(self.response_symbols)
        if len(response_symbols) != len(self.response_symbols):
            raise ValueError("response symbols must be unique")
        parameter_names = {variable.name for variable in self.variables}
        if response_symbols & parameter_names:
            raise ValueError(
                "response symbols must be disjoint from parameter variables"
            )
        if self.answer_kind == "formula" and not response_symbols:
            raise ValueError("formula answers require at least one response symbol")
        if (
            self.answer_kind == "formula"
            and self.compiler_profile != "assessment_computation_v0"
        ):
            raise ValueError(
                "formula answers require the assessment computation compiler profile"
            )
        if self.answer_kind == "numeric" and response_symbols:
            raise ValueError("numeric answers cannot declare response symbols")
        if self.answer_kind == "numeric" and not self.variables:
            raise ValueError("numeric parameterized answers require a variable")
        return self


class ItemResponse(BaseModel):
    correct_order: list[str] = Field(default_factory=list, max_length=20)
    matching_pairs: list[MatchingPair] = Field(default_factory=list, max_length=20)
    blanks: list[ClozeBlank] = Field(default_factory=list, max_length=20)
    numeric_answer: float | None = None
    numeric_tolerance: float = Field(default=0, ge=0)
    select_n: int | None = Field(default=None, ge=1, le=20)
    image_url: str | None = Field(default=None, max_length=4_096)
    image_alt: str | None = Field(default=None, max_length=1_000)
    hotspot_regions: list[HotspotRegion] = Field(default_factory=list, max_length=30)
    highlight_segments: list[HighlightSegment] = Field(
        default_factory=list, max_length=50
    )
    matrix_columns: list[Choice] = Field(default_factory=list, max_length=12)
    matrix_rows: list[MatrixRow] = Field(default_factory=list, max_length=30)
    bow_tie_actions: BowTieGroup | None = None
    bow_tie_condition: BowTieGroup | None = None
    bow_tie_parameters: BowTieGroup | None = None
    parameterized: ParameterizedItemSpec | None = None

    @field_validator("correct_order", mode="before")
    @classmethod
    def normalize_correct_order_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [item.upper() if isinstance(item, str) else item for item in value]

    @field_validator("blanks", mode="before")
    @classmethod
    def normalize_blank_shorthand(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        for index, blank in enumerate(value, start=1):
            if isinstance(blank, str):
                normalized.append({"id": f"B{index}", "correct": [blank]})
            else:
                normalized.append(blank)
        return normalized


class HintRungType(StrEnum):
    CONCEPTUAL = "conceptual"
    STRATEGIC = "strategic"
    SPECIFIC = "specific"


class HintRungDraft(BaseModel):
    rung: HintRungType
    text: str = Field(min_length=5, max_length=2_000)
    citation_paragraphs: list[int] = Field(min_length=1, max_length=20)
    answer_leak_detected: bool = False


class HintLadderDraft(BaseModel):
    concept_label: str = Field(min_length=2, max_length=160)
    rungs: list[HintRungDraft] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_rungs(self) -> "HintLadderDraft":
        expected = [
            HintRungType.CONCEPTUAL,
            HintRungType.STRATEGIC,
            HintRungType.SPECIFIC,
        ]
        if [rung.rung for rung in self.rungs] != expected:
            raise ValueError("hint rungs must be conceptual, strategic, then specific")
        return self


class GeneratedHintLadderDraft(HintLadderDraft):
    """Provider response contract; stored reviewer ladders may retain leak flags."""

    @model_validator(mode="after")
    def reject_self_reported_leaks(self) -> "GeneratedHintLadderDraft":
        leaking = [rung.rung.value for rung in self.rungs if rung.answer_leak_detected]
        if leaking:
            raise ValueError(
                "generated hint ladder self-reports answer leakage in: "
                + ", ".join(leaking)
            )
        validate_hint_math(self)
        return self


class QuestionDraft(BaseModel):
    schema_version: Literal["assessment-item-v2"] = "assessment-item-v2"
    item_type: AssessmentItemType = AssessmentItemType.MULTIPLE_CHOICE
    context_type: ItemContextType = ItemContextType.STANDARD
    concept_label: str = Field(min_length=2, max_length=160)
    stem: str = Field(min_length=5, max_length=2_000)
    stimulus: str | None = Field(default=None, max_length=8_000)
    set_key: str | None = Field(default=None, max_length=100)
    choices: list[Choice] = Field(default_factory=list, max_length=30)
    response: ItemResponse = Field(default_factory=ItemResponse)
    explanation: str = Field(min_length=5, max_length=4_000)
    bloom: BloomLevel
    difficulty: Difficulty
    citation_paragraphs: list[int] = Field(min_length=1)
    needs_human_verification: bool = False
    specialist_review_required: bool = False
    targeted_misconception: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="before")
    @classmethod
    def infer_missing_select_n(cls, value: object) -> object:
        if not isinstance(value, dict) or value.get("item_type") not in {
            AssessmentItemType.SELECT_N,
            AssessmentItemType.SELECT_N.value,
        }:
            return value
        correct_count = sum(
            choice.correct
            if isinstance(choice, Choice)
            else choice.get("correct") is True
            for choice in value.get("choices", [])
            if isinstance(choice, (Choice, dict))
        )
        if correct_count < 1:
            return value
        response = value.get("response")
        if response is None:
            response = {}
        if isinstance(response, ItemResponse):
            if response.select_n is not None:
                return value
            response = response.model_copy(update={"select_n": correct_count})
        elif isinstance(response, dict):
            if response.get("select_n") is not None:
                return value
            response = {**response, "select_n": correct_count}
        else:
            return value
        return {**value, "response": response}

    @model_validator(mode="after")
    def validate_item(self) -> "QuestionDraft":
        if not _xml_text_tree_is_valid(self.model_dump(mode="python")):
            raise ValueError("question text contains XML-forbidden control characters")
        ids = [choice.id for choice in self.choices]
        if len(ids) != len(set(ids)):
            raise ValueError("choice ids must be unique")
        correct_count = sum(choice.correct for choice in self.choices)
        single_choice = {
            AssessmentItemType.MULTIPLE_CHOICE,
            AssessmentItemType.TRUE_FALSE,
            AssessmentItemType.SELECT_CHOICE,
            AssessmentItemType.DROPDOWN,
        }
        multiple_choice = {
            AssessmentItemType.MULTIPLE_RESPONSE,
            AssessmentItemType.SELECT_ALL,
            AssessmentItemType.SELECT_N,
        }
        if self.item_type == AssessmentItemType.MULTIPLE_CHOICE and len(ids) != 4:
            raise ValueError("multiple-choice drafts require exactly four choices")
        if self.item_type == AssessmentItemType.TRUE_FALSE and len(ids) != 2:
            raise ValueError("true/false drafts require exactly two choices")
        if self.item_type in single_choice and correct_count != 1:
            raise ValueError("single-choice drafts require exactly one correct choice")
        if self.item_type in multiple_choice:
            if len(ids) < 2 or correct_count < 1:
                raise ValueError("multiple-response drafts require correct choices")
            if self.item_type == AssessmentItemType.SELECT_N:
                if self.response.select_n is None:
                    raise ValueError("select-N drafts require select_n")
                if self.response.select_n != correct_count:
                    raise ValueError(
                        "select_n must equal the number of correct choices"
                    )
        if self.item_type == AssessmentItemType.ORDERING:
            if len(ids) < 3 or set(self.response.correct_order) != set(ids):
                raise ValueError(
                    "ordering drafts require an order containing every choice"
                )
        if (
            self.item_type == AssessmentItemType.MATCHING
            and len(self.response.matching_pairs) < 2
        ):
            raise ValueError("matching drafts require at least two pairs")
        if (
            self.item_type
            in {
                AssessmentItemType.FILL_IN_BLANK,
                AssessmentItemType.DRAG_DROP_CLOZE,
            }
            and not self.response.blanks
        ):
            raise ValueError("blank and cloze drafts require response blanks")
        if (
            self.item_type == AssessmentItemType.NUMERICAL
            and self.response.numeric_answer is None
        ):
            raise ValueError("numerical drafts require a numeric answer")
        if self.item_type == AssessmentItemType.IMAGE_HOTSPOT:
            if not self.response.image_url or not self.response.image_alt:
                raise ValueError("hotspot drafts require an image URL and alt text")
            if not any(region.correct for region in self.response.hotspot_regions):
                raise ValueError("hotspot drafts require at least one correct region")
        if self.item_type in {
            AssessmentItemType.HIGHLIGHT_TEXT,
            AssessmentItemType.HIGHLIGHT_TABLE,
        } and not any(segment.correct for segment in self.response.highlight_segments):
            raise ValueError("highlight drafts require at least one correct segment")
        if self.item_type == AssessmentItemType.MATRIX:
            column_ids = {choice.id for choice in self.response.matrix_columns}
            if len(column_ids) < 2 or not self.response.matrix_rows:
                raise ValueError("matrix drafts require rows and at least two columns")
            if any(
                not set(row.correct_column_ids).issubset(column_ids)
                for row in self.response.matrix_rows
            ):
                raise ValueError("matrix rows reference unknown columns")
        if self.item_type == AssessmentItemType.BOW_TIE and not all(
            (
                self.response.bow_tie_actions,
                self.response.bow_tie_condition,
                self.response.bow_tie_parameters,
            )
        ):
            raise ValueError(
                "bow-tie drafts require actions, condition, and parameters"
            )
        if self.item_type in {
            AssessmentItemType.WEBWORK,
            AssessmentItemType.IMATHAS,
        }:
            parameterized = self.response.parameterized
            if parameterized is None or parameterized.engine != self.item_type.value:
                raise ValueError("parameterized drafts require a matching engine spec")
            if parameterized.compiler_profile == "legacy":
                # Keep the accepted provider retry boundary aligned with the
                # publication boundary for BUILD-08 items. Computation-owned
                # specs are deliberately not reparsed here: their persisted
                # strings are display metadata, while source and previews are
                # compiled only from the owning typed blueprint.
                from .parameterized import compile_parameterized_item

                compile_parameterized_item(parameterized, validation_seeds=25)
        if (
            self.context_type
            in {
                ItemContextType.SCENARIO,
                ItemContextType.CASE,
                ItemContextType.SHARED_STIMULUS,
            }
            and not self.stimulus
        ):
            raise ValueError("scenario and shared-stimulus items require stimulus text")
        if self.context_type == ItemContextType.SHARED_STIMULUS and not self.set_key:
            raise ValueError("shared-stimulus items require a set key")
        return self


class GeneratedQuestionDraft(QuestionDraft):
    """Strict provider contract; stored legacy reviewer drafts remain loadable."""

    @model_validator(mode="after")
    def require_canonical_math(self) -> "GeneratedQuestionDraft":
        validate_question_math(self)
        return self


COMPUTATION_TASK_SLOT = "[[computed_task]]"
COMPUTATION_RESULT_SLOT = "[[computed_result]]"


class ComputationQuestionDraft(QuestionDraft):
    """Provider prose contract for a computation-owned assessment draft.

    The provider can author the source-grounded framing and pedagogy, but it
    must leave the two answer-bearing spans as typed server slots.  The
    computation workflow replaces those slots only after the typed result has
    been frozen.
    """

    @model_validator(mode="after")
    def require_server_owned_computation_slots(self) -> "ComputationQuestionDraft":
        if self.stem.count(COMPUTATION_TASK_SLOT) != 1:
            raise ValueError(
                f"computation stem must contain exactly one {COMPUTATION_TASK_SLOT} slot"
            )
        if self.explanation.count(COMPUTATION_RESULT_SLOT) != 1:
            raise ValueError(
                "computation explanation must contain exactly one "
                f"{COMPUTATION_RESULT_SLOT} slot"
            )
        return self


class GeneratedComputationQuestionDraft(ComputationQuestionDraft):
    """Computation prose slots plus the strict human-facing math contract."""

    @model_validator(mode="after")
    def require_canonical_math(self) -> "GeneratedComputationQuestionDraft":
        validate_question_math(self)
        return self


def _xml_text_tree_is_valid(value: object) -> bool:
    if isinstance(value, str):
        return all(
            code in {0x9, 0xA, 0xD}
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
            for code in map(ord, value)
        )
    if isinstance(value, dict):
        return all(
            _xml_text_tree_is_valid(key) and _xml_text_tree_is_valid(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_xml_text_tree_is_valid(item) for item in value)
    return True


class Critique(BaseModel):
    issues: list[str] = Field(default_factory=list, max_length=12)
    distractor_flags: list[str] = Field(default_factory=list, max_length=12)
    revision_instructions: list[str] = Field(default_factory=list, max_length=12)
    revision_required: bool


class GeneratedCritique(Critique):
    @model_validator(mode="after")
    def require_canonical_math(self) -> "GeneratedCritique":
        validate_critique_math(self)
        return self


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
    generation_mode: Literal["auto", "selected"] = "auto"
    item_types: list[AssessmentItemType] = Field(default_factory=list, max_length=8)
    item_count: int = Field(default=4, ge=1, le=8)
    include_hint_ladder: bool = True
    computation_profile: ComputationProfile | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_generation_selection(self) -> "GenerateRequest":
        if self.generation_mode == "selected" and not self.item_types:
            raise ValueError("choose at least one item type")
        if len(self.item_types) != len(set(self.item_types)):
            raise ValueError("item type selections must not contain duplicates")
        if self.generation_mode == "selected" and self.item_count < len(
            self.item_types
        ):
            raise ValueError(
                "total item count must be at least the number of selected item types"
            )
        return self


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
