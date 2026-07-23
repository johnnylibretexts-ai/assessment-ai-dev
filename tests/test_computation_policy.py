from __future__ import annotations

import inspect
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

import app.computation_policy as computation_policy
from app.adapt import AdaptCreateResult, FrameworkItem, ResolvedAlignment
from app.catalog import suggested_topic
from app.computation import (
    AssessmentComputationBlueprint,
    CheckStatus,
    ComputationValidationRequest,
    ComputationProfile,
    ExpressionNode,
    ValidationCheck,
    ValidationStatus,
    VariableSpec,
    compute_blueprint,
    deterministic_seeds,
    validate_computation,
)
from app.computation_policy import (
    ComputationPolicyError,
    QualifiedComputationRuntime,
    computation_runtime_promotion_sha256,
    configured_computation_runtime,
    evaluate_computation_gate,
    qualified_computation_runtime,
    require_computation_gate,
)
from app.computation_client import (
    IN_PROCESS_RUNTIME_MANIFEST_SHA256,
    InProcessAssessmentComputationClient,
)
from app.computation_workflow import (
    build_computation_artifacts,
    not_applicable_validation_write,
    parameterized_spec_from_blueprint,
    parameterized_typed_inputs,
    preflight_computation,
    validation_write,
)
from app.config import Settings
from app.db import (
    ComputationAttestationWrite,
    ComputationEvidenceError,
    ComputationValidationRecord,
    ComputationValidationWrite,
    ConcurrentDraftUpdateError,
    DraftRepository,
    DraftWrite,
    EngineValidationRecord,
    PublicationState,
    init_database,
)
from app.pipeline import ReviewService
from app.parameterized import compile_typed_parameterized_item
from app.publishing import PublicationService, PublicationValidationError
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    COMPUTATION_RESULT_SLOT,
    COMPUTATION_TASK_SLOT,
    Concept,
    Critique,
    Difficulty,
    ItemResponse,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
)


ISOTOPES_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
    "02%3A_Atoms_and_the_Periodic_Table/2.03%3A_Isotopes_and_Atomic_Weight"
)
QUALIFIED_TEST_DIGEST = f"sha256:{'1' * 64}"
QUALIFIED_TEST_IMAGE_REFERENCE = (
    f"registry.example/assessment-computation@{QUALIFIED_TEST_DIGEST}"
)


@pytest.fixture(autouse=True)
def qualified_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            QUALIFIED_TEST_DIGEST: QualifiedComputationRuntime(
                image_reference=QUALIFIED_TEST_IMAGE_REFERENCE,
                container_digest=QUALIFIED_TEST_DIGEST,
                runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
                qualification_report_sha256="2" * 64,
                families=frozenset({"numeric", "algebraic", "unit"}),
                ucum_qualification_report_sha256="3" * 64,
            )
        },
    )


def _settings(
    tmp_path: Path,
    *,
    mode: str,
    specialists: str = "",
) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / f'{mode}-policy.db'}",
        allowed_origin="http://testserver",
        computation_mode=mode,
        computation_image_reference=QUALIFIED_TEST_IMAGE_REFERENCE,
        computation_family_allowlist="numeric,algebraic,unit",
        computation_specialist_subject_allowlist=specialists,
        adapt_publishing_enabled=True,
        adapt_password=SecretStr("not-a-live-secret"),
        adapt_folder_id=42,
        qti_storage_dir=tmp_path / f"{mode}-qti",
        ollama_api_key=None,
    )


def test_runtime_promotion_resolver_rejects_every_malformed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = QUALIFIED_TEST_DIGEST
    image_reference = QUALIFIED_TEST_IMAGE_REFERENCE
    valid = QualifiedComputationRuntime(
        image_reference=image_reference,
        container_digest=digest,
        runtime_manifest_sha256="4" * 64,
        qualification_report_sha256="5" * 64,
        families=frozenset({"numeric", "unit"}),
        ucum_qualification_report_sha256="6" * 64,
    )
    malformed = {
        "mutable_image": replace(
            valid,
            image_reference="registry.example/assessment-computation:mutable",
        ),
        "wrong_digest": replace(
            valid,
            container_digest=f"sha256:{'7' * 64}",
        ),
        "uppercase_manifest": replace(
            valid,
            runtime_manifest_sha256="A" * 64,
        ),
        "nonstring_manifest": replace(
            valid,
            runtime_manifest_sha256=123,  # type: ignore[arg-type]
        ),
        "bad_report_hash": replace(
            valid,
            qualification_report_sha256="not-a-sha256",
        ),
        "mutable_families": replace(
            valid,
            families={"numeric", "unit"},  # type: ignore[arg-type]
        ),
        "empty_families": replace(valid, families=frozenset()),
        "unknown_family": replace(valid, families=frozenset({"numeric", "other"})),
        "unit_without_ucum": replace(
            valid,
            ucum_qualification_report_sha256=None,
        ),
        "ucum_without_unit": replace(
            valid,
            families=frozenset({"numeric"}),
        ),
        "duplicate_evidence": replace(
            valid,
            qualification_report_sha256="4" * 64,
        ),
        "wrong_entry_type": {},
    }
    settings = _settings(tmp_path, mode="enforce")
    for name, promotion in malformed.items():
        monkeypatch.setattr(
            computation_policy,
            "QUALIFIED_COMPUTATION_RUNTIMES",
            {digest: promotion},
        )
        assert (
            qualified_computation_runtime(
                image_reference=image_reference,
                container_digest=digest,
            )
            is None
        ), name
        assert configured_computation_runtime(settings) is None, name
        assert (
            computation_runtime_promotion_sha256(
                image_reference=image_reference,
                container_digest=digest,
            )
            is None
        ), name


