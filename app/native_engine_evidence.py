from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .computation import (
    AssessmentComputationBlueprint,
    ComparisonConstraint,
    ComputationOperation,
    ComputationResult,
    ExpressionKind,
    ExpressionNode,
    canonical_blueprint_hash,
    deterministic_seeds,
)
from .native_engine_runner import (
    NativeEngineRunnerReceipt,
    seed_observations_sha256,
    seed_plan_sha256,
)
from .parameterized import (
    typed_formula_submission,
    typed_parameterized_constraints_satisfied,
    typed_parameterized_submission_pair,
)
from .schemas import ParameterVariable, ParameterizedItemSpec


class NativeEvidenceVerificationError(ValueError):
    """Native observations do not match the server-owned typed computation."""


@dataclass(frozen=True)
class NativeVerificationPlan:
    spec: ParameterizedItemSpec
    answer_expression: ExpressionNode
    alternate_expression: ExpressionNode | None
    constraints: tuple[ComparisonConstraint, ...]


def native_verification_plan(
    blueprint: AssessmentComputationBlueprint,
    result: ComputationResult,
) -> NativeVerificationPlan:
    """Derive native verification inputs without parsing strings or using CAS."""

    if result.blueprint_hash != canonical_blueprint_hash(blueprint):
        raise NativeEvidenceVerificationError(
            "native result does not match the typed blueprint"
        )
    answer_expression = _replace_fixed_symbols(
        result.answer_expression or blueprint.expression,
        blueprint,
    )
    constraints = tuple(
        constraint.model_copy(
            update={
                "left": _replace_fixed_symbols(constraint.left, blueprint),
                "right": _replace_fixed_symbols(constraint.right, blueprint),
            },
            deep=True,
        )
        for constraint in blueprint.constraints
    )
    referenced = _referenced_symbols(answer_expression)
    ranged = [
        variable for variable in blueprint.variables if variable.minimum is not None
    ]
    response_symbols = [
        variable.name
        for variable in blueprint.variables
        if variable.minimum is None
        and variable.name in referenced
        and variable.name not in blueprint.substitutions
    ]
    if not ranged and not response_symbols:
        raise NativeEvidenceVerificationError(
            "native external evidence requires parameters or response symbols"
        )
    parameters: list[ParameterVariable] = []
    for variable in ranged:
        if variable.domain.value != "integer":
            raise NativeEvidenceVerificationError(
                "native v0 evidence supports only integer parameter grids"
            )
        parameters.append(
            ParameterVariable(
                name=variable.name,
                minimum=float(_integer_literal(variable.minimum)),
                maximum=float(_integer_literal(variable.maximum)),
                step=float(_integer_literal(variable.step)),
                integer=True,
            )
        )
    answer_kind = "formula" if response_symbols else "numeric"
    magnitude = Decimal(result.numeric_value or "0")
    tolerance = max(
        Decimal(blueprint.tolerance.absolute),
        Decimal(blueprint.tolerance.relative) * abs(magnitude),
    )
    tolerance_float = float(tolerance)
    if not math.isfinite(tolerance_float):
        raise NativeEvidenceVerificationError("native tolerance is not finite")
    spec = ParameterizedItemSpec(
        engine=blueprint.profile.delivery.value,
        variables=parameters,
        prompt_template="Typed native verification prompt.",
        answer_expression="server-owned-typed-expression",
        answer_kind=answer_kind,
        compiler_profile="assessment_computation_v0",
        response_symbols=response_symbols,
        explanation_template="Typed native verification explanation.",
        tolerance=tolerance_float,
        units=result.target_unit,
        seed_policy="per_student",
    )
    alternate_expression: ExpressionNode | None = None
    if answer_kind == "formula":
        if blueprint.operation == ComputationOperation.EQUIVALENT:
            alternate_expression = blueprint.comparison_expression
        elif blueprint.operation == ComputationOperation.SUBSTITUTE:
            alternate_expression = ExpressionNode(
                kind=ExpressionKind.ADD,
                args=[
                    answer_expression,
                    ExpressionNode(kind=ExpressionKind.INTEGER, integer=0),
                ],
            )
        elif blueprint.operation in {
            ComputationOperation.EXPAND,
            ComputationOperation.FACTOR,
        }:
            alternate_expression = blueprint.expression
        if alternate_expression is None:
            raise NativeEvidenceVerificationError(
                "formula evidence requires an independent typed equivalent form"
            )
        alternate_expression = _replace_fixed_symbols(
            alternate_expression,
            blueprint,
        )
    return NativeVerificationPlan(
        spec=spec,
        answer_expression=answer_expression,
        alternate_expression=alternate_expression,
        constraints=constraints,
    )


