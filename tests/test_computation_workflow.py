from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.computation_policy as computation_policy
import app.parameterized as parameterized_module
from app.computation import (
    AssessmentComputationBlueprint,
    AssessmentValidationReport,
    ComparisonConstraint,
    ComputationProfile,
    ComputationResult,
    ComputationValidationRequest,
    ExpressionNode,
    ValidationCheck,
    ValidationStatus,
    VariableSpec,
    compute_blueprint,
    deterministic_seeds,
    validate_computation,
)
from app.computation_client import (
    IN_PROCESS_RUNTIME_MANIFEST_SHA256,
    ComputationTimeoutError,
    InProcessAssessmentComputationClient,
)
from app.computation_policy import QualifiedComputationRuntime
from app.computation_workflow import (
    ComputationWorkflowError,
    _bind_runtime_evidence,
    bind_draft,
    build_computation_artifacts,
    legacy_unsupported_validation_write,
    parameterized_spec_from_blueprint,
    parameterized_typed_inputs,
    preflight_computation,
    revalidate_edited_draft,
)
from app.db import DraftRepository, DraftWrite, draft_content_sha256, init_database
from app.parameterized import (
    ALGEBRA_QUALIFICATION_COMPILER_VERSION,
    FORMULA_COMPILER_VERSION,
    TYPED_COMPUTATION_COMPILER_VERSION,
    ParameterizedCompileError,
    QualifiedFormulaAdapterPromotion,
    compile_parameterized_item,
    compile_typed_algebra_qualification_item,
    compile_typed_parameterized_item,
    formula_adapter_promotion_identity,
    formula_adapter_promotion_sha256,
    formula_adapter_registry_sha256,
    qualified_formula_adapter,
)
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    COMPUTATION_RESULT_SLOT,
    COMPUTATION_TASK_SLOT,
    Concept,
    ComputationQuestionDraft,
    Critique,
    Difficulty,
    GenerateRequest,
    ItemResponse,
    ItemContextType,
    NormalizedPage,
    Paragraph,
    ParameterVariable,
    ParameterizedItemSpec,
    QuestionDraft,
    SourceInfo,
    SourceType,
)


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


class _TimeoutValidationClient(InProcessAssessmentComputationClient):
    async def validate(
        self,
        _request: ComputationValidationRequest,
    ) -> AssessmentValidationReport:
        raise ComputationTimeoutError("do not persist private timeout details")


class _UnsupportedValidationClient(InProcessAssessmentComputationClient):
    async def validate(
        self,
        request: ComputationValidationRequest,
    ) -> AssessmentValidationReport:
        report = await super().validate(request)
        return report.model_copy(
            update={
                "status": ValidationStatus.UNSUPPORTED,
                "result": None,
                "checks": [
                    ValidationCheck(
                        code="supported_profile",
                        status="inconclusive",
                        message="The runtime no longer qualifies this operation.",
                    )
                ],
            },
            deep=True,
        )


def _symbol(name: str) -> ExpressionNode:
    return ExpressionNode(kind="symbol", symbol=name)


def _binary(kind: str, left: ExpressionNode, right: ExpressionNode) -> ExpressionNode:
    return ExpressionNode(kind=kind, args=[left, right])


def _range(name: str = "a") -> VariableSpec:
    return VariableSpec(
        name=name,
        domain="integer",
        minimum=_integer(1),
        maximum=_integer(3),
        step=_integer(1),
    )


def _page() -> NormalizedPage:
    text = "A measured quantity can be evaluated with the stated values."
    return NormalizedPage(
        title="Computation",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            canonical_url=(
                "https://dev.libretexts.org/Sandboxes/johnnyphung/Demo/Computation"
            ),
            path="/Sandboxes/johnnyphung/Demo/Computation/",
            page_id="computation-workflow-1",
        ),
    )


def _draft(
    item_type: AssessmentItemType,
    *,
    stem: str = "Compute the requested value.",
    numeric_answer: float | None = None,
    parameterized: ParameterizedItemSpec | None = None,
) -> QuestionDraft:
    return QuestionDraft(
        item_type=item_type,
        concept_label="Computed quantity",
        stem=stem,
        response=ItemResponse(
            numeric_answer=numeric_answer,
            parameterized=parameterized,
        ),
        explanation="Provider supplied an intentionally incorrect explanation.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _provider_parameterized(engine: str) -> ParameterizedItemSpec:
    return ParameterizedItemSpec(
        engine=engine,
        variables=[
            ParameterVariable(name="z", minimum=1, maximum=3, step=1),
        ],
        prompt_template="Provider template uses {z}.",
        answer_expression="z + 99",
        explanation_template="Provider explanation uses {z}.",
    )


def _numeric_blueprint(
    *,
    delivery: str = "numerical",
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery=delivery),
        operation="evaluate",
        expression=_binary("add", _integer(2), _integer(3)),
    )


def _external_numeric_blueprint(
    *,
    delivery: str = "webwork",
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery=delivery),
        operation="evaluate",
        expression=_binary("add", _symbol("a"), _integer(1)),
        variables=[_range()],
    )


def _formula_blueprint(
    *,
    delivery: str = "webwork",
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery=delivery),
        operation="equivalent",
        expression=_binary(
            "pow",
            _binary("add", _symbol("x"), _integer(1)),
            _integer(2),
        ),
        comparison_expression=_binary(
            "add",
            _binary(
                "add",
                _binary("pow", _symbol("x"), _integer(2)),
                _binary("mul", _integer(2), _symbol("x")),
            ),
            _integer(1),
        ),
        variables=[VariableSpec(name="x")],
    )


