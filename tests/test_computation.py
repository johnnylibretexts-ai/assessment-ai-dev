from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

import app.computation as computation
from app.computation import (
    AssessmentComputationBlueprint,
    CheckStatus,
    ComputationChoice,
    ComputationDependencyError,
    ComputationProfile,
    ComputationValidationRequest,
    ExpressionNode,
    NativeEngineEvidence,
    ValidationStatus,
    VariableSpec,
    assert_computation_dependencies,
    canonical_blueprint_hash,
    compute_blueprint,
    deterministic_seeds,
    validate_computation,
    validate_unit_code,
)


def integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


def rational(numerator: int, denominator: int) -> ExpressionNode:
    return ExpressionNode(kind="rational", numerator=numerator, denominator=denominator)


def decimal(value: str) -> ExpressionNode:
    return ExpressionNode(kind="decimal", decimal=value)


def symbol(name: str) -> ExpressionNode:
    return ExpressionNode(kind="symbol", symbol=name)


def operation(kind: str, *args: ExpressionNode) -> ExpressionNode:
    return ExpressionNode(kind=kind, args=list(args))


def profile(family: str, delivery: str = "numerical") -> ComputationProfile:
    return ComputationProfile(family=family, delivery=delivery)


def numeric_blueprint(
    expression: ExpressionNode,
    *,
    delivery: str = "numerical",
    variables: list[VariableSpec] | None = None,
    substitutions: dict[str, ExpressionNode] | None = None,
    tolerance: dict[str, str] | None = None,
    choice_expressions: list[ExpressionNode] | None = None,
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=profile("numeric", delivery),
        operation="evaluate",
        expression=expression,
        variables=variables or [],
        substitutions=substitutions or {},
        tolerance=tolerance or {"absolute": "0", "relative": "0"},
        choice_expressions=choice_expressions or [],
    )


def native_evidence(
    engine: str = "webwork", *, passed: bool = True
) -> NativeEngineEvidence:
    return NativeEngineEvidence(
        engine=engine,
        source_sha256="a" * 64,
        receipt_sha256="b" * 64,
        seeds_validated=25,
        passed=passed,
    )


def test_minimal_numeric_computation_and_model_dump() -> None:
    blueprint = numeric_blueprint(operation("add", integer(2), rational(3, 2)))
    result = compute_blueprint(blueprint)

    assert result.exact_value == "7/2"
    assert result.numeric_value == "3.5"
    assert result.model_dump(mode="json")["operation"] == "evaluate"


def test_zero_to_zero_power_is_rejected_by_the_typed_schema() -> None:
    with pytest.raises(ValidationError, match="zero exponents"):
        operation("pow", integer(0), integer(0))


def test_variable_dependent_fractional_power_is_unsupported() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("numeric", "webwork"),
        operation="evaluate",
        expression=operation(
            "pow",
            operation(
                "sub",
                operation(
                    "pow",
                    operation("sub", x, integer(100)),
                    integer(2),
                ),
                integer(1),
            ),
            rational(1, 2),
        ),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(0),
                maximum=integer(1000),
                step=integer(1),
            )
        ],
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.UNSUPPORTED
    assert report.result is None
    assert "variable-dependent fractional powers" in report.checks[-1].message


def test_numeric_substitution_is_exact() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("numeric"),
        operation="substitute",
        expression=operation("div", symbol("x"), integer(3)),
        variables=[VariableSpec(name="x")],
        substitutions={"x": integer(2)},
    )

    result = compute_blueprint(blueprint)

    assert result.exact_value == "2/3"
    assert result.numeric_value.startswith("0.666666666666")


def test_zero_tolerance_compares_exact_rational_before_approximation() -> None:
    blueprint = numeric_blueprint(rational(1, 3))

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=rational(1, 3),
        )
    )

    assert report.status == ValidationStatus.VALIDATED


def test_numeric_tolerance_absolute_and_relative() -> None:
    blueprint = numeric_blueprint(
        integer(100),
        tolerance={"absolute": "0.01", "relative": "0.001"},
    )
    accepted = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint, candidate_expression=decimal("100.05")
        )
    )
    rejected = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint, candidate_expression=decimal("100.11")
        )
    )

    assert accepted.status == ValidationStatus.VALIDATED
    assert rejected.status == ValidationStatus.VALIDATION_FAILED


def test_wrong_candidate_report_has_unique_stage_specific_check_codes() -> None:
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=numeric_blueprint(integer(2)),
            candidate_expression=integer(3),
        )
    )

    codes = [check.code for check in report.checks]
    assert report.status == ValidationStatus.VALIDATION_FAILED
    assert len(codes) == len(set(codes))
    assert any(
        check.code == "candidate" and check.status == CheckStatus.FAILED
        for check in report.checks
    )


def test_native_numerical_magnitude_limit_is_unsupported_during_preflight() -> None:
    blueprint = numeric_blueprint(integer(10**15))

    with pytest.raises(
        computation.ComputationUnsupportedError,
        match="native ADAPT magnitude bounds",
    ):
        compute_blueprint(blueprint)

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))
    assert report.status == ValidationStatus.UNSUPPORTED
    assert report.result is None