def _page() -> NormalizedPage:
    text = "An isotope has a whole-number mass number."
    return NormalizedPage(
        title="2.3: Isotopes and Atomic Weight",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=ISOTOPES_URL,
            path=(
                "chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
                "Fundamentals_of_General_Organic_and_Biological_Chemistry_(LibreTexts)/"
                "02:_Atoms_and_the_Periodic_Table/2.03:_Isotopes_and_Atomic_Weight"
            ),
            page_id="86190",
        ),
    )


def _question(
    item_type: AssessmentItemType = AssessmentItemType.MULTIPLE_CHOICE,
    *,
    stem: str = "What is the result of adding two and three?",
) -> QuestionDraft:
    if item_type == AssessmentItemType.NUMERICAL:
        return QuestionDraft(
            item_type=item_type,
            concept_label="Whole-number arithmetic",
            stem=stem,
            response=ItemResponse(numeric_answer=5, numeric_tolerance=0),
            explanation="Adding two and three gives five.",
            bloom=BloomLevel.APPLY,
            difficulty=Difficulty.EASY,
            citation_paragraphs=[0],
        )
    return QuestionDraft(
        concept_label="Whole-number arithmetic",
        stem=stem,
        choices=[
            Choice(id="A", text="5", correct=True),
            Choice(id="B", text="4", correct=False),
            Choice(id="C", text="6", correct=False),
            Choice(id="D", text="7", correct=False),
        ],
        explanation="Adding two and three gives five.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


class _UnsupportedUnitClient(InProcessAssessmentComputationClient):
    async def validate(self, request: ComputationValidationRequest):
        report = await super().validate(request)
        return report.model_copy(
            update={
                "status": ValidationStatus.UNSUPPORTED,
                "result": None,
                "checks": [
                    ValidationCheck(
                        code="supported_profile",
                        status=CheckStatus.INCONCLUSIVE,
                        message="Synthetic unsupported unit profile.",
                    )
                ],
            },
            deep=True,
        )


def _validation(
    status: str,
    *,
    marker: str = "initial",
) -> ComputationValidationWrite:
    blueprint = AssessmentComputationBlueprint(
        profile={"family": "numeric", "delivery": "numerical"},
        operation="evaluate",
        expression=ExpressionNode(
            kind="add",
            args=[_integer(2), _integer(3)],
        ),
    )
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=_integer(5),
        )
    )
    report = report.model_copy(
        update={
            "checks": [
                *report.checks,
                ValidationCheck(
                    code="presentation_binding",
                    status=CheckStatus.PASSED,
                    message="Learner-facing fields were server-bound.",
                ),
            ]
        }
    )
    if status == ValidationStatus.PARTIALLY_VALIDATED.value:
        report = report.model_copy(
            update={
                "status": ValidationStatus.PARTIALLY_VALIDATED,
                "checks": [
                    *report.checks,
                    ValidationCheck(
                        code="secondary",
                        status=CheckStatus.INCONCLUSIVE,
                        message="A secondary check is unavailable.",
                    ),
                ],
            }
        )
    elif status == ValidationStatus.UNSUPPORTED.value:
        report = report.model_copy(
            update={
                "status": ValidationStatus.UNSUPPORTED,
                "result": None,
                "checks": [
                    ValidationCheck(
                        code="supported_profile",
                        status=CheckStatus.INCONCLUSIVE,
                        message="This operation is outside the qualified profile.",
                    )
                ],
            }
        )
    elif status == ValidationStatus.VALIDATION_FAILED.value:
        report = report.model_copy(
            update={
                "status": ValidationStatus.VALIDATION_FAILED,
                "result": None,
                "checks": [
                    ValidationCheck(
                        code="computation",
                        status=CheckStatus.FAILED,
                        message="A material mismatch was detected.",
                    )
                ],
            }
        )
    report = report.model_copy(
        update={
            "checks": [
                *report.checks,
                ValidationCheck(
                    code="runtime_identity",
                    status=CheckStatus.PASSED,
                    message="The qualified test runtime identity is bound.",
                    details={
                        "image_reference": QUALIFIED_TEST_IMAGE_REFERENCE,
                        "container_digest": QUALIFIED_TEST_DIGEST,
                        "runtime_manifest_sha256": (IN_PROCESS_RUNTIME_MANIFEST_SHA256),
                        "qualification_report_sha256": "2" * 64,
                    },
                ),
                ValidationCheck(
                    code="runtime_family_qualification",
                    status=CheckStatus.PASSED,
                    message="The numeric runtime family is qualified.",
                    details={"family": "numeric"},
                ),
            ]
        }
    )
    report = report.model_copy(
        update={"limitations": [*report.limitations, f"test marker: {marker}"]}
    )
    return validation_write(
        blueprint=blueprint,
        report=report,
        container_digest=f"sha256:{'1' * 64}",
        duration_ms=4,
        engine_validation=None,
    )


