from __future__ import annotations

import ast
import hashlib
import html
import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .computation import (
    AssessmentComputationBlueprint,
    ComparisonConstraint,
    ComputationFamily,
    ComputationOperation,
    ComputationResult,
    ExpressionKind,
    ExpressionNode,
    canonical_blueprint_hash,
)
from .schemas import ParameterVariable, ParameterizedItemSpec


COMPILER_VERSION = "parameterized-dsl-v1"
COMPUTATION_COMPILER_VERSION = "assessment-computation-parameterized-dsl-v0"
FORMULA_COMPILER_VERSION = "assessment-computation-formula-dsl-v0"
TYPED_COMPUTATION_COMPILER_VERSION = "assessment-computation-typed-ast-v0"
ALGEBRA_QUALIFICATION_COMPILER_VERSION = (
    "assessment-computation-algebra-native-qualification-v0"
)
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_ALLOWED_BINARY = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.Pow: lambda left, right: left**right,
    ast.Mod: lambda left, right: left % right,
}
_ALLOWED_UNARY = {
    ast.UAdd: lambda value: value,
    ast.USub: lambda value: -value,
}
_ALLOWED_COMPARE = {
    ast.Eq: lambda left, right: left == right,
    ast.NotEq: lambda left, right: left != right,
    ast.Lt: lambda left, right: left < right,
    ast.LtE: lambda left, right: left <= right,
    ast.Gt: lambda left, right: left > right,
    ast.GtE: lambda left, right: left >= right,
}
_RESERVED_RESPONSE_SYMBOLS = {"e", "i", "inf", "infinity", "nan", "pi"}
_COMPUTATION_ENGINE_VARIABLES = {"answer", "assessment_prompt", "parameters_valid"}
_MAX_EXPRESSION_NODES = 128
_MAX_EXPRESSION_DEPTH = 16
_MAX_NUMERIC_DIGITS = 100
_MAX_EXPONENT_MAGNITUDE = 12
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class QualifiedFormulaAdapterPromotion:
    """Reviewed binding from native receipts to exact production compilers."""

    engine: str
    production_compiler_versions: frozenset[str]
    families: frozenset[str]
    operations: frozenset[str]
    qualification_compiler_version: str
    qualification_manifest_sha256: str
    qualification_report_sha256: str
    engine_image_digest: str
    adapter_image_digest: str | None
    native_grader: str
    promotion_approval_sha256: str


# Formula delivery is disabled until an exact native qualification bundle is
# reviewed and added here in source control. Runtime reports cannot promote
# themselves, and the v0 spike intentionally ships with this registry empty.
QUALIFIED_FORMULA_ADAPTERS: Mapping[
    tuple[str, str], QualifiedFormulaAdapterPromotion
] = MappingProxyType({})
_ALGEBRA_QUALIFICATION_FORMULA_TOKEN = object()


def formula_adapter_is_qualified(
    engine: str,
    compiler_version: str,
    *,
    family: str | None = None,
    operation: str | None = None,
) -> bool:
    """Resolve an exact fail-closed formula-adapter promotion."""

    return (
        qualified_formula_adapter(
            engine,
            compiler_version,
            family=family,
            operation=operation,
        )
        is not None
    )


def qualified_formula_adapter(
    engine: str,
    compiler_version: str,
    *,
    family: str | None = None,
    operation: str | None = None,
) -> QualifiedFormulaAdapterPromotion | None:
    """Return the exact reviewed promotion, never only a registry-key match."""

    promotion = QUALIFIED_FORMULA_ADAPTERS.get((engine, compiler_version))
    if (
        promotion is None
        or promotion.engine != engine
        or not isinstance(promotion.production_compiler_versions, frozenset)
        or compiler_version != TYPED_COMPUTATION_COMPILER_VERSION
        or promotion.production_compiler_versions
        != frozenset({TYPED_COMPUTATION_COMPILER_VERSION})
        or promotion.families != frozenset({"algebraic"})
        or promotion.operations
        != frozenset({"substitute", "expand", "factor", "equivalent"})
        or (family is not None and family not in promotion.families)
        or (operation is not None and operation not in promotion.operations)
        or promotion.qualification_compiler_version
        != TYPED_COMPUTATION_COMPILER_VERSION
        or _SHA256.fullmatch(promotion.qualification_manifest_sha256) is None
        or _SHA256.fullmatch(promotion.qualification_report_sha256) is None
        or _SHA256.fullmatch(promotion.promotion_approval_sha256) is None
        or len(
            {
                promotion.qualification_manifest_sha256,
                promotion.qualification_report_sha256,
                promotion.promotion_approval_sha256,
            }
        )
        != 3
        or _IMAGE_DIGEST.fullmatch(promotion.engine_image_digest) is None
    ):
        return None
    if engine == "webwork":
        valid = (
            promotion.adapter_image_digest is None
            and promotion.native_grader == "MathObjects::Formula::cmp"
        )
    elif engine == "imathas":
        valid = (
            promotion.adapter_image_digest is not None
            and _IMAGE_DIGEST.fullmatch(promotion.adapter_image_digest) is not None
            and promotion.native_grader == "native_symbolic_equivalence_v0"
        )
    else:
        valid = False
    return promotion if valid else None


def formula_adapter_promotion_identity(
    promotion: QualifiedFormulaAdapterPromotion,
    *,
    compiler_version: str,
    family: str,
    operation: str,
) -> dict[str, str | None]:
    """Build the immutable identity persisted with one formula execution."""

    resolved = qualified_formula_adapter(
        promotion.engine,
        compiler_version,
        family=family,
        operation=operation,
    )
    if resolved is not promotion:
        raise ParameterizedCompileError(
            "formula adapter promotion is not the exact current qualified entry"
        )
    return {
        "engine": promotion.engine,
        "compiler_version": compiler_version,
        "family": family,
        "operation": operation,
        "qualification_compiler_version": (promotion.qualification_compiler_version),
        "qualification_manifest_sha256": (promotion.qualification_manifest_sha256),
        "qualification_report_sha256": promotion.qualification_report_sha256,
        "engine_image_digest": promotion.engine_image_digest,
        "adapter_image_digest": promotion.adapter_image_digest,
        "native_grader": promotion.native_grader,
        "promotion_approval_sha256": promotion.promotion_approval_sha256,
    }