def test_unbounded_or_answer_scale_tolerance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absolute tolerance"):
        numeric_blueprint(
            integer(5),
            tolerance={"absolute": "1000000", "relative": "0"},
        )
    with pytest.raises(ValidationError, match="relative tolerance"):
        numeric_blueprint(
            integer(5),
            tolerance={"absolute": "0", "relative": "0.06"},
        )
    blueprint = numeric_blueprint(
        integer(5),
        tolerance={"absolute": "1", "relative": "0"},
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint, candidate_expression=integer(5)
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_missing_candidate_is_partial_not_validated() -> None:
    report = validate_computation(
        ComputationValidationRequest(blueprint=numeric_blueprint(integer(2)))
    )

    assert report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert any(
        check.code == "candidate" and check.status == CheckStatus.INCONCLUSIVE
        for check in report.checks
    )


def test_nonfinite_division_fails_closed() -> None:
    blueprint = numeric_blueprint(operation("div", integer(1), integer(0)))
    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.VALIDATION_FAILED
    assert report.result is None


def test_numeric_constant_is_directly_constructed() -> None:
    blueprint = numeric_blueprint(ExpressionNode(kind="constant", constant="pi"))

    result = compute_blueprint(blueprint)

    assert result.exact_value == "pi"
    assert result.numeric_value.startswith("3.1415926535")


def test_extra_and_executable_fields_are_rejected() -> None:
    payload = {
        "kind": "integer",
        "integer": 1,
        "python_code": "__import__('os')",
        "url": "https://example.invalid",
    }

    with pytest.raises(ValidationError):
        ExpressionNode.model_validate(payload)


def test_integer_does_not_coerce_string_or_bool() -> None:
    with pytest.raises(ValidationError):
        ExpressionNode(kind="integer", integer="2")
    with pytest.raises(ValidationError):
        ExpressionNode(kind="integer", integer=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "integer", "integer": 1, "args": [{"kind": "integer", "integer": 2}]},
        {"kind": "rational", "numerator": 1, "denominator": 0},
        {"kind": "decimal", "decimal": "1e10"},
        {"kind": "symbol", "symbol": "X"},
        {"kind": "add", "args": [{"kind": "integer", "integer": 1}]},
        {
            "kind": "pow",
            "args": [
                {"kind": "integer", "integer": 2},
                {"kind": "integer", "integer": 13},
            ],
        },
    ],
)
def test_invalid_expression_shapes_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExpressionNode.model_validate(payload)


def test_ast_node_and_depth_bounds() -> None:
    deep = integer(1)
    for _ in range(16):
        deep = operation("neg", deep)
    with pytest.raises(ValidationError, match="depth"):
        numeric_blueprint(deep)

    layer = [integer(index) for index in range(65)]
    while len(layer) > 1:
        next_layer: list[ExpressionNode] = []
        for index in range(0, len(layer), 2):
            if index + 1 == len(layer):
                next_layer.append(layer[index])
            else:
                next_layer.append(operation("add", layer[index], layer[index + 1]))
        layer = next_layer
    large = layer[0]
    with pytest.raises(ValidationError, match="128 AST nodes"):
        numeric_blueprint(large)


def test_undeclared_symbol_and_contradictory_assumptions_are_rejected() -> None:
    with pytest.raises(ValidationError, match="undeclared"):
        numeric_blueprint(symbol("x"))
    with pytest.raises(ValidationError, match="contradictory"):
        VariableSpec(name="x", assumptions=["positive", "nonpositive"])


def test_variable_range_requires_interior_grid_and_exact_endpoint() -> None:
    with pytest.raises(ValidationError, match="interior"):
        VariableSpec(
            name="x",
            domain="integer",
            minimum=integer(0),
            maximum=integer(1),
            step=integer(1),
        )
    with pytest.raises(ValidationError, match="step grid"):
        VariableSpec(
            name="x",
            minimum=integer(0),
            maximum=integer(5),
            step=integer(2),
        )


@pytest.mark.parametrize(
    ("assumption", "minimum", "maximum"),
    [
        ("positive", 0, 3),
        ("nonnegative", -1, 3),
        ("negative", -3, 0),
        ("nonpositive", -3, 1),
        ("nonzero", -2, 2),
    ],
)
def test_variable_ranges_enforce_structured_assumptions(
    assumption: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(ValidationError, match=assumption):
        VariableSpec(
            name="x",
            domain="integer",
            minimum=integer(minimum),
            maximum=integer(maximum),
            step=integer(1),
            assumptions=[assumption],
        )


def test_substitutions_enforce_domain_and_assumptions() -> None:
    positive = AssessmentComputationBlueprint(
        profile=profile("numeric"),
        operation="substitute",
        expression=symbol("x"),
        variables=[VariableSpec(name="x", assumptions=["positive"])],
        substitutions={"x": integer(-1)},
    )
    fractional_integer = AssessmentComputationBlueprint(
        profile=profile("numeric"),
        operation="substitute",
        expression=symbol("x"),
        variables=[VariableSpec(name="x", domain="integer")],
        substitutions={"x": rational(1, 2)},
    )

    assert (
        validate_computation(ComputationValidationRequest(blueprint=positive)).status
        == ValidationStatus.VALIDATION_FAILED
    )
    assert (
        validate_computation(
            ComputationValidationRequest(blueprint=fractional_integer)
        ).status
        == ValidationStatus.VALIDATION_FAILED
    )


def test_valid_assumed_ranges_produce_only_conforming_samples() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("numeric", "webwork"),
        operation="evaluate",
        expression=symbol("x"),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(1),
                maximum=integer(5),
                step=integer(1),
                assumptions=["positive", "nonzero"],
            )
        ],
    )

    result = compute_blueprint(blueprint)

    assert all(int(sample.values["x"]) > 0 for sample in result.seed_samples)


def test_algebraic_expand_and_factor() -> None:
    x = symbol("x")
    variables = [VariableSpec(name="x")]
    expanded_blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="expand",
        expression=operation(
            "mul",
            operation("add", x, integer(1)),
            operation("sub", x, integer(1)),
        ),
        variables=variables,
    )
    factored_blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "imathas"),
        operation="factor",
        expression=operation("sub", operation("pow", x, integer(2)), integer(1)),
        variables=variables,
    )

    assert compute_blueprint(expanded_blueprint).canonical_expression == "x**2 - 1"
    assert (
        compute_blueprint(factored_blueprint).canonical_expression == "(x - 1)*(x + 1)"
    )