def _external_validation_bundle(
    *,
    tamper_computation_engine_hash: bool = False,
) -> tuple[QuestionDraft, dict[str, object], ComputationValidationWrite]:
    symbol = ExpressionNode(kind="symbol", symbol="a")
    expression = ExpressionNode(
        kind="add",
        args=[symbol, _integer(1)],
    )
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=expression,
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=_integer(1),
                maximum=_integer(3),
                step=_integer(1),
            )
        ],
    )
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
    engine_validation: dict[str, object] = {
        "engine": compiled.engine,
        "compiler_version": compiled.compiler_version,
        "answer_kind": spec.answer_kind,
        "source_sha256": compiled.source_sha256,
        "seed_count": len(compiled.previews),
        "previews": [
            {
                "seed": preview.seed,
                "variables": preview.variables,
                "prompt": preview.prompt,
                "answer": preview.answer,
                "explanation": preview.explanation,
            }
            for preview in compiled.previews
        ],
    }
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=expression,
        )
    )
    report = report.model_copy(
        update={
            "checks": [
                *report.checks,
                ValidationCheck(
                    code="presentation_binding",
                    status=CheckStatus.PASSED,
                    message="Learner-facing fields were server-bound.",
                ),
                ValidationCheck(
                    code="runtime_identity",
                    status=CheckStatus.PASSED,
                    message="The qualified test runtime identity is bound.",
                    details={
                        "image_reference": QUALIFIED_TEST_IMAGE_REFERENCE,
                        "container_digest": QUALIFIED_TEST_DIGEST,
                        "runtime_manifest_sha256": (IN_PROCESS_RUNTIME_MANIFEST_SHA256),
                        "qualification_report_sha256": "2" * 64,
                    },
                ),
                ValidationCheck(
                    code="runtime_family_qualification",
                    status=CheckStatus.PASSED,
                    message="The numeric runtime family is qualified.",
                    details={"family": "numeric"},
                ),
            ]
        }
    )
    persistence = validation_write(
        blueprint=blueprint,
        report=report,
        container_digest=f"sha256:{'1' * 64}",
        duration_ms=5,
        engine_validation=engine_validation,
    )
    if tamper_computation_engine_hash:
        engine_evidence = dict(engine_validation)
        engine_evidence["source_sha256"] = "0" * 64
        persistence = replace(
            persistence,
            engine_evidence_json=json.dumps(
                engine_evidence,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    question = QuestionDraft(
        item_type=AssessmentItemType.WEBWORK,
        concept_label="Whole-number arithmetic",
        stem="Evaluate the displayed sum.",
        response=ItemResponse(parameterized=spec),
        explanation="Add one to the displayed value.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
        specialist_review_required=True,
    )
    return question, engine_validation, persistence


def _seed_external(
    repository: DraftRepository,
    *,
    tamper_computation_engine_hash: bool = False,
) -> int:
    question, engine_validation, computation_validation = _external_validation_bundle(
        tamper_computation_engine_hash=tamper_computation_engine_hash,
    )
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="computation-policy-webwork",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Whole-number arithmetic",
                    description="Add one to a whole number.",
                    source_paragraphs=[0],
                ),
                raw=question,
                critique=Critique(issues=[], revision_required=False),
                revised=question,
                engine_validation=engine_validation,
                computation_validation=computation_validation,
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


def test_generation_derives_engine_row_from_computation_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="assist")
    database, repository = _repository(settings)
    try:
        question, independent_engine, computation_validation = (
            _external_validation_bundle()
        )
        mismatched_engine = {
            **independent_engine,
            "source_sha256": "0" * 64,
        }
        stored = repository.replace_generated_drafts(
            page=_page(),
            pipeline_version="computation-policy-webwork-mismatched-row",
            drafts=[
                DraftWrite(
                    position=0,
                    concept=Concept(
                        label="Whole-number arithmetic",
                        description="Add one to a whole number.",
                        source_paragraphs=[0],
                    ),
                    raw=question,
                    critique=Critique(issues=[], revision_required=False),
                    revised=question,
                    engine_validation=mismatched_engine,
                    computation_validation=computation_validation,
                )
            ],
            llm_calls=[],
        )

        persisted = repository.require_draft(
            stored.draft_ids[0]
        ).current_engine_validation
        computation_engine = json.loads(computation_validation.engine_evidence_json)
        assert persisted is not None
        assert persisted.source_sha256 == computation_engine["source_sha256"]
        assert persisted.source_sha256 != mismatched_engine["source_sha256"]
        assert persisted.compiler_version == computation_engine["compiler_version"]
        assert persisted.previews_json == computation_engine["previews"]
    finally:
        database.dispose()


def _seed(
    repository: DraftRepository,
    *,
    item_type: AssessmentItemType = AssessmentItemType.MULTIPLE_CHOICE,
    status: str | None = None,
) -> int:
    question = _question(item_type)
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version=f"computation-policy-{item_type.value}-{status}",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Whole-number arithmetic",
                    description="Add two whole numbers.",
                    source_paragraphs=[0],
                ),
                raw=question,
                critique=Critique(issues=[], revision_required=False),
                revised=question,
                computation_validation=_validation(status) if status else None,
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


def _repository(settings: Settings):
    database = init_database(settings.database_url)
    return database, DraftRepository(database)


@pytest.mark.parametrize("mode", ["off", "assist"])
def test_non_enforce_modes_do_not_read_evidence(
    tmp_path: Path,
    mode: str,
) -> None:
    settings = _settings(tmp_path, mode=mode)

    class EvidenceTrap:
        def get_current_computation_validation(self, _draft_id: int):
            raise AssertionError("non-enforce mode must not read evidence")

    draft = type(
        "DraftStub",
        (),
        {
            "id": 7,
            "current": type(
                "QuestionStub",
                (),
                {"item_type": AssessmentItemType.NUMERICAL},
            )(),
        },
    )()
    decision = evaluate_computation_gate(settings, EvidenceTrap(), draft)  # type: ignore[arg-type]

    assert decision.allowed is True
    assert decision.scoped is False
    assert decision.publication_evidence is None