def _qualified_formula_promotion(
    engine: str,
    *,
    compiler_versions: frozenset[str] = frozenset({TYPED_COMPUTATION_COMPILER_VERSION}),
) -> QualifiedFormulaAdapterPromotion:
    return QualifiedFormulaAdapterPromotion(
        engine=engine,
        production_compiler_versions=compiler_versions,
        families=frozenset({"algebraic"}),
        operations=frozenset({"substitute", "expand", "factor", "equivalent"}),
        qualification_compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        qualification_manifest_sha256="1" * 64,
        qualification_report_sha256="2" * 64,
        engine_image_digest=f"sha256:{'4' * 64}",
        adapter_image_digest=(f"sha256:{'5' * 64}" if engine == "imathas" else None),
        native_grader=(
            "native_symbolic_equivalence_v0"
            if engine == "imathas"
            else "MathObjects::Formula::cmp"
        ),
        promotion_approval_sha256="3" * 64,
    )


def test_formula_promotion_identity_rejects_replacement_under_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ("webwork", TYPED_COMPUTATION_COMPILER_VERSION)
    original = _qualified_formula_promotion("webwork")
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {key: original},
    )
    identity = formula_adapter_promotion_identity(
        original,
        compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        family="algebraic",
        operation="equivalent",
    )
    original_sha256 = formula_adapter_promotion_sha256(
        original,
        compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        family="algebraic",
        operation="equivalent",
    )
    original_registry_sha256 = formula_adapter_registry_sha256()

    replacement = replace(original, qualification_report_sha256="9" * 64)
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {key: replacement},
    )

    assert identity["qualification_report_sha256"] == "2" * 64
    assert (
        qualified_formula_adapter(
            "webwork",
            TYPED_COMPUTATION_COMPILER_VERSION,
            family="algebraic",
            operation="equivalent",
        )
        is replacement
    )
    with pytest.raises(
        ParameterizedCompileError,
        match="not the exact current qualified entry",
    ):
        formula_adapter_promotion_identity(
            original,
            compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
            family="algebraic",
            operation="equivalent",
        )
    assert (
        formula_adapter_promotion_sha256(
            replacement,
            compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
            family="algebraic",
            operation="equivalent",
        )
        != original_sha256
    )
    assert formula_adapter_registry_sha256() != original_registry_sha256


def test_generate_request_profile_is_nullable_and_core_models_forbid_extras() -> None:
    request = GenerateRequest(
        source_type=SourceType.SANDBOX,
        source_locator="/Sandboxes/johnnyphung/Demo/Computation",
    )
    selected = GenerateRequest.model_validate(
        {
            "source_type": "sandbox",
            "source_locator": "/Sandboxes/johnnyphung/Demo/Computation",
            "computation_profile": {
                "family": "numeric",
                "delivery": "numerical",
            },
        }
    )

    assert request.computation_profile is None
    assert selected.computation_profile == ComputationProfile(
        family="numeric",
        delivery="numerical",
    )
    with pytest.raises(ValidationError, match="extra"):
        ExpressionNode.model_validate(
            {
                "kind": "integer",
                "integer": 1,
                "url": "https://example.invalid/escape",
            }
        )
    payload = _numeric_blueprint().model_dump(mode="json")
    payload["python"] = "__import__('os').system('id')"
    with pytest.raises(ValidationError, match="extra"):
        AssessmentComputationBlueprint.model_validate(payload)


def test_unit_binding_overwrites_magnitude_and_appends_fixed_target_unit() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="unit", delivery="numerical"),
        operation="convert_unit",
        expression=_integer(150),
        source_unit="cm",
        target_unit="m",
    )
    result = compute_blueprint(blueprint)
    provider = _draft(
        AssessmentItemType.NUMERICAL,
        numeric_answer=999,
        stem="Convert the stated length.",
    )

    bound, compiled = bind_draft(blueprint, result, provider)

    assert compiled is None
    assert bound.response.numeric_answer == pytest.approx(1.5)
    assert bound.stem == (
        r"Convert the stated length. Convert \(150\) from \(\mathrm{cm}\) "
        r"to \(\mathrm{m}\). Enter the numerical magnitude in \(\mathrm{m}\)."
    )
    assert bound.explanation.endswith(r"Computed result: \(\frac{3}{2}\,\mathrm{m}\).")
    assert "Provider supplied" in bound.explanation


def test_presentation_binding_failure_produces_persistable_failed_report() -> None:
    blueprint = _numeric_blueprint()
    result = compute_blueprint(blueprint)
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=result.answer_expression,
        )
    )
    bound, _compiled = bind_draft(
        blueprint,
        result,
        _draft(AssessmentItemType.NUMERICAL, numeric_answer=999),
    )
    drifted = bound.model_copy(update={"stem": "Unbound provider text."}, deep=True)

    failed = _bind_runtime_evidence(
        report,
        blueprint=blueprint,
        draft=drifted,
        container_digest=f"sha256:{'9' * 64}",
        image_reference="unavailable",
        runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
    )

    assert failed.status == ValidationStatus.VALIDATION_FAILED
    assert failed.result is None
    assert any(
        check.code == "presentation_binding" and check.status.value == "failed"
        for check in failed.checks
    )