def test_polynomial_equivalence_and_degree_limit() -> None:
    x = symbol("x")
    equivalent = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="equivalent",
        expression=operation(
            "mul",
            operation("add", x, integer(1)),
            operation("sub", x, integer(1)),
        ),
        comparison_expression=operation(
            "sub", operation("pow", x, integer(2)), integer(1)
        ),
        variables=[VariableSpec(name="x")],
    )
    unsupported = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="expand",
        expression=operation("pow", x, integer(5)),
        variables=[VariableSpec(name="x")],
    )

    assert compute_blueprint(equivalent).equivalent is True
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=unsupported,
            candidate_expression=operation("pow", x, integer(5)),
            native_engine_evidence=native_evidence(),
        )
    )
    assert report.status == ValidationStatus.UNSUPPORTED


@pytest.mark.parametrize(
    ("expression", "substitution", "candidate"),
    [
        (
            operation("pow", symbol("x"), integer(5)),
            integer(2),
            integer(32),
        ),
        (
            operation("pow", symbol("x"), rational(1, 2)),
            integer(4),
            integer(2),
        ),
        (
            operation("div", integer(1), symbol("x")),
            integer(2),
            rational(1, 2),
        ),
    ],
)
def test_fixed_substitution_cannot_hide_out_of_profile_algebra(
    expression: ExpressionNode,
    substitution: ExpressionNode,
    candidate: ExpressionNode,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic"),
        operation="substitute",
        expression=expression,
        variables=[VariableSpec(name="x")],
        substitutions={"x": substitution},
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=candidate,
        )
    )

    assert report.status == ValidationStatus.UNSUPPORTED
    assert report.result is None


def test_fixed_quartic_substitution_remains_in_profile() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic"),
        operation="substitute",
        expression=operation("pow", x, integer(4)),
        variables=[VariableSpec(name="x")],
        substitutions={"x": integer(2)},
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(16),
        )
    )

    assert report.status == ValidationStatus.VALIDATED


@pytest.mark.parametrize("operation_name", ["expand", "factor"])
def test_fixed_substitution_cannot_hide_nonpolynomial_coefficient(
    operation_name: str,
) -> None:
    x = symbol("x")
    parameter = symbol("p")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation=operation_name,
        expression=operation(
            "mul",
            x,
            operation("pow", parameter, rational(1, 2)),
        ),
        variables=[VariableSpec(name="x"), VariableSpec(name="p")],
        substitutions={"p": integer(4)},
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.UNSUPPORTED


def test_fixed_substitution_cannot_hide_out_of_profile_comparison_rhs_or_choice() -> (
    None
):
    x = symbol("x")
    hidden_degree_five = operation("pow", x, integer(5))
    comparison = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="equivalent",
        expression=integer(32),
        comparison_expression=hidden_degree_five,
        variables=[VariableSpec(name="x")],
        substitutions={"x": integer(2)},
    )
    rhs = AssessmentComputationBlueprint(
        profile=profile("algebraic"),
        operation="solve",
        expression=symbol("z"),
        equation_rhs=hidden_degree_five,
        solve_for="z",
        variables=[VariableSpec(name="x"), VariableSpec(name="z")],
        substitutions={"x": integer(2)},
    )
    choice = AssessmentComputationBlueprint(
        profile=profile("algebraic", "multiple_choice"),
        operation="substitute",
        expression=x,
        variables=[VariableSpec(name="x")],
        substitutions={"x": integer(2)},
        choice_expressions=[x, hidden_degree_five, integer(3), integer(4)],
    )

    assert (
        validate_computation(ComputationValidationRequest(blueprint=comparison)).status
        == ValidationStatus.UNSUPPORTED
    )
    assert (
        validate_computation(
            ComputationValidationRequest(
                blueprint=rhs,
                candidate_solutions=[integer(32)],
            )
        ).status
        == ValidationStatus.UNSUPPORTED
    )
    assert (
        validate_computation(ComputationValidationRequest(blueprint=choice)).status
        == ValidationStatus.UNSUPPORTED
    )


@pytest.mark.parametrize(
    ("blueprint_expression", "substitution", "candidate"),
    [
        (
            operation("mul", integer(16), symbol("x")),
            integer(2),
            operation("pow", symbol("x"), integer(5)),
        ),
        (
            operation("div", symbol("x"), integer(2)),
            integer(4),
            operation("pow", symbol("x"), rational(1, 2)),
        ),
    ],
)
def test_candidate_cannot_hide_out_of_profile_shape_through_substitution(
    blueprint_expression: ExpressionNode,
    substitution: ExpressionNode,
    candidate: ExpressionNode,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic"),
        operation="substitute",
        expression=blueprint_expression,
        variables=[VariableSpec(name="x")],
        substitutions={"x": substitution},
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=candidate,
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED
    assert any(check.code == "candidate_profile" for check in report.checks)


@pytest.mark.parametrize(
    ("rhs", "substitution", "candidate"),
    [
        (
            operation("mul", integer(16), symbol("p")),
            integer(2),
            operation("pow", symbol("p"), integer(5)),
        ),
        (
            operation("div", symbol("p"), integer(2)),
            integer(4),
            operation("pow", symbol("p"), rational(1, 2)),
        ),
    ],
)
def test_solution_candidate_cannot_hide_out_of_profile_shape_through_substitution(
    rhs: ExpressionNode,
    substitution: ExpressionNode,
    candidate: ExpressionNode,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic"),
        operation="solve",
        expression=symbol("z"),
        equation_rhs=rhs,
        solve_for="z",
        variables=[VariableSpec(name="p"), VariableSpec(name="z")],
        substitutions={"p": substitution},
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_solutions=[candidate],
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED
    assert any(check.code == "candidate_profile" for check in report.checks)


def test_closed_exact_radicals_remain_valid_quadratic_solution_candidates() -> None:
    x = symbol("x")
    positive_root = operation("pow", integer(2), rational(1, 2))
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("pow", x, integer(2)),
        equation_rhs=integer(2),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_solutions=[
                operation("neg", positive_root),
                positive_root,
            ],
        )
    )

    assert report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert any(
        check.code == "candidate_solutions" and check.status == CheckStatus.PASSED
        for check in report.checks
    )