def test_enforce_blocks_legacy_and_stale_computational_drafts(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        legacy_id = _seed(repository, item_type=AssessmentItemType.NUMERICAL)
        legacy = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(legacy_id),
        )
        assert legacy.allowed is False
        assert legacy.reason_code == "legacy_without_blueprint"
        assert legacy.validation_status == "unsupported"

        current_id = _seed(repository, status="validated")
        repository.edit_draft(
            current_id,
            _question(stem="What is three added to two?"),
            editor="reviewer@example.org",
        )
        stale = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(current_id),
        )
        assert stale.allowed is False
        assert stale.reason_code == "stale_evidence"
    finally:
        database.dispose()


def test_enforce_blocks_unpromoted_computation_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        monkeypatch.setattr(
            computation_policy,
            "QUALIFIED_COMPUTATION_RUNTIMES",
            {},
        )

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )

        assert decision.allowed is False
        assert decision.reason_code == "runtime_not_qualified"
    finally:
        database.dispose()


def test_enforce_requires_the_exact_qualified_repository_reference(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce").model_copy(
        update={
            "computation_image_reference": (
                f"other.example/assessment-computation@{QUALIFIED_TEST_DIGEST}"
            )
        }
    )
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )

        assert decision.allowed is False
        assert decision.reason_code == "runtime_not_qualified"
    finally:
        database.dispose()


def test_enforce_blocks_report_from_a_different_sidecar_build_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        current = computation_policy.QUALIFIED_COMPUTATION_RUNTIMES[
            QUALIFIED_TEST_DIGEST
        ]
        monkeypatch.setattr(
            computation_policy,
            "QUALIFIED_COMPUTATION_RUNTIMES",
            {
                QUALIFIED_TEST_DIGEST: replace(
                    current,
                    runtime_manifest_sha256="9" * 64,
                )
            },
        )

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )

        assert decision.allowed is False
        assert decision.reason_code == "runtime_identity_mismatch"
    finally:
        database.dispose()


def test_enforce_blocks_unpromoted_runtime_even_with_valid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {},
    )
    try:
        draft_id = _seed(
            repository,
            item_type=AssessmentItemType.NUMERICAL,
            status=ValidationStatus.VALIDATED.value,
        )

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )

        assert decision.allowed is False
        assert decision.reason_code == "runtime_not_qualified"
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_enforce_rejects_replaced_ucum_report_under_same_runtime_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="unit", delivery="numerical"),
        operation="convert_unit",
        expression=_integer(150),
        source_unit="cm",
        target_unit="m",
    )
    provider = QuestionDraft(
        item_type=AssessmentItemType.NUMERICAL,
        concept_label="Unit conversion",
        stem=f"Convert the stated quantity. {COMPUTATION_TASK_SLOT}",
        response=ItemResponse(numeric_answer=999, numeric_tolerance=0),
        explanation=f"Apply the conversion. {COMPUTATION_RESULT_SLOT}",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=provider,
        container_digest=QUALIFIED_TEST_DIGEST,
        image_reference=QUALIFIED_TEST_IMAGE_REFERENCE,
    )
    assert artifacts.report.status == ValidationStatus.VALIDATED
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="computation-policy-ucum-identity",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Unit conversion",
                    description="Convert centimetres to metres.",
                    source_paragraphs=[0],
                ),
                raw=provider,
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    allowed = require_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
        action="approval",
    )
    assert allowed.runtime_promotion_sha256 is not None

    current = computation_policy.QUALIFIED_COMPUTATION_RUNTIMES[QUALIFIED_TEST_DIGEST]
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            QUALIFIED_TEST_DIGEST: replace(
                current,
                ucum_qualification_report_sha256="9" * 64,
            )
        },
    )

    decision = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
    )
    assert decision.allowed is False
    assert decision.reason_code == "ucum_profile_identity_mismatch"
    with pytest.raises(
        ConcurrentDraftUpdateError,
        match="runtime promotion changed",
    ):
        ReviewService(repository, settings).decide(
            draft_id,
            ReviewDecision(
                status=ReviewStatus.READY_TO_PUBLISH,
                bloom_confirmed=True,
                difficulty_confirmed=True,
                reviewer_notes="Replaced UCUM evidence must not approve.",
            ),
            reviewer="reviewer@example.edu",
            computation_binding=allowed.atomic_binding(settings),
        )
    assert repository.require_draft(draft_id).status == ReviewStatus.READY_FOR_REVIEW
    database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery", "item_type"),
    [
        ("numerical", AssessmentItemType.NUMERICAL),
        ("multiple_choice", AssessmentItemType.MULTIPLE_CHOICE),
    ],
)
async def test_unsupported_unit_report_with_exact_ucum_allows_attestation_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
    item_type: AssessmentItemType,
) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="unit", delivery=delivery),
        operation="convert_unit",
        expression=_integer(150),
        source_unit="cm",
        target_unit="m",
        choice_expressions=(
            [_integer(1), _integer(2), _integer(3), _integer(4)]
            if delivery == "multiple_choice"
            else []
        ),
    )
    preflight = await preflight_computation(
        client=_UnsupportedUnitClient(),
        blueprint=blueprint,
        container_digest=QUALIFIED_TEST_DIGEST,
        image_reference=QUALIFIED_TEST_IMAGE_REFERENCE,
    )
    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED
    assert {check.code: check.status.value for check in preflight.report.checks}[
        "ucum_subset_qualification"
    ] == "passed"
    provider = _question(item_type, stem="Review this unit conversion.")
    persistence = validation_write(
        blueprint=blueprint,
        report=preflight.report,
        container_digest=QUALIFIED_TEST_DIGEST,
        duration_ms=preflight.duration_ms,
        engine_validation=None,
    )
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version=f"unsupported-unit-{delivery}",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Unit conversion",
                    description="Convert centimetres to metres.",
                    source_paragraphs=[0],
                ),
                raw=provider,
                critique=Critique(issues=[], revision_required=False),
                revised=provider,
                computation_validation=persistence,
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    current = repository.get_current_computation_validation(draft_id)
    assert current is not None
    blocked = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
    )
    assert blocked.reason_code == "specialist_attestation_required"

    repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=current.report_sha256,
        attestation=ComputationAttestationWrite(
            specialist_identity="trusted-specialist",
            rationale="The unsupported conversion was independently checked.",
        ),
    )
    assert require_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
        action="approval",
    ).allowed

    current_runtime = computation_policy.QUALIFIED_COMPUTATION_RUNTIMES[
        QUALIFIED_TEST_DIGEST
    ]
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            QUALIFIED_TEST_DIGEST: replace(
                current_runtime,
                ucum_qualification_report_sha256="9" * 64,
            )
        },
    )
    replaced = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
    )
    assert replaced.allowed is False
    assert replaced.reason_code == "ucum_profile_identity_mismatch"

    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {},
    )
    revoked = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
    )
    assert revoked.allowed is False
    assert revoked.reason_code == "runtime_not_qualified"
    database.dispose()


