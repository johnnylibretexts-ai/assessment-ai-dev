from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest
from pydantic import ValidationError

import app.native_engine_runner as runner_module
from app.computation import (
    AssessmentComputationBlueprint,
    ComparisonConstraint,
    ComputationProfile,
    ExpressionNode,
    FormulaAdapterPromotionEvidence,
    VariableSpec,
    canonical_blueprint_hash,
    compute_blueprint,
    deterministic_seeds,
)
from app.native_engine_evidence import (
    NativeEvidenceVerificationError,
    native_verification_plan,
    verify_native_engine_observations,
)
from app.native_engine_runner import (
    NATIVE_RUNNER_RECEIPT_VERSION,
    NativeEngineRunnerReceipt,
    NativeSeedObservation,
    build_native_seed_observation,
    seed_observations_sha256,
    seed_plan_sha256,
)
from app.parameterized import (
    TYPED_COMPUTATION_COMPILER_VERSION,
    typed_formula_submission,
    typed_parameterized_submission_pair,
)


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


def _symbol(name: str) -> ExpressionNode:
    return ExpressionNode(kind="symbol", symbol=name)


def _binary(
    kind: str,
    left: ExpressionNode,
    right: ExpressionNode,
) -> ExpressionNode:
    return ExpressionNode(kind=kind, args=[left, right])


def _numeric_blueprint(
    *,
    constrained: bool = False,
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=_binary("add", _symbol("a"), _integer(1)),
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=_integer(1),
                maximum=_integer(4 if constrained else 3),
                step=_integer(1),
            )
        ],
        constraints=(
            [
                ComparisonConstraint(
                    operator="gt",
                    left=_symbol("a"),
                    right=_integer(2),
                )
            ]
            if constrained
            else []
        ),
    )


def _formula_blueprint() -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery="webwork"),
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