def test_ranged_algebraic_degree_is_qualified_before_substitution() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="substitute",
        expression=operation("pow", x, integer(5)),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(0),
                maximum=integer(10),
                step=integer(1),
            )
        ],
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.UNSUPPORTED


def test_all_ranged_algebraic_result_branches_retain_seed_evidence() -> None:
    x = symbol("x")
    variable = VariableSpec(
        name="x",
        domain="integer",
        minimum=integer(0),
        maximum=integer(10),
        step=integer(1),
    )
    expanded = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="expand",
        expression=operation("pow", operation("add", x, integer(1)), integer(2)),
        variables=[variable],
    )
    factored = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="factor",
        expression=operation("sub", operation("pow", x, integer(2)), integer(1)),
        variables=[variable],
    )
    equivalent = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="equivalent",
        expression=operation("pow", operation("add", x, integer(1)), integer(2)),
        comparison_expression=operation(
            "add",
            operation(
                "add",
                operation("pow", x, integer(2)),
                operation("mul", integer(2), x),
            ),
            integer(1),
        ),
        variables=[variable],
    )

    for blueprint in (expanded, factored, equivalent):
        result = compute_blueprint(blueprint)
        assert sum(sample.kind == "seeded" for sample in result.seed_samples) == 25
    report = validate_computation(ComputationValidationRequest(blueprint=equivalent))
    assert report.status == ValidationStatus.PARTIALLY_VALIDATED


def test_false_equivalence_claim_fails_validation() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="equivalent",
        expression=operation("pow", x, integer(2)),
        comparison_expression=operation("add", x, integer(2)),
        variables=[VariableSpec(name="x")],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint, native_engine_evidence=native_evidence()
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_decimal_literals_remain_exact_and_do_not_equal_one_third() -> None:
    x = symbol("x")
    finite_third = decimal("0.3333333333333333333333333333333333333333")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "multiple_choice"),
        operation="substitute",
        expression=operation("mul", rational(1, 3), x),
        variables=[VariableSpec(name="x")],
        choice_expressions=[
            operation("mul", finite_third, x),
            operation("mul", rational(1, 3), x),
            operation("mul", rational(1, 2), x),
            operation("mul", rational(2, 3), x),
        ],
    )

    compiled_decimal = computation.compile_expression(finite_third)
    result = compute_blueprint(blueprint)

    assert compiled_decimal.is_Rational
    assert compiled_decimal != computation._sympy.Rational(1, 3)
    assert result.correct_choice_index == 1


def test_rational_function_is_unsupported_for_algebra() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="expand",
        expression=operation("div", integer(1), x),
        variables=[VariableSpec(name="x")],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation("div", integer(1), x),
            native_engine_evidence=native_evidence(),
        )
    )

    assert report.status == ValidationStatus.UNSUPPORTED


def test_on_grid_rational_function_is_still_outside_algebra_profile() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="substitute",
        expression=operation("div", x, x),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                assumptions=["positive"],
                minimum=integer(1),
                maximum=integer(3),
                step=integer(1),
            )
        ],
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.UNSUPPORTED
    assert any(
        "variable-dependent denominator" in check.message for check in report.checks
    )


def test_transcendental_coefficient_is_outside_rational_algebra_profile() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="substitute",
        expression=operation(
            "mul",
            ExpressionNode(kind="constant", constant="pi"),
            x,
        ),
        variables=[VariableSpec(name="x")],
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.UNSUPPORTED
    assert any("non-rational constant" in check.message for check in report.checks)


def quadratic_solve_blueprint() -> AssessmentComputationBlueprint:
    x = symbol("x")
    return AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("sub", operation("pow", x, integer(2)), integer(1)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )


def test_quadratic_solve_and_complete_candidate_set() -> None:
    blueprint = quadratic_solve_blueprint()

    result = compute_blueprint(blueprint)
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_solutions=[integer(-1), integer(1)],
            native_engine_evidence=native_evidence(),
        )
    )

    assert result.solutions == ["-1", "1"]
    assert result.solution_expressions == [integer(-1), integer(1)]
    assert report.status == ValidationStatus.PARTIALLY_VALIDATED


@pytest.mark.parametrize(
    "candidates",
    [
        [integer(1)],
        [integer(-1), integer(1), integer(2)],
        [integer(-1), integer(-1), integer(1)],
    ],
)
def test_solve_rejects_missing_extraneous_or_duplicate_solutions(
    candidates: list[ExpressionNode],
) -> None:
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=quadratic_solve_blueprint(),
            candidate_solutions=candidates,
            native_engine_evidence=native_evidence(),
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_solve_with_no_real_solutions_accepts_explicit_empty_set() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("add", operation("pow", x, integer(2)), integer(1)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_solutions=[],
            native_engine_evidence=native_evidence(),
        )
    )

    assert report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert report.result is not None and report.result.solutions == []


def test_quadratic_irrational_solutions_use_typed_rational_powers() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("sub", operation("pow", x, integer(2)), integer(2)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )
    square_root_two = operation("pow", integer(2), rational(1, 2))

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_solutions=[
                operation("neg", square_root_two),
                square_root_two,
            ],
            native_engine_evidence=native_evidence(),
        )
    )

    assert report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert report.result is not None
    assert report.result.solutions == ["-sqrt(2)", "sqrt(2)"]
    assert [
        computation.compile_expression(node)
        for node in report.result.solution_expressions
    ] == [
        -computation._sympy.sqrt(2),
        computation._sympy.sqrt(2),
    ]