def verify_native_engine_observations(
    *,
    blueprint: AssessmentComputationBlueprint,
    result: ComputationResult,
    receipt: NativeEngineRunnerReceipt,
) -> None:
    """Independently validate all 25 engine-observed seed executions."""

    plan = native_verification_plan(blueprint, result)
    seeds = list(deterministic_seeds(blueprint))
    if (
        receipt.answer_kind != plan.spec.answer_kind
        or receipt.blueprint_sha256 != canonical_blueprint_hash(blueprint)
        or receipt.seeds != seeds
        or receipt.seed_plan_sha256 != seed_plan_sha256(seeds)
        or [item.seed for item in receipt.observations] != seeds
        or receipt.seed_receipts_sha256
        != seed_observations_sha256(receipt.observations)
    ):
        raise NativeEvidenceVerificationError(
            "native observations do not match the typed seed plan"
        )
    for observation in receipt.observations:
        values = observation.observed_variables
        _validate_observed_grid(plan.spec, values)
        if (
            not observation.parameters_satisfied
            or not observation.constraints_satisfied
            or not typed_parameterized_constraints_satisfied(
                plan.constraints,
                values,
            )
        ):
            raise NativeEvidenceVerificationError(
                "native observed parameters violate typed constraints"
            )
        correct, wrong = typed_parameterized_submission_pair(
            plan.spec,
            plan.answer_expression,
            values,
        )
        if (
            observation.correct_submission_sha256 != _text_sha256(correct)
            or observation.wrong_submission_sha256 != _text_sha256(wrong)
            or not observation.correct_answer_accepted
            or not observation.wrong_answer_rejected
        ):
            raise NativeEvidenceVerificationError(
                "native grading does not match typed correct/wrong submissions"
            )
        if plan.alternate_expression is None:
            if (
                observation.alternate_correct_submission_sha256 is not None
                or observation.alternate_correct_answer_accepted is not None
            ):
                raise NativeEvidenceVerificationError(
                    "numeric evidence cannot claim a formula alternate"
                )
        else:
            alternate = typed_formula_submission(
                plan.spec,
                plan.alternate_expression,
                values,
            )
            if (
                alternate == correct
                or observation.alternate_correct_submission_sha256
                != _text_sha256(alternate)
                or observation.alternate_correct_answer_accepted is not True
            ):
                raise NativeEvidenceVerificationError(
                    "native symbolic grading did not accept a distinct equivalent form"
                )
        if (
            not observation.rendered
            or observation.render_sha256 != observation.repeat_render_sha256
            or observation.warnings_count != 0
            or observation.errors_count != 0
            or observation.outbound_request_count != 0
        ):
            raise NativeEvidenceVerificationError(
                "native execution was nondeterministic or emitted warnings/errors"
            )


def _validate_observed_grid(
    spec: ParameterizedItemSpec,
    values: Mapping[str, int],
) -> None:
    expected = {variable.name: variable for variable in spec.variables}
    if set(values) != set(expected):
        raise NativeEvidenceVerificationError(
            "native observed variables do not match typed parameters"
        )
    for name, variable in expected.items():
        value = values[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not variable.integer
        ):
            raise NativeEvidenceVerificationError(
                "native v0 observations require integer parameter values"
            )
        minimum = Decimal(str(variable.minimum))
        maximum = Decimal(str(variable.maximum))
        step = Decimal(str(variable.step))
        observed = Decimal(value)
        if (
            minimum != minimum.to_integral_value()
            or maximum != maximum.to_integral_value()
            or step != step.to_integral_value()
            or step <= 0
            or observed < minimum
            or observed > maximum
            or (observed - minimum) % step != 0
        ):
            raise NativeEvidenceVerificationError(
                "native observed parameter is outside its typed integer grid"
            )


def _replace_fixed_symbols(
    node: ExpressionNode,
    blueprint: AssessmentComputationBlueprint,
) -> ExpressionNode:
    if node.kind == ExpressionKind.SYMBOL and node.symbol in blueprint.substitutions:
        return blueprint.substitutions[node.symbol].model_copy(deep=True)
    if not node.args:
        return node.model_copy(deep=True)
    return node.model_copy(
        update={
            "args": [_replace_fixed_symbols(child, blueprint) for child in node.args]
        },
        deep=True,
    )


def _referenced_symbols(node: ExpressionNode) -> set[str]:
    symbols = {node.symbol} if node.kind == ExpressionKind.SYMBOL else set()
    for child in node.args:
        symbols.update(_referenced_symbols(child))
    return {symbol for symbol in symbols if symbol is not None}


def _integer_literal(node: ExpressionNode | None) -> int:
    if node is None:
        raise NativeEvidenceVerificationError("native parameter grid is incomplete")
    if node.kind == ExpressionKind.INTEGER:
        assert node.integer is not None
        return node.integer
    if node.kind == ExpressionKind.RATIONAL:
        assert node.numerator is not None and node.denominator is not None
        value = Decimal(node.numerator) / Decimal(node.denominator)
    elif node.kind == ExpressionKind.DECIMAL:
        assert node.decimal is not None
        value = Decimal(node.decimal)
    else:
        raise NativeEvidenceVerificationError(
            "native parameter bounds must be numeric literals"
        )
    if value != value.to_integral_value():
        raise NativeEvidenceVerificationError(
            "native parameter bounds must be integral"
        )
    return int(value)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "NativeEvidenceVerificationError",
    "NativeVerificationPlan",
    "native_verification_plan",
    "verify_native_engine_observations",
]