def _symbolic_substitute_blueprint(
    engine: str,
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _formula_promotion(
    blueprint: AssessmentComputationBlueprint,
) -> FormulaAdapterPromotionEvidence:
    engine = blueprint.profile.delivery.value
    payload: dict[str, object] = {
        "engine": engine,
        "compiler_version": TYPED_COMPUTATION_COMPILER_VERSION,
        "family": "algebraic",
        "operation": blueprint.operation.value,
        "qualification_compiler_version": TYPED_COMPUTATION_COMPILER_VERSION,
        "qualification_manifest_sha256": "a" * 64,
        "qualification_report_sha256": "b" * 64,
        "engine_image_digest": f"sha256:{'3' * 64}",
        "adapter_image_digest": (f"sha256:{'d' * 64}" if engine == "imathas" else None),
        "native_grader": (
            "native_symbolic_equivalence_v0"
            if engine == "imathas"
            else "MathObjects::Formula::cmp"
        ),
        "promotion_approval_sha256": "c" * 64,
    }
    payload["identity_sha256"] = runner_module._canonical_sha256(payload)
    return FormulaAdapterPromotionEvidence.model_validate(payload)


def _observation_for_values(
    blueprint: AssessmentComputationBlueprint,
    *,
    seed: int,
    values: dict[str, int],
    wrong_hash: str | None = None,
    alternate_hash: str | None = None,
    alternate_accepted: bool | None = None,
) -> NativeSeedObservation:
    result = compute_blueprint(blueprint)
    plan = native_verification_plan(blueprint, result)
    correct, wrong = typed_parameterized_submission_pair(
        plan.spec,
        plan.answer_expression,
        values,
    )
    if plan.alternate_expression is not None:
        alternate = typed_formula_submission(
            plan.spec,
            plan.alternate_expression,
            values,
        )
        alternate_hash = alternate_hash or _sha256(alternate)
        if alternate_accepted is None:
            alternate_accepted = True
    render_hash = _sha256(
        f"render:{seed}:{sorted(values.items())}:{blueprint.operation.value}"
    )
    return build_native_seed_observation(
        seed=seed,
        observed_variables=values,
        correct_submission_sha256=_sha256(correct),
        alternate_correct_submission_sha256=alternate_hash,
        wrong_submission_sha256=wrong_hash or _sha256(wrong),
        alternate_correct_answer_accepted=alternate_accepted,
        render_sha256=render_hash,
        repeat_render_sha256=render_hash,
    )


def _receipt_payload(
    blueprint: AssessmentComputationBlueprint,
    *,
    values_for_index: Callable[[int], dict[str, int]],
    wrong_hash_for_index: Callable[[int], str | None] | None = None,
    alternate_hash_for_index: Callable[[int], str | None] | None = None,
    alternate_accepted_for_index: Callable[[int], bool | None] | None = None,
) -> dict[str, object]:
    result = compute_blueprint(blueprint)
    plan = native_verification_plan(blueprint, result)
    seeds = list(deterministic_seeds(blueprint))
    observations = [
        _observation_for_values(
            blueprint,
            seed=seed,
            values=values_for_index(index),
            wrong_hash=(
                wrong_hash_for_index(index)
                if wrong_hash_for_index is not None
                else None
            ),
            alternate_hash=(
                alternate_hash_for_index(index)
                if alternate_hash_for_index is not None
                else None
            ),
            alternate_accepted=(
                alternate_accepted_for_index(index)
                if alternate_accepted_for_index is not None
                else None
            ),
        )
        for index, seed in enumerate(seeds)
    ]
    aggregate_render = runner_module._canonical_sha256(
        [item.render_sha256 for item in observations]
    )
    engine = blueprint.profile.delivery.value
    promotion = (
        _formula_promotion(blueprint) if plan.spec.answer_kind == "formula" else None
    )
    payload: dict[str, object] = {
        "schema_version": NATIVE_RUNNER_RECEIPT_VERSION,
        "runner_id": "qualified-runner",
        "runner_version": "runner-v0",
        "runner_manifest_sha256": "1" * 64,
        "runner_image_digest": f"sha256:{'2' * 64}",
        "qualification_report_sha256": "5" * 64,
        "promotion_approval_sha256": "6" * 64,
        "engine": engine,
        "compiler_version": TYPED_COMPUTATION_COMPILER_VERSION,
        "answer_kind": plan.spec.answer_kind,
        "native_grader": (
            (
                "native_symbolic_equivalence_v0"
                if engine == "imathas"
                else "MathObjects::Formula::cmp"
            )
            if plan.spec.answer_kind == "formula"
            else (
                "native_calculated_v0"
                if engine == "imathas"
                else "MathObjects::Real::cmp"
            )
        ),
        "formula_adapter_promotion": (
            promotion.model_dump(mode="json", exclude_none=False)
            if promotion is not None
            else None
        ),
        "source_sha256": "7" * 64,
        "blueprint_sha256": canonical_blueprint_hash(blueprint),
        "draft_sha256": "8" * 64,
        "request_sha256": "9" * 64,
        "seeds": seeds,
        "seed_plan_sha256": seed_plan_sha256(seeds),
        "seed_receipts_sha256": seed_observations_sha256(observations),
        "seeds_validated": 25,
        "observations": [
            item.model_dump(mode="json", exclude_none=False) for item in observations
        ],
        "engine_image_digest": f"sha256:{'3' * 64}",
        "adapter_image_digest": (f"sha256:{'d' * 64}" if engine == "imathas" else None),
        "network_attestation_sha256": "4" * 64,
        "correct_answer_accepted": True,
        "wrong_answer_rejected": True,
        "rendered": True,
        "render_sha256": aggregate_render,
        "repeat_render_sha256": aggregate_render,
        "warnings_count": 0,
        "errors_count": 0,
        "outbound_request_count": 0,
        "passed": True,
    }
    payload["receipt_sha256"] = runner_module._canonical_sha256(payload)
    return payload


def _receipt(
    blueprint: AssessmentComputationBlueprint,
    *,
    values_for_index: Callable[[int], dict[str, int]],
    wrong_hash_for_index: Callable[[int], str | None] | None = None,
    alternate_hash_for_index: Callable[[int], str | None] | None = None,
) -> NativeEngineRunnerReceipt:
    return NativeEngineRunnerReceipt.model_validate(
        _receipt_payload(
            blueprint,
            values_for_index=values_for_index,
            wrong_hash_for_index=wrong_hash_for_index,
            alternate_hash_for_index=alternate_hash_for_index,
        )
    )


def _rehash_observation(payload: dict[str, object]) -> None:
    payload["observation_sha256"] = runner_module._canonical_sha256(
        {key: value for key, value in payload.items() if key != "observation_sha256"}
    )


def _rehash_receipt(payload: dict[str, object]) -> None:
    observations = [
        NativeSeedObservation.model_validate(item)
        for item in payload["observations"]  # type: ignore[union-attr]
    ]
    payload["seed_receipts_sha256"] = seed_observations_sha256(observations)
    payload["receipt_sha256"] = runner_module._canonical_sha256(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )


def test_exact_numeric_observations_match_typed_ground_truth() -> None:
    blueprint = _numeric_blueprint()
    receipt = _receipt(
        blueprint,
        values_for_index=lambda index: {"a": (index % 3) + 1},
    )

    verify_native_engine_observations(
        blueprint=blueprint,
        result=compute_blueprint(blueprint),
        receipt=receipt,
    )


def test_arbitrary_seed_receipt_aggregate_is_rejected() -> None:
    payload = _receipt_payload(
        _numeric_blueprint(),
        values_for_index=lambda index: {"a": (index % 3) + 1},
    )
    payload["seed_receipts_sha256"] = "f" * 64
    payload["receipt_sha256"] = runner_module._canonical_sha256(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )

    with pytest.raises(ValidationError, match="aggregate hash"):
        NativeEngineRunnerReceipt.model_validate(payload)


def test_missing_or_duplicate_seed_observations_are_rejected() -> None:
    blueprint = _numeric_blueprint()
    missing = _receipt_payload(
        blueprint,
        values_for_index=lambda index: {"a": (index % 3) + 1},
    )
    missing["observations"] = missing["observations"][:-1]  # type: ignore[index]
    with pytest.raises(ValidationError):
        NativeEngineRunnerReceipt.model_validate(missing)

    duplicate = _receipt_payload(
        blueprint,
        values_for_index=lambda index: {"a": (index % 3) + 1},
    )
    observations = duplicate["observations"]
    assert isinstance(observations, list)
    second = dict(observations[1])
    first = observations[0]
    assert isinstance(first, dict)
    second["seed"] = first["seed"]
    _rehash_observation(second)
    observations[1] = second
    duplicate["seeds"] = [
        item["seed"] for item in observations if isinstance(item, dict)
    ]
    duplicate["seed_plan_sha256"] = seed_plan_sha256(duplicate["seeds"])
    _rehash_receipt(duplicate)
    with pytest.raises(ValidationError, match="unique deterministic seeds"):
        NativeEngineRunnerReceipt.model_validate(duplicate)


def test_off_grid_engine_observation_fails_independent_verification() -> None:
    blueprint = _numeric_blueprint()
    receipt = _receipt(
        blueprint,
        values_for_index=lambda index: {"a": 99 if index == 0 else 1},
    )

    with pytest.raises(NativeEvidenceVerificationError, match="integer grid"):
        verify_native_engine_observations(
            blueprint=blueprint,
            result=compute_blueprint(blueprint),
            receipt=receipt,
        )


def test_in_grid_constraint_violation_fails_independent_verification() -> None:
    blueprint = _numeric_blueprint(constrained=True)
    receipt = _receipt(
        blueprint,
        values_for_index=lambda index: {"a": 1 if index == 0 else 3},
    )

    with pytest.raises(NativeEvidenceVerificationError, match="typed constraints"):
        verify_native_engine_observations(
            blueprint=blueprint,
            result=compute_blueprint(blueprint),
            receipt=receipt,
        )


def test_wrong_answer_hash_fails_independent_verification() -> None:
    blueprint = _numeric_blueprint()
    receipt = _receipt(
        blueprint,
        values_for_index=lambda index: {"a": (index % 3) + 1},
        wrong_hash_for_index=lambda index: "e" * 64 if index == 0 else None,
    )

    with pytest.raises(NativeEvidenceVerificationError, match="correct/wrong"):
        verify_native_engine_observations(
            blueprint=blueprint,
            result=compute_blueprint(blueprint),
            receipt=receipt,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("warnings_count", 1, "literal_error"),
        ("errors_count", 1, "literal_error"),
        ("outbound_request_count", 1, "literal_error"),
    ],
)
def test_nonzero_execution_counts_are_rejected(
    field: str,
    value: int,
    message: str,
) -> None:
    payload = _receipt_payload(
        _numeric_blueprint(),
        values_for_index=lambda index: {"a": (index % 3) + 1},
    )
    observations = payload["observations"]
    assert isinstance(observations, list)
    first = dict(observations[0])
    first[field] = value
    _rehash_observation(first)
    observations[0] = first

    with pytest.raises(ValidationError, match=message):
        NativeEngineRunnerReceipt.model_validate(payload)