@pytest.mark.asyncio
async def test_malformed_runtime_cannot_pass_runtime_or_ucum_or_authorize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    current_runtime = computation_policy.QUALIFIED_COMPUTATION_RUNTIMES[
        QUALIFIED_TEST_DIGEST
    ]
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            QUALIFIED_TEST_DIGEST: replace(
                current_runtime,
                families={"unit"},  # type: ignore[arg-type]
            )
        },
    )
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="unit", delivery="numerical"),
        operation="convert_unit",
        expression=_integer(150),
        source_unit="cm",
        target_unit="m",
    )
    provider = QuestionDraft(
        item_type=AssessmentItemType.NUMERICAL,
        concept_label="Unit conversion",
        stem=f"Convert the stated quantity. {COMPUTATION_TASK_SLOT}",
        response=ItemResponse(numeric_answer=999, numeric_tolerance=0),
        explanation=f"Apply the conversion. {COMPUTATION_RESULT_SLOT}",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=provider,
        container_digest=QUALIFIED_TEST_DIGEST,
        image_reference=QUALIFIED_TEST_IMAGE_REFERENCE,
    )
    checks = {check.code: check.status.value for check in artifacts.report.checks}
    assert artifacts.report.status == ValidationStatus.VALIDATION_FAILED
    assert checks["runtime_identity"] == "failed"
    assert checks.get("ucum_subset_qualification") != "passed"
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="malformed-runtime-unit",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Unit conversion",
                    description="Convert centimetres to metres.",
                    source_paragraphs=[0],
                ),
                raw=provider,
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    decision = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(stored.draft_ids[0]),
    )
    assert decision.allowed is False
    assert decision.reason_code == "validation_failed"
    database.dispose()


def test_enforce_blocks_family_removed_from_current_allowlist(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce").model_copy(
        update={"computation_family_allowlist": "unit"}
    )
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status=ValidationStatus.VALIDATED.value)

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )

        assert decision.allowed is False
        assert decision.reason_code == "family_not_enabled"
    finally:
        database.dispose()


def test_not_applicable_cannot_downgrade_typed_computation_history(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(
            repository,
            item_type=AssessmentItemType.MULTIPLE_CHOICE,
            status=ValidationStatus.VALIDATED.value,
        )
        repository.append_computation_validation(
            draft_id,
            edit_count=0,
            record=not_applicable_validation_write(),
        )

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )

        assert decision.allowed is False
        assert decision.scoped is True
        assert decision.reason_code == "invalid_computation_scope"
    finally:
        database.dispose()