def test_computation_provider_contract_requires_typed_prose_slots() -> None:
    payload = QuestionDraft(
        concept_label="Computed quantity",
        stem="Use the source-grounded relationship.",
        choices=[
            Choice(id="A", text="placeholder", correct=True),
            Choice(id="B", text="placeholder", correct=False),
            Choice(id="C", text="placeholder", correct=False),
            Choice(id="D", text="placeholder", correct=False),
        ],
        explanation="Apply the governing relationship.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    ).model_dump(mode="json")

    with pytest.raises(ValidationError, match="computed_task"):
        ComputationQuestionDraft.model_validate(payload)
    payload["stem"] = f"Use the source relationship. {COMPUTATION_TASK_SLOT}"
    with pytest.raises(ValidationError, match="computed_result"):
        ComputationQuestionDraft.model_validate(payload)


@pytest.mark.asyncio
async def test_provider_cannot_put_answer_bearing_content_outside_slot() -> None:
    with pytest.raises(ComputationWorkflowError, match="server-owned"):
        await build_computation_artifacts(
            client=InProcessAssessmentComputationClient(),
            blueprint=_numeric_blueprint(),
            draft=_draft(
                AssessmentItemType.NUMERICAL,
                numeric_answer=18,
                stem=f"What is 9 + 9? {COMPUTATION_TASK_SLOT}",
            ),
            container_digest=f"sha256:{'9' * 64}",
        )


@pytest.mark.asyncio
async def test_unit_artifact_stays_partial_until_ucum_subset_is_promoted() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="unit", delivery="numerical"),
        operation="convert_unit",
        expression=_integer(150),
        source_unit="cm",
        target_unit="m",
    )

    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=_draft(
            AssessmentItemType.NUMERICAL,
            numeric_answer=999,
        ),
        container_digest=f"sha256:{'9' * 64}",
    )

    assert artifacts.report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert any(
        check.code == "ucum_subset_qualification"
        and check.status.value == "inconclusive"
        for check in artifacts.report.checks
    )


@pytest.mark.asyncio
async def test_unit_artifact_can_validate_only_for_exact_promoted_ucum_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = f"sha256:{'8' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            digest: QualifiedComputationRuntime(
                image_reference=image_reference,
                container_digest=digest,
                runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
                qualification_report_sha256="7" * 64,
                families=frozenset({"unit"}),
                ucum_qualification_report_sha256="6" * 64,
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

    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=_draft(AssessmentItemType.NUMERICAL, numeric_answer=999),
        container_digest=digest,
        image_reference=image_reference,
    )

    assert artifacts.report.status == ValidationStatus.VALIDATED
    assert any(
        check.code == "ucum_subset_qualification" and check.status.value == "passed"
        for check in artifacts.report.checks
    )

    mismatched = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=_draft(AssessmentItemType.NUMERICAL, numeric_answer=999),
        container_digest=digest,
        image_reference=f"other.example/assessment-computation@{digest}",
    )
    assert mismatched.report.status == ValidationStatus.VALIDATION_FAILED
    assert any(
        check.code == "runtime_identity" and check.status.value == "failed"
        for check in mismatched.report.checks
    )


@pytest.mark.asyncio
async def test_numeric_only_runtime_leaves_algebraic_preflight_and_draft_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = f"sha256:{'4' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            digest: QualifiedComputationRuntime(
                image_reference=image_reference,
                container_digest=digest,
                runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
                qualification_report_sha256="5" * 64,
                families=frozenset({"numeric"}),
            )
        },
    )
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery="numerical"),
        operation="substitute",
        expression=_binary("pow", _symbol("x"), _integer(2)),
        variables=[VariableSpec(name="x")],
        substitutions={"x": _integer(3)},
    )
    client = InProcessAssessmentComputationClient()

    preflight = await preflight_computation(
        client=client,
        blueprint=blueprint,
        container_digest=digest,
        image_reference=image_reference,
    )
    artifacts = await build_computation_artifacts(
        client=client,
        blueprint=blueprint,
        draft=_draft(AssessmentItemType.NUMERICAL, numeric_answer=999),
        container_digest=digest,
        image_reference=image_reference,
        frozen_result=preflight.result,
    )

    for report in (preflight.report, artifacts.report):
        checks = {check.code: check.status.value for check in report.checks}
        assert report.status == ValidationStatus.PARTIALLY_VALIDATED
        assert checks["runtime_identity"] == "passed"
        assert checks["runtime_family_qualification"] == "inconclusive"


@pytest.mark.asyncio
async def test_runtime_identity_failure_wins_over_unqualified_formula_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = f"sha256:{'6' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            digest: QualifiedComputationRuntime(
                image_reference=image_reference,
                container_digest=digest,
                runtime_manifest_sha256="7" * 64,
                qualification_report_sha256="8" * 64,
                families=frozenset({"algebraic"}),
            )
        },
    )
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {},
    )

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=_formula_blueprint(),
        container_digest=digest,
        image_reference=image_reference,
    )

    checks = {check.code: check.status.value for check in preflight.report.checks}
    assert preflight.report.status == ValidationStatus.VALIDATION_FAILED
    assert preflight.report.result is None
    assert checks["runtime_identity"] == "failed"