def test_nondeterministic_render_is_rejected() -> None:
    payload = _receipt_payload(
        _numeric_blueprint(),
        values_for_index=lambda index: {"a": (index % 3) + 1},
    )
    observations = payload["observations"]
    assert isinstance(observations, list)
    first = dict(observations[0])
    first["repeat_render_sha256"] = "d" * 64
    _rehash_observation(first)
    observations[0] = first

    with pytest.raises(ValidationError, match="render was not stable"):
        NativeEngineRunnerReceipt.model_validate(payload)


def test_formula_receipt_requires_and_verifies_distinct_equivalent_form() -> None:
    blueprint = _formula_blueprint()
    receipt = _receipt(
        blueprint,
        values_for_index=lambda _index: {},
    )
    first = receipt.observations[0]

    assert first.alternate_correct_submission_sha256 is not None
    assert first.alternate_correct_submission_sha256 != (
        first.correct_submission_sha256
    )
    assert first.alternate_correct_answer_accepted is True
    verify_native_engine_observations(
        blueprint=blueprint,
        result=compute_blueprint(blueprint),
        receipt=receipt,
    )


@pytest.mark.parametrize("engine", ["webwork", "imathas"])
def test_symbolic_substitute_receipt_verifies_distinct_equivalent_form(
    engine: str,
) -> None:
    blueprint = _symbolic_substitute_blueprint(engine)
    result = compute_blueprint(blueprint)
    plan = native_verification_plan(blueprint, result)
    assert plan.spec.answer_kind == "formula"
    assert plan.spec.response_symbols == ["x"]
    assert plan.alternate_expression is not None
    correct = typed_formula_submission(plan.spec, plan.answer_expression, {})
    alternate = typed_formula_submission(plan.spec, plan.alternate_expression, {})
    assert alternate != correct

    receipt = _receipt(
        blueprint,
        values_for_index=lambda _index: {},
    )
    first = receipt.observations[0]
    assert first.alternate_correct_submission_sha256 == _sha256(alternate)
    assert first.alternate_correct_answer_accepted is True
    verify_native_engine_observations(
        blueprint=blueprint,
        result=result,
        receipt=receipt,
    )


