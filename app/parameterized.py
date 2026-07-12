from __future__ import annotations

import ast
import hashlib
import json
import random
import re
from dataclasses import dataclass
from typing import Any

from .schemas import ParameterVariable, ParameterizedItemSpec


COMPILER_VERSION = "parameterized-dsl-v1"
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


class ParameterizedCompileError(ValueError):
    pass


@dataclass(frozen=True)
class SeedPreview:
    seed: int
    variables: dict[str, float | int]
    prompt: str
    answer: float
    explanation: str


@dataclass(frozen=True)
class CompiledParameterizedItem:
    engine: str
    source: str
    source_sha256: str
    compiler_version: str
    previews: tuple[SeedPreview, ...]


def compile_parameterized_item(
    spec: ParameterizedItemSpec,
    *,
    validation_seeds: int = 25,
) -> CompiledParameterizedItem:
    if not 1 <= validation_seeds <= 100:
        raise ParameterizedCompileError("validation_seeds must be between 1 and 100")
    names = [variable.name for variable in spec.variables]
    if len(names) != len(set(names)):
        raise ParameterizedCompileError("parameter variable names must be unique")
    _validate_template(spec.prompt_template, names)
    _validate_template(spec.explanation_template, names)
    _parse_expression(spec.answer_expression, names)
    for constraint in spec.constraints:
        _parse_expression(constraint, names, allow_comparison=True)
    previews = tuple(
        _preview_for_seed(spec, seed) for seed in range(1, validation_seeds + 1)
    )
    source = (
        _compile_webwork(spec)
        if spec.engine == "webwork"
        else _compile_imathas(spec)
    )
    return CompiledParameterizedItem(
        engine=spec.engine,
        source=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        compiler_version=COMPILER_VERSION,
        previews=previews,
    )


def _preview_for_seed(spec: ParameterizedItemSpec, seed: int) -> SeedPreview:
    generator = random.Random(seed)
    for _attempt in range(1_000):
        values = {
            variable.name: _sample(variable, generator)
            for variable in spec.variables
        }
        if all(
            bool(_evaluate(constraint, values, allow_comparison=True))
            for constraint in spec.constraints
        ):
            answer = float(_evaluate(spec.answer_expression, values))
            if not (-1e15 < answer < 1e15):
                raise ParameterizedCompileError("generated answer is outside safe bounds")
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
    expression: str, names: list[str], *, allow_comparison: bool = False
) -> ast.Expression:
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ParameterizedCompileError("parameter expression is invalid") from None
    _validate_node(parsed.body, set(names), allow_comparison=allow_comparison)
    return parsed


def _validate_node(node: ast.AST, names: set[str], *, allow_comparison: bool) -> None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ParameterizedCompileError("only numeric constants are allowed")
        return
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise ParameterizedCompileError(f"unknown variable in expression: {node.id}")
        return
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINARY:
        _validate_node(node.left, names, allow_comparison=allow_comparison)
        _validate_node(node.right, names, allow_comparison=allow_comparison)
        return
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        _validate_node(node.operand, names, allow_comparison=allow_comparison)
        return
    if allow_comparison and isinstance(node, ast.Compare) and len(node.ops) == 1:
        if type(node.ops[0]) not in _ALLOWED_COMPARE:
            raise ParameterizedCompileError("comparison operator is not allowed")
        _validate_node(node.left, names, allow_comparison=False)
        _validate_node(node.comparators[0], names, allow_comparison=False)
        return
    raise ParameterizedCompileError(
        f"expression construct is not allowed: {type(node).__name__}"
    )


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
    declarations = []
    for variable in spec.variables:
        declarations.append(
            f"${variable.name} = random({variable.minimum:g},{variable.maximum:g},{variable.step:g});"
        )
    answer = _expression_for_engine(spec.answer_expression, prefix="$", power="**")
    prompt = _template_for_engine(spec.prompt_template, prefix="$", wrapper="\\(", suffix="\\)")
    return "\n".join(
        [
            "DOCUMENT();",
            'loadMacros("PGstandard.pl","MathObjects.pl");',
            'Context("Numeric");',
            *declarations,
            f"$answer = {answer};",
            "BEGIN_TEXT",
            prompt,
            "\\{ ans_rule(20) \\}",
            "END_TEXT",
            f"ANS(Real($answer)->cmp(tol=>{spec.tolerance:g}));",
            "ENDDOCUMENT();",
            "",
        ]
    )


def _compile_imathas(spec: ParameterizedItemSpec) -> str:
    payload = {
        "compiler": COMPILER_VERSION,
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
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _expression_for_engine(expression: str, *, prefix: str, power: str) -> str:
    parsed = _parse_expression(expression, sorted(set(re.findall(r"[a-z][a-z0-9_]*", expression))))

    def render(node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.Name):
            return prefix + node.id
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
            return f"({render(node.left)} {operators[type(node.op)]} {render(node.right)})"
        raise ParameterizedCompileError("cannot compile expression node")

    return render(parsed.body)


def _template_for_engine(
    template: str, *, prefix: str, wrapper: str, suffix: str
) -> str:
    return _PLACEHOLDER.sub(
        lambda match: f"{wrapper}{prefix}{match.group(1)}{suffix}", template
    )
