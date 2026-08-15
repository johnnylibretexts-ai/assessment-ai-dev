from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import signal
import stat
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from xml.etree import ElementTree

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

from app.computation import (
    UCUM_ESSENCE_SHA256,
    UCUM_PROFILE,
    AssessmentComputationBlueprint,
    AssessmentValidationReport,
    ComputationValidationRequest,
    ComputationResult,
    ExpressionNode,
    ValidationStatus,
    canonical_blueprint_hash,
    compute_blueprint as _compute_blueprint_core,
    deterministic_seeds,
    validate_computation as _validate_computation_core,
    validate_unit_code,
)
from app.computation_client import (
    AssessmentComputationClient,
    ComputationClient,
)
from app.computation_workflow import (
    parameterized_spec_from_blueprint,
    parameterized_typed_inputs,
)
from app.parameterized import (
    ParameterizedCompileError,
    compile_parameterized_item,
    compile_typed_algebra_qualification_item,
    compile_typed_parameterized_item,
    parameterized_constraints_satisfied,
)
from app.schemas import ParameterVariable, ParameterizedItemSpec

from .adapt_seed import finalize_seed_receipts
from .fixtures import build_seed_plan
from .models import AdaptSeedAttestation, EngineProbeReceipt, SeedReceipt
from .validators import validate_seed_receipts


CATALOG_PATH = Path(__file__).with_name("computation_fixtures.json")
CATALOG_SCHEMA_VERSION = "assessment-computation-fixture-catalog-v0"
MANIFEST_SCHEMA_VERSION = "assessment-computation-evaluation-v0"
VALIDATOR_REVISION = "assessment-computation-evaluation-v0"
SEEDS_PER_ENGINE_ITEM = 100
ITEMS_PER_ENGINE = 20
EXECUTIONS_PER_PLAN = 4_000
TOTAL_PLANNED_ENGINE_EXECUTIONS = 8_000
FORMULA_QUALIFICATION_OPERATIONS = (
    "substitute",
    "expand",
    "factor",
    "equivalent",
)
MAX_RECEIPT_LEDGER_BYTES = 32 * 1024 * 1024
MAX_RECEIPT_LINE_BYTES = 256 * 1024
MAX_UCUM_ARTIFACT_BYTES = 1024 * 1024
MAX_EVIDENCE_LEDGER_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_LINE_BYTES = 512 * 1024
MAX_CANARY_STAGE_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_CANARY_COMMAND_OUTPUT_BYTES = 64 * 1024
ACCEPTED_BUILD08_BASE_COMMIT = "8497aad448d18c49d967480134eff9f80a444bd0"
ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST = (
    "sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed"
)
CANARY_LOCAL_EXECUTOR_REVISION = "assessment-computation-local-canary-executor-v0"
CANARY_DISPOSABLE_MARKER = "assessment-computation-canary-disposable-v0\n"
OFFLINE_MUTATION_RUNNER_VERSION = "assessment-computation-mutation-runner-v0"
WORKFLOW_POSITIVE_RUNNER_VERSION = "assessment-computation-workflow-runner-v0"
NATIVE_RECEIPT_SCHEMA_VERSION = "assessment-computation-native-receipt-v0"
NATIVE_EXECUTION_PROTOCOL_VERSION = "assessment-computation-native-execution-v0"
ALGEBRA_NATIVE_RECEIPT_SCHEMA_VERSION = (
    "assessment-computation-algebra-native-receipt-v0"
)
UCUM_QUALIFICATION_SCHEMA_VERSION = "assessment-computation-ucum-qualification-v0"
PINNED_UCUM_FUNCTIONAL_TEST_SHA256 = (
    "3b3feb9d8ecfe8958da69b1afcd25572e3f8d80a36bfcbcda4834a10a0eeef0d"
)
PINNED_UCUM_FUNCTIONAL_TEST_BYTES = 36_782
PINNED_UCUM_FUNCTIONAL_TEST_SOURCE = "FHIR-Ucum-java-e7d054e6776935e3"
PINNED_UCUM_SECTION_COUNTS = {
    "validation": 529,
    "displaynamegeneration": 9,
    "conversion": 30,
    "multiplication": 2,
    "division": 3,
}
PINNED_UCUM_SUBSET_CASES = 190
PINNED_UCUM_SUBSET_CASE_KIND_COUNTS = {
    "validation": 173,
    "conversion": 17,
}
UCUM_SELECTION_REVISION = "libretexts-edu-units-v0-selection-2026-07-23"
UCUMVERT_GRAMMAR_SHA256 = (
    "101165c3b2d2c8cfaf394dbc159889cadafdaa8e044ae0a52cfa4400217fdeed"
)
PINT_UCUM_DEFINITIONS_SHA256 = (
    "cc64e39ec183e2b28dbd1ec462eec7081c3f4c3cbdd9705501332f18b653024a"
)
UCUMVERT_WHEEL_SHA256 = (
    "b1c3c5b875843642f52f5f015fc8728ff997ce9c337b64bd25141ae44848bdd0"
)
PINT_WHEEL_SHA256 = "27eb25143bd5de9fcc4d5a4b484f16faf6b4615aa93ece6b3373a8c1a3c1b97d"


@dataclass(frozen=True)
class SpikeQualificationPromotion:
    """Code-reviewed promotion of one exact, externally audited evidence bundle."""

    evidence_bundle_sha256: str
    manifest_sha256: str
    candidate_image_digest: str
    approval_record_sha256: str


# Evidence hashes prove consistency, not who observed the evidence. Reports must
# never promote themselves; an exact reviewed bundle is added here separately.
QUALIFIED_SPIKE_PROMOTIONS: Mapping[str, SpikeQualificationPromotion] = (
    MappingProxyType({})
)

# This is deliberately the named LibreTexts subset, not a claim that every UCUM
# code is implemented. Keep it independent of private implementation constants so
# an evaluation report states exactly what it attempted to qualify.
LIBRETEXTS_UCUM_CODES = frozenset(
    {
        "1",
        "%",
        "m",
        "cm",
        "mm",
        "km",
        "s",
        "ms",
        "min",
        "h",
        "g",
        "mg",
        "kg",
        "mol",
        "A",
        "K",
        "cd",
        "rad",
        "deg",
        "Hz",
        "N",
        "Pa",
        "kPa",
        "J",
        "kJ",
        "W",
        "kW",
        "C",
        "V",
        "Ohm",
        "L",
        "mL",
    }
)
_MANIFEST_JSON_CACHE: dict[tuple[str, int, int], str] = {}


def compute_blueprint(
    blueprint: AssessmentComputationBlueprint,
):
    """Load the compute-only runtime only for evaluation paths that execute it."""

    from app.computation_runtime import bind_runtime_dependencies

    bind_runtime_dependencies()
    return _compute_blueprint_core(blueprint)


def validate_computation(request: ComputationValidationRequest):
    """Load the compute-only runtime only for evaluation paths that execute it."""

    from app.computation_runtime import bind_runtime_dependencies

    bind_runtime_dependencies()
    return _validate_computation_core(request)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FixtureDifficulty(StrEnum):
    BASIC = "basic"
    INTERMEDIATE = "intermediate"
    BOUNDARY = "boundary"


class FixtureSplit(StrEnum):
    DEVELOPMENT = "development"
    SEALED = "sealed"


class EvaluationSurface(StrEnum):
    NUMERIC = "numeric"
    ALGEBRAIC = "algebraic"
    UNIT = "unit"
    WEBWORK = "webwork"
    IMATHAS = "imathas"


class MutationKind(StrEnum):
    POSITIVE = "positive"
    SEMANTIC = "semantic"
    SAFETY_BOUNDARY = "safety_boundary"


class ObservedValidationState(StrEnum):
    VALIDATED = "validated"
    PARTIALLY_VALIDATED = "partially_validated"
    UNSUPPORTED = "unsupported"
    VALIDATION_FAILED = "validation_failed"


class FixtureProvenance(StrictModel):
    provenance_id: Literal["libretexts-synthetic-computation-v0"]
    origin: Literal["independently_authored_synthetic"]
    rights_basis: Literal["MIT"]
    rights_statement: str = Field(min_length=20, max_length=500)
    contains_learner_data: Literal[False]
    contains_proprietary_data: Literal[False]
    contains_wolfram_data: Literal[False]
    wolfram_api_used: Literal[False]
    expert_approved: Literal[False]
    review_status: Literal["awaiting_sme_review"]
    prohibited_sources: list[str] = Field(min_length=4, max_length=20)

    @model_validator(mode="after")
    def validate_prohibited_sources(self) -> "FixtureProvenance":
        normalized = " ".join(self.prohibited_sources).lower()
        if "wolfram" not in normalized or "learner" not in normalized:
            raise ValueError(
                "provenance must explicitly prohibit Wolfram and learner data"
            )
        return self


class ComputationFixture(StrictModel):
    fixture_id: str = Field(pattern=r"^acv0-(numeric|algebraic|unit)-[0-9]{2}$")
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    family: Literal["numeric", "algebraic", "unit"]
    difficulty: FixtureDifficulty
    split: FixtureSplit
    provenance_id: Literal["libretexts-synthetic-computation-v0"]
    review_status: Literal["awaiting_sme_review"]
    blueprint: AssessmentComputationBlueprint
    expected_exact: str | None = Field(default=None, max_length=500)
    expected_solutions: list[str] = Field(default_factory=list, max_length=4)
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_fixture_hash(self) -> "ComputationFixture":
        _require_matching_hash(self, "fixture_sha256")
        return self


class ParameterizedLineage(StrictModel):
    lineage_id: str = Field(pattern=r"^acv0-lineage-[0-9]{2}$")
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    difficulty: FixtureDifficulty
    split: FixtureSplit
    provenance_id: Literal["libretexts-synthetic-computation-v0"]
    review_status: Literal["awaiting_sme_review"]
    expression: ExpressionNode
    constraints: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    variable_specs: list[ParameterVariable] = Field(min_length=1, max_length=12)
    prompt_template: str = Field(min_length=5, max_length=4_000)
    explanation_template: str = Field(min_length=5, max_length=4_000)
    tolerance: float = Field(ge=0)
    units: str | None = Field(default=None, max_length=100)
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_lineage_hash(self) -> "ParameterizedLineage":
        _require_matching_hash(self, "lineage_sha256")
        return self


class EngineTwinFixture(StrictModel):
    fixture_id: str = Field(pattern=r"^acv0-(webwork|imathas)-lineage-[0-9]{2}$")
    lineage_id: str = Field(pattern=r"^acv0-lineage-[0-9]{2}$")
    engine: Literal["webwork", "imathas"]
    difficulty: FixtureDifficulty
    split: FixtureSplit
    answer_kind: Literal["numeric"]
    parameterized_spec: ParameterizedItemSpec
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_status: Literal["planned_native_execution"]
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_fixture_hash(self) -> "EngineTwinFixture":
        _require_matching_hash(self, "fixture_sha256")
        return self


class FormulaQualificationCase(StrictModel):
    case_id: str = Field(
        pattern=(
            r"^acv0-formula-(webwork|imathas)-"
            r"(substitute|expand|factor|equivalent)$"
        )
    )
    engine: Literal["webwork", "imathas"]
    operation: Literal["substitute", "expand", "factor", "equivalent"]
    expression: ExpressionNode
    comparison_expression: ExpressionNode
    status: Literal["qualification_pending", "expected_unsupported"]
    reason: str = Field(min_length=10, max_length=500)
    native_receipts_present: Literal[False]