def test_solve_filters_roots_by_domain_assumptions() -> None:
    x = symbol("x")
    positive_quadratic = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("sub", operation("pow", x, integer(2)), integer(1)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x", assumptions=["positive"])],
    )
    positive_linear = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("add", x, integer(1)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x", assumptions=["positive"])],
    )
    integer_quadratic = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="solve",
        expression=operation("sub", operation("pow", x, integer(2)), integer(2)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x", domain="integer")],
    )

    assert compute_blueprint(positive_quadratic).solutions == ["1"]
    assert compute_blueprint(positive_linear).solutions == []
    assert compute_blueprint(integer_quadratic).solutions == []
    accepted = validate_computation(
        ComputationValidationRequest(
            blueprint=positive_quadratic,
            candidate_solutions=[integer(1)],
        )
    )
    rejected = validate_computation(
        ComputationValidationRequest(
            blueprint=positive_quadratic,
            candidate_solutions=[integer(-1), integer(1)],
        )
    )
    assert accepted.status == ValidationStatus.PARTIALLY_VALIDATED
    assert rejected.status == ValidationStatus.VALIDATION_FAILED


def test_symbolic_algebra_cannot_use_native_numerical_delivery() -> None:
    with pytest.raises(ValidationError, match="symbolic algebra"):
        AssessmentComputationBlueprint(
            profile=profile("algebraic", "numerical"),
            operation="expand",
            expression=operation("pow", symbol("x"), integer(2)),
            variables=[VariableSpec(name="x")],
        )


def test_numerical_delivery_requires_resolved_scalar_or_single_solution() -> None:
    with pytest.raises(computation.ComputationUnsupportedError, match="resolved"):
        compute_blueprint(
            AssessmentComputationBlueprint(
                profile=profile("numeric", "numerical"),
                operation="evaluate",
                expression=symbol("x"),
                variables=[VariableSpec(name="x")],
            )
        )
    x = symbol("x")
    linear = AssessmentComputationBlueprint(
        profile=profile("algebraic", "numerical"),
        operation="solve",
        expression=operation("add", operation("mul", integer(2), x), integer(1)),
        equation_rhs=integer(0),
        solve_for="x",
        variables=[VariableSpec(name="x")],
    )
    quadratic = quadratic_solve_blueprint().model_copy(
        update={"profile": profile("algebraic", "numerical")}
    )

    linear_result = compute_blueprint(linear)
    assert linear_result.exact_value == "-1/2"
    assert linear_result.solution_expressions == [rational(-1, 2)]
    with pytest.raises(computation.ComputationUnsupportedError, match="exactly one"):
        compute_blueprint(quadratic)


def test_ranged_variables_require_external_engine_delivery() -> None:
    with pytest.raises(ValidationError, match="ranged variables"):
        numeric_blueprint(
            symbol("x"),
            variables=[
                VariableSpec(
                    name="x",
                    minimum=integer(0),
                    maximum=integer(2),
                    step=integer(1),
                )
            ],
        )


def test_variable_cannot_be_both_ranged_and_fixed() -> None:
    ranged = VariableSpec(
        name="x",
        domain="integer",
        minimum=integer(0),
        maximum=integer(2),
        step=integer(1),
    )

    with pytest.raises(ValidationError, match="both ranged and fixed"):
        AssessmentComputationBlueprint(
            profile=profile("numeric", "webwork"),
            operation="evaluate",
            expression=symbol("x"),
            variables=[ranged],
            substitutions={"x": integer(1)},
        )


def test_multiple_choice_ground_truth_is_bound_before_generation() -> None:
    blueprint = numeric_blueprint(
        operation("add", integer(2), integer(3)),
        delivery="multiple_choice",
        choice_expressions=[integer(4), integer(5), integer(6), integer(7)],
    )

    result = compute_blueprint(blueprint)
    partial = validate_computation(ComputationValidationRequest(blueprint=blueprint))
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            choices=[
                ComputationChoice(choice_id="A", expression=integer(4)),
                ComputationChoice(
                    choice_id="B", expression=integer(5), marked_correct=True
                ),
                ComputationChoice(choice_id="C", expression=integer(6)),
                ComputationChoice(choice_id="D", expression=integer(7)),
            ],
        )
    )

    assert result.correct_choice_index == 1
    assert partial.status == ValidationStatus.PARTIALLY_VALIDATED
    assert report.status == ValidationStatus.VALIDATED


def test_multiple_choice_duplicate_equivalent_and_wrong_mark_are_rejected() -> None:
    duplicate = numeric_blueprint(
        integer(5),
        delivery="multiple_choice",
        choice_expressions=[
            integer(5),
            rational(10, 2),
            integer(6),
            integer(7),
        ],
    )
    with pytest.raises(computation.ComputationValidationError, match="exactly one"):
        compute_blueprint(duplicate)

    blueprint = numeric_blueprint(
        integer(5),
        delivery="multiple_choice",
        choice_expressions=[integer(4), integer(5), integer(6), integer(7)],
    )
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            choices=[
                ComputationChoice(
                    choice_id="A", expression=integer(4), marked_correct=True
                ),
                ComputationChoice(choice_id="B", expression=integer(5)),
                ComputationChoice(choice_id="C", expression=integer(6)),
            ],
        )
    )
    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_candidate_domain_cannot_disappear_during_symbolic_simplification() -> None:
    blueprint = numeric_blueprint(
        integer(1),
        variables=[VariableSpec(name="x")],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation(
                "div",
                symbol("x"),
                symbol("x"),
            ),
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED
    assert report.result is None
    assert any(
        check.code == "candidate_domain" and check.status == CheckStatus.FAILED
        for check in report.checks
    )


def test_zero_tolerance_rejects_decimal_approximation_of_exact_constant() -> None:
    blueprint = numeric_blueprint(ExpressionNode(kind="constant", constant="pi"))
    result = compute_blueprint(blueprint)
    assert result.numeric_value is not None

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=decimal(result.numeric_value),
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_final_choices_must_match_blueprint_count_order_and_expressions() -> None:
    blueprint = numeric_blueprint(
        integer(5),
        delivery="multiple_choice",
        choice_expressions=[integer(4), integer(5), integer(6), integer(7)],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            choices=[
                ComputationChoice(choice_id="A", expression=integer(99)),
                ComputationChoice(
                    choice_id="B",
                    expression=integer(5),
                    marked_correct=True,
                ),
            ],
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED
    assert len({check.code for check in report.checks}) == len(report.checks)


def parameter_blueprint(
    expression: ExpressionNode,
    *,
    constraints: list[dict[str, object]] | None = None,
) -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=profile("numeric", "webwork"),
        operation="evaluate",
        expression=expression,
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(-3),
                maximum=integer(3),
                step=integer(1),
            )
        ],
        constraints=constraints or [],
    )