def test_atomic_approval_rejects_a_newer_report_after_gate_decision(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        draft = repository.require_draft(draft_id)
        decision = require_computation_gate(
            settings,
            repository,
            draft,
            action="approval",
        )
        repository.append_computation_validation(
            draft_id,
            edit_count=draft.edit_count,
            record=_validation("validated", marker="newer-report"),
        )

        with pytest.raises(
            ConcurrentDraftUpdateError,
            match="evidence changed",
        ):
            ReviewService(repository).decide(
                draft_id,
                ReviewDecision(
                    status=ReviewStatus.READY_TO_PUBLISH,
                    bloom_confirmed=True,
                    difficulty_confirmed=True,
                    reviewer_notes="This stale decision must not commit.",
                ),
                reviewer="reviewer@example.org",
                computation_binding=decision.atomic_binding(settings),
            )
        stored = repository.require_draft(draft_id)
        assert stored.status == ReviewStatus.READY_FOR_REVIEW
        assert stored.bloom_confirmed is False
        assert stored.difficulty_confirmed is False
    finally:
        database.dispose()


def test_enforce_review_service_rejects_ready_without_atomic_binding(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        with pytest.raises(
            ComputationEvidenceError,
            match="atomic computation gate binding",
        ):
            ReviewService(repository, settings).decide(
                draft_id,
                ReviewDecision(
                    status=ReviewStatus.READY_TO_PUBLISH,
                    bloom_confirmed=True,
                    difficulty_confirmed=True,
                    reviewer_notes="Missing atomic evidence.",
                ),
                reviewer="reviewer@example.org",
            )
    finally:
        database.dispose()


def test_atomic_approval_rejects_refreshed_envelope_with_same_report(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        draft = repository.require_draft(draft_id)
        decision = require_computation_gate(
            settings,
            repository,
            draft,
            action="approval",
        )
        original = _validation("validated")
        repository.append_computation_validation(
            draft_id,
            edit_count=draft.edit_count,
            record=replace(
                original,
                container_digest=f"sha256:{'2' * 64}",
            ),
        )

        with pytest.raises(
            ConcurrentDraftUpdateError,
            match="evidence changed",
        ):
            ReviewService(repository).decide(
                draft_id,
                ReviewDecision(
                    status=ReviewStatus.READY_TO_PUBLISH,
                    bloom_confirmed=True,
                    difficulty_confirmed=True,
                    reviewer_notes="This stale envelope must not authorize approval.",
                ),
                reviewer="reviewer@example.org",
                computation_binding=decision.atomic_binding(settings),
            )
    finally:
        database.dispose()


def test_atomic_approval_recomputes_the_persisted_evidence_envelope(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        draft = repository.require_draft(draft_id)
        decision = require_computation_gate(
            settings,
            repository,
            draft,
            action="approval",
        )
        with database.session_factory.begin() as session:
            record = session.get(
                ComputationValidationRecord,
                decision.validation_record_id,
            )
            assert record is not None
            record.container_digest = f"sha256:{'9' * 64}"

        with pytest.raises(
            ComputationEvidenceError,
            match="immutable validation envelope",
        ):
            ReviewService(repository).decide(
                draft_id,
                ReviewDecision(
                    status=ReviewStatus.READY_TO_PUBLISH,
                    bloom_confirmed=True,
                    difficulty_confirmed=True,
                    reviewer_notes="Tampered evidence must fail closed.",
                ),
                reviewer="reviewer@example.org",
                computation_binding=decision.atomic_binding(settings),
            )
        assert (
            repository.require_draft(draft_id).status == ReviewStatus.READY_FOR_REVIEW
        )
    finally:
        database.dispose()


def test_atomic_approval_locks_draft_before_evidence_verification() -> None:
    source = inspect.getsource(DraftRepository.apply_review_decision)
    lock_position = source.index(".with_for_update()")
    verify_position = source.index("_verify_computation_gate_binding(")
    assert lock_position < verify_position


def test_off_mode_reconstructs_existing_typed_external_source_without_string_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, mode="off")
    database, repository = _repository(settings)
    try:
        draft_id = _seed_external(repository)
        draft = repository.require_draft(draft_id)
        decision = evaluate_computation_gate(settings, repository, draft)

        def reject_parser(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("typed external source reached ast.parse")

        monkeypatch.setattr("app.parameterized.ast.parse", reject_parser)
        compiled, binding = PublicationService(
            settings,
            repository,
            _FakeAdapt(),
        )._verified_external_compilation(
            draft,
            computation_decision=decision,
        )

        assert compiled is not None
        assert compiled.compiler_version == "assessment-computation-typed-ast-v0"
        assert binding is None
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "assist"])
async def test_non_enforce_typed_publication_rejects_current_engine_row_mismatch(
    tmp_path: Path,
    mode: str,
) -> None:
    settings = _settings(tmp_path, mode=mode)
    database, repository = _repository(settings)
    fake = _FakeAdapt()
    try:
        draft_id = _seed_external(repository)
        _question, _engine, drifted_computation = _external_validation_bundle(
            tamper_computation_engine_hash=True,
        )
        repository.append_computation_validation(
            draft_id,
            edit_count=0,
            record=drifted_computation,
        )
        current_engine = repository.require_draft(draft_id).current_engine_validation
        assert current_engine is not None
        assert current_engine.source_sha256 == "0" * 64
        _approve(repository, draft_id)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None

        with pytest.raises(
            PublicationValidationError,
            match="freshly compiled engine artifact",
        ):
            await PublicationService(settings, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=topic.stable_id,
                alignment_confirmed=True,
            )

        # Non-enforce mode keeps the accepted BUILD-08 destination-lookup
        # ordering, but it must fail before any create request or reservation.
        assert fake.resolve_calls == 1
        assert fake.create_calls == 0
        assert repository.require_draft(draft_id).publications == []
    finally:
        database.dispose()


def test_gate_rejects_tampered_status_column_even_with_valid_report(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validated")
        with database.session() as session:
            record = session.query(ComputationValidationRecord).one()
            record.status = "partially_validated"
            session.commit()

        decision = evaluate_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
        )
        assert decision.allowed is False
        assert decision.reason_code == "invalid_computation_evidence"
    finally:
        database.dispose()


@pytest.mark.parametrize("status", ["partially_validated", "unsupported"])
def test_attestable_status_requires_current_trusted_specialist(
    tmp_path: Path,
    status: str,
) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status=status)
        draft = repository.require_draft(draft_id)
        blocked = evaluate_computation_gate(settings, repository, draft)
        assert blocked.reason_code == "specialist_attestation_required"
        assert blocked.allowed is False

        current = repository.get_current_computation_validation(draft_id)
        assert current is not None
        repository.append_computation_attestation(
            draft_id,
            edit_count=draft.edit_count,
            report_sha256=current.report_sha256,
            attestation=ComputationAttestationWrite(
                specialist_identity="untrusted-specialist",
                rationale="Independent calculation was checked against the source.",
            ),
        )
        assert evaluate_computation_gate(settings, repository, draft).allowed is False

        trusted = repository.append_computation_attestation(
            draft_id,
            edit_count=draft.edit_count,
            report_sha256=current.report_sha256,
            attestation=ComputationAttestationWrite(
                specialist_identity="trusted-specialist",
                rationale="Independent calculation was checked against the source.",
            ),
        )
        allowed = require_computation_gate(
            settings,
            repository,
            draft,
            action="approval",
        )
        assert allowed.allowed is True
        assert allowed.reason_code == "specialist_attested"
        assert allowed.attestation_sha256s == (trusted.attestation_sha256,)
    finally:
        database.dispose()


def test_validation_failed_has_no_attestation_override(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    try:
        draft_id = _seed(repository, status="validation_failed")
        draft = repository.require_draft(draft_id)
        current = repository.get_current_computation_validation(draft_id)
        assert current is not None
        repository.append_computation_attestation(
            draft_id,
            edit_count=draft.edit_count,
            report_sha256=current.report_sha256,
            attestation=ComputationAttestationWrite(
                specialist_identity="trusted-specialist",
                rationale="The mismatch was reviewed, but it remains a mismatch.",
            ),
        )
        with pytest.raises(ComputationPolicyError, match="cannot override"):
            require_computation_gate(
                settings,
                repository,
                draft,
                action="approval",
            )
    finally:
        database.dispose()


class _FakeAdapt:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.create_calls = 0

    async def resolve_destination(self, **_kwargs: object) -> ResolvedAlignment:
        self.resolve_calls += 1
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        return ResolvedAlignment(
            framework_id=7,
            framework_title="Chemistry",
            chapter=FrameworkItem(id=20, text=topic.chapter_title),
            topic=FrameworkItem(id=23, text=topic.title),
            chapter_stable_id=topic.chapter_stable_id,
            topic_stable_id=topic.stable_id,
        )

    async def create_question(self, _payload: dict[str, object]) -> AdaptCreateResult:
        self.create_calls += 1
        return AdaptCreateResult(question_id=501, page_id=501)

    async def find_question_by_tag(self, _tag: str) -> AdaptCreateResult | None:
        return None

    async def sync_hint_rungs(
        self,
        _question_id: int,
        _payload: dict[str, object],
    ) -> None:
        return None


def _approve(
    repository: DraftRepository,
    draft_id: int,
    *,
    settings: Settings | None = None,
) -> None:
    computation_binding = None
    if settings is not None and settings.computation_mode == "enforce":
        decision = require_computation_gate(
            settings,
            repository,
            repository.require_draft(draft_id),
            action="approval",
        )
        computation_binding = decision.atomic_binding(settings)
    ReviewService(repository).decide(
        draft_id,
        ReviewDecision(
            status=ReviewStatus.READY_TO_PUBLISH,
            bloom_confirmed=True,
            difficulty_confirmed=True,
            reviewer_notes="Source and labels checked.",
        ),
        reviewer="reviewer@example.org",
        computation_binding=computation_binding,
    )


@pytest.mark.asyncio
async def test_publication_blocks_before_external_calls_when_evidence_missing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    fake = _FakeAdapt()
    try:
        draft_id = _seed(repository, item_type=AssessmentItemType.NUMERICAL)
        # Simulate a pre-v0 approval already present before enforce mode.
        _approve(repository, draft_id)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        with pytest.raises(PublicationValidationError, match="typed blueprint"):
            await PublicationService(settings, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=topic.stable_id,
                alignment_confirmed=True,
            )
        assert fake.resolve_calls == 0
        assert fake.create_calls == 0
        assert repository.require_draft(draft_id).publications == []
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_enforce_publication_freezes_exact_validation_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    fake = _FakeAdapt()
    try:
        draft_id = _seed(
            repository,
            item_type=AssessmentItemType.NUMERICAL,
            status="validated",
        )
        _approve(repository, draft_id, settings=settings)
        current = repository.get_current_computation_validation(draft_id)
        assert current is not None
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        publication = await PublicationService(settings, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert publication.state == PublicationState.SUCCEEDED.value
        assert fake.create_calls == 1
        evidence = repository.get_publication_computation_evidence(publication.id)
        assert evidence is not None
        assert evidence.report_sha256 == current.report_sha256
        assert evidence.attestation_sha256s == ()
        assert current.report_sha256 in evidence.snapshot_json
        assert publication.qti_path is not None
        with zipfile.ZipFile(publication.qti_path) as archive:
            item_path = next(
                name for name in archive.namelist() if name.startswith("items/")
            )
            item_xml = archive.read(item_path).decode("utf-8")
        assert current.report_sha256 in item_xml
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_external_publication_freezes_exact_engine_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    fake = _FakeAdapt()
    try:
        draft_id = _seed_external(repository)
        draft = repository.require_draft(draft_id)
        report = repository.get_current_computation_validation(draft_id)
        engine = draft.current_engine_validation
        assert report is not None
        assert engine is not None
        attestation = repository.append_computation_attestation(
            draft_id,
            edit_count=draft.edit_count,
            report_sha256=report.report_sha256,
            attestation=ComputationAttestationWrite(
                specialist_identity="trusted-specialist",
                rationale="The pending native-engine receipt was independently reviewed.",
            ),
        )
        _approve(repository, draft_id, settings=settings)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        service = PublicationService(settings, repository, fake)
        captured: dict[str, object] = {}
        original = service._key_material

        def capture_key_material(*args: object, **kwargs: object) -> dict[str, object]:
            material = original(*args, **kwargs)  # type: ignore[arg-type]
            captured.update(material)
            return material

        monkeypatch.setattr(service, "_key_material", capture_key_material)

        publication = await service.publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        frozen = repository.get_publication_computation_evidence(publication.id)
        assert frozen is not None
        snapshot = json.loads(frozen.snapshot_json)
        assert frozen.attestation_sha256s == (attestation.attestation_sha256,)
        assert captured["engine_validation"] == {
            "engine": engine.engine,
            "compiler_version": engine.compiler_version,
            "source_sha256": engine.source_sha256,
        }
        assert snapshot["engine_validation"] == {
            "validation_record_id": engine.id,
            "draft_id": draft.id,
            "edit_count": draft.edit_count,
            "draft_sha256": report.draft_sha256,
            "engine": engine.engine,
            "compiler_version": engine.compiler_version,
            "source_sha256": engine.source_sha256,
            "seed_count": engine.seed_count,
        }
        assert fake.resolve_calls == 1
        assert fake.create_calls == 1
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_computation_engine_hash_mismatch_blocks_before_external_calls(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    fake = _FakeAdapt()
    try:
        draft_id = _seed_external(
            repository,
            tamper_computation_engine_hash=True,
        )
        draft = repository.require_draft(draft_id)
        report = repository.get_current_computation_validation(draft_id)
        assert report is not None
        repository.append_computation_attestation(
            draft_id,
            edit_count=draft.edit_count,
            report_sha256=report.report_sha256,
            attestation=ComputationAttestationWrite(
                specialist_identity="trusted-specialist",
                rationale="The pending native-engine receipt was independently reviewed.",
            ),
        )
        _approve(repository, draft_id, settings=settings)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None

        with pytest.raises(
            PublicationValidationError,
            match="engine evidence no longer matches",
        ):
            await PublicationService(settings, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=topic.stable_id,
                alignment_confirmed=True,
            )

        assert fake.resolve_calls == 0
        assert fake.create_calls == 0
        assert repository.require_draft(draft_id).publications == []
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_enforce_publication_binds_trusted_attestation_hash(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    fake = _FakeAdapt()
    try:
        draft_id = _seed(
            repository,
            item_type=AssessmentItemType.NUMERICAL,
            status="partially_validated",
        )
        draft = repository.require_draft(draft_id)
        current = repository.get_current_computation_validation(draft_id)
        assert current is not None
        attestation = repository.append_computation_attestation(
            draft_id,
            edit_count=draft.edit_count,
            report_sha256=current.report_sha256,
            attestation=ComputationAttestationWrite(
                specialist_identity="trusted-specialist",
                rationale="The unavailable secondary check was independently reviewed.",
            ),
        )
        _approve(repository, draft_id, settings=settings)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        publication = await PublicationService(settings, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )
        repeated = await PublicationService(settings, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        evidence = repository.get_publication_computation_evidence(publication.id)
        assert evidence is not None
        assert repeated.id == publication.id
        assert fake.create_calls == 1
        assert evidence.attestation_sha256s == (attestation.attestation_sha256,)
        assert attestation.attestation_sha256 in evidence.snapshot_json
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_publication_reservation_rejects_report_race_before_create(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, mode="enforce")
    database, repository = _repository(settings)
    draft_id = _seed(
        repository,
        item_type=AssessmentItemType.NUMERICAL,
        status="validated",
    )
    _approve(repository, draft_id, settings=settings)

    class RacingAdapt(_FakeAdapt):
        async def resolve_destination(self, **kwargs: object) -> ResolvedAlignment:
            draft = repository.require_draft(draft_id)
            repository.append_computation_validation(
                draft_id,
                edit_count=draft.edit_count,
                record=_validation("validated", marker="racing-report"),
            )
            return await super().resolve_destination(**kwargs)

    fake = RacingAdapt()
    try:
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        with pytest.raises(
            PublicationValidationError,
            match="changed before publication reservation",
        ):
            await PublicationService(settings, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=topic.stable_id,
                alignment_confirmed=True,
            )
        assert fake.resolve_calls == 1
        assert fake.create_calls == 0
        assert repository.require_draft(draft_id).publications == []
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_enforce_publication_rejects_engine_receipt_race_before_create(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        mode="enforce",
        specialists="trusted-specialist",
    )
    database, repository = _repository(settings)
    draft_id = _seed_external(repository)
    draft = repository.require_draft(draft_id)
    report = repository.get_current_computation_validation(draft_id)
    original = draft.current_engine_validation
    assert report is not None
    assert original is not None
    repository.append_computation_attestation(
        draft_id,
        edit_count=draft.edit_count,
        report_sha256=report.report_sha256,
        attestation=ComputationAttestationWrite(
            specialist_identity="trusted-specialist",
            rationale="The native-engine evidence was independently reviewed.",
        ),
    )
    _approve(repository, draft_id, settings=settings)

    class RacingAdapt(_FakeAdapt):
        async def resolve_destination(self, **kwargs: object) -> ResolvedAlignment:
            with database.session_factory.begin() as session:
                session.add(
                    EngineValidationRecord(
                        draft_id=original.draft_id,
                        edit_count=original.edit_count,
                        engine=original.engine,
                        compiler_version=f"{original.compiler_version}-raced",
                        source_sha256="2" * 64,
                        seed_count=original.seed_count,
                        previews_json=original.previews_json,
                        status="passed",
                    )
                )
            return await super().resolve_destination(**kwargs)

    fake = RacingAdapt()
    try:
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        with pytest.raises(
            PublicationValidationError,
            match="changed before publication reservation",
        ):
            await PublicationService(settings, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=topic.stable_id,
                alignment_confirmed=True,
            )
        assert fake.resolve_calls == 1
        assert fake.create_calls == 0
        assert repository.require_draft(draft_id).publications == []
    finally:
        database.dispose()