@pytest.mark.asyncio
async def test_exact_rational_uses_audited_native_float_serialization() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="numerical"),
        operation="evaluate",
        expression=ExpressionNode(
            kind="div",
            args=[_integer(1), _integer(3)],
        ),
    )

    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=_draft(
            AssessmentItemType.NUMERICAL,
            numeric_answer=999,
        ),
        container_digest=f"sha256:{'9' * 64}",
    )

    assert artifacts.draft.response.numeric_answer == 1 / 3
    assert artifacts.draft.response.numeric_tolerance == 0
    assert artifacts.report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert len({check.code for check in artifacts.report.checks}) == len(
        artifacts.report.checks
    )
    assert any(
        check.code == "native_numeric_serialization" and check.status == "passed"
        for check in artifacts.report.checks
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["numerical", "multiple_choice", "webwork"])
async def test_binding_rejects_answer_bearing_provider_context(
    delivery: str,
) -> None:
    if delivery == "multiple_choice":
        blueprint = AssessmentComputationBlueprint(
            profile=ComputationProfile(
                family="numeric",
                delivery="multiple_choice",
            ),
            operation="evaluate",
            expression=_binary("add", _integer(2), _integer(3)),
            choice_expressions=[
                _integer(4),
                _integer(5),
                _integer(6),
                _integer(7),
            ],
        )
        provider = QuestionDraft(
            concept_label="Poisoned context",
            stem="Provider stem.",
            choices=[
                Choice(id="A", text="999", correct=True),
                Choice(id="B", text="998", correct=False),
                Choice(id="C", text="997", correct=False),
                Choice(id="D", text="996", correct=False),
            ],
            explanation="Provider explanation.",
            bloom=BloomLevel.APPLY,
            difficulty=Difficulty.EASY,
            citation_paragraphs=[0],
        )
    elif delivery == "webwork":
        blueprint = _external_numeric_blueprint()
        provider = _draft(
            AssessmentItemType.WEBWORK,
            parameterized=_provider_parameterized("webwork"),
        )
    else:
        blueprint = _numeric_blueprint()
        provider = _draft(AssessmentItemType.NUMERICAL, numeric_answer=999)
    poisoned = provider.model_copy(
        update={
            "context_type": ItemContextType.SCENARIO,
            "stimulus": "Use the false given 2 + 3 = 999 and enter 999.",
            "set_key": "poisoned-set",
            "targeted_misconception": "The correct answer is 999.",
        },
        deep=True,
    )

    with pytest.raises(ComputationWorkflowError, match="stimulus"):
        await build_computation_artifacts(
            client=InProcessAssessmentComputationClient(),
            blueprint=blueprint,
            draft=poisoned,
            container_digest=f"sha256:{'9' * 64}",
        )


@pytest.mark.asyncio
async def test_binding_preserves_qualitative_provider_context_and_pedagogy() -> None:
    blueprint = _numeric_blueprint()
    provider = _draft(
        AssessmentItemType.NUMERICAL,
        numeric_answer=999,
        stem=f"Use the source-grounded relationship. {COMPUTATION_TASK_SLOT}",
    ).model_copy(
        update={
            "context_type": ItemContextType.SCENARIO,
            "stimulus": "A learner observes a change described in the source.",
            "set_key": "source-grounded-scenario",
            "explanation": (
                "Apply the governing relationship before simplifying. "
                f"{COMPUTATION_RESULT_SLOT}"
            ),
            "targeted_misconception": (
                "Learners may select an operation based only on surface wording."
            ),
        },
        deep=True,
    )

    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=provider,
        container_digest=f"sha256:{'9' * 64}",
    )

    assert artifacts.draft.context_type == ItemContextType.SCENARIO
    assert artifacts.draft.stimulus == provider.stimulus
    assert artifacts.draft.set_key == provider.set_key
    assert artifacts.draft.targeted_misconception == provider.targeted_misconception
    assert artifacts.draft.stem.startswith("Use the source-grounded relationship.")
    assert artifacts.draft.explanation.startswith("Apply the governing relationship")
    assert artifacts.draft.response.numeric_answer == 5
    assert artifacts.report.status == ValidationStatus.PARTIALLY_VALIDATED


def test_rebind_preserves_reviewed_pedagogy_and_rejects_answer_drift() -> None:
    blueprint = _numeric_blueprint()
    result = compute_blueprint(blueprint)
    provider = _draft(
        AssessmentItemType.NUMERICAL,
        numeric_answer=999,
        stem=f"Apply the relationship from the source. {COMPUTATION_TASK_SLOT}",
    ).model_copy(
        update={
            "explanation": (
                f"First identify the governing relationship. {COMPUTATION_RESULT_SLOT}"
            ),
            "targeted_misconception": (
                "Learners may use an unrelated operation from prior experience."
            ),
        },
        deep=True,
    )
    bound, _compiled = bind_draft(blueprint, result, provider)
    reviewed = bound.model_copy(
        update={
            "stem": bound.stem.replace(
                "Apply the relationship from the source.",
                "Use the source principle before carrying out the task.",
            ),
            "explanation": bound.explanation.replace(
                "First identify the governing relationship.",
                "Connect the source principle to the requested operation.",
            ),
            "response": ItemResponse(numeric_answer=-999, numeric_tolerance=99),
        },
        deep=True,
    )

    rebound, _compiled = bind_draft(blueprint, result, reviewed)

    assert rebound.stem.startswith("Use the source principle")
    assert rebound.explanation.startswith("Connect the source principle")
    assert rebound.targeted_misconception == provider.targeted_misconception
    assert rebound.response.numeric_answer == 5
    assert rebound.response.numeric_tolerance == 0

    drifted = rebound.model_copy(
        update={
            "stem": rebound.stem.replace(
                r"\left(2 + 3\right)",
                r"\left(2 + 4\right)",
            )
        },
        deep=True,
    )
    with pytest.raises(ComputationWorkflowError, match="server-owned"):
        bind_draft(blueprint, result, drifted)


def test_multiple_choice_feedback_prose_survives_server_correctness_binding() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="multiple_choice"),
        operation="evaluate",
        expression=_binary("add", _integer(2), _integer(3)),
        choice_expressions=[_integer(4), _integer(5), _integer(6), _integer(7)],
    )
    provider = QuestionDraft(
        concept_label="Computed quantity",
        stem=f"Use the source relationship. {COMPUTATION_TASK_SLOT}",
        choices=[
            Choice(
                id=identifier,
                text="provider placeholder",
                correct=index == 0,
                feedback="This response reflects a distinct misconception.",
            )
            for index, identifier in enumerate(("A", "B", "C", "D"))
        ],
        explanation=f"Apply the relationship. {COMPUTATION_RESULT_SLOT}",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )

    bound, _compiled = bind_draft(blueprint, compute_blueprint(blueprint), provider)

    assert [choice.text for choice in bound.choices] == ["4", "5", "6", "7"]
    assert [choice.correct for choice in bound.choices] == [
        False,
        True,
        False,
        False,
    ]
    assert all(
        "distinct misconception" in (choice.feedback or "") for choice in bound.choices
    )
    assert "matches the deterministic computed result" in (
        bound.choices[1].feedback or ""
    )