def test_parameter_sampling_is_reproducible_bounded_and_complete() -> None:
    blueprint = parameter_blueprint(operation("mul", integer(2), symbol("x")))

    first = compute_blueprint(blueprint)
    second = compute_blueprint(blueprint)
    seeded = [sample for sample in first.seed_samples if sample.kind == "seeded"]
    boundaries = [sample for sample in first.seed_samples if sample.kind == "boundary"]

    assert first.seed_samples == second.seed_samples
    assert len(seeded) == 25
    assert len({sample.seed for sample in seeded}) == 25
    assert all(
        sample.seed is not None and 0 <= sample.seed <= 2**31 - 1 for sample in seeded
    )
    assert {"-3", "-2", "0", "2", "3"} <= {sample.values["x"] for sample in boundaries}


def test_parameter_expression_is_evaluated_at_zero_and_boundaries() -> None:
    blueprint = parameter_blueprint(operation("div", integer(1), symbol("x")))

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation("div", integer(1), symbol("x")),
            native_engine_evidence=native_evidence(),
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


@pytest.mark.parametrize(
    "expression",
    [
        operation(
            "div",
            operation("sub", symbol("x"), integer(3)),
            operation("sub", symbol("x"), integer(3)),
        ),
        operation("div", integer(1), operation("sub", symbol("x"), integer(3))),
        operation(
            "pow",
            operation("sub", symbol("x"), integer(3)),
            integer(-1),
        ),
    ],
)
def test_structural_singularities_are_checked_before_simplification(
    expression: ExpressionNode,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("numeric", "webwork"),
        operation="evaluate",
        expression=expression,
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(0),
                maximum=integer(100),
                step=integer(1),
            )
        ],
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_structural_singularity_must_be_explicitly_excluded() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("numeric", "webwork"),
        operation="evaluate",
        expression=operation("div", integer(1), operation("sub", x, integer(3))),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(0),
                maximum=integer(100),
                step=integer(1),
            )
        ],
        constraints=[
            {
                "operator": "ne",
                "left": x,
                "right": integer(3),
            }
        ],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation(
                "div", integer(1), operation("sub", x, integer(3))
            ),
        )
    )

    assert report.status == ValidationStatus.PARTIALLY_VALIDATED
    structural = next(
        check for check in report.checks if check.code == "structural_singularities"
    )
    assert structural.details["candidates"] == [
        {
            "variable": "x",
            "value": "3",
            "status": "excluded_by_constraints",
        }
    ]


def test_constraints_generate_exactly_25_valid_seed_samples() -> None:
    constraint = {
        "operator": "gt",
        "left": symbol("x").model_dump(),
        "right": integer(0).model_dump(),
    }
    blueprint = parameter_blueprint(
        operation("pow", symbol("x"), integer(2)), constraints=[constraint]
    )

    result = compute_blueprint(blueprint)

    seeded = [sample for sample in result.seed_samples if sample.kind == "seeded"]
    assert len(seeded) == 25
    assert all(int(sample.values["x"]) > 0 for sample in seeded)


def test_formula_parameter_leaves_one_qualified_response_symbol() -> None:
    a = symbol("a")
    x = symbol("x")
    expected = operation("add", operation("mul", a, x), integer(1))
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="substitute",
        expression=expected,
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=integer(1),
                maximum=integer(5),
                step=integer(1),
            ),
            VariableSpec(name="x"),
        ],
    )

    result = compute_blueprint(blueprint)
    accepted = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation("add", integer(1), operation("mul", x, a)),
            native_engine_evidence=native_evidence(),
        )
    )
    rejected = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation("add", operation("mul", a, x), integer(2)),
            native_engine_evidence=native_evidence(),
        )
    )

    assert result.answer_expression == expected
    assert accepted.status == ValidationStatus.PARTIALLY_VALIDATED
    assert rejected.status == ValidationStatus.VALIDATION_FAILED


def test_parameterized_rational_equivalence_is_exact() -> None:
    x = symbol("x")
    blueprint = AssessmentComputationBlueprint(
        profile=profile("numeric", "webwork"),
        operation="evaluate",
        expression=operation("div", x, integer(3)),
        variables=[
            VariableSpec(
                name="x",
                domain="integer",
                minimum=integer(1),
                maximum=integer(9),
                step=integer(1),
            )
        ],
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=operation("mul", rational(1, 3), x),
        )
    )

    assert report.status == ValidationStatus.PARTIALLY_VALIDATED