def formula_adapter_promotion_sha256(
    promotion: QualifiedFormulaAdapterPromotion,
    *,
    compiler_version: str,
    family: str,
    operation: str,
) -> str:
    """Hash one exact formula promotion binding for policy race detection."""

    identity = formula_adapter_promotion_identity(
        promotion,
        compiler_version=compiler_version,
        family=family,
        operation=operation,
    )
    return hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def formula_adapter_registry_sha256() -> str:
    """Hash all source-controlled formula promotions for generation identity."""

    registry = [
        {
            "key": list(key),
            "promotion": {
                "engine": promotion.engine,
                "production_compiler_versions": sorted(
                    promotion.production_compiler_versions
                ),
                "families": sorted(promotion.families),
                "operations": sorted(promotion.operations),
                "qualification_compiler_version": (
                    promotion.qualification_compiler_version
                ),
                "qualification_manifest_sha256": (
                    promotion.qualification_manifest_sha256
                ),
                "qualification_report_sha256": (promotion.qualification_report_sha256),
                "engine_image_digest": promotion.engine_image_digest,
                "adapter_image_digest": promotion.adapter_image_digest,
                "native_grader": promotion.native_grader,
                "promotion_approval_sha256": (promotion.promotion_approval_sha256),
            },
        }
        for key, promotion in sorted(QUALIFIED_FORMULA_ADAPTERS.items())
    ]
    return hashlib.sha256(
        json.dumps(
            registry,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


class ParameterizedCompileError(ValueError):
    pass


@dataclass(frozen=True)
class SeedPreview:
    seed: int
    variables: dict[str, float | int]
    prompt: str
    answer: float | str
    explanation: str


@dataclass(frozen=True)
class CompiledParameterizedItem:
    engine: str
    source: str
    source_sha256: str
    compiler_version: str
    previews: tuple[SeedPreview, ...]


@dataclass(frozen=True)
class CompiledAlgebraQualificationItem:
    """Qualification-only native algebra artifact; never a publication payload."""

    engine: str
    operation: str
    answer_kind: str
    source: str
    source_sha256: str
    compiler_version: str
    response_symbols: tuple[str, ...]
    correct_submission: str
    alternate_correct_submission: str | None
    wrong_submission: str
    production_spec: ParameterizedItemSpec | None
    qualification_only: bool = True


def compile_typed_algebra_qualification_item(
    blueprint: AssessmentComputationBlueprint,
    result: ComputationResult,
    *,
    engine: str,
) -> CompiledAlgebraQualificationItem:
    """Build one fixed native-grader plan without enabling learner delivery.

    This entry point exists only for offline native qualification. Production
    generation continues to fail closed for symbolic external delivery until
    fresh native receipts are promoted separately.
    """

    if engine not in {"webwork", "imathas"}:
        raise ParameterizedCompileError("algebra qualification engine is unsupported")
    if blueprint.profile.family != ComputationFamily.ALGEBRAIC:
        raise ParameterizedCompileError(
            "algebra qualification requires an algebraic typed blueprint"
        )
    if result.blueprint_hash != canonical_blueprint_hash(blueprint):
        raise ParameterizedCompileError(
            "algebra qualification result does not match the typed blueprint"
        )

    operation = blueprint.operation
    response_symbols: tuple[str, ...] = ()
    alternate: str | None = None
    if operation == ComputationOperation.SUBSTITUTE:
        answer = result.answer_expression
        if answer is None:
            raise ParameterizedCompileError(
                "algebraic substitution qualification requires one typed answer"
            )
        _validate_typed_adapter_node(answer)
        symbols = _typed_symbols(answer)
        if len(symbols) > 1:
            raise ParameterizedCompileError(
                "formula qualification requires exactly one response symbol"
            )
        response_symbols = tuple(sorted(symbols))
        answer_kind = "formula" if response_symbols else "numeric"
        correct = _render_typed_expression(
            answer,
            prefix="",
            power="**",
            unprefixed_names=set(response_symbols),
        )
        if answer_kind == "formula":
            # A syntactically distinct but exactly equivalent typed form proves
            # that the native grader is checking symbolic equivalence rather
            # than accepting only the rendered answer string.
            alternate_node = ExpressionNode(
                kind=ExpressionKind.ADD,
                args=[
                    answer,
                    ExpressionNode(kind=ExpressionKind.INTEGER, integer=0),
                ],
            )
            alternate = _render_typed_expression(
                alternate_node,
                prefix="",
                power="**",
                unprefixed_names=set(response_symbols),
            )
        wrong = f"({correct} + 1)"
        solutions: tuple[ExpressionNode, ...] = ()
    elif operation in {
        ComputationOperation.EXPAND,
        ComputationOperation.FACTOR,
        ComputationOperation.EQUIVALENT,
    }:
        answer = result.answer_expression
        if answer is None:
            raise ParameterizedCompileError(
                "symbolic algebra qualification requires a typed answer"
            )
        _validate_typed_adapter_node(answer)
        symbols = _typed_symbols(answer)
        if len(symbols) != 1:
            raise ParameterizedCompileError(
                "formula qualification requires exactly one response symbol"
            )
        response_symbols = tuple(sorted(symbols))
        answer_kind = "formula"
        correct = _render_typed_expression(
            answer,
            prefix="",
            power="**",
            unprefixed_names=set(response_symbols),
        )
        alternate_node = (
            blueprint.comparison_expression
            if operation == ComputationOperation.EQUIVALENT
            else blueprint.expression
        )
        assert alternate_node is not None
        _validate_typed_adapter_node(alternate_node)
        alternate = _render_typed_expression(
            alternate_node,
            prefix="",
            power="**",
            unprefixed_names=set(response_symbols),
        )
        wrong = f"({correct} + 1)"
        solutions = ()
    elif operation == ComputationOperation.SOLVE:
        if (
            blueprint.solve_for is None
            or not 1 <= len(result.solution_expressions) <= 2
        ):
            raise ParameterizedCompileError(
                "solve qualification requires one or two bounded real solutions"
            )
        solutions = tuple(result.solution_expressions)
        for solution in solutions:
            _validate_typed_adapter_node(solution)
            if _typed_symbols(solution):
                raise ParameterizedCompileError(
                    "solve qualification requires closed solution values"
                )
        response_symbols = (blueprint.solve_for,)
        answer_kind = "solution_set"
        rendered_solutions = tuple(
            _render_typed_expression(solution, prefix="", power="**")
            for solution in solutions
        )
        correct = "{" + ",".join(rendered_solutions) + "}"
        alternate = (
            "{" + ",".join(reversed(rendered_solutions)) + "}"
            if len(rendered_solutions) == 2
            else "{(" + rendered_solutions[0] + ")}"
        )
        wrong = (
            "{" + rendered_solutions[0] + "}"
            if len(rendered_solutions) == 2
            else ("{" + rendered_solutions[0] + ",(" + rendered_solutions[0] + " + 1)}")
        )
        answer = None
    else:
        raise ParameterizedCompileError(
            "operation has no algebra native qualification template"
        )

    production_spec: ParameterizedItemSpec | None = None
    if answer_kind in {"numeric", "formula"}:
        assert answer is not None
        production_spec = _algebra_qualification_production_spec(
            engine=engine,
            answer_kind=answer_kind,
            answer=answer,
            response_symbols=response_symbols,
        )
        production_compiled = compile_typed_parameterized_item(
            production_spec,
            answer_expression=answer,
            constraints=(),
            validation_seeds=1,
            validation_seed_values=(1,),
            _qualification_token=_ALGEBRA_QUALIFICATION_FORMULA_TOKEN,
        )
        source = production_compiled.source
        source_sha256 = production_compiled.source_sha256
        compiler_version = production_compiled.compiler_version
    else:
        source = (
            _compile_algebra_qualification_webwork(
                answer_kind=answer_kind,
                answer=answer,
                solutions=solutions,
                response_symbols=response_symbols,
            )
            if engine == "webwork"
            else _compile_algebra_qualification_imathas(
                operation=operation.value,
                answer_kind=answer_kind,
                answer=answer,
                solutions=solutions,
                response_symbols=response_symbols,
            )
        )
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        compiler_version = ALGEBRA_QUALIFICATION_COMPILER_VERSION
    return CompiledAlgebraQualificationItem(
        engine=engine,
        operation=operation.value,
        answer_kind=answer_kind,
        source=source,
        source_sha256=source_sha256,
        compiler_version=compiler_version,
        response_symbols=response_symbols,
        correct_submission=correct,
        alternate_correct_submission=alternate,
        wrong_submission=wrong,
        production_spec=production_spec,
    )


def _algebra_qualification_production_spec(
    *,
    engine: str,
    answer_kind: str,
    answer: ExpressionNode,
    response_symbols: Sequence[str],
) -> ParameterizedItemSpec:
    """Build fixed data consumed by the exact production typed compiler."""

    formula = answer_kind == "formula"
    rendered_answer = _render_typed_expression(
        answer,
        prefix="",
        power="**",
        unprefixed_names=set(response_symbols),
    )
    return ParameterizedItemSpec(
        engine=engine,
        variables=(
            []
            if formula
            else [
                ParameterVariable(
                    name="qualification_sample",
                    minimum=0,
                    maximum=1,
                    step=1,
                    integer=True,
                )
            ]
        ),
        prompt_template=(
            "Enter an algebraically equivalent expression."
            if formula
            else "Enter the computed numeric value."
        ),
        answer_expression=rendered_answer,
        answer_kind=answer_kind,
        compiler_profile="assessment_computation_v0",
        response_symbols=list(response_symbols),
        explanation_template=(
            "Use the typed expression and native symbolic-equivalence grader."
            if formula
            else "Use the typed expression and native numeric grader."
        ),
        constraints=[],
        tolerance=0,
        units=None,
        seed_policy="per_student",
    )


def compile_parameterized_item(
    spec: ParameterizedItemSpec,
    *,
    validation_seeds: int = 25,
    validation_seed_values: Sequence[int] | None = None,
) -> CompiledParameterizedItem:
    bounded = spec.compiler_profile == "assessment_computation_v0"
    if not 1 <= validation_seeds <= 100:
        raise ParameterizedCompileError("validation_seeds must be between 1 and 100")
    if validation_seed_values is None:
        resolved_seeds = tuple(range(1, validation_seeds + 1))
    else:
        resolved_seeds = tuple(validation_seed_values)
        if len(resolved_seeds) != validation_seeds:
            raise ParameterizedCompileError(
                "validation_seed_values must match validation_seeds"
            )
        if any(
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= 0x7FFFFFFF
            for seed in resolved_seeds
        ):
            raise ParameterizedCompileError(
                "validation seed values must be 31-bit nonnegative integers"
            )
    names = [variable.name for variable in spec.variables]
    if len(names) != len(set(names)):
        raise ParameterizedCompileError("parameter variable names must be unique")
    if bounded and set(names) & _COMPUTATION_ENGINE_VARIABLES:
        raise ParameterizedCompileError(
            "computation parameter conflicts with a reserved engine variable"
        )
    for variable in spec.variables:
        if bounded and not all(
            math.isfinite(value)
            for value in (variable.minimum, variable.maximum, variable.step)
        ):
            raise ParameterizedCompileError(
                "parameter ranges require finite numeric values"
            )
    if spec.answer_kind not in {"numeric", "formula"}:
        raise ParameterizedCompileError("answer_kind must be numeric or formula")
    if len(spec.response_symbols) > 4 or any(
        not isinstance(symbol, str) or _SAFE_NAME.fullmatch(symbol) is None
        for symbol in spec.response_symbols
    ):
        raise ParameterizedCompileError("response symbol is not a safe identifier")
    response_symbols = set(spec.response_symbols)
    if len(response_symbols) != len(spec.response_symbols):
        raise ParameterizedCompileError("response symbols must be unique")
    if response_symbols & set(names):
        raise ParameterizedCompileError(
            "response symbols must be disjoint from parameter variables"
        )
    if response_symbols & _RESERVED_RESPONSE_SYMBOLS:
        raise ParameterizedCompileError(
            "formula response symbol conflicts with a reserved math name"
        )
    if spec.answer_kind == "numeric" and response_symbols:
        raise ParameterizedCompileError(
            "numeric answers cannot declare response symbols"
        )
    if spec.answer_kind == "numeric" and not spec.variables:
        raise ParameterizedCompileError(
            "numeric parameterized answers require at least one variable"
        )
    if spec.answer_kind == "formula" and not response_symbols:
        raise ParameterizedCompileError(
            "formula answers require at least one response symbol"
        )
    if spec.answer_kind == "formula" and not formula_adapter_is_qualified(
        spec.engine,
        FORMULA_COMPILER_VERSION,
    ):
        engine_label = "IMathAS" if spec.engine == "imathas" else "WeBWorK"
        raise ParameterizedCompileError(
            f"{engine_label} formula answers are unsupported until the exact native "
            "symbolic-equivalence adapter and compiler are promoted"
        )
    _validate_template(spec.prompt_template, names)
    _validate_template(spec.explanation_template, names)
    answer_names = names + spec.response_symbols
    parsed_answer = _parse_expression(
        spec.answer_expression,
        answer_names,
        bounded=bounded,
    )
    if spec.answer_kind == "formula":
        referenced_names = {
            node.id for node in ast.walk(parsed_answer) if isinstance(node, ast.Name)
        }
        missing = response_symbols - referenced_names
        if missing:
            raise ParameterizedCompileError(
                "formula answer does not reference response symbol(s): "
                + ", ".join(sorted(missing))
            )
    contains_modulo = any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
        for node in ast.walk(parsed_answer)
    )
    if bounded and contains_modulo:
        raise ParameterizedCompileError(
            "assessment computation engine adapters do not support modulo"
        )
    for constraint in spec.constraints:
        _parse_expression(
            constraint,
            names,
            allow_comparison=True,
            bounded=bounded,
        )
    previews = tuple(_preview_for_seed(spec, seed) for seed in resolved_seeds)
    source = (
        _compile_webwork(spec) if spec.engine == "webwork" else _compile_imathas(spec)
    )
    return CompiledParameterizedItem(
        engine=spec.engine,
        source=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        compiler_version=_compiler_version(spec),
        previews=previews,
    )


def compile_typed_parameterized_item(
    spec: ParameterizedItemSpec,
    *,
    answer_expression: ExpressionNode,
    constraints: Sequence[ComparisonConstraint] = (),
    validation_seeds: int = 25,
    validation_seed_values: Sequence[int] | None = None,
    _qualification_token: object | None = None,
) -> CompiledParameterizedItem:
    """Compile computation-owned typed nodes without parsing expression strings.

    The private token is an object-identity capability used only by
    ``compile_typed_algebra_qualification_item`` to render the exact production
    formula bytes before a promotion exists. It cannot arrive through any JSON,
    request, draft, or persisted specification.
    """

    if spec.compiler_profile != "assessment_computation_v0":
        raise ParameterizedCompileError(
            "typed computation compilation requires its fixed compiler profile"
        )
    resolved_seeds = _resolve_validation_seeds(
        validation_seeds,
        validation_seed_values,
    )
    names = [variable.name for variable in spec.variables]
    if len(names) != len(set(names)):
        raise ParameterizedCompileError("parameter variable names must be unique")
    if set(names) & _COMPUTATION_ENGINE_VARIABLES:
        raise ParameterizedCompileError(
            "computation parameter conflicts with a reserved engine variable"
        )
    for variable in spec.variables:
        if not all(
            math.isfinite(value)
            for value in (variable.minimum, variable.maximum, variable.step)
        ):
            raise ParameterizedCompileError(
                "parameter ranges require finite numeric values"
            )
    response_symbols = set(spec.response_symbols)
    if (
        len(response_symbols) != len(spec.response_symbols)
        or len(response_symbols) > 4
        or any(_SAFE_NAME.fullmatch(symbol) is None for symbol in response_symbols)
    ):
        raise ParameterizedCompileError("response symbol is not a safe identifier")
    if response_symbols & set(names) or response_symbols & _RESERVED_RESPONSE_SYMBOLS:
        raise ParameterizedCompileError(
            "formula response symbols conflict with parameters or reserved names"
        )
    if spec.answer_kind == "numeric" and response_symbols:
        raise ParameterizedCompileError(
            "numeric answers cannot declare response symbols"
        )
    if spec.answer_kind == "numeric" and not spec.variables:
        raise ParameterizedCompileError(
            "numeric parameterized answers require at least one variable"
        )
    if spec.answer_kind == "formula" and not response_symbols:
        raise ParameterizedCompileError(
            "formula answers require at least one response symbol"
        )
    if (
        spec.answer_kind == "formula"
        and _qualification_token is not _ALGEBRA_QUALIFICATION_FORMULA_TOKEN
        and not formula_adapter_is_qualified(
            spec.engine,
            TYPED_COMPUTATION_COMPILER_VERSION,
        )
    ):
        engine_label = "IMathAS" if spec.engine == "imathas" else "WeBWorK"
        raise ParameterizedCompileError(
            f"{engine_label} formula answers are unsupported until the exact native "
            "symbolic-equivalence adapter and compiler are promoted"
        )
    _validate_template(spec.prompt_template, names)
    _validate_template(spec.explanation_template, names)
    answer_symbols = _typed_symbols(answer_expression)
    allowed_answer_symbols = set(names) | response_symbols
    unknown_answer_symbols = answer_symbols - allowed_answer_symbols
    if unknown_answer_symbols:
        raise ParameterizedCompileError(
            "unknown variable in typed expression: "
            + ", ".join(sorted(unknown_answer_symbols))
        )
    if spec.answer_kind == "formula":
        missing = response_symbols - answer_symbols
        if missing:
            raise ParameterizedCompileError(
                "formula answer does not reference response symbol(s): "
                + ", ".join(sorted(missing))
            )
    _validate_typed_adapter_node(answer_expression)
    for constraint in constraints:
        symbols = _typed_symbols(constraint.left) | _typed_symbols(constraint.right)
        unknown = symbols - set(names)
        if unknown:
            raise ParameterizedCompileError(
                "unknown variable in typed constraint: " + ", ".join(sorted(unknown))
            )
        _validate_typed_adapter_node(constraint.left)
        _validate_typed_adapter_node(constraint.right)
    previews = tuple(
        _typed_preview_for_seed(
            spec,
            answer_expression,
            constraints,
            seed,
        )
        for seed in resolved_seeds
    )
    source = (
        _compile_typed_webwork(spec, answer_expression, constraints)
        if spec.engine == "webwork"
        else _compile_typed_imathas(spec, answer_expression, constraints)
    )
    return CompiledParameterizedItem(
        engine=spec.engine,
        source=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        previews=previews,
    )


def evaluate_parameterized_answer(
    spec: ParameterizedItemSpec, values: dict[str, float | int]
) -> float | str:
    """Evaluate a compiled item's answer against engine-observed values."""
    _validate_runtime_values(spec, values)
    if spec.answer_kind == "formula":
        return _formula_for_values(spec, values)
    answer = float(_evaluate(spec.answer_expression, values))
    if not (-1e15 < answer < 1e15):
        raise ParameterizedCompileError("generated answer is outside safe bounds")
    return answer


def parameterized_constraints_satisfied(
    spec: ParameterizedItemSpec, values: dict[str, float | int]
) -> bool:
    """Check constraints against engine-observed values, never preview RNG state."""
    _validate_runtime_values(spec, values)
    return all(
        bool(_evaluate(constraint, values, allow_comparison=True))
        for constraint in spec.constraints
    )


def _validate_runtime_values(
    spec: ParameterizedItemSpec, values: dict[str, float | int]
) -> None:
    expected = {variable.name for variable in spec.variables}
    if set(values) != expected:
        raise ParameterizedCompileError(
            "runtime values do not match the parameter specification"
        )
    for variable in spec.variables:
        value = values[variable.name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterizedCompileError("runtime parameter is not numeric")
        if not variable.minimum <= value <= variable.maximum:
            raise ParameterizedCompileError("runtime parameter is outside its range")
        offset = (float(value) - variable.minimum) / variable.step
        if abs(offset - round(offset)) > 1e-8:
            raise ParameterizedCompileError("runtime parameter is off its step grid")
        if variable.integer and float(value) != int(value):
            raise ParameterizedCompileError("runtime integer parameter is fractional")


def _preview_for_seed(spec: ParameterizedItemSpec, seed: int) -> SeedPreview:
    generator = random.Random(seed)
    for _attempt in range(1_000):
        values = {
            variable.name: _sample(variable, generator) for variable in spec.variables
        }
        if all(
            bool(_evaluate(constraint, values, allow_comparison=True))
            for constraint in spec.constraints
        ):
            if spec.answer_kind == "formula":
                answer: float | str = _formula_for_values(spec, values)
            else:
                answer = float(_evaluate(spec.answer_expression, values))
                if not (-1e15 < answer < 1e15):
                    raise ParameterizedCompileError(
                        "generated answer is outside safe bounds"
                    )
            return SeedPreview(
                seed=seed,
                variables=values,
                prompt=_render_template(spec.prompt_template, values),
                answer=answer,
                explanation=_render_template(spec.explanation_template, values),
            )
    raise ParameterizedCompileError(
        f"constraints could not produce a valid parameter set for seed {seed}"
    )


def _sample(variable: ParameterVariable, generator: random.Random) -> float | int:
    count = int((variable.maximum - variable.minimum) // variable.step)
    if count < 1 or count > 1_000_000:
        raise ParameterizedCompileError(
            f"parameter {variable.name} has an unsafe sampling range"
        )
    value = variable.minimum + generator.randint(0, count) * variable.step
    return int(round(value)) if variable.integer else round(value, 10)


def _resolve_validation_seeds(
    validation_seeds: int,
    validation_seed_values: Sequence[int] | None,
) -> tuple[int, ...]:
    if not 1 <= validation_seeds <= 100:
        raise ParameterizedCompileError("validation_seeds must be between 1 and 100")
    if validation_seed_values is None:
        return tuple(range(1, validation_seeds + 1))
    resolved = tuple(validation_seed_values)
    if len(resolved) != validation_seeds:
        raise ParameterizedCompileError(
            "validation_seed_values must match validation_seeds"
        )
    if any(
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed <= 0x7FFFFFFF
        for seed in resolved
    ):
        raise ParameterizedCompileError(
            "validation seed values must be 31-bit nonnegative integers"
        )
    return resolved


def _typed_symbols(node: ExpressionNode) -> set[str]:
    symbols: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.kind == ExpressionKind.SYMBOL:
            assert current.symbol is not None
            symbols.add(current.symbol)
        stack.extend(current.args)
    return symbols


def _validate_typed_adapter_node(node: ExpressionNode) -> None:
    if node.kind == ExpressionKind.CONSTANT:
        raise ParameterizedCompileError(
            "allowlisted symbolic constants are outside the external numeric adapter"
        )
    if node.kind == ExpressionKind.MOD:
        raise ParameterizedCompileError(
            "assessment computation engine adapters do not support modulo"
        )
    if node.kind == ExpressionKind.POW:
        exponent = node.args[1]
        if exponent.kind != ExpressionKind.INTEGER:
            raise ParameterizedCompileError(
                "external-engine power exponents must be typed integer literals"
            )
        assert exponent.integer is not None
        if abs(exponent.integer) > _MAX_EXPONENT_MAGNITUDE:
            raise ParameterizedCompileError(
                "power exponent exceeds the typed adapter magnitude limit"
            )
    for child in node.args:
        _validate_typed_adapter_node(child)


def _typed_numeric_leaf(node: ExpressionNode) -> float | int:
    if node.kind == ExpressionKind.INTEGER:
        assert node.integer is not None
        return node.integer
    if node.kind == ExpressionKind.RATIONAL:
        assert node.numerator is not None and node.denominator is not None
        return node.numerator / node.denominator
    if node.kind == ExpressionKind.DECIMAL:
        assert node.decimal is not None
        return float(node.decimal)
    raise ParameterizedCompileError("typed node is not a numeric literal")


def _evaluate_typed_node(
    node: ExpressionNode,
    values: dict[str, float | int],
) -> float | int:
    if node.kind in {
        ExpressionKind.INTEGER,
        ExpressionKind.RATIONAL,
        ExpressionKind.DECIMAL,
    }:
        return _typed_numeric_leaf(node)
    if node.kind == ExpressionKind.SYMBOL:
        assert node.symbol is not None
        try:
            return values[node.symbol]
        except KeyError:
            raise ParameterizedCompileError(
                f"unknown typed runtime symbol: {node.symbol}"
            ) from None
    if node.kind == ExpressionKind.NEG:
        return -_evaluate_typed_node(node.args[0], values)
    if node.kind == ExpressionKind.ADD:
        return sum(_evaluate_typed_node(child, values) for child in node.args)
    if node.kind == ExpressionKind.MUL:
        result: float | int = 1
        for child in node.args:
            result *= _evaluate_typed_node(child, values)
        return result
    left = _evaluate_typed_node(node.args[0], values)
    right = _evaluate_typed_node(node.args[1], values)
    operations = {
        ExpressionKind.SUB: lambda: left - right,
        ExpressionKind.DIV: lambda: left / right,
        ExpressionKind.POW: lambda: left**right,
    }
    operation = operations.get(node.kind)
    if operation is None:
        raise ParameterizedCompileError("typed adapter reached an unsupported node")
    try:
        result = operation()
    except (ArithmeticError, OverflowError):
        raise ParameterizedCompileError(
            "typed expression is undefined for sampled parameters"
        ) from None
    if isinstance(result, complex) or not math.isfinite(float(result)):
        raise ParameterizedCompileError(
            "typed expression produced a non-finite real result"
        )
    return result


def _typed_constraint_holds(
    constraint: ComparisonConstraint,
    values: dict[str, float | int],
) -> bool:
    left = _evaluate_typed_node(constraint.left, values)
    right = _evaluate_typed_node(constraint.right, values)
    comparisons = {
        "eq": lambda: left == right,
        "ne": lambda: left != right,
        "lt": lambda: left < right,
        "le": lambda: left <= right,
        "gt": lambda: left > right,
        "ge": lambda: left >= right,
    }
    comparison = comparisons.get(constraint.operator.value)
    if comparison is None:
        raise ParameterizedCompileError("typed comparison operator is unsupported")
    return bool(comparison())


def _typed_preview_for_seed(
    spec: ParameterizedItemSpec,
    answer_expression: ExpressionNode,
    constraints: Sequence[ComparisonConstraint],
    seed: int,
) -> SeedPreview:
    generator = random.Random(seed)
    for _attempt in range(1_000):
        values = {
            variable.name: _sample(variable, generator) for variable in spec.variables
        }
        if all(
            _typed_constraint_holds(constraint, values) for constraint in constraints
        ):
            if spec.answer_kind == "formula":
                answer: float | str = _typed_formula_for_values(
                    answer_expression,
                    values,
                    set(spec.response_symbols),
                )
            else:
                answer = float(_evaluate_typed_node(answer_expression, values))
                if not (-1e15 < answer < 1e15):
                    raise ParameterizedCompileError(
                        "generated answer is outside safe bounds"
                    )
            return SeedPreview(
                seed=seed,
                variables=values,
                prompt=_render_template(spec.prompt_template, values),
                answer=answer,
                explanation=_render_template(spec.explanation_template, values),
            )
    raise ParameterizedCompileError(
        f"constraints could not produce a valid parameter set for seed {seed}"
    )


def _typed_literal(node: ExpressionNode) -> str:
    if node.kind == ExpressionKind.INTEGER:
        assert node.integer is not None
        return str(node.integer)
    if node.kind == ExpressionKind.RATIONAL:
        assert node.numerator is not None and node.denominator is not None
        return f"({node.numerator} / {node.denominator})"
    if node.kind == ExpressionKind.DECIMAL:
        assert node.decimal is not None
        return node.decimal
    raise ParameterizedCompileError("typed node is not a renderable literal")


def _render_typed_expression(
    node: ExpressionNode,
    *,
    prefix: str,
    power: str,
    unprefixed_names: set[str] | None = None,
    values: dict[str, float | int] | None = None,
) -> str:
    unprefixed_names = unprefixed_names or set()
    if node.kind in {
        ExpressionKind.INTEGER,
        ExpressionKind.RATIONAL,
        ExpressionKind.DECIMAL,
    }:
        return _typed_literal(node)
    if node.kind == ExpressionKind.SYMBOL:
        assert node.symbol is not None
        if values is not None and node.symbol not in unprefixed_names:
            value = values[node.symbol]
            rendered = str(value)
            return f"({rendered})" if float(value) < 0 else rendered
        return node.symbol if node.symbol in unprefixed_names else prefix + node.symbol
    if node.kind == ExpressionKind.NEG:
        return f"(-{_render_typed_expression(node.args[0], prefix=prefix, power=power, unprefixed_names=unprefixed_names, values=values)})"
    if node.kind in {ExpressionKind.ADD, ExpressionKind.MUL}:
        operator = "+" if node.kind == ExpressionKind.ADD else "*"
        rendered = [
            _render_typed_expression(
                child,
                prefix=prefix,
                power=power,
                unprefixed_names=unprefixed_names,
                values=values,
            )
            for child in node.args
        ]
        return f"({f' {operator} '.join(rendered)})"
    operators = {
        ExpressionKind.SUB: "-",
        ExpressionKind.DIV: "/",
        ExpressionKind.POW: power,
    }
    operator = operators.get(node.kind)
    if operator is None:
        raise ParameterizedCompileError("typed adapter cannot render this node")
    left = _render_typed_expression(
        node.args[0],
        prefix=prefix,
        power=power,
        unprefixed_names=unprefixed_names,
        values=values,
    )
    right = _render_typed_expression(
        node.args[1],
        prefix=prefix,
        power=power,
        unprefixed_names=unprefixed_names,
        values=values,
    )
    return f"({left} {operator} {right})"


def _render_typed_constraint(
    constraint: ComparisonConstraint,
    *,
    prefix: str,
    power: str,
) -> str:
    operators = {
        "eq": "==",
        "ne": "!=",
        "lt": "<",
        "le": "<=",
        "gt": ">",
        "ge": ">=",
    }
    return (
        f"({_render_typed_expression(constraint.left, prefix=prefix, power=power)} "
        f"{operators[constraint.operator.value]} "
        f"{_render_typed_expression(constraint.right, prefix=prefix, power=power)})"
    )


def _typed_formula_for_values(
    expression: ExpressionNode,
    values: dict[str, float | int],
    response_symbols: set[str],
) -> str:
    return _render_typed_expression(
        expression,
        prefix="",
        power="**",
        unprefixed_names=response_symbols,
        values=values,
    )


def typed_parameterized_submission_pair(
    spec: ParameterizedItemSpec,
    answer_expression: ExpressionNode,
    values: Mapping[str, int],
) -> tuple[str, str]:
    """Derive canonical correct/wrong submissions from typed observed values.

    This pure helper is shared by server-side receipt verification and native
    runner implementations.  It parses no expression strings and imports no
    symbolic runtime.
    """

    expected_names = {variable.name for variable in spec.variables}
    if set(values) != expected_names or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in values.values()
    ):
        raise ParameterizedCompileError(
            "native observed values do not match typed parameter names"
        )
    normalized_values: dict[str, float | int] = dict(values)
    _validate_typed_adapter_node(answer_expression)
    if spec.answer_kind == "formula":
        correct = _typed_formula_for_values(
            answer_expression,
            normalized_values,
            set(spec.response_symbols),
        )
        wrong = f"({correct}) + 1"
    else:
        answer = float(_evaluate_typed_node(answer_expression, normalized_values))
        if not math.isfinite(answer) or not (-1e15 < answer < 1e15):
            raise ParameterizedCompileError(
                "native observed answer is outside safe numeric bounds"
            )
        correct = format(answer, ".17g")
        offset = max(
            1.0,
            abs(answer) * 10.0,
            (spec.tolerance * 100.0) + 1.0,
        )
        wrong_value = answer + offset
        if not math.isfinite(wrong_value):
            wrong_value = answer - offset
        if not math.isfinite(wrong_value):
            raise ParameterizedCompileError(
                "native wrong-answer probe is outside safe numeric bounds"
            )
        wrong = format(wrong_value, ".17g")
    if (
        not correct
        or not wrong
        or correct == wrong
        or len(correct) > 2_000
        or len(wrong) > 2_000
    ):
        raise ParameterizedCompileError(
            "native canonical submissions are invalid or exceed bounds"
        )
    return correct, wrong


def typed_formula_submission(
    spec: ParameterizedItemSpec,
    expression: ExpressionNode,
    values: Mapping[str, int],
) -> str:
    """Render one independently typed formula form for native qualification."""

    if spec.answer_kind != "formula":
        raise ParameterizedCompileError("alternate native submissions are formula-only")
    correct, _wrong = typed_parameterized_submission_pair(
        spec,
        expression,
        values,
    )
    return correct


def typed_parameterized_constraints_satisfied(
    constraints: Sequence[ComparisonConstraint],
    values: Mapping[str, int],
) -> bool:
    normalized_values: dict[str, float | int] = dict(values)
    return all(
        _typed_constraint_holds(constraint, normalized_values)
        for constraint in constraints
    )


def _compile_typed_webwork(
    spec: ParameterizedItemSpec,
    answer_expression: ExpressionNode,
    constraints: Sequence[ComparisonConstraint],
) -> str:
    declarations = [
        (
            f"${variable.name} = random({format(variable.minimum, '.17g')},"
            f"{format(variable.maximum, '.17g')},"
            f"{format(variable.step, '.17g')});"
        )
        for variable in spec.variables
    ]
    parameter_block = declarations
    if constraints:
        checks = [
            _render_typed_constraint(constraint, prefix="$", power="**")
            for constraint in constraints
        ]
        parameter_block = [
            "$parameters_valid = 0;",
            "for (1..1000) {",
            *[f"  {declaration}" for declaration in declarations],
            f"  if ({' && '.join(checks)}) {{ $parameters_valid = 1; last; }}",
            "}",
            'die("Unable to generate safe parameters") unless $parameters_valid;',
        ]
    answer = _render_typed_expression(
        answer_expression,
        prefix="$",
        power="**",
        unprefixed_names=set(spec.response_symbols),
    )
    response_context: list[str] = []
    answer_declaration = f"$answer = {answer};"
    answer_evaluator = f"ANS(Real($answer)->cmp(tol=>{spec.tolerance:g}));"
    if spec.answer_kind == "formula":
        context_symbols = sorted(set(spec.response_symbols) - {"x"})
        if context_symbols:
            context_variables = ",".join(
                f'{symbol}=>"Real"' for symbol in context_symbols
            )
            response_context.append(f"Context()->variables->add({context_variables});")
        answer_declaration = f'$answer = Formula("{answer}");'
        answer_evaluator = f"ANS($answer->cmp(tol=>{spec.tolerance:g}));"
    return "\n".join(
        [
            "DOCUMENT();",
            'loadMacros("PGstandard.pl","MathObjects.pl");',
            'Context("Numeric");',
            *response_context,
            *parameter_block,
            answer_declaration,
            f"$assessment_prompt = {_safe_prompt_expression(spec.prompt_template)};",
            "BEGIN_TEXT",
            "\\{ $assessment_prompt \\}",
            "\\{ ans_rule(20) \\}",
            "END_TEXT",
            answer_evaluator,
            "ENDDOCUMENT();",
            "",
        ]
    )


def _compile_typed_imathas(
    spec: ParameterizedItemSpec,
    answer_expression: ExpressionNode,
    constraints: Sequence[ComparisonConstraint],
) -> str:
    payload: dict[str, Any] = {
        "compiler": TYPED_COMPUTATION_COMPILER_VERSION,
        "engine": "imathas",
        "variables": [variable.model_dump(mode="json") for variable in spec.variables],
        "constraints": [
            _render_typed_constraint(constraint, prefix="", power="**")
            for constraint in constraints
        ],
        "prompt_template": spec.prompt_template,
        "answer_expression": _render_typed_expression(
            answer_expression,
            prefix="",
            power="**",
        ),
        "explanation_template": spec.explanation_template,
        "tolerance": spec.tolerance,
        "units": spec.units,
        "seed_policy": spec.seed_policy,
    }
    if spec.answer_kind == "formula":
        payload.update(
            {
                "schema_version": (
                    "assessment-computation-imathas-symbolic-equivalence-v0"
                ),
                "answer_kind": "formula",
                "grader": "native_symbolic_equivalence_v0",
                "response_symbols": list(spec.response_symbols),
            }
        )
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _compile_algebra_qualification_webwork(
    *,
    answer_kind: str,
    answer: ExpressionNode | None,
    solutions: Sequence[ExpressionNode],
    response_symbols: Sequence[str],
) -> str:
    context_variables = [symbol for symbol in sorted(response_symbols) if symbol != "x"]
    context = (
        [
            "Context()->variables->add("
            + ",".join(f'{symbol}=>"Real"' for symbol in context_variables)
            + ");"
        ]
        if context_variables
        else []
    )
    if answer_kind == "numeric":
        assert answer is not None
        rendered = _render_typed_expression(answer, prefix="", power="**")
        declaration = f"$answer = {rendered};"
        evaluator = "ANS(Real($answer)->cmp(tol=>0));"
    elif answer_kind == "formula":
        assert answer is not None
        rendered = _render_typed_expression(
            answer,
            prefix="",
            power="**",
            unprefixed_names=set(response_symbols),
        )
        declaration = f'$answer = Formula("{rendered}");'
        evaluator = "ANS($answer->cmp());"
    elif answer_kind == "solution_set":
        rendered = ",".join(
            _render_typed_expression(solution, prefix="", power="**")
            for solution in solutions
        )
        declaration = f"$answer = Set({rendered});"
        evaluator = "ANS($answer->cmp());"
    else:
        raise ParameterizedCompileError("unknown algebra qualification answer kind")
    return "\n".join(
        [
            "DOCUMENT();",
            'loadMacros("PGstandard.pl","MathObjects.pl");',
            'Context("Numeric");',
            *context,
            declaration,
            "BEGIN_TEXT",
            "\\{ ans_rule(25) \\}",
            "END_TEXT",
            evaluator,
            "ENDDOCUMENT();",
            "",
        ]
    )


def _compile_algebra_qualification_imathas(
    *,
    operation: str,
    answer_kind: str,
    answer: ExpressionNode | None,
    solutions: Sequence[ExpressionNode],
    response_symbols: Sequence[str],
) -> str:
    grader = {
        "numeric": "native_numeric_v0",
        "formula": "native_symbolic_equivalence_v0",
        "solution_set": "native_solution_set_v0",
    }.get(answer_kind)
    if grader is None:
        raise ParameterizedCompileError("unknown algebra qualification answer kind")
    payload = {
        "schema_version": "assessment-computation-imathas-algebra-template-v0",
        "compiler": ALGEBRA_QUALIFICATION_COMPILER_VERSION,
        "engine": "imathas",
        "operation": operation,
        "answer_kind": answer_kind,
        "grader": grader,
        "answer_expression": (
            _render_typed_expression(
                answer,
                prefix="",
                power="**",
                unprefixed_names=set(response_symbols),
            )
            if answer is not None
            else None
        ),
        "solution_expressions": [
            _render_typed_expression(solution, prefix="", power="**")
            for solution in solutions
        ],
        "response_symbols": list(response_symbols),
        "qualification_only": True,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_template(template: str, names: list[str]) -> None:
    placeholders = set(_PLACEHOLDER.findall(template))
    unknown = placeholders - set(names)
    if unknown:
        raise ParameterizedCompileError(
            "template references unknown variable(s): " + ", ".join(sorted(unknown))
        )
    stripped = _PLACEHOLDER.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise ParameterizedCompileError("templates contain malformed placeholders")


def _render_template(template: str, values: dict[str, float | int]) -> str:
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)


def _parse_expression(
    expression: str,
    names: list[str],
    *,
    allow_comparison: bool = False,
    bounded: bool = False,
) -> ast.Expression:
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ParameterizedCompileError("parameter expression is invalid") from None
    if bounded:
        nodes = tuple(ast.walk(parsed))
        if len(nodes) > _MAX_EXPRESSION_NODES:
            raise ParameterizedCompileError(
                f"parameter expression exceeds {_MAX_EXPRESSION_NODES} AST nodes"
            )
        if _ast_depth(parsed.body) > _MAX_EXPRESSION_DEPTH:
            raise ParameterizedCompileError(
                f"parameter expression exceeds depth {_MAX_EXPRESSION_DEPTH}"
            )
    _validate_node(
        parsed.body,
        set(names),
        allow_comparison=allow_comparison,
        bounded=bounded,
    )
    return parsed


def _ast_depth(node: ast.AST) -> int:
    children = tuple(ast.iter_child_nodes(node))
    if not children:
        return 1
    return 1 + max(_ast_depth(child) for child in children)


def _validate_node(
    node: ast.AST,
    names: set[str],
    *,
    allow_comparison: bool,
    bounded: bool,
) -> None:
    if isinstance(node, ast.Constant):
        value = node.value
        too_many_digits = (
            isinstance(value, int) and len(str(abs(value))) > _MAX_NUMERIC_DIGITS
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (bounded and isinstance(value, float) and not math.isfinite(value))
            or (bounded and too_many_digits)
        ):
            raise ParameterizedCompileError("only numeric constants are allowed")
        return
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise ParameterizedCompileError(
                f"unknown variable in expression: {node.id}"
            )
        return
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINARY:
        _validate_node(
            node.left,
            names,
            allow_comparison=allow_comparison,
            bounded=bounded,
        )
        _validate_node(
            node.right,
            names,
            allow_comparison=allow_comparison,
            bounded=bounded,
        )
        if bounded and isinstance(node.op, ast.Pow):
            exponent = _literal_numeric_ast(node.right)
            if exponent is None or abs(exponent) > _MAX_EXPONENT_MAGNITUDE:
                raise ParameterizedCompileError(
                    "power exponent must be a numeric literal with magnitude at most "
                    f"{_MAX_EXPONENT_MAGNITUDE}"
                )
        return
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        _validate_node(
            node.operand,
            names,
            allow_comparison=allow_comparison,
            bounded=bounded,
        )
        return
    if allow_comparison and isinstance(node, ast.Compare) and len(node.ops) == 1:
        if type(node.ops[0]) not in _ALLOWED_COMPARE:
            raise ParameterizedCompileError("comparison operator is not allowed")
        _validate_node(
            node.left,
            names,
            allow_comparison=False,
            bounded=bounded,
        )
        _validate_node(
            node.comparators[0],
            names,
            allow_comparison=False,
            bounded=bounded,
        )
        return
    raise ParameterizedCompileError(
        f"expression construct is not allowed: {type(node).__name__}"
    )


def _literal_numeric_ast(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        value = float(node.operand.value)
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _evaluate(
    expression: str,
    values: dict[str, float | int],
    *,
    allow_comparison: bool = False,
) -> Any:
    parsed = _parse_expression(
        expression, list(values), allow_comparison=allow_comparison
    )
    return _evaluate_node(parsed.body, values)


def _evaluate_node(node: ast.AST, values: dict[str, float | int]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.BinOp):
        return _ALLOWED_BINARY[type(node.op)](
            _evaluate_node(node.left, values),
            _evaluate_node(node.right, values),
        )
    if isinstance(node, ast.UnaryOp):
        return _ALLOWED_UNARY[type(node.op)](_evaluate_node(node.operand, values))
    if isinstance(node, ast.Compare):
        return _ALLOWED_COMPARE[type(node.ops[0])](
            _evaluate_node(node.left, values),
            _evaluate_node(node.comparators[0], values),
        )
    raise ParameterizedCompileError("expression evaluation reached an unsafe node")


def _compile_webwork(spec: ParameterizedItemSpec) -> str:
    preserve_literals = spec.compiler_profile == "assessment_computation_v0"
    declarations = []
    for variable in spec.variables:
        minimum = (
            format(variable.minimum, ".17g")
            if preserve_literals
            else f"{variable.minimum:g}"
        )
        maximum = (
            format(variable.maximum, ".17g")
            if preserve_literals
            else f"{variable.maximum:g}"
        )
        step = (
            format(variable.step, ".17g") if preserve_literals else f"{variable.step:g}"
        )
        declarations.append(f"${variable.name} = random({minimum},{maximum},{step});")
    parameter_block = declarations
    if spec.constraints:
        checks = [
            _expression_for_engine(
                constraint,
                prefix="$",
                power="**",
                allow_comparison=True,
                preserve_literals=preserve_literals,
            )
            for constraint in spec.constraints
        ]
        parameter_block = [
            "$parameters_valid = 0;",
            "for (1..1000) {",
            *[f"  {declaration}" for declaration in declarations],
            f"  if ({' && '.join(checks)}) {{ $parameters_valid = 1; last; }}",
            "}",
            'die("Unable to generate safe parameters") unless $parameters_valid;',
        ]
    answer = _expression_for_engine(
        spec.answer_expression,
        prefix="$",
        power="**",
        unprefixed_names=set(spec.response_symbols),
        preserve_literals=preserve_literals,
    )
    safe_prompt_declaration: list[str] = []
    if spec.compiler_profile == "assessment_computation_v0":
        # Provider-facing prose is treated only as runtime data. Each literal
        # segment is HTML-escaped, whitespace-normalized, and represented by
        # Perl Unicode escapes; only validated numeric placeholders become PG
        # variables. The resulting string is interpolated once and is never
        # reparsed as BEGIN_TEXT/END_TEXT or a PG code block.
        safe_prompt_declaration = [
            f"$assessment_prompt = {_safe_prompt_expression(spec.prompt_template)};"
        ]
        prompt = "\\{ $assessment_prompt \\}"
    else:
        # This is the accepted BUILD-08 source path. Keep it byte-for-byte
        # stable for legacy/off-mode items.
        prompt = _template_for_engine(
            spec.prompt_template, prefix="$", wrapper="\\(", suffix="\\)"
        )
    response_context: list[str] = []
    answer_declaration = f"$answer = {answer};"
    answer_evaluator = f"ANS(Real($answer)->cmp(tol=>{spec.tolerance:g}));"
    if spec.answer_kind == "formula":
        context_symbols = sorted(set(spec.response_symbols) - {"x"})
        if context_symbols:
            context_variables = ",".join(
                f'{symbol}=>"Real"' for symbol in context_symbols
            )
            response_context.append(f"Context()->variables->add({context_variables});")
        # The formula text is rendered exclusively from the validated arithmetic
        # AST. No provider-controlled PG source reaches this quoted string.
        answer_declaration = f'$answer = Formula("{answer}");'
        answer_evaluator = f"ANS($answer->cmp(tol=>{spec.tolerance:g}));"
    return "\n".join(
        [
            "DOCUMENT();",
            'loadMacros("PGstandard.pl","MathObjects.pl");',
            'Context("Numeric");',
            *response_context,
            *parameter_block,
            answer_declaration,
            *safe_prompt_declaration,
            "BEGIN_TEXT",
            prompt,
            "\\{ ans_rule(20) \\}",
            "END_TEXT",
            answer_evaluator,
            "ENDDOCUMENT();",
            "",
        ]
    )


def _compiler_version(spec: ParameterizedItemSpec) -> str:
    if spec.answer_kind == "formula":
        return FORMULA_COMPILER_VERSION
    if spec.compiler_profile == "assessment_computation_v0":
        return COMPUTATION_COMPILER_VERSION
    return COMPILER_VERSION


def _safe_prompt_expression(template: str) -> str:
    """Compile a validated template into a code-free PG string expression."""

    parts: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER.finditer(template):
        parts.append(_escaped_perl_literal(template[cursor : match.start()]))
        parts.append(f"${match.group(1)}")
        cursor = match.end()
    parts.append(_escaped_perl_literal(template[cursor:]))
    return " . ".join(parts)


def _escaped_perl_literal(value: str) -> str:
    normalized = " ".join(value.split())
    escaped = html.escape(normalized, quote=True)
    return '"' + "".join(f"\\x{{{ord(character):X}}}" for character in escaped) + '"'


def _compile_imathas(spec: ParameterizedItemSpec) -> str:
    payload: dict[str, Any] = {
        "compiler": _compiler_version(spec),
        "engine": "imathas",
        "variables": [variable.model_dump(mode="json") for variable in spec.variables],
        "constraints": spec.constraints,
        "prompt_template": spec.prompt_template,
        "answer_expression": spec.answer_expression,
        "explanation_template": spec.explanation_template,
        "tolerance": spec.tolerance,
        "units": spec.units,
        "seed_policy": spec.seed_policy,
    }
    if spec.answer_kind == "formula":
        payload.update(
            {
                "schema_version": (
                    "assessment-computation-imathas-symbolic-equivalence-v0"
                ),
                "answer_kind": "formula",
                "grader": "native_symbolic_equivalence_v0",
                "response_symbols": list(spec.response_symbols),
            }
        )
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _expression_for_engine(
    expression: str,
    *,
    prefix: str,
    power: str,
    allow_comparison: bool = False,
    unprefixed_names: set[str] | None = None,
    preserve_literals: bool = False,
) -> str:
    unprefixed_names = unprefixed_names or set()
    parsed = _parse_expression(
        expression,
        sorted(set(re.findall(r"[a-z][a-z0-9_]*", expression))),
        allow_comparison=allow_comparison,
        bounded=preserve_literals,
    )

    def render(node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            if preserve_literals:
                literal = ast.get_source_segment(expression, node)
                if literal is None:
                    raise ParameterizedCompileError(
                        "cannot preserve typed numeric literal"
                    )
                return literal
            return str(node.value)
        if isinstance(node, ast.Name):
            return node.id if node.id in unprefixed_names else prefix + node.id
        if isinstance(node, ast.UnaryOp):
            operator = "+" if isinstance(node.op, ast.UAdd) else "-"
            return f"({operator}{render(node.operand)})"
        if isinstance(node, ast.BinOp):
            operators = {
                ast.Add: "+",
                ast.Sub: "-",
                ast.Mult: "*",
                ast.Div: "/",
                ast.Pow: power,
                ast.Mod: "%",
            }
            return (
                f"({render(node.left)} {operators[type(node.op)]} {render(node.right)})"
            )
        if isinstance(node, ast.Compare):
            operators = {
                ast.Eq: "==",
                ast.NotEq: "!=",
                ast.Lt: "<",
                ast.LtE: "<=",
                ast.Gt: ">",
                ast.GtE: ">=",
            }
            return (
                f"({render(node.left)} {operators[type(node.ops[0])]} "
                f"{render(node.comparators[0])})"
            )
        raise ParameterizedCompileError("cannot compile expression node")

    return render(parsed.body)


def _formula_for_values(
    spec: ParameterizedItemSpec,
    values: dict[str, float | int],
) -> str:
    parsed = _parse_expression(
        spec.answer_expression,
        [*values, *spec.response_symbols],
        bounded=spec.compiler_profile == "assessment_computation_v0",
    )
    response_symbols = set(spec.response_symbols)

    def render(node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            if spec.compiler_profile == "assessment_computation_v0":
                literal = ast.get_source_segment(spec.answer_expression, node)
                if literal is None:
                    raise ParameterizedCompileError(
                        "cannot preserve typed numeric literal"
                    )
                return literal
            return str(node.value)
        if isinstance(node, ast.Name):
            if node.id in response_symbols:
                return node.id
            value = values[node.id]
            rendered = str(value)
            return f"({rendered})" if float(value) < 0 else rendered
        if isinstance(node, ast.UnaryOp):
            operator = "+" if isinstance(node.op, ast.UAdd) else "-"
            return f"({operator}{render(node.operand)})"
        if isinstance(node, ast.BinOp):
            operators = {
                ast.Add: "+",
                ast.Sub: "-",
                ast.Mult: "*",
                ast.Div: "/",
                ast.Pow: "**",
            }
            operator = operators.get(type(node.op))
            if operator is None:
                raise ParameterizedCompileError("cannot render formula expression node")
            return f"({render(node.left)} {operator} {render(node.right)})"
        raise ParameterizedCompileError("cannot render formula expression node")

    return render(parsed.body)


def _template_for_engine(
    template: str, *, prefix: str, wrapper: str, suffix: str
) -> str:
    return _PLACEHOLDER.sub(
        lambda match: f"{wrapper}{prefix}{match.group(1)}{suffix}", template
    )