@pytest.mark.parametrize(
    ("blueprint", "expected_stem", "expected_answer"),
    [
        (
            AssessmentComputationBlueprint(
                profile=ComputationProfile(
                    family="numeric",
                    delivery="numerical",
                ),
                operation="evaluate",
                expression=_binary("add", _symbol("x"), _integer(1)),
                variables=[VariableSpec(name="x")],
                substitutions={"x": _integer(2)},
            ),
            r"Given \(\mathrm{x} = 2\). "
            r"Evaluate \(\left(\mathrm{x} + 1\right)\).",
            3,
        ),
        (
            AssessmentComputationBlueprint(
                profile=ComputationProfile(
                    family="algebraic",
                    delivery="numerical",
                ),
                operation="solve",
                expression=_binary("pow", _symbol("x"), _integer(2)),
                equation_rhs=_integer(1),
                solve_for="x",
                variables=[VariableSpec(name="x", assumptions=["positive"])],
            ),
            r"Given \(\mathrm{x}\) is positive. "
            r"Solve \[\left(\mathrm{x}\right)^{2} = 1\] "
            r"for \(\mathrm{x}\).",
            1,
        ),
    ],
)
def test_computed_stem_states_answer_affecting_givens(
    blueprint: AssessmentComputationBlueprint,
    expected_stem: str,
    expected_answer: float,
) -> None:
    bound, _compiled = bind_draft(
        blueprint,
        compute_blueprint(blueprint),
        _draft(
            AssessmentItemType.NUMERICAL,
            numeric_answer=999,
            stem=COMPUTATION_TASK_SLOT,
        ),
    )

    assert bound.stem == expected_stem
    assert bound.response.numeric_answer == expected_answer


@pytest.mark.asyncio
async def test_external_ast_compilation_is_partial_without_native_receipt() -> None:
    blueprint = _external_numeric_blueprint()
    provider = _draft(
        AssessmentItemType.WEBWORK,
        parameterized=_provider_parameterized("webwork"),
    )

    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=provider,
        container_digest=f"sha256:{'1' * 64}",
    )

    spec = artifacts.draft.response.parameterized
    assert spec is not None
    assert [variable.name for variable in spec.variables] == ["a"]
    assert spec.answer_expression == "(a + 1)"
    assert "z" not in spec.answer_expression
    assert artifacts.report.status == ValidationStatus.PARTIALLY_VALIDATED
    native = next(
        check for check in artifacts.report.checks if check.code == "native_engine"
    )
    assert native.status.value == "inconclusive"
    evidence = json.loads(artifacts.persistence.engine_evidence_json)
    assert evidence["engine"] == "webwork"
    assert evidence["source_sha256"] == artifacts.engine_validation["source_sha256"]


@pytest.mark.asyncio
async def test_fractional_external_parameter_grid_is_explicitly_unsupported() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary("add", _symbol("a"), _integer(1)),
        variables=[
            VariableSpec(
                name="a",
                domain="real",
                minimum=ExpressionNode(
                    kind="rational",
                    numerator=1,
                    denominator=3,
                ),
                maximum=ExpressionNode(
                    kind="rational",
                    numerator=2,
                    denominator=3,
                ),
                step=ExpressionNode(
                    kind="rational",
                    numerator=1,
                    denominator=6,
                ),
            )
        ],
    )

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
    )

    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED
    adapter = next(
        check for check in preflight.report.checks if check.code == "delivery_adapter"
    )
    assert "exact integer parameter ranges" in adapter.message


@pytest.mark.asyncio
async def test_external_solution_set_delivery_is_explicitly_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _qualified_formula_promotion("webwork")
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {("webwork", TYPED_COMPUTATION_COMPILER_VERSION): promotion},
    )
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery="webwork"),
        operation="solve",
        expression=_binary("pow", _symbol("x"), _integer(2)),
        equation_rhs=_integer(1),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
    )

    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED
    assert any(
        check.code == "delivery_adapter"
        and "solve-set delivery is unsupported" in check.message
        for check in preflight.report.checks
    )


@pytest.mark.asyncio
async def test_external_compiler_grammar_is_exercised_during_preflight() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary(
            "mul",
            _symbol("a"),
            ExpressionNode(kind="constant", constant="pi"),
        ),
        variables=[_range()],
    )

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
    )

    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED
    assert any(
        check.code == "delivery_adapter" and "constant" in check.message
        for check in preflight.report.checks
    )


@pytest.mark.asyncio
async def test_external_modulo_is_unsupported_before_draft_binding() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary("mod", _symbol("a"), _integer(2)),
        variables=[_range()],
    )

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
    )

    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED


def test_large_integer_parameter_grid_is_not_six_digit_rounded() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary("add", _symbol("a"), _integer(1)),
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=_integer(1_234_567),
                maximum=_integer(1_234_569),
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
        validation_seeds=2,
        validation_seed_values=deterministic_seeds(blueprint)[:2],
    )

    assert "$a = random(1234567,1234569,1);" in compiled.source
    assert "1.23457e+06" not in compiled.source


