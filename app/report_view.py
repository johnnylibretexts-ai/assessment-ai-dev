"""Reviewer-safe projections of persisted computation evidence.

Native-engine receipts contain immutable grading evidence that is useful for
publication gates but too detailed for the reviewer page.  This module keeps
the reviewer projection deliberately separate from the receipt model and uses
an explicit field allowlist.  In particular, compiled source and submission
hashes never enter the template context.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


MAX_NATIVE_SEED_OBSERVATIONS = 25
_MAX_OBSERVED_VARIABLES = 12
_MAX_OBSERVED_INTEGER = (10**100) - 1
_VARIABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBSERVATION_BOOLEAN_FIELDS = (
    "parameters_satisfied",
    "constraints_satisfied",
    "correct_answer_accepted",
    "alternate_correct_answer_accepted",
    "wrong_answer_rejected",
    "rendered",
)
_OBSERVATION_COUNT_FIELDS = (
    "warnings_count",
    "errors_count",
    "outbound_request_count",
)
_OBSERVATION_HASH_FIELDS = (
    "observation_sha256",
    "render_sha256",
    "repeat_render_sha256",
)


def attach_native_seed_observations(
    view: dict[str, Any] | None,
    persisted_engine_evidence: str | Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a view with a bounded, reviewer-safe native observation summary."""

    if view is None:
        return None
    projected = dict(view)
    projected["native_seed_observations"] = native_seed_observation_summaries(
        persisted_engine_evidence
    )
    return projected


def native_seed_observation_summaries(
    persisted_engine_evidence: str | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project at most 25 native observations through a strict allowlist."""

    evidence = _object(persisted_engine_evidence)
    receipt = evidence.get("native_receipt")
    if not isinstance(receipt, Mapping):
        return []
    observations = receipt.get("observations")
    if not isinstance(observations, list):
        return []

    summaries: list[dict[str, Any]] = []
    seen_seeds: set[int] = set()
    for observation in observations:
        if len(summaries) >= MAX_NATIVE_SEED_OBSERVATIONS:
            break
        summary = _safe_native_seed_observation(observation)
        if summary is None or summary["seed"] in seen_seeds:
            continue
        seen_seeds.add(summary["seed"])
        summaries.append(summary)
    return summaries


def _object(value: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_native_seed_observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    seed = value.get("seed")
    if (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or not 0 <= seed <= 0x7FFFFFFF
    ):
        return None

    raw_variables = value.get("observed_variables")
    if not isinstance(raw_variables, Mapping):
        return None
    observed_variables: dict[str, int] = {}
    for name, observed in sorted(raw_variables.items(), key=lambda item: str(item[0])):
        if len(observed_variables) >= _MAX_OBSERVED_VARIABLES:
            break
        if (
            not isinstance(name, str)
            or _VARIABLE_NAME.fullmatch(name) is None
            or not isinstance(observed, int)
            or isinstance(observed, bool)
            or abs(observed) > _MAX_OBSERVED_INTEGER
        ):
            continue
        observed_variables[name] = observed

    hashes: dict[str, str] = {}
    for field in _OBSERVATION_HASH_FIELDS:
        item = value.get(field)
        if not isinstance(item, str) or _SHA256.fullmatch(item) is None:
            return None
        hashes[field] = item

    summary: dict[str, Any] = {
        "seed": seed,
        "observed_variables": observed_variables,
        **hashes,
    }
    for field in _OBSERVATION_BOOLEAN_FIELDS:
        item = value.get(field)
        if isinstance(item, bool):
            summary[field] = item
    for field in _OBSERVATION_COUNT_FIELDS:
        item = value.get(field)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 100:
            summary[field] = item
    return summary


__all__ = [
    "MAX_NATIVE_SEED_OBSERVATIONS",
    "attach_native_seed_observations",
    "native_seed_observation_summaries",
]