class AlgebraNativePlanCase(StrictModel):
    schema_version: Literal["assessment-computation-algebra-native-plan-v0"] = (
        "assessment-computation-algebra-native-plan-v0"
    )
    plan_id: str = Field(pattern=r"^acv0-algebra-(webwork|imathas)-[0-9]{2}$")
    fixture_id: str = Field(pattern=r"^acv0-algebraic-[0-9]{2}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: Literal["webwork", "imathas"]
    operation: Literal["substitute", "expand", "factor", "equivalent", "solve"]
    answer_kind: Literal["numeric", "formula", "solution_set"]
    source_contract: Literal[
        "production_typed_ast_v0",
        "qualification_only_solution_set_v0",
    ]
    coverage_scope: Literal["learner_delivery_native_qualification"]
    production_delivery_eligible: Literal[False]
    applicability: Literal["fixed_template_native_receipt_required"]
    compiler_version: Literal[
        "assessment-computation-typed-ast-v0",
        "assessment-computation-algebra-native-qualification-v0",
    ]
    source: str = Field(min_length=20, max_length=20_000)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_symbols: list[str] = Field(default_factory=list, max_length=1)
    correct_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alternate_correct_submission_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    wrong_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_status: Literal["planned_not_executed"]
    native_receipts_present: Literal[False]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> "AlgebraNativePlanCase":
        if (
            hashlib.sha256(self.source.encode("utf-8")).hexdigest()
            != self.source_sha256
        ):
            raise ValueError("algebra native source hash does not match source")
        if self.answer_kind == "numeric":
            if self.response_symbols or self.alternate_correct_submission_sha256:
                raise ValueError(
                    "numeric algebra plans cannot declare symbolic response evidence"
                )
        elif len(self.response_symbols) != 1:
            raise ValueError(
                "formula and solution-set plans require one response symbol"
            )
        if self.answer_kind in {"formula", "solution_set"} and (
            self.alternate_correct_submission_sha256 is None
        ):
            raise ValueError(
                "symbolic algebra plans require an alternate correct submission"
            )
        expected_compiler = (
            "assessment-computation-algebra-native-qualification-v0"
            if self.answer_kind == "solution_set"
            else "assessment-computation-typed-ast-v0"
        )
        if self.compiler_version != expected_compiler:
            raise ValueError(
                "only solution-set plans may use the qualification-only compiler"
            )
        expected_source_contract = (
            "qualification_only_solution_set_v0"
            if self.answer_kind == "solution_set"
            else "production_typed_ast_v0"
        )
        if self.source_contract != expected_source_contract:
            raise ValueError("algebra plan source contract does not match answer kind")
        _require_matching_hash(self, "plan_sha256")
        return self


class MutationFixture(StrictModel):
    mutation_id: str = Field(
        pattern=(
            r"^acv0-(numeric|algebraic|unit|webwork|imathas)-"
            r"[a-z0-9_-]+-(semantic|safety)$"
        )
    )
    parent_fixture_id: str = Field(min_length=5, max_length=100)
    surface: EvaluationSurface
    split: FixtureSplit
    kind: Literal[MutationKind.SEMANTIC, MutationKind.SAFETY_BOUNDARY]
    critical: Literal[True]
    transform: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    expected_states: list[ObservedValidationState] = Field(min_length=1, max_length=2)
    materialization_revision: Literal[
        "assessment-computation-mutation-materialization-v0"
    ] = "assessment-computation-mutation-materialization-v0"
    expected_mutated_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_status: Literal["awaiting_sme_review"]
    mutation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_mutation_hash(self) -> "MutationFixture":
        _require_matching_hash(self, "mutation_sha256")
        return self


class EngineSeedPlanCase(StrictModel):
    schema_version: Literal["assessment-computation-engine-seed-plan-v0"] = (
        "assessment-computation-engine-seed-plan-v0"
    )
    run_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(pattern=r"^acv0-(webwork|imathas)-lineage-[0-9]{2}$")
    lineage_id: str = Field(pattern=r"^acv0-lineage-[0-9]{2}$")
    engine: Literal["webwork", "imathas"]
    split: FixtureSplit
    seed: int = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_oracle: Literal["native_engine"]
    expected_answer_source: Literal["engine_observed_parameters"]
    execution_status: Literal["planned_not_executed"]


class EngineSeedPlanSummary(StrictModel):
    plan_id: Literal["build08-compatibility", "assessment-computation-v0"]
    purpose: Literal["backward_compatibility", "typed_lineage_qualification"]
    item_count: Literal[40]
    items_per_engine: Literal[20]
    seeds_per_item: Literal[100]
    planned_executions: Literal[4_000]
    execution_status: Literal["planned_not_executed"]
    runtime_oracle: Literal["native_engine"]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ComputationEvaluationManifest(StrictModel):
    schema_version: Literal["assessment-computation-evaluation-v0"] = (
        MANIFEST_SCHEMA_VERSION
    )
    catalog_revision: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$")
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_revision: Literal["assessment-computation-evaluation-v0"] = (
        VALIDATOR_REVISION
    )
    provenance: FixtureProvenance
    computation_cases: list[ComputationFixture] = Field(min_length=60, max_length=60)
    parameterized_lineages: list[ParameterizedLineage] = Field(
        min_length=20, max_length=20
    )
    engine_twins: list[EngineTwinFixture] = Field(min_length=40, max_length=40)
    formula_qualification_cases: list[FormulaQualificationCase] = Field(
        min_length=8, max_length=8
    )
    algebra_native_plans: list[AlgebraNativePlanCase] = Field(
        min_length=40, max_length=40
    )
    mutations: list[MutationFixture] = Field(min_length=200, max_length=200)
    seed_plans: list[EngineSeedPlanSummary] = Field(min_length=2, max_length=2)
    total_positive_surfaces: Literal[100]
    total_mutations: Literal[200]
    total_planned_engine_executions: Literal[8_000]
    total_planned_algebra_native_receipts: Literal[40]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_matrix(self) -> "ComputationEvaluationManifest":
        case_ids = [case.fixture_id for case in self.computation_cases]
        twin_ids = [case.fixture_id for case in self.engine_twins]
        positive_ids = case_ids + twin_ids
        if len(positive_ids) != len(set(positive_ids)):
            raise ValueError("positive fixture identifiers must be unique")

        for family in ("numeric", "algebraic", "unit"):
            cases = [case for case in self.computation_cases if case.family == family]
            _validate_twenty_case_distribution(cases, family)
        _validate_twenty_case_distribution(
            self.parameterized_lineages, "parameterized lineages"
        )

        expected_formula_cases = {
            (engine, operation)
            for engine in ("webwork", "imathas")
            for operation in FORMULA_QUALIFICATION_OPERATIONS
        }
        observed_formula_cases = {
            (case.engine, case.operation) for case in self.formula_qualification_cases
        }
        if (
            len(self.formula_qualification_cases) != len(observed_formula_cases)
            or observed_formula_cases != expected_formula_cases
        ):
            raise ValueError(
                "legacy formula qualification cases must cover substitute, "
                "expand, factor, and equivalent exactly once per engine"
            )

        for engine in ("webwork", "imathas"):
            twins = [case for case in self.engine_twins if case.engine == engine]
            _validate_twenty_case_distribution(twins, engine)
            if {case.lineage_id for case in twins} != {
                lineage.lineage_id for lineage in self.parameterized_lineages
            }:
                raise ValueError(f"{engine} must contain every shared lineage")
            algebra_plans = [
                case for case in self.algebra_native_plans if case.engine == engine
            ]
            algebra_fixture_ids = {
                case.fixture_id
                for case in self.computation_cases
                if case.family == "algebraic"
            }
            if (
                len(algebra_plans) != 20
                or {case.fixture_id for case in algebra_plans} != algebra_fixture_ids
            ):
                raise ValueError(
                    f"{engine} algebra qualification plans must cover all 20 "
                    "typed algebra fixtures"
                )
            if Counter(case.answer_kind for case in algebra_plans) != {
                "numeric": 3,
                "formula": 11,
                "solution_set": 6,
            }:
                raise ValueError(
                    f"{engine} algebra qualification plans have the wrong "
                    "operation distribution"
                )
            formula_operations = {
                case.operation
                for case in algebra_plans
                if case.answer_kind == "formula"
            }
            if formula_operations != set(FORMULA_QUALIFICATION_OPERATIONS):
                raise ValueError(
                    f"{engine} formula qualification plans must cover substitute, "
                    "expand, factor, and equivalent; evaluate is not a formula "
                    "qualification operation"
                )
            if any(
                case.native_receipts_present
                or case.qualification_status != "planned_not_executed"
                or case.production_delivery_eligible
                for case in algebra_plans
            ):
                raise ValueError(
                    "algebra qualification plans cannot claim production support "
                    "or native evidence"
                )

        mutations_by_parent = Counter(case.parent_fixture_id for case in self.mutations)
        if set(mutations_by_parent) != set(positive_ids):
            raise ValueError("mutations must cover every positive surface")
        if any(count != 2 for count in mutations_by_parent.values()):
            raise ValueError("each positive surface requires exactly two mutations")
        for fixture_id in positive_ids:
            kinds = {
                mutation.kind
                for mutation in self.mutations
                if mutation.parent_fixture_id == fixture_id
            }
            if kinds != {MutationKind.SEMANTIC, MutationKind.SAFETY_BOUNDARY}:
                raise ValueError(
                    "every positive surface needs semantic and safety mutations"
                )

        if {plan.plan_id for plan in self.seed_plans} != {
            "build08-compatibility",
            "assessment-computation-v0",
        }:
            raise ValueError("both 4,000-execution plans are required")
        algebra_plan_ids = [plan.plan_id for plan in self.algebra_native_plans]
        if len(algebra_plan_ids) != len(set(algebra_plan_ids)):
            raise ValueError("algebra qualification plan identifiers must be unique")
        _require_matching_hash(self, "manifest_sha256")
        return self


class ComputationEvaluationObservation(StrictModel):
    case_id: str = Field(min_length=5, max_length=150)
    surface: EvaluationSurface
    kind: MutationKind
    state: ObservedValidationState
    oracle_match: bool
    critical_defect_detected: bool
    deterministic_replay: bool


class EngineExecutionSummary(StrictModel):
    build08_planned: Literal[4_000] = 4_000
    build08_executed: int = Field(ge=0, le=4_000)
    build08_passed: int = Field(ge=0, le=4_000)
    computation_planned: Literal[4_000] = 4_000
    computation_executed: int = Field(ge=0, le=4_000)
    computation_passed: int = Field(ge=0, le=4_000)

    @model_validator(mode="after")
    def validate_counts(self) -> "EngineExecutionSummary":
        if self.build08_passed > self.build08_executed:
            raise ValueError("BUILD-08 passes cannot exceed executions")
        if self.computation_passed > self.computation_executed:
            raise ValueError("computation passes cannot exceed executions")
        return self


class PairedStudySummary(StrictModel):
    control_count: int = Field(ge=0)
    treatment_count: int = Field(ge=0)
    treatment_correct_without_edit_rate: float = Field(ge=0, le=1)
    control_defect_rate: float = Field(ge=0, le=1)
    treatment_defect_rate: float = Field(ge=0, le=1)
    control_median_correction_seconds: float = Field(ge=0)
    treatment_median_correction_seconds: float = Field(ge=0)
    source_grounding_delta: float = Field(ge=-1, le=1)
    pedagogy_delta: float = Field(ge=-1, le=1)
    reviewer_approval_complete: bool


class AcceptanceMetrics(StrictModel):
    observation_count: int = Field(ge=0)
    positive_count: int = Field(ge=0)
    mutation_count: int = Field(ge=0)
    correct_validated_positive_count: int = Field(ge=0)
    materially_bad_validated_count: int = Field(ge=0)
    critical_mutations_detected: int = Field(ge=0)
    false_positive_defect_flags: int = Field(ge=0)
    supported_coverage: float = Field(ge=0, le=1)
    coverage_by_surface: dict[str, float]
    critical_mutation_recall: float = Field(ge=0, le=1)
    critical_mutation_precision: float = Field(ge=0, le=1)
    reproducibility_rate: float = Field(ge=0, le=1)
    fixture_gate_passed: bool
    engine_gate_passed: bool
    algebra_native_gate_passed: bool
    paired_study_gate_passed: bool
    sme_review_gate_passed: bool
    ucum_gate_passed: bool
    safety_gate_passed: bool
    canary_gate_passed: bool
    promotion_gate_passed: bool
    evidence_bound: bool
    evidence_bundle_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    spike_passed: bool
    failures: list[str]


class MutationDetectionLayer(StrEnum):
    TYPED_SCHEMA = "typed_schema"
    TYPED_VALIDATOR = "typed_validator"
    PARAMETERIZED_COMPILER = "parameterized_compiler"
    SEALED_HASH_BINDING = "sealed_hash_binding"


class OfflineMutationReceipt(StrictModel):
    schema_version: Literal["assessment-computation-mutation-receipt-v0"] = (
        "assessment-computation-mutation-receipt-v0"
    )
    runner_version: Literal["assessment-computation-mutation-runner-v0"] = (
        OFFLINE_MUTATION_RUNNER_VERSION
    )
    mutation_id: str = Field(min_length=5, max_length=150)
    mutation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_fixture_id: str = Field(min_length=5, max_length=100)
    parent_fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    surface: EvaluationSurface
    kind: Literal[MutationKind.SEMANTIC, MutationKind.SAFETY_BOUNDARY]
    transform: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    detection_layer: MutationDetectionLayer
    observed_state: ObservedValidationState
    outcome_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    detected: Literal[True]
    network_calls: Literal[0] = 0
    native_engine_executions: Literal[0] = 0
    mutated_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> "OfflineMutationReceipt":
        _require_matching_hash(self, "receipt_sha256")
        return self


class OfflineMutationQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-mutation-report-v0"] = (
        "assessment-computation-mutation-report-v0"
    )
    runner_version: Literal["assessment-computation-mutation-runner-v0"] = (
        OFFLINE_MUTATION_RUNNER_VERSION
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_status: Literal["executed_offline"]
    planned_mutations: Literal[200]
    executed_mutations: Literal[200]
    detected_mutations: Literal[200]
    network_calls: Literal[0] = 0
    native_engine_executions: Literal[0] = 0
    all_critical_mutations_detected: Literal[True]
    receipts: list[OfflineMutationReceipt] = Field(min_length=200, max_length=200)
    receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> "OfflineMutationQualificationReport":
        if len({receipt.mutation_id for receipt in self.receipts}) != 200:
            raise ValueError(
                "offline mutation receipts must cover 200 unique mutations"
            )
        observed = _sha256_json(
            [receipt.model_dump(mode="json") for receipt in self.receipts]
        )
        if observed != self.receipts_sha256:
            raise ValueError("receipts_sha256 does not match mutation receipts")
        _require_matching_hash(self, "report_sha256")
        return self


class NativeExecutionRequest(StrictModel):
    """Closed request sent to an isolated native-engine runner over a Unix socket."""

    schema_version: Literal["assessment-computation-native-execution-v0"] = (
        NATIVE_EXECUTION_PROTOCOL_VERSION
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(pattern=r"^acv0-(webwork|imathas)-lineage-[0-9]{2}$")
    lineage_id: str = Field(pattern=r"^acv0-lineage-[0-9]{2}$")
    engine: Literal["webwork", "imathas"]
    seed: StrictInt = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_source: str = Field(min_length=1, max_length=100_000)
    parameterized_spec: ParameterizedItemSpec
    lineage_blueprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> "NativeExecutionRequest":
        if (
            hashlib.sha256(self.engine_source.encode()).hexdigest()
            != self.source_sha256
        ):
            raise ValueError("native execution source does not match source_sha256")
        if self.parameterized_spec.engine != self.engine:
            raise ValueError("native execution spec does not match its engine")
        if self.engine == "imathas" and self.imathas_namespace is None:
            raise ValueError("IMathAS execution requires a disposable namespace")
        if self.engine == "webwork" and self.imathas_namespace is not None:
            raise ValueError("WeBWorK execution cannot carry an IMathAS namespace")
        _require_matching_hash(self, "request_sha256")
        return self


class NativeExecutionObservation(StrictModel):
    """Raw result returned by the isolated runner before a receipt is minted."""

    schema_version: Literal["assessment-computation-native-observation-v0"] = (
        "assessment-computation-native-observation-v0"
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(pattern=r"^acv0-(webwork|imathas)-lineage-[0-9]{2}$")
    engine: Literal["webwork", "imathas"]
    seed: StrictInt = Field(ge=1, le=100)
    engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    imathas_object_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$"
    )
    engine_observed_values: dict[str, "NativeObservedValue"] = Field(
        min_length=1, max_length=12
    )
    engine_observed_correct_answer: str = Field(
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=110
    )
    engine_observed_wrong_answer: str = Field(
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=110
    )
    constraints_satisfied: StrictBool
    correct_answer_accepted: StrictBool
    wrong_answer_rejected: StrictBool
    rendered: StrictBool
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings: list[str] = Field(default_factory=list, max_length=20)
    errors: list[str] = Field(default_factory=list, max_length=20)
    outbound_request_count: StrictInt = Field(ge=0, le=1_000)
    raw_engine_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_observation(self) -> "NativeExecutionObservation":
        if self.engine == "imathas" and any(
            value is None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError("IMathAS observations require adapter and object identity")
        if self.engine == "webwork" and any(
            value is not None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError("WeBWorK observations cannot carry IMathAS identity")
        _require_matching_hash(self, "observation_sha256")
        return self


class NativeObservedValue(StrictModel):
    kind: Literal["integer", "decimal"]
    integer: StrictInt | None = None
    decimal: str | None = Field(
        default=None,
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$",
        max_length=110,
    )

    @model_validator(mode="after")
    def validate_value(self) -> "NativeObservedValue":
        if self.kind == "integer":
            if self.integer is None or self.decimal is not None:
                raise ValueError("integer runtime value requires only integer")
        elif self.decimal is None or self.integer is not None:
            raise ValueError("decimal runtime value requires only decimal")
        elif _canonical_decimal(Decimal(self.decimal)) != self.decimal:
            raise ValueError("decimal runtime value must be canonical")
        return self

    def as_runtime_number(self) -> int | float:
        if self.kind == "integer":
            assert self.integer is not None
            return self.integer
        assert self.decimal is not None
        return float(Decimal(self.decimal))


class NativeQualificationReceipt(StrictModel):
    schema_version: Literal["assessment-computation-native-receipt-v0"] = (
        NATIVE_RECEIPT_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    endpoint_profile: Literal["isolated_canary_v0"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(pattern=r"^acv0-(webwork|imathas)-lineage-[0-9]{2}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_id: str = Field(pattern=r"^acv0-lineage-[0-9]{2}$")
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_blueprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: Literal["webwork", "imathas"]
    answer_kind: Literal["numeric"]
    seed: StrictInt = Field(ge=1, le=100)
    compiler_version: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    imathas_object_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$"
    )
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_observed_values: dict[str, NativeObservedValue] = Field(
        min_length=1, max_length=12
    )
    engine_observed_values_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_observed_correct_answer: str = Field(
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=110
    )
    engine_observed_wrong_answer: str = Field(
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=110
    )
    constraints_satisfied: StrictBool
    correct_answer_accepted: StrictBool
    wrong_answer_rejected: StrictBool
    rendered: StrictBool
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings: list[str] = Field(default_factory=list, max_length=20)
    errors: list[str] = Field(default_factory=list, max_length=20)
    outbound_request_count: StrictInt = Field(ge=0, le=1_000)
    execution_status: Literal["executed"]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_shape(self) -> "NativeQualificationReceipt":
        if self.engine == "imathas" and (
            self.adapter_image_digest is None
            or self.imathas_namespace is None
            or self.imathas_object_id is None
        ):
            raise ValueError(
                "IMathAS receipts require adapter, namespace, and object identity"
            )
        if self.engine == "webwork" and any(
            value is not None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError(
                "WeBWorK receipts must not claim IMathAS adapter or namespace fields"
            )
        observed_values_hash = _sha256_json(
            {
                name: value.model_dump(mode="json")
                for name, value in self.engine_observed_values.items()
            }
        )
        if observed_values_hash != self.engine_observed_values_sha256:
            raise ValueError(
                "engine_observed_values_sha256 does not match observed values"
            )
        for value in (
            self.engine_observed_correct_answer,
            self.engine_observed_wrong_answer,
        ):
            if _canonical_decimal(Decimal(value)) != value:
                raise ValueError("engine-observed answers must be canonical decimals")
        _require_matching_hash(self, "receipt_sha256")
        return self


@dataclass(frozen=True)
class LoadedNativeReceiptLedger:
    """Receipts plus the identity of the exact bytes imported from disk."""

    records: tuple[NativeQualificationReceipt, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class NativeQualificationTrustPolicy(StrictModel):
    schema_version: Literal["assessment-computation-native-trust-policy-v0"] = (
        "assessment-computation-native-trust-policy-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    endpoint_profile: Literal["isolated_canary_v0"]
    webwork_engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    imathas_engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    imathas_adapter_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_ledger_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imathas_namespace: str = Field(pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$")
    imathas_namespace_disposable: Literal[True] = True
    imathas_namespace_created_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imathas_namespace_cleanup_status: Literal["verified"]
    imathas_namespace_cleanup_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_policy_hash(self) -> "NativeQualificationTrustPolicy":
        if (
            len(
                {
                    self.receipt_ledger_raw_sha256,
                    self.network_attestation_sha256,
                    self.imathas_namespace_created_attestation_sha256,
                    self.imathas_namespace_cleanup_attestation_sha256,
                    self.operator_attestation_sha256,
                }
            )
            != 5
        ):
            raise ValueError(
                "native policy evidence hashes must have distinct purposes"
            )
        _require_matching_hash(self, "policy_sha256")
        return self


class NativeQualificationIssue(StrictModel):
    item_id: str = Field(min_length=5, max_length=100)
    seed: StrictInt = Field(ge=1, le=100)
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")


class NativeQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-native-report-v0"] = (
        "assessment-computation-native-report-v0"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_receipts: Literal[4_000] = EXECUTIONS_PER_PLAN
    imported_receipts: int = Field(ge=0, le=4_000)
    valid_receipts: int = Field(ge=0, le=4_000)
    invalid_receipts: int = Field(ge=0, le=4_000)
    missing_receipts: int = Field(ge=0, le=4_000)
    duplicate_receipts: int = Field(ge=0, le=4_000)
    unexpected_receipts: int = Field(ge=0, le=4_000)
    execution_status: Literal["not_run", "partial", "failed", "passed"]
    qualified: StrictBool
    trust_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trust_policy_applied: StrictBool
    qualification_run_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$"
    )
    endpoint_profile: Literal["isolated_canary_v0"] | None = None
    webwork_engine_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_engine_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    network_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    raw_receipt_ledger_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    raw_receipt_ledger_byte_count: int | None = Field(default=None, ge=0)
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    imathas_namespace_cleanup_status: Literal["verified"] | None = None
    imathas_namespace_cleanup_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    receipt_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_execution_claims: int = Field(ge=0, le=4_000)
    issues: list[NativeQualificationIssue] = Field(
        default_factory=list, max_length=4_000
    )
    limitations: list[str] = Field(min_length=2, max_length=10)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_state(self) -> "NativeQualificationReport":
        if self.qualified != (self.execution_status == "passed"):
            raise ValueError("only a complete passed report may be qualified")
        if self.qualified and not self.trust_policy_applied:
            raise ValueError(
                "native qualification requires an operator-pinned trust policy"
            )
        if self.qualified and any(
            value is None
            for value in (
                self.qualification_run_id,
                self.endpoint_profile,
                self.webwork_engine_image_digest,
                self.imathas_engine_image_digest,
                self.imathas_adapter_image_digest,
                self.network_attestation_sha256,
                self.raw_receipt_ledger_sha256,
                self.raw_receipt_ledger_byte_count,
                self.imathas_namespace,
                self.imathas_namespace_cleanup_status,
                self.imathas_namespace_cleanup_attestation_sha256,
            )
        ):
            raise ValueError(
                "qualified native report must expose its pinned runtime identity"
            )
        if self.execution_status == "passed" and (
            self.valid_receipts != EXECUTIONS_PER_PLAN
            or self.imported_receipts != EXECUTIONS_PER_PLAN
            or self.invalid_receipts
            or self.missing_receipts
            or self.duplicate_receipts
            or self.unexpected_receipts
        ):
            raise ValueError(
                "native qualification requires exactly 4,000 valid receipts"
            )
        if self.execution_status == "not_run" and self.imported_receipts != 0:
            raise ValueError("not_run cannot contain imported receipts")
        _require_matching_hash(self, "report_sha256")
        return self


class AlgebraNativeExecutionRequest(StrictModel):
    """Sealed, qualification-only request for one native algebra grader."""

    schema_version: Literal["assessment-computation-algebra-native-execution-v0"] = (
        "assessment-computation-algebra-native-execution-v0"
    )
    qualification_only: Literal[True] = True
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^acv0-algebra-(webwork|imathas)-[0-9]{2}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_id: str = Field(pattern=r"^acv0-algebraic-[0-9]{2}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: Literal["webwork", "imathas"]
    operation: Literal["substitute", "expand", "factor", "equivalent", "solve"]
    answer_kind: Literal["numeric", "formula", "solution_set"]
    source_contract: Literal[
        "production_typed_ast_v0",
        "qualification_only_solution_set_v0",
    ]
    compiler_version: Literal[
        "assessment-computation-typed-ast-v0",
        "assessment-computation-algebra-native-qualification-v0",
    ]
    source: str = Field(min_length=20, max_length=20_000)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_symbols: list[str] = Field(default_factory=list, max_length=1)
    correct_submission: str = Field(min_length=1, max_length=2_000)
    alternate_correct_submission: str | None = Field(
        default=None, min_length=1, max_length=2_000
    )
    wrong_submission: str = Field(min_length=1, max_length=2_000)
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> "AlgebraNativeExecutionRequest":
        if (
            hashlib.sha256(self.source.encode("utf-8")).hexdigest()
            != self.source_sha256
        ):
            raise ValueError("algebra execution source does not match source_sha256")
        if self.engine == "imathas" and self.imathas_namespace is None:
            raise ValueError(
                "IMathAS algebra execution requires a disposable namespace"
            )
        if self.engine == "webwork" and self.imathas_namespace is not None:
            raise ValueError(
                "WeBWorK algebra execution cannot carry an IMathAS namespace"
            )
        if self.answer_kind == "numeric":
            if self.response_symbols or self.alternate_correct_submission is not None:
                raise ValueError(
                    "numeric algebra execution cannot carry symbolic alternatives"
                )
        elif (
            len(self.response_symbols) != 1 or self.alternate_correct_submission is None
        ):
            raise ValueError(
                "symbolic algebra execution requires one symbol and an alternate"
            )
        expected_compiler = (
            "assessment-computation-algebra-native-qualification-v0"
            if self.answer_kind == "solution_set"
            else "assessment-computation-typed-ast-v0"
        )
        if self.compiler_version != expected_compiler:
            raise ValueError(
                "algebra execution compiler does not match its answer kind"
            )
        expected_source_contract = (
            "qualification_only_solution_set_v0"
            if self.answer_kind == "solution_set"
            else "production_typed_ast_v0"
        )
        if self.source_contract != expected_source_contract:
            raise ValueError(
                "algebra execution source contract does not match answer kind"
            )
        _require_matching_hash(self, "request_sha256")
        return self


class AlgebraNativeExecutionObservation(StrictModel):
    """Raw native-runner result before Assessment AI derives a receipt."""

    schema_version: Literal["assessment-computation-algebra-native-observation-v0"] = (
        "assessment-computation-algebra-native-observation-v0"
    )
    qualification_only: Literal[True] = True
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^acv0-algebra-(webwork|imathas)-[0-9]{2}$")
    engine: Literal["webwork", "imathas"]
    engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    imathas_object_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$"
    )
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_engine_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    constraints_satisfied: StrictBool
    correct_answer_accepted: StrictBool
    alternate_correct_answer_accepted: StrictBool | None = None
    wrong_answer_rejected: StrictBool
    rendered: StrictBool
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings: list[str] = Field(default_factory=list, max_length=20)
    errors: list[str] = Field(default_factory=list, max_length=20)
    outbound_request_count: Literal[0]
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_observation(self) -> "AlgebraNativeExecutionObservation":
        if self.engine == "imathas" and any(
            value is None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError(
                "IMathAS algebra observations require adapter and object identity"
            )
        if self.engine == "webwork" and any(
            value is not None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError(
                "WeBWorK algebra observations cannot carry IMathAS identity"
            )
        _require_matching_hash(self, "observation_sha256")
        return self


class AlgebraNativeQualificationReceipt(StrictModel):
    """One native grader result bound to an exact sealed algebra plan."""

    schema_version: Literal["assessment-computation-algebra-native-receipt-v0"] = (
        ALGEBRA_NATIVE_RECEIPT_SCHEMA_VERSION
    )
    qualification_only: Literal[True] = True
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    endpoint_profile: Literal["isolated_canary_v0"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^acv0-algebra-(webwork|imathas)-[0-9]{2}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_id: str = Field(pattern=r"^acv0-algebraic-[0-9]{2}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: Literal["webwork", "imathas"]
    operation: Literal["substitute", "expand", "factor", "equivalent", "solve"]
    answer_kind: Literal["numeric", "formula", "solution_set"]
    source_contract: Literal[
        "production_typed_ast_v0",
        "qualification_only_solution_set_v0",
    ]
    compiler_version: Literal[
        "assessment-computation-typed-ast-v0",
        "assessment-computation-algebra-native-qualification-v0",
    ]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    correct_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alternate_correct_submission_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    wrong_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    imathas_object_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$"
    )
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_engine_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    constraints_satisfied: StrictBool
    correct_answer_accepted: StrictBool
    alternate_correct_answer_accepted: StrictBool | None = None
    wrong_answer_rejected: StrictBool
    rendered: StrictBool
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings: list[str] = Field(default_factory=list, max_length=20)
    errors: list[str] = Field(default_factory=list, max_length=20)
    outbound_request_count: Literal[0]
    execution_status: Literal["executed"]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "AlgebraNativeQualificationReceipt":
        if self.engine == "imathas" and any(
            value is None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError(
                "IMathAS algebra receipts require adapter and object identity"
            )
        if self.engine == "webwork" and any(
            value is not None
            for value in (
                self.adapter_image_digest,
                self.imathas_namespace,
                self.imathas_object_id,
            )
        ):
            raise ValueError("WeBWorK algebra receipts cannot carry IMathAS identity")
        if self.answer_kind == "numeric":
            if (
                self.alternate_correct_submission_sha256 is not None
                or self.alternate_correct_answer_accepted is not None
            ):
                raise ValueError(
                    "numeric algebra receipts cannot claim alternate input evidence"
                )
        elif (
            self.alternate_correct_submission_sha256 is None
            or self.alternate_correct_answer_accepted is None
        ):
            raise ValueError(
                "symbolic algebra receipts require alternate input evidence"
            )
        expected_compiler = (
            "assessment-computation-algebra-native-qualification-v0"
            if self.answer_kind == "solution_set"
            else "assessment-computation-typed-ast-v0"
        )
        if self.compiler_version != expected_compiler:
            raise ValueError("algebra receipt compiler does not match its answer kind")
        expected_source_contract = (
            "qualification_only_solution_set_v0"
            if self.answer_kind == "solution_set"
            else "production_typed_ast_v0"
        )
        if self.source_contract != expected_source_contract:
            raise ValueError(
                "algebra receipt source contract does not match answer kind"
            )
        _require_matching_hash(self, "receipt_sha256")
        return self


@dataclass(frozen=True)
class LoadedAlgebraNativeExecutionPlan:
    records: tuple[AlgebraNativeExecutionRequest, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class LoadedAlgebraNativeReceiptLedger:
    records: tuple[AlgebraNativeQualificationReceipt, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class AlgebraNativeQualificationIssue(StrictModel):
    plan_id: str = Field(pattern=r"^acv0-algebra-(webwork|imathas)-[0-9]{2}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")


class AlgebraNativeQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-algebra-native-report-v0"] = (
        "assessment-computation-algebra-native-report-v0"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_receipts: Literal[40] = 40
    imported_receipts: int = Field(ge=0, le=40)
    valid_receipts: int = Field(ge=0, le=40)
    invalid_receipts: int = Field(ge=0, le=40)
    missing_receipts: int = Field(ge=0, le=40)
    duplicate_receipts: int = Field(ge=0, le=40)
    unexpected_receipts: int = Field(ge=0, le=40)
    execution_status: Literal["not_run", "partial", "failed", "passed"]
    qualified: StrictBool
    production_delivery_enabled: Literal[False] = False
    trust_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trust_policy_applied: StrictBool
    qualification_run_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$"
    )
    endpoint_profile: Literal["isolated_canary_v0"] | None = None
    webwork_engine_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_engine_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    network_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    imathas_namespace: str | None = Field(
        default=None, pattern=r"^acv0-canary-[a-zA-Z0-9_.-]{1,80}$"
    )
    imathas_namespace_cleanup_status: Literal["verified"] | None = None
    imathas_namespace_cleanup_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    raw_receipt_ledger_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    raw_receipt_ledger_byte_count: int | None = Field(default=None, ge=0)
    receipt_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issues: list[AlgebraNativeQualificationIssue] = Field(
        default_factory=list, max_length=50
    )
    limitations: list[str] = Field(min_length=2, max_length=10)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> "AlgebraNativeQualificationReport":
        if self.qualified != (self.execution_status == "passed"):
            raise ValueError("only a complete passed algebra report may be qualified")
        if self.qualified and (
            not self.trust_policy_applied
            or self.imported_receipts != 40
            or self.valid_receipts != 40
            or self.invalid_receipts
            or self.missing_receipts
            or self.duplicate_receipts
            or self.unexpected_receipts
            or self.issues
            or any(
                value is None
                for value in (
                    self.qualification_run_id,
                    self.endpoint_profile,
                    self.webwork_engine_image_digest,
                    self.imathas_engine_image_digest,
                    self.imathas_adapter_image_digest,
                    self.network_attestation_sha256,
                    self.imathas_namespace,
                    self.imathas_namespace_cleanup_status,
                    self.imathas_namespace_cleanup_attestation_sha256,
                    self.raw_receipt_ledger_sha256,
                    self.raw_receipt_ledger_byte_count,
                )
            )
        ):
            raise ValueError(
                "qualified algebra evidence requires all 40 trusted native receipts"
            )
        if self.execution_status == "not_run" and self.imported_receipts:
            raise ValueError("not_run cannot contain algebra receipts")
        _require_matching_hash(self, "report_sha256")
        return self


class UcumFunctionalCaseReceipt(StrictModel):
    test_id: str = Field(min_length=1, max_length=200)
    case_kind: Literal["validation", "conversion"]
    unit_code: str | None = Field(default=None, min_length=1, max_length=100)
    expected_valid: StrictBool | None = None
    source_unit: str | None = Field(default=None, min_length=1, max_length=100)
    target_unit: str | None = Field(default=None, min_length=1, max_length=100)
    input_value: str | None = Field(default=None, min_length=1, max_length=120)
    expected_value: str | None = Field(default=None, min_length=1, max_length=120)
    observed_value: str | None = Field(default=None, max_length=120)
    status: Literal["passed", "failed"]
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_case_hash(self) -> "UcumFunctionalCaseReceipt":
        if self.case_kind == "validation":
            if self.unit_code is None or self.expected_valid is None:
                raise ValueError(
                    "validation cases require unit_code and expected_valid"
                )
            if any(
                value is not None
                for value in (
                    self.source_unit,
                    self.target_unit,
                    self.input_value,
                    self.expected_value,
                    self.observed_value,
                )
            ):
                raise ValueError("validation cases cannot contain conversion fields")
        elif (
            self.source_unit is None
            or self.target_unit is None
            or self.input_value is None
            or self.expected_value is None
            or self.unit_code is not None
            or self.expected_valid is not None
        ):
            raise ValueError("conversion cases require only conversion fields")
        _require_matching_hash(self, "case_sha256")
        return self


class UcumQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-ucum-qualification-v0"] = (
        UCUM_QUALIFICATION_SCHEMA_VERSION
    )
    profile: Literal["libretexts-edu-units-v0"] = UCUM_PROFILE
    ucum_version: Literal["2.2"] = "2.2"
    essence_sha256: Literal[
        "6022a1f4a77d93efa23b941ae50055cb9d3fdcb8bb5db6b85deda004467bb380"
    ] = UCUM_ESSENCE_SHA256
    ucumvert_grammar_sha256: Literal[
        "101165c3b2d2c8cfaf394dbc159889cadafdaa8e044ae0a52cfa4400217fdeed"
    ] = UCUMVERT_GRAMMAR_SHA256
    pint_ucum_definitions_sha256: Literal[
        "cc64e39ec183e2b28dbd1ec462eec7081c3f4c3cbdd9705501332f18b653024a"
    ] = PINT_UCUM_DEFINITIONS_SHA256
    ucumvert_wheel_sha256: Literal[
        "b1c3c5b875843642f52f5f015fc8728ff997ce9c337b64bd25141ae44848bdd0"
    ] = UCUMVERT_WHEEL_SHA256
    pint_wheel_sha256: Literal[
        "27eb25143bd5de9fcc4d5a4b484f16faf6b4615aa93ece6b3373a8c1a3c1b97d"
    ] = PINT_WHEEL_SHA256
    runtime_integrity_status: Literal["verified", "failed"]
    runtime_asset_hashes_observed: dict[str, str]
    named_subset: list[str] = Field(min_length=32, max_length=32)
    artifact_status: Literal[
        "not_supplied", "checksum_verified", "checksum_mismatch", "invalid"
    ]
    artifact_identity: Literal[
        "absent",
        "pinned_ucum_java_mirror",
        "unrecognized_checksum_pinned_artifact",
    ]
    artifact_byte_count: int = Field(ge=0, le=MAX_UCUM_ARTIFACT_BYTES)
    artifact_sha256_expected: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_sha256_observed: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    official_cases_discovered: int = Field(ge=0)
    artifact_section_counts: dict[str, int]
    selection_revision: Literal["libretexts-edu-units-v0-selection-2026-07-23"] = (
        UCUM_SELECTION_REVISION
    )
    pinned_subset_expected_cases: Literal[190] = PINNED_UCUM_SUBSET_CASES
    subset_cases_selected: int = Field(ge=0)
    subset_cases_executed: int = Field(ge=0)
    subset_cases_passed: int = Field(ge=0)
    official_functional_tests_status: Literal[
        "not_run", "no_supported_cases", "subset_passed", "failed"
    ]
    libretexts_unit_corpus_cases: Literal[20] = 20
    libretexts_unit_corpus_passed: int = Field(ge=0, le=20)
    qualification_status: Literal["not_run", "partial", "passed", "failed"]
    subset_qualified: StrictBool
    official_functional_test_conformance_claimed: Literal[False] = False
    full_ucum_conformance_claimed: Literal[False] = False
    official_attachment_equivalence: Literal["unverified", "verified"] = "unverified"
    equivalence_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    equivalence_attestation_raw_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    equivalence_attestation_raw_byte_count: int | None = Field(default=None, gt=0)
    equivalence_reviewer_subject_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    network_calls: Literal[0] = 0
    case_receipts: list[UcumFunctionalCaseReceipt] = Field(
        default_factory=list, max_length=20_000
    )
    limitations: list[str] = Field(min_length=2, max_length=10)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_state(self) -> "UcumQualificationReport":
        if self.named_subset != sorted(LIBRETEXTS_UCUM_CODES):
            raise ValueError("named_subset must exactly match libretexts-edu-units-v0")
        if self.subset_cases_passed > self.subset_cases_executed:
            raise ValueError("UCUM passes cannot exceed executions")
        if self.subset_qualified != (
            self.qualification_status == "passed"
            and self.artifact_status == "checksum_verified"
            and self.artifact_identity == "pinned_ucum_java_mirror"
            and self.runtime_integrity_status == "verified"
            and self.official_attachment_equivalence == "verified"
            and self.official_functional_tests_status == "subset_passed"
            and self.subset_cases_executed == PINNED_UCUM_SUBSET_CASES
            and self.subset_cases_passed == self.subset_cases_executed
            and self.libretexts_unit_corpus_passed == 20
        ):
            raise ValueError(
                "subset_qualified is inconsistent with qualification evidence"
            )
        if self.subset_qualified and (
            len(self.case_receipts) != self.subset_cases_executed
            or len({receipt.case_sha256 for receipt in self.case_receipts})
            != self.subset_cases_executed
            or any(receipt.status != "passed" for receipt in self.case_receipts)
        ):
            raise ValueError(
                "qualified UCUM evidence requires every unique passed case receipt"
            )
        equivalence_fields = (
            self.equivalence_attestation_sha256,
            self.equivalence_attestation_raw_sha256,
            self.equivalence_attestation_raw_byte_count,
            self.equivalence_reviewer_subject_sha256,
        )
        if self.official_attachment_equivalence == "verified":
            if any(value is None for value in equivalence_fields):
                raise ValueError(
                    "verified UCUM equivalence requires the imported review attestation"
                )
        elif any(value is not None for value in equivalence_fields):
            raise ValueError(
                "unverified UCUM equivalence cannot carry attestation identity"
            )
        _require_matching_hash(self, "report_sha256")
        return self


class UcumArtifactEquivalenceAttestation(StrictModel):
    """Independent review binding the pinned mirror to the official attachment."""

    schema_version: Literal["assessment-computation-ucum-artifact-equivalence-v0"] = (
        "assessment-computation-ucum-artifact-equivalence-v0"
    )
    ucum_version: Literal["2.2"] = "2.2"
    comparison_method: Literal["byte_for_byte"]
    official_attachment_sha256: Literal[
        "3b3feb9d8ecfe8958da69b1afcd25572e3f8d80a36bfcbcda4834a10a0eeef0d"
    ] = PINNED_UCUM_FUNCTIONAL_TEST_SHA256
    official_attachment_byte_count: Literal[36_782] = PINNED_UCUM_FUNCTIONAL_TEST_BYTES
    local_artifact_sha256: Literal[
        "3b3feb9d8ecfe8958da69b1afcd25572e3f8d80a36bfcbcda4834a10a0eeef0d"
    ] = PINNED_UCUM_FUNCTIONAL_TEST_SHA256
    local_artifact_byte_count: Literal[36_782] = PINNED_UCUM_FUNCTIONAL_TEST_BYTES
    official_release_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparison_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["equivalent"]
    rationale: str = Field(min_length=20, max_length=1_000)
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_attestation(self) -> "UcumArtifactEquivalenceAttestation":
        scoped_hashes = {
            self.official_release_record_sha256,
            self.comparison_ledger_sha256,
            self.reviewer_subject_sha256,
            self.reviewer_attestation_sha256,
        }
        if len(scoped_hashes) != 4:
            raise ValueError(
                "UCUM equivalence evidence hashes must be independently scoped"
            )
        _require_matching_hash(self, "attestation_sha256")
        return self


@dataclass(frozen=True)
class LoadedUcumArtifactEquivalenceAttestation:
    record: UcumArtifactEquivalenceAttestation
    raw_attestation_sha256: str
    raw_byte_count: int


class WorkflowPositiveReceipt(StrictModel):
    schema_version: Literal["assessment-computation-workflow-positive-receipt-v0"] = (
        "assessment-computation-workflow-positive-receipt-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_id: str = Field(
        pattern=r"^acv0-(numeric|algebraic|unit|webwork|imathas)-[a-z0-9_-]+$"
    )
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    surface: EvaluationSurface
    runner_version: Literal["assessment-computation-workflow-runner-v0"] = (
        WORKFLOW_POSITIVE_RUNNER_VERSION
    )
    transport: Literal["unix_socket"] = "unix_socket"
    computation_runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_request: ComputationValidationRequest
    validation_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compute_result: ComputationResult
    compute_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_report: AssessmentValidationReport
    replay_report: AssessmentValidationReport
    state: ObservedValidationState
    oracle_match: StrictBool
    deterministic_replay: StrictBool
    workflow_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    service_response_count: Literal[3] = 3
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "WorkflowPositiveReceipt":
        blueprint_hash = canonical_blueprint_hash(self.validation_request.blueprint)
        if (
            self.compute_result.blueprint_hash != blueprint_hash
            or self.workflow_report.blueprint_hash != blueprint_hash
            or self.replay_report.blueprint_hash != blueprint_hash
        ):
            raise ValueError("workflow receipt blueprint identities do not match")
        if self.validation_request != _validation_request_from_result(
            self.validation_request.blueprint,
            self.compute_result,
        ):
            raise ValueError(
                "workflow validation request was not derived from the compute result"
            )
        if self.validation_request_sha256 != _sha256_json(
            self.validation_request.model_dump(mode="json")
        ):
            raise ValueError("workflow validation request hash does not match")
        if self.compute_result_sha256 != _sha256_json(
            self.compute_result.model_dump(mode="json")
        ):
            raise ValueError("workflow compute result hash does not match")
        workflow_report_sha256 = _sha256_json(
            self.workflow_report.model_dump(mode="json")
        )
        replay_report_sha256 = _sha256_json(self.replay_report.model_dump(mode="json"))
        if (
            self.workflow_report_sha256 != workflow_report_sha256
            or self.replay_report_sha256 != replay_report_sha256
        ):
            raise ValueError("workflow report hash does not match its report")
        reports_match = self.workflow_report == self.replay_report
        if self.deterministic_replay != reports_match:
            raise ValueError("workflow deterministic replay claim does not match")
        if self.state.value != self.workflow_report.status.value:
            raise ValueError("workflow state does not match the service report")
        if self.workflow_capture_sha256 != _workflow_capture_sha256(
            self.case_id,
            capture_index=1,
            report_sha256=workflow_report_sha256,
        ):
            raise ValueError("workflow first capture hash does not match")
        if self.replay_capture_sha256 != _workflow_capture_sha256(
            self.case_id,
            capture_index=2,
            report_sha256=replay_report_sha256,
        ):
            raise ValueError("workflow replay capture hash does not match")
        if self.workflow_capture_sha256 == self.replay_capture_sha256:
            raise ValueError(
                "workflow replay requires an independently captured report"
            )
        is_engine = self.surface in {
            EvaluationSurface.WEBWORK,
            EvaluationSurface.IMATHAS,
        }
        if is_engine != (self.compiled_source_sha256 is not None):
            raise ValueError(
                "workflow compiler hash is required only for external-engine surfaces"
            )
        _require_matching_hash(self, "receipt_sha256")
        return self


@dataclass(frozen=True)
class LoadedWorkflowPositiveLedger:
    records: tuple[WorkflowPositiveReceipt, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class WorkflowEvidenceTrustPolicy(StrictModel):
    schema_version: Literal["assessment-computation-workflow-trust-policy-v0"] = (
        "assessment-computation-workflow-trust-policy-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_version: Literal["assessment-computation-workflow-runner-v0"] = (
        WORKFLOW_POSITIVE_RUNNER_VERSION
    )
    transport: Literal["unix_socket"] = "unix_socket"
    computation_runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_receipt_ledger_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_policy(self) -> "WorkflowEvidenceTrustPolicy":
        if self.computation_runtime_manifest_sha256 == "0" * 64:
            raise ValueError(
                "workflow policy cannot trust the in-process test runtime identity"
            )
        if (
            len(
                {
                    self.positive_receipt_ledger_raw_sha256,
                    self.mutation_report_sha256,
                    self.operator_attestation_sha256,
                }
            )
            != 3
        ):
            raise ValueError("workflow policy hashes must have distinct purposes")
        _require_matching_hash(self, "policy_sha256")
        return self


class WorkflowObservationEvidence(StrictModel):
    schema_version: Literal["assessment-computation-workflow-observations-v0"] = (
        "assessment-computation-workflow-observations-v0"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_version: Literal["assessment-computation-workflow-runner-v0"] = (
        WORKFLOW_POSITIVE_RUNNER_VERSION
    )
    transport: Literal["unix_socket"] = "unix_socket"
    computation_runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations: list[ComputationEvaluationObservation] = Field(
        min_length=300, max_length=300
    )
    positive_workflow_receipt_sha256s: dict[str, str] = Field(
        min_length=100, max_length=100
    )
    positive_receipt_ledger_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_receipt_ledger_raw_byte_count: int = Field(gt=0)
    trust_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust_policy_applied: Literal[True] = True
    mutation_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence_hashes(self) -> "WorkflowObservationEvidence":
        ids = [observation.case_id for observation in self.observations]
        if len(set(ids)) != 300:
            raise ValueError("workflow observations must contain 300 unique cases")
        if any(
            not _is_sha256(value)
            for value in self.positive_workflow_receipt_sha256s.values()
        ):
            raise ValueError("positive workflow receipt values must be SHA-256 hashes")
        if len(set(self.positive_workflow_receipt_sha256s.values())) != 100:
            raise ValueError("positive workflow receipts must be independently hashed")
        observed = _sha256_json(
            [record.model_dump(mode="json") for record in self.observations]
        )
        if observed != self.observations_sha256:
            raise ValueError("observations_sha256 does not match observations")
        _require_matching_hash(self, "evidence_sha256")
        return self


class QualificationMergedObservationEvidence(StrictModel):
    """Derived evaluation states bound to raw workflow and native reports."""

    schema_version: Literal["assessment-computation-qualified-observations-v0"] = (
        "assessment-computation-qualified-observations-v0"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    algebra_native_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upgraded_case_report_sha256s: dict[str, str] = Field(
        min_length=60,
        max_length=60,
    )
    observations: list[ComputationEvaluationObservation] = Field(
        min_length=300,
        max_length=300,
    )
    observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upgraded_positive_count: Literal[60] = 60
    solution_set_production_delivery_enabled: Literal[False] = False
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> "QualificationMergedObservationEvidence":
        ids = [observation.case_id for observation in self.observations]
        if len(set(ids)) != 300:
            raise ValueError(
                "qualified observation evidence must contain 300 unique cases"
            )
        if any(
            not _is_sha256(value)
            for value in self.upgraded_case_report_sha256s.values()
        ):
            raise ValueError("qualified observation report bindings must be SHA-256")
        if (
            _sha256_json(
                [
                    observation.model_dump(mode="json")
                    for observation in self.observations
                ]
            )
            != self.observations_sha256
        ):
            raise ValueError(
                "qualified observations_sha256 does not match observations"
            )
        _require_matching_hash(self, "evidence_sha256")
        return self


class SmeReviewRecord(StrictModel):
    schema_version: Literal["assessment-computation-sme-review-v0"] = (
        "assessment-computation-sme-review-v0"
    )
    record_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_type: Literal["computation_fixture", "parameterized_lineage", "mutation"]
    target_id: str = Field(min_length=5, max_length=150)
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    rationale: str = Field(min_length=10, max_length=1_000)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "SmeReviewRecord":
        if (
            len(
                {
                    self.manifest_sha256,
                    self.reviewer_subject_sha256,
                    self.reviewer_attestation_sha256,
                    self.target_sha256,
                }
            )
            != 4
        ):
            raise ValueError("SME evidence hashes must have distinct purposes")
        _require_matching_hash(self, "record_sha256")
        return self


@dataclass(frozen=True)
class LoadedSmeReviewLedger:
    """SME records plus the identity of the exact append-only file."""

    records: tuple[SmeReviewRecord, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class SmeReviewQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-sme-review-report-v0"] = (
        "assessment-computation-sme-review-report-v0"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_targets: Literal[280] = 280
    reviewed_targets: int = Field(ge=0, le=280)
    approved_targets: int = Field(ge=0, le=280)
    rejected_targets: int = Field(ge=0, le=280)
    missing_targets: int = Field(ge=0, le=280)
    duplicate_targets: int = Field(ge=0, le=280)
    unexpected_targets: int = Field(ge=0, le=280)
    reviewer_count: int = Field(ge=0, le=280)
    unique_record_hashes: int = Field(ge=0, le=280)
    unique_reviewer_attestation_hashes: int = Field(ge=0, le=280)
    raw_review_ledger_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    raw_review_ledger_byte_count: int | None = Field(default=None, ge=0)
    raw_ledger_bound: StrictBool
    execution_status: Literal["not_run", "incomplete", "failed", "passed"]
    qualified: StrictBool
    review_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_state(self) -> "SmeReviewQualificationReport":
        if self.qualified != (self.execution_status == "passed"):
            raise ValueError("SME report qualification state is inconsistent")
        if self.qualified and (
            self.reviewed_targets != 280
            or self.approved_targets != 280
            or self.rejected_targets
            or self.missing_targets
            or self.duplicate_targets
            or self.unexpected_targets
            or self.unique_record_hashes != 280
            or self.unique_reviewer_attestation_hashes != 280
            or not self.raw_ledger_bound
            or self.raw_review_ledger_sha256 is None
            or self.raw_review_ledger_byte_count is None
        ):
            raise ValueError("SME qualification requires 280 exact approvals")
        _require_matching_hash(self, "report_sha256")
        return self


class PairedDraftEvidence(StrictModel):
    schema_version: Literal["assessment-computation-paired-draft-v0"] = (
        "assessment-computation-paired-draft-v0"
    )
    sealed_concept_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: Literal["control", "treatment"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    computation_blueprint_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    computation_report_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    provider_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_call_receipt_sha256s: list[str] = Field(min_length=1, max_length=4)
    provider_cost_usd: str = Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_draft_evidence(self) -> "PairedDraftEvidence":
        if self.arm == "treatment" and (
            self.computation_blueprint_sha256 is None
            or self.computation_report_sha256 is None
        ):
            raise ValueError(
                "treatment drafts require exact blueprint and report hashes"
            )
        if self.arm == "control" and (
            self.computation_blueprint_sha256 is not None
            or self.computation_report_sha256 is not None
        ):
            raise ValueError(
                "control drafts cannot claim treatment computation evidence"
            )
        if any(
            not _is_sha256(value) for value in self.provider_call_receipt_sha256s
        ) or len(set(self.provider_call_receipt_sha256s)) != len(
            self.provider_call_receipt_sha256s
        ):
            raise ValueError("provider call receipt hashes must be unique SHA-256s")
        if Decimal(self.provider_cost_usd) < 0:
            raise ValueError("provider cost cannot be negative")
        _require_matching_hash(self, "evidence_sha256")
        return self


class PairedReviewRecord(StrictModel):
    schema_version: Literal["assessment-computation-paired-review-v0"] = (
        "assessment-computation-paired-review-v0"
    )
    sealed_concept_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: Literal["control", "treatment"]
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    computationally_correct_without_edit: StrictBool
    material_computation_defect: StrictBool
    correction_seconds: StrictInt | StrictFloat = Field(ge=0, le=86_400)
    source_grounding_score: StrictInt = Field(ge=0, le=100)
    pedagogy_score: StrictInt = Field(ge=0, le=100)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "PairedReviewRecord":
        _require_matching_hash(self, "record_sha256")
        return self


class PairedStudyEvidenceLedger(StrictModel):
    schema_version: Literal["assessment-computation-paired-study-v0"] = (
        "assessment-computation-paired-study-v0"
    )
    study_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    computation_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sealed_concept_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    randomization_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blinding_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixed_provider_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_call_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_cost_usd: str = Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
    drafts: list[PairedDraftEvidence] = Field(default_factory=list, max_length=100)
    drafts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: list[PairedReviewRecord] = Field(default_factory=list, max_length=200)
    records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_ledger_hashes(self) -> "PairedStudyEvidenceLedger":
        observed_drafts = _sha256_json(
            [draft.model_dump(mode="json") for draft in self.drafts]
        )
        if observed_drafts != self.drafts_sha256:
            raise ValueError("paired-study drafts_sha256 does not match drafts")
        observed_records = _sha256_json(
            [record.model_dump(mode="json") for record in self.records]
        )
        if observed_records != self.records_sha256:
            raise ValueError("paired-study records_sha256 does not match records")
        concepts = sorted({draft.sealed_concept_sha256 for draft in self.drafts})
        if _sha256_json(concepts) != self.sealed_concept_set_sha256:
            raise ValueError("sealed_concept_set_sha256 does not match draft evidence")
        if any(
            draft.provider_settings_sha256 != self.fixed_provider_settings_sha256
            for draft in self.drafts
        ):
            raise ValueError("draft evidence does not use fixed provider settings")
        provider_receipts = sorted(
            receipt
            for draft in self.drafts
            for receipt in draft.provider_call_receipt_sha256s
        )
        if len(provider_receipts) != len(set(provider_receipts)):
            raise ValueError("provider call receipts must bind one exact draft only")
        if _sha256_json(provider_receipts) != self.provider_call_ledger_sha256:
            raise ValueError(
                "provider_call_ledger_sha256 does not match bound provider receipts"
            )
        observed_cost = sum(
            (Decimal(draft.provider_cost_usd) for draft in self.drafts),
            start=Decimal(0),
        )
        if observed_cost != Decimal(self.provider_cost_usd):
            raise ValueError("provider_cost_usd does not match bound draft costs")
        _require_matching_hash(self, "ledger_sha256")
        return self


class PairedStudyQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-paired-study-report-v0"] = (
        "assessment-computation-paired-study-report-v0"
    )
    study_id: str = Field(min_length=1, max_length=100)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    computation_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    concept_count: int = Field(ge=0, le=50)
    draft_count: int = Field(ge=0, le=100)
    provider_call_count: int = Field(ge=0, le=400)
    reviewer_count: int = Field(ge=0, le=2)
    record_count: int = Field(ge=0, le=200)
    drafts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_cost_usd: str
    treatment_correct_without_edit_rate: float = Field(ge=0, le=1)
    control_defect_rate: float = Field(ge=0, le=1)
    treatment_defect_rate: float = Field(ge=0, le=1)
    control_median_correction_seconds: float = Field(ge=0)
    treatment_median_correction_seconds: float = Field(ge=0)
    source_grounding_delta: float = Field(ge=-1, le=1)
    pedagogy_delta: float = Field(ge=-1, le=1)
    execution_status: Literal["not_run", "incomplete", "failed", "passed"]
    qualified: StrictBool
    failures: list[str] = Field(default_factory=list, max_length=20)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_state(self) -> "PairedStudyQualificationReport":
        if self.qualified != (self.execution_status == "passed"):
            raise ValueError("paired-study qualification state is inconsistent")
        if self.qualified and (
            self.concept_count != 50
            or self.draft_count != 100
            or self.provider_call_count < 100
            or self.reviewer_count != 2
            or self.record_count != 200
        ):
            raise ValueError(
                "paired-study qualification requires exact draft and review coverage"
            )
        _require_matching_hash(self, "report_sha256")
        return self


class Build08CompatibilityQualification(StrictModel):
    schema_version: Literal["assessment-computation-build08-native-evidence-v0"] = (
        "assessment-computation-build08-native-evidence-v0"
    )
    run_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$"
    )
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_receipts: Literal[4_000] = 4_000
    imported_seed_receipts: int = Field(ge=0, le=4_000)
    imported_engine_probes: int = Field(ge=0, le=4_000)
    imported_adapt_attestations: int = Field(ge=0, le=4_000)
    valid_receipts: int = Field(ge=0, le=4_000)
    seed_receipt_ledger_raw_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    seed_receipt_ledger_raw_byte_count: int | None = Field(default=None, ge=0)
    engine_probe_ledger_raw_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    engine_probe_ledger_raw_byte_count: int | None = Field(default=None, ge=0)
    adapt_attestation_ledger_raw_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    adapt_attestation_ledger_raw_byte_count: int | None = Field(default=None, ge=0)
    receipt_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trust_policy_applied: StrictBool
    webwork_engine_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_engine_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    imathas_adapter_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    network_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    operator_attestation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    executed: int = Field(ge=0, le=4_000)
    passed: int = Field(ge=0, le=4_000)
    warning_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    execution_status: Literal["not_run", "partial", "failed", "passed"]
    qualified: StrictBool
    issues: list[str] = Field(default_factory=list, max_length=100)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence_hash(self) -> "Build08CompatibilityQualification":
        if self.qualified != (self.execution_status == "passed"):
            raise ValueError(
                "BUILD-08 compatibility qualification state is inconsistent"
            )
        if self.qualified and (
            not self.trust_policy_applied
            or self.run_id is None
            or self.valid_receipts != 4_000
            or self.imported_seed_receipts != 4_000
            or self.imported_engine_probes != 4_000
            or self.imported_adapt_attestations != 4_000
            or self.executed != 4_000
            or self.passed != 4_000
            or self.warning_count
            or self.error_count
            or self.issues
            or any(
                value is None
                for value in (
                    self.seed_receipt_ledger_raw_sha256,
                    self.seed_receipt_ledger_raw_byte_count,
                    self.engine_probe_ledger_raw_sha256,
                    self.engine_probe_ledger_raw_byte_count,
                    self.adapt_attestation_ledger_raw_sha256,
                    self.adapt_attestation_ledger_raw_byte_count,
                    self.trust_policy_sha256,
                    self.webwork_engine_image_digest,
                    self.imathas_engine_image_digest,
                    self.imathas_adapter_image_digest,
                    self.network_attestation_sha256,
                    self.operator_attestation_sha256,
                )
            )
        ):
            raise ValueError(
                "qualified BUILD-08 evidence requires three exact 4,000-row ledgers"
            )
        _require_matching_hash(self, "evidence_sha256")
        return self


class Build08CompatibilityTrustPolicy(StrictModel):
    schema_version: Literal["assessment-computation-build08-trust-policy-v0"] = (
        "assessment-computation-build08-trust-policy-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed_receipt_ledger_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_probe_ledger_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapt_attestation_ledger_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    webwork_engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    imathas_engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    imathas_adapter_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_policy(self) -> "Build08CompatibilityTrustPolicy":
        if (
            len(
                {
                    self.seed_receipt_ledger_raw_sha256,
                    self.engine_probe_ledger_raw_sha256,
                    self.adapt_attestation_ledger_raw_sha256,
                    self.network_attestation_sha256,
                    self.operator_attestation_sha256,
                }
            )
            != 5
        ):
            raise ValueError(
                "BUILD-08 policy evidence hashes must be independently scoped"
            )
        _require_matching_hash(self, "policy_sha256")
        return self


@dataclass(frozen=True)
class LoadedEvidenceLedger:
    records: tuple[Any, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class SafetyMonitorReceipt(StrictModel):
    schema_version: Literal["assessment-computation-safety-monitor-v0"] = (
        "assessment-computation-safety-monitor-v0"
    )
    category: Literal[
        "publication",
        "grading",
        "permission",
        "migration_loss",
        "network",
        "file_access",
        "process_escape",
        "cross_draft",
    ]
    evidence_source: Literal[
        "application_audit",
        "database_audit",
        "network_monitor",
        "filesystem_monitor",
        "process_monitor",
        "canary_test_runner",
    ]
    observation_window_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_event_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_observer_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_events: Literal[0] = 0
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_monitor_receipt(self) -> "SafetyMonitorReceipt":
        if (
            len(
                {
                    self.observation_window_sha256,
                    self.raw_event_ledger_sha256,
                    self.independent_observer_attestation_sha256,
                }
            )
            != 3
        ):
            raise ValueError("safety monitor hashes must have distinct purposes")
        _require_matching_hash(self, "receipt_sha256")
        return self


class SpikeSafetyEvidence(StrictModel):
    schema_version: Literal["assessment-computation-safety-evidence-v0"] = (
        "assessment-computation-safety-evidence-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    computation_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cloned_database_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    off_mode_parity_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_restore_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fake_publication_adapter_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    isolation_test_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_observer_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    monitor_receipts: list[SafetyMonitorReceipt] = Field(min_length=8, max_length=8)
    monitor_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_status: Literal["completed"]
    publication_events: Literal[0] = 0
    grading_events: Literal[0] = 0
    permission_events: Literal[0] = 0
    migration_loss_events: Literal[0] = 0
    network_events: Literal[0] = 0
    file_access_events: Literal[0] = 0
    process_escape_events: Literal[0] = 0
    cross_draft_events: Literal[0] = 0
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence_hash(self) -> "SpikeSafetyEvidence":
        categories = [receipt.category for receipt in self.monitor_receipts]
        if set(categories) != {
            "publication",
            "grading",
            "permission",
            "migration_loss",
            "network",
            "file_access",
            "process_escape",
            "cross_draft",
        } or len(categories) != len(set(categories)):
            raise ValueError(
                "safety evidence requires one receipt for every stopping invariant"
            )
        if (
            len({receipt.raw_event_ledger_sha256 for receipt in self.monitor_receipts})
            != 8
            or len(
                {
                    receipt.independent_observer_attestation_sha256
                    for receipt in self.monitor_receipts
                }
            )
            != 8
        ):
            raise ValueError(
                "safety monitors require independent raw ledgers and attestations"
            )
        observed_receipts = _sha256_json(
            [receipt.model_dump(mode="json") for receipt in self.monitor_receipts]
        )
        if observed_receipts != self.monitor_receipts_sha256:
            raise ValueError(
                "monitor_receipts_sha256 does not match safety monitor receipts"
            )
        if self.operator_attestation_sha256 == (
            self.independent_observer_attestation_sha256
        ):
            raise ValueError("operator and independent observer must attest separately")
        _require_matching_hash(self, "evidence_sha256")
        return self


class CanaryStage(StrEnum):
    OFFLINE_CORPUS_SECURITY = "offline_corpus_security"
    CANDIDATE_OFF_PARITY = "candidate_off_parity"
    BACKUP_RESTORE = "backup_restore"
    PRIOR_IMAGE_ROLLBACK = "prior_image_rollback"
    ASSIST_NUMERIC = "assist_numeric"
    ASSIST_ALGEBRAIC = "assist_algebraic"
    ASSIST_UNIT = "assist_unit"
    ENFORCE_FAKE_ADAPTERS_ATTESTATIONS = "enforce_fake_adapters_attestations"
    COMPATIBILITY_SHADOW_380 = "compatibility_shadow_380"
    RETURN_OFF = "return_off"


CANARY_STAGE_ORDER = tuple(CanaryStage)


class CanaryStageEventKind(StrEnum):
    OFFLINE_FAILURE_COUNT = "offline_failure_count"
    OFF_MODE_EFFECT_COUNT = "off_mode_effect_count"
    BACKUP_RESTORE_MISMATCH_COUNT = "backup_restore_mismatch_count"
    PRIOR_IMAGE_ROLLBACK_MISMATCH_COUNT = "prior_image_rollback_mismatch_count"
    ASSIST_NUMERIC_REPORT_COUNT = "assist_numeric_report_count"
    ASSIST_ALGEBRAIC_REPORT_COUNT = "assist_algebraic_report_count"
    ASSIST_UNIT_REPORT_COUNT = "assist_unit_report_count"
    ENFORCE_FAKE_ADAPTER_SCENARIO_COUNT = "enforce_fake_adapter_scenario_count"
    COMPATIBILITY_SHADOW_DRAFT_COUNT = "compatibility_shadow_draft_count"
    RETURN_OFF_EFFECT_COUNT = "return_off_effect_count"
    REAL_PUBLICATION_ATTEMPT_COUNT = "real_publication_attempt_count"


CANARY_STAGE_EVENT_KIND: Mapping[CanaryStage, CanaryStageEventKind] = MappingProxyType(
    {
        CanaryStage.OFFLINE_CORPUS_SECURITY: (
            CanaryStageEventKind.OFFLINE_FAILURE_COUNT
        ),
        CanaryStage.CANDIDATE_OFF_PARITY: CanaryStageEventKind.OFF_MODE_EFFECT_COUNT,
        CanaryStage.BACKUP_RESTORE: (
            CanaryStageEventKind.BACKUP_RESTORE_MISMATCH_COUNT
        ),
        CanaryStage.PRIOR_IMAGE_ROLLBACK: (
            CanaryStageEventKind.PRIOR_IMAGE_ROLLBACK_MISMATCH_COUNT
        ),
        CanaryStage.ASSIST_NUMERIC: (CanaryStageEventKind.ASSIST_NUMERIC_REPORT_COUNT),
        CanaryStage.ASSIST_ALGEBRAIC: (
            CanaryStageEventKind.ASSIST_ALGEBRAIC_REPORT_COUNT
        ),
        CanaryStage.ASSIST_UNIT: CanaryStageEventKind.ASSIST_UNIT_REPORT_COUNT,
        CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS: (
            CanaryStageEventKind.ENFORCE_FAKE_ADAPTER_SCENARIO_COUNT
        ),
        CanaryStage.COMPATIBILITY_SHADOW_380: (
            CanaryStageEventKind.COMPATIBILITY_SHADOW_DRAFT_COUNT
        ),
        CanaryStage.RETURN_OFF: CanaryStageEventKind.RETURN_OFF_EFFECT_COUNT,
    }
)


def _canary_mode_and_family(
    stage: CanaryStage,
) -> tuple[Literal["offline", "off", "assist", "enforce"], str | None]:
    assist_family = {
        CanaryStage.ASSIST_NUMERIC: "numeric",
        CanaryStage.ASSIST_ALGEBRAIC: "algebraic",
        CanaryStage.ASSIST_UNIT: "unit",
    }.get(stage)
    mode: Literal["offline", "off", "assist", "enforce"] = (
        "offline"
        if stage == CanaryStage.OFFLINE_CORPUS_SECURITY
        else "assist"
        if assist_family is not None
        else "enforce"
        if stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
        else "off"
    )
    return mode, assist_family


def _canary_stage_contract_sha256(stage: CanaryStage) -> str:
    mode, family = _canary_mode_and_family(stage)
    return _sha256_json(
        {
            "revision": CANARY_LOCAL_EXECUTOR_REVISION,
            "accepted_build08_base_commit": ACCEPTED_BUILD08_BASE_COMMIT,
            "accepted_build08_prior_image_digest": (
                ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
            ),
            "stage": stage,
            "mode": mode,
            "family": family,
            "required_event_kinds": [
                CANARY_STAGE_EVENT_KIND[stage],
                CanaryStageEventKind.REAL_PUBLICATION_ATTEMPT_COUNT,
            ],
            "specialist_attestation_evidence_required": (
                stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
            ),
        }
    )


def _validate_canary_stage_identity(
    *,
    stage: CanaryStage,
    sequence: int,
    mode: str,
    family: str | None,
    cloned_database_snapshot_sha256: str | None,
) -> None:
    expected_stage = CANARY_STAGE_ORDER[sequence - 1]
    if stage != expected_stage:
        raise ValueError("canary stage sequence does not match the fixed rollout plan")
    expected_mode, expected_family = _canary_mode_and_family(stage)
    if mode != expected_mode or family != expected_family:
        raise ValueError("canary stage mode/family does not match the fixed plan")
    database_stage = stage != CanaryStage.OFFLINE_CORPUS_SECURITY
    if (cloned_database_snapshot_sha256 is not None) != database_stage:
        raise ValueError("database canary stages require the cloned snapshot hash")


class SpecialistAttestationCanaryEvidence(StrictModel):
    """Observed positive and negative paths for trusted specialist identity."""

    schema_version: Literal[
        "assessment-computation-specialist-attestation-canary-v0"
    ] = "assessment-computation-specialist-attestation-canary-v0"
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    sequence: Literal[8] = 8
    stage: Literal[CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS] = (
        CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
    )
    authenticated_allowlisted_accept_count: StrictInt = Field(ge=1, le=100)
    missing_or_invalid_token_rejection_count: StrictInt = Field(ge=1, le=100)
    client_subject_spoof_rejection_count: StrictInt = Field(ge=1, le=100)
    reviewer_identity_only_rejection_count: StrictInt = Field(ge=1, le=100)
    accepted_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_header_stripping_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    negative_path_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> "SpecialistAttestationCanaryEvidence":
        if (
            len(
                {
                    self.accepted_attestation_sha256,
                    self.proxy_header_stripping_receipt_sha256,
                    self.negative_path_ledger_sha256,
                }
            )
            != 3
        ):
            raise ValueError(
                "specialist canary paths require distinct positive, proxy, and "
                "negative-path evidence"
            )
        _require_matching_hash(self, "evidence_sha256")
        return self


class CanaryStageEvent(StrictModel):
    """One self-hashed observation emitted by a local disposable stage command."""

    schema_version: Literal["assessment-computation-canary-event-v0"] = (
        "assessment-computation-canary-event-v0"
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    sequence: StrictInt = Field(ge=1, le=10)
    stage: CanaryStage
    kind: CanaryStageEventKind
    observed_count: StrictInt = Field(ge=0, le=100_000)
    observed_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    specialist_attestation_evidence: SpecialistAttestationCanaryEvidence | None = None
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_event(self) -> "CanaryStageEvent":
        stage_kind = CANARY_STAGE_EVENT_KIND[self.stage]
        if self.kind not in {
            stage_kind,
            CanaryStageEventKind.REAL_PUBLICATION_ATTEMPT_COUNT,
        }:
            raise ValueError("canary event kind does not belong to its stage")
        if (
            self.kind
            in {
                CanaryStageEventKind.OFFLINE_FAILURE_COUNT,
                CanaryStageEventKind.OFF_MODE_EFFECT_COUNT,
                CanaryStageEventKind.BACKUP_RESTORE_MISMATCH_COUNT,
                CanaryStageEventKind.PRIOR_IMAGE_ROLLBACK_MISMATCH_COUNT,
                CanaryStageEventKind.RETURN_OFF_EFFECT_COUNT,
                CanaryStageEventKind.REAL_PUBLICATION_ATTEMPT_COUNT,
            }
            and self.observed_count != 0
        ):
            raise ValueError("canary zero-event invariant was violated")
        if (
            self.kind
            in {
                CanaryStageEventKind.ASSIST_NUMERIC_REPORT_COUNT,
                CanaryStageEventKind.ASSIST_ALGEBRAIC_REPORT_COUNT,
                CanaryStageEventKind.ASSIST_UNIT_REPORT_COUNT,
                CanaryStageEventKind.ENFORCE_FAKE_ADAPTER_SCENARIO_COUNT,
            }
            and self.observed_count < 1
        ):
            raise ValueError("canary stage did not observe a required scenario")
        if (
            self.kind == CanaryStageEventKind.COMPATIBILITY_SHADOW_DRAFT_COUNT
            and self.observed_count != 380
        ):
            raise ValueError("compatibility shadow must observe exactly 380 drafts")
        rollback_event = (
            self.kind == CanaryStageEventKind.PRIOR_IMAGE_ROLLBACK_MISMATCH_COUNT
        )
        if rollback_event:
            if (
                self.observed_image_digest
                != ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
            ):
                raise ValueError(
                    "rollback event did not observe the accepted BUILD-08 image"
                )
        elif self.observed_image_digest is not None:
            raise ValueError("only rollback evidence may carry an observed image")
        specialist_event = (
            self.stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
            and self.kind == CanaryStageEventKind.ENFORCE_FAKE_ADAPTER_SCENARIO_COUNT
        )
        if (self.specialist_attestation_evidence is not None) != specialist_event:
            raise ValueError(
                "the enforce canary scenario requires explicit specialist "
                "attestation exercise evidence"
            )
        if self.specialist_attestation_evidence is not None and (
            self.specialist_attestation_evidence.request_sha256 != self.request_sha256
            or self.specialist_attestation_evidence.run_id != self.run_id
            or self.specialist_attestation_evidence.sequence != self.sequence
            or self.specialist_attestation_evidence.stage != self.stage
        ):
            raise ValueError(
                "specialist attestation exercise evidence changed the stage identity"
            )
        _require_matching_hash(self, "event_sha256")
        return self


class CanaryStageObserverAttestation(StrictModel):
    schema_version: Literal["assessment-computation-canary-observer-v0"] = (
        "assessment-computation-canary-observer-v0"
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    accepted_build08_base_commit: Literal[
        "8497aad448d18c49d967480134eff9f80a444bd0"
    ] = ACCEPTED_BUILD08_BASE_COMMIT
    prior_image_digest: Literal[
        "sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed"
    ] = ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
    candidate_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    computation_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: StrictInt = Field(ge=1, le=10)
    stage: CanaryStage
    input_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_event_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_event_ledger_byte_count: StrictInt = Field(
        ge=1, le=MAX_CANARY_STAGE_ARTIFACT_BYTES
    )
    observer_subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["passed"]
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_attestation(self) -> "CanaryStageObserverAttestation":
        if (
            len(
                {
                    self.raw_event_ledger_sha256,
                    self.observer_subject_sha256,
                    self.observation_artifact_sha256,
                }
            )
            != 3
        ):
            raise ValueError("observer evidence hashes must have distinct purposes")
        _require_matching_hash(self, "attestation_sha256")
        return self


class CanaryStageExecutionRequest(StrictModel):
    """Immutable local command identity; it contains no outcome booleans."""

    schema_version: Literal["assessment-computation-canary-execution-v0"] = (
        "assessment-computation-canary-execution-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_build08_base_commit: Literal[
        "8497aad448d18c49d967480134eff9f80a444bd0"
    ] = ACCEPTED_BUILD08_BASE_COMMIT
    prior_image_digest: Literal[
        "sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed"
    ] = ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
    candidate_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    computation_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: StrictInt = Field(ge=1, le=10)
    stage: CanaryStage
    mode: Literal["offline", "off", "assist", "enforce"]
    family: Literal["numeric", "algebraic", "unit"] | None = None
    cloned_database_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    command_argv: list[str] = Field(min_length=1, max_length=32)
    command_argv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> "CanaryStageExecutionRequest":
        _validate_canary_stage_identity(
            stage=self.stage,
            sequence=self.sequence,
            mode=self.mode,
            family=self.family,
            cloned_database_snapshot_sha256=self.cloned_database_snapshot_sha256,
        )
        if (
            len(
                {
                    self.prior_image_digest,
                    self.candidate_image_digest,
                    self.computation_image_digest,
                }
            )
            != 3
        ):
            raise ValueError(
                "prior, candidate, and computation images must be distinct"
            )
        if any(
            not argument
            or len(argument.encode("utf-8")) > 1024
            or "\x00" in argument
            or "\n" in argument
            for argument in self.command_argv
        ):
            raise ValueError("canary command arguments must be nonempty and bounded")
        if _sha256_json(self.command_argv) != self.command_argv_sha256:
            raise ValueError("command_argv_sha256 does not match the command")
        if self.stage_contract_sha256 != _canary_stage_contract_sha256(self.stage):
            raise ValueError("canary request has the wrong stage contract")
        _require_matching_hash(self, "request_sha256")
        return self


@dataclass(frozen=True)
class LocalCanaryStageArtifacts:
    """Paths used by one local command inside an explicitly disposable root."""

    working_directory: Path
    input_state: Path
    output_state: Path
    raw_event_ledger: Path
    observer_attestation: Path
    timeout_seconds: float = 300.0


class CanaryStageReceipt(StrictModel):
    schema_version: Literal["assessment-computation-canary-stage-v0"] = (
        "assessment-computation-canary-stage-v0"
    )
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_build08_base_commit: Literal[
        "8497aad448d18c49d967480134eff9f80a444bd0"
    ] = ACCEPTED_BUILD08_BASE_COMMIT
    prior_image_digest: Literal[
        "sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed"
    ] = ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
    candidate_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    computation_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: StrictInt = Field(ge=1, le=10)
    stage: CanaryStage
    mode: Literal["offline", "off", "assist", "enforce"]
    family: Literal["numeric", "algebraic", "unit"] | None = None
    cloned_database_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    input_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_revision: Literal["assessment-computation-local-canary-executor-v0"] = (
        CANARY_LOCAL_EXECUTOR_REVISION
    )
    command_argv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_exit_code: Literal[0] = 0
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_event_kind: CanaryStageEventKind
    rollback_observed_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    raw_event_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_event_ledger_byte_count: StrictInt = Field(
        ge=1, le=MAX_CANARY_STAGE_ARTIFACT_BYTES
    )
    raw_event_count: Literal[2] = 2
    independent_observer_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fake_publication_adapter_used: StrictBool
    specialist_attestation_evidence: SpecialistAttestationCanaryEvidence | None = None
    specialist_attestation_paths_exercised: StrictBool
    real_publication_attempt_count: Literal[0] = 0
    passed: Literal[True] = True
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_stage(self) -> "CanaryStageReceipt":
        _validate_canary_stage_identity(
            stage=self.stage,
            sequence=self.sequence,
            mode=self.mode,
            family=self.family,
            cloned_database_snapshot_sha256=self.cloned_database_snapshot_sha256,
        )
        enforce_stage = self.stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
        if (
            self.fake_publication_adapter_used != enforce_stage
            or (self.specialist_attestation_evidence is not None) != enforce_stage
            or self.specialist_attestation_paths_exercised
            != (self.specialist_attestation_evidence is not None)
        ):
            raise ValueError(
                "only enforce canary uses fake publication and specialist attestations"
            )
        if self.specialist_attestation_evidence is not None and (
            self.specialist_attestation_evidence.run_id != self.run_id
            or self.specialist_attestation_evidence.sequence != self.sequence
            or self.specialist_attestation_evidence.stage != self.stage
        ):
            raise ValueError(
                "specialist attestation exercise evidence changed the receipt identity"
            )
        if (
            len(
                {
                    self.prior_image_digest,
                    self.candidate_image_digest,
                    self.computation_image_digest,
                }
            )
            != 3
        ):
            raise ValueError(
                "prior, candidate, and computation images must be distinct"
            )
        if self.stage_contract_sha256 != _canary_stage_contract_sha256(self.stage):
            raise ValueError("canary receipt has the wrong stage contract")
        if self.stage_event_kind != CANARY_STAGE_EVENT_KIND[self.stage]:
            raise ValueError("canary receipt has the wrong stage event")
        rollback_stage = self.stage == CanaryStage.PRIOR_IMAGE_ROLLBACK
        if rollback_stage:
            if (
                self.rollback_observed_image_digest
                != ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
            ):
                raise ValueError(
                    "rollback receipt did not observe the accepted BUILD-08 image"
                )
        elif self.rollback_observed_image_digest is not None:
            raise ValueError("only the rollback stage may record the prior image")
        if self.raw_event_ledger_sha256 == self.independent_observer_attestation_sha256:
            raise ValueError(
                "canary event ledger and observer attestation must be independent"
            )
        if (
            self.stage
            in {
                CanaryStage.CANDIDATE_OFF_PARITY,
                CanaryStage.BACKUP_RESTORE,
            }
            and self.input_state_sha256 != self.output_state_sha256
        ):
            raise ValueError("canary parity stage changed the observed state")
        _require_matching_hash(self, "receipt_sha256")
        return self


@dataclass(frozen=True)
class LoadedCanaryStageLedger:
    records: tuple[CanaryStageReceipt, ...]
    raw_ledger_sha256: str
    raw_byte_count: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class CanaryQualificationReport(StrictModel):
    schema_version: Literal["assessment-computation-canary-report-v0"] = (
        "assessment-computation-canary-report-v0"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_build08_base_commit: Literal[
        "8497aad448d18c49d967480134eff9f80a444bd0"
    ] = ACCEPTED_BUILD08_BASE_COMMIT
    prior_image_digest: Literal[
        "sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed"
    ] = ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
    run_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$"
    )
    candidate_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    computation_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    expected_stages: Literal[10] = 10
    imported_stages: int = Field(ge=0, le=10)
    passed_stages: int = Field(ge=0, le=10)
    stage_receipt_sha256s: dict[str, str] = Field(default_factory=dict, max_length=10)
    raw_stage_ledger_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_stage_ledger_byte_count: int | None = Field(default=None, ge=0)
    execution_status: Literal["not_run", "partial", "failed", "passed"]
    qualified: StrictBool
    issues: list[str] = Field(default_factory=list, max_length=20)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> "CanaryQualificationReport":
        if self.qualified != (self.execution_status == "passed"):
            raise ValueError("canary qualification state is inconsistent")
        if self.qualified and (
            self.run_id is None
            or self.candidate_image_digest is None
            or self.computation_image_digest is None
            or self.imported_stages != 10
            or self.passed_stages != 10
            or set(self.stage_receipt_sha256s)
            != {stage.value for stage in CANARY_STAGE_ORDER}
            or self.raw_stage_ledger_sha256 is None
            or self.raw_stage_ledger_byte_count is None
            or self.issues
        ):
            raise ValueError(
                "qualified canary evidence requires all ten stage receipts"
            )
        _require_matching_hash(self, "report_sha256")
        return self


def build_computation_evaluation_manifest(
    catalog_path: Path = CATALOG_PATH,
) -> ComputationEvaluationManifest:
    stat = catalog_path.stat()
    cache_key = (str(catalog_path.resolve()), stat.st_mtime_ns, stat.st_size)
    cached = _MANIFEST_JSON_CACHE.get(cache_key)
    if cached is not None:
        return ComputationEvaluationManifest.model_validate_json(cached)
    raw_bytes = catalog_path.read_bytes()
    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict) or raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("unsupported computation fixture catalog")

    provenance = FixtureProvenance.model_validate(raw.get("provenance"))
    computation_cases = [
        _build_computation_fixture(
            family=family,
            index=index,
            raw_case=raw_case,
            provenance=provenance,
        )
        for family in ("numeric", "algebraic", "unit")
        for index, raw_case in enumerate(_require_list(raw, family), start=1)
    ]
    lineages = [
        _build_lineage(index, raw_lineage, provenance)
        for index, raw_lineage in enumerate(_require_list(raw, "lineages"), start=1)
    ]
    twins = [
        _build_engine_twin(lineage, engine)
        for engine in ("webwork", "imathas")
        for lineage in lineages
    ]
    formula_cases = _build_formula_qualification_cases()
    algebra_cases = [case for case in computation_cases if case.family == "algebraic"]
    algebra_native_plans = [
        _build_algebra_native_plan(case, engine)
        for engine in ("webwork", "imathas")
        for case in algebra_cases
    ]
    mutations = _build_mutations(computation_cases, twins, lineages)
    seed_plans = build_seed_plan_summaries(twins)

    content: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "catalog_revision": raw.get("catalog_revision"),
        "catalog_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "validator_revision": VALIDATOR_REVISION,
        "provenance": provenance.model_dump(mode="json"),
        "computation_cases": [
            case.model_dump(mode="json") for case in computation_cases
        ],
        "parameterized_lineages": [
            lineage.model_dump(mode="json") for lineage in lineages
        ],
        "engine_twins": [twin.model_dump(mode="json") for twin in twins],
        "formula_qualification_cases": [
            case.model_dump(mode="json") for case in formula_cases
        ],
        "algebra_native_plans": [
            case.model_dump(mode="json") for case in algebra_native_plans
        ],
        "mutations": [mutation.model_dump(mode="json") for mutation in mutations],
        "seed_plans": [plan.model_dump(mode="json") for plan in seed_plans],
        "total_positive_surfaces": 100,
        "total_mutations": 200,
        "total_planned_engine_executions": TOTAL_PLANNED_ENGINE_EXECUTIONS,
        "total_planned_algebra_native_receipts": 40,
    }
    content["manifest_sha256"] = _sha256_json(content)
    manifest = ComputationEvaluationManifest.model_validate(content)
    _MANIFEST_JSON_CACHE[cache_key] = manifest.model_dump_json()
    return manifest


def build_computation_seed_plan(
    run_id: str = "assessment-computation-v0-planned",
    *,
    manifest: ComputationEvaluationManifest | None = None,
) -> list[EngineSeedPlanCase]:
    manifest = manifest or build_computation_evaluation_manifest()
    return [
        EngineSeedPlanCase(
            run_id=run_id,
            item_id=twin.fixture_id,
            lineage_id=twin.lineage_id,
            engine=twin.engine,
            split=twin.split,
            seed=seed,
            compiler_version=twin.compiler_version,
            source_sha256=twin.source_sha256,
            runtime_oracle="native_engine",
            expected_answer_source="engine_observed_parameters",
            execution_status="planned_not_executed",
        )
        for twin in manifest.engine_twins
        for seed in range(1, SEEDS_PER_ENGINE_ITEM + 1)
    ]


def build_algebra_native_execution_plan(
    *,
    run_id: str,
    imathas_namespace: str,
    manifest: ComputationEvaluationManifest | None = None,
) -> list[AlgebraNativeExecutionRequest]:
    """Build the 40 sealed requests a native algebra runner must execute.

    These requests are qualification artifacts only. They neither change the
    production parameterized-item contract nor make symbolic delivery eligible.
    """

    manifest = manifest or build_computation_evaluation_manifest()
    fixtures = {
        case.fixture_id: case
        for case in manifest.computation_cases
        if case.family == "algebraic"
    }
    requests: list[AlgebraNativeExecutionRequest] = []
    for plan in manifest.algebra_native_plans:
        requests.append(
            _build_algebra_native_execution_request(
                plan,
                fixtures[plan.fixture_id],
                run_id=run_id,
                manifest_sha256=manifest.manifest_sha256,
                imathas_namespace=(
                    imathas_namespace if plan.engine == "imathas" else None
                ),
            )
        )
    if len(requests) != 40:
        raise ValueError("algebra native execution requires exactly 40 requests")
    return requests


def _build_algebra_native_execution_request(
    plan: AlgebraNativePlanCase,
    case: ComputationFixture,
    *,
    run_id: str,
    manifest_sha256: str,
    imathas_namespace: str | None,
) -> AlgebraNativeExecutionRequest:
    result = compute_blueprint(case.blueprint)
    compiled = compile_typed_algebra_qualification_item(
        case.blueprint,
        result,
        engine=plan.engine,
    )
    alternate_hash = (
        hashlib.sha256(
            compiled.alternate_correct_submission.encode("utf-8")
        ).hexdigest()
        if compiled.alternate_correct_submission is not None
        else None
    )
    if (
        case.fixture_id != plan.fixture_id
        or case.fixture_sha256 != plan.fixture_sha256
        or compiled.compiler_version != plan.compiler_version
        or compiled.answer_kind != plan.answer_kind
        or compiled.source != plan.source
        or compiled.source_sha256 != plan.source_sha256
        or list(compiled.response_symbols) != plan.response_symbols
        or hashlib.sha256(compiled.correct_submission.encode("utf-8")).hexdigest()
        != plan.correct_submission_sha256
        or alternate_hash != plan.alternate_correct_submission_sha256
        or hashlib.sha256(compiled.wrong_submission.encode("utf-8")).hexdigest()
        != plan.wrong_submission_sha256
    ):
        raise ValueError(
            "algebra native source or submissions differ from the sealed plan"
        )
    identity: dict[str, Any] = {
        "schema_version": "assessment-computation-algebra-native-execution-v0",
        "qualification_only": True,
        "run_id": run_id,
        "manifest_sha256": manifest_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "fixture_id": plan.fixture_id,
        "fixture_sha256": plan.fixture_sha256,
        "engine": plan.engine,
        "operation": plan.operation,
        "answer_kind": plan.answer_kind,
        "source_contract": plan.source_contract,
        "compiler_version": plan.compiler_version,
        "source": compiled.source,
        "source_sha256": compiled.source_sha256,
        "response_symbols": list(compiled.response_symbols),
        "correct_submission": compiled.correct_submission,
        "alternate_correct_submission": compiled.alternate_correct_submission,
        "wrong_submission": compiled.wrong_submission,
        "imathas_namespace": imathas_namespace,
    }
    return AlgebraNativeExecutionRequest(
        **identity,
        request_sha256=_sha256_json(identity),
    )


class UnixSocketNativeExecutor:
    """Client for a separately deployed native-engine canary runner.

    The runner is deliberately outside Assessment AI and is reachable only via
    an already-created Unix socket. This client never accepts an HTTP(S) URL.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not socket_path.is_absolute() or socket_path.is_symlink():
            raise ValueError(
                "native runner socket must be an absolute non-symlink path"
            )
        try:
            mode = os.lstat(socket_path).st_mode
        except FileNotFoundError as exc:
            raise ValueError("native runner socket does not exist") from exc
        if not stat.S_ISSOCK(mode):
            raise ValueError("native runner path is not a Unix socket")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("native runner timeout must be between 1 and 120 seconds")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def __call__(
        self, request: NativeExecutionRequest
    ) -> NativeExecutionObservation:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        body = bytearray()
        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    "http://native-runner/v0/execute",
                    json=request.model_dump(mode="json"),
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("native runner returned a non-success status")
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith("application/json"):
                        raise ValueError(
                            "native runner returned an unexpected content type"
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None and int(declared) > 256 * 1024:
                        raise ValueError("native runner response exceeds 256 KiB")
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            raise ValueError("native runner response exceeds 256 KiB")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ValueError("native runner request failed") from exc
        try:
            return NativeExecutionObservation.model_validate_json(body)
        except Exception as exc:
            raise ValueError("native runner returned a malformed observation") from exc


class UnixSocketAlgebraNativeExecutor:
    """Bounded UDS client for qualification-only algebra grader requests."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not socket_path.is_absolute() or socket_path.is_symlink():
            raise ValueError(
                "algebra native runner socket must be an absolute non-symlink path"
            )
        try:
            mode = os.lstat(socket_path).st_mode
        except FileNotFoundError as exc:
            raise ValueError("algebra native runner socket does not exist") from exc
        if not stat.S_ISSOCK(mode):
            raise ValueError("algebra native runner path is not a Unix socket")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError(
                "algebra native runner timeout must be between 1 and 120 seconds"
            )
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def __call__(
        self,
        request: AlgebraNativeExecutionRequest,
    ) -> AlgebraNativeExecutionObservation:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        body = bytearray()
        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    "http://native-runner/v0/execute",
                    json=request.model_dump(mode="json"),
                ) as response:
                    if response.status_code != 200:
                        raise ValueError(
                            "algebra native runner returned a non-success status"
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith("application/json"):
                        raise ValueError(
                            "algebra native runner returned an unexpected content type"
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None and int(declared) > 256 * 1024:
                        raise ValueError(
                            "algebra native runner response exceeds 256 KiB"
                        )
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            raise ValueError(
                                "algebra native runner response exceeds 256 KiB"
                            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ValueError("algebra native runner request failed") from exc
        try:
            return AlgebraNativeExecutionObservation.model_validate_json(body)
        except Exception as exc:
            raise ValueError(
                "algebra native runner returned a malformed observation"
            ) from exc


async def execute_computation_native_plan(
    cases: Iterable[EngineSeedPlanCase],
    *,
    output: Path,
    executor: Callable[[NativeExecutionRequest], Awaitable[NativeExecutionObservation]],
    engine: Literal["webwork", "imathas"],
    run_id: str,
    webwork_engine_image_digest: str,
    imathas_engine_image_digest: str,
    imathas_adapter_image_digest: str,
    network_attestation_sha256: str,
    imathas_namespace: str,
    concurrency: int = 4,
    max_cases: int | None = None,
    manifest: ComputationEvaluationManifest | None = None,
) -> tuple[int, int]:
    """Execute sealed typed cases and append fully derived native receipts.

    The function is resumable by `(item_id, seed)`. It validates runner output
    against the request and exact pinned runtime identity before writing it.
    A complete run consists of separate 2,000-case WeBWorK and IMathAS calls
    whose ledgers are subsequently merged and reviewed by the receipt importer.
    """

    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    if max_cases is not None and max_cases < 1:
        raise ValueError("max_cases must be positive")
    if not _is_sha256(network_attestation_sha256):
        raise ValueError("network attestation must be a lowercase SHA-256")
    for digest in (
        webwork_engine_image_digest,
        imathas_engine_image_digest,
        imathas_adapter_image_digest,
    ):
        if not _is_image_digest(digest):
            raise ValueError("native engine identities require full image digests")
    if not re.fullmatch(r"acv0-canary-[a-zA-Z0-9_.-]{1,80}", imathas_namespace):
        raise ValueError("IMathAS namespace must be a disposable acv0 canary name")

    manifest = manifest or build_computation_evaluation_manifest()
    expected_plan = {
        (case.item_id, case.seed): case
        for case in build_computation_seed_plan(run_id, manifest=manifest)
        if case.engine == engine
    }
    supplied = [case for case in cases if case.engine == engine]
    supplied_keys = [(case.item_id, case.seed) for case in supplied]
    if len(supplied_keys) != len(set(supplied_keys)):
        raise ValueError("typed native execution plan contains duplicate cases")
    for case in supplied:
        expected = expected_plan.get((case.item_id, case.seed))
        if expected is None or case != expected:
            raise ValueError("typed native execution case differs from the sealed plan")

    completed: set[tuple[str, int]] = set()
    if output.exists():
        loaded = load_native_qualification_receipts(output)
        for receipt in loaded:
            if receipt.run_id != run_id:
                raise ValueError("existing native receipt ledger has a different run")
            completed.add((receipt.item_id, receipt.seed))
    pending = [case for case in supplied if (case.item_id, case.seed) not in completed]
    if max_cases is not None:
        pending = pending[:max_cases]

    twins = {twin.fixture_id: twin for twin in manifest.engine_twins}
    lineages = {
        lineage.lineage_id: lineage for lineage in manifest.parameterized_lineages
    }
    plan_sha256 = next(
        plan.plan_sha256
        for plan in manifest.seed_plans
        if plan.plan_id == "assessment-computation-v0"
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def execute_one(
        case: EngineSeedPlanCase,
    ) -> NativeQualificationReceipt:
        twin = twins[case.item_id]
        lineage = lineages[case.lineage_id]
        request = _build_native_execution_request(
            case,
            twin=twin,
            lineage=lineage,
            manifest=manifest,
            plan_sha256=plan_sha256,
            imathas_namespace=(imathas_namespace if case.engine == "imathas" else None),
        )
        async with semaphore:
            observation = await executor(request)
        expected_engine_digest = (
            webwork_engine_image_digest
            if case.engine == "webwork"
            else imathas_engine_image_digest
        )
        if (
            observation.request_sha256 != request.request_sha256
            or observation.item_id != case.item_id
            or observation.engine != case.engine
            or observation.seed != case.seed
            or observation.engine_image_digest != expected_engine_digest
            or observation.network_attestation_sha256 != network_attestation_sha256
            or (
                case.engine == "imathas"
                and (
                    observation.adapter_image_digest != imathas_adapter_image_digest
                    or observation.imathas_namespace != imathas_namespace
                )
            )
        ):
            raise ValueError("native runner observation changed sealed run identity")
        receipt = _native_receipt_from_observation(
            request,
            observation,
            twin=twin,
            lineage=lineage,
        )
        issue = _validate_native_receipt(
            receipt,
            twin,
            lineage,
            manifest_sha256=manifest.manifest_sha256,
            plan_sha256=plan_sha256,
            trust_policy=None,
        )
        if issue is not None:
            raise ValueError(
                f"{case.item_id}/{case.seed}: native observation failed: {issue}"
            )
        return receipt

    receipts = await asyncio.gather(*(execute_one(case) for case in pending))
    if receipts:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            for receipt in receipts:
                handle.write(receipt.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return len(pending), len(receipts)


async def execute_algebra_native_plan(
    requests: Iterable[AlgebraNativeExecutionRequest],
    *,
    output: Path,
    executor: Callable[
        [AlgebraNativeExecutionRequest],
        Awaitable[AlgebraNativeExecutionObservation],
    ],
    engine: Literal["webwork", "imathas"],
    run_id: str,
    webwork_engine_image_digest: str,
    imathas_engine_image_digest: str,
    imathas_adapter_image_digest: str,
    network_attestation_sha256: str,
    imathas_namespace: str,
    concurrency: int = 4,
    max_cases: int | None = None,
    manifest: ComputationEvaluationManifest | None = None,
) -> tuple[int, int]:
    """Execute one 20-case half of the exact sealed 40-plan algebra workflow."""

    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    if max_cases is not None and max_cases < 1:
        raise ValueError("max_cases must be positive")
    if not _is_sha256(network_attestation_sha256):
        raise ValueError("network attestation must be a lowercase SHA-256")
    for digest in (
        webwork_engine_image_digest,
        imathas_engine_image_digest,
        imathas_adapter_image_digest,
    ):
        if not _is_image_digest(digest):
            raise ValueError(
                "algebra native engine identities require full image digests"
            )
    if not re.fullmatch(r"acv0-canary-[a-zA-Z0-9_.-]{1,80}", imathas_namespace):
        raise ValueError("IMathAS namespace must be a disposable acv0 canary name")

    manifest = manifest or build_computation_evaluation_manifest()
    expected = build_algebra_native_execution_plan(
        run_id=run_id,
        imathas_namespace=imathas_namespace,
        manifest=manifest,
    )
    expected_by_id = {request.plan_id: request for request in expected}
    supplied = list(requests)
    supplied_ids = [request.plan_id for request in supplied]
    if (
        len(supplied) != 40
        or len(supplied_ids) != len(set(supplied_ids))
        or set(supplied_ids) != set(expected_by_id)
        or any(request != expected_by_id[request.plan_id] for request in supplied)
    ):
        raise ValueError(
            "algebra native execution input must be the exact sealed 40-request plan"
        )

    output_records: list[AlgebraNativeQualificationReceipt] = []
    if output.is_symlink():
        raise ValueError("algebra native receipt output cannot be a symlink")
    if output.exists():
        output_records = list(load_algebra_native_qualification_receipts(output))
    completed: set[str] = set()
    fixtures = {
        case.fixture_id: case
        for case in manifest.computation_cases
        if case.family == "algebraic"
    }
    plans = {plan.plan_id: plan for plan in manifest.algebra_native_plans}
    for receipt in output_records:
        expected_request = expected_by_id.get(receipt.plan_id)
        plan = plans.get(receipt.plan_id)
        if (
            expected_request is None
            or plan is None
            or receipt.plan_id in completed
            or receipt.run_id != run_id
            or receipt.request_sha256 != expected_request.request_sha256
            or receipt.network_attestation_sha256 != network_attestation_sha256
            or receipt.engine_image_digest
            != (
                webwork_engine_image_digest
                if receipt.engine == "webwork"
                else imathas_engine_image_digest
            )
            or (
                receipt.engine == "imathas"
                and (
                    receipt.adapter_image_digest != imathas_adapter_image_digest
                    or receipt.imathas_namespace != imathas_namespace
                )
            )
            or _validate_algebra_native_receipt(
                receipt,
                plan,
                fixtures[plan.fixture_id],
                manifest_sha256=manifest.manifest_sha256,
                trust_policy=None,
            )
            is not None
        ):
            raise ValueError(
                "existing algebra native receipt ledger does not match the sealed run"
            )
        completed.add(receipt.plan_id)

    pending = [
        request
        for request in expected
        if request.engine == engine and request.plan_id not in completed
    ]
    if max_cases is not None:
        pending = pending[:max_cases]
    semaphore = asyncio.Semaphore(concurrency)

    async def execute_one(
        request: AlgebraNativeExecutionRequest,
    ) -> AlgebraNativeQualificationReceipt:
        async with semaphore:
            observation = await executor(request)
        expected_engine_digest = (
            webwork_engine_image_digest
            if request.engine == "webwork"
            else imathas_engine_image_digest
        )
        if (
            observation.request_sha256 != request.request_sha256
            or observation.plan_id != request.plan_id
            or observation.engine != request.engine
            or observation.engine_image_digest != expected_engine_digest
            or observation.network_attestation_sha256 != network_attestation_sha256
            or (
                request.engine == "imathas"
                and (
                    observation.adapter_image_digest != imathas_adapter_image_digest
                    or observation.imathas_namespace != imathas_namespace
                )
            )
        ):
            raise ValueError(
                "algebra native runner observation changed sealed run identity"
            )
        receipt = _algebra_native_receipt_from_observation(request, observation)
        plan = plans[request.plan_id]
        issue = _validate_algebra_native_receipt(
            receipt,
            plan,
            fixtures[plan.fixture_id],
            manifest_sha256=manifest.manifest_sha256,
            trust_policy=None,
        )
        if issue is not None:
            raise ValueError(
                f"{request.plan_id}: algebra native observation failed: {issue}"
            )
        return receipt

    receipts = await asyncio.gather(*(execute_one(request) for request in pending))
    if receipts:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            for receipt in receipts:
                handle.write(receipt.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return len(pending), len(receipts)


def _algebra_native_receipt_from_observation(
    request: AlgebraNativeExecutionRequest,
    observation: AlgebraNativeExecutionObservation,
) -> AlgebraNativeQualificationReceipt:
    content: dict[str, Any] = {
        "schema_version": ALGEBRA_NATIVE_RECEIPT_SCHEMA_VERSION,
        "qualification_only": True,
        "run_id": request.run_id,
        "endpoint_profile": "isolated_canary_v0",
        "manifest_sha256": request.manifest_sha256,
        "request_sha256": request.request_sha256,
        "plan_id": request.plan_id,
        "plan_sha256": request.plan_sha256,
        "fixture_id": request.fixture_id,
        "fixture_sha256": request.fixture_sha256,
        "engine": request.engine,
        "operation": request.operation,
        "answer_kind": request.answer_kind,
        "source_contract": request.source_contract,
        "compiler_version": request.compiler_version,
        "source_sha256": request.source_sha256,
        "correct_submission_sha256": hashlib.sha256(
            request.correct_submission.encode("utf-8")
        ).hexdigest(),
        "alternate_correct_submission_sha256": (
            hashlib.sha256(
                request.alternate_correct_submission.encode("utf-8")
            ).hexdigest()
            if request.alternate_correct_submission is not None
            else None
        ),
        "wrong_submission_sha256": hashlib.sha256(
            request.wrong_submission.encode("utf-8")
        ).hexdigest(),
        "engine_image_digest": observation.engine_image_digest,
        "adapter_image_digest": observation.adapter_image_digest,
        "imathas_namespace": observation.imathas_namespace,
        "imathas_object_id": observation.imathas_object_id,
        "network_attestation_sha256": observation.network_attestation_sha256,
        "raw_engine_evidence_sha256": observation.raw_engine_evidence_sha256,
        "constraints_satisfied": observation.constraints_satisfied,
        "correct_answer_accepted": observation.correct_answer_accepted,
        "alternate_correct_answer_accepted": (
            observation.alternate_correct_answer_accepted
        ),
        "wrong_answer_rejected": observation.wrong_answer_rejected,
        "rendered": observation.rendered,
        "render_sha256": observation.render_sha256,
        "repeat_render_sha256": observation.repeat_render_sha256,
        "warnings": observation.warnings,
        "errors": observation.errors,
        "outbound_request_count": observation.outbound_request_count,
        "execution_status": "executed",
    }
    content["receipt_sha256"] = _sha256_json(content)
    return AlgebraNativeQualificationReceipt.model_validate(content)


def build_seed_plan_summaries(
    twins: Sequence[EngineTwinFixture] | None = None,
) -> list[EngineSeedPlanSummary]:
    baseline = build_seed_plan()
    if len(baseline) != EXECUTIONS_PER_PLAN:
        raise ValueError("the BUILD-08 compatibility plan must contain 4,000 cases")
    baseline_digest = _sha256_json([case.model_dump(mode="json") for case in baseline])

    if twins is None:
        manifest = build_computation_evaluation_manifest()
        twins = manifest.engine_twins
    computation_identity = [
        {
            "item_id": twin.fixture_id,
            "lineage_id": twin.lineage_id,
            "engine": twin.engine,
            "source_sha256": twin.source_sha256,
            "compiler_version": twin.compiler_version,
            "seed_start": 1,
            "seed_end": SEEDS_PER_ENGINE_ITEM,
        }
        for twin in twins
    ]
    if len(computation_identity) != 40:
        raise ValueError("the computation plan requires 40 engine twins")
    return [
        EngineSeedPlanSummary(
            plan_id="build08-compatibility",
            purpose="backward_compatibility",
            item_count=40,
            items_per_engine=20,
            seeds_per_item=100,
            planned_executions=4_000,
            execution_status="planned_not_executed",
            runtime_oracle="native_engine",
            plan_sha256=baseline_digest,
        ),
        EngineSeedPlanSummary(
            plan_id="assessment-computation-v0",
            purpose="typed_lineage_qualification",
            item_count=40,
            items_per_engine=20,
            seeds_per_item=100,
            planned_executions=4_000,
            execution_status="planned_not_executed",
            runtime_oracle="native_engine",
            plan_sha256=_sha256_json(computation_identity),
        ),
    ]


def run_offline_mutation_qualification(
    manifest: ComputationEvaluationManifest | None = None,
) -> OfflineMutationQualificationReport:
    """Materialize every curated mutation and prove local fail-closed detection.

    This runner deliberately performs no native-engine or network work. Semantic
    cases go through the real typed validator/compiler and safety cases go through
    the real strict request schema before the validator is allowed to see them.
    A missed mutation raises instead of producing a misleading qualification.
    """

    manifest = manifest or build_computation_evaluation_manifest()
    computation_by_id = {case.fixture_id: case for case in manifest.computation_cases}
    twins_by_id = {case.fixture_id: case for case in manifest.engine_twins}
    lineages_by_id = {
        lineage.lineage_id: lineage for lineage in manifest.parameterized_lineages
    }
    receipts: list[OfflineMutationReceipt] = []
    for mutation in manifest.mutations:
        if mutation.parent_fixture_id in computation_by_id:
            case = computation_by_id[mutation.parent_fixture_id]
            receipt = _execute_computation_mutation(mutation, case)
        else:
            twin = twins_by_id[mutation.parent_fixture_id]
            receipt = _execute_engine_mutation(
                mutation,
                twin,
                lineages_by_id[twin.lineage_id],
            )
        receipts.append(receipt)

    expected_ids = {mutation.mutation_id for mutation in manifest.mutations}
    observed_ids = {receipt.mutation_id for receipt in receipts}
    if len(receipts) != 200 or observed_ids != expected_ids:
        raise RuntimeError("offline mutation qualification did not cover all mutations")
    receipts_payload = [receipt.model_dump(mode="json") for receipt in receipts]
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-mutation-report-v0",
        "runner_version": OFFLINE_MUTATION_RUNNER_VERSION,
        "manifest_sha256": manifest.manifest_sha256,
        "execution_status": "executed_offline",
        "planned_mutations": 200,
        "executed_mutations": 200,
        "detected_mutations": 200,
        "network_calls": 0,
        "native_engine_executions": 0,
        "all_critical_mutations_detected": True,
        "receipts": receipts_payload,
        "receipts_sha256": _sha256_json(receipts_payload),
    }
    content["report_sha256"] = _sha256_json(content)
    return OfflineMutationQualificationReport.model_validate(content)


def build_lineage_blueprint(
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
) -> AssessmentComputationBlueprint:
    """Build the typed identity that a native receipt must bind exactly."""

    if (
        twin.lineage_id != lineage.lineage_id
        or twin.engine != twin.parameterized_spec.engine
    ):
        raise ValueError("engine twin and lineage do not match")
    blueprint = _lineage_blueprint_for_engine(lineage, twin.engine)
    result = compute_blueprint(blueprint)
    production_spec = parameterized_spec_from_blueprint(blueprint, result)
    if production_spec != twin.parameterized_spec:
        raise ValueError(
            "engine twin does not match the production typed computation adapter"
        )
    return blueprint


def _build_native_execution_request(
    case: EngineSeedPlanCase,
    *,
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
    manifest: ComputationEvaluationManifest,
    plan_sha256: str,
    imathas_namespace: str | None,
) -> NativeExecutionRequest:
    blueprint = build_lineage_blueprint(twin, lineage)
    result = compute_blueprint(blueprint)
    answer_expression, constraints = parameterized_typed_inputs(blueprint, result)
    compiled = compile_typed_parameterized_item(
        twin.parameterized_spec,
        answer_expression=answer_expression,
        constraints=constraints,
        validation_seeds=25,
        validation_seed_values=deterministic_seeds(blueprint),
    )
    if (
        compiled.compiler_version != case.compiler_version
        or compiled.source_sha256 != case.source_sha256
        or compiled.source_sha256 != twin.source_sha256
    ):
        raise ValueError("typed native source differs from the sealed fixture")
    content: dict[str, Any] = {
        "schema_version": NATIVE_EXECUTION_PROTOCOL_VERSION,
        "run_id": case.run_id,
        "manifest_sha256": manifest.manifest_sha256,
        "plan_sha256": plan_sha256,
        "item_id": case.item_id,
        "lineage_id": case.lineage_id,
        "engine": case.engine,
        "seed": case.seed,
        "compiler_version": case.compiler_version,
        "source_sha256": case.source_sha256,
        "engine_source": compiled.source,
        "parameterized_spec": twin.parameterized_spec.model_dump(mode="json"),
        "lineage_blueprint_sha256": canonical_blueprint_hash(blueprint),
        "imathas_namespace": imathas_namespace,
    }
    content["request_sha256"] = _sha256_json(content)
    return NativeExecutionRequest.model_validate(content)


def _native_receipt_from_observation(
    request: NativeExecutionRequest,
    observation: NativeExecutionObservation,
    *,
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
) -> NativeQualificationReceipt:
    observed_values = {
        name: value.model_dump(mode="json")
        for name, value in observation.engine_observed_values.items()
    }
    content: dict[str, Any] = {
        "schema_version": NATIVE_RECEIPT_SCHEMA_VERSION,
        "run_id": request.run_id,
        "endpoint_profile": "isolated_canary_v0",
        "manifest_sha256": request.manifest_sha256,
        "plan_sha256": request.plan_sha256,
        "item_id": request.item_id,
        "fixture_sha256": twin.fixture_sha256,
        "lineage_id": request.lineage_id,
        "lineage_sha256": lineage.lineage_sha256,
        "lineage_blueprint_sha256": request.lineage_blueprint_sha256,
        "engine": request.engine,
        "answer_kind": twin.answer_kind,
        "seed": request.seed,
        "compiler_version": request.compiler_version,
        "source_sha256": request.source_sha256,
        "engine_image_digest": observation.engine_image_digest,
        "adapter_image_digest": observation.adapter_image_digest,
        "imathas_namespace": observation.imathas_namespace,
        "imathas_object_id": observation.imathas_object_id,
        "network_attestation_sha256": observation.network_attestation_sha256,
        "engine_observed_values": observed_values,
        "engine_observed_values_sha256": _sha256_json(observed_values),
        "engine_observed_correct_answer": (observation.engine_observed_correct_answer),
        "engine_observed_wrong_answer": observation.engine_observed_wrong_answer,
        "constraints_satisfied": observation.constraints_satisfied,
        "correct_answer_accepted": observation.correct_answer_accepted,
        "wrong_answer_rejected": observation.wrong_answer_rejected,
        "rendered": observation.rendered,
        "render_sha256": observation.render_sha256,
        "repeat_render_sha256": observation.repeat_render_sha256,
        "warnings": observation.warnings,
        "errors": observation.errors,
        "outbound_request_count": observation.outbound_request_count,
        "execution_status": "executed",
    }
    content["receipt_sha256"] = _sha256_json(content)
    return NativeQualificationReceipt.model_validate(content)


def _lineage_blueprint_for_engine(
    lineage: ParameterizedLineage,
    engine: Literal["webwork", "imathas"],
) -> AssessmentComputationBlueprint:
    """Build the exact production blueprint for one typed engine lineage."""

    variables = [
        {
            "name": variable.name,
            "domain": "integer" if variable.integer else "real",
            "minimum": _numeric_expression_payload(variable.minimum),
            "maximum": _numeric_expression_payload(variable.maximum),
            "step": _numeric_expression_payload(variable.step),
        }
        for variable in lineage.variable_specs
    ]
    constraints = [
        {
            "operator": constraint["op"],
            "left": constraint["left"],
            "right": constraint["right"],
        }
        for constraint in lineage.constraints
    ]
    payload: dict[str, Any] = {
        "schema_version": "assessment-computation-v0",
        "profile": {
            "family": "unit" if lineage.units else "numeric",
            "delivery": engine,
        },
        "operation": "convert_unit" if lineage.units else "evaluate",
        "expression": lineage.expression.model_dump(mode="json"),
        "variables": variables,
        "constraints": constraints,
        "tolerance": {
            "absolute": _canonical_decimal(lineage.tolerance),
            "relative": "0",
        },
        "seed_count": 25,
    }
    if lineage.units:
        validate_unit_code(lineage.units)
        payload["source_unit"] = lineage.units
        payload["target_unit"] = lineage.units
    return AssessmentComputationBlueprint.model_validate(payload)


def load_native_qualification_receipts(
    path: Path,
) -> LoadedNativeReceiptLedger:
    """Load a bounded, local JSONL receipt ledger without invoking an engine."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("native receipt ledger must be an explicit local regular file")
    size = path.stat().st_size
    if size > MAX_RECEIPT_LEDGER_BYTES:
        raise ValueError("native receipt ledger exceeds 32 MiB")
    receipts: list[NativeQualificationReceipt] = []
    raw_hasher = hashlib.sha256()
    raw_byte_count = 0
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            raw_hasher.update(raw_line)
            raw_byte_count += len(raw_line)
            if len(raw_line) > MAX_RECEIPT_LINE_BYTES:
                raise ValueError(f"native receipt line {line_number} exceeds 256 KiB")
            if not raw_line.strip():
                continue
            if len(receipts) >= EXECUTIONS_PER_PLAN:
                raise ValueError("native receipt ledger contains more than 4,000 rows")
            try:
                receipts.append(
                    NativeQualificationReceipt.model_validate_json(raw_line)
                )
            except Exception as exc:
                raise ValueError(
                    f"native receipt line {line_number} is malformed"
                ) from exc
    if raw_byte_count != size:
        raise ValueError("native receipt ledger changed while it was imported")
    return LoadedNativeReceiptLedger(
        records=tuple(receipts),
        raw_ledger_sha256=raw_hasher.hexdigest(),
        raw_byte_count=raw_byte_count,
    )


def load_algebra_native_qualification_receipts(
    path: Path,
) -> LoadedAlgebraNativeReceiptLedger:
    """Load at most 40 qualification-only algebra receipts from local JSONL."""

    loaded = _load_bounded_jsonl(
        path,
        AlgebraNativeQualificationReceipt,
        label="algebra native receipt",
        max_records=40,
    )
    return LoadedAlgebraNativeReceiptLedger(
        records=tuple(loaded.records),
        raw_ledger_sha256=loaded.raw_ledger_sha256,
        raw_byte_count=loaded.raw_byte_count,
    )


def load_algebra_native_execution_plan(
    path: Path,
) -> LoadedAlgebraNativeExecutionPlan:
    """Import the exact bounded 40-request algebra plan from local JSONL."""

    loaded = _load_bounded_jsonl(
        path,
        AlgebraNativeExecutionRequest,
        label="algebra native execution plan",
        max_records=40,
    )
    records = tuple(loaded.records)
    if len(records) != 40:
        raise ValueError("algebra native execution plan must contain exactly 40 rows")
    if len({record.plan_id for record in records}) != 40:
        raise ValueError("algebra native execution plan contains duplicate plan IDs")
    return LoadedAlgebraNativeExecutionPlan(
        records=records,
        raw_ledger_sha256=loaded.raw_ledger_sha256,
        raw_byte_count=loaded.raw_byte_count,
    )


def load_native_qualification_trust_policy(
    path: Path,
) -> NativeQualificationTrustPolicy:
    if path.is_symlink() or not path.is_file():
        raise ValueError("native trust policy must be an explicit local regular file")
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("native trust policy exceeds 64 KiB")
    try:
        return NativeQualificationTrustPolicy.model_validate_json(raw)
    except Exception as exc:
        raise ValueError("native trust policy is malformed") from exc


def load_workflow_positive_receipts(path: Path) -> LoadedWorkflowPositiveLedger:
    loaded = _load_bounded_jsonl(
        path,
        WorkflowPositiveReceipt,
        label="workflow positive receipt",
        max_records=100,
    )
    return LoadedWorkflowPositiveLedger(
        records=tuple(loaded.records),
        raw_ledger_sha256=loaded.raw_ledger_sha256,
        raw_byte_count=loaded.raw_byte_count,
    )


def load_workflow_evidence_trust_policy(path: Path) -> WorkflowEvidenceTrustPolicy:
    return _load_bounded_json_record(
        path,
        WorkflowEvidenceTrustPolicy,
        label="workflow evidence trust policy",
    )


def build_workflow_observation_evidence(
    positive_receipts: LoadedWorkflowPositiveLedger,
    *,
    mutation_qualification: OfflineMutationQualificationReport,
    trust_policy: WorkflowEvidenceTrustPolicy,
    manifest: ComputationEvaluationManifest | None = None,
) -> WorkflowObservationEvidence:
    """Derive the 300 observations from raw positive and mutation receipts."""

    manifest = manifest or build_computation_evaluation_manifest()
    computation_cases = {case.fixture_id: case for case in manifest.computation_cases}
    engine_twins = {case.fixture_id: case for case in manifest.engine_twins}
    lineages = {
        lineage.lineage_id: lineage for lineage in manifest.parameterized_lineages
    }
    expected: dict[str, tuple[EvaluationSurface, str]] = {
        case.fixture_id: (EvaluationSurface(case.family), case.fixture_sha256)
        for case in manifest.computation_cases
    }
    expected.update(
        {
            case.fixture_id: (EvaluationSurface(case.engine), case.fixture_sha256)
            for case in manifest.engine_twins
        }
    )
    records = list(positive_receipts)
    by_id = {record.case_id: record for record in records}
    if len(records) != 100 or len(by_id) != 100 or set(by_id) != set(expected):
        raise ValueError(
            "workflow evidence requires one receipt for every positive surface"
        )
    if (
        trust_policy.manifest_sha256 != manifest.manifest_sha256
        or trust_policy.positive_receipt_ledger_raw_sha256
        != positive_receipts.raw_ledger_sha256
        or trust_policy.mutation_report_sha256 != mutation_qualification.report_sha256
        or mutation_qualification.manifest_sha256 != manifest.manifest_sha256
        or not mutation_qualification.all_critical_mutations_detected
    ):
        raise ValueError("workflow evidence does not match its reviewed policy")
    for record in records:
        surface, fixture_sha256 = expected[record.case_id]
        if (
            record.run_id != trust_policy.run_id
            or record.manifest_sha256 != manifest.manifest_sha256
            or record.runner_version != trust_policy.runner_version
            or record.transport != trust_policy.transport
            or record.computation_runtime_manifest_sha256
            != trust_policy.computation_runtime_manifest_sha256
            or record.surface != surface
            or record.fixture_sha256 != fixture_sha256
        ):
            raise ValueError(
                f"{record.case_id}: workflow receipt identity does not match"
            )
        if record.case_id in computation_cases:
            case = computation_cases[record.case_id]
            if record.validation_request.blueprint != case.blueprint:
                raise ValueError(
                    f"{record.case_id}: workflow blueprint differs from fixture"
                )
            oracle_match = _computation_fixture_oracle_match(
                case,
                record.compute_result,
                record.workflow_report,
            )
        else:
            twin = engine_twins[record.case_id]
            oracle_match = _engine_workflow_oracle_match(
                twin,
                lineages[twin.lineage_id],
                record,
            )
        if record.oracle_match != oracle_match:
            raise ValueError(
                f"{record.case_id}: workflow oracle claim does not match evidence"
            )

    positives = [
        ComputationEvaluationObservation(
            case_id=record.case_id,
            surface=record.surface,
            kind=MutationKind.POSITIVE,
            state=record.state,
            oracle_match=record.oracle_match,
            critical_defect_detected=False,
            deterministic_replay=record.deterministic_replay,
        )
        for record in sorted(records, key=lambda value: value.case_id)
    ]
    mutation_by_id = {mutation.mutation_id: mutation for mutation in manifest.mutations}
    mutation_receipts = {
        receipt.mutation_id: receipt for receipt in mutation_qualification.receipts
    }
    if set(mutation_receipts) != set(mutation_by_id):
        raise ValueError("mutation report does not cover the sealed manifest")
    mutations = [
        ComputationEvaluationObservation(
            case_id=receipt.mutation_id,
            surface=receipt.surface,
            kind=receipt.kind,
            state=receipt.observed_state,
            oracle_match=False,
            critical_defect_detected=receipt.detected,
            deterministic_replay=True,
        )
        for receipt in sorted(
            mutation_qualification.receipts,
            key=lambda value: value.mutation_id,
        )
    ]
    observations = positives + mutations
    observation_payload = [
        observation.model_dump(mode="json") for observation in observations
    ]
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-workflow-observations-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "runner_version": trust_policy.runner_version,
        "transport": trust_policy.transport,
        "computation_runtime_manifest_sha256": (
            trust_policy.computation_runtime_manifest_sha256
        ),
        "observations": observation_payload,
        "positive_workflow_receipt_sha256s": {
            record.case_id: record.receipt_sha256
            for record in sorted(records, key=lambda value: value.case_id)
        },
        "positive_receipt_ledger_raw_sha256": (positive_receipts.raw_ledger_sha256),
        "positive_receipt_ledger_raw_byte_count": positive_receipts.raw_byte_count,
        "trust_policy_sha256": trust_policy.policy_sha256,
        "trust_policy_applied": True,
        "mutation_report_sha256": mutation_qualification.report_sha256,
        "operator_attestation_sha256": trust_policy.operator_attestation_sha256,
        "observations_sha256": _sha256_json(observation_payload),
    }
    content["evidence_sha256"] = _sha256_json(content)
    return WorkflowObservationEvidence.model_validate(content)


def build_qualification_merged_observation_evidence(
    workflow_evidence: WorkflowObservationEvidence,
    *,
    native_qualification: NativeQualificationReport,
    algebra_native_qualification: AlgebraNativeQualificationReport,
    manifest: ComputationEvaluationManifest | None = None,
) -> QualificationMergedObservationEvidence:
    """Upgrade only evaluation observations backed by complete native evidence.

    The raw service reports remain unchanged. In particular, qualification-only
    solution-set receipts do not become production learner-delivery support.
    """

    manifest = manifest or build_computation_evaluation_manifest()
    if (
        workflow_evidence.manifest_sha256 != manifest.manifest_sha256
        or native_qualification.manifest_sha256 != manifest.manifest_sha256
        or algebra_native_qualification.manifest_sha256 != manifest.manifest_sha256
        or not native_qualification.qualified
        or not algebra_native_qualification.qualified
        or algebra_native_qualification.production_delivery_enabled
        or not _native_reports_share_runtime_identity(
            native_qualification,
            algebra_native_qualification,
        )
    ):
        raise ValueError(
            "qualified observation merge requires matching passed native reports"
        )
    raw_observations = {
        observation.case_id: observation
        for observation in workflow_evidence.observations
    }
    algebra_ids = {
        case.fixture_id
        for case in manifest.computation_cases
        if case.family == "algebraic"
    }
    engine_ids = {case.fixture_id for case in manifest.engine_twins}
    upgraded_ids = algebra_ids | engine_ids
    if (
        len(algebra_ids) != 20
        or len(engine_ids) != 40
        or len(upgraded_ids) != 60
        or not upgraded_ids <= set(raw_observations)
    ):
        raise ValueError("qualified observation merge target matrix is incomplete")

    upgraded_report_hashes = {
        case_id: (
            algebra_native_qualification.report_sha256
            if case_id in algebra_ids
            else native_qualification.report_sha256
        )
        for case_id in sorted(upgraded_ids)
    }
    derived: list[ComputationEvaluationObservation] = []
    for observation in workflow_evidence.observations:
        if observation.case_id not in upgraded_ids:
            derived.append(observation)
            continue
        if (
            observation.kind != MutationKind.POSITIVE
            or observation.state != ObservedValidationState.PARTIALLY_VALIDATED
            or not observation.oracle_match
            or not observation.deterministic_replay
        ):
            raise ValueError(
                f"{observation.case_id}: native merge requires an honest partial "
                "positive workflow observation"
            )
        derived.append(
            observation.model_copy(update={"state": ObservedValidationState.VALIDATED})
        )

    observation_payload = [
        observation.model_dump(mode="json") for observation in derived
    ]
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-qualified-observations-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "workflow_evidence_sha256": workflow_evidence.evidence_sha256,
        "workflow_observations_sha256": workflow_evidence.observations_sha256,
        "native_report_sha256": native_qualification.report_sha256,
        "algebra_native_report_sha256": (algebra_native_qualification.report_sha256),
        "upgraded_case_report_sha256s": upgraded_report_hashes,
        "observations": observation_payload,
        "observations_sha256": _sha256_json(observation_payload),
        "upgraded_positive_count": 60,
        "solution_set_production_delivery_enabled": False,
    }
    content["evidence_sha256"] = _sha256_json(content)
    return QualificationMergedObservationEvidence.model_validate(content)


def _native_reports_share_runtime_identity(
    native_qualification: NativeQualificationReport,
    algebra_native_qualification: AlgebraNativeQualificationReport,
) -> bool:
    return bool(
        native_qualification.qualification_run_id
        == algebra_native_qualification.qualification_run_id
        and native_qualification.endpoint_profile
        == algebra_native_qualification.endpoint_profile
        and native_qualification.webwork_engine_image_digest
        == algebra_native_qualification.webwork_engine_image_digest
        and native_qualification.imathas_engine_image_digest
        == algebra_native_qualification.imathas_engine_image_digest
        and native_qualification.imathas_adapter_image_digest
        == algebra_native_qualification.imathas_adapter_image_digest
        and native_qualification.network_attestation_sha256
        == algebra_native_qualification.network_attestation_sha256
        and native_qualification.imathas_namespace
        == algebra_native_qualification.imathas_namespace
    )


def validate_native_qualification_receipts(
    receipts: Iterable[NativeQualificationReceipt] | LoadedNativeReceiptLedger,
    *,
    manifest: ComputationEvaluationManifest | None = None,
    trust_policy: NativeQualificationTrustPolicy | None = None,
) -> NativeQualificationReport:
    """Validate exact native evidence while keeping absent evidence `not_run`."""

    manifest = manifest or build_computation_evaluation_manifest()
    loaded_ledger = (
        receipts if isinstance(receipts, LoadedNativeReceiptLedger) else None
    )
    records = list(receipts)
    raw_ledger_sha256 = (
        loaded_ledger.raw_ledger_sha256 if loaded_ledger is not None else None
    )
    raw_byte_count = loaded_ledger.raw_byte_count if loaded_ledger is not None else None
    twins = {twin.fixture_id: twin for twin in manifest.engine_twins}
    lineages = {
        lineage.lineage_id: lineage for lineage in manifest.parameterized_lineages
    }
    computation_plan_sha256 = next(
        plan.plan_sha256
        for plan in manifest.seed_plans
        if plan.plan_id == "assessment-computation-v0"
    )
    expected_keys = {
        (twin.fixture_id, seed)
        for twin in manifest.engine_twins
        for seed in range(1, SEEDS_PER_ENGINE_ITEM + 1)
    }
    seen: set[tuple[str, int]] = set()
    valid_keys: set[tuple[str, int]] = set()
    issues: list[NativeQualificationIssue] = []
    duplicate_count = 0
    unexpected_count = 0

    for receipt in records:
        key = (receipt.item_id, receipt.seed)
        if key in seen:
            duplicate_count += 1
            issues.append(
                NativeQualificationIssue(
                    item_id=receipt.item_id,
                    seed=receipt.seed,
                    code="duplicate_receipt",
                )
            )
            continue
        seen.add(key)
        if key not in expected_keys:
            unexpected_count += 1
            issues.append(
                NativeQualificationIssue(
                    item_id=receipt.item_id,
                    seed=receipt.seed,
                    code="unexpected_receipt",
                )
            )
            continue
        twin = twins[receipt.item_id]
        issue_code = _validate_native_receipt(
            receipt,
            twin,
            lineages[twin.lineage_id],
            manifest_sha256=manifest.manifest_sha256,
            plan_sha256=computation_plan_sha256,
            trust_policy=trust_policy,
        )
        if issue_code is None:
            valid_keys.add(key)
        else:
            issues.append(
                NativeQualificationIssue(
                    item_id=receipt.item_id,
                    seed=receipt.seed,
                    code=issue_code,
                )
            )

    invalid_count = len(records) - len(valid_keys) - duplicate_count - unexpected_count
    if records and trust_policy is None and len(issues) < EXECUTIONS_PER_PLAN:
        first = records[0]
        issues.append(
            NativeQualificationIssue(
                item_id=first.item_id,
                seed=first.seed,
                code="operator_trust_policy_missing",
            )
        )
    if records and loaded_ledger is None and len(issues) < EXECUTIONS_PER_PLAN:
        first = records[0]
        issues.append(
            NativeQualificationIssue(
                item_id=first.item_id,
                seed=first.seed,
                code="raw_receipt_ledger_unbound",
            )
        )
    if (
        records
        and trust_policy is not None
        and raw_ledger_sha256 != trust_policy.receipt_ledger_raw_sha256
        and len(issues) < EXECUTIONS_PER_PLAN
    ):
        first = records[0]
        issues.append(
            NativeQualificationIssue(
                item_id=first.item_id,
                seed=first.seed,
                code="raw_receipt_ledger_hash_mismatch",
            )
        )
    if records and trust_policy is not None:
        mixed_identity = len({receipt.run_id for receipt in records}) != 1
        for engine in ("webwork", "imathas"):
            engine_identity = {
                (
                    receipt.engine_image_digest,
                    receipt.adapter_image_digest,
                    receipt.network_attestation_sha256,
                )
                for receipt in records
                if receipt.engine == engine
            }
            mixed_identity = mixed_identity or len(engine_identity) > 1
        if mixed_identity and len(issues) < EXECUTIONS_PER_PLAN:
            first = records[0]
            issues.append(
                NativeQualificationIssue(
                    item_id=first.item_id,
                    seed=first.seed,
                    code="mixed_run_identity",
                )
            )
        imathas_receipts = [
            receipt for receipt in records if receipt.engine == "imathas"
        ]
        if imathas_receipts:
            namespaces = {receipt.imathas_namespace for receipt in imathas_receipts}
            objects_by_item = {
                receipt.item_id: {
                    candidate.imathas_object_id
                    for candidate in imathas_receipts
                    if candidate.item_id == receipt.item_id
                }
                for receipt in imathas_receipts
            }
            object_ids = {
                next(iter(object_ids))
                for object_ids in objects_by_item.values()
                if len(object_ids) == 1
            }
            lifecycle_invalid = (
                namespaces != {trust_policy.imathas_namespace}
                or any(len(object_ids) != 1 for object_ids in objects_by_item.values())
                or len(object_ids) != len(objects_by_item)
            )
            if lifecycle_invalid and len(issues) < EXECUTIONS_PER_PLAN:
                first = imathas_receipts[0]
                issues.append(
                    NativeQualificationIssue(
                        item_id=first.item_id,
                        seed=first.seed,
                        code="imathas_namespace_lifecycle_mismatch",
                    )
                )
    missing_count = len(expected_keys - valid_keys)
    if not records:
        execution_status = "not_run"
    elif (
        len(records) == EXECUTIONS_PER_PLAN
        and len(valid_keys) == EXECUTIONS_PER_PLAN
        and trust_policy is not None
        and loaded_ledger is not None
        and raw_ledger_sha256 == trust_policy.receipt_ledger_raw_sha256
        and not issues
        and not duplicate_count
        and not unexpected_count
    ):
        execution_status = "passed"
    elif issues:
        execution_status = "failed"
    else:
        execution_status = "partial"
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-native-report-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "expected_receipts": EXECUTIONS_PER_PLAN,
        "imported_receipts": len(records),
        "valid_receipts": len(valid_keys),
        "invalid_receipts": invalid_count,
        "missing_receipts": missing_count,
        "duplicate_receipts": duplicate_count,
        "unexpected_receipts": unexpected_count,
        "execution_status": execution_status,
        "qualified": execution_status == "passed",
        "trust_policy_sha256": (
            trust_policy.policy_sha256 if trust_policy is not None else None
        ),
        "trust_policy_applied": trust_policy is not None,
        "qualification_run_id": (
            trust_policy.run_id if trust_policy is not None else None
        ),
        "endpoint_profile": (
            trust_policy.endpoint_profile if trust_policy is not None else None
        ),
        "webwork_engine_image_digest": (
            trust_policy.webwork_engine_image_digest
            if trust_policy is not None
            else None
        ),
        "imathas_engine_image_digest": (
            trust_policy.imathas_engine_image_digest
            if trust_policy is not None
            else None
        ),
        "imathas_adapter_image_digest": (
            trust_policy.imathas_adapter_image_digest
            if trust_policy is not None
            else None
        ),
        "network_attestation_sha256": (
            trust_policy.network_attestation_sha256
            if trust_policy is not None
            else None
        ),
        "raw_receipt_ledger_sha256": raw_ledger_sha256,
        "raw_receipt_ledger_byte_count": raw_byte_count,
        "imathas_namespace": (
            trust_policy.imathas_namespace if trust_policy is not None else None
        ),
        "imathas_namespace_cleanup_status": (
            trust_policy.imathas_namespace_cleanup_status
            if trust_policy is not None
            else None
        ),
        "imathas_namespace_cleanup_attestation_sha256": (
            trust_policy.imathas_namespace_cleanup_attestation_sha256
            if trust_policy is not None
            else None
        ),
        "receipt_ledger_sha256": _sha256_json(
            [record.model_dump(mode="json") for record in records]
        ),
        "imported_execution_claims": len(records),
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "limitations": [
            "Imported receipts are execution claims, not independently observed executions.",
            "The report binds exact JSONL bytes and a disposable IMathAS namespace lifecycle; hashes provide integrity, not observer authenticity.",
            "Operator trust comes from external review of the pinned canary policy and the final source-controlled promotion.",
        ],
    }
    content["report_sha256"] = _sha256_json(content)
    return NativeQualificationReport.model_validate(content)


def validate_algebra_native_qualification_receipts(
    receipts: (
        Iterable[AlgebraNativeQualificationReceipt] | LoadedAlgebraNativeReceiptLedger
    ),
    *,
    manifest: ComputationEvaluationManifest | None = None,
    trust_policy: NativeQualificationTrustPolicy | None = None,
) -> AlgebraNativeQualificationReport:
    """Fail closed unless all 40 sealed algebra plans have trusted receipts."""

    manifest = manifest or build_computation_evaluation_manifest()
    loaded_ledger = (
        receipts if isinstance(receipts, LoadedAlgebraNativeReceiptLedger) else None
    )
    records = list(receipts)
    if len(records) > 40:
        raise ValueError("algebra native receipt ledger contains more than 40 rows")
    raw_ledger_sha256 = (
        loaded_ledger.raw_ledger_sha256 if loaded_ledger is not None else None
    )
    raw_byte_count = loaded_ledger.raw_byte_count if loaded_ledger is not None else None
    plans = {plan.plan_id: plan for plan in manifest.algebra_native_plans}
    fixtures = {
        case.fixture_id: case
        for case in manifest.computation_cases
        if case.family == "algebraic"
    }
    seen: set[str] = set()
    valid: set[str] = set()
    issues: list[AlgebraNativeQualificationIssue] = []
    duplicate_count = 0
    unexpected_count = 0

    for receipt in records:
        if receipt.plan_id in seen:
            duplicate_count += 1
            issues.append(
                AlgebraNativeQualificationIssue(
                    plan_id=receipt.plan_id,
                    code="duplicate_receipt",
                )
            )
            continue
        seen.add(receipt.plan_id)
        plan = plans.get(receipt.plan_id)
        if plan is None:
            unexpected_count += 1
            issues.append(
                AlgebraNativeQualificationIssue(
                    plan_id=receipt.plan_id,
                    code="unexpected_receipt",
                )
            )
            continue
        issue_code = _validate_algebra_native_receipt(
            receipt,
            plan,
            fixtures[plan.fixture_id],
            manifest_sha256=manifest.manifest_sha256,
            trust_policy=trust_policy,
        )
        if issue_code is None:
            valid.add(receipt.plan_id)
        else:
            issues.append(
                AlgebraNativeQualificationIssue(
                    plan_id=receipt.plan_id,
                    code=issue_code,
                )
            )

    marker_plan_id = records[0].plan_id if records else "acv0-algebra-webwork-01"
    if records and trust_policy is None:
        issues.append(
            AlgebraNativeQualificationIssue(
                plan_id=marker_plan_id,
                code="operator_trust_policy_missing",
            )
        )
    if records and loaded_ledger is None:
        issues.append(
            AlgebraNativeQualificationIssue(
                plan_id=marker_plan_id,
                code="raw_receipt_ledger_unbound",
            )
        )
    if (
        records
        and trust_policy is not None
        and raw_ledger_sha256 != trust_policy.receipt_ledger_raw_sha256
    ):
        issues.append(
            AlgebraNativeQualificationIssue(
                plan_id=marker_plan_id,
                code="raw_receipt_ledger_hash_mismatch",
            )
        )
    if records and trust_policy is not None:
        runtime_identities = {
            engine: {
                (
                    receipt.engine_image_digest,
                    receipt.adapter_image_digest,
                    receipt.network_attestation_sha256,
                )
                for receipt in records
                if receipt.engine == engine
            }
            for engine in ("webwork", "imathas")
        }
        if len({receipt.run_id for receipt in records}) != 1 or any(
            len(identities) > 1 for identities in runtime_identities.values()
        ):
            issues.append(
                AlgebraNativeQualificationIssue(
                    plan_id=marker_plan_id,
                    code="mixed_run_identity",
                )
            )
        imathas_receipts = [
            receipt for receipt in records if receipt.engine == "imathas"
        ]
        if imathas_receipts and (
            {receipt.imathas_namespace for receipt in imathas_receipts}
            != {trust_policy.imathas_namespace}
            or len({receipt.imathas_object_id for receipt in imathas_receipts})
            != len(imathas_receipts)
        ):
            issues.append(
                AlgebraNativeQualificationIssue(
                    plan_id=imathas_receipts[0].plan_id,
                    code="imathas_namespace_lifecycle_mismatch",
                )
            )

    invalid_count = len(records) - len(valid) - duplicate_count - unexpected_count
    missing_count = len(set(plans) - valid)
    if not records:
        execution_status = "not_run"
    elif (
        len(records) == 40
        and len(valid) == 40
        and trust_policy is not None
        and loaded_ledger is not None
        and raw_ledger_sha256 == trust_policy.receipt_ledger_raw_sha256
        and not issues
    ):
        execution_status = "passed"
    elif issues:
        execution_status = "failed"
    else:
        execution_status = "partial"
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-algebra-native-report-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "expected_receipts": 40,
        "imported_receipts": len(records),
        "valid_receipts": len(valid),
        "invalid_receipts": invalid_count,
        "missing_receipts": missing_count,
        "duplicate_receipts": duplicate_count,
        "unexpected_receipts": unexpected_count,
        "execution_status": execution_status,
        "qualified": execution_status == "passed",
        "production_delivery_enabled": False,
        "trust_policy_sha256": (
            trust_policy.policy_sha256 if trust_policy is not None else None
        ),
        "trust_policy_applied": trust_policy is not None,
        "qualification_run_id": (
            trust_policy.run_id if trust_policy is not None else None
        ),
        "endpoint_profile": (
            trust_policy.endpoint_profile if trust_policy is not None else None
        ),
        "webwork_engine_image_digest": (
            trust_policy.webwork_engine_image_digest
            if trust_policy is not None
            else None
        ),
        "imathas_engine_image_digest": (
            trust_policy.imathas_engine_image_digest
            if trust_policy is not None
            else None
        ),
        "imathas_adapter_image_digest": (
            trust_policy.imathas_adapter_image_digest
            if trust_policy is not None
            else None
        ),
        "network_attestation_sha256": (
            trust_policy.network_attestation_sha256
            if trust_policy is not None
            else None
        ),
        "imathas_namespace": (
            trust_policy.imathas_namespace if trust_policy is not None else None
        ),
        "imathas_namespace_cleanup_status": (
            trust_policy.imathas_namespace_cleanup_status
            if trust_policy is not None
            else None
        ),
        "imathas_namespace_cleanup_attestation_sha256": (
            trust_policy.imathas_namespace_cleanup_attestation_sha256
            if trust_policy is not None
            else None
        ),
        "raw_receipt_ledger_sha256": raw_ledger_sha256,
        "raw_receipt_ledger_byte_count": raw_byte_count,
        "receipt_ledger_sha256": _sha256_json(
            [record.model_dump(mode="json") for record in records]
        ),
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "limitations": [
            "Numeric and formula receipts exercise exact production compiler sources, but this report alone does not enable learner delivery.",
            "Bounded solve receipts exercise a qualification-only solution-set helper outside the numeric-or-formula production contract.",
            "Source grounding, pedagogy, accessibility, and human approval remain outside this native grader evidence.",
        ],
    }
    content["report_sha256"] = _sha256_json(content)
    return AlgebraNativeQualificationReport.model_validate(content)


def _validate_algebra_native_receipt(
    receipt: AlgebraNativeQualificationReceipt,
    plan: AlgebraNativePlanCase,
    case: ComputationFixture,
    *,
    manifest_sha256: str,
    trust_policy: NativeQualificationTrustPolicy | None,
) -> str | None:
    expected_request = _build_algebra_native_execution_request(
        plan,
        case,
        run_id=receipt.run_id,
        manifest_sha256=manifest_sha256,
        imathas_namespace=receipt.imathas_namespace,
    )
    expected_identity = (
        receipt.manifest_sha256 == manifest_sha256
        and receipt.request_sha256 == expected_request.request_sha256
        and receipt.plan_sha256 == plan.plan_sha256
        and receipt.fixture_id == plan.fixture_id
        and receipt.fixture_sha256 == plan.fixture_sha256
        and receipt.engine == plan.engine
        and receipt.operation == plan.operation
        and receipt.answer_kind == plan.answer_kind
        and receipt.source_contract == plan.source_contract
        and receipt.compiler_version == plan.compiler_version
        and receipt.source_sha256 == plan.source_sha256
        and receipt.correct_submission_sha256 == plan.correct_submission_sha256
        and receipt.alternate_correct_submission_sha256
        == plan.alternate_correct_submission_sha256
        and receipt.wrong_submission_sha256 == plan.wrong_submission_sha256
    )
    if not expected_identity:
        return "sealed_plan_mismatch"
    if not receipt.constraints_satisfied:
        return "constraint_mismatch"
    if (
        not receipt.correct_answer_accepted
        or (
            receipt.answer_kind != "numeric"
            and not receipt.alternate_correct_answer_accepted
        )
        or not receipt.wrong_answer_rejected
    ):
        return "native_grading_mismatch"
    if not receipt.rendered or receipt.render_sha256 != receipt.repeat_render_sha256:
        return "repeat_render_mismatch"
    if receipt.warnings or receipt.errors:
        return "native_warning_or_error"
    if trust_policy is not None and (
        receipt.run_id != trust_policy.run_id
        or receipt.endpoint_profile != trust_policy.endpoint_profile
        or receipt.network_attestation_sha256 != trust_policy.network_attestation_sha256
        or (
            receipt.engine == "webwork"
            and receipt.engine_image_digest != trust_policy.webwork_engine_image_digest
        )
        or (
            receipt.engine == "imathas"
            and (
                receipt.engine_image_digest != trust_policy.imathas_engine_image_digest
                or receipt.adapter_image_digest
                != trust_policy.imathas_adapter_image_digest
                or receipt.imathas_namespace != trust_policy.imathas_namespace
            )
        )
    ):
        return "runtime_identity_mismatch"
    return None


def load_ucum_artifact_equivalence_attestation(
    path: Path,
) -> LoadedUcumArtifactEquivalenceAttestation:
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            "UCUM artifact equivalence attestation must be an explicit local "
            "regular file"
        )
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("UCUM artifact equivalence attestation exceeds 64 KiB")
    try:
        record = UcumArtifactEquivalenceAttestation.model_validate(
            _strict_json_value(raw)
        )
    except Exception as exc:
        raise ValueError("UCUM artifact equivalence attestation is malformed") from exc
    return LoadedUcumArtifactEquivalenceAttestation(
        record=record,
        raw_attestation_sha256=hashlib.sha256(raw).hexdigest(),
        raw_byte_count=len(raw),
    )


def qualify_ucum_subset(
    artifact_path: Path | None = None,
    *,
    expected_sha256: str | None = None,
    equivalence_attestation: (LoadedUcumArtifactEquivalenceAttestation | None) = None,
    manifest: ComputationEvaluationManifest | None = None,
) -> UcumQualificationReport:
    """Qualify only the named unit subset from a local, checksum-pinned XML file.

    The harness never downloads an artifact. An absent artifact is honestly
    reported as `not_run`; a supplied artifact must have an explicit expected
    checksum before any XML is parsed or any functional case is executed.
    """

    manifest = manifest or build_computation_evaluation_manifest()
    corpus_passed = _run_libretexts_unit_corpus(manifest)
    runtime_verified, runtime_hashes = _verify_ucum_runtime_integrity()
    base: dict[str, Any] = {
        "schema_version": UCUM_QUALIFICATION_SCHEMA_VERSION,
        "profile": UCUM_PROFILE,
        "ucum_version": "2.2",
        "essence_sha256": UCUM_ESSENCE_SHA256,
        "ucumvert_grammar_sha256": UCUMVERT_GRAMMAR_SHA256,
        "pint_ucum_definitions_sha256": PINT_UCUM_DEFINITIONS_SHA256,
        "ucumvert_wheel_sha256": UCUMVERT_WHEEL_SHA256,
        "pint_wheel_sha256": PINT_WHEEL_SHA256,
        "runtime_integrity_status": "verified" if runtime_verified else "failed",
        "runtime_asset_hashes_observed": runtime_hashes,
        "named_subset": sorted(LIBRETEXTS_UCUM_CODES),
        "artifact_identity": "absent",
        "artifact_byte_count": 0,
        "artifact_sha256_expected": expected_sha256,
        "artifact_sha256_observed": None,
        "official_cases_discovered": 0,
        "artifact_section_counts": {},
        "selection_revision": UCUM_SELECTION_REVISION,
        "pinned_subset_expected_cases": PINNED_UCUM_SUBSET_CASES,
        "subset_cases_selected": 0,
        "subset_cases_executed": 0,
        "subset_cases_passed": 0,
        "libretexts_unit_corpus_cases": 20,
        "libretexts_unit_corpus_passed": corpus_passed,
        "subset_qualified": False,
        "official_functional_test_conformance_claimed": False,
        "full_ucum_conformance_claimed": False,
        "official_attachment_equivalence": "unverified",
        "equivalence_attestation_sha256": None,
        "equivalence_attestation_raw_sha256": None,
        "equivalence_attestation_raw_byte_count": None,
        "equivalence_reviewer_subject_sha256": None,
        "network_calls": 0,
        "case_receipts": [],
        "limitations": [
            "Qualification is limited to the named LibreTexts education subset.",
            "Passing selected cases is not a claim of full UCUM conformance.",
            "The harness consumes only a caller-supplied checksum-pinned local artifact.",
            "Wheel hashes are qualification pins; runtime verification separately checks installed versions and behavior-controlling assets.",
        ],
    }
    if artifact_path is None:
        if expected_sha256 is not None or equivalence_attestation is not None:
            raise ValueError(
                "UCUM checksum and equivalence evidence require a local artifact"
            )
        base.update(
            {
                "artifact_status": "not_supplied",
                "official_functional_tests_status": "not_run",
                "qualification_status": "not_run",
            }
        )
        return _build_ucum_report(base)
    if expected_sha256 is None or not _is_sha256(expected_sha256):
        raise ValueError("a supplied UCUM artifact requires an exact SHA-256 pin")
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise ValueError("UCUM artifact must be an explicit local regular file")
    raw = artifact_path.read_bytes()
    if len(raw) > MAX_UCUM_ARTIFACT_BYTES:
        raise ValueError("UCUM functional-test artifact exceeds 1 MiB")
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    base["artifact_byte_count"] = len(raw)
    base["artifact_sha256_observed"] = observed_sha256
    if observed_sha256 != expected_sha256:
        base.update(
            {
                "artifact_status": "checksum_mismatch",
                "official_functional_tests_status": "not_run",
                "qualification_status": "failed",
            }
        )
        return _build_ucum_report(base)
    equivalence_verified = False
    if equivalence_attestation is not None:
        attestation = equivalence_attestation.record
        if (
            attestation.local_artifact_sha256 != observed_sha256
            or attestation.official_attachment_sha256 != observed_sha256
            or attestation.local_artifact_byte_count != len(raw)
            or attestation.official_attachment_byte_count != len(raw)
        ):
            raise ValueError(
                "UCUM equivalence attestation does not match the local artifact"
            )
        equivalence_verified = True
        base.update(
            {
                "official_attachment_equivalence": "verified",
                "equivalence_attestation_sha256": (attestation.attestation_sha256),
                "equivalence_attestation_raw_sha256": (
                    equivalence_attestation.raw_attestation_sha256
                ),
                "equivalence_attestation_raw_byte_count": (
                    equivalence_attestation.raw_byte_count
                ),
                "equivalence_reviewer_subject_sha256": (
                    attestation.reviewer_subject_sha256
                ),
            }
        )
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        base.update(
            {
                "artifact_status": "invalid",
                "official_functional_tests_status": "not_run",
                "qualification_status": "failed",
            }
        )
        return _build_ucum_report(base)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        base.update(
            {
                "artifact_status": "invalid",
                "official_functional_tests_status": "not_run",
                "qualification_status": "failed",
            }
        )
        return _build_ucum_report(base)

    root_tag = root.tag.rsplit("}", maxsplit=1)[-1]
    element_count, maximum_depth = _xml_stats(root)
    if element_count > 10_000 or maximum_depth > 32:
        base.update(
            {
                "artifact_status": "invalid",
                "official_functional_tests_status": "not_run",
                "qualification_status": "failed",
            }
        )
        return _build_ucum_report(base)
    section_counts = _ucum_section_counts(root)
    base["artifact_section_counts"] = section_counts
    pinned_artifact = observed_sha256 == PINNED_UCUM_FUNCTIONAL_TEST_SHA256
    base["artifact_identity"] = (
        "pinned_ucum_java_mirror"
        if pinned_artifact
        else "unrecognized_checksum_pinned_artifact"
    )
    if pinned_artifact and (
        len(raw) != PINNED_UCUM_FUNCTIONAL_TEST_BYTES
        or root_tag != "ucumTests"
        or section_counts != PINNED_UCUM_SECTION_COUNTS
    ):
        base.update(
            {
                "artifact_status": "invalid",
                "official_functional_tests_status": "not_run",
                "qualification_status": "failed",
            }
        )
        return _build_ucum_report(base)

    discovered, selected = _select_ucum_functional_cases(root)
    selected_case_kind_counts = Counter(case["case_kind"] for case in selected)
    if pinned_artifact and (
        discovered != 573
        or len(selected) != PINNED_UCUM_SUBSET_CASES
        or selected_case_kind_counts != PINNED_UCUM_SUBSET_CASE_KIND_COUNTS
    ):
        base.update(
            {
                "artifact_status": "invalid",
                "official_cases_discovered": discovered,
                "subset_cases_selected": len(selected),
                "official_functional_tests_status": "not_run",
                "qualification_status": "failed",
            }
        )
        return _build_ucum_report(base)
    receipts = [_execute_ucum_functional_case(case) for case in selected]
    passed = sum(receipt.status == "passed" for receipt in receipts)
    if not selected:
        official_status = "no_supported_cases"
        qualification_status = "partial"
    elif (
        pinned_artifact
        and runtime_verified
        and passed == len(receipts)
        and corpus_passed == 20
        and equivalence_verified
    ):
        official_status = "subset_passed"
        qualification_status = "passed"
    elif (
        pinned_artifact
        and runtime_verified
        and passed == len(receipts)
        and corpus_passed == 20
    ):
        # The named subset passed against the pinned mirror, but qualification
        # remains partial until independent official-attachment equivalence is
        # imported and bound to these exact bytes.
        official_status = "subset_passed"
        qualification_status = "partial"
    elif passed == len(receipts) and corpus_passed == 20:
        # Useful dry-run evidence, but not qualification: the artifact is not
        # the reviewed, pinned UCUM-java mirror identity.
        official_status = "subset_passed"
        qualification_status = "partial"
    else:
        official_status = "failed"
        qualification_status = "failed"
    base.update(
        {
            "artifact_status": "checksum_verified",
            "official_cases_discovered": discovered,
            "subset_cases_selected": len(selected),
            "subset_cases_executed": len(receipts),
            "subset_cases_passed": passed,
            "official_functional_tests_status": official_status,
            "qualification_status": qualification_status,
            "subset_qualified": qualification_status == "passed",
            "case_receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        }
    )
    return _build_ucum_report(base)


def load_build08_seed_receipts(path: Path) -> LoadedEvidenceLedger:
    return _load_bounded_jsonl(
        path,
        SeedReceipt,
        label="BUILD-08 seed receipt",
        max_records=4_000,
    )


def load_build08_engine_probes(path: Path) -> LoadedEvidenceLedger:
    return _load_bounded_jsonl(
        path,
        EngineProbeReceipt,
        label="BUILD-08 engine probe",
        max_records=4_000,
    )


def load_build08_adapt_attestations(path: Path) -> LoadedEvidenceLedger:
    return _load_bounded_jsonl(
        path,
        AdaptSeedAttestation,
        label="BUILD-08 ADAPT attestation",
        max_records=4_000,
    )


def load_build08_compatibility_trust_policy(
    path: Path,
) -> Build08CompatibilityTrustPolicy:
    return _load_bounded_json_record(
        path,
        Build08CompatibilityTrustPolicy,
        label="BUILD-08 compatibility trust policy",
    )


def validate_build08_compatibility_receipts(
    seed_receipts: LoadedEvidenceLedger | None,
    engine_probes: LoadedEvidenceLedger | None,
    adapt_attestations: LoadedEvidenceLedger | None,
    *,
    trust_policy: Build08CompatibilityTrustPolicy | None = None,
    manifest: ComputationEvaluationManifest | None = None,
) -> Build08CompatibilityQualification:
    """Rebuild BUILD-08 final receipts from all three exact raw ledgers."""

    manifest = manifest or build_computation_evaluation_manifest()
    plan_sha256 = next(
        plan.plan_sha256
        for plan in manifest.seed_plans
        if plan.plan_id == "build08-compatibility"
    )
    seeds = list(seed_receipts.records) if seed_receipts is not None else []
    probes = list(engine_probes.records) if engine_probes is not None else []
    attestations = (
        list(adapt_attestations.records) if adapt_attestations is not None else []
    )
    issues: list[str] = []
    valid_receipts = 0

    if any((seeds, probes, attestations)):
        if not all(
            ledger is not None
            for ledger in (seed_receipts, engine_probes, adapt_attestations)
        ):
            issues.append("all_three_raw_ledgers_required")
        if trust_policy is None:
            issues.append("operator_trust_policy_missing")
    if trust_policy is not None:
        if trust_policy.plan_sha256 != plan_sha256:
            issues.append("plan_hash_mismatch")
        raw_bindings = (
            (
                seed_receipts,
                trust_policy.seed_receipt_ledger_raw_sha256,
                "seed_raw_hash_mismatch",
            ),
            (
                engine_probes,
                trust_policy.engine_probe_ledger_raw_sha256,
                "probe_raw_hash_mismatch",
            ),
            (
                adapt_attestations,
                trust_policy.adapt_attestation_ledger_raw_sha256,
                "adapt_raw_hash_mismatch",
            ),
        )
        for ledger, expected_hash, issue in raw_bindings:
            if ledger is None or ledger.raw_ledger_sha256 != expected_hash:
                issues.append(issue)

    if seeds and trust_policy is not None:
        expected_plan = {
            (case.item_id, case.seed): case
            for case in build_seed_plan(trust_policy.run_id)
        }
        seed_keys = [(receipt.item_id, receipt.seed) for receipt in seeds]
        if len(seed_keys) != len(set(seed_keys)):
            issues.append("duplicate_seed_receipt")
        if set(seed_keys) != set(expected_plan):
            issues.append("seed_plan_coverage_mismatch")
        else:
            for receipt in seeds:
                planned = expected_plan[(receipt.item_id, receipt.seed)]
                if (
                    receipt.run_id != trust_policy.run_id
                    or receipt.item_type != planned.item_type
                    or receipt.compiler_version != planned.compiler_version
                    or receipt.source_sha256 != planned.source_sha256
                ):
                    issues.append("seed_plan_identity_mismatch")
                    break

    if probes and trust_policy is not None:
        for probe in probes:
            expected_engine_digest = (
                trust_policy.webwork_engine_image_digest
                if probe.item_type.value == "webwork"
                else trust_policy.imathas_engine_image_digest
            )
            expected_adapter = (
                None
                if probe.item_type.value == "webwork"
                else trust_policy.imathas_adapter_image_digest
            )
            if (
                probe.run_id != trust_policy.run_id
                or probe.engine_image_sha256 != expected_engine_digest
                or probe.adapter_image_sha256 != expected_adapter
                or probe.network_isolation_attestation_sha256
                != trust_policy.network_attestation_sha256
            ):
                issues.append("probe_runtime_identity_mismatch")
                break

    if (
        len(seeds) == 4_000
        and len(probes) == 4_000
        and len(attestations) == 4_000
        and not issues
    ):
        try:
            rebuilt = finalize_seed_receipts(probes, attestations)

            def sort_key(row: SeedReceipt) -> tuple[str, int]:
                return row.item_id, row.seed

            if [
                row.model_dump(mode="json") for row in sorted(rebuilt, key=sort_key)
            ] != [row.model_dump(mode="json") for row in sorted(seeds, key=sort_key)]:
                issues.append("finalized_seed_ledger_mismatch")
            section = validate_seed_receipts(seeds)
            if not section.passed:
                issues.append("build08_seed_validator_failed")
            if not issues:
                valid_receipts = 4_000
        except (TypeError, ValueError) as exc:
            issues.append(f"receipt_rebuild_failed:{type(exc).__name__.lower()}")

    if not any((seeds, probes, attestations)):
        status = "not_run"
    elif issues:
        status = "failed"
    elif valid_receipts == 4_000:
        status = "passed"
    else:
        status = "partial"
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-build08-native-evidence-v0",
        "run_id": trust_policy.run_id if trust_policy is not None else None,
        "plan_sha256": plan_sha256,
        "expected_receipts": 4_000,
        "imported_seed_receipts": len(seeds),
        "imported_engine_probes": len(probes),
        "imported_adapt_attestations": len(attestations),
        "valid_receipts": valid_receipts,
        "seed_receipt_ledger_raw_sha256": (
            seed_receipts.raw_ledger_sha256 if seed_receipts is not None else None
        ),
        "seed_receipt_ledger_raw_byte_count": (
            seed_receipts.raw_byte_count if seed_receipts is not None else None
        ),
        "engine_probe_ledger_raw_sha256": (
            engine_probes.raw_ledger_sha256 if engine_probes is not None else None
        ),
        "engine_probe_ledger_raw_byte_count": (
            engine_probes.raw_byte_count if engine_probes is not None else None
        ),
        "adapt_attestation_ledger_raw_sha256": (
            adapt_attestations.raw_ledger_sha256
            if adapt_attestations is not None
            else None
        ),
        "adapt_attestation_ledger_raw_byte_count": (
            adapt_attestations.raw_byte_count
            if adapt_attestations is not None
            else None
        ),
        "receipt_ledger_sha256": _sha256_json(
            [receipt.model_dump(mode="json") for receipt in seeds]
        ),
        "trust_policy_sha256": (
            trust_policy.policy_sha256 if trust_policy is not None else None
        ),
        "trust_policy_applied": trust_policy is not None,
        "webwork_engine_image_digest": (
            trust_policy.webwork_engine_image_digest
            if trust_policy is not None
            else None
        ),
        "imathas_engine_image_digest": (
            trust_policy.imathas_engine_image_digest
            if trust_policy is not None
            else None
        ),
        "imathas_adapter_image_digest": (
            trust_policy.imathas_adapter_image_digest
            if trust_policy is not None
            else None
        ),
        "network_attestation_sha256": (
            trust_policy.network_attestation_sha256
            if trust_policy is not None
            else None
        ),
        "operator_attestation_sha256": (
            trust_policy.operator_attestation_sha256
            if trust_policy is not None
            else None
        ),
        "executed": len(probes),
        "passed": valid_receipts,
        "warning_count": sum(probe.warning_count for probe in probes),
        "error_count": sum(probe.error_count for probe in probes),
        "execution_status": status,
        "qualified": status == "passed",
        "issues": list(dict.fromkeys(issues)),
    }
    content["evidence_sha256"] = _sha256_json(content)
    return Build08CompatibilityQualification.model_validate(content)


def load_canary_stage_execution_request(path: Path) -> CanaryStageExecutionRequest:
    return _load_bounded_json_record(
        path,
        CanaryStageExecutionRequest,
        label="computation canary stage execution request",
    )


async def execute_local_canary_stage(
    request: CanaryStageExecutionRequest,
    artifacts: LocalCanaryStageArtifacts,
    *,
    disposable_root: Path,
) -> CanaryStageReceipt:
    """Run one bounded local wrapper and derive its receipt from artifact bytes.

    This supports only a byte-pinned executable stored inside a marked local
    disposable root. It inherits no credentials and rejects overt URL, SSH,
    Hostinger, and live LibreTexts target arguments. It is not a filesystem or
    network sandbox: the operator must place the entire process in an independently
    proven isolated environment. The wrapper must emit a stage-specific event
    ledger plus an independently authored observer record. Exact source-controlled
    promotion remains a separate, empty-by-default trust gate.
    """

    root = _validate_local_canary_root(disposable_root)
    working_directory = _local_canary_path(
        artifacts.working_directory,
        root=root,
        label="canary working directory",
        kind="directory",
    )
    input_state = _local_canary_path(
        artifacts.input_state,
        root=root,
        label="canary input state",
        kind="file",
    )
    output_state = _local_canary_path(
        artifacts.output_state,
        root=root,
        label="canary output state",
        kind="absent",
    )
    raw_event_ledger = _local_canary_path(
        artifacts.raw_event_ledger,
        root=root,
        label="canary raw event ledger",
        kind="absent",
    )
    observer_attestation = _local_canary_path(
        artifacts.observer_attestation,
        root=root,
        label="canary observer attestation",
        kind="absent",
    )
    if (
        len(
            {
                input_state,
                output_state,
                raw_event_ledger,
                observer_attestation,
            }
        )
        != 4
    ):
        raise ValueError("canary evidence paths must be distinct")
    if not 1 <= artifacts.timeout_seconds <= 600:
        raise ValueError("canary stage timeout must be between 1 and 600 seconds")

    executable = _local_canary_path(
        Path(request.command_argv[0]),
        root=root,
        label="canary executable",
        kind="file",
    )
    if not os.access(executable, os.X_OK):
        raise ValueError("canary executable is not executable")
    executable_sha256 = _hash_bounded_canary_artifact(
        executable,
        label="canary executable",
    )
    if executable_sha256 != request.executable_sha256:
        raise ValueError("canary executable bytes differ from the sealed request")
    _reject_live_canary_arguments(request.command_argv)

    sandbox_home = root / "home"
    sandbox_tmp = root / "tmp"
    sandbox_home.mkdir(exist_ok=True)
    sandbox_tmp.mkdir(exist_ok=True)
    environment = {
        "HOME": str(sandbox_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(sandbox_tmp),
        "ASSESSMENT_COMPUTATION_CANARY_REQUEST_SHA256": request.request_sha256,
        "ASSESSMENT_COMPUTATION_CANARY_STAGE": request.stage.value,
        "ASSESSMENT_COMPUTATION_CANARY_SEQUENCE": str(request.sequence),
    }
    try:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *request.command_argv[1:],
            cwd=working_directory,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ValueError("local canary command could not start") from exc
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(
            asyncio.gather(
                _read_bounded_canary_stream(process.stdout),
                _read_bounded_canary_stream(process.stderr),
                process.wait(),
            ),
            timeout=artifacts.timeout_seconds,
        )
    except TimeoutError as exc:
        _kill_local_canary_process_group(process.pid)
        await process.wait()
        raise ValueError("local canary command timed out") from exc
    except Exception:
        _kill_local_canary_process_group(process.pid)
        await process.wait()
        raise
    _kill_local_canary_process_group(process.pid)
    if exit_code != 0:
        raise ValueError(f"local canary command exited with status {exit_code}")
    if (
        _hash_bounded_canary_artifact(
            executable,
            label="canary executable",
        )
        != executable_sha256
    ):
        raise ValueError("canary executable bytes changed during execution")

    output_state = _local_canary_path(
        output_state,
        root=root,
        label="canary output state",
        kind="file",
    )
    raw_event_ledger = _local_canary_path(
        raw_event_ledger,
        root=root,
        label="canary raw event ledger",
        kind="file",
    )
    observer_attestation = _local_canary_path(
        observer_attestation,
        root=root,
        label="canary observer attestation",
        kind="file",
    )
    input_state_sha256 = _hash_bounded_canary_artifact(
        input_state, label="canary input state"
    )
    output_state_sha256 = _hash_bounded_canary_artifact(
        output_state, label="canary output state"
    )
    loaded_events = _load_bounded_jsonl(
        raw_event_ledger,
        CanaryStageEvent,
        label="computation canary stage event",
        max_records=2,
    )
    if (
        loaded_events.raw_byte_count < 1
        or loaded_events.raw_byte_count > MAX_CANARY_STAGE_ARTIFACT_BYTES
    ):
        raise ValueError("canary stage event ledger is empty or oversized")
    events = list(loaded_events.records)
    required_kinds = {
        CANARY_STAGE_EVENT_KIND[request.stage],
        CanaryStageEventKind.REAL_PUBLICATION_ATTEMPT_COUNT,
    }
    if len(events) != 2 or {event.kind for event in events} != required_kinds:
        raise ValueError("canary command did not emit the exact stage event contract")
    if any(
        event.request_sha256 != request.request_sha256
        or event.run_id != request.run_id
        or event.sequence != request.sequence
        or event.stage != request.stage
        for event in events
    ):
        raise ValueError("canary event ledger changed the sealed request identity")
    if (
        len({event.event_sha256 for event in events}) != 2
        or len({event.evidence_sha256 for event in events}) != 2
    ):
        raise ValueError("canary stage events must carry independent evidence")

    observer = _load_bounded_json_record(
        observer_attestation,
        CanaryStageObserverAttestation,
        label="computation canary observer attestation",
    )
    if (
        observer.request_sha256 != request.request_sha256
        or observer.run_id != request.run_id
        or observer.accepted_build08_base_commit != request.accepted_build08_base_commit
        or observer.prior_image_digest != request.prior_image_digest
        or observer.candidate_image_digest != request.candidate_image_digest
        or observer.computation_image_digest != request.computation_image_digest
        or observer.sequence != request.sequence
        or observer.stage != request.stage
        or observer.input_state_sha256 != input_state_sha256
        or observer.output_state_sha256 != output_state_sha256
        or observer.raw_event_ledger_sha256 != loaded_events.raw_ledger_sha256
        or observer.raw_event_ledger_byte_count != loaded_events.raw_byte_count
    ):
        raise ValueError("canary observer did not attest the exact stage artifacts")
    observer_file_sha256 = _hash_bounded_canary_artifact(
        observer_attestation,
        label="canary observer attestation",
    )
    stage_event = next(
        event
        for event in events
        if event.kind == CANARY_STAGE_EVENT_KIND[request.stage]
    )
    enforce_stage = request.stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
    specialist_attestation_evidence = stage_event.specialist_attestation_evidence
    if enforce_stage and specialist_attestation_evidence is None:
        raise ValueError(
            "enforce canary stage lacks specialist attestation exercise evidence"
        )
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-canary-stage-v0",
        "run_id": request.run_id,
        "manifest_sha256": request.manifest_sha256,
        "accepted_build08_base_commit": request.accepted_build08_base_commit,
        "prior_image_digest": request.prior_image_digest,
        "candidate_image_digest": request.candidate_image_digest,
        "computation_image_digest": request.computation_image_digest,
        "sequence": request.sequence,
        "stage": request.stage,
        "mode": request.mode,
        "family": request.family,
        "cloned_database_snapshot_sha256": (request.cloned_database_snapshot_sha256),
        "input_state_sha256": input_state_sha256,
        "output_state_sha256": output_state_sha256,
        "executor_revision": CANARY_LOCAL_EXECUTOR_REVISION,
        "command_argv_sha256": request.command_argv_sha256,
        "executable_sha256": executable_sha256,
        "command_exit_code": 0,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stage_contract_sha256": request.stage_contract_sha256,
        "stage_event_kind": stage_event.kind,
        "rollback_observed_image_digest": stage_event.observed_image_digest,
        "raw_event_ledger_sha256": loaded_events.raw_ledger_sha256,
        "raw_event_ledger_byte_count": loaded_events.raw_byte_count,
        "raw_event_count": 2,
        "independent_observer_attestation_sha256": observer_file_sha256,
        "fake_publication_adapter_used": enforce_stage,
        "specialist_attestation_evidence": (
            specialist_attestation_evidence.model_dump(mode="json")
            if specialist_attestation_evidence is not None
            else None
        ),
        "specialist_attestation_paths_exercised": (
            specialist_attestation_evidence is not None
        ),
        "real_publication_attempt_count": 0,
        "passed": True,
    }
    content["receipt_sha256"] = _sha256_json(content)
    return CanaryStageReceipt.model_validate(content)


async def _read_bounded_canary_stream(
    stream: asyncio.StreamReader | None,
) -> bytes:
    if stream is None:
        return b""
    output = bytearray()
    while chunk := await stream.read(8192):
        output.extend(chunk)
        if len(output) > MAX_CANARY_COMMAND_OUTPUT_BYTES:
            raise ValueError("local canary command output exceeds 64 KiB")
    return bytes(output)


def _kill_local_canary_process_group(process_group_id: int) -> None:
    if process_group_id <= 0:
        raise ValueError("local canary process group identity is invalid")
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _validate_local_canary_root(root: Path) -> Path:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("local canary root must be an absolute non-symlink directory")
    resolved = root.resolve(strict=True)
    marker = resolved / ".assessment-computation-canary-disposable-v0"
    if (
        marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != CANARY_DISPOSABLE_MARKER
    ):
        raise ValueError("local canary root lacks the exact disposable marker")
    return resolved


def _local_canary_path(
    path: Path,
    *,
    root: Path,
    label: str,
    kind: Literal["file", "directory", "absent"],
) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError(f"{label} must be an absolute non-symlink path")
    resolved = path.resolve(strict=kind != "absent")
    if (resolved == root and kind != "directory") or (
        resolved != root and root not in resolved.parents
    ):
        raise ValueError(f"{label} must stay inside the disposable root")
    if kind == "file" and (not resolved.is_file() or resolved.is_symlink()):
        raise ValueError(f"{label} must be a regular file")
    if kind == "directory" and (not resolved.is_dir() or resolved.is_symlink()):
        raise ValueError(f"{label} must be a directory")
    if kind == "absent" and path.exists():
        raise ValueError(f"{label} must not pre-exist")
    return resolved


def _reject_live_canary_arguments(arguments: Sequence[str]) -> None:
    forbidden_fragments = (
        "://",
        "hostinger",
        "/opt/libretexts",
        "libretexts.dev",
        "libretexts.org",
    )
    forbidden_programs = {"ssh", "scp", "sftp", "rsync"}
    for argument in arguments:
        lowered = argument.lower()
        if any(fragment in lowered for fragment in forbidden_fragments):
            raise ValueError("local canary command contains a live or remote target")
        if Path(argument).name.lower() in forbidden_programs:
            raise ValueError("local canary command may not invoke a remote transport")


def _hash_bounded_canary_artifact(path: Path, *, label: str) -> str:
    size = path.stat().st_size
    if size < 1 or size > MAX_CANARY_STAGE_ARTIFACT_BYTES:
        raise ValueError(f"{label} must be between 1 byte and 4 MiB")
    hasher = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            byte_count += len(chunk)
            if byte_count > MAX_CANARY_STAGE_ARTIFACT_BYTES:
                raise ValueError(f"{label} exceeds 4 MiB")
            hasher.update(chunk)
    if byte_count != size:
        raise ValueError(f"{label} changed while it was hashed")
    return hasher.hexdigest()


def load_canary_stage_receipts(path: Path) -> LoadedCanaryStageLedger:
    loaded = _load_bounded_jsonl(
        path,
        CanaryStageReceipt,
        label="computation canary stage",
        max_records=10,
    )
    return LoadedCanaryStageLedger(
        records=tuple(loaded.records),
        raw_ledger_sha256=loaded.raw_ledger_sha256,
        raw_byte_count=loaded.raw_byte_count,
    )


def validate_canary_stage_receipts(
    receipts: LoadedCanaryStageLedger | None,
    *,
    manifest: ComputationEvaluationManifest | None = None,
) -> CanaryQualificationReport:
    manifest = manifest or build_computation_evaluation_manifest()
    rows = list(receipts.records) if receipts is not None else []
    issues: list[str] = []
    if rows:
        if len(rows) != 10:
            issues.append("ten_stage_receipts_required")
        if [row.stage for row in rows] != list(CANARY_STAGE_ORDER):
            issues.append("stage_order_mismatch")
        if [row.sequence for row in rows] != list(range(1, 11)):
            issues.append("stage_sequence_mismatch")
        if len({row.receipt_sha256 for row in rows}) != len(rows):
            issues.append("duplicate_stage_receipt_hash")
        raw_event_hashes = {row.raw_event_ledger_sha256 for row in rows}
        observer_hashes = {row.independent_observer_attestation_sha256 for row in rows}
        if len(raw_event_hashes) != len(rows):
            issues.append("reused_stage_event_ledger")
        if len(observer_hashes) != len(rows):
            issues.append("reused_stage_observer_attestation")
        if raw_event_hashes & observer_hashes:
            issues.append("event_and_observer_evidence_not_independent")
        if len({row.stage_contract_sha256 for row in rows}) != len(rows):
            issues.append("reused_or_missing_stage_contract")
        if len({row.run_id for row in rows}) != 1:
            issues.append("mixed_run_identity")
        if len({row.accepted_build08_base_commit for row in rows}) != 1:
            issues.append("mixed_build08_base_commit")
        if len({row.prior_image_digest for row in rows}) != 1:
            issues.append("mixed_prior_image")
        if len({row.candidate_image_digest for row in rows}) != 1:
            issues.append("mixed_candidate_image")
        if len({row.computation_image_digest for row in rows}) != 1:
            issues.append("mixed_computation_image")
        if any(row.manifest_sha256 != manifest.manifest_sha256 for row in rows):
            issues.append("manifest_binding_mismatch")
        database_hashes = {
            row.cloned_database_snapshot_sha256
            for row in rows
            if row.stage != CanaryStage.OFFLINE_CORPUS_SECURITY
        }
        if len(database_hashes) != 1:
            issues.append("cloned_database_identity_changed")
        if (
            len(rows) == 10
            and rows[1].input_state_sha256 != rows[1].output_state_sha256
        ):
            issues.append("off_mode_parity_mismatch")
        if (
            len(rows) == 10
            and rows[2].input_state_sha256 != rows[2].output_state_sha256
        ):
            issues.append("backup_restore_state_mismatch")
        if (
            len(rows) == 10
            and rows[1].input_state_sha256 != rows[9].output_state_sha256
        ):
            issues.append("return_off_state_mismatch")
        if (
            len(rows) == 10
            and rows[1].input_state_sha256 != rows[3].output_state_sha256
        ):
            issues.append("rollback_did_not_restore_off_baseline")
    if not rows:
        status = "not_run"
    elif issues:
        status = "failed"
    elif len(rows) == 10:
        status = "passed"
    else:
        status = "partial"
    first = rows[0] if rows else None
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-canary-report-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "accepted_build08_base_commit": ACCEPTED_BUILD08_BASE_COMMIT,
        "prior_image_digest": ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST,
        "run_id": first.run_id if first is not None else None,
        "candidate_image_digest": (
            first.candidate_image_digest if first is not None else None
        ),
        "computation_image_digest": (
            first.computation_image_digest if first is not None else None
        ),
        "expected_stages": 10,
        "imported_stages": len(rows),
        "passed_stages": sum(row.passed for row in rows),
        "stage_receipt_sha256s": {row.stage.value: row.receipt_sha256 for row in rows},
        "raw_stage_ledger_sha256": (
            receipts.raw_ledger_sha256 if receipts is not None else None
        ),
        "raw_stage_ledger_byte_count": (
            receipts.raw_byte_count if receipts is not None else None
        ),
        "execution_status": status,
        "qualified": status == "passed",
        "issues": list(dict.fromkeys(issues)),
    }
    content["report_sha256"] = _sha256_json(content)
    return CanaryQualificationReport.model_validate(content)


def validate_sme_review_records(
    records: Iterable[SmeReviewRecord] | LoadedSmeReviewLedger,
    *,
    manifest: ComputationEvaluationManifest | None = None,
) -> SmeReviewQualificationReport:
    manifest = manifest or build_computation_evaluation_manifest()
    loaded_ledger = records if isinstance(records, LoadedSmeReviewLedger) else None
    rows = list(records)
    expected: dict[tuple[str, str], str] = {}
    expected.update(
        {
            ("computation_fixture", case.fixture_id): case.fixture_sha256
            for case in manifest.computation_cases
        }
    )
    expected.update(
        {
            ("parameterized_lineage", lineage.lineage_id): lineage.lineage_sha256
            for lineage in manifest.parameterized_lineages
        }
    )
    expected.update(
        {
            ("mutation", mutation.mutation_id): mutation.mutation_sha256
            for mutation in manifest.mutations
        }
    )
    if len(expected) != 280:
        raise RuntimeError("SME review target set must contain exactly 280 records")
    seen: set[tuple[str, str]] = set()
    approved: set[tuple[str, str]] = set()
    rejected: set[tuple[str, str]] = set()
    duplicate_count = 0
    unexpected_count = 0
    manifest_mismatch_count = 0
    for record in rows:
        key = (record.target_type, record.target_id)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        if record.manifest_sha256 != manifest.manifest_sha256:
            manifest_mismatch_count += 1
            unexpected_count += 1
            continue
        if expected.get(key) != record.target_sha256:
            unexpected_count += 1
            continue
        if record.decision == "approved":
            approved.add(key)
        else:
            rejected.add(key)
    reviewed = approved | rejected
    missing_count = len(set(expected) - reviewed)
    unique_record_hashes = len({record.record_sha256 for record in rows})
    unique_attestation_hashes = len(
        {record.reviewer_attestation_sha256 for record in rows}
    )
    independent_hashes = unique_record_hashes == len(
        rows
    ) and unique_attestation_hashes == len(rows)
    if not rows:
        status = "not_run"
    elif (
        duplicate_count
        or unexpected_count
        or manifest_mismatch_count
        or rejected
        or not independent_hashes
        or loaded_ledger is None
    ):
        status = "failed"
    elif reviewed == set(expected):
        status = "passed"
    else:
        status = "incomplete"
    ledger_sha256 = _sha256_json([record.model_dump(mode="json") for record in rows])
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-sme-review-report-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "expected_targets": 280,
        "reviewed_targets": len(reviewed),
        "approved_targets": len(approved),
        "rejected_targets": len(rejected),
        "missing_targets": missing_count,
        "duplicate_targets": duplicate_count,
        "unexpected_targets": unexpected_count,
        "reviewer_count": len({record.reviewer_subject_sha256 for record in rows}),
        "unique_record_hashes": unique_record_hashes,
        "unique_reviewer_attestation_hashes": unique_attestation_hashes,
        "raw_review_ledger_sha256": (
            loaded_ledger.raw_ledger_sha256 if loaded_ledger is not None else None
        ),
        "raw_review_ledger_byte_count": (
            loaded_ledger.raw_byte_count if loaded_ledger is not None else None
        ),
        "raw_ledger_bound": loaded_ledger is not None,
        "execution_status": status,
        "qualified": status == "passed",
        "review_ledger_sha256": ledger_sha256,
    }
    content["report_sha256"] = _sha256_json(content)
    return SmeReviewQualificationReport.model_validate(content)


def load_sme_review_records(path: Path) -> LoadedSmeReviewLedger:
    if path.is_symlink() or not path.is_file():
        raise ValueError("SME review ledger must be an explicit local regular file")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("SME review ledger exceeds 8 MiB")
    records: list[SmeReviewRecord] = []
    raw_hasher = hashlib.sha256()
    raw_byte_count = 0
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            raw_hasher.update(raw_line)
            raw_byte_count += len(raw_line)
            if len(raw_line) > MAX_RECEIPT_LINE_BYTES:
                raise ValueError(f"SME review line {line_number} exceeds 256 KiB")
            if not raw_line.strip():
                continue
            if len(records) >= 280:
                raise ValueError("SME review ledger contains more than 280 rows")
            try:
                records.append(SmeReviewRecord.model_validate_json(raw_line))
            except Exception as exc:
                raise ValueError(f"SME review line {line_number} is malformed") from exc
    if raw_byte_count != path.stat().st_size:
        raise ValueError("SME review ledger changed while it was imported")
    return LoadedSmeReviewLedger(
        records=tuple(records),
        raw_ledger_sha256=raw_hasher.hexdigest(),
        raw_byte_count=raw_byte_count,
    )


def load_paired_study_evidence(path: Path) -> PairedStudyEvidenceLedger:
    if path.is_symlink() or not path.is_file():
        raise ValueError("paired-study ledger must be an explicit local regular file")
    raw = path.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("paired-study ledger exceeds 16 MiB")
    try:
        return PairedStudyEvidenceLedger.model_validate_json(raw)
    except Exception as exc:
        raise ValueError("paired-study ledger is malformed") from exc


def validate_paired_study_evidence(
    ledger: PairedStudyEvidenceLedger,
) -> PairedStudyQualificationReport:
    records = list(ledger.records)
    drafts = list(ledger.drafts)
    failures: list[str] = []
    concepts = {draft.sealed_concept_sha256 for draft in drafts}
    reviewers = {record.reviewer_subject_sha256 for record in records}
    draft_keys = [(draft.sealed_concept_sha256, draft.arm) for draft in drafts]
    if len(draft_keys) != len(set(draft_keys)):
        failures.append("paired-study ledger contains duplicate draft arms")
    if len(drafts) != 100:
        failures.append("paired study requires 100 exact arm drafts")
    draft_by_key = {(draft.sealed_concept_sha256, draft.arm): draft for draft in drafts}
    for concept in concepts:
        concept_drafts = [
            draft for draft in drafts if draft.sealed_concept_sha256 == concept
        ]
        if {draft.arm for draft in concept_drafts} != {"control", "treatment"}:
            failures.append("each sealed concept requires both exact arm drafts")
            break
        if len({draft.source_sha256 for draft in concept_drafts}) != 1:
            failures.append("paired arm drafts must bind the same sealed source")
            break
    keys = [
        (
            record.sealed_concept_sha256,
            record.arm,
            record.reviewer_subject_sha256,
        )
        for record in records
    ]
    if len(keys) != len(set(keys)):
        failures.append("paired-study ledger contains duplicate review keys")
    if len(concepts) != 50:
        failures.append("paired study requires exactly 50 sealed concepts")
    if len(reviewers) != 2:
        failures.append("paired study requires exactly two reviewers")
    if len(records) != 200:
        failures.append("paired study requires 200 blinded arm reviews")
    for record in records:
        draft = draft_by_key.get((record.sealed_concept_sha256, record.arm))
        if (
            draft is None
            or record.draft_sha256 != draft.draft_sha256
            or record.draft_evidence_sha256 != draft.evidence_sha256
        ):
            failures.append(
                "every review must bind the exact arm draft and provider evidence"
            )
            break
    for concept in concepts:
        concept_records = [
            record for record in records if record.sealed_concept_sha256 == concept
        ]
        for reviewer in reviewers:
            arms = {
                record.arm
                for record in concept_records
                if record.reviewer_subject_sha256 == reviewer
            }
            if arms != {"control", "treatment"}:
                failures.append(
                    "each reviewer must score both arms for every sealed concept"
                )
                break
        if failures and failures[-1].startswith("each reviewer"):
            break
    try:
        provider_cost = Decimal(ledger.provider_cost_usd)
    except ArithmeticError:
        provider_cost = Decimal("Infinity")
    if provider_cost > Decimal("25"):
        failures.append("paired-study provider cost exceeds USD 25")

    treatment_by_concept = {
        concept: [
            record
            for record in records
            if record.sealed_concept_sha256 == concept and record.arm == "treatment"
        ]
        for concept in concepts
    }
    control_by_concept = {
        concept: [
            record
            for record in records
            if record.sealed_concept_sha256 == concept and record.arm == "control"
        ]
        for concept in concepts
    }
    treatment_correct_rate = _ratio(
        sum(
            bool(group)
            and all(record.computationally_correct_without_edit for record in group)
            for group in treatment_by_concept.values()
        ),
        len(concepts),
    )
    control_defect_rate = _ratio(
        sum(
            any(record.material_computation_defect for record in group)
            for group in control_by_concept.values()
        ),
        len(concepts),
    )
    treatment_defect_rate = _ratio(
        sum(
            any(record.material_computation_defect for record in group)
            for group in treatment_by_concept.values()
        ),
        len(concepts),
    )
    control_records = [record for record in records if record.arm == "control"]
    treatment_records = [record for record in records if record.arm == "treatment"]
    control_median = _median(
        [float(record.correction_seconds) for record in control_records]
    )
    treatment_median = _median(
        [float(record.correction_seconds) for record in treatment_records]
    )
    source_delta = _score_delta(
        treatment_records, control_records, "source_grounding_score"
    )
    pedagogy_delta = _score_delta(treatment_records, control_records, "pedagogy_score")
    quality_improved = (
        treatment_defect_rate <= control_defect_rate / 2
        or treatment_median <= control_median * 0.8
    )
    thresholds_passed = (
        treatment_correct_rate >= 0.95
        and quality_improved
        and source_delta >= -0.05
        and pedagogy_delta >= -0.05
        and provider_cost <= Decimal("25")
    )
    if not thresholds_passed:
        failures.append("paired-study quality thresholds are unmet")
    if not records:
        status = "not_run"
    elif (
        len(concepts) != 50
        or len(drafts) != 100
        or len(reviewers) != 2
        or len(records) != 200
    ):
        status = "incomplete"
    elif failures:
        status = "failed"
    else:
        status = "passed"
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-paired-study-report-v0",
        "study_id": ledger.study_id,
        "manifest_sha256": ledger.manifest_sha256,
        "candidate_image_digest": ledger.candidate_image_digest,
        "computation_image_digest": ledger.computation_image_digest,
        "ledger_sha256": ledger.ledger_sha256,
        "concept_count": len(concepts),
        "draft_count": len(drafts),
        "provider_call_count": sum(
            len(draft.provider_call_receipt_sha256s) for draft in drafts
        ),
        "reviewer_count": len(reviewers),
        "record_count": len(records),
        "drafts_sha256": ledger.drafts_sha256,
        "provider_cost_usd": ledger.provider_cost_usd,
        "treatment_correct_without_edit_rate": treatment_correct_rate,
        "control_defect_rate": control_defect_rate,
        "treatment_defect_rate": treatment_defect_rate,
        "control_median_correction_seconds": control_median,
        "treatment_median_correction_seconds": treatment_median,
        "source_grounding_delta": source_delta,
        "pedagogy_delta": pedagogy_delta,
        "execution_status": status,
        "qualified": status == "passed",
        "failures": list(dict.fromkeys(failures)),
    }
    content["report_sha256"] = _sha256_json(content)
    return PairedStudyQualificationReport.model_validate(content)


def calculate_acceptance_metrics(
    observations: Iterable[ComputationEvaluationObservation],
    *,
    manifest: ComputationEvaluationManifest | None = None,
    engine_execution: EngineExecutionSummary | None = None,
    paired_study: PairedStudySummary | None = None,
    workflow_evidence: WorkflowObservationEvidence | None = None,
    qualification_merged_evidence: (
        QualificationMergedObservationEvidence | None
    ) = None,
    mutation_qualification: OfflineMutationQualificationReport | None = None,
    native_qualification: NativeQualificationReport | None = None,
    algebra_native_qualification: AlgebraNativeQualificationReport | None = None,
    ucum_qualification: UcumQualificationReport | None = None,
    sme_review_qualification: SmeReviewQualificationReport | None = None,
    build08_qualification: Build08CompatibilityQualification | None = None,
    paired_study_qualification: PairedStudyQualificationReport | None = None,
    safety_evidence: SpikeSafetyEvidence | None = None,
    canary_qualification: CanaryQualificationReport | None = None,
) -> AcceptanceMetrics:
    failures: list[str] = []
    raw_records = list(observations)
    expected_merge_bindings: dict[str, str] | None = None
    if (
        manifest is not None
        and native_qualification is not None
        and algebra_native_qualification is not None
    ):
        algebra_evaluation_ids = {
            case.fixture_id
            for case in manifest.computation_cases
            if case.family == "algebraic"
        }
        expected_merge_bindings = {
            case_id: (
                algebra_native_qualification.report_sha256
                if case_id in algebra_evaluation_ids
                else native_qualification.report_sha256
            )
            for case_id in sorted(
                algebra_evaluation_ids
                | {case.fixture_id for case in manifest.engine_twins}
            )
        }
    expected_qualification_merge: QualificationMergedObservationEvidence | None = None
    if (
        manifest is not None
        and workflow_evidence is not None
        and native_qualification is not None
        and algebra_native_qualification is not None
    ):
        try:
            expected_qualification_merge = (
                build_qualification_merged_observation_evidence(
                    workflow_evidence,
                    native_qualification=native_qualification,
                    algebra_native_qualification=algebra_native_qualification,
                    manifest=manifest,
                )
            )
        except ValueError:
            expected_qualification_merge = None
    qualification_merge_bound = bool(
        manifest
        and workflow_evidence
        and native_qualification
        and algebra_native_qualification
        and qualification_merged_evidence
        and native_qualification.qualified
        and algebra_native_qualification.qualified
        and native_qualification.manifest_sha256 == manifest.manifest_sha256
        and algebra_native_qualification.manifest_sha256 == manifest.manifest_sha256
        and qualification_merged_evidence.manifest_sha256 == manifest.manifest_sha256
        and qualification_merged_evidence.workflow_evidence_sha256
        == workflow_evidence.evidence_sha256
        and qualification_merged_evidence.workflow_observations_sha256
        == workflow_evidence.observations_sha256
        and qualification_merged_evidence.native_report_sha256
        == native_qualification.report_sha256
        and qualification_merged_evidence.algebra_native_report_sha256
        == algebra_native_qualification.report_sha256
        and qualification_merged_evidence.upgraded_case_report_sha256s
        == expected_merge_bindings
        and qualification_merged_evidence == expected_qualification_merge
        and qualification_merged_evidence.solution_set_production_delivery_enabled
        is False
        and _sha256_json([record.model_dump(mode="json") for record in raw_records])
        == workflow_evidence.observations_sha256
        and _native_reports_share_runtime_identity(
            native_qualification,
            algebra_native_qualification,
        )
    )
    if not qualification_merge_bound:
        failures.append(
            "qualified observation states lack the exact workflow/native evidence merge"
        )
    records = (
        list(qualification_merged_evidence.observations)
        if qualification_merge_bound and qualification_merged_evidence is not None
        else raw_records
    )
    identifiers = [record.case_id for record in records]
    if len(identifiers) != len(set(identifiers)):
        failures.append("evaluation observations contain duplicate case IDs")

    if manifest is not None:
        expected_ids = (
            {case.fixture_id for case in manifest.computation_cases}
            | {case.fixture_id for case in manifest.engine_twins}
            | {mutation.mutation_id for mutation in manifest.mutations}
        )
        observed_ids = set(identifiers)
        missing = expected_ids - observed_ids
        unexpected = observed_ids - expected_ids
        if missing:
            failures.append(f"evaluation is missing {len(missing)} planned cases")
        if unexpected:
            failures.append(f"evaluation contains {len(unexpected)} unplanned cases")

    positives = [record for record in records if record.kind == MutationKind.POSITIVE]
    mutations = [record for record in records if record.kind != MutationKind.POSITIVE]
    correct_validated = [
        record
        for record in positives
        if record.state == ObservedValidationState.VALIDATED and record.oracle_match
    ]
    bad_validated = [
        record
        for record in records
        if record.state == ObservedValidationState.VALIDATED
        and (record.kind != MutationKind.POSITIVE or not record.oracle_match)
    ]
    detected_mutations = [
        record
        for record in mutations
        if record.critical_defect_detected
        and record.state != ObservedValidationState.VALIDATED
    ]
    false_positive_flags = [
        record for record in positives if record.critical_defect_detected
    ]

    supported_coverage = _ratio(len(correct_validated), len(positives))
    coverage_by_surface = {
        surface.value: _ratio(
            sum(
                record.state == ObservedValidationState.VALIDATED
                and record.oracle_match
                for record in positives
                if record.surface == surface
            ),
            sum(record.surface == surface for record in positives),
        )
        for surface in EvaluationSurface
    }
    mutation_recall = _ratio(len(detected_mutations), len(mutations))
    mutation_precision = _ratio(
        len(detected_mutations),
        len(detected_mutations) + len(false_positive_flags),
    )
    reproducibility = _ratio(
        sum(record.deterministic_replay for record in records), len(records)
    )

    if not positives or not mutations:
        failures.append("evaluation requires both positive and mutated cases")
    if len(bad_validated) != 0:
        failures.append("materially incorrect or mutated assertions were validated")
    if supported_coverage < 0.90:
        failures.append("supported-case coverage is below 90 percent")
    for surface, coverage in coverage_by_surface.items():
        if not any(record.surface.value == surface for record in positives):
            failures.append(f"{surface} has no positive observations")
        elif coverage < 0.85:
            failures.append(f"{surface} coverage is below 85 percent")
    if mutation_recall < 1.0:
        failures.append("not every critical mutation was detected")
    if reproducibility < 1.0:
        failures.append("fixed-case replay was not fully deterministic")

    observation_metrics_passed = not failures
    observation_gate_passed = bool(
        manifest
        and workflow_evidence
        and mutation_qualification
        and workflow_evidence.manifest_sha256 == manifest.manifest_sha256
        and workflow_evidence.mutation_report_sha256
        == mutation_qualification.report_sha256
        and qualification_merge_bound
        and qualification_merged_evidence
        and qualification_merged_evidence.observations_sha256
        == _sha256_json([record.model_dump(mode="json") for record in records])
        and set(workflow_evidence.positive_workflow_receipt_sha256s)
        == (
            {case.fixture_id for case in manifest.computation_cases}
            | {case.fixture_id for case in manifest.engine_twins}
        )
    )
    mutation_gate_passed = bool(
        manifest
        and mutation_qualification
        and mutation_qualification.manifest_sha256 == manifest.manifest_sha256
        and mutation_qualification.all_critical_mutations_detected
        and mutation_qualification.detected_mutations == 200
    )
    sme_review_gate_passed = bool(
        manifest
        and sme_review_qualification
        and sme_review_qualification.manifest_sha256 == manifest.manifest_sha256
        and sme_review_qualification.qualified
    )
    if not observation_gate_passed:
        failures.append(
            "positive and mutation observations lack a hash-bound workflow ledger"
        )
    if not mutation_gate_passed:
        failures.append("the exact offline mutation qualification report is missing")
    if not sme_review_gate_passed:
        failures.append("the hash-bound 280-target SME review ledger is incomplete")
    fixture_gate_passed = (
        observation_metrics_passed
        and observation_gate_passed
        and mutation_gate_passed
        and sme_review_gate_passed
    )

    build08_plan_sha256 = (
        next(
            (
                plan.plan_sha256
                for plan in manifest.seed_plans
                if plan.plan_id == "build08-compatibility"
            ),
            None,
        )
        if manifest is not None
        else None
    )
    algebra_native_gate_passed = bool(
        manifest
        and native_qualification
        and algebra_native_qualification
        and algebra_native_qualification.manifest_sha256 == manifest.manifest_sha256
        and algebra_native_qualification.qualified
        and algebra_native_qualification.qualification_run_id
        == native_qualification.qualification_run_id
        and algebra_native_qualification.webwork_engine_image_digest
        == native_qualification.webwork_engine_image_digest
        and algebra_native_qualification.imathas_engine_image_digest
        == native_qualification.imathas_engine_image_digest
        and algebra_native_qualification.imathas_adapter_image_digest
        == native_qualification.imathas_adapter_image_digest
        and algebra_native_qualification.network_attestation_sha256
        == native_qualification.network_attestation_sha256
        and algebra_native_qualification.imathas_namespace
        == native_qualification.imathas_namespace
        and qualification_merge_bound
    )
    if not algebra_native_gate_passed:
        failures.append("all 40 exact algebra native qualification receipts must pass")
    engine_gate_passed = bool(
        manifest
        and native_qualification
        and native_qualification.manifest_sha256 == manifest.manifest_sha256
        and native_qualification.qualified
        and build08_qualification
        and build08_qualification.qualified
        and build08_qualification.plan_sha256 == build08_plan_sha256
        and build08_qualification.webwork_engine_image_digest
        == native_qualification.webwork_engine_image_digest
        and build08_qualification.imathas_engine_image_digest
        == native_qualification.imathas_engine_image_digest
        and build08_qualification.imathas_adapter_image_digest
        == native_qualification.imathas_adapter_image_digest
        and build08_qualification.network_attestation_sha256
        == native_qualification.network_attestation_sha256
        and algebra_native_gate_passed
    )
    if not engine_gate_passed:
        failures.append("both 4,000-execution native-engine plans must pass")

    paired_study_gate_passed = bool(
        manifest
        and paired_study_qualification
        and paired_study_qualification.qualified
        and paired_study_qualification.manifest_sha256 == manifest.manifest_sha256
        and safety_evidence
        and paired_study_qualification.candidate_image_digest
        == safety_evidence.candidate_image_digest
        and paired_study_qualification.computation_image_digest
        == safety_evidence.computation_image_digest
    )
    if not paired_study_gate_passed:
        failures.append("the sealed 50-pair reviewer study threshold is unmet")

    ucum_gate_passed = bool(ucum_qualification and ucum_qualification.subset_qualified)
    if not ucum_gate_passed:
        failures.append("the pinned named UCUM subset qualification is unmet")

    safety_gate_passed = bool(
        manifest
        and safety_evidence
        and safety_evidence.manifest_sha256 == manifest.manifest_sha256
        and native_qualification
        and build08_qualification
        and safety_evidence.run_id == native_qualification.qualification_run_id
        and safety_evidence.run_id == build08_qualification.run_id
        and safety_evidence.network_attestation_sha256
        == native_qualification.network_attestation_sha256
        and safety_evidence.network_attestation_sha256
        == build08_qualification.network_attestation_sha256
    )
    if not safety_gate_passed:
        failures.append("zero-event hash-bound safety evidence is missing")

    canary_gate_passed = bool(
        manifest
        and safety_evidence
        and canary_qualification
        and canary_qualification.qualified
        and canary_qualification.manifest_sha256 == manifest.manifest_sha256
        and canary_qualification.run_id == safety_evidence.run_id
        and canary_qualification.candidate_image_digest
        == safety_evidence.candidate_image_digest
        and canary_qualification.computation_image_digest
        == safety_evidence.computation_image_digest
    )
    if not canary_gate_passed:
        failures.append("all ten disposable canary stages must pass and return off")

    evidence_bound = bool(
        qualification_merge_bound
        and all(
            evidence is not None
            for evidence in (
                workflow_evidence,
                qualification_merged_evidence,
                mutation_qualification,
                native_qualification,
                algebra_native_qualification,
                ucum_qualification,
                sme_review_qualification,
                build08_qualification,
                paired_study_qualification,
                safety_evidence,
                canary_qualification,
            )
        )
    )
    evidence_bundle_sha256 = (
        _sha256_json(
            {
                "manifest_sha256": manifest.manifest_sha256,
                "workflow_evidence_sha256": workflow_evidence.evidence_sha256,
                "qualification_merged_evidence_sha256": (
                    qualification_merged_evidence.evidence_sha256
                ),
                "mutation_report_sha256": mutation_qualification.report_sha256,
                "native_report_sha256": native_qualification.report_sha256,
                "algebra_native_report_sha256": (
                    algebra_native_qualification.report_sha256
                ),
                "ucum_report_sha256": ucum_qualification.report_sha256,
                "sme_report_sha256": sme_review_qualification.report_sha256,
                "build08_evidence_sha256": build08_qualification.evidence_sha256,
                "paired_study_report_sha256": (
                    paired_study_qualification.report_sha256
                ),
                "safety_evidence_sha256": safety_evidence.evidence_sha256,
                "canary_report_sha256": canary_qualification.report_sha256,
                "candidate_image_digest": safety_evidence.candidate_image_digest,
                "computation_image_digest": (safety_evidence.computation_image_digest),
                "safety_monitor_receipts_sha256": (
                    safety_evidence.monitor_receipts_sha256
                ),
            }
        )
        if (
            evidence_bound
            and manifest is not None
            and workflow_evidence is not None
            and qualification_merged_evidence is not None
            and mutation_qualification is not None
            and native_qualification is not None
            and algebra_native_qualification is not None
            and ucum_qualification is not None
            and sme_review_qualification is not None
            and build08_qualification is not None
            and paired_study_qualification is not None
            and safety_evidence is not None
            and canary_qualification is not None
        )
        else None
    )
    promotion = (
        QUALIFIED_SPIKE_PROMOTIONS.get(evidence_bundle_sha256)
        if evidence_bundle_sha256 is not None
        else None
    )
    promotion_gate_passed = bool(
        promotion is not None
        and manifest is not None
        and safety_evidence is not None
        and promotion.evidence_bundle_sha256 == evidence_bundle_sha256
        and promotion.manifest_sha256 == manifest.manifest_sha256
        and promotion.candidate_image_digest == safety_evidence.candidate_image_digest
        and _is_sha256(promotion.approval_record_sha256)
    )
    if not promotion_gate_passed:
        failures.append(
            "the exact evidence bundle has no separate code-reviewed promotion"
        )
    if engine_execution is not None or paired_study is not None:
        failures.append(
            "aggregate execution or study summaries are descriptive only and cannot qualify the spike"
        )

    return AcceptanceMetrics(
        observation_count=len(records),
        positive_count=len(positives),
        mutation_count=len(mutations),
        correct_validated_positive_count=len(correct_validated),
        materially_bad_validated_count=len(bad_validated),
        critical_mutations_detected=len(detected_mutations),
        false_positive_defect_flags=len(false_positive_flags),
        supported_coverage=supported_coverage,
        coverage_by_surface=coverage_by_surface,
        critical_mutation_recall=mutation_recall,
        critical_mutation_precision=mutation_precision,
        reproducibility_rate=reproducibility,
        fixture_gate_passed=fixture_gate_passed,
        engine_gate_passed=engine_gate_passed,
        algebra_native_gate_passed=algebra_native_gate_passed,
        paired_study_gate_passed=paired_study_gate_passed,
        sme_review_gate_passed=sme_review_gate_passed,
        ucum_gate_passed=ucum_gate_passed,
        safety_gate_passed=safety_gate_passed,
        canary_gate_passed=canary_gate_passed,
        promotion_gate_passed=promotion_gate_passed,
        evidence_bound=evidence_bound,
        evidence_bundle_sha256=evidence_bundle_sha256,
        spike_passed=(
            fixture_gate_passed
            and engine_gate_passed
            and paired_study_gate_passed
            and sme_review_gate_passed
            and ucum_gate_passed
            and safety_gate_passed
            and canary_gate_passed
            and promotion_gate_passed
            and evidence_bound
        ),
        failures=list(dict.fromkeys(failures)),
    )


def _build_computation_fixture(
    *,
    family: str,
    index: int,
    raw_case: Any,
    provenance: FixtureProvenance,
) -> ComputationFixture:
    if not isinstance(raw_case, dict):
        raise ValueError(f"{family} fixture {index} must be an object")
    fixture_id = f"acv0-{family}-{index:02d}"
    expression_payload = _expression_payload(raw_case.get("expression"))
    operation = "convert_unit" if family == "unit" else raw_case.get("operation")
    comparison_payload = (
        _optional_expression_payload(raw_case.get("comparison_expression"))
        if operation == "equivalent"
        else None
    )
    equation_rhs_payload = _optional_expression_payload(raw_case.get("equation_rhs"))
    substitution_payload = {
        name: _expression_payload(value)
        for name, value in _require_mapping(
            raw_case.get("substitutions", {}), "substitutions"
        ).items()
    }
    symbols = set(_collect_symbols(expression_payload))
    if comparison_payload is not None:
        symbols.update(_collect_symbols(comparison_payload))
    if equation_rhs_payload is not None:
        symbols.update(_collect_symbols(equation_rhs_payload))
    symbols.update(substitution_payload)
    payload: dict[str, Any] = {
        "schema_version": "assessment-computation-v0",
        "profile": {
            "family": family,
            # Symbolic learner responses are external-engine-only in v0.
            "delivery": "webwork" if family == "algebraic" else "numerical",
        },
        "operation": operation,
        "expression": expression_payload,
        "comparison_expression": comparison_payload,
        "equation_rhs": equation_rhs_payload,
        "solve_for": raw_case.get("solve_for"),
        "variables": [{"name": name, "domain": "real"} for name in sorted(symbols)],
        "substitutions": substitution_payload,
        "source_unit": raw_case.get("source_unit"),
        "target_unit": raw_case.get("target_unit"),
        "seed_count": 25,
    }
    if raw_case.get("tolerance") is not None:
        payload["tolerance"] = _tolerance_payload(raw_case["tolerance"])
    elif family == "unit":
        payload["tolerance"] = {
            "absolute": "0.000000001",
            "relative": "0.000000001",
        }
    blueprint = AssessmentComputationBlueprint.model_validate(payload)
    identity = {
        "fixture_id": fixture_id,
        "slug": raw_case.get("slug"),
        "family": family,
        "difficulty": _difficulty_for_index(index),
        "split": _split_for_index(index),
        "blueprint": blueprint.model_dump(mode="json"),
        "expected_exact": raw_case.get("expected_exact"),
        "expected_solutions": raw_case.get("expected_solutions", []),
        "provenance_id": provenance.provenance_id,
        "review_status": provenance.review_status,
    }
    return ComputationFixture(
        **identity,
        fixture_sha256=_sha256_json(identity),
    )


def _build_lineage(
    index: int,
    raw_lineage: Any,
    provenance: FixtureProvenance,
) -> ParameterizedLineage:
    if not isinstance(raw_lineage, dict):
        raise ValueError(f"lineage {index} must be an object")
    variables = [
        _build_parameter_variable(raw_variable)
        for raw_variable in _require_sequence(raw_lineage.get("variables"), "variables")
    ]
    constraints = [
        _comparison_payload(raw_constraint)
        for raw_constraint in _require_sequence(
            raw_lineage.get("constraints", []), "constraints"
        )
    ]
    expression = ExpressionNode.model_validate(
        _expression_payload(raw_lineage.get("expression"))
    )
    identity = {
        "lineage_id": f"acv0-lineage-{index:02d}",
        "slug": raw_lineage.get("slug"),
        "difficulty": _difficulty_for_index(index),
        "split": _split_for_index(index),
        "provenance_id": provenance.provenance_id,
        "review_status": provenance.review_status,
        "expression": expression.model_dump(mode="json"),
        "constraints": constraints,
        "variable_specs": [variable.model_dump(mode="json") for variable in variables],
        "prompt_template": raw_lineage.get("prompt"),
        "explanation_template": raw_lineage.get("explanation"),
        "tolerance": float(raw_lineage.get("tolerance")),
        "units": raw_lineage.get("units"),
    }
    return ParameterizedLineage(
        **identity,
        lineage_sha256=_sha256_json(identity),
    )


def _build_engine_twin(
    lineage: ParameterizedLineage,
    engine: Literal["webwork", "imathas"],
) -> EngineTwinFixture:
    blueprint = _lineage_blueprint_for_engine(lineage, engine)
    result = compute_blueprint(blueprint)
    spec = parameterized_spec_from_blueprint(blueprint, result)
    answer_expression, constraints = parameterized_typed_inputs(blueprint, result)
    compiled = compile_typed_parameterized_item(
        spec,
        answer_expression=answer_expression,
        constraints=constraints,
        validation_seeds=25,
        validation_seed_values=deterministic_seeds(blueprint),
    )
    fixture_id = f"acv0-{engine}-{lineage.lineage_id.removeprefix('acv0-')}"
    identity = {
        "fixture_id": fixture_id,
        "lineage_id": lineage.lineage_id,
        "engine": engine,
        "difficulty": lineage.difficulty,
        "split": lineage.split,
        "answer_kind": "numeric",
        "parameterized_spec": spec.model_dump(mode="json"),
        "compiler_version": compiled.compiler_version,
        "source_sha256": compiled.source_sha256,
        "qualification_status": "planned_native_execution",
    }
    return EngineTwinFixture(
        **identity,
        fixture_sha256=_sha256_json(identity),
    )


def _build_algebra_native_plan(
    case: ComputationFixture,
    engine: Literal["webwork", "imathas"],
) -> AlgebraNativePlanCase:
    """Seal a qualification-only native grader plan for one algebra fixture."""

    if case.family != "algebraic":
        raise ValueError("algebra native plans require algebraic fixtures")
    result = compute_blueprint(case.blueprint)
    compiled = compile_typed_algebra_qualification_item(
        case.blueprint,
        result,
        engine=engine,
    )
    fixture_number = case.fixture_id.rsplit("-", 1)[-1]
    identity: dict[str, Any] = {
        "schema_version": "assessment-computation-algebra-native-plan-v0",
        "plan_id": f"acv0-algebra-{engine}-{fixture_number}",
        "fixture_id": case.fixture_id,
        "fixture_sha256": case.fixture_sha256,
        "engine": engine,
        "operation": case.blueprint.operation.value,
        "answer_kind": compiled.answer_kind,
        "source_contract": (
            "qualification_only_solution_set_v0"
            if compiled.answer_kind == "solution_set"
            else "production_typed_ast_v0"
        ),
        "coverage_scope": "learner_delivery_native_qualification",
        "production_delivery_eligible": False,
        "applicability": "fixed_template_native_receipt_required",
        "compiler_version": compiled.compiler_version,
        "source": compiled.source,
        "source_sha256": compiled.source_sha256,
        "response_symbols": list(compiled.response_symbols),
        "correct_submission_sha256": hashlib.sha256(
            compiled.correct_submission.encode("utf-8")
        ).hexdigest(),
        "alternate_correct_submission_sha256": (
            hashlib.sha256(
                compiled.alternate_correct_submission.encode("utf-8")
            ).hexdigest()
            if compiled.alternate_correct_submission is not None
            else None
        ),
        "wrong_submission_sha256": hashlib.sha256(
            compiled.wrong_submission.encode("utf-8")
        ).hexdigest(),
        "qualification_status": "planned_not_executed",
        "native_receipts_present": False,
    }
    return AlgebraNativePlanCase(
        **identity,
        plan_sha256=_sha256_json(identity),
    )


def _build_formula_qualification_cases() -> list[FormulaQualificationCase]:
    raw_cases = {
        "substitute": (
            ["add", ["mul", ["integer", 2], ["symbol", "x"]], ["integer", 1]],
            ["add", ["integer", 1], ["mul", ["symbol", "x"], ["integer", 2]]],
        ),
        "expand": (
            [
                "add",
                [
                    "add",
                    ["pow", ["symbol", "x"], ["integer", 2]],
                    ["mul", ["integer", 3], ["symbol", "x"]],
                ],
                ["integer", 2],
            ],
            [
                "mul",
                ["add", ["symbol", "x"], ["integer", 1]],
                ["add", ["symbol", "x"], ["integer", 2]],
            ],
        ),
        "factor": (
            [
                "mul",
                ["sub", ["symbol", "x"], ["integer", 1]],
                ["add", ["symbol", "x"], ["integer", 1]],
            ],
            ["sub", ["pow", ["symbol", "x"], ["integer", 2]], ["integer", 1]],
        ),
        "equivalent": (
            ["mul", ["integer", 2], ["add", ["symbol", "x"], ["integer", 3]]],
            ["add", ["mul", ["integer", 2], ["symbol", "x"]], ["integer", 6]],
        ),
    }
    return [
        FormulaQualificationCase(
            case_id=f"acv0-formula-{engine}-{operation}",
            engine=engine,
            operation=operation,
            expression=ExpressionNode.model_validate(
                _expression_payload(raw_cases[operation][0])
            ),
            comparison_expression=ExpressionNode.model_validate(
                _expression_payload(raw_cases[operation][1])
            ),
            status="qualification_pending",
            reason=(
                f"The fixed {operation} symbolic template requires fresh native "
                "grader receipts before it can be called qualified."
            ),
            native_receipts_present=False,
        )
        for engine in ("webwork", "imathas")
        for operation in FORMULA_QUALIFICATION_OPERATIONS
    ]


def _build_mutations(
    computation_cases: Sequence[ComputationFixture],
    twins: Sequence[EngineTwinFixture],
    lineages: Sequence[ParameterizedLineage],
) -> list[MutationFixture]:
    positives: list[tuple[str, EvaluationSurface, FixtureSplit, int]] = [
        (
            case.fixture_id,
            EvaluationSurface(case.family),
            case.split,
            index,
        )
        for index, case in enumerate(computation_cases, start=1)
    ] + [
        (
            case.fixture_id,
            EvaluationSurface(case.engine),
            case.split,
            index,
        )
        for index, case in enumerate(twins, start=1)
    ]
    semantic_transforms = {
        EvaluationSurface.NUMERIC: (
            "answer_plus_one",
            "wrong_tolerance",
            "sign_inversion",
            "wrong_substitution",
        ),
        EvaluationSurface.ALGEBRAIC: (
            "wrong_coefficient",
            "omitted_solution_branch",
            "false_equivalence",
            "duplicate_equivalent_choice",
        ),
        EvaluationSurface.UNIT: (
            "wrong_scale_factor",
            "incompatible_candidate_unit",
            "omitted_conversion",
            "wrong_dimension",
        ),
        EvaluationSurface.WEBWORK: (
            "answer_plus_one",
            "constraint_removed",
            "source_hash_mismatch",
            "wrong_tolerance",
        ),
        EvaluationSurface.IMATHAS: (
            "answer_plus_one",
            "constraint_removed",
            "source_hash_mismatch",
            "wrong_tolerance",
        ),
    }
    safety_transforms = (
        "unknown_ast_operator",
        "forbidden_code_field",
        "forbidden_url_field",
        "ast_depth_17",
        "ast_nodes_129",
        "decimal_digits_101",
        "exponent_magnitude_13",
        "unknown_symbol",
        "zero_denominator",
        "unsupported_unit",
    )

    mutations: list[MutationFixture] = []
    computation_by_id = {case.fixture_id: case for case in computation_cases}
    twins_by_id = {case.fixture_id: case for case in twins}
    lineages_by_id = {case.lineage_id: case for case in lineages}
    for fixture_id, surface, split, index in positives:
        suffix = fixture_id.removeprefix(f"acv0-{surface.value}-")
        for kind, transform, expected_states in (
            (
                MutationKind.SEMANTIC,
                semantic_transforms[surface][(index - 1) % 4],
                [ObservedValidationState.VALIDATION_FAILED],
            ),
            (
                MutationKind.SAFETY_BOUNDARY,
                safety_transforms[(index - 1) % len(safety_transforms)],
                [
                    ObservedValidationState.VALIDATION_FAILED,
                    ObservedValidationState.UNSUPPORTED,
                ],
            ),
        ):
            short_kind = "semantic" if kind == MutationKind.SEMANTIC else "safety"
            identity = {
                "mutation_id": (f"acv0-{surface.value}-{suffix}-{short_kind}"),
                "parent_fixture_id": fixture_id,
                "surface": surface,
                "split": split,
                "kind": kind,
                "critical": True,
                "transform": transform,
                "expected_states": expected_states,
                "materialization_revision": (
                    "assessment-computation-mutation-materialization-v0"
                ),
                "expected_mutated_payload_sha256": (
                    _expected_mutated_payload_sha256(
                        fixture_id=fixture_id,
                        kind=kind,
                        transform=transform,
                        computation_case=computation_by_id.get(fixture_id),
                        twin=twins_by_id.get(fixture_id),
                        lineage=(
                            lineages_by_id[twins_by_id[fixture_id].lineage_id]
                            if fixture_id in twins_by_id
                            else None
                        ),
                    )
                ),
                "review_status": "awaiting_sme_review",
            }
            mutations.append(
                MutationFixture(
                    **identity,
                    mutation_sha256=_sha256_json(identity),
                )
            )
    return mutations


def _expected_mutated_payload_sha256(
    *,
    fixture_id: str,
    kind: MutationKind,
    transform: str,
    computation_case: ComputationFixture | None,
    twin: EngineTwinFixture | None,
    lineage: ParameterizedLineage | None,
) -> str:
    if computation_case is not None:
        request = _baseline_validation_request(computation_case.blueprint)
        payload = (
            _materialize_safety_payload(transform, request)
            if kind == MutationKind.SAFETY_BOUNDARY
            else _semantic_computation_request_payload(transform, request)
        )
        return _sha256_json(payload)
    if twin is None or lineage is None:
        raise RuntimeError(f"mutation parent is not materializable: {fixture_id}")
    if kind == MutationKind.SAFETY_BOUNDARY:
        request = ComputationValidationRequest(
            blueprint=build_lineage_blueprint(twin, lineage)
        )
        return _sha256_json(_materialize_safety_payload(transform, request))
    return _sha256_json(_materialize_engine_semantic_payload(twin, transform))


def _require_expected_mutation_payload_hash(
    mutation: MutationFixture,
    observed: str,
) -> None:
    if observed != mutation.expected_mutated_payload_sha256:
        raise RuntimeError(
            f"mutation materialization drifted from fixture: {mutation.mutation_id}"
        )


def _materialize_engine_semantic_payload(
    twin: EngineTwinFixture,
    transform: str,
) -> dict[str, Any]:
    spec_payload = twin.parameterized_spec.model_dump(mode="json")
    claimed_source_sha256 = twin.source_sha256
    if transform == "source_hash_mismatch":
        claimed_source_sha256 = "0" * 64
    elif transform == "answer_plus_one":
        spec_payload["answer_expression"] = f"({spec_payload['answer_expression']}) + 1"
    elif transform == "constraint_removed":
        constraints = list(spec_payload["constraints"])
        if constraints:
            spec_payload["constraints"] = constraints[1:]
        else:
            # The parameter range is itself a compiler-enforced constraint.
            # Widening it materializes removal even on unconstrained lineages.
            variable = dict(spec_payload["variables"][0])
            variable["maximum"] = variable["maximum"] + variable["step"]
            spec_payload["variables"][0] = variable
    elif transform == "wrong_tolerance":
        spec_payload["tolerance"] = max(float(spec_payload["tolerance"]) + 1.0, 1.0)
    else:
        raise RuntimeError(f"unknown engine semantic mutation: {transform}")
    return {
        "spec": spec_payload,
        "claimed_source_sha256": claimed_source_sha256,
    }


def _materialize_safety_payload(
    transform: str,
    baseline_request: ComputationValidationRequest,
) -> dict[str, Any]:
    payload = baseline_request.model_dump(mode="json")
    blueprint = payload["blueprint"]
    if transform == "unknown_ast_operator":
        blueprint["expression"] = {"kind": "sin", "args": []}
    elif transform == "forbidden_code_field":
        payload["code"] = "blocked_inert_probe"
    elif transform == "forbidden_url_field":
        payload["url"] = "https://invalid.example/never-requested"
    elif transform == "ast_depth_17":
        blueprint["expression"] = _nested_negation_payload(17)
    elif transform == "ast_nodes_129":
        blueprint["expression"] = _balanced_addition_payload(65)
    elif transform == "decimal_digits_101":
        blueprint["expression"] = {"kind": "decimal", "decimal": "1" * 101}
    elif transform == "exponent_magnitude_13":
        blueprint["expression"] = {
            "kind": "pow",
            "args": [
                {"kind": "integer", "integer": 2},
                {"kind": "integer", "integer": 13},
            ],
        }
    elif transform == "unknown_symbol":
        blueprint["expression"] = {
            "kind": "symbol",
            "symbol": "undeclared_probe",
        }
    elif transform == "zero_denominator":
        blueprint["expression"] = {
            "kind": "rational",
            "numerator": 1,
            "denominator": 0,
        }
    elif transform == "unsupported_unit":
        if blueprint["operation"] == "convert_unit":
            blueprint["target_unit"] = "ft"
        else:
            blueprint["source_unit"] = "ft"
            blueprint["target_unit"] = "ft"
    else:
        raise RuntimeError(f"unknown safety mutation: {transform}")
    return payload


def _execute_computation_mutation(
    mutation: MutationFixture,
    case: ComputationFixture,
) -> OfflineMutationReceipt:
    baseline_request = _baseline_validation_request(case.blueprint)
    baseline_report = validate_computation(baseline_request)
    if baseline_report.status not in {
        ValidationStatus.VALIDATED,
        ValidationStatus.PARTIALLY_VALIDATED,
    }:
        raise RuntimeError(
            f"positive fixture {case.fixture_id} did not pass its typed baseline"
        )
    if mutation.kind == MutationKind.SAFETY_BOUNDARY:
        return _execute_safety_mutation(
            mutation,
            baseline_request,
            parent_fixture_sha256=case.fixture_sha256,
        )

    payload = _semantic_computation_request_payload(
        mutation.transform,
        baseline_request,
    )
    mutated_payload_sha256 = _sha256_json(payload)
    _require_expected_mutation_payload_hash(mutation, mutated_payload_sha256)
    try:
        mutated_request = ComputationValidationRequest.model_validate(payload)
    except Exception:
        return _build_mutation_receipt(
            mutation,
            parent_fixture_sha256=case.fixture_sha256,
            detection_layer=MutationDetectionLayer.TYPED_SCHEMA,
            observed_state=ObservedValidationState.VALIDATION_FAILED,
            outcome_code="schema_rejected",
            mutated_payload_sha256=mutated_payload_sha256,
        )
    report = validate_computation(mutated_request)
    replay = validate_computation(mutated_request)
    if _sha256_json(report.model_dump(mode="json")) != _sha256_json(
        replay.model_dump(mode="json")
    ):
        raise RuntimeError(
            f"mutation {mutation.mutation_id} replay was not deterministic"
        )
    if report.status not in {
        ValidationStatus.VALIDATION_FAILED,
        ValidationStatus.UNSUPPORTED,
    }:
        raise RuntimeError(
            f"critical semantic mutation was not detected: {mutation.mutation_id}"
        )
    return _build_mutation_receipt(
        mutation,
        parent_fixture_sha256=case.fixture_sha256,
        detection_layer=MutationDetectionLayer.TYPED_VALIDATOR,
        observed_state=ObservedValidationState(report.status.value),
        outcome_code="validator_rejected_semantic_mismatch",
        mutated_payload_sha256=mutated_payload_sha256,
    )


def _execute_engine_mutation(
    mutation: MutationFixture,
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
) -> OfflineMutationReceipt:
    if mutation.kind == MutationKind.SAFETY_BOUNDARY:
        blueprint = build_lineage_blueprint(twin, lineage)
        # A few compiler-qualified numeric lineages intentionally exceed the
        # symbolic computation subset. Safety mutation tests need only the real
        # strict typed request boundary; they must not pretend those lineages
        # have a positive symbolic computation result.
        baseline_request = ComputationValidationRequest(blueprint=blueprint)
        return _execute_safety_mutation(
            mutation,
            baseline_request,
            parent_fixture_sha256=twin.fixture_sha256,
        )

    transform = mutation.transform
    mutated_payload = _materialize_engine_semantic_payload(twin, transform)
    mutated_payload_sha256 = _sha256_json(mutated_payload)
    _require_expected_mutation_payload_hash(mutation, mutated_payload_sha256)
    if transform == "source_hash_mismatch":
        if twin.source_sha256 == mutated_payload["claimed_source_sha256"]:
            raise RuntimeError("source-hash mutation did not alter the sealed hash")
        return _build_mutation_receipt(
            mutation,
            parent_fixture_sha256=twin.fixture_sha256,
            detection_layer=MutationDetectionLayer.SEALED_HASH_BINDING,
            observed_state=ObservedValidationState.VALIDATION_FAILED,
            outcome_code="source_hash_binding_failed",
            mutated_payload_sha256=mutated_payload_sha256,
        )
    spec_payload = mutated_payload["spec"]
    try:
        mutated_spec = ParameterizedItemSpec.model_validate(spec_payload)
        compiled = compile_parameterized_item(mutated_spec, validation_seeds=25)
    except (ValueError, ParameterizedCompileError):
        return _build_mutation_receipt(
            mutation,
            parent_fixture_sha256=twin.fixture_sha256,
            detection_layer=MutationDetectionLayer.PARAMETERIZED_COMPILER,
            observed_state=ObservedValidationState.VALIDATION_FAILED,
            outcome_code="compiler_rejected_mutation",
            mutated_payload_sha256=mutated_payload_sha256,
        )
    replay = compile_parameterized_item(mutated_spec, validation_seeds=25)
    if (
        compiled.source_sha256 != replay.source_sha256
        or compiled.compiler_version != replay.compiler_version
    ):
        raise RuntimeError(
            f"mutation {mutation.mutation_id} compile was nondeterministic"
        )
    if (
        compiled.source_sha256 == twin.source_sha256
        and mutated_payload_sha256
        == _sha256_json(twin.parameterized_spec.model_dump(mode="json"))
    ):
        raise RuntimeError(
            f"critical compiler mutation was not detected: {mutation.mutation_id}"
        )
    return _build_mutation_receipt(
        mutation,
        parent_fixture_sha256=twin.fixture_sha256,
        detection_layer=MutationDetectionLayer.SEALED_HASH_BINDING,
        observed_state=ObservedValidationState.VALIDATION_FAILED,
        outcome_code="compiled_source_binding_failed",
        mutated_payload_sha256=mutated_payload_sha256,
    )


def _execute_safety_mutation(
    mutation: MutationFixture,
    baseline_request: ComputationValidationRequest,
    *,
    parent_fixture_sha256: str,
) -> OfflineMutationReceipt:
    payload = _materialize_safety_payload(mutation.transform, baseline_request)
    mutated_payload_sha256 = _sha256_json(payload)
    _require_expected_mutation_payload_hash(mutation, mutated_payload_sha256)
    try:
        request = ComputationValidationRequest.model_validate(payload)
    except Exception:
        return _build_mutation_receipt(
            mutation,
            parent_fixture_sha256=parent_fixture_sha256,
            detection_layer=MutationDetectionLayer.TYPED_SCHEMA,
            observed_state=ObservedValidationState.VALIDATION_FAILED,
            outcome_code="schema_rejected",
            mutated_payload_sha256=mutated_payload_sha256,
        )
    report = validate_computation(request)
    if report.status not in {
        ValidationStatus.UNSUPPORTED,
        ValidationStatus.VALIDATION_FAILED,
    }:
        raise RuntimeError(
            f"critical safety mutation was not detected: {mutation.mutation_id}"
        )
    return _build_mutation_receipt(
        mutation,
        parent_fixture_sha256=parent_fixture_sha256,
        detection_layer=MutationDetectionLayer.TYPED_VALIDATOR,
        observed_state=ObservedValidationState(report.status.value),
        outcome_code="validator_rejected_unsafe_payload",
        mutated_payload_sha256=mutated_payload_sha256,
    )


def _baseline_validation_request(
    blueprint: AssessmentComputationBlueprint,
) -> ComputationValidationRequest:
    result = compute_blueprint(blueprint)
    payload: dict[str, Any] = {"blueprint": blueprint.model_dump(mode="json")}
    if blueprint.operation.value == "solve":
        payload["candidate_solutions"] = [
            expression.model_dump(mode="json")
            for expression in result.solution_expressions
        ]
    elif blueprint.operation.value != "equivalent":
        if result.answer_expression is None:
            raise RuntimeError("positive blueprint did not produce a typed answer")
        payload["candidate_expression"] = result.answer_expression.model_dump(
            mode="json"
        )
        if blueprint.profile.family.value == "unit":
            payload["candidate_unit"] = blueprint.target_unit
    return ComputationValidationRequest.model_validate(payload)


def _semantic_computation_request_payload(
    transform: str,
    baseline_request: ComputationValidationRequest,
) -> dict[str, Any]:
    payload = baseline_request.model_dump(mode="json")
    blueprint = payload["blueprint"]
    operation = blueprint["operation"]
    if operation == "equivalent":
        comparison = blueprint["comparison_expression"]
        blueprint["comparison_expression"] = _add_one_payload(comparison)
        return payload
    if operation == "solve":
        solutions = list(payload.get("candidate_solutions") or [])
        if transform == "omitted_solution_branch":
            payload["candidate_solutions"] = solutions[:-1]
        elif transform == "duplicate_equivalent_choice" and solutions:
            payload["candidate_solutions"] = [*solutions, solutions[0]]
        elif solutions:
            payload["candidate_solutions"][0] = _add_one_payload(solutions[0])
        else:
            payload["candidate_solutions"] = [{"kind": "integer", "integer": 0}]
        return payload
    candidate = payload["candidate_expression"]
    if transform == "sign_inversion":
        payload["candidate_expression"] = {"kind": "neg", "args": [candidate]}
    elif transform == "omitted_conversion":
        payload["candidate_expression"] = blueprint["expression"]
        payload["candidate_unit"] = None
    elif transform in {"incompatible_candidate_unit", "wrong_dimension"}:
        payload["candidate_unit"] = "s" if blueprint.get("target_unit") != "s" else "m"
    else:
        payload["candidate_expression"] = _add_one_payload(candidate)
    return payload


def _build_mutation_receipt(
    mutation: MutationFixture,
    *,
    parent_fixture_sha256: str,
    detection_layer: MutationDetectionLayer,
    observed_state: ObservedValidationState,
    outcome_code: str,
    mutated_payload_sha256: str,
) -> OfflineMutationReceipt:
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-mutation-receipt-v0",
        "runner_version": OFFLINE_MUTATION_RUNNER_VERSION,
        "mutation_id": mutation.mutation_id,
        "mutation_sha256": mutation.mutation_sha256,
        "parent_fixture_id": mutation.parent_fixture_id,
        "parent_fixture_sha256": parent_fixture_sha256,
        "surface": mutation.surface,
        "kind": mutation.kind,
        "transform": mutation.transform,
        "detection_layer": detection_layer,
        "observed_state": observed_state,
        "outcome_code": outcome_code,
        "detected": True,
        "network_calls": 0,
        "native_engine_executions": 0,
        "mutated_payload_sha256": mutated_payload_sha256,
    }
    content["receipt_sha256"] = _sha256_json(content)
    return OfflineMutationReceipt.model_validate(content)


def _validate_native_receipt(
    receipt: NativeQualificationReceipt,
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
    *,
    manifest_sha256: str,
    plan_sha256: str,
    trust_policy: NativeQualificationTrustPolicy | None,
) -> str | None:
    if trust_policy is not None:
        expected_engine_digest = (
            trust_policy.webwork_engine_image_digest
            if receipt.engine == "webwork"
            else trust_policy.imathas_engine_image_digest
        )
        if (
            receipt.run_id != trust_policy.run_id
            or receipt.endpoint_profile != trust_policy.endpoint_profile
            or receipt.engine_image_digest != expected_engine_digest
            or receipt.network_attestation_sha256
            != trust_policy.network_attestation_sha256
            or (
                receipt.engine == "imathas"
                and (
                    receipt.adapter_image_digest
                    != trust_policy.imathas_adapter_image_digest
                    or receipt.imathas_namespace != trust_policy.imathas_namespace
                )
            )
        ):
            return "operator_trust_policy_binding_mismatch"
    if (
        receipt.manifest_sha256 != manifest_sha256
        or receipt.plan_sha256 != plan_sha256
        or receipt.fixture_sha256 != twin.fixture_sha256
        or receipt.lineage_sha256 != lineage.lineage_sha256
    ):
        return "manifest_plan_fixture_binding_mismatch"
    if receipt.lineage_id != twin.lineage_id or receipt.engine != twin.engine:
        return "lineage_or_engine_mismatch"
    if receipt.answer_kind != twin.answer_kind:
        return "answer_kind_mismatch"
    blueprint = build_lineage_blueprint(twin, lineage)
    if receipt.lineage_blueprint_sha256 != canonical_blueprint_hash(blueprint):
        return "lineage_blueprint_hash_mismatch"
    if (
        receipt.compiler_version != twin.compiler_version
        or receipt.source_sha256 != twin.source_sha256
    ):
        return "compiler_source_binding_mismatch"
    values = {
        name: value.as_runtime_number()
        for name, value in receipt.engine_observed_values.items()
    }
    try:
        constraints_satisfied = parameterized_constraints_satisfied(
            twin.parameterized_spec,
            values,
        )
        fixed_payload = blueprint.model_dump(mode="json")
        fixed_payload["variables"] = [
            {
                "name": variable.name,
                "domain": variable.domain.value,
                "assumptions": [
                    assumption.value for assumption in variable.assumptions
                ],
            }
            for variable in blueprint.variables
        ]
        fixed_payload["constraints"] = []
        fixed_payload["substitutions"] = {
            name: (
                {"kind": "integer", "integer": value.integer}
                if value.kind == "integer"
                else {"kind": "decimal", "decimal": value.decimal}
            )
            for name, value in receipt.engine_observed_values.items()
        }
        fixed_blueprint = AssessmentComputationBlueprint.model_validate(fixed_payload)
        exact_result = compute_blueprint(fixed_blueprint)
    except (TypeError, ValueError, ParameterizedCompileError):
        return "engine_observed_values_invalid"
    if not constraints_satisfied or not receipt.constraints_satisfied:
        return "constraints_not_satisfied"
    if exact_result.numeric_value is None:
        return "numeric_oracle_unavailable"
    expected_number = Decimal(exact_result.numeric_value)
    correct = Decimal(receipt.engine_observed_correct_answer)
    wrong = Decimal(receipt.engine_observed_wrong_answer)
    if not all(value.is_finite() for value in (expected_number, correct, wrong)):
        return "nonfinite_engine_value"
    absolute_tolerance = Decimal(blueprint.tolerance.absolute)
    relative_tolerance = Decimal(blueprint.tolerance.relative)
    tolerance = max(
        absolute_tolerance,
        relative_tolerance * abs(expected_number),
        Decimal("0.000000000001"),
    )
    if abs(correct - expected_number) > tolerance:
        return "engine_answer_mismatch"
    if abs(wrong - expected_number) <= tolerance:
        return "wrong_answer_not_distinct"
    expected_wrong = expected_number + max(tolerance * 10, Decimal(1))
    if abs(wrong - expected_wrong) > Decimal("0.000000000001"):
        return "wrong_answer_probe_mismatch"
    if not receipt.correct_answer_accepted:
        return "correct_answer_rejected"
    if not receipt.wrong_answer_rejected:
        return "wrong_answer_accepted"
    if not receipt.rendered:
        return "render_failed"
    if receipt.render_sha256 != receipt.repeat_render_sha256:
        return "repeat_render_hash_mismatch"
    if receipt.warnings:
        return "engine_warning_present"
    if receipt.errors:
        return "engine_error_present"
    if receipt.outbound_request_count != 0:
        return "engine_egress_observed"
    return None


def _run_libretexts_unit_corpus(
    manifest: ComputationEvaluationManifest,
) -> int:
    cases = [case for case in manifest.computation_cases if case.family == "unit"]
    if len(cases) != 20:
        raise ValueError("LibreTexts unit qualification corpus requires 20 cases")
    passed = 0
    for case in cases:
        try:
            result = compute_blueprint(case.blueprint)
            if (
                result.numeric_value is not None
                and case.expected_exact is not None
                and math.isclose(
                    float(result.numeric_value),
                    _expected_numeric_literal(case.expected_exact),
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            ):
                passed += 1
        except Exception:
            continue
    return passed


def _verify_ucum_runtime_integrity() -> tuple[bool, dict[str, str]]:
    observed: dict[str, str] = {}
    try:
        if importlib.metadata.version("ucumvert") != "0.3.2":
            return False, observed
        if importlib.metadata.version("Pint") != "0.25.3":
            return False, observed
        specification = importlib.util.find_spec("ucumvert")
        if specification is None or specification.origin is None:
            return False, observed
        package_root = Path(specification.origin).resolve().parent
        expected = {
            "ucum-essence.xml": (
                package_root / "vendor" / "ucum-essence.xml",
                UCUM_ESSENCE_SHA256,
            ),
            "ucum_grammar.lark": (
                package_root / "ucum_grammar.lark",
                UCUMVERT_GRAMMAR_SHA256,
            ),
            "pint_ucum_defs.txt": (
                package_root / "pint_ucum_defs.txt",
                PINT_UCUM_DEFINITIONS_SHA256,
            ),
        }
        for name, (path, digest) in expected.items():
            if path.is_symlink() or not path.is_file():
                return False, observed
            observed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            if observed[name] != digest:
                return False, observed
    except (OSError, importlib.metadata.PackageNotFoundError):
        return False, observed
    return True, observed


def _select_ucum_functional_cases(
    root: ElementTree.Element,
) -> tuple[int, list[dict[str, str]]]:
    discovered = sum(
        element.tag.rsplit("}", maxsplit=1)[-1].lower() in {"case", "test"}
        for element in root.iter()
    )
    selected: list[dict[str, str]] = []
    validation_elements: list[ElementTree.Element] = []
    conversion_elements: list[ElementTree.Element] = []
    for section in root.iter():
        section_tag = section.tag.rsplit("}", maxsplit=1)[-1].lower()
        if section_tag == "validation":
            validation_elements.extend(
                child
                for child in list(section)
                if child.tag.rsplit("}", maxsplit=1)[-1].lower() in {"case", "test"}
            )
        elif section_tag == "conversion":
            conversion_elements.extend(
                child
                for child in list(section)
                if child.tag.rsplit("}", maxsplit=1)[-1].lower() in {"case", "test"}
            )
    # A small synthetic fixture may consist solely of conversion-shaped case
    # elements without the official section wrapper.
    if not validation_elements and not conversion_elements:
        conversion_elements = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", maxsplit=1)[-1].lower() in {"case", "test"}
        ]
    for ordinal, element in enumerate(validation_elements):
        attributes = {
            key.rsplit("}", maxsplit=1)[-1].lower(): value
            for key, value in element.attrib.items()
        }
        unit_code = attributes.get("unit")
        expected_valid_text = attributes.get("valid")
        if unit_code is None or expected_valid_text not in {"true", "false"}:
            continue
        expected_valid = expected_valid_text == "true"
        if expected_valid:
            try:
                validate_unit_code(unit_code)
            except ValueError:
                # An upstream-valid code outside the named profile is not part of
                # this qualified subset.
                continue
        selected.append(
            {
                "case_kind": "validation",
                "test_id": (
                    f"validation:{ordinal}:{attributes.get('id') or 'anonymous'}:"
                    f"{_sha256_json(attributes)[:12]}"
                ),
                "unit_code": unit_code,
                "expected_valid": expected_valid_text,
            }
        )
    for ordinal, element in enumerate(conversion_elements):
        attributes = {
            key.rsplit("}", maxsplit=1)[-1].lower(): value
            for key, value in element.attrib.items()
        }
        source = _first_present(attributes, ("srcunit", "src", "source", "sourceunit"))
        target = _first_present(attributes, ("dstunit", "dst", "target", "targetunit"))
        input_value = _first_present(attributes, ("value", "input", "inputvalue"))
        expected = _first_present(
            attributes, ("outcome", "expected", "output", "outputvalue")
        )
        if None in {source, target, input_value, expected}:
            continue
        try:
            validate_unit_code(source)
            validate_unit_code(target)
            normalized_input = _canonical_decimal(Decimal(input_value))
            normalized_expected = _canonical_decimal(Decimal(expected))
        except (ValueError, ArithmeticError):
            continue
        selected.append(
            {
                "case_kind": "conversion",
                "test_id": (
                    f"conversion:{ordinal}:"
                    f"{attributes.get('id') or 'anonymous'}:"
                    f"{_sha256_json(attributes)[:12]}"
                ),
                "source_unit": source,
                "target_unit": target,
                "input_value": normalized_input,
                "expected_value": normalized_expected,
            }
        )
    return discovered, selected


def _ucum_section_counts(root: ElementTree.Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    for section in list(root):
        section_tag = section.tag.rsplit("}", maxsplit=1)[-1].lower()
        if section_tag == "history":
            continue
        case_count = sum(
            child.tag.rsplit("}", maxsplit=1)[-1].lower() in {"case", "test"}
            for child in list(section)
        )
        if case_count:
            counts[section_tag] = case_count
    return counts


def _xml_stats(root: ElementTree.Element) -> tuple[int, int]:
    count = 0
    maximum_depth = 0
    stack: list[tuple[ElementTree.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        maximum_depth = max(maximum_depth, depth)
        stack.extend((child, depth + 1) for child in list(element))
    return count, maximum_depth


def _execute_ucum_functional_case(
    case: Mapping[str, str],
) -> UcumFunctionalCaseReceipt:
    if case["case_kind"] == "validation":
        expected_valid = case["expected_valid"] == "true"
        try:
            validate_unit_code(case["unit_code"])
            observed_valid = True
        except ValueError:
            observed_valid = False
        content: dict[str, Any] = {
            "test_id": case["test_id"],
            "case_kind": "validation",
            "unit_code": case["unit_code"],
            "expected_valid": expected_valid,
            "source_unit": None,
            "target_unit": None,
            "input_value": None,
            "expected_value": None,
            "observed_value": None,
            "status": "passed" if observed_valid == expected_valid else "failed",
        }
        content["case_sha256"] = _sha256_json(content)
        return UcumFunctionalCaseReceipt.model_validate(content)
    blueprint = AssessmentComputationBlueprint.model_validate(
        {
            "schema_version": "assessment-computation-v0",
            "profile": {"family": "unit", "delivery": "numerical"},
            "operation": "convert_unit",
            "expression": _numeric_expression_payload(Decimal(case["input_value"])),
            "source_unit": case["source_unit"],
            "target_unit": case["target_unit"],
            "tolerance": {"absolute": "0.000000000001", "relative": "0.000000000001"},
            "seed_count": 25,
        }
    )
    observed: str | None = None
    status: Literal["passed", "failed"] = "failed"
    try:
        result = compute_blueprint(blueprint)
        observed = result.numeric_value
        if observed is not None and math.isclose(
            float(observed),
            float(case["expected_value"]),
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            status = "passed"
    except Exception:
        pass
    content = {
        "test_id": case["test_id"],
        "case_kind": "conversion",
        "unit_code": None,
        "expected_valid": None,
        "source_unit": case["source_unit"],
        "target_unit": case["target_unit"],
        "input_value": case["input_value"],
        "expected_value": case["expected_value"],
        "observed_value": observed,
        "status": status,
    }
    content["case_sha256"] = _sha256_json(content)
    return UcumFunctionalCaseReceipt.model_validate(content)


def _build_ucum_report(content: dict[str, Any]) -> UcumQualificationReport:
    content["report_sha256"] = _sha256_json(content)
    return UcumQualificationReport.model_validate(content)


def _numeric_expression_payload(value: int | float | Decimal) -> dict[str, Any]:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("numeric qualification values must be finite")
    integral = decimal.to_integral_value()
    if decimal == integral:
        integer = int(integral)
        if len(str(abs(integer))) <= 100:
            return {"kind": "integer", "integer": integer}
    return {"kind": "decimal", "decimal": _canonical_decimal(decimal)}


def _load_bounded_jsonl(
    path: Path,
    model: type[BaseModel],
    *,
    label: str,
    max_records: int,
) -> LoadedEvidenceLedger:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} ledger must be an explicit local regular file")
    size = path.stat().st_size
    if size > MAX_EVIDENCE_LEDGER_BYTES:
        raise ValueError(f"{label} ledger exceeds 64 MiB")
    records: list[BaseModel] = []
    hasher = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            hasher.update(raw_line)
            byte_count += len(raw_line)
            if len(raw_line) > MAX_EVIDENCE_LINE_BYTES:
                raise ValueError(f"{label} line {line_number} exceeds 512 KiB")
            if not raw_line.strip():
                continue
            if len(records) >= max_records:
                raise ValueError(
                    f"{label} ledger contains more than {max_records} rows"
                )
            try:
                records.append(model.model_validate(_strict_json_value(raw_line)))
            except Exception as exc:
                raise ValueError(f"{label} line {line_number} is malformed") from exc
    if byte_count != size:
        raise ValueError(f"{label} ledger changed while it was imported")
    return LoadedEvidenceLedger(
        records=tuple(records),
        raw_ledger_sha256=hasher.hexdigest(),
        raw_byte_count=byte_count,
    )


def _load_bounded_json_record(
    path: Path,
    model: type[BaseModel],
    *,
    label: str,
) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an explicit local regular file")
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError(f"{label} exceeds 64 KiB")
    try:
        return model.model_validate(_strict_json_value(raw))
    except Exception as exc:
        raise ValueError(f"{label} is malformed") from exc


def _strict_json_value(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        raw,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def _canonical_decimal(value: int | float | Decimal) -> str:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("decimal must be finite")
    rendered = format(decimal, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


def _add_one_payload(expression: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "add",
        "args": [dict(expression), {"kind": "integer", "integer": 1}],
    }


def _nested_negation_payload(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "integer", "integer": 1}
    for _ in range(depth):
        node = {"kind": "neg", "args": [node]}
    return node


def _balanced_addition_payload(leaf_count: int) -> dict[str, Any]:
    nodes = [{"kind": "integer", "integer": 1} for _ in range(leaf_count)]
    while len(nodes) > 1:
        next_level: list[dict[str, Any]] = []
        for index in range(0, len(nodes), 2):
            if index + 1 == len(nodes):
                next_level.append(nodes[index])
            else:
                next_level.append(
                    {
                        "kind": "add",
                        "args": [nodes[index], nodes[index + 1]],
                    }
                )
        nodes = next_level
    return nodes[0]


def _first_present(
    values: Mapping[str, str],
    names: Sequence[str],
) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None:
            return value
    return None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_image_digest(value: str) -> bool:
    return value.startswith("sha256:") and _is_sha256(value.removeprefix("sha256:"))


def _expected_numeric_literal(value: str) -> float:
    if value == "pi":
        return math.pi
    if value == "2*pi":
        return 2 * math.pi
    if "/" in value:
        numerator, denominator = value.split("/", maxsplit=1)
        return int(numerator) / int(denominator)
    return float(value)


def _expression_payload(value: Any) -> dict[str, Any]:
    parts = _require_sequence(value, "expression node")
    if not parts or not isinstance(parts[0], str):
        raise ValueError("expression node requires a string tag")
    tag = parts[0]
    if tag == "integer" and len(parts) == 2 and type(parts[1]) is int:
        return {"kind": "integer", "integer": parts[1]}
    if (
        tag == "rational"
        and len(parts) == 3
        and type(parts[1]) is int
        and type(parts[2]) is int
    ):
        return {"kind": "rational", "numerator": parts[1], "denominator": parts[2]}
    if tag == "decimal" and len(parts) == 2 and isinstance(parts[1], str):
        return {"kind": "decimal", "decimal": parts[1]}
    if tag == "constant" and len(parts) == 2 and parts[1] in {"pi", "e"}:
        return {"kind": "constant", "constant": parts[1]}
    if tag == "symbol" and len(parts) == 2 and isinstance(parts[1], str):
        return {"kind": "symbol", "symbol": parts[1]}
    if tag == "neg" and len(parts) == 2:
        return {"kind": "neg", "args": [_expression_payload(parts[1])]}
    if tag in {"add", "sub", "mul", "div", "pow", "mod"} and len(parts) == 3:
        return {
            "kind": tag,
            "args": [
                _expression_payload(parts[1]),
                _expression_payload(parts[2]),
            ],
        }
    raise ValueError(f"unsupported compact expression node: {tag}")


def _optional_expression_payload(value: Any) -> dict[str, Any] | None:
    return None if value is None else _expression_payload(value)


def _collect_symbols(value: Mapping[str, Any]) -> set[str]:
    symbols: set[str] = set()
    if value.get("kind") == "symbol":
        symbols.add(str(value["symbol"]))
    for argument in value.get("args", []):
        symbols.update(_collect_symbols(argument))
    return symbols


def _comparison_payload(value: Any) -> dict[str, Any]:
    parts = _require_sequence(value, "comparison")
    if len(parts) != 3 or parts[0] not in {"eq", "ne", "lt", "le", "gt", "ge"}:
        raise ValueError("comparison must contain one allowlisted binary operator")
    return {
        "op": parts[0],
        "left": _expression_payload(parts[1]),
        "right": _expression_payload(parts[2]),
    }


def _tolerance_payload(value: Any) -> dict[str, Any]:
    raw = _require_mapping(value, "tolerance")
    kind = raw.get("kind")
    if kind != "absolute":
        raise ValueError("fixture catalog currently supports absolute tolerance only")
    return {"absolute": raw.get("value"), "relative": "0"}


def _build_parameter_variable(value: Any) -> ParameterVariable:
    parts = _require_sequence(value, "parameter variable")
    if len(parts) != 5 or not isinstance(parts[0], str) or type(parts[4]) is not bool:
        raise ValueError("parameter variable uses name/min/max/step/integer")
    return ParameterVariable(
        name=parts[0],
        minimum=parts[1],
        maximum=parts[2],
        step=parts[3],
        integer=parts[4],
    )


def _render_expression(node: ExpressionNode | Mapping[str, Any]) -> str:
    value = node.model_dump(mode="json") if isinstance(node, BaseModel) else dict(node)
    kind = value.get("kind")
    if kind == "integer":
        return str(value["integer"])
    if kind == "rational":
        return f"({value['numerator']} / {value['denominator']})"
    if kind == "decimal":
        return str(value["decimal"])
    if kind == "symbol":
        return str(value["symbol"])
    if kind == "constant":
        raise ValueError("parameterized v0 lineages do not allow named constants")
    args = value.get("args", [])
    if kind == "neg" and len(args) == 1:
        return f"(-{_render_expression(args[0])})"
    operators = {
        "add": "+",
        "sub": "-",
        "mul": "*",
        "div": "/",
        "pow": "**",
        "mod": "%",
    }
    if kind in operators and len(args) == 2:
        return (
            f"({_render_expression(args[0])} {operators[kind]} "
            f"{_render_expression(args[1])})"
        )
    raise ValueError("cannot render unsupported typed expression")


def _difficulty_for_index(index: int) -> FixtureDifficulty:
    if 1 <= index <= 8:
        return FixtureDifficulty.BASIC
    if 9 <= index <= 16:
        return FixtureDifficulty.INTERMEDIATE
    if 17 <= index <= 20:
        return FixtureDifficulty.BOUNDARY
    raise ValueError("fixture families require exactly twenty ordered cases")


def _split_for_index(index: int) -> FixtureSplit:
    if 1 <= index <= 14:
        return FixtureSplit.DEVELOPMENT
    if 15 <= index <= 20:
        return FixtureSplit.SEALED
    raise ValueError("fixture families require exactly twenty ordered cases")


def _validate_twenty_case_distribution(cases: Sequence[Any], label: str) -> None:
    if len(cases) != 20:
        raise ValueError(f"{label} requires exactly twenty cases")
    difficulty = Counter(case.difficulty for case in cases)
    if difficulty != {
        FixtureDifficulty.BASIC: 8,
        FixtureDifficulty.INTERMEDIATE: 8,
        FixtureDifficulty.BOUNDARY: 4,
    }:
        raise ValueError(f"{label} requires an 8/8/4 difficulty distribution")
    split = Counter(case.split for case in cases)
    if split != {FixtureSplit.DEVELOPMENT: 14, FixtureSplit.SEALED: 6}:
        raise ValueError(f"{label} requires a deterministic 14/6 split")


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _score_delta(
    treatment: Sequence[PairedReviewRecord],
    control: Sequence[PairedReviewRecord],
    field_name: Literal["source_grounding_score", "pedagogy_score"],
) -> float:
    if not treatment or not control:
        return 0.0
    treatment_mean = sum(getattr(record, field_name) for record in treatment) / len(
        treatment
    )
    control_mean = sum(getattr(record, field_name) for record in control) / len(control)
    return (treatment_mean - control_mean) / 100


def _require_list(value: Mapping[str, Any], key: str) -> list[Any]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValueError(f"catalog {key} must be a list")
    return result


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()


def _require_matching_hash(model: BaseModel, field_name: str) -> None:
    expected = getattr(model, field_name)
    observed = _sha256_json(model.model_dump(mode="json", exclude={field_name}))
    if observed != expected:
        raise ValueError(f"{field_name} does not match the canonical record")


def _json_default(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot canonically encode {type(value).__name__}")


async def execute_workflow_positive_plan(
    *,
    socket_path: Path,
    expected_runtime_manifest_sha256: str,
    output: Path,
    run_id: str,
    manifest: ComputationEvaluationManifest | None = None,
) -> tuple[WorkflowPositiveReceipt, ...]:
    """Exercise all 100 positive fixtures through the isolated UDS service.

    This entry point accepts no host name, URL, provider client, or caller-supplied
    observations. Every state, oracle result, replay result, and hash in the
    output ledger is derived from typed responses returned over the Unix socket.
    The output is created exclusively only after the complete plan succeeds.
    """

    if (
        not socket_path.is_absolute()
        or socket_path.is_symlink()
        or not socket_path.exists()
    ):
        raise ValueError(
            "workflow computation socket must be an existing absolute non-symlink"
        )
    try:
        mode = os.lstat(socket_path).st_mode
    except FileNotFoundError as exc:
        raise ValueError("workflow computation socket does not exist") from exc
    if not stat.S_ISSOCK(mode):
        raise ValueError("workflow computation path is not a Unix socket")
    if (
        not _is_sha256(expected_runtime_manifest_sha256)
        or expected_runtime_manifest_sha256 == "0" * 64
    ):
        raise ValueError(
            "workflow execution requires the exact non-test runtime manifest hash"
        )
    _validate_workflow_output_target(output)

    async with AssessmentComputationClient(
        socket_path,
        expected_runtime_manifest_sha256=expected_runtime_manifest_sha256,
    ) as client:
        return await _execute_workflow_positive_plan_with_client(
            client=client,
            output=output,
            run_id=run_id,
            manifest=manifest,
            expected_runtime_manifest_sha256=expected_runtime_manifest_sha256,
        )


async def _execute_workflow_positive_plan_with_client(
    *,
    client: ComputationClient,
    output: Path,
    run_id: str,
    manifest: ComputationEvaluationManifest | None = None,
    expected_runtime_manifest_sha256: str | None = None,
) -> tuple[WorkflowPositiveReceipt, ...]:
    """Internal dependency-injection seam used by deterministic unit tests."""

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", run_id):
        raise ValueError("workflow run ID is invalid")
    _validate_workflow_output_target(output)
    manifest = manifest or build_computation_evaluation_manifest()
    status = await client.ready()
    runtime_manifest_sha256 = status.runtime_manifest_sha256
    if not _is_sha256(runtime_manifest_sha256):
        raise ValueError("workflow service returned an invalid runtime identity")
    if (
        expected_runtime_manifest_sha256 is not None
        and runtime_manifest_sha256 != expected_runtime_manifest_sha256
    ):
        raise ValueError("workflow service runtime identity does not match")

    lineages = {
        lineage.lineage_id: lineage for lineage in manifest.parameterized_lineages
    }
    receipts: list[WorkflowPositiveReceipt] = []
    for case in manifest.computation_cases:
        receipts.append(
            await _workflow_computation_receipt(
                client,
                case=case,
                run_id=run_id,
                manifest_sha256=manifest.manifest_sha256,
                runtime_manifest_sha256=runtime_manifest_sha256,
            )
        )
    for twin in manifest.engine_twins:
        receipts.append(
            await _workflow_engine_receipt(
                client,
                twin=twin,
                lineage=lineages[twin.lineage_id],
                run_id=run_id,
                manifest_sha256=manifest.manifest_sha256,
                runtime_manifest_sha256=runtime_manifest_sha256,
            )
        )

    expected_ids = {case.fixture_id for case in manifest.computation_cases} | {
        twin.fixture_id for twin in manifest.engine_twins
    }
    if (
        len(receipts) != 100
        or len({receipt.case_id for receipt in receipts}) != 100
        or {receipt.case_id for receipt in receipts} != expected_ids
    ):
        raise RuntimeError("workflow positive execution did not cover all 100 fixtures")
    _write_workflow_positive_ledger(output, receipts)
    return tuple(receipts)


async def _workflow_computation_receipt(
    client: ComputationClient,
    *,
    case: ComputationFixture,
    run_id: str,
    manifest_sha256: str,
    runtime_manifest_sha256: str,
) -> WorkflowPositiveReceipt:
    result = await client.compute(case.blueprint)
    request = _validation_request_from_result(case.blueprint, result)
    workflow_report = await client.validate(request)
    replay_report = await client.validate(request)
    oracle_match = _computation_fixture_oracle_match(case, result, workflow_report)
    return _build_workflow_positive_receipt(
        run_id=run_id,
        manifest_sha256=manifest_sha256,
        case_id=case.fixture_id,
        fixture_sha256=case.fixture_sha256,
        surface=EvaluationSurface(case.family),
        runtime_manifest_sha256=runtime_manifest_sha256,
        request=request,
        result=result,
        workflow_report=workflow_report,
        replay_report=replay_report,
        oracle_match=oracle_match,
        compiled_source_sha256=None,
    )


async def _workflow_engine_receipt(
    client: ComputationClient,
    *,
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
    run_id: str,
    manifest_sha256: str,
    runtime_manifest_sha256: str,
) -> WorkflowPositiveReceipt:
    blueprint = _lineage_blueprint_for_engine(lineage, twin.engine)
    result = await client.compute(blueprint)
    production_spec = parameterized_spec_from_blueprint(blueprint, result)
    answer_expression, constraints = parameterized_typed_inputs(blueprint, result)
    compiled = compile_typed_parameterized_item(
        production_spec,
        answer_expression=answer_expression,
        constraints=constraints,
        validation_seeds=25,
        validation_seed_values=deterministic_seeds(blueprint),
    )
    request = _validation_request_from_result(blueprint, result)
    workflow_report = await client.validate(request)
    replay_report = await client.validate(request)
    compiler_oracle_match = (
        production_spec == twin.parameterized_spec
        and compiled.compiler_version == twin.compiler_version
        and compiled.source_sha256 == twin.source_sha256
    )
    oracle_match = compiler_oracle_match and _service_result_matches_report(
        result,
        workflow_report,
    )
    return _build_workflow_positive_receipt(
        run_id=run_id,
        manifest_sha256=manifest_sha256,
        case_id=twin.fixture_id,
        fixture_sha256=twin.fixture_sha256,
        surface=EvaluationSurface(twin.engine),
        runtime_manifest_sha256=runtime_manifest_sha256,
        request=request,
        result=result,
        workflow_report=workflow_report,
        replay_report=replay_report,
        oracle_match=oracle_match,
        compiled_source_sha256=compiled.source_sha256,
    )


def _validation_request_from_result(
    blueprint: AssessmentComputationBlueprint,
    result: ComputationResult,
) -> ComputationValidationRequest:
    blueprint_hash = canonical_blueprint_hash(blueprint)
    if result.blueprint_hash != blueprint_hash:
        raise ValueError("workflow compute response does not match its blueprint")
    payload: dict[str, Any] = {"blueprint": blueprint.model_dump(mode="json")}
    if blueprint.operation.value == "solve":
        payload["candidate_solutions"] = [
            expression.model_dump(mode="json")
            for expression in result.solution_expressions
        ]
    elif blueprint.operation.value != "equivalent":
        if result.answer_expression is None:
            raise ValueError("workflow compute response has no typed answer")
        payload["candidate_expression"] = result.answer_expression.model_dump(
            mode="json"
        )
        if blueprint.profile.family.value == "unit":
            payload["candidate_unit"] = blueprint.target_unit
    return ComputationValidationRequest.model_validate(payload)


def _computation_fixture_oracle_match(
    case: ComputationFixture,
    result: ComputationResult,
    report: AssessmentValidationReport,
) -> bool:
    if case.expected_solutions:
        expected_match = result.solutions == case.expected_solutions
    elif case.expected_exact in {"true", "false"}:
        expected_match = result.equivalent is (case.expected_exact == "true")
    else:
        expected_match = (
            result.exact_value or result.canonical_expression
        ) == case.expected_exact
    return expected_match and _service_result_matches_report(result, report)


def _service_result_matches_report(
    result: ComputationResult,
    report: AssessmentValidationReport,
) -> bool:
    return bool(
        report.status
        in {
            ValidationStatus.VALIDATED,
            ValidationStatus.PARTIALLY_VALIDATED,
        }
        and report.result == result
        and all(check.status.value != "failed" for check in report.checks)
    )


def _engine_workflow_oracle_match(
    twin: EngineTwinFixture,
    lineage: ParameterizedLineage,
    receipt: WorkflowPositiveReceipt,
) -> bool:
    expected_blueprint = _lineage_blueprint_for_engine(lineage, twin.engine)
    if receipt.validation_request.blueprint != expected_blueprint:
        return False
    try:
        production_spec = parameterized_spec_from_blueprint(
            expected_blueprint,
            receipt.compute_result,
        )
        answer_expression, constraints = parameterized_typed_inputs(
            expected_blueprint,
            receipt.compute_result,
        )
        compiled = compile_typed_parameterized_item(
            production_spec,
            answer_expression=answer_expression,
            constraints=constraints,
            validation_seeds=25,
            validation_seed_values=deterministic_seeds(expected_blueprint),
        )
    except (ParameterizedCompileError, ValueError):
        return False
    return bool(
        production_spec == twin.parameterized_spec
        and compiled.compiler_version == twin.compiler_version
        and compiled.source_sha256 == twin.source_sha256
        and receipt.compiled_source_sha256 == compiled.source_sha256
        and _service_result_matches_report(
            receipt.compute_result,
            receipt.workflow_report,
        )
    )


def _build_workflow_positive_receipt(
    *,
    run_id: str,
    manifest_sha256: str,
    case_id: str,
    fixture_sha256: str,
    surface: EvaluationSurface,
    runtime_manifest_sha256: str,
    request: ComputationValidationRequest,
    result: ComputationResult,
    workflow_report: AssessmentValidationReport,
    replay_report: AssessmentValidationReport,
    oracle_match: bool,
    compiled_source_sha256: str | None,
) -> WorkflowPositiveReceipt:
    request_payload = request.model_dump(mode="json")
    result_payload = result.model_dump(mode="json")
    workflow_payload = workflow_report.model_dump(mode="json")
    replay_payload = replay_report.model_dump(mode="json")
    workflow_report_sha256 = _sha256_json(workflow_payload)
    replay_report_sha256 = _sha256_json(replay_payload)
    content: dict[str, Any] = {
        "schema_version": "assessment-computation-workflow-positive-receipt-v0",
        "run_id": run_id,
        "manifest_sha256": manifest_sha256,
        "case_id": case_id,
        "fixture_sha256": fixture_sha256,
        "surface": surface,
        "runner_version": WORKFLOW_POSITIVE_RUNNER_VERSION,
        "transport": "unix_socket",
        "computation_runtime_manifest_sha256": runtime_manifest_sha256,
        "validation_request": request_payload,
        "validation_request_sha256": _sha256_json(request_payload),
        "compute_result": result_payload,
        "compute_result_sha256": _sha256_json(result_payload),
        "workflow_report": workflow_payload,
        "replay_report": replay_payload,
        "state": workflow_report.status.value,
        "oracle_match": oracle_match,
        "deterministic_replay": workflow_report == replay_report,
        "workflow_report_sha256": workflow_report_sha256,
        "replay_report_sha256": replay_report_sha256,
        "workflow_capture_sha256": _workflow_capture_sha256(
            case_id,
            capture_index=1,
            report_sha256=workflow_report_sha256,
        ),
        "replay_capture_sha256": _workflow_capture_sha256(
            case_id,
            capture_index=2,
            report_sha256=replay_report_sha256,
        ),
        "compiled_source_sha256": compiled_source_sha256,
        "service_response_count": 3,
    }
    content["receipt_sha256"] = _sha256_json(content)
    return WorkflowPositiveReceipt.model_validate(content)


def _workflow_capture_sha256(
    case_id: str,
    *,
    capture_index: Literal[1, 2],
    report_sha256: str,
) -> str:
    return _sha256_json(
        {
            "case_id": case_id,
            "capture_index": capture_index,
            "report_sha256": report_sha256,
        }
    )


def _write_workflow_positive_ledger(
    output: Path,
    receipts: Sequence[WorkflowPositiveReceipt],
) -> None:
    _validate_workflow_output_target(output)
    raw = b"".join(
        (
            json.dumps(
                receipt.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for receipt in receipts
    )
    if len(raw) > MAX_EVIDENCE_LEDGER_BYTES:
        raise ValueError("workflow receipt ledger exceeds 64 MiB")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(output, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        output.unlink(missing_ok=True)
        raise


def _validate_workflow_output_target(output: Path) -> None:
    if output.is_symlink() or output.exists():
        raise ValueError("workflow receipt output must not already exist")
    if not output.parent.is_dir():
        raise ValueError("workflow receipt output parent must exist")