def test_formula_parameters_reject_multiple_response_symbols() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("algebraic", "webwork"),
        operation="substitute",
        expression=operation(
            "add",
            operation("mul", symbol("a"), symbol("x")),
            symbol("y"),
        ),
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=integer(1),
                maximum=integer(5),
                step=integer(1),
            ),
            VariableSpec(name="x"),
            VariableSpec(name="y"),
        ],
    )

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.UNSUPPORTED


def test_impossible_constraints_fail_closed() -> None:
    impossible = [{"operator": "gt", "left": symbol("x"), "right": integer(10)}]
    blueprint = parameter_blueprint(symbol("x"), constraints=impossible)

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_hashes_and_seed_plan_are_stable_and_sensitive() -> None:
    first = numeric_blueprint(integer(1))
    same = numeric_blueprint(integer(1))
    different = numeric_blueprint(integer(2))

    assert canonical_blueprint_hash(first) == canonical_blueprint_hash(same)
    assert canonical_blueprint_hash(first) != canonical_blueprint_hash(different)
    assert deterministic_seeds(first) == deterministic_seeds(same)
    assert len(set(deterministic_seeds(first))) == 25


def test_range_literal_nodes_count_toward_blueprint_ast_budget() -> None:
    layer = [integer(index) for index in range(50)]
    while len(layer) > 1:
        next_layer: list[ExpressionNode] = []
        for index in range(0, len(layer), 2):
            if index + 1 == len(layer):
                next_layer.append(layer[index])
            else:
                next_layer.append(operation("add", layer[index], layer[index + 1]))
        layer = next_layer
    variables = [
        VariableSpec(
            name=f"x{index}",
            domain="integer",
            minimum=integer(0),
            maximum=integer(2),
            step=integer(1),
        )
        for index in range(10)
    ]

    with pytest.raises(ValidationError, match="128 AST nodes"):
        AssessmentComputationBlueprint(
            profile=profile("numeric", "webwork"),
            operation="evaluate",
            expression=layer[0],
            variables=variables,
        )


def test_validation_request_enforces_aggregate_candidate_ast_bounds() -> None:
    deep = integer(1)
    for _ in range(16):
        deep = operation("neg", deep)
    with pytest.raises(ValidationError, match="candidate expression depth"):
        ComputationValidationRequest(
            blueprint=numeric_blueprint(integer(1)),
            candidate_expression=deep,
        )

    choice_blueprint = numeric_blueprint(
        integer(1),
        delivery="multiple_choice",
        choice_expressions=[integer(1), integer(2), integer(3), integer(4)],
    )
    choices: list[ComputationChoice] = []
    for index in range(12):
        expression = integer(index)
        for _ in range(6):
            expression = operation("add", expression, integer(1))
        choices.append(
            ComputationChoice(
                choice_id=chr(ord("A") + index),
                expression=expression,
                marked_correct=index == 0,
            )
        )
    with pytest.raises(ValidationError, match="aggregate AST nodes"):
        ComputationValidationRequest(
            blueprint=choice_blueprint,
            choices=choices,
        )


@pytest.mark.parametrize(
    "code",
    [
        "1",
        "%",
        "m",
        "kg.m/s2",
        "cm2",
        "m/s",
        "m3",
        "deg",
        "Ohm",
        "mL",
        "m0",
        "m-0",
        "%0",
        "kg0",
        "1/s",
        "1.m",
        "m/1",
        "m/s/s",
        "m/s.s",
        "kg/m/s2",
        "/s",
        "/m.s",
        "/m/s",
    ],
)
def test_qualified_ucum_codes(code: str) -> None:
    assert validate_unit_code(code) == code


@pytest.mark.parametrize(
    "code",
    [
        "Cel",
        "dB",
        "[foo]",
        "m^3",
        "kg*m/s^2",
        "m4",
        "m-4",
        "m+2",
        "10",
        "11",
        "12",
        "13",
        "m//s",
        "//s",
        "m///s",
        "m/./s",
        "m./s",
        "/",
        "/Cel",
        "/[foo]",
        "/m{foo}",
        "/(m.s)",
        "../m",
        "http://m",
    ],
)
def test_unqualified_unit_codes_are_rejected(code: str) -> None:
    with pytest.raises(ValueError, match="qualified"):
        validate_unit_code(code)


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        (integer(100), "cm", "m", "1"),
        (integer(1), "m3", "L", "1000"),
        (integer(100), "%", "1", "1"),
        (integer(1), "m0", "1", "1"),
        (integer(1), "1/s", "Hz", "1"),
        (integer(1), "%0", "1", "1"),
    ],
)
def test_unit_conversion_and_round_trip(
    value: ExpressionNode, source: str, target: str, expected: str
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=value,
        source_unit=source,
        target_unit=target,
    )
    result = compute_blueprint(blueprint)
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=decimal(expected),
            candidate_unit=target,
        )
    )

    assert result.numeric_value == expected
    assert report.status == ValidationStatus.VALIDATED
    assert {check.code for check in report.checks} >= {
        "unit_dimensions",
        "unit_round_trip",
    }


def test_ucum_compound_operators_are_left_associative() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=decimal("6.3"),
        source_unit="s/m.mg",
        target_unit="s.m-1.g",
    )

    result = compute_blueprint(blueprint)

    assert result.numeric_value == "0.0063"


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("m/s/s", "m.s-2", "1"),
        ("m/s.s", "m", "1"),
        ("/m.s", "m-1.s-1", "1"),
        ("/m/s", "s/m", "1"),
        ("/cm", "/m", "100"),
        ("cm/m/cm", "/m", "1"),
    ],
)
def test_ucum_repeated_division_and_leading_reciprocal_semantics(
    source: str,
    target: str,
    expected: str,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=integer(1),
        source_unit=source,
        target_unit=target,
    )

    result = compute_blueprint(blueprint)

    assert result.numeric_value == expected


