"""Typed, deterministic computation primitives for Assessment AI.

This module deliberately accepts a small data language rather than source code.
User/provider supplied strings are never parsed as Python or SymPy expressions.
Every SymPy object is constructed directly from a validated :class:`ExpressionNode`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

# The networked Assessment AI process imports this module for the closed Pydantic
# contracts and deterministic hashes only.  Computation dependencies are bound
# explicitly by ``app.computation_runtime`` inside the networkless sidecar (and
# by the in-process test facade).  Do not import SymPy, Pint, or ucumvert here.
_sympy: Any = None
_pint: Any = None
_ucumvert: Any = None


SCHEMA_VERSION = "assessment-computation-v0"
VALIDATOR_VERSION = "assessment-computation-validator-v0"
QUALIFIED_SYMPY_VERSION = "1.14.0"
QUALIFIED_PINT_VERSION = "0.25.3"
QUALIFIED_UCUMVERT_VERSION = "0.3.2"
UCUM_PROFILE = "libretexts-edu-units-v0"
UCUM_ESSENCE_SHA256 = "6022a1f4a77d93efa23b941ae50055cb9d3fdcb8bb5db6b85deda004467bb380"

MAX_AST_NODES = 128
MAX_AST_DEPTH = 16
MAX_SYMBOLS = 12
MAX_NUMERIC_DIGITS = 100
MAX_EXPONENT_MAGNITUDE = 12
MAX_CONSTRAINTS = 20
DEFAULT_SEED_COUNT = 25
MAX_ABSOLUTE_TOLERANCE = Decimal("1")
MAX_RELATIVE_TOLERANCE = Decimal("0.05")
NATIVE_ADAPT_MAGNITUDE_LIMIT = 1e15

_SYMBOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_UNIT_CODES = frozenset(
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
_COMPOUND_UNIT_CODES = _UNIT_CODES - {"1"}
_UNIT_CODE_ALTERNATION = "|".join(
    re.escape(code)
    for code in sorted(_COMPOUND_UNIT_CODES, key=lambda code: (-len(code), code))
)
_UNIT_ATOM = rf"(?:(?:{_UNIT_CODE_ALTERNATION})(?:-?[0-3])?|1)"
_UNIT_PATTERN = re.compile(rf"^/?{_UNIT_ATOM}(?:[./]{_UNIT_ATOM})*$")
# Exact scale to SI, power of pi, and dimensions ordered as
# mass, length, time, current, temperature, amount, luminous intensity.
_D0 = (0, 0, 0, 0, 0, 0, 0)
_UNIT_EXACT_DEFINITIONS: dict[
    str, tuple[int, int, int, tuple[int, int, int, int, int, int, int]]
] = {
    "1": (1, 1, 0, _D0),
    "%": (1, 100, 0, _D0),
    "m": (1, 1, 0, (0, 1, 0, 0, 0, 0, 0)),
    "cm": (1, 100, 0, (0, 1, 0, 0, 0, 0, 0)),
    "mm": (1, 1_000, 0, (0, 1, 0, 0, 0, 0, 0)),
    "km": (1_000, 1, 0, (0, 1, 0, 0, 0, 0, 0)),
    "s": (1, 1, 0, (0, 0, 1, 0, 0, 0, 0)),
    "ms": (1, 1_000, 0, (0, 0, 1, 0, 0, 0, 0)),
    "min": (60, 1, 0, (0, 0, 1, 0, 0, 0, 0)),
    "h": (3_600, 1, 0, (0, 0, 1, 0, 0, 0, 0)),
    "g": (1, 1_000, 0, (1, 0, 0, 0, 0, 0, 0)),
    "mg": (1, 1_000_000, 0, (1, 0, 0, 0, 0, 0, 0)),
    "kg": (1, 1, 0, (1, 0, 0, 0, 0, 0, 0)),
    "mol": (1, 1, 0, (0, 0, 0, 0, 0, 1, 0)),
    "A": (1, 1, 0, (0, 0, 0, 1, 0, 0, 0)),
    "K": (1, 1, 0, (0, 0, 0, 0, 1, 0, 0)),
    "cd": (1, 1, 0, (0, 0, 0, 0, 0, 0, 1)),
    "rad": (1, 1, 0, _D0),
    "deg": (1, 180, 1, _D0),
    "Hz": (1, 1, 0, (0, 0, -1, 0, 0, 0, 0)),
    "N": (1, 1, 0, (1, 1, -2, 0, 0, 0, 0)),
    "Pa": (1, 1, 0, (1, -1, -2, 0, 0, 0, 0)),
    "kPa": (1_000, 1, 0, (1, -1, -2, 0, 0, 0, 0)),
    "J": (1, 1, 0, (1, 2, -2, 0, 0, 0, 0)),
    "kJ": (1_000, 1, 0, (1, 2, -2, 0, 0, 0, 0)),
    "W": (1, 1, 0, (1, 2, -3, 0, 0, 0, 0)),
    "kW": (1_000, 1, 0, (1, 2, -3, 0, 0, 0, 0)),
    "C": (1, 1, 0, (0, 0, 1, 1, 0, 0, 0)),
    "V": (1, 1, 0, (1, 2, -3, -1, 0, 0, 0)),
    "Ohm": (1, 1, 0, (1, 2, -3, -2, 0, 0, 0)),
    "L": (1, 1_000, 0, (0, 3, 0, 0, 0, 0, 0)),
    "mL": (1, 1_000_000, 0, (0, 3, 0, 0, 0, 0, 0)),
}


class ComputationError(RuntimeError):
    """Base class for fail-closed computation errors."""


class ComputationDependencyError(ComputationError):
    """A pinned runtime dependency is absent, mismatched, or corrupt."""


class ComputationUnsupportedError(ComputationError):
    """The request is valid but outside the qualified v0 profile."""


class ComputationValidationError(ComputationError):
    """The typed request cannot be evaluated safely or consistently."""


class StrictModel(BaseModel):
    # Enum values must remain JSON-friendly, while security-relevant scalar
    # fields use StrictInt/StrictBool explicitly to prevent coercion.
    model_config = ConfigDict(extra="forbid")


class ComputationFamily(StrEnum):
    NUMERIC = "numeric"
    ALGEBRAIC = "algebraic"
    UNIT = "unit"


class ComputationDelivery(StrEnum):
    NUMERICAL = "numerical"
    MULTIPLE_CHOICE = "multiple_choice"
    WEBWORK = "webwork"
    IMATHAS = "imathas"


class ComputationOperation(StrEnum):
    EVALUATE = "evaluate"
    SUBSTITUTE = "substitute"
    EXPAND = "expand"
    FACTOR = "factor"
    EQUIVALENT = "equivalent"
    SOLVE = "solve"
    CONVERT_UNIT = "convert_unit"


class ExpressionKind(StrEnum):
    INTEGER = "integer"
    RATIONAL = "rational"
    DECIMAL = "decimal"
    CONSTANT = "constant"
    SYMBOL = "symbol"
    NEG = "neg"
    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"
    POW = "pow"
    MOD = "mod"


class VariableDomain(StrEnum):
    INTEGER = "integer"
    REAL = "real"


class VariableAssumption(StrEnum):
    POSITIVE = "positive"
    NONNEGATIVE = "nonnegative"
    NEGATIVE = "negative"
    NONPOSITIVE = "nonpositive"
    NONZERO = "nonzero"


class ComparisonOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


class ValidationStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    VALIDATED = "validated"
    PARTIALLY_VALIDATED = "partially_validated"
    UNSUPPORTED = "unsupported"
    VALIDATION_FAILED = "validation_failed"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


class ComputationProfile(StrictModel):
    family: ComputationFamily
    delivery: ComputationDelivery


class ExpressionNode(StrictModel):
    """A closed, recursively typed expression tree.

    Each ``kind`` permits exactly one payload shape. In particular there is no
    generic expression-string field that could reach a parser.
    """

    kind: ExpressionKind
    integer: StrictInt | None = None
    numerator: StrictInt | None = None
    denominator: StrictInt | None = None
    decimal: str | None = None
    constant: Literal["pi", "e"] | None = None
    symbol: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    args: list["ExpressionNode"] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_payload(self) -> "ExpressionNode":
        scalar_fields = {
            "integer": self.integer,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "decimal": self.decimal,
            "constant": self.constant,
            "symbol": self.symbol,
        }
        present = {name for name, value in scalar_fields.items() if value is not None}
        expected: set[str]
        arity = 0
        if self.kind == ExpressionKind.INTEGER:
            expected = {"integer"}
            self._check_integer(self.integer)
        elif self.kind == ExpressionKind.RATIONAL:
            expected = {"numerator", "denominator"}
            self._check_integer(self.numerator)
            self._check_integer(self.denominator)
            if self.denominator == 0:
                raise ValueError("rational denominator must not be zero")
            if self.denominator < 0:
                raise ValueError("rational denominator must be positive")
        elif self.kind == ExpressionKind.DECIMAL:
            expected = {"decimal"}
            _validate_decimal_string(self.decimal, field_name="decimal")
        elif self.kind == ExpressionKind.CONSTANT:
            expected = {"constant"}
        elif self.kind == ExpressionKind.SYMBOL:
            expected = {"symbol"}
        elif self.kind == ExpressionKind.NEG:
            expected, arity = set(), 1
        else:
            expected, arity = set(), 2
        if present != expected:
            raise ValueError(
                f"{self.kind.value} expression requires only "
                f"{', '.join(sorted(expected)) or 'args'}"
            )
        if len(self.args) != arity:
            raise ValueError(
                f"{self.kind.value} expression requires {arity} argument(s)"
            )
        if self.kind == ExpressionKind.POW:
            exponent = self.args[1]
            exponent_value = _literal_rational(exponent)
            if (
                exponent_value is None
                or abs(exponent_value[0]) > MAX_EXPONENT_MAGNITUDE
                or exponent_value[1] > MAX_EXPONENT_MAGNITUDE
            ):
                raise ValueError(
                    "power exponent must be a rational literal whose numerator and "
                    f"denominator are at most {MAX_EXPONENT_MAGNITUDE}"
                )
            if exponent_value[0] == 0:
                raise ValueError("zero exponents are outside assessment-computation-v0")
        return self

    @staticmethod
    def _check_integer(value: int | None) -> None:
        if value is None:
            return
        if len(str(abs(value))) > MAX_NUMERIC_DIGITS:
            raise ValueError(
                f"integer literals may have at most {MAX_NUMERIC_DIGITS} digits"
            )


class VariableSpec(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    domain: VariableDomain = VariableDomain.REAL
    minimum: ExpressionNode | None = None
    maximum: ExpressionNode | None = None
    step: ExpressionNode | None = None
    assumptions: list[VariableAssumption] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_range_and_assumptions(self) -> "VariableSpec":
        range_fields = (self.minimum, self.maximum, self.step)
        if any(value is not None for value in range_fields) and not all(
            value is not None for value in range_fields
        ):
            raise ValueError("minimum, maximum, and step must be provided together")
        if len(self.assumptions) != len(set(self.assumptions)):
            raise ValueError("variable assumptions must be unique")
        incompatible = (
            {VariableAssumption.POSITIVE, VariableAssumption.NONPOSITIVE},
            {VariableAssumption.NEGATIVE, VariableAssumption.NONNEGATIVE},
            {VariableAssumption.POSITIVE, VariableAssumption.NEGATIVE},
            {
                VariableAssumption.NONNEGATIVE,
                VariableAssumption.NONPOSITIVE,
                VariableAssumption.NONZERO,
            },
        )
        if any(pair <= set(self.assumptions) for pair in incompatible):
            raise ValueError("variable assumptions are contradictory")
        if all(value is not None for value in range_fields):
            minimum = _leaf_fraction(self.minimum)
            maximum = _leaf_fraction(self.maximum)
            step = _leaf_fraction(self.step)
            if minimum is None or maximum is None or step is None:
                raise ValueError("variable range values must be numeric literal nodes")
            if maximum <= minimum:
                raise ValueError("variable maximum must be greater than minimum")
            if step <= 0:
                raise ValueError("variable step must be positive")
            count = (maximum - minimum) / step
            if count.denominator != 1:
                raise ValueError("variable range must end on its step grid")
            if count < 2:
                raise ValueError(
                    "variable range requires at least one interior grid value"
                )
            if count > 1_000_000:
                raise ValueError(
                    "variable range has more than 1,000,000 grid intervals"
                )
            if self.domain == VariableDomain.INTEGER and any(
                value.denominator != 1 for value in (minimum, maximum, step)
            ):
                raise ValueError("integer variable ranges require integer literals")
            assumptions = set(self.assumptions)
            if VariableAssumption.POSITIVE in assumptions and minimum <= 0:
                raise ValueError("positive variable ranges must be strictly above zero")
            if VariableAssumption.NONNEGATIVE in assumptions and minimum < 0:
                raise ValueError(
                    "nonnegative variable ranges must start at zero or above"
                )
            if VariableAssumption.NEGATIVE in assumptions and maximum >= 0:
                raise ValueError("negative variable ranges must be strictly below zero")
            if VariableAssumption.NONPOSITIVE in assumptions and maximum > 0:
                raise ValueError(
                    "nonpositive variable ranges must end at zero or below"
                )
            if (
                VariableAssumption.NONZERO in assumptions
                and minimum <= 0 <= maximum
                and (0 - minimum) % step == 0
            ):
                raise ValueError("nonzero variable ranges must not include zero")
        return self


class ComparisonConstraint(StrictModel):
    operator: ComparisonOperator
    left: ExpressionNode
    right: ExpressionNode


class TolerancePolicy(StrictModel):
    absolute: str = "0"
    relative: str = "0"

    @field_validator("absolute", "relative")
    @classmethod
    def validate_tolerance(cls, value: str) -> str:
        number = _validate_decimal_string(value, field_name="tolerance")
        if number < 0:
            raise ValueError("tolerance must be nonnegative")
        return value

    @model_validator(mode="after")
    def validate_qualified_bounds(self) -> "TolerancePolicy":
        if Decimal(self.absolute) > MAX_ABSOLUTE_TOLERANCE:
            raise ValueError(
                f"absolute tolerance exceeds the qualified v0 maximum of "
                f"{MAX_ABSOLUTE_TOLERANCE}"
            )
        if Decimal(self.relative) > MAX_RELATIVE_TOLERANCE:
            raise ValueError(
                f"relative tolerance exceeds the qualified v0 maximum of "
                f"{MAX_RELATIVE_TOLERANCE}"
            )
        return self


class AssessmentComputationBlueprint(StrictModel):
    schema_version: Literal["assessment-computation-v0"] = SCHEMA_VERSION
    profile: ComputationProfile
    source_concept_label: str | None = Field(
        default=None,
        min_length=2,
        max_length=160,
    )
    operation: ComputationOperation
    expression: ExpressionNode
    comparison_expression: ExpressionNode | None = None
    equation_rhs: ExpressionNode | None = None
    solve_for: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    variables: list[VariableSpec] = Field(default_factory=list, max_length=MAX_SYMBOLS)
    substitutions: dict[str, ExpressionNode] = Field(
        default_factory=dict, max_length=MAX_SYMBOLS
    )
    constraints: list[ComparisonConstraint] = Field(
        default_factory=list, max_length=MAX_CONSTRAINTS
    )
    tolerance: TolerancePolicy = Field(default_factory=TolerancePolicy)
    source_unit: str | None = Field(default=None, max_length=100)
    target_unit: str | None = Field(default=None, max_length=100)
    choice_expressions: list[ExpressionNode] = Field(default_factory=list, max_length=4)
    seed_count: Literal[25] = DEFAULT_SEED_COUNT

    @model_validator(mode="after")
    def validate_blueprint(self) -> "AssessmentComputationBlueprint":
        if self.source_concept_label is not None and (
            "://" in self.source_concept_label
            or any(
                character in self.source_concept_label
                for character in ("\n", "\r", "\x00", "<", ">")
            )
        ):
            raise ValueError(
                "source_concept_label must be bounded plain text without URLs"
            )
        names = [variable.name for variable in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("variable names must be unique")
        for key in self.substitutions:
            if not _SYMBOL_PATTERN.fullmatch(key):
                raise ValueError("substitution keys must be valid variable names")
        nodes = [
            self.expression,
            *self.substitutions.values(),
            *self.choice_expressions,
        ]
        for variable in self.variables:
            nodes.extend(
                node
                for node in (variable.minimum, variable.maximum, variable.step)
                if node is not None
            )
        nodes.extend(
            value
            for value in (self.comparison_expression, self.equation_rhs)
            if value is not None
        )
        for constraint in self.constraints:
            nodes.extend((constraint.left, constraint.right))
        total_nodes = 0
        referenced_symbols: set[str] = set()
        for node in nodes:
            count, depth, symbols = _expression_stats(node)
            total_nodes += count
            if depth > MAX_AST_DEPTH:
                raise ValueError(
                    f"expression depth exceeds the limit of {MAX_AST_DEPTH}"
                )
            referenced_symbols.update(symbols)
        if total_nodes > MAX_AST_NODES:
            raise ValueError(
                f"blueprint exceeds the limit of {MAX_AST_NODES} AST nodes"
            )
        declared = set(names)
        if not referenced_symbols <= declared:
            unknown = ", ".join(sorted(referenced_symbols - declared))
            raise ValueError(f"expression references undeclared symbol(s): {unknown}")
        if not set(self.substitutions) <= declared:
            unknown = ", ".join(sorted(set(self.substitutions) - declared))
            raise ValueError(
                f"substitution references undeclared variable(s): {unknown}"
            )
        ranged_names = {
            variable.name for variable in self.variables if variable.minimum is not None
        }
        overlap = ranged_names & set(self.substitutions)
        if overlap:
            raise ValueError(
                "variables cannot be both ranged and fixed: "
                + ", ".join(sorted(overlap))
            )
        if self.constraints and not any(
            variable.minimum is not None for variable in self.variables
        ):
            raise ValueError("constraints require at least one ranged variable")
        ranged_or_fixed = ranged_names | set(self.substitutions)
        for constraint in self.constraints:
            constraint_symbols = (
                _expression_stats(constraint.left)[2]
                | _expression_stats(constraint.right)[2]
            )
            if not constraint_symbols <= ranged_or_fixed:
                raise ValueError(
                    "constraints may reference only ranged or fixed variables"
                )
        if any(
            variable.minimum is not None for variable in self.variables
        ) and self.profile.delivery not in {
            ComputationDelivery.WEBWORK,
            ComputationDelivery.IMATHAS,
        }:
            raise ValueError(
                "ranged variables require webwork or imathas delivery in v0"
            )
        self._validate_operation_shape()
        self._validate_units()
        return self

    def _validate_operation_shape(self) -> None:
        if self.choice_expressions and (
            self.profile.delivery != ComputationDelivery.MULTIPLE_CHOICE
        ):
            raise ValueError(
                "choice_expressions are only valid for multiple_choice delivery"
            )
        if (
            self.profile.delivery == ComputationDelivery.MULTIPLE_CHOICE
            and len(self.choice_expressions) != 4
        ):
            raise ValueError(
                "multiple_choice delivery requires exactly four typed "
                "choice_expressions"
            )
        if self.operation == ComputationOperation.EQUIVALENT:
            if self.comparison_expression is None:
                raise ValueError("equivalent operation requires comparison_expression")
        elif self.comparison_expression is not None:
            raise ValueError("comparison_expression is only valid for equivalent")
        if self.operation == ComputationOperation.SOLVE:
            if self.equation_rhs is None or self.solve_for is None:
                raise ValueError("solve operation requires equation_rhs and solve_for")
            if self.solve_for not in {variable.name for variable in self.variables}:
                raise ValueError("solve_for must name a declared variable")
        elif self.equation_rhs is not None or self.solve_for is not None:
            raise ValueError("equation_rhs and solve_for are only valid for solve")
        family_operations = {
            ComputationFamily.NUMERIC: {
                ComputationOperation.EVALUATE,
                ComputationOperation.SUBSTITUTE,
            },
            ComputationFamily.ALGEBRAIC: {
                ComputationOperation.SUBSTITUTE,
                ComputationOperation.EXPAND,
                ComputationOperation.FACTOR,
                ComputationOperation.EQUIVALENT,
                ComputationOperation.SOLVE,
            },
            ComputationFamily.UNIT: {ComputationOperation.CONVERT_UNIT},
        }
        if self.operation not in family_operations[self.profile.family]:
            raise ValueError(
                f"{self.operation.value} is not valid for {self.profile.family.value}"
            )
        if (
            self.operation
            in {
                ComputationOperation.EXPAND,
                ComputationOperation.FACTOR,
                ComputationOperation.EQUIVALENT,
            }
            and self.profile.delivery == ComputationDelivery.NUMERICAL
        ):
            raise ValueError(
                "symbolic algebra requires multiple_choice, webwork, or imathas delivery"
            )

    def _validate_units(self) -> None:
        has_units = self.source_unit is not None or self.target_unit is not None
        if self.operation == ComputationOperation.CONVERT_UNIT:
            if self.source_unit is None or self.target_unit is None:
                raise ValueError("unit conversion requires source_unit and target_unit")
            validate_unit_code(self.source_unit)
            validate_unit_code(self.target_unit)
        elif has_units:
            raise ValueError("units are only valid for convert_unit in v0")


class ComputationChoice(StrictModel):
    choice_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    expression: ExpressionNode
    marked_correct: StrictBool = False


class FormulaAdapterPromotionEvidence(StrictModel):
    """Exact independently reviewed formula-adapter promotion identity."""

    engine: Literal["webwork", "imathas"]
    compiler_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    family: Literal["algebraic"]
    operation: Literal["substitute", "expand", "factor", "equivalent"]
    qualification_compiler_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    qualification_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    native_grader: str = Field(min_length=1, max_length=100)
    promotion_approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> "FormulaAdapterPromotionEvidence":
        if self.engine == "webwork":
            if (
                self.adapter_image_digest is not None
                or self.native_grader != "MathObjects::Formula::cmp"
            ):
                raise ValueError("WeBWorK formula promotion identity is invalid")
        elif (
            self.adapter_image_digest is None
            or self.native_grader != "native_symbolic_equivalence_v0"
        ):
            raise ValueError("IMathAS formula promotion identity is invalid")
        payload = self.model_dump(
            mode="json",
            exclude={"identity_sha256"},
            exclude_none=False,
        )
        expected = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        if expected != self.identity_sha256:
            raise ValueError("formula promotion identity hash does not match payload")
        return self


class NativeEngineEvidence(StrictModel):
    engine: Literal["webwork", "imathas"]
    compiler_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seeds_validated: int = Field(ge=25, le=100)
    passed: StrictBool
    server_verified: StrictBool = False
    answer_kind: Literal["numeric", "formula"] | None = None
    native_grader: str | None = Field(default=None, min_length=1, max_length=100)
    blueprint_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    draft_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    seed_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    seed_receipts_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runner_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,63}$",
    )
    runner_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    runner_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runner_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    qualification_report_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    promotion_approval_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    engine_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    adapter_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    formula_adapter_promotion: FormulaAdapterPromotionEvidence | None = None
    network_attestation_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    correct_answer_accepted: StrictBool | None = None
    wrong_answer_rejected: StrictBool | None = None
    rendered: StrictBool | None = None
    render_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    warnings_count: int | None = Field(default=None, ge=0, le=20)
    errors_count: int | None = Field(default=None, ge=0, le=20)
    outbound_request_count: int | None = Field(default=None, ge=0, le=1_000)


class ComputationValidationRequest(StrictModel):
    blueprint: AssessmentComputationBlueprint
    candidate_expression: ExpressionNode | None = None
    candidate_solutions: list[ExpressionNode] | None = Field(default=None, max_length=4)
    candidate_unit: str | None = Field(default=None, max_length=100)
    candidate_representation: Literal["exact", "native_binary64"] = "exact"
    choices: list[ComputationChoice] = Field(default_factory=list, max_length=12)
    native_engine_evidence: NativeEngineEvidence | None = None
    native_engine_validated: StrictBool = False

    @model_validator(mode="after")
    def validate_request_shape(self) -> "ComputationValidationRequest":
        if self.candidate_unit is not None:
            validate_unit_code(self.candidate_unit)
        if self.candidate_representation == "native_binary64" and (
            self.candidate_expression is None
            or self.blueprint.profile.delivery != ComputationDelivery.NUMERICAL
        ):
            raise ValueError(
                "native_binary64 representation requires a numerical candidate"
            )
        if (
            self.choices
            and self.blueprint.profile.delivery != ComputationDelivery.MULTIPLE_CHOICE
        ):
            raise ValueError("choices are only valid for multiple_choice delivery")
        ids = [choice.choice_id for choice in self.choices]
        if len(ids) != len(set(ids)):
            raise ValueError("choice ids must be unique")
        if self.native_engine_evidence is not None:
            if (
                self.native_engine_evidence.engine
                != self.blueprint.profile.delivery.value
            ):
                raise ValueError("native evidence engine does not match delivery")
        if self.native_engine_validated and self.native_engine_evidence is None:
            raise ValueError(
                "native_engine_validated cannot replace hashed native engine evidence"
            )
        if self.native_engine_validated:
            assert self.native_engine_evidence is not None
            evidence = self.native_engine_evidence
            if evidence.answer_kind == "formula":
                promotion = evidence.formula_adapter_promotion
                if (
                    promotion is None
                    or promotion.engine != evidence.engine
                    or promotion.compiler_version != evidence.compiler_version
                    or promotion.family != self.blueprint.profile.family.value
                    or promotion.operation != self.blueprint.operation.value
                    or promotion.native_grader != evidence.native_grader
                    or promotion.engine_image_digest != evidence.engine_image_digest
                    or promotion.adapter_image_digest != evidence.adapter_image_digest
                ):
                    raise ValueError(
                        "formula native evidence requires its exact adapter promotion"
                    )
            elif evidence.formula_adapter_promotion is not None:
                raise ValueError(
                    "numeric native evidence cannot claim a formula adapter promotion"
                )
            required = (
                evidence.compiler_version,
                evidence.answer_kind,
                evidence.native_grader,
                evidence.blueprint_sha256,
                evidence.draft_sha256,
                evidence.request_sha256,
                evidence.seed_plan_sha256,
                evidence.seed_receipts_sha256,
                evidence.runner_id,
                evidence.runner_version,
                evidence.runner_manifest_sha256,
                evidence.runner_image_digest,
                evidence.qualification_report_sha256,
                evidence.promotion_approval_sha256,
                evidence.engine_image_digest,
                evidence.network_attestation_sha256,
                evidence.correct_answer_accepted,
                evidence.wrong_answer_rejected,
                evidence.rendered,
                evidence.render_sha256,
                evidence.repeat_render_sha256,
                evidence.warnings_count,
                evidence.errors_count,
                evidence.outbound_request_count,
            )
            if any(value is None for value in required):
                raise ValueError(
                    "server-validated native evidence requires a complete receipt"
                )
            expected_seed_hash = hashlib.sha256(
                json.dumps(
                    list(deterministic_seeds(self.blueprint)),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest()
            if (
                not evidence.passed
                or not evidence.server_verified
                or evidence.seeds_validated != 25
                or evidence.blueprint_sha256 != canonical_blueprint_hash(self.blueprint)
                or evidence.seed_plan_sha256 != expected_seed_hash
                or evidence.correct_answer_accepted is not True
                or evidence.wrong_answer_rejected is not True
                or evidence.rendered is not True
                or evidence.render_sha256 != evidence.repeat_render_sha256
                or evidence.warnings_count != 0
                or evidence.errors_count != 0
                or evidence.outbound_request_count != 0
                or (
                    evidence.engine == "webwork"
                    and evidence.adapter_image_digest is not None
                )
                or (
                    evidence.engine == "imathas"
                    and evidence.adapter_image_digest is None
                )
            ):
                raise ValueError(
                    "server-validated native evidence failed its receipt invariants"
                )
        if self.candidate_solutions is not None and (
            self.blueprint.operation != ComputationOperation.SOLVE
        ):
            raise ValueError("candidate_solutions are only valid for solve operations")
        if (
            self.blueprint.operation == ComputationOperation.SOLVE
            and self.candidate_expression is not None
        ):
            raise ValueError("solve answers must use typed candidate_solutions")
        if (
            self.blueprint.operation == ComputationOperation.EQUIVALENT
            and self.candidate_expression is not None
        ):
            raise ValueError(
                "equivalent operation validates its typed comparison_expression directly"
            )
        candidate_nodes = [
            node
            for node in (
                self.candidate_expression,
                *(self.candidate_solutions or []),
                *(choice.expression for choice in self.choices),
            )
            if node is not None
        ]
        total_nodes = 0
        candidate_symbols: set[str] = set()
        for node in candidate_nodes:
            count, depth, symbols = _expression_stats(node)
            total_nodes += count
            if depth > MAX_AST_DEPTH:
                raise ValueError(f"candidate expression depth exceeds {MAX_AST_DEPTH}")
            candidate_symbols.update(symbols)
        if total_nodes > MAX_AST_NODES:
            raise ValueError(
                f"candidate expressions exceed {MAX_AST_NODES} aggregate AST nodes"
            )
        declared = {variable.name for variable in self.blueprint.variables}
        if not candidate_symbols <= declared:
            unknown = ", ".join(sorted(candidate_symbols - declared))
            raise ValueError(f"candidate references undeclared symbol(s): {unknown}")
        return self


class SeedSample(StrictModel):
    kind: Literal["boundary", "seeded"]
    seed: int | None = None
    values: dict[str, str]


class ComputationResult(StrictModel):
    schema_version: Literal["assessment-computation-v0"] = SCHEMA_VERSION
    blueprint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: ComputationOperation
    exact_value: str | None = None
    approximate_value: str | None = None
    numeric_value: str | None = None
    canonical_expression: str | None = None
    answer_expression: ExpressionNode | None = None
    solutions: list[str] = Field(default_factory=list, max_length=4)
    solution_expressions: list[ExpressionNode] = Field(
        default_factory=list, max_length=4
    )
    target_unit: str | None = None
    equivalent: bool | None = None
    correct_choice_index: int | None = Field(default=None, ge=0, le=11)
    seed_samples: list[SeedSample] = Field(default_factory=list, max_length=100)


class ValidationCheck(StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    status: CheckStatus
    message: str = Field(min_length=1, max_length=1_000)
    details: dict[str, Any] = Field(default_factory=dict)


class AssessmentValidationReport(StrictModel):
    schema_version: Literal["assessment-computation-v0"] = SCHEMA_VERSION
    validator_version: Literal["assessment-computation-validator-v0"] = (
        VALIDATOR_VERSION
    )
    status: ValidationStatus
    reason: Literal["legacy_without_blueprint"] | None = None
    blueprint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: list[ValidationCheck] = Field(default_factory=list, max_length=100)
    result: ComputationResult | None = None
    seed_plan: list[int] = Field(default_factory=list, max_length=25)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    dependencies: dict[str, str] = Field(default_factory=dict)


def canonical_blueprint_hash(blueprint: AssessmentComputationBlueprint) -> str:
    payload = blueprint.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_seeds(
    blueprint: AssessmentComputationBlueprint, count: int = DEFAULT_SEED_COUNT
) -> tuple[int, ...]:
    if not 1 <= count <= DEFAULT_SEED_COUNT:
        raise ComputationValidationError("seed count must be between 1 and 25")
    digest = canonical_blueprint_hash(blueprint)
    return tuple(
        int.from_bytes(
            hashlib.sha256(
                f"{VALIDATOR_VERSION}:{digest}:{index}".encode("ascii")
            ).digest()[:4],
            "big",
        )
        & 0x7FFFFFFF
        for index in range(count)
    )


def compile_expression(
    node: ExpressionNode, variables: list[VariableSpec] | tuple[VariableSpec, ...] = ()
) -> Any:
    """Construct a bounded SymPy expression directly from typed nodes."""

    sp = _require_sympy()
    symbol_table = _symbol_table(variables)

    def build(current: ExpressionNode) -> Any:
        if current.kind == ExpressionKind.INTEGER:
            return sp.Integer(current.integer)
        if current.kind == ExpressionKind.RATIONAL:
            return sp.Rational(current.numerator, current.denominator)
        if current.kind == ExpressionKind.DECIMAL:
            decimal_value = Decimal(current.decimal)
            numerator, denominator = decimal_value.as_integer_ratio()
            return sp.Rational(numerator, denominator)
        if current.kind == ExpressionKind.CONSTANT:
            return sp.pi if current.constant == "pi" else sp.E
        if current.kind == ExpressionKind.SYMBOL:
            try:
                return symbol_table[current.symbol]
            except KeyError:
                raise ComputationValidationError(
                    f"undeclared symbol: {current.symbol}"
                ) from None
        values = [build(arg) for arg in current.args]
        if current.kind == ExpressionKind.NEG:
            return sp.Mul(sp.Integer(-1), values[0])
        constructors = {
            ExpressionKind.ADD: sp.Add,
            ExpressionKind.SUB: lambda left, right: sp.Add(
                left, sp.Mul(sp.Integer(-1), right)
            ),
            ExpressionKind.MUL: sp.Mul,
            ExpressionKind.DIV: lambda left, right: sp.Mul(
                left, sp.Pow(right, sp.Integer(-1))
            ),
            ExpressionKind.POW: sp.Pow,
            ExpressionKind.MOD: sp.Mod,
        }
        try:
            return constructors[current.kind](*values)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ComputationValidationError("expression construction failed") from exc

    return build(node)


def compute_blueprint(
    blueprint: AssessmentComputationBlueprint,
) -> ComputationResult:
    """Compute immutable ground truth for a validated blueprint."""

    sp = _require_sympy()
    if blueprint.profile.family == ComputationFamily.ALGEBRAIC:
        _validate_algebraic_typed_profile(blueprint)
    _validate_structural_domains(blueprint)
    if blueprint.profile.family == ComputationFamily.ALGEBRAIC:
        # Qualify the provider-owned algebra before fixed substitutions can
        # collapse a degree-five or non-polynomial expression to a safe-looking
        # scalar. Fixed and ranged variables remain typed coefficient parameters
        # during this pass.
        _prequalify_algebraic_assertions(blueprint)
    blueprint_hash = canonical_blueprint_hash(blueprint)
    variables = blueprint.variables
    expression = compile_expression(blueprint.expression, variables)
    substitutions = _compile_substitutions(blueprint)
    expression = expression.subs(substitutions)
    operation = blueprint.operation
    if blueprint.profile.family == ComputationFamily.ALGEBRAIC:
        # Re-check the instantiated assertions so fixed values cannot introduce
        # non-rational coefficients into the qualified polynomial profile.
        _prequalify_algebraic_assertions(
            blueprint,
            substitutions=substitutions,
        )
    seed_samples = _parameter_samples(blueprint)
    _validate_sample_evaluations(blueprint, expression, seed_samples)

    if operation in {
        ComputationOperation.EVALUATE,
        ComputationOperation.SUBSTITUTE,
    }:
        value = expression
        if value.free_symbols:
            if blueprint.profile.delivery == ComputationDelivery.NUMERICAL:
                raise ComputationUnsupportedError(
                    "numerical delivery requires a fully resolved scalar answer"
                )
            return _finalize_result(
                blueprint,
                ComputationResult(
                    blueprint_hash=blueprint_hash,
                    operation=operation,
                    canonical_expression=_canonical(value),
                    answer_expression=_expression_node_from_sympy(value),
                    seed_samples=seed_samples,
                ),
            )
        _require_closed_finite_real(value)
        return _finalize_result(
            blueprint,
            _result_from_expression(
                blueprint_hash, operation, value, seed_samples=seed_samples
            ),
        )

    if operation in {ComputationOperation.EXPAND, ComputationOperation.FACTOR}:
        symbol = _single_polynomial_symbol(expression)
        polynomial = _qualified_polynomial(expression, symbol)
        value = (
            sp.expand(polynomial.as_expr())
            if operation == ComputationOperation.EXPAND
            else sp.factor(polynomial.as_expr())
        )
        return _finalize_result(
            blueprint,
            _result_from_expression(
                blueprint_hash, operation, value, seed_samples=seed_samples
            ),
        )

    if operation == ComputationOperation.EQUIVALENT:
        other = compile_expression(blueprint.comparison_expression, variables).subs(
            substitutions
        )
        equivalent = _expressions_equivalent(expression, other, blueprint, seed_samples)
        return _finalize_result(
            blueprint,
            ComputationResult(
                blueprint_hash=blueprint_hash,
                operation=operation,
                canonical_expression=_canonical(expression),
                answer_expression=_expression_node_from_sympy(expression),
                equivalent=equivalent,
                seed_samples=seed_samples,
            ),
        )

    if operation == ComputationOperation.SOLVE:
        rhs = compile_expression(blueprint.equation_rhs, variables).subs(substitutions)
        symbol = _symbol_table(variables)[blueprint.solve_for]
        polynomial = _qualified_polynomial(expression - rhs, symbol)
        solved_variable = next(
            variable
            for variable in blueprint.variables
            if variable.name == blueprint.solve_for
        )
        solutions = _filter_solutions_for_variable(
            _solve_linear_or_quadratic(polynomial), solved_variable
        )
        if (
            blueprint.profile.delivery == ComputationDelivery.NUMERICAL
            and len(solutions) != 1
        ):
            raise ComputationUnsupportedError(
                "numerical solve delivery requires exactly one real solution"
            )
        numeric_solution = solutions[0] if len(solutions) == 1 else None
        return _finalize_result(
            blueprint,
            ComputationResult(
                blueprint_hash=blueprint_hash,
                operation=operation,
                exact_value=(
                    _canonical(numeric_solution)
                    if numeric_solution is not None
                    and blueprint.profile.delivery == ComputationDelivery.NUMERICAL
                    else None
                ),
                approximate_value=(
                    _decimal_approx(numeric_solution)
                    if numeric_solution is not None
                    and blueprint.profile.delivery == ComputationDelivery.NUMERICAL
                    else None
                ),
                numeric_value=(
                    _decimal_approx(numeric_solution)
                    if numeric_solution is not None
                    and blueprint.profile.delivery == ComputationDelivery.NUMERICAL
                    else None
                ),
                canonical_expression=f"{_canonical(expression)} = {_canonical(rhs)}",
                answer_expression=(
                    _expression_node_from_sympy(numeric_solution)
                    if numeric_solution is not None
                    else None
                ),
                solutions=[_canonical(solution) for solution in solutions],
                solution_expressions=[
                    _expression_node_from_sympy(solution) for solution in solutions
                ],
                seed_samples=seed_samples,
            ),
        )

    if operation == ComputationOperation.CONVERT_UNIT:
        value = expression
        if value.free_symbols:
            converted_expression = _converted_symbolic_expression(
                value, blueprint.source_unit, blueprint.target_unit
            )
            _validate_sample_evaluations(blueprint, converted_expression, seed_samples)
            return _finalize_result(
                blueprint,
                ComputationResult(
                    blueprint_hash=blueprint_hash,
                    operation=operation,
                    canonical_expression=_canonical(converted_expression),
                    answer_expression=_expression_node_from_sympy(converted_expression),
                    target_unit=blueprint.target_unit,
                    seed_samples=seed_samples,
                ),
            )
        _require_closed_finite_real(value)
        converted = _convert_unit(value, blueprint.source_unit, blueprint.target_unit)
        converted_expression = _converted_symbolic_expression(
            value, blueprint.source_unit, blueprint.target_unit
        )
        return _finalize_result(
            blueprint,
            ComputationResult(
                blueprint_hash=blueprint_hash,
                operation=operation,
                exact_value=_canonical(converted_expression),
                approximate_value=converted,
                numeric_value=converted,
                canonical_expression=_canonical(converted_expression),
                answer_expression=_expression_node_from_sympy(converted_expression),
                target_unit=blueprint.target_unit,
                seed_samples=seed_samples,
            ),
        )

    raise ComputationUnsupportedError(f"unsupported operation: {operation.value}")


def validate_computation(
    request: ComputationValidationRequest,
) -> AssessmentValidationReport:
    """Return deterministic validation evidence; never silently downgrade failures."""

    blueprint = request.blueprint
    digest = canonical_blueprint_hash(blueprint)
    seeds = list(deterministic_seeds(blueprint))
    checks: list[ValidationCheck] = []
    limitations = [
        "Validation covers structured computational assertions only.",
        "Source alignment, wording, accessibility, and pedagogy require human review.",
    ]
    failure_code = "computation"
    try:
        result = compute_blueprint(blueprint)
        checks.append(_passed("computation", "Typed computation completed safely."))
        failure_code = "structural_singularities"
        singularities = _validate_structural_domains(blueprint)
        if singularities:
            checks.append(
                _passed(
                    "structural_singularities",
                    "Every on-grid denominator root is explicitly excluded by "
                    "structured constraints.",
                    candidates=singularities,
                )
            )
        failure_code = "candidate_domain"
        _validate_candidate_structural_domains(request)
        failure_code = "candidate_profile"
        _validate_algebraic_candidate_profile(request)
        failure_code = "tolerance"
        _record_tolerance_check(blueprint, result, checks)
        failure_code = "deterministic_samples"
        _record_sample_checks(blueprint, result.seed_samples, checks)
        if blueprint.profile.family == ComputationFamily.UNIT:
            failure_code = "unit_validation"
            _record_unit_checks(blueprint, checks)
        failure_code = "candidate"
        _validate_candidate(request, result, checks)
        failure_code = "choices"
        _validate_choices(request, result, checks)
        failure_code = "native_engine"
        _validate_native_evidence(request, checks)
        status = (
            ValidationStatus.PARTIALLY_VALIDATED
            if any(check.status == CheckStatus.INCONCLUSIVE for check in checks)
            else ValidationStatus.VALIDATED
        )
    except ComputationUnsupportedError as exc:
        result = None
        status = ValidationStatus.UNSUPPORTED
        checks.append(_inconclusive("supported_profile", str(exc)))
    except (ComputationDependencyError, ComputationValidationError) as exc:
        result = None
        status = ValidationStatus.VALIDATION_FAILED
        checks = [check for check in checks if check.code != failure_code]
        checks.append(_failed(failure_code, str(exc)))
    except Exception:
        # Do not return implementation details or turn an unexpected failure into
        # partial evidence.
        result = None
        status = ValidationStatus.VALIDATION_FAILED
        checks = [check for check in checks if check.code != failure_code]
        checks.append(_failed(failure_code, "Computation failed closed."))
    return AssessmentValidationReport(
        status=status,
        blueprint_hash=digest,
        checks=checks,
        result=result,
        seed_plan=seeds,
        limitations=limitations,
        dependencies=_dependency_versions(),
    )


def validate_unit_code(code: str) -> str:
    """Validate the qualified UCUM subset before invoking ucumvert/Pint."""

    if not _UNIT_PATTERN.fullmatch(code):
        raise ValueError(f"unit code is outside the qualified {UCUM_PROFILE} subset")
    try:
        term = code[1:] if code.startswith("/") else code
        for atom in re.split(r"[./]", term):
            _split_unit_atom(atom)
    except ComputationValidationError as exc:
        raise ValueError(
            f"unit code is outside the qualified {UCUM_PROFILE} subset"
        ) from exc
    return code


def _validate_candidate(
    request: ComputationValidationRequest,
    result: ComputationResult,
    checks: list[ValidationCheck],
) -> None:
    if request.blueprint.operation == ComputationOperation.EQUIVALENT:
        if result.equivalent is not True:
            raise ComputationValidationError(
                "comparison expression is not equivalent to ground truth"
            )
        checks.append(
            _passed(
                "equivalence",
                "Comparison expression is algebraically equivalent to ground truth.",
            )
        )
        return
    if request.blueprint.operation == ComputationOperation.SOLVE:
        _validate_solution_candidates(request, result, checks)
        return
    if request.blueprint.profile.delivery == ComputationDelivery.MULTIPLE_CHOICE:
        if request.candidate_expression is not None:
            raise ComputationValidationError(
                "multiple-choice answers must use typed choices"
            )
        return
    candidate = request.candidate_expression
    if candidate is None:
        checks.append(
            _inconclusive("candidate", "No final candidate answer was supplied.")
        )
        return
    candidate_expression = compile_expression(candidate, request.blueprint.variables)
    native_binary64 = request.candidate_representation == "native_binary64"
    if native_binary64:
        if (
            candidate.kind != ExpressionKind.DECIMAL
            or candidate_expression.free_symbols
            or result.numeric_value is None
        ):
            raise ComputationValidationError(
                "native binary64 candidate is not a closed decimal magnitude"
            )
        try:
            expected_float = float(result.numeric_value)
        except (TypeError, ValueError, OverflowError):
            raise ComputationValidationError(
                "computed result cannot be represented as native binary64"
            ) from None
        if not math.isfinite(expected_float) or Decimal(candidate.decimal) != Decimal(
            str(expected_float)
        ):
            raise ComputationValidationError(
                "native binary64 candidate is not the deterministic serialization "
                "of ground truth"
            )
        equivalent = True
    elif candidate_expression.free_symbols:
        if result.canonical_expression is None:
            raise ComputationValidationError(
                "candidate expression cannot be compared to this result"
            )
        expected = _expected_symbolic_expression(request.blueprint, result)
        equivalent = _expressions_equivalent(
            candidate_expression,
            expected,
            request.blueprint,
            result.seed_samples,
        )
    elif result.numeric_value is not None:
        _require_closed_finite_real(candidate_expression)
        expected = _expected_symbolic_expression(request.blueprint, result)
        sp = _require_sympy()
        equivalent = sp.simplify(candidate_expression - expected) == 0
        if not equivalent and _tolerance_is_nonzero(request.blueprint.tolerance):
            equivalent = _within_tolerance(
                candidate_expression,
                result.numeric_value,
                request.blueprint.tolerance,
            )
    elif result.equivalent is not None:
        equivalent = bool(candidate_expression) == result.equivalent
    else:
        expected = _expected_symbolic_expression(request.blueprint, result)
        equivalent = _expressions_equivalent(
            candidate_expression,
            expected,
            request.blueprint,
            result.seed_samples,
        )
    if not equivalent:
        raise ComputationValidationError(
            "candidate answer is not equivalent to ground truth"
        )
    if (
        request.blueprint.profile.family == ComputationFamily.UNIT
        and request.candidate_unit != request.blueprint.target_unit
    ):
        raise ComputationValidationError(
            "candidate target unit does not match blueprint"
        )
    checks.append(
        _passed(
            "candidate",
            (
                "Candidate is the deterministic native binary64 serialization "
                "of computed ground truth."
                if native_binary64
                else "Candidate answer matches computed ground truth."
            ),
            representation=request.candidate_representation,
        )
    )


def _validate_solution_candidates(
    request: ComputationValidationRequest,
    result: ComputationResult,
    checks: list[ValidationCheck],
) -> None:
    candidates = request.candidate_solutions
    if candidates is None:
        checks.append(
            _inconclusive(
                "candidate_solutions", "No candidate solution set was supplied."
            )
        )
        return
    blueprint = request.blueprint
    sp = _require_sympy()
    symbols = _symbol_table(blueprint.variables)
    solved_symbol = symbols[blueprint.solve_for]
    fixed = _compile_substitutions(blueprint)
    left = compile_expression(blueprint.expression, blueprint.variables).subs(fixed)
    right = compile_expression(blueprint.equation_rhs, blueprint.variables).subs(fixed)
    expected_polynomial = _qualified_polynomial(left - right, solved_symbol)
    solved_variable = next(
        variable
        for variable in blueprint.variables
        if variable.name == blueprint.solve_for
    )
    expected = _filter_solutions_for_variable(
        _solve_linear_or_quadratic(expected_polynomial), solved_variable
    )
    compiled: list[Any] = []
    for candidate in candidates:
        value = compile_expression(candidate, blueprint.variables).subs(fixed)
        _require_closed_finite_real(value)
        if not _value_conforms_variable(solved_variable, value):
            raise ComputationValidationError(
                "candidate solution violates the solve variable domain"
            )
        residual = sp.simplify((left - right).subs({solved_symbol: value}))
        if residual != 0:
            raise ComputationValidationError(
                "candidate solution does not satisfy the equation"
            )
        if any(sp.simplify(value - existing) == 0 for existing in compiled):
            raise ComputationValidationError(
                "candidate solution set contains duplicates"
            )
        compiled.append(value)
    if len(compiled) != len(expected) or any(
        not any(sp.simplify(candidate - answer) == 0 for candidate in compiled)
        for answer in expected
    ):
        raise ComputationValidationError(
            "candidate solution set is incomplete or contains an extraneous solution"
        )
    checks.append(
        _passed(
            "candidate_solutions",
            "Candidate solutions satisfy the equation and form the complete real solution set.",
            solution_count=len(expected),
        )
    )


def _validate_choices(
    request: ComputationValidationRequest,
    result: ComputationResult,
    checks: list[ValidationCheck],
) -> None:
    choices = request.choices
    if request.blueprint.profile.delivery != ComputationDelivery.MULTIPLE_CHOICE:
        if choices:
            raise ComputationValidationError("choices supplied for non-choice delivery")
        return
    if not choices:
        checks.append(
            _inconclusive("choices", "No final multiple-choice claims were supplied.")
        )
        return
    blueprint_choices = request.blueprint.choice_expressions
    if len(choices) != len(blueprint_choices):
        raise ComputationValidationError(
            "final choices do not match the typed blueprint choice count"
        )
    expected_ids = [chr(ord("A") + index) for index in range(len(choices))]
    if [choice.choice_id for choice in choices] != expected_ids:
        raise ComputationValidationError(
            "final choice ids do not match the typed blueprint order"
        )
    if any(
        choice.expression != blueprint_choices[index]
        for index, choice in enumerate(choices)
    ):
        raise ComputationValidationError(
            "final choice expressions do not match the typed blueprint order"
        )
    expected = _expected_symbolic_expression(request.blueprint, result)
    equivalent_indices: list[int] = []
    compiled: list[Any] = []
    for index, choice in enumerate(choices):
        value = compile_expression(choice.expression, request.blueprint.variables)
        compiled.append(value)
        if _expressions_equivalent(
            value, expected, request.blueprint, result.seed_samples
        ):
            equivalent_indices.append(index)
    if len(equivalent_indices) != 1:
        raise ComputationValidationError(
            "choices must contain exactly one answer equivalent to ground truth"
        )
    for left in range(len(compiled)):
        for right in range(left + 1, len(compiled)):
            if _expressions_equivalent(
                compiled[left],
                compiled[right],
                request.blueprint,
                result.seed_samples,
            ):
                raise ComputationValidationError(
                    "choice distractors contain equivalents"
                )
    marked = [index for index, choice in enumerate(choices) if choice.marked_correct]
    if marked != equivalent_indices:
        raise ComputationValidationError(
            "marked correct choice does not match ground truth"
        )
    result.correct_choice_index = equivalent_indices[0]
    checks.append(
        _passed(
            "choices",
            "Exactly one choice is correct and no distractors are equivalent.",
            correct_choice_index=equivalent_indices[0],
        )
    )


def _validate_native_evidence(
    request: ComputationValidationRequest, checks: list[ValidationCheck]
) -> CheckStatus:
    if request.blueprint.profile.delivery not in {
        ComputationDelivery.WEBWORK,
        ComputationDelivery.IMATHAS,
    }:
        return CheckStatus.SKIPPED
    evidence = request.native_engine_evidence
    if evidence is None:
        checks.append(
            _inconclusive(
                "native_engine",
                "Native engine validation evidence has not been attached.",
            )
        )
        return CheckStatus.INCONCLUSIVE
    if evidence is not None and not evidence.passed:
        raise ComputationValidationError("native engine validation did not pass")
    if request.native_engine_validated:
        checks.append(
            ValidationCheck(
                code="native_engine",
                status=CheckStatus.PASSED,
                message=(
                    "A server-verified native engine receipt passed all 25 "
                    "deterministic seeds."
                ),
                details=evidence.model_dump(mode="json", exclude_none=True),
            )
        )
        return CheckStatus.PASSED
    checks.append(
        ValidationCheck(
            code="native_engine",
            status=CheckStatus.INCONCLUSIVE,
            message=(
                "Native engine receipt was supplied but has not been bound by the "
                "server-trusted engine-validation record."
            ),
            details={
                "engine": evidence.engine,
                "source_sha256": evidence.source_sha256,
                "receipt_sha256": evidence.receipt_sha256,
                "seeds_validated": evidence.seeds_validated,
            },
        )
    )
    return CheckStatus.INCONCLUSIVE


def _expected_symbolic_expression(
    blueprint: AssessmentComputationBlueprint, result: ComputationResult
) -> Any:
    sp = _require_sympy()
    expression = compile_expression(blueprint.expression, blueprint.variables).subs(
        _compile_substitutions(blueprint)
    )
    if blueprint.operation in {
        ComputationOperation.EVALUATE,
        ComputationOperation.SUBSTITUTE,
    }:
        return expression
    if blueprint.operation == ComputationOperation.EXPAND:
        return sp.expand(expression)
    if blueprint.operation == ComputationOperation.FACTOR:
        return sp.factor(expression)
    if blueprint.operation == ComputationOperation.SOLVE:
        if len(result.solutions) != 1:
            raise ComputationUnsupportedError(
                "a multiple-choice solve item requires exactly one real solution"
            )
        rhs = compile_expression(blueprint.equation_rhs, blueprint.variables).subs(
            _compile_substitutions(blueprint)
        )
        symbol = _symbol_table(blueprint.variables)[blueprint.solve_for]
        solved_variable = next(
            variable
            for variable in blueprint.variables
            if variable.name == blueprint.solve_for
        )
        solutions = _filter_solutions_for_variable(
            _solve_linear_or_quadratic(_qualified_polynomial(expression - rhs, symbol)),
            solved_variable,
        )
        if len(solutions) != 1:
            raise ComputationUnsupportedError(
                "a multiple-choice solve item requires exactly one qualified solution"
            )
        return solutions[0]
    if blueprint.operation == ComputationOperation.CONVERT_UNIT:
        return _converted_symbolic_expression(
            expression, blueprint.source_unit, blueprint.target_unit
        )
    if result.numeric_value is not None:
        return sp.Float(result.numeric_value, 100)
    if blueprint.operation == ComputationOperation.EQUIVALENT:
        return expression
    raise ComputationValidationError("operation has no single candidate expression")


def _expressions_equivalent(
    left: Any,
    right: Any,
    blueprint: AssessmentComputationBlueprint,
    samples: list[SeedSample] | None = None,
) -> bool:
    fixed = _compile_substitutions(blueprint)
    left = left.subs(fixed)
    right = right.subs(fixed)
    if any(variable.minimum is not None for variable in blueprint.variables):
        from fractions import Fraction

        effective_samples = samples or _parameter_samples(blueprint)
        if not effective_samples:
            raise ComputationValidationError(
                "parameterized equivalence requires deterministic samples"
            )
        for sample in effective_samples:
            values = {name: Fraction(value) for name, value in sample.values.items()}
            substitutions = _sample_substitutions(blueprint, values)
            instantiated_left = left.subs(substitutions)
            instantiated_right = right.subs(substitutions)
            if not _instantiated_expressions_equivalent(
                instantiated_left, instantiated_right, blueprint.tolerance
            ):
                return False
        sp = _require_sympy()
        try:
            return sp.cancel(left - right) == 0
        except (ArithmeticError, TypeError, ValueError):
            raise ComputationUnsupportedError(
                "parameterized expressions could not be compared exactly"
            ) from None
    return _instantiated_expressions_equivalent(left, right, blueprint.tolerance)


def _instantiated_expressions_equivalent(
    left: Any, right: Any, tolerance: TolerancePolicy
) -> bool:
    if not left.free_symbols and not right.free_symbols:
        _require_closed_finite_real(left)
        _require_closed_finite_real(right)
        if _require_sympy().simplify(left - right) == 0:
            return True
        if not _tolerance_is_nonzero(tolerance):
            return False
        return _within_tolerance(left, _decimal_approx(right), tolerance)
    _qualified_response_polynomial(left, right)
    return _polynomial_equivalent(left, right)


def _polynomial_equivalent(left: Any, right: Any) -> bool:
    symbol = _single_polynomial_symbol(left, right)
    return _qualified_polynomial(left, symbol) == _qualified_polynomial(right, symbol)


def _prequalify_algebraic_assertions(
    blueprint: AssessmentComputationBlueprint,
    *,
    substitutions: dict[Any, Any] | None = None,
) -> None:
    assertion_nodes = [blueprint.expression]
    assertion_nodes.extend(
        node
        for node in (blueprint.comparison_expression, blueprint.equation_rhs)
        if node is not None
    )
    assertions = [
        compile_expression(node, blueprint.variables) for node in assertion_nodes
    ]
    choices = [
        compile_expression(node, blueprint.variables)
        for node in blueprint.choice_expressions
    ]
    ranged_names = {
        variable.name
        for variable in blueprint.variables
        if variable.minimum is not None
    }
    if substitutions is None:
        parameter_names = ranged_names | set(blueprint.substitutions)
        substitution_values = [
            compile_expression(node, blueprint.variables)
            for node in blueprint.substitutions.values()
        ]
        _prequalify_algebraic_expressions(
            substitution_values,
            parameter_names=set(),
            allow_closed_algebraic=False,
            candidate_context=False,
        )
    else:
        parameter_names = ranged_names
        assertions = [assertion.subs(substitutions) for assertion in assertions]
        choices = [choice.subs(substitutions) for choice in choices]
    _prequalify_algebraic_expressions(
        assertions,
        parameter_names=parameter_names,
        allow_closed_algebraic=False,
        candidate_context=False,
    )
    # Answer choices may be exact closed algebraic values (for example a
    # quadratic root), but a symbol-bearing choice must still satisfy the same
    # polynomial profile before substitutions can erase its shape.
    _prequalify_algebraic_expressions(
        choices,
        parameter_names=parameter_names,
        allow_closed_algebraic=True,
        candidate_context=False,
    )


def _prequalify_algebraic_expressions(
    expressions: list[Any],
    *,
    parameter_names: set[str],
    allow_closed_algebraic: bool,
    candidate_context: bool,
) -> None:
    sp = _require_sympy()

    def reject(message: str) -> None:
        if candidate_context:
            raise ComputationValidationError(f"candidate {message}")
        raise ComputationUnsupportedError(message)

    for assertion in expressions:
        if assertion.has(sp.Mod):
            reject("uses modulo outside the algebraic v0 profile")
        free_symbols = set(assertion.free_symbols)
        response_symbols = {
            item for item in free_symbols if item.name not in parameter_names
        }
        if len(response_symbols) > 1:
            reject("algebraic v0 permits at most one learner response symbol")
        if not free_symbols:
            if not allow_closed_algebraic and assertion.is_Rational is not True:
                reject(
                    "is outside the rational-coefficient polynomial algebraic "
                    "v0 profile"
                )
            continue
        try:
            if response_symbols:
                response_symbol = next(iter(response_symbols))
                parameter_symbols = sorted(
                    free_symbols - response_symbols,
                    key=sp.default_sort_key,
                )
                domain = (
                    sp.QQ.poly_ring(*parameter_symbols) if parameter_symbols else sp.QQ
                )
                polynomial = sp.Poly(
                    assertion,
                    response_symbol,
                    domain=domain,
                )
                degree = polynomial.degree()
            else:
                ordered = sorted(free_symbols, key=sp.default_sort_key)
                polynomial = sp.Poly(assertion, *ordered, domain=sp.QQ)
                degree = polynomial.total_degree()
        except (sp.polys.polyerrors.BasePolynomialError, TypeError, ValueError):
            reject("is not a polynomial assertion in the algebraic v0 profile")
        if degree > 4:
            reject("exceeds the algebraic v0 polynomial degree-four limit")


def _qualified_polynomial(expression: Any, symbol: Any) -> Any:
    sp = _require_sympy()
    if expression.has(sp.Mod):
        raise ComputationUnsupportedError(
            "modulo is not supported in algebraic profiles"
        )
    try:
        polynomial = sp.Poly(expression, symbol, domain=sp.QQ)
    except (sp.PolynomialError, TypeError, ValueError):
        raise ComputationUnsupportedError(
            "algebraic v0 requires rational-coefficient univariate polynomials"
        ) from None
    if polynomial.degree() > 4:
        raise ComputationUnsupportedError(
            "algebraic v0 supports polynomials only through degree four"
        )
    return polynomial


def _single_polynomial_symbol(*expressions: Any) -> Any:
    symbols: set[Any] = set()
    for expression in expressions:
        symbols.update(expression.free_symbols)
    if len(symbols) != 1:
        raise ComputationUnsupportedError(
            "algebraic v0 requires exactly one symbolic variable"
        )
    return next(iter(symbols))


def _solve_linear_or_quadratic(polynomial: Any) -> list[Any]:
    sp = _require_sympy()
    degree = polynomial.degree()
    if degree < 1 or degree > 2:
        raise ComputationUnsupportedError(
            "v0 solving supports only real linear and quadratic equations"
        )
    coefficients = polynomial.all_coeffs()
    if degree == 1:
        a, b = coefficients
        return [sp.cancel(-b / a)]
    a, b, c = coefficients
    discriminant = sp.cancel(b**2 - 4 * a * c)
    if discriminant.is_negative:
        return []
    root = sp.sqrt(discriminant)
    solutions = {sp.cancel((-b - root) / (2 * a)), sp.cancel((-b + root) / (2 * a))}
    real_solutions = [value for value in solutions if value.is_real is not False]
    return sorted(real_solutions, key=sp.default_sort_key)


def _filter_solutions_for_variable(
    solutions: list[Any], variable: VariableSpec
) -> list[Any]:
    return [
        solution
        for solution in solutions
        if _value_conforms_variable(variable, solution)
    ]


def _value_conforms_variable(variable: VariableSpec, value: Any) -> bool:
    if value.is_real is not True:
        return False
    if variable.domain == VariableDomain.INTEGER and value.is_integer is not True:
        return False
    predicates = {
        VariableAssumption.POSITIVE: value.is_positive,
        VariableAssumption.NONNEGATIVE: value.is_nonnegative,
        VariableAssumption.NEGATIVE: value.is_negative,
        VariableAssumption.NONPOSITIVE: value.is_nonpositive,
        VariableAssumption.NONZERO: value.is_zero is False,
    }
    if any(predicates[assumption] is not True for assumption in variable.assumptions):
        return False
    if variable.minimum is not None:
        sp = _require_sympy()
        minimum_fraction = _leaf_fraction(variable.minimum)
        maximum_fraction = _leaf_fraction(variable.maximum)
        step_fraction = _leaf_fraction(variable.step)
        minimum = sp.Rational(minimum_fraction.numerator, minimum_fraction.denominator)
        maximum = sp.Rational(maximum_fraction.numerator, maximum_fraction.denominator)
        step = sp.Rational(step_fraction.numerator, step_fraction.denominator)
        if (value < minimum) not in (False, sp.false):
            return False
        if (value > maximum) not in (False, sp.false):
            return False
        offset = (value - minimum) / step
        if offset.is_integer is not True:
            return False
    return True


def _expression_node_from_sympy(value: Any) -> ExpressionNode:
    """Encode a trusted bounded SymPy result back into the closed typed AST."""

    try:
        node = _expression_node_from_sympy_unchecked(value)
        node_count, depth, _symbols = _expression_stats(node)
        if node_count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise ComputationUnsupportedError(
                "computed answer is outside the qualified v0 typed expression bounds"
            )
        return node
    except ComputationUnsupportedError:
        raise
    except (ArithmeticError, TypeError, ValueError) as exc:
        # Pydantic model errors are ValueErrors. A valid typed input whose
        # normalized result exceeds the closed output schema is unsupported,
        # rather than malformed or an execution failure.
        raise ComputationUnsupportedError(
            "computed answer is outside the qualified v0 typed expression bounds"
        ) from exc


def _expression_node_from_sympy_unchecked(value: Any) -> ExpressionNode:
    sp = _require_sympy()
    if isinstance(value, sp.Integer):
        return ExpressionNode(kind=ExpressionKind.INTEGER, integer=int(value))
    if isinstance(value, sp.Rational):
        return ExpressionNode(
            kind=ExpressionKind.RATIONAL,
            numerator=int(value.p),
            denominator=int(value.q),
        )
    if isinstance(value, sp.Float):
        normalized = _decimal_approx(value)
        if "." not in normalized:
            return ExpressionNode(kind=ExpressionKind.INTEGER, integer=int(normalized))
        return ExpressionNode(kind=ExpressionKind.DECIMAL, decimal=normalized)
    if value == sp.pi:
        return ExpressionNode(kind=ExpressionKind.CONSTANT, constant="pi")
    if value == sp.E:
        return ExpressionNode(kind=ExpressionKind.CONSTANT, constant="e")
    if isinstance(value, sp.Symbol):
        return ExpressionNode(kind=ExpressionKind.SYMBOL, symbol=str(value))
    if isinstance(value, sp.Pow):
        return ExpressionNode(
            kind=ExpressionKind.POW,
            args=[
                _expression_node_from_sympy(value.base),
                _expression_node_from_sympy(value.exp),
            ],
        )
    if isinstance(value, sp.Add):
        return _fold_expression_nodes(
            ExpressionKind.ADD,
            [_expression_node_from_sympy(term) for term in value.as_ordered_terms()],
        )
    if isinstance(value, sp.Mul):
        factors = list(value.as_ordered_factors())
        if len(factors) == 2 and factors[0] == -1:
            return ExpressionNode(
                kind=ExpressionKind.NEG,
                args=[_expression_node_from_sympy(factors[1])],
            )
        return _fold_expression_nodes(
            ExpressionKind.MUL,
            [_expression_node_from_sympy(factor) for factor in factors],
        )
    raise ComputationUnsupportedError(
        "computed solution cannot be represented by the v0 typed expression schema"
    )


def _fold_expression_nodes(
    kind: ExpressionKind, nodes: list[ExpressionNode]
) -> ExpressionNode:
    if not nodes:
        raise ComputationValidationError("cannot encode an empty symbolic expression")
    result = nodes[0]
    for node in nodes[1:]:
        result = ExpressionNode(kind=kind, args=[result, node])
    return result


def _result_from_expression(
    blueprint_hash: str,
    operation: ComputationOperation,
    value: Any,
    *,
    seed_samples: list[SeedSample] | None = None,
) -> ComputationResult:
    approximate = None if value.free_symbols else _decimal_approx(value)
    return ComputationResult(
        blueprint_hash=blueprint_hash,
        operation=operation,
        exact_value=_canonical(value),
        approximate_value=approximate,
        numeric_value=approximate,
        canonical_expression=_canonical(value),
        answer_expression=_expression_node_from_sympy(value),
        seed_samples=seed_samples or [],
    )


def _finalize_result(
    blueprint: AssessmentComputationBlueprint, result: ComputationResult
) -> ComputationResult:
    """Bind deterministic choice ground truth before prose generation."""

    _require_native_numerical_representability(blueprint, result)
    if not blueprint.choice_expressions:
        return result
    if blueprint.profile.delivery != ComputationDelivery.MULTIPLE_CHOICE:
        raise ComputationValidationError(
            "choice_expressions require multiple_choice delivery"
        )
    expected = _expected_symbolic_expression(blueprint, result)
    compiled = [
        compile_expression(choice, blueprint.variables).subs(
            _compile_substitutions(blueprint)
        )
        for choice in blueprint.choice_expressions
    ]
    matches = [
        index
        for index, choice in enumerate(compiled)
        if _expressions_equivalent(choice, expected, blueprint, result.seed_samples)
    ]
    if len(matches) != 1:
        raise ComputationValidationError(
            "choice_expressions must contain exactly one ground-truth equivalent"
        )
    for left in range(len(compiled)):
        for right in range(left + 1, len(compiled)):
            if _expressions_equivalent(
                compiled[left], compiled[right], blueprint, result.seed_samples
            ):
                raise ComputationValidationError(
                    "choice_expressions contain equivalent distractors"
                )
    result.correct_choice_index = matches[0]
    return result


def _require_native_numerical_representability(
    blueprint: AssessmentComputationBlueprint,
    result: ComputationResult,
) -> None:
    """Fail before prose generation when ADAPT cannot store the magnitude."""

    if blueprint.profile.delivery != ComputationDelivery.NUMERICAL:
        return
    if result.numeric_value is None:
        raise ComputationUnsupportedError(
            "numerical delivery requires a native ADAPT numeric magnitude"
        )
    try:
        native_value = float(result.numeric_value)
    except (TypeError, ValueError, OverflowError):
        raise ComputationUnsupportedError(
            "numerical answer is outside native ADAPT magnitude bounds"
        ) from None
    if not (
        math.isfinite(native_value)
        and -NATIVE_ADAPT_MAGNITUDE_LIMIT < native_value < NATIVE_ADAPT_MAGNITUDE_LIMIT
    ):
        raise ComputationUnsupportedError(
            "numerical answer is outside native ADAPT magnitude bounds"
        )


def _compile_substitutions(blueprint: AssessmentComputationBlueprint) -> dict[Any, Any]:
    symbols = _symbol_table(blueprint.variables)
    specifications = {variable.name: variable for variable in blueprint.variables}
    compiled: dict[Any, Any] = {}
    for name, node in blueprint.substitutions.items():
        value = compile_expression(node, blueprint.variables)
        _require_closed_finite_real(value)
        _validate_variable_value(specifications[name], value, context="substitution")
        compiled[symbols[name]] = value
    return compiled


def _validate_variable_value(
    variable: VariableSpec, value: Any, *, context: str
) -> None:
    _require_closed_finite_real(value)
    if variable.domain == VariableDomain.INTEGER and value.is_integer is not True:
        raise ComputationValidationError(
            f"{context} for integer variable {variable.name} is not an integer"
        )
    predicates = {
        VariableAssumption.POSITIVE: value.is_positive,
        VariableAssumption.NONNEGATIVE: value.is_nonnegative,
        VariableAssumption.NEGATIVE: value.is_negative,
        VariableAssumption.NONPOSITIVE: value.is_nonpositive,
        VariableAssumption.NONZERO: value.is_zero is False,
    }
    for assumption in variable.assumptions:
        if predicates[assumption] is not True:
            raise ComputationValidationError(
                f"{context} for variable {variable.name} violates its "
                f"{assumption.value} assumption"
            )


def _symbol_table(
    variables: list[VariableSpec] | tuple[VariableSpec, ...],
) -> dict[str, Any]:
    sp = _require_sympy()
    table: dict[str, Any] = {}
    for variable in variables:
        assumptions: dict[str, bool] = {"real": True}
        if variable.domain == VariableDomain.INTEGER:
            assumptions["integer"] = True
        for assumption in variable.assumptions:
            assumptions[assumption.value] = True
        table[variable.name] = sp.Symbol(variable.name, **assumptions)
    return table


def _validate_structural_domains(
    blueprint: AssessmentComputationBlueprint,
    *,
    nodes_override: list[ExpressionNode] | None = None,
    candidate_context: bool = False,
) -> list[dict[str, str]]:
    """Check typed denominators before SymPy can cancel their singularities."""

    sp = _require_sympy()
    denominators: list[ExpressionNode] = []
    substituted_symbols = set(blueprint.substitutions)

    def visit(node: ExpressionNode) -> None:
        if node.kind in {ExpressionKind.DIV, ExpressionKind.MOD}:
            denominators.append(node.args[1])
        elif node.kind == ExpressionKind.POW:
            exponent = _literal_rational(node.args[1])
            if exponent is not None:
                if exponent[0] < 0:
                    denominators.append(node.args[0])
                base_symbols = _expression_stats(node.args[0])[2]
                if exponent[1] != 1 and base_symbols - substituted_symbols:
                    raise ComputationUnsupportedError(
                        "variable-dependent fractional powers are outside "
                        "assessment-computation-v0"
                    )
        for child in node.args:
            visit(child)

    if nodes_override is None:
        nodes = [blueprint.expression, *blueprint.substitutions.values()]
        nodes.extend(
            node
            for node in (blueprint.comparison_expression, blueprint.equation_rhs)
            if node is not None
        )
        nodes.extend(blueprint.choice_expressions)
        for constraint in blueprint.constraints:
            nodes.extend((constraint.left, constraint.right))
    else:
        nodes = nodes_override
    for node in nodes:
        visit(node)
    if not denominators:
        return []

    fixed = _compile_substitutions(blueprint)
    ranged = {
        variable.name: variable
        for variable in blueprint.variables
        if variable.minimum is not None
    }
    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for denominator_node in denominators:
        denominator = compile_expression(denominator_node, blueprint.variables).subs(
            fixed
        )
        if not denominator.free_symbols:
            _require_closed_finite_real(denominator)
            if denominator == 0:
                raise ComputationValidationError(
                    "typed expression contains a zero denominator"
                )
            continue
        free_names = {symbol.name for symbol in denominator.free_symbols}
        if not free_names <= set(ranged):
            message = "response-dependent denominators are outside the v0 profile"
            if candidate_context:
                raise ComputationValidationError(f"candidate {message}")
            raise ComputationUnsupportedError(message)
        if len(free_names) != 1:
            message = "multivariate parameter denominators are outside the v0 profile"
            if candidate_context:
                raise ComputationValidationError(f"candidate {message}")
            raise ComputationUnsupportedError(message)
        name = next(iter(free_names))
        sym = next(iter(denominator.free_symbols))
        polynomial = _qualified_polynomial(denominator, sym)
        if polynomial.is_zero:
            raise ComputationValidationError(
                "typed expression contains an identically zero denominator"
            )
        roots = sp.polys.polytools.ground_roots(polynomial)
        variable = ranged[name]
        minimum = _leaf_fraction(variable.minimum)
        maximum = _leaf_fraction(variable.maximum)
        step = _leaf_fraction(variable.step)
        for root in roots:
            if not isinstance(root, sp.Rational):
                continue
            from fractions import Fraction

            value = Fraction(int(root.p), int(root.q))
            if not minimum <= value <= maximum or (value - minimum) % step != 0:
                continue
            key = (name, str(value))
            if key in seen:
                continue
            seen.add(key)
            if not _singular_value_excluded(blueprint, name, value):
                raise ComputationValidationError(
                    f"parameter {name} can make a typed denominator zero at {value}"
                )
            evidence.append(
                {
                    "variable": name,
                    "value": str(value),
                    "status": "excluded_by_constraints",
                }
            )
    return evidence


def _validate_candidate_structural_domains(
    request: ComputationValidationRequest,
) -> None:
    nodes = [
        node
        for node in (
            request.candidate_expression,
            *(request.candidate_solutions or []),
            *(choice.expression for choice in request.choices),
        )
        if node is not None
    ]
    if nodes:
        _validate_structural_domains(
            request.blueprint,
            nodes_override=nodes,
            candidate_context=True,
        )


def _validate_algebraic_typed_nodes(
    nodes: list[ExpressionNode],
    *,
    fixed_names: set[str],
    candidate_context: bool,
) -> None:
    def reject(message: str) -> None:
        if candidate_context:
            raise ComputationValidationError(f"candidate {message}")
        raise ComputationUnsupportedError(message)

    def visit(node: ExpressionNode) -> None:
        if node.kind == ExpressionKind.CONSTANT:
            reject("uses a non-rational constant outside the algebraic v0 profile")
        if node.kind == ExpressionKind.MOD:
            reject("uses modulo outside the algebraic v0 profile")
        if node.kind == ExpressionKind.DIV:
            denominator_symbols = _expression_stats(node.args[1])[2] - fixed_names
            if denominator_symbols:
                reject(
                    "contains a variable-dependent denominator outside the "
                    "polynomial algebraic v0 profile"
                )
        if node.kind == ExpressionKind.POW:
            exponent = _literal_rational(node.args[1])
            base_symbols = _expression_stats(node.args[0])[2] - fixed_names
            if exponent is not None and exponent[0] < 0 and base_symbols:
                reject(
                    "contains a negative variable power outside the polynomial "
                    "algebraic v0 profile"
                )
        for child in node.args:
            visit(child)

    for item in nodes:
        visit(item)


def _validate_algebraic_typed_profile(
    blueprint: AssessmentComputationBlueprint,
) -> None:
    nodes = [blueprint.expression, *blueprint.substitutions.values()]
    nodes.extend(
        node
        for node in (blueprint.comparison_expression, blueprint.equation_rhs)
        if node is not None
    )
    nodes.extend(blueprint.choice_expressions)
    for constraint in blueprint.constraints:
        nodes.extend((constraint.left, constraint.right))
    _validate_algebraic_typed_nodes(
        nodes,
        fixed_names=set(blueprint.substitutions),
        candidate_context=False,
    )


def _validate_algebraic_candidate_profile(
    request: ComputationValidationRequest,
) -> None:
    if request.blueprint.profile.family != ComputationFamily.ALGEBRAIC:
        return
    nodes = [
        node
        for node in (
            request.candidate_expression,
            *(request.candidate_solutions or []),
            *(choice.expression for choice in request.choices),
        )
        if node is not None
    ]
    _validate_algebraic_typed_nodes(
        nodes,
        fixed_names=set(request.blueprint.substitutions),
        candidate_context=True,
    )
    parameter_names = {
        variable.name
        for variable in request.blueprint.variables
        if variable.minimum is not None
    } | set(request.blueprint.substitutions)
    _prequalify_algebraic_expressions(
        [compile_expression(node, request.blueprint.variables) for node in nodes],
        parameter_names=parameter_names,
        # Exact closed radicals remain valid quadratic answers. What must not
        # pass is a symbol-bearing non-polynomial or over-degree expression
        # whose shape disappears only after fixed substitutions are applied.
        allow_closed_algebraic=True,
        candidate_context=True,
    )


def _singular_value_excluded(
    blueprint: AssessmentComputationBlueprint, variable_name: str, value: Any
) -> bool:
    sp = _require_sympy()
    symbols = _symbol_table(blueprint.variables)
    substitutions = _compile_substitutions(blueprint)
    substitutions[symbols[variable_name]] = sp.Rational(
        value.numerator, value.denominator
    )
    operators = {
        ComparisonOperator.EQ: lambda left, right: left == right,
        ComparisonOperator.NE: lambda left, right: left != right,
        ComparisonOperator.LT: lambda left, right: left < right,
        ComparisonOperator.LE: lambda left, right: left <= right,
        ComparisonOperator.GT: lambda left, right: left > right,
        ComparisonOperator.GE: lambda left, right: left >= right,
    }
    for constraint in blueprint.constraints:
        symbols_in_constraint = (
            _expression_stats(constraint.left)[2]
            | _expression_stats(constraint.right)[2]
        ) - set(blueprint.substitutions)
        if not symbols_in_constraint <= {variable_name}:
            continue
        left = compile_expression(constraint.left, blueprint.variables).subs(
            substitutions
        )
        right = compile_expression(constraint.right, blueprint.variables).subs(
            substitutions
        )
        verdict = operators[constraint.operator](left, right)
        if verdict in (False, sp.false):
            return True
    return False


def _parameter_samples(blueprint: AssessmentComputationBlueprint) -> list[SeedSample]:
    ranged = [
        variable for variable in blueprint.variables if variable.minimum is not None
    ]
    if not ranged:
        return []

    def grid(variable: VariableSpec) -> tuple[Any, Any, Any, int]:
        minimum = _leaf_fraction(variable.minimum)
        maximum = _leaf_fraction(variable.maximum)
        step = _leaf_fraction(variable.step)
        return minimum, maximum, step, int((maximum - minimum) / step)

    # Exercise full endpoints, adjacent-to-endpoint values, and zero whenever
    # zero lies on the declared grid. Other variables remain at a deterministic
    # midpoint so a single dimension is stressed at a time.
    midpoint: dict[str, Any] = {}
    for variable in ranged:
        minimum, _maximum, step, count = grid(variable)
        midpoint[variable.name] = minimum + (count // 2) * step
    boundary_candidates: list[dict[str, Any]] = [
        {variable.name: grid(variable)[0] for variable in ranged},
        {variable.name: grid(variable)[1] for variable in ranged},
    ]
    for variable in ranged:
        minimum, maximum, step, count = grid(variable)
        stressed = {minimum, minimum + step, maximum - step, maximum}
        if minimum <= 0 <= maximum and (0 - minimum) % step == 0:
            stressed.add(type(minimum)(0))
        for value in sorted(stressed):
            candidate = dict(midpoint)
            candidate[variable.name] = value
            boundary_candidates.append(candidate)

    boundary_samples: list[SeedSample] = []
    seen_boundaries: set[str] = set()
    for values in boundary_candidates:
        key = _sample_key(values)
        if key in seen_boundaries or not _constraints_hold(blueprint, values):
            continue
        seen_boundaries.add(key)
        boundary_samples.append(
            SeedSample(kind="boundary", values=_sample_values(values))
        )
    if not boundary_samples:
        raise ComputationValidationError(
            "constraints exclude every deterministic boundary sample"
        )

    seeded_samples: list[SeedSample] = []
    for seed in deterministic_seeds(blueprint):
        for attempt in range(1_000):
            values = {}
            for variable in ranged:
                minimum, _maximum, step, count = grid(variable)
                # VariableSpec guarantees at least one interior grid point.
                values[variable.name] = (
                    minimum
                    + (
                        1
                        + _deterministic_grid_index(
                            seed, attempt, variable.name, count - 1
                        )
                    )
                    * step
                )
            if _constraints_hold(blueprint, values):
                seeded_samples.append(
                    SeedSample(
                        kind="seeded",
                        seed=seed,
                        values=_sample_values(values),
                    )
                )
                break
        else:
            raise ComputationValidationError(
                f"constraints could not produce a valid sample for seed {seed}"
            )
    if len(seeded_samples) != DEFAULT_SEED_COUNT:
        raise ComputationValidationError(
            "deterministic sampling did not produce exactly 25 valid seeded samples"
        )
    return [*boundary_samples, *seeded_samples]


def _deterministic_grid_index(
    seed: int, attempt: int, variable_name: str, size: int
) -> int:
    if size < 1:
        raise ComputationValidationError("parameter grid has no interior value")
    digest = hashlib.sha256(
        f"{VALIDATOR_VERSION}:{seed}:{attempt}:{variable_name}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big") % size


def _sample_values(values: dict[str, Any]) -> dict[str, str]:
    return {name: str(value) for name, value in sorted(values.items())}


def _sample_key(values: dict[str, Any]) -> str:
    return json.dumps(_sample_values(values), sort_keys=True, separators=(",", ":"))


def _sample_substitutions(
    blueprint: AssessmentComputationBlueprint, values: dict[str, Any]
) -> dict[Any, Any]:
    sp = _require_sympy()
    symbols = _symbol_table(blueprint.variables)
    specifications = {variable.name: variable for variable in blueprint.variables}
    substitutions = _compile_substitutions(blueprint)
    for name, value in values.items():
        compiled_value = sp.Rational(value.numerator, value.denominator)
        _validate_variable_value(specifications[name], compiled_value, context="sample")
        substitutions[symbols[name]] = compiled_value
    return substitutions


def _constraints_hold(
    blueprint: AssessmentComputationBlueprint, values: dict[str, Any]
) -> bool:
    if not blueprint.constraints:
        return True
    sp = _require_sympy()
    substitutions = _sample_substitutions(blueprint, values)
    operators = {
        ComparisonOperator.EQ: lambda left, right: left == right,
        ComparisonOperator.NE: lambda left, right: left != right,
        ComparisonOperator.LT: lambda left, right: left < right,
        ComparisonOperator.LE: lambda left, right: left <= right,
        ComparisonOperator.GT: lambda left, right: left > right,
        ComparisonOperator.GE: lambda left, right: left >= right,
    }
    for constraint in blueprint.constraints:
        left = compile_expression(constraint.left, blueprint.variables).subs(
            substitutions
        )
        right = compile_expression(constraint.right, blueprint.variables).subs(
            substitutions
        )
        verdict = operators[constraint.operator](left, right)
        if verdict not in (True, sp.true):
            return False
    return True


def _validate_sample_evaluations(
    blueprint: AssessmentComputationBlueprint,
    expression: Any,
    samples: list[SeedSample],
) -> None:
    if not samples:
        return
    from fractions import Fraction

    for sample in samples:
        values = {name: Fraction(value) for name, value in sample.values.items()}
        if not _constraints_hold(blueprint, values):
            raise ComputationValidationError(
                "a recorded deterministic sample violates its constraints"
            )
        evaluated = expression.subs(_sample_substitutions(blueprint, values))
        if evaluated.free_symbols:
            _qualified_response_polynomial(evaluated)
        else:
            _require_closed_finite_real(evaluated)


def _qualified_response_polynomial(*expressions: Any) -> None:
    symbols: set[Any] = set()
    for expression in expressions:
        symbols.update(expression.free_symbols)
    if len(symbols) > 1:
        raise ComputationUnsupportedError(
            "each parameter instantiation must leave at most one response symbol"
        )
    if len(symbols) == 1:
        symbol = next(iter(symbols))
        for expression in expressions:
            _qualified_polynomial(expression, symbol)


def _record_tolerance_check(
    blueprint: AssessmentComputationBlueprint,
    result: ComputationResult,
    checks: list[ValidationCheck],
) -> None:
    absolute = Decimal(blueprint.tolerance.absolute)
    relative = Decimal(blueprint.tolerance.relative)
    targets: list[Decimal] = []
    symbolic_response = False
    if result.numeric_value is not None:
        targets.append(Decimal(result.numeric_value))
    elif result.answer_expression is not None:
        expression = compile_expression(result.answer_expression, blueprint.variables)
        if result.seed_samples:
            from fractions import Fraction

            for sample in result.seed_samples:
                values = {
                    name: Fraction(value) for name, value in sample.values.items()
                }
                instantiated = expression.subs(_sample_substitutions(blueprint, values))
                if instantiated.free_symbols:
                    symbolic_response = True
                    continue
                _require_closed_finite_real(instantiated)
                targets.append(Decimal(_decimal_approx(instantiated)))
        elif expression.free_symbols:
            symbolic_response = True
    if symbolic_response and (absolute != 0 or relative != 0):
        raise ComputationValidationError(
            "symbolic responses require zero numeric tolerance"
        )
    for target in targets:
        effective = max(absolute, relative * abs(target))
        qualified_limit = MAX_RELATIVE_TOLERANCE * max(abs(target), Decimal("1"))
        if effective > qualified_limit:
            raise ComputationValidationError(
                "effective tolerance exceeds five percent of the qualified answer scale"
            )
    checks.append(
        _passed(
            "tolerance",
            "Tolerance is within the qualified v0 absolute, relative, and answer-scale "
            "bounds.",
            absolute=str(absolute),
            relative=str(relative),
            targets_checked=len(targets),
        )
    )


def _record_sample_checks(
    blueprint: AssessmentComputationBlueprint,
    samples: list[SeedSample],
    checks: list[ValidationCheck],
) -> None:
    ranged = [
        variable for variable in blueprint.variables if variable.minimum is not None
    ]
    if not ranged:
        return
    boundary_count = sum(sample.kind == "boundary" for sample in samples)
    seeded_count = sum(sample.kind == "seeded" for sample in samples)
    if boundary_count < 1 or seeded_count != DEFAULT_SEED_COUNT:
        raise ComputationValidationError(
            "bounded deterministic sampling evidence is incomplete"
        )
    checks.append(
        _passed(
            "deterministic_samples",
            "Endpoints, near-boundaries, zero candidates, and 25 seeded interior samples "
            "were constraint-valid and finite.",
            boundary_count=boundary_count,
            seeded_count=seeded_count,
        )
    )


def _record_unit_checks(
    blueprint: AssessmentComputationBlueprint, checks: list[ValidationCheck]
) -> None:
    registry = _require_units()
    try:
        source = registry.from_ucum(_normalized_ucum_code(blueprint.source_unit))
        target = registry.from_ucum(_normalized_ucum_code(blueprint.target_unit))
        if source.dimensionality != target.dimensionality:
            raise ComputationValidationError(
                "source and target units are dimensionally incompatible"
            )
        forward = _exact_unit_ratio(blueprint.source_unit, blueprint.target_unit)
        reverse = _exact_unit_ratio(blueprint.target_unit, blueprint.source_unit)
        if _require_sympy().cancel(forward * reverse) != 1:
            raise ComputationValidationError(
                "unit conversion failed deterministic round-trip validation"
            )
    except ComputationValidationError:
        raise
    except Exception as exc:
        if _pint is not None and isinstance(exc, _pint.DimensionalityError):
            raise ComputationValidationError(
                "source and target units are dimensionally incompatible"
            ) from None
        raise ComputationValidationError("unit qualification failed closed") from exc
    checks.extend(
        [
            _passed(
                "unit_dimensions",
                "Source and target units have compatible dimensions.",
                source_unit=blueprint.source_unit,
                target_unit=blueprint.target_unit,
            ),
            _passed(
                "unit_round_trip",
                "Forward and reverse conversion passed exact round-trip validation.",
                exact_product="1",
            ),
        ]
    )


def _convert_unit(value: Any, source_code: str, target_code: str) -> str:
    converted = _require_sympy().Mul(value, _exact_unit_ratio(source_code, target_code))
    _require_closed_finite_real(converted)
    return _decimal_approx(converted)


def _converted_symbolic_expression(
    expression: Any, source_code: str, target_code: str
) -> Any:
    return _require_sympy().Mul(_exact_unit_ratio(source_code, target_code), expression)


def _exact_unit_ratio(source_code: str, target_code: str) -> Any:
    registry = _require_units()
    try:
        source_quantity = registry.from_ucum(_normalized_ucum_code(source_code))
        target_quantity = registry.from_ucum(_normalized_ucum_code(target_code))
        if source_quantity.dimensionality != target_quantity.dimensionality:
            raise ComputationValidationError(
                "source and target units are dimensionally incompatible"
            )
    except ComputationValidationError:
        raise
    except Exception as exc:
        if _pint is not None and isinstance(exc, _pint.DimensionalityError):
            raise ComputationValidationError(
                "source and target units are dimensionally incompatible"
            ) from None
        raise ComputationValidationError("unit conversion failed closed") from exc
    source_scale, source_dimensions = _exact_unit_spec(source_code)
    target_scale, target_dimensions = _exact_unit_spec(target_code)
    if source_dimensions != target_dimensions:
        raise ComputationValidationError(
            "source and target units are dimensionally incompatible"
        )
    return _require_sympy().cancel(source_scale / target_scale)


def _exact_unit_spec(code: str) -> tuple[Any, tuple[int, ...]]:
    sp = _require_sympy()
    scale = sp.Integer(1)
    dimensions = [0] * len(_D0)

    def apply_atom(unit_code: str, power: int) -> None:
        nonlocal scale
        numerator_value, denominator_value, pi_power, unit_dimensions = (
            _UNIT_EXACT_DEFINITIONS[unit_code]
        )
        unit_scale = sp.Mul(
            sp.Rational(numerator_value, denominator_value),
            sp.Pow(sp.pi, sp.Integer(pi_power)),
        )
        scale = sp.Mul(scale, sp.Pow(unit_scale, sp.Integer(power)))
        for index, dimension in enumerate(unit_dimensions):
            dimensions[index] += dimension * power

    for unit_code, power in _unit_tokens(code):
        apply_atom(unit_code, power)
    return sp.cancel(scale), tuple(dimensions)


def _unit_tokens(code: str) -> tuple[tuple[str, int], ...]:
    """Decode the qualified subset using UCUM's operator semantics.

    Binary multiplication and division are left associative.  UCUM's optional
    leading solidus is unary and applies to the complete following term, so
    ``/m/s`` means ``1 / (m / s)`` rather than ``(1 / m) / s``.
    """

    validate_unit_code(code)
    leading_reciprocal = code.startswith("/")
    term = code[1:] if leading_reciprocal else code
    parts = re.split(r"([./])", term)
    tokens = [_split_unit_atom(parts[0])]
    for operator, atom in zip(parts[1::2], parts[2::2], strict=True):
        unit_code, exponent = _split_unit_atom(atom)
        tokens.append((unit_code, exponent if operator == "." else -exponent))
    if leading_reciprocal:
        tokens = [(unit_code, -power) for unit_code, power in tokens]
    return tuple(tokens)


def _normalized_ucum_code(code: str) -> str:
    """Render an equivalent ucumvert-safe form of the qualified unit code."""

    atoms: list[str] = []
    for unit_code, power in _unit_tokens(code):
        if unit_code == "1" or power == 0:
            continue
        atoms.append(unit_code if power == 1 else f"{unit_code}{power}")
    return ".".join(atoms) or "1"


def _split_unit_atom(atom: str) -> tuple[str, int]:
    if atom == "1":
        return "1", 1
    for code in sorted(_COMPOUND_UNIT_CODES, key=lambda item: (-len(item), item)):
        if not atom.startswith(code):
            continue
        suffix = atom[len(code) :]
        if suffix == "":
            return code, 1
        if re.fullmatch(r"-?[0-3]", suffix):
            return code, int(suffix)
    raise ComputationValidationError("qualified unit atom could not be decoded")


def _require_sympy() -> Any:
    if _sympy is None:
        raise ComputationDependencyError("qualified SymPy runtime is unavailable")
    if _sympy.__version__ != QUALIFIED_SYMPY_VERSION:
        raise ComputationDependencyError(
            f"SymPy version is not qualified for {VALIDATOR_VERSION}"
        )
    return _sympy


def assert_computation_dependencies() -> None:
    """Fail unless the complete pinned computation runtime is ready."""

    _require_sympy()
    _require_units()


_UNIT_REGISTRY: Any = None


def _require_units() -> Any:
    global _UNIT_REGISTRY
    if _pint is None or _ucumvert is None:
        raise ComputationDependencyError("qualified UCUM/Pint runtime is unavailable")
    if _pint.__version__ != QUALIFIED_PINT_VERSION:
        raise ComputationDependencyError("Pint version is not qualified")
    if _ucumvert.__version__ != QUALIFIED_UCUMVERT_VERSION:
        raise ComputationDependencyError("ucumvert version is not qualified")
    package_root = Path(_ucumvert.__file__).resolve().parent
    candidates = tuple(package_root.parent.rglob("ucum-essence.xml"))
    if len(candidates) != 1:
        raise ComputationDependencyError(
            "qualified UCUM artifact was not found uniquely"
        )
    artifact_hash = hashlib.sha256(candidates[0].read_bytes()).hexdigest()
    if artifact_hash != UCUM_ESSENCE_SHA256:
        raise ComputationDependencyError("qualified UCUM artifact checksum mismatch")
    if _UNIT_REGISTRY is None:
        _UNIT_REGISTRY = _ucumvert.PintUcumRegistry()
    return _UNIT_REGISTRY


def _dependency_versions() -> dict[str, str]:
    return {
        "sympy": getattr(_sympy, "__version__", "unavailable"),
        "pint": getattr(_pint, "__version__", "unavailable"),
        "ucumvert": getattr(_ucumvert, "__version__", "unavailable"),
        "ucum_profile": UCUM_PROFILE,
        "ucum_essence_sha256": UCUM_ESSENCE_SHA256,
    }


def _require_closed_finite_real(value: Any) -> None:
    if value.free_symbols:
        raise ComputationValidationError("numeric computation has unresolved variables")
    if value.is_real is False:
        raise ComputationValidationError("computation produced a non-real value")
    numeric = value.evalf(30)
    if numeric.has(
        _require_sympy().zoo,
        _require_sympy().oo,
        -_require_sympy().oo,
        _require_sympy().nan,
    ):
        raise ComputationValidationError("computation produced a non-finite value")
    try:
        if not math.isfinite(float(numeric)):
            raise ComputationValidationError("computation produced a non-finite value")
    except (TypeError, ValueError, OverflowError):
        raise ComputationValidationError(
            "computation did not produce a finite real number"
        ) from None


def _within_tolerance(value: Any, expected: str, tolerance: TolerancePolicy) -> bool:
    with localcontext() as context:
        context.prec = 100
        actual_decimal = Decimal(str(value.evalf(90)))
        expected_decimal = Decimal(expected)
        difference = abs(actual_decimal - expected_decimal)
        absolute = Decimal(tolerance.absolute)
        relative = Decimal(tolerance.relative) * abs(expected_decimal)
        return difference <= max(absolute, relative)


def _tolerance_is_nonzero(tolerance: TolerancePolicy) -> bool:
    return Decimal(tolerance.absolute) != 0 or Decimal(tolerance.relative) != 0


def _canonical(value: Any) -> str:
    return _require_sympy().sstr(value, order="lex")


def _decimal_approx(value: Any) -> str:
    _require_closed_finite_real(value)
    return _normalize_decimal(Decimal(str(value.evalf(30))))


def _normalize_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ComputationValidationError("numeric value is not finite")
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"-0", ""} else normalized


def _validate_decimal_string(value: str | None, *, field_name: str) -> Decimal:
    if value is None or not _DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a plain decimal string")
    digits = sum(character.isdigit() for character in value)
    if digits > MAX_NUMERIC_DIGITS:
        raise ValueError(f"{field_name} may have at most {MAX_NUMERIC_DIGITS} digits")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field_name} is not a valid decimal") from None


def _literal_rational(node: ExpressionNode) -> tuple[int, int] | None:
    if node.kind == ExpressionKind.INTEGER:
        return node.integer, 1
    if node.kind == ExpressionKind.RATIONAL:
        return node.numerator, node.denominator
    return None


def _leaf_fraction(node: ExpressionNode | None) -> Any:
    if node is None:
        return None
    from fractions import Fraction

    if node.kind == ExpressionKind.INTEGER:
        return Fraction(node.integer, 1)
    if node.kind == ExpressionKind.RATIONAL:
        return Fraction(node.numerator, node.denominator)
    if node.kind == ExpressionKind.DECIMAL:
        return Fraction(Decimal(node.decimal))
    return None


def _expression_stats(node: ExpressionNode) -> tuple[int, int, set[str]]:
    count, depth = 1, 1
    symbols = {node.symbol} if node.kind == ExpressionKind.SYMBOL else set()
    for child in node.args:
        child_count, child_depth, child_symbols = _expression_stats(child)
        count += child_count
        depth = max(depth, child_depth + 1)
        symbols.update(child_symbols)
    return count, depth, symbols


def _passed(code: str, message: str, **details: Any) -> ValidationCheck:
    return ValidationCheck(
        code=code, status=CheckStatus.PASSED, message=message, details=details
    )


def _failed(code: str, message: str) -> ValidationCheck:
    return ValidationCheck(code=code, status=CheckStatus.FAILED, message=message)


def _inconclusive(code: str, message: str) -> ValidationCheck:
    return ValidationCheck(code=code, status=CheckStatus.INCONCLUSIVE, message=message)


__all__ = [
    "AssessmentComputationBlueprint",
    "AssessmentValidationReport",
    "CheckStatus",
    "ComparisonConstraint",
    "ComparisonOperator",
    "ComputationChoice",
    "ComputationDelivery",
    "ComputationDependencyError",
    "ComputationError",
    "ComputationFamily",
    "ComputationOperation",
    "ComputationProfile",
    "ComputationResult",
    "ComputationUnsupportedError",
    "ComputationValidationError",
    "ComputationValidationRequest",
    "ExpressionKind",
    "ExpressionNode",
    "FormulaAdapterPromotionEvidence",
    "NativeEngineEvidence",
    "SeedSample",
    "TolerancePolicy",
    "ValidationCheck",
    "ValidationStatus",
    "VariableAssumption",
    "VariableDomain",
    "VariableSpec",
    "assert_computation_dependencies",
    "canonical_blueprint_hash",
    "compile_expression",
    "compute_blueprint",
    "deterministic_seeds",
    "validate_computation",
    "validate_unit_code",
]
