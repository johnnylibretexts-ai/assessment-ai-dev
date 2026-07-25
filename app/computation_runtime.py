"""Compute-only dependency boundary for Assessment Computation v0.

The networked Assessment AI application imports :mod:`app.computation` for its
typed contracts, hashes, and bounded rendering helpers.  Only the networkless
sidecar imports this module, which binds the pinned symbolic and units
dependencies to the otherwise dependency-free computation core.
"""

from __future__ import annotations

import pint
import sympy
import ucumvert

from . import computation as _core


def bind_runtime_dependencies() -> None:
    """Bind the fixed, compile-time dependency set to the computation core."""

    _core._sympy = sympy
    _core._pint = pint
    _core._ucumvert = ucumvert


bind_runtime_dependencies()


# Keep the executable surface explicit.  Contracts continue to come from
# ``app.computation`` so HTTP clients and the sidecar share exactly one model
# identity and JSON schema.
def assert_computation_dependencies() -> None:
    _core.assert_computation_dependencies()


def compile_expression(
    node: _core.ExpressionNode,
    variables: list[_core.VariableSpec] | tuple[_core.VariableSpec, ...] = (),
):
    return _core.compile_expression(node, variables)


def compute_blueprint(
    blueprint: _core.AssessmentComputationBlueprint,
) -> _core.ComputationResult:
    return _core.compute_blueprint(blueprint)


def validate_computation(
    request: _core.ComputationValidationRequest,
) -> _core.AssessmentValidationReport:
    return _core.validate_computation(request)


__all__ = [
    "assert_computation_dependencies",
    "bind_runtime_dependencies",
    "compile_expression",
    "compute_blueprint",
    "validate_computation",
]