def test_angular_unit_conversion_is_supported() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=integer(180),
        source_unit="deg",
        target_unit="rad",
    )

    result = compute_blueprint(blueprint)

    assert result.exact_value == "pi"
    assert result.numeric_value is not None
    assert float(result.numeric_value) == pytest.approx(3.141592653589793)


@pytest.mark.parametrize(
    ("expression", "source_unit", "target_unit"),
    [
        (
            operation(
                "pow",
                ExpressionNode(kind="constant", constant="pi"),
                integer(12),
            ),
            "deg3",
            "rad3",
        ),
        (
            integer(1),
            ".".join(["kW3"] * 12),
            ".".join(["W3"] * 12),
        ),
    ],
)
def test_unit_result_outside_typed_ast_limits_is_stably_unsupported(
    expression: ExpressionNode,
    source_unit: str,
    target_unit: str,
) -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=expression,
        source_unit=source_unit,
        target_unit=target_unit,
    )

    with pytest.raises(
        computation.ComputationUnsupportedError,
        match="typed expression bounds",
    ):
        compute_blueprint(blueprint)

    report = validate_computation(ComputationValidationRequest(blueprint=blueprint))
    assert report.status == ValidationStatus.UNSUPPORTED
    assert report.result is None
    assert any(
        check.code == "supported_profile" and check.status == CheckStatus.INCONCLUSIVE
        for check in report.checks
    )


def test_unit_conversion_preserves_integer_precision_above_float_range() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=integer(9_007_199_254_740_993),
        source_unit="cm",
        target_unit="m",
    )

    result = compute_blueprint(blueprint)
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=rational(9_007_199_254_740_993, 100),
            candidate_unit="m",
        )
    )

    assert result.exact_value == "9007199254740993/100"
    assert result.numeric_value == "90071992547409.93"
    assert result.answer_expression == rational(9_007_199_254_740_993, 100)
    assert report.status == ValidationStatus.VALIDATED


def test_fixed_and_parameterized_units_expose_typed_target_answer() -> None:
    fixed = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=integer(150),
        source_unit="cm",
        target_unit="m",
    )
    parameterized = AssessmentComputationBlueprint(
        profile=profile("unit", "webwork"),
        operation="convert_unit",
        expression=symbol("a"),
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=integer(100),
                maximum=integer(500),
                step=integer(100),
            )
        ],
        source_unit="cm",
        target_unit="m",
    )

    fixed_result = compute_blueprint(fixed)
    parameterized_result = compute_blueprint(parameterized)
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=parameterized,
            candidate_expression=operation("mul", decimal("0.01"), symbol("a")),
            candidate_unit="m",
            native_engine_evidence=native_evidence(),
        )
    )

    assert fixed_result.numeric_value == "1.5"
    assert fixed_result.exact_value == "3/2"
    assert fixed_result.answer_expression == rational(3, 2)
    assert parameterized_result.answer_expression is not None
    assert report.status == ValidationStatus.PARTIALLY_VALIDATED


def test_dimension_mismatch_fails_validation() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=integer(1),
        source_unit="m",
        target_unit="s",
    )

    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(1),
            candidate_unit="s",
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_unit_candidate_must_use_fixed_target_unit() -> None:
    blueprint = AssessmentComputationBlueprint(
        profile=profile("unit"),
        operation="convert_unit",
        expression=integer(100),
        source_unit="cm",
        target_unit="m",
    )
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(1),
            candidate_unit="cm",
        )
    )

    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_native_engine_evidence_controls_external_status() -> None:
    blueprint = numeric_blueprint(integer(2), delivery="webwork")
    missing = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint, candidate_expression=integer(2)
        )
    )
    passed = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(2),
            native_engine_evidence=native_evidence(),
        )
    )
    failed = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(2),
            native_engine_evidence=native_evidence(passed=False),
        )
    )

    assert missing.status == ValidationStatus.PARTIALLY_VALIDATED
    assert passed.status == ValidationStatus.PARTIALLY_VALIDATED
    assert any(
        check.code == "native_engine"
        and check.status == CheckStatus.INCONCLUSIVE
        and "server-trusted" in check.message
        for check in passed.checks
    )
    assert failed.status == ValidationStatus.VALIDATION_FAILED


def test_native_evidence_must_match_delivery() -> None:
    blueprint = numeric_blueprint(integer(2), delivery="imathas")
    with pytest.raises(ValidationError, match="does not match"):
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(2),
            native_engine_evidence=native_evidence("webwork"),
        )


def test_native_boolean_cannot_replace_hashed_evidence() -> None:
    blueprint = numeric_blueprint(integer(2), delivery="webwork")

    with pytest.raises(ValidationError, match="cannot replace"):
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=integer(2),
            native_engine_validated=True,
        )


def test_dependency_readiness_and_ucum_checksum() -> None:
    assert_computation_dependencies()
    versions = validate_computation(
        ComputationValidationRequest(
            blueprint=numeric_blueprint(integer(1)),
            candidate_expression=integer(1),
        )
    ).dependencies

    assert versions["sympy"] == "1.14.0"
    assert versions["pint"] == "0.25.3"
    assert versions["ucumvert"] == "0.3.2"
    assert versions["ucum_essence_sha256"] == computation.UCUM_ESSENCE_SHA256


def test_dependency_version_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(computation._sympy, "__version__", "99.0")

    with pytest.raises(ComputationDependencyError, match="not qualified"):
        assert_computation_dependencies()
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=numeric_blueprint(integer(1)),
            candidate_expression=integer(1),
        )
    )
    assert report.status == ValidationStatus.VALIDATION_FAILED


def test_source_never_invokes_string_expression_or_code_parsers() -> None:
    source = inspect.getsource(computation)

    for forbidden_call in (
        "sympify(",
        "parse_expr(",
        "lambdify(",
        "eval(",
        "exec(",
        "pickle.",
        "__import__(",
    ):
        assert forbidden_call not in source