def test_external_constraints_substitute_fixed_typed_variables() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary("add", _symbol("x"), _integer(1)),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=_integer(1),
                maximum=_integer(4),
                step=_integer(1),
            ),
            VariableSpec(name="y"),
        ],
        substitutions={"y": _integer(2)},
        constraints=[
            ComparisonConstraint(
                operator="gt",
                left=_symbol("x"),
                right=_symbol("y"),
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
        validation_seeds=2,
        validation_seed_values=deterministic_seeds(blueprint)[:2],
    )

    assert spec.constraints == ["x > 2"]
    assert "$x > 2" in compiled.source
    assert "$y" not in compiled.source


def test_typed_external_compiler_never_uses_python_expression_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary("add", _symbol("a"), _integer(1)),
        variables=[_range()],
    )
    result = compute_blueprint(blueprint)
    spec = parameterized_spec_from_blueprint(blueprint, result)
    answer_expression, constraints = parameterized_typed_inputs(blueprint, result)

    def reject_parser(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("typed computation compilation reached ast.parse")

    monkeypatch.setattr("app.parameterized.ast.parse", reject_parser)
    compiled = compile_typed_parameterized_item(
        spec,
        answer_expression=answer_expression,
        constraints=constraints,
        validation_seeds=2,
        validation_seed_values=deterministic_seeds(blueprint)[:2],
    )

    assert compiled.compiler_version == "assessment-computation-typed-ast-v0"
    assert len(compiled.previews) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["webwork", "imathas"])
async def test_formula_preflight_is_unsupported_without_promotion(
    engine: str,
) -> None:
    blueprint = _formula_blueprint(delivery=engine)
    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
    )

    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED
    assert any(
        check.code == "delivery_adapter" and check.status.value == "inconclusive"
        for check in preflight.report.checks
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["webwork", "imathas"])
async def test_formula_preflight_and_typed_compiler_use_exact_promoted_adapter(
    engine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _qualified_formula_promotion(engine)
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {(engine, TYPED_COMPUTATION_COMPILER_VERSION): promotion},
    )
    blueprint = _formula_blueprint(delivery=engine)

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
    )

    assert preflight.result is not None
    assert preflight.report.status != ValidationStatus.UNSUPPORTED
    spec = parameterized_spec_from_blueprint(blueprint, preflight.result)
    assert spec.answer_kind == "formula"
    assert spec.response_symbols == ["x"]
    answer_expression, constraints = parameterized_typed_inputs(
        blueprint,
        preflight.result,
    )
    compiled = compile_typed_parameterized_item(
        spec,
        answer_expression=answer_expression,
        constraints=constraints,
        validation_seeds=2,
        validation_seed_values=deterministic_seeds(blueprint)[:2],
    )
    assert compiled.compiler_version == TYPED_COMPUTATION_COMPILER_VERSION
    if engine == "webwork":
        assert "$answer = Formula(" in compiled.source
        assert "x" in compiled.source
        assert "ANS($answer->cmp(" in compiled.source
        assert "string" not in compiled.source.lower()
    else:
        payload = json.loads(compiled.source)
        assert payload["answer_kind"] == "formula"
        assert payload["grader"] == "native_symbolic_equivalence_v0"
        assert payload["response_symbols"] == ["x"]


@pytest.mark.parametrize("engine", ["webwork", "imathas"])
def test_formula_qualification_source_is_byte_identical_to_production_compiler(
    engine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _formula_blueprint(delivery=engine)
    result = compute_blueprint(blueprint)
    qualification = compile_typed_algebra_qualification_item(
        blueprint,
        result,
        engine=engine,
    )

    assert qualification.production_spec is not None
    assert qualification.compiler_version == TYPED_COMPUTATION_COMPILER_VERSION
    with pytest.raises(
        ParameterizedCompileError,
        match="unsupported until the exact native",
    ):
        compile_typed_parameterized_item(
            qualification.production_spec,
            answer_expression=result.answer_expression,
            constraints=(),
            validation_seeds=1,
            validation_seed_values=(1,),
        )
    with pytest.raises(
        ParameterizedCompileError,
        match="unsupported until the exact native",
    ):
        compile_typed_parameterized_item(
            qualification.production_spec,
            answer_expression=result.answer_expression,
            constraints=(),
            validation_seeds=1,
            validation_seed_values=(1,),
            _qualification_token=object(),
        )

    promotion = _qualified_formula_promotion(engine)
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {(engine, TYPED_COMPUTATION_COMPILER_VERSION): promotion},
    )
    assert result.answer_expression is not None
    production = compile_typed_parameterized_item(
        qualification.production_spec,
        answer_expression=result.answer_expression,
        constraints=(),
        validation_seeds=1,
        validation_seed_values=(1,),
    )

    assert qualification.source == production.source
    assert qualification.source_sha256 == production.source_sha256
    assert qualification.compiler_version == production.compiler_version


@pytest.mark.parametrize("engine", ["webwork", "imathas"])
def test_numeric_algebra_qualification_uses_exact_production_typed_compiler(
    engine: str,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery=engine),
        operation="substitute",
        expression=_binary("add", _symbol("x"), _integer(3)),
        variables=[VariableSpec(name="x")],
        substitutions={"x": _integer(2)},
    )
    result = compute_blueprint(blueprint)
    qualification = compile_typed_algebra_qualification_item(
        blueprint,
        result,
        engine=engine,
    )

    assert qualification.answer_kind == "numeric"
    assert qualification.production_spec is not None
    assert result.answer_expression is not None
    production = compile_typed_parameterized_item(
        qualification.production_spec,
        answer_expression=result.answer_expression,
        constraints=(),
        validation_seeds=1,
        validation_seed_values=(1,),
    )

    assert qualification.source == production.source
    assert qualification.source_sha256 == production.source_sha256
    assert qualification.compiler_version == production.compiler_version