@pytest.mark.parametrize("engine", ["webwork", "imathas"])
def test_formula_promotion_evidence_rejects_evaluate_operation(
    engine: str,
) -> None:
    blueprint = _symbolic_substitute_blueprint(engine)
    payload = _formula_promotion(blueprint).model_dump(
        mode="json",
        exclude_none=False,
    )
    payload["operation"] = "evaluate"
    payload["identity_sha256"] = runner_module._canonical_sha256(
        {key: value for key, value in payload.items() if key != "identity_sha256"}
    )

    with pytest.raises(ValidationError):
        FormulaAdapterPromotionEvidence.model_validate(payload)


def test_formula_receipt_cannot_substitute_an_arbitrary_accepted_string() -> None:
    blueprint = _formula_blueprint()
    receipt = _receipt(
        blueprint,
        values_for_index=lambda _index: {},
        alternate_hash_for_index=lambda index: "d" * 64 if index == 0 else None,
    )

    with pytest.raises(
        NativeEvidenceVerificationError,
        match="distinct equivalent form",
    ):
        verify_native_engine_observations(
            blueprint=blueprint,
            result=compute_blueprint(blueprint),
            receipt=receipt,
        )


def test_formula_receipt_rejects_string_comparison_behavior() -> None:
    payload = _receipt_payload(
        _formula_blueprint(),
        values_for_index=lambda _index: {},
        alternate_accepted_for_index=lambda index: False if index == 0 else None,
    )

    with pytest.raises(ValidationError, match="accepted alternate form"):
        NativeEngineRunnerReceipt.model_validate(payload)