@pytest.mark.parametrize("engine", ["webwork", "imathas"])
def test_symbolic_substitution_uses_qualified_production_formula_compiler(
    engine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery=engine),
        operation="substitute",
        expression=_binary(
            "add",
            _binary("mul", _symbol("a"), _symbol("x")),
            _integer(1),
        ),
        variables=[VariableSpec(name="a"), VariableSpec(name="x")],
        substitutions={"a": _integer(2)},
    )
    result = compute_blueprint(blueprint)
    qualification = compile_typed_algebra_qualification_item(
        blueprint,
        result,
        engine=engine,
    )

    assert qualification.answer_kind == "formula"
    assert qualification.response_symbols == ("x",)
    assert qualification.production_spec is not None
    assert qualification.alternate_correct_submission is not None
    assert (
        qualification.alternate_correct_submission != qualification.correct_submission
    )

    promotion = _qualified_formula_promotion(engine)
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {(engine, TYPED_COMPUTATION_COMPILER_VERSION): promotion},
    )
    assert (
        qualified_formula_adapter(
            engine,
            TYPED_COMPUTATION_COMPILER_VERSION,
            family="algebraic",
            operation="substitute",
        )
        is promotion
    )
    assert (
        qualified_formula_adapter(
            engine,
            TYPED_COMPUTATION_COMPILER_VERSION,
            family="algebraic",
            operation="evaluate",
        )
        is None
    )
    production = compile_typed_parameterized_item(
        qualification.production_spec,
        answer_expression=result.answer_expression,
        constraints=(),
        validation_seeds=1,
        validation_seed_values=(1,),
    )
    assert production.source == qualification.source
    assert production.source_sha256 == qualification.source_sha256


@pytest.mark.parametrize("engine", ["webwork", "imathas"])
def test_solve_set_template_remains_qualification_only_and_separate(
    engine: str,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery=engine),
        operation="solve",
        expression=_binary("pow", _symbol("x"), _integer(2)),
        equation_rhs=_integer(1),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )
    result = compute_blueprint(blueprint)
    qualification = compile_typed_algebra_qualification_item(
        blueprint,
        result,
        engine=engine,
    )

    assert qualification.answer_kind == "solution_set"
    assert qualification.production_spec is None
    assert qualification.compiler_version == ALGEBRA_QUALIFICATION_COMPILER_VERSION
    if engine == "webwork":
        assert "$answer = Set(" in qualification.source
    else:
        assert json.loads(qualification.source)["grader"] == "native_solution_set_v0"


@pytest.mark.asyncio
async def test_formula_promotion_is_bound_to_the_exact_production_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _qualified_formula_promotion(
        "webwork",
        compiler_versions=frozenset({"different-compiler-v0"}),
    )
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {("webwork", TYPED_COMPUTATION_COMPILER_VERSION): promotion},
    )

    preflight = await preflight_computation(
        client=InProcessAssessmentComputationClient(),
        blueprint=_formula_blueprint(),
    )

    assert preflight.result is None
    assert preflight.report.status == ValidationStatus.UNSUPPORTED


def test_formula_promotion_cannot_enable_the_string_dsl_lookalike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _qualified_formula_promotion(
        "webwork",
        compiler_versions=frozenset({FORMULA_COMPILER_VERSION}),
    )
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {("webwork", FORMULA_COMPILER_VERSION): promotion},
    )
    spec = _provider_parameterized("webwork").model_copy(
        update={
            "answer_expression": "z * x + 1",
            "answer_kind": "formula",
            "compiler_profile": "assessment_computation_v0",
            "response_symbols": ["x"],
        }
    )

    with pytest.raises(
        ParameterizedCompileError,
        match="unsupported until the exact native",
    ):
        compile_parameterized_item(spec)


def test_imathas_formula_fails_closed_before_any_source_is_qualified() -> None:
    blueprint = _formula_blueprint(delivery="imathas")
    result = ComputationResult(
        blueprint_hash="a" * 64,
        operation="evaluate",
        canonical_expression="a*x + 1",
    )

    with pytest.raises(ComputationWorkflowError, match="imathas formula"):
        parameterized_spec_from_blueprint(blueprint, result)
    with pytest.raises(ParameterizedCompileError, match="IMathAS formula"):
        compile_parameterized_item(
            _provider_parameterized("imathas").model_copy(
                update={"answer_kind": "formula", "response_symbols": ["x"]}
            )
        )


@pytest.mark.asyncio
async def test_edit_revalidation_makes_old_evidence_stale_atomically(
    tmp_path: Path,
) -> None:
    database = init_database(f"sqlite:///{tmp_path / 'workflow.db'}")
    repository = DraftRepository(database)
    blueprint = _numeric_blueprint()
    initial = _draft(AssessmentItemType.NUMERICAL, numeric_answer=999)
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=initial,
        container_digest=f"sha256:{'2' * 64}",
    )
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="workflow-test-v0",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="A bounded numerical calculation.",
                    source_paragraphs=[0],
                ),
                raw=initial,
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    old = repository.get_current_computation_validation(draft_id)
    assert old is not None
    edited_provider = _draft(
        AssessmentItemType.NUMERICAL,
        numeric_answer=-123,
        stem="Compute the edited value.",
    )

    rebound, new_write = await revalidate_edited_draft(
        client=InProcessAssessmentComputationClient(),
        current_record=old,
        draft=edited_provider,
        container_digest=f"sha256:{'2' * 64}",
    )
    repository.edit_draft(
        draft_id,
        rebound,
        editor="faculty@example.edu",
        computation_validation=new_write,
    )

    current = repository.get_current_computation_validation(draft_id)
    historical = repository.get_computation_validation(
        draft_id,
        edit_count=0,
        report_sha256=old.report_sha256,
    )
    assert rebound.response.numeric_answer == pytest.approx(5.0)
    assert current is not None and current.edit_count == 1 and current.is_current
    assert historical is not None and historical.is_current is False
    assert repository.require_draft(draft_id).edit_count == 1
    database.dispose()


@pytest.mark.asyncio
async def test_edit_timeout_appends_failed_evidence_with_safe_frozen_content(
    tmp_path: Path,
) -> None:
    database = init_database(f"sqlite:///{tmp_path / 'workflow-timeout.db'}")
    repository = DraftRepository(database)
    blueprint = _numeric_blueprint()
    initial = _draft(AssessmentItemType.NUMERICAL, numeric_answer=999)
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=initial,
        container_digest=f"sha256:{'4' * 64}",
    )
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="workflow-timeout-test-v0",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="A bounded numerical calculation.",
                    source_paragraphs=[0],
                ),
                raw=initial,
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    before = repository.require_draft(draft_id)
    old = repository.get_current_computation_validation(draft_id)
    assert old is not None
    assert old.status in {"validated", "partially_validated"}
    edited_provider = _draft(
        AssessmentItemType.NUMERICAL,
        numeric_answer=-123,
        stem="Compute the faculty-edited value.",
    )

    rebound, failed_write = await revalidate_edited_draft(
        client=_TimeoutValidationClient(),
        current_record=old,
        draft=edited_provider,
        container_digest=f"sha256:{'4' * 64}",
    )
    repository.edit_draft(
        draft_id,
        rebound,
        editor="faculty@example.edu",
        computation_validation=failed_write,
        expected_edit_count=before.edit_count,
        expected_draft_sha256=draft_content_sha256(before.current_json),
    )

    after = repository.require_draft(draft_id)
    history = repository.list_computation_validations(draft_id)
    current = repository.get_current_computation_validation(draft_id)
    assert after.edit_count == 1
    assert after.current.response.numeric_answer == pytest.approx(5.0)
    assert "faculty-edited value" in after.current.stem
    assert "2 + 3" in after.current.stem
    assert "-123" not in after.current.stem
    assert len(history) == 2
    assert history[0].report_sha256 == old.report_sha256
    assert history[0].is_current is False
    assert current is not None
    assert current.edit_count == 1
    assert current.status == "validation_failed"
    assert current.is_current is True
    report = json.loads(current.report_json)
    assert report.get("result") is None
    assert report["checks"][0] == {
        "code": "computation_timeout",
        "details": {"phase": "edit_preflight"},
        "message": (
            "The isolated computation service timed out; validation failed closed."
        ),
        "status": "failed",
    }
    assert "private timeout details" not in current.report_json
    database.dispose()


@pytest.mark.asyncio
async def test_edit_unsupported_report_rebinds_prior_frozen_result(
    tmp_path: Path,
) -> None:
    database = init_database(f"sqlite:///{tmp_path / 'workflow-unsupported.db'}")
    repository = DraftRepository(database)
    blueprint = _numeric_blueprint()
    initial = _draft(AssessmentItemType.NUMERICAL, numeric_answer=999)
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=initial,
        container_digest=f"sha256:{'6' * 64}",
    )
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="workflow-unsupported-test-v0",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="A bounded numerical calculation.",
                    source_paragraphs=[0],
                ),
                raw=initial,
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    current = repository.get_current_computation_validation(stored.draft_ids[0])
    assert current is not None

    rebound, evidence = await revalidate_edited_draft(
        client=_UnsupportedValidationClient(),
        current_record=current,
        draft=_draft(
            AssessmentItemType.NUMERICAL,
            numeric_answer=-123,
            stem="Compute the faculty-edited value.",
        ),
        container_digest=f"sha256:{'6' * 64}",
    )

    assert rebound.response.numeric_answer == pytest.approx(5.0)
    assert evidence.status == ValidationStatus.UNSUPPORTED.value
    assert json.loads(evidence.report_json).get("result") is None
    database.dispose()


@pytest.mark.asyncio
async def test_external_final_timeout_does_not_emit_engine_pass_evidence() -> None:
    artifacts = await build_computation_artifacts(
        client=_TimeoutValidationClient(),
        blueprint=_external_numeric_blueprint(),
        draft=_draft(
            AssessmentItemType.WEBWORK,
            parameterized=_provider_parameterized("webwork"),
        ),
        container_digest=f"sha256:{'5' * 64}",
    )

    assert artifacts.report.status == ValidationStatus.VALIDATION_FAILED
    assert artifacts.engine_validation is None
    assert json.loads(artifacts.persistence.engine_evidence_json) == {}
    report = json.loads(artifacts.persistence.report_json)
    assert report["checks"][0]["code"] == "computation_timeout"
    assert report["checks"][0]["details"] == {"phase": "final_validation"}


@pytest.mark.asyncio
async def test_legacy_computational_edit_is_explicitly_unsupported() -> None:
    legacy = _draft(AssessmentItemType.NUMERICAL, numeric_answer=3)

    rebound, evidence = await revalidate_edited_draft(
        client=InProcessAssessmentComputationClient(),
        current_record=None,
        draft=legacy,
        container_digest=f"sha256:{'3' * 64}",
    )

    assert rebound == legacy
    assert evidence == legacy_unsupported_validation_write()
    assert evidence.status == ValidationStatus.UNSUPPORTED.value
    assert json.loads(evidence.report_json)["reason"] == "legacy_without_blueprint"
