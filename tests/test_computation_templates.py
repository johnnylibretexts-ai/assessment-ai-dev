from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.computation_workflow import _safe_engine_evidence, report_view
from app.db import ComputationValidationRead
from app.report_view import attach_native_seed_observations


TEMPLATE_DIR = Path(__file__).parents[1] / "app" / "templates"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(("html",)),
    )


def _render_index(**overrides: Any) -> str:
    context: dict[str, Any] = {
        "notice": None,
        "error": None,
        "drafts": [],
        "public_sources_enabled": True,
        "advanced_items_enabled": False,
        "hint_generation_enabled": False,
        "item_type_options": [],
    }
    context.update(overrides)
    return _environment().get_template("index.html").render(**context)


def _draft() -> dict[str, Any]:
    return {
        "id": 17,
        "status": "ready_for_review",
        "status_label": "ready for review",
        "source_title": "Synthetic computation source",
        "source_type": "public",
        "source_url": "https://chem.libretexts.org/example",
        "source_library": "chem",
        "source_page_id": "42",
        "cited_paragraphs": [],
        "item_type": "multiple_choice",
        "item_type_label": "multiple choice",
        "context_type": "standard",
        "model_id": "test-model",
        "prompt_version": "test-prompt",
        "stimulus": None,
        "stem": "What is the computed value?",
        "choices": [
            {"id": "A", "text": "1.5", "correct": True},
            {"id": "B", "text": "2.0", "correct": False},
        ],
        "response": {},
        "explanation": "The exact result is three halves.",
        "critique_issues": [],
        "bloom": "apply",
        "difficulty": "intermediate",
        "bloom_confirmed": False,
        "difficulty_confirmed": False,
        "reviewer_notes": "",
        "specialist_review_required": False,
        "hint_ladder": None,
        "publications": [],
    }


def _render_draft(
    computation: dict[str, Any] | None = None,
    *,
    include_computation_key: bool = True,
) -> str:
    context: dict[str, Any] = {
        "draft": _draft(),
        "notice": None,
        "error": None,
        "bloom_options": ["apply"],
        "difficulty_options": ["intermediate"],
    }
    if include_computation_key:
        context["computation"] = computation
    return _environment().get_template("draft.html").render(**context)


def _computation_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "validated",
        "status_label": "Validated",
        "current": True,
        "stale": False,
        "legacy_reason": None,
        "exact_result": "3/2",
        "approximate_result": "1.500000000000000",
        "target_unit": "m/s",
        "domain": "real",
        "assumptions": ["x is real", {"kind": "nonzero", "symbol": "x"}],
        "checks": [
            {
                "name": "exact substitution",
                "status": "passed",
                "detail": "All exact substitutions matched.",
                "details": {
                    "candidate_count": 4,
                    "boundary_values": ["0", "1"],
                },
            },
            {
                "name": "native engine receipt",
                "status": "unavailable",
                "detail": "Not required for numerical delivery.",
            },
        ],
        "limitations": ["Source alignment and pedagogy are outside this report."],
        "hashes": {
            "source": "source-sha",
            "draft": "draft-sha",
            "blueprint": "blueprint-sha",
            "report": "report-sha",
        },
        "schema_version": "assessment-computation-v0",
        "validator_revision": "validator-v0",
        "dependency_versions": {
            "sympy": "1.14.0",
            "pint": "0.25.3",
            "ucumvert": "0.3.2",
        },
        "container_digest": "sha256:container",
        "seeds": [{"index": index, "seed": f"seed-{index}"} for index in range(30)],
        "seed_samples": [
            {
                "kind": "boundary" if index == 0 else "seeded",
                "seed": None if index == 0 else index,
                "values": {"x": str(index)},
            }
            for index in range(30)
        ],
        "engine_evidence": {
            "engine": "webwork",
            "compiler_version": "assessment-computation-webwork-v0",
            "source_sha256": "a" * 64,
            "seed_count": 25,
        },
        "native_receipt_evidence": {
            "engine": "webwork",
            "source_sha256": "a" * 64,
            "receipt_sha256": "b" * 64,
            "seeds_validated": 25,
            "passed": True,
        },
        "duration_ms": 19,
        "attestations": [],
        "specialist_allowed": False,
    }
    report.update(overrides)
    return report


def test_computation_controls_are_absent_when_off_or_context_is_missing() -> None:
    missing = _render_index()
    off = _render_index(
        computation_mode="off",
        computation_families=["numeric", "unit"],
    )

    for rendered in (missing, off):
        assert 'name="computation_family"' not in rendered
        assert 'name="computation_delivery"' not in rendered


def test_computation_controls_require_an_allowlisted_family_and_preserve_blank() -> (
    None
):
    no_families = _render_index(
        computation_mode="assist",
        computation_families=[],
    )
    rendered = _render_index(
        computation_mode="assist",
        computation_families=["numeric", "unit"],
    )

    assert 'name="computation_family"' not in no_families
    assert 'name="computation_family"' in rendered
    assert 'name="computation_delivery"' in rendered
    assert '<option value="">No computation profile</option>' in rendered
    assert '<option value="">Use the standard item selection</option>' in rendered
    assert '<option value="numeric">Numeric</option>' in rendered
    assert '<option value="unit">Unit</option>' in rendered
    assert "Leaving either field blank preserves standard generation." in rendered


def test_draft_without_computation_context_keeps_legacy_rendering() -> None:
    rendered = _render_draft(include_computation_key=False)

    assert "Synthetic computation source" in rendered
    assert 'id="computation-heading"' not in rendered
    assert "/computation/attest" not in rendered


def test_validation_panel_exposes_scope_status_checks_and_bounded_evidence() -> None:
    rendered = _render_draft(_computation_report())
    normalized = " ".join(rendered.split())

    assert 'aria-labelledby="computation-heading"' in rendered
    assert "Status: Validated" in rendered
    assert "Current evidence:" in rendered
    assert "Human review is still mandatory." in rendered
    assert (
        "does not validate source alignment, wording, accessibility, or pedagogy"
        in normalized
    )
    assert "Exact result" in rendered
    assert "3/2" in rendered
    assert "Target unit" in rendered
    assert "m/s" in rendered
    assert "<caption>Deterministic validation checks</caption>" in rendered
    assert "exact substitution" in rendered
    assert "passed" in rendered
    assert "Show bounded structured evidence" in rendered
    assert "candidate_count" in rendered
    assert "assessment-computation-v0" in rendered
    assert "blueprint-sha" in rendered
    assert "Bounded deterministic seed evidence (25)" in rendered
    assert "seed-24" in rendered
    assert "seed-25" not in rendered
    assert "Bounded computation seed samples (25)" in rendered
    assert '"x": "24"' in rendered
    assert '"x": "25"' not in rendered
    assert "External-engine evidence" in rendered
    assert "Compiled source" in rendered
    assert "Native receipt" in rendered
    assert "assessment-computation-webwork-v0" in rendered
    assert "a" * 64 in rendered
    assert "b" * 64 in rendered
    assert 'name="blueprint"' not in rendered
    assert 'name="computation_blueprint"' not in rendered


def test_stale_and_legacy_evidence_are_explicit_text_not_color_only() -> None:
    rendered = _render_draft(
        _computation_report(
            status="unsupported",
            status_label="Unsupported",
            current=False,
            stale=True,
            legacy_reason="legacy_without_blueprint",
        )
    )

    assert "Status: Unsupported" in rendered
    assert "Stale evidence:" in rendered
    assert "Unsupported legacy draft:" in rendered
    assert "must be explicitly revalidated" in rendered
    assert 'action="/drafts/17/computation/revalidate"' in rendered
    assert 'name="expected_edit_count"' in rendered
    assert 'name="blueprint_json"' in rendered
    assert 'aria-describedby="computation-revalidation-help"' in rendered
    assert "Expression strings" in rendered

    typed = _render_draft(_computation_report())
    assert "/computation/revalidate" not in typed


def test_specialist_attestation_form_is_strictly_status_and_identity_scoped() -> None:
    eligible = _render_draft(
        _computation_report(
            status="partially_validated",
            status_label="Partially validated",
            specialist_allowed=True,
        )
    )
    wrong_status = _render_draft(
        _computation_report(status="validated", specialist_allowed=True)
    )
    wrong_identity = _render_draft(
        _computation_report(
            status="unsupported",
            status_label="Unsupported",
            specialist_allowed=False,
        )
    )
    stale = _render_draft(
        _computation_report(
            status="partially_validated",
            status_label="Partially validated",
            specialist_allowed=True,
            current=False,
            stale=True,
        )
    )

    assert 'action="/drafts/17/computation/attest"' in eligible
    assert 'name="rationale"' in eligible
    assert 'minlength="20"' in eligible
    assert "append-only" in eligible
    assert "/computation/attest" not in wrong_status
    assert "/computation/attest" not in wrong_identity
    assert "/computation/attest" not in stale


def test_existing_specialist_attestation_is_presented_with_rationale() -> None:
    rendered = _render_draft(
        _computation_report(
            status="unsupported",
            status_label="Unsupported",
            attestations=[
                {
                    "specialist_identity": "specialist@example.edu",
                    "rationale": "The native-engine receipt is temporarily unavailable.",
                    "created_at": "2026-07-22T12:00:00Z",
                }
            ],
        )
    )

    assert "Specialist attestations" in rendered
    assert "specialist@example.edu" in rendered
    assert "The native-engine receipt is temporarily unavailable." in rendered


def test_report_view_bounds_actual_samples_details_and_whitelists_engine_evidence() -> (
    None
):
    report = {
        "status": "partially_validated",
        "result": {
            "exact_value": "5",
            "seed_samples": [
                {
                    "kind": "boundary" if index == 0 else "seeded",
                    "seed": None if index == 0 else index,
                    "values": {"x": str(index)},
                }
                for index in range(30)
            ],
        },
        "checks": [
            {
                "code": "sample_plan",
                "status": "passed",
                "message": "Deterministic samples passed.",
                "details": {
                    "samples": list(range(20)),
                    "long_note": "z" * 600,
                },
            },
            {
                "code": "native_engine",
                "status": "inconclusive",
                "message": "A native receipt is pending server trust.",
                "details": {
                    "engine": "webwork",
                    "source_sha256": "a" * 64,
                    "receipt_sha256": "b" * 64,
                    "seeds_validated": 25,
                },
            },
        ],
        "limitations": [],
    }
    record = ComputationValidationRead(
        id=1,
        draft_id=17,
        edit_count=0,
        source_sha256="1" * 64,
        draft_sha256="2" * 64,
        blueprint_sha256="3" * 64,
        report_sha256="4" * 64,
        evidence_sha256="6" * 64,
        status="partially_validated",
        schema_version="assessment-computation-v0",
        validator_revision="assessment-computation-validator-v0",
        blueprint_json=json.dumps(
            {
                "variables": [
                    {
                        "name": "x",
                        "domain": "integer",
                        "assumptions": ["positive"],
                    }
                ]
            }
        ),
        report_json=json.dumps(report),
        dependency_versions_json='{"sympy":"1.14.0"}',
        container_digest=f"sha256:{'5' * 64}",
        seed_plan_json=json.dumps(list(range(30))),
        duration_ms=23,
        engine_evidence_json=json.dumps(
            {
                "engine": "webwork",
                "compiler_version": "assessment-computation-webwork-v0",
                "source_sha256": "a" * 64,
                "seed_count": 25,
                "previews": [{"prompt": "must not be reviewer-visible"}],
                "credential": "must-not-leak",
            }
        ),
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
        is_current=True,
    )

    view = report_view(record, legacy_computational=True)

    assert view is not None
    assert len(view["seed_samples"]) == 25
    assert view["seed_samples"][0] == {
        "kind": "boundary",
        "seed": None,
        "values": {"x": "0"},
    }
    assert view["seed_samples"][-1]["seed"] == 24
    sample_check = view["checks"][0]
    assert sample_check["details"]["samples"] == list(range(12))
    assert len(sample_check["details"]["long_note"]) == 500
    assert sample_check["details_truncated"] is True
    assert view["engine_evidence"] == {
        "engine": "webwork",
        "compiler_version": "assessment-computation-webwork-v0",
        "source_sha256": "a" * 64,
        "seed_count": 25,
    }
    assert view["native_receipt_evidence"] == {
        "engine": "webwork",
        "source_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
        "seeds_validated": 25,
    }
    assert "previews" not in view["engine_evidence"]
    assert "credential" not in view["engine_evidence"]

    rendered = _render_draft(view)

    assert "must not be reviewer-visible" not in rendered
    assert "must-not-leak" not in rendered
    assert "Additional or oversized detail was omitted" in rendered
    assert "Bounded computation seed samples (25)" in rendered
    assert "Receipt SHA-256" in rendered


def test_native_receipt_identity_is_whitelisted_and_reviewer_visible() -> None:
    raw = {
        "engine": "webwork",
        "answer_kind": "numeric",
        "native_grader": "MathObjects::Real::cmp",
        "compiler_version": "assessment-computation-typed-ast-v0",
        "source_sha256": "1" * 64,
        "blueprint_sha256": "2" * 64,
        "draft_sha256": "3" * 64,
        "request_sha256": "4" * 64,
        "receipt_sha256": "5" * 64,
        "seed_plan_sha256": "6" * 64,
        "seed_receipts_sha256": "7" * 64,
        "runner_id": "qualified-runner",
        "runner_version": "runner-v0",
        "runner_manifest_sha256": "8" * 64,
        "runner_image_digest": f"sha256:{'9' * 64}",
        "qualification_report_sha256": "a" * 64,
        "promotion_approval_sha256": "b" * 64,
        "engine_image_digest": f"sha256:{'c' * 64}",
        "network_attestation_sha256": "d" * 64,
        "warnings_count": 0,
        "errors_count": 0,
        "outbound_request_count": 0,
        "seeds_validated": 25,
        "correct_answer_accepted": True,
        "wrong_answer_rejected": True,
        "rendered": True,
        "passed": True,
        "server_verified": True,
        "source": "must-not-render",
        "probes": ["must-not-render"],
    }
    safe = _safe_engine_evidence(raw)
    rendered = _render_draft(
        _computation_report(
            native_receipt_evidence=safe,
            engine_evidence={},
        )
    )

    assert safe["runner_id"] == "qualified-runner"
    assert safe["request_sha256"] == "4" * 64
    assert safe["warnings_count"] == 0
    assert "source" not in safe
    assert "probes" not in safe
    assert "Native grader" in rendered
    assert "Runner version" in rendered
    assert "Request SHA-256" in rendered
    assert "Warnings" in rendered
    assert "Server verified" in rendered
    assert "must-not-render" not in rendered


def test_persisted_native_seed_observations_are_bounded_safe_and_accessible() -> None:
    correct_submission_sha256 = hashlib.sha256(
        b"private correct submission"
    ).hexdigest()
    alternate_submission_sha256 = hashlib.sha256(
        b"private alternate submission"
    ).hexdigest()
    wrong_submission_sha256 = hashlib.sha256(b"private wrong submission").hexdigest()
    observations = []
    for index in range(30):
        render_sha256 = hashlib.sha256(f"render-{index}".encode()).hexdigest()
        observations.append(
            {
                "seed": index,
                "observed_variables": {"x": 100_000 + index},
                "correct_submission_sha256": correct_submission_sha256,
                "alternate_correct_submission_sha256": (alternate_submission_sha256),
                "wrong_submission_sha256": wrong_submission_sha256,
                "parameters_satisfied": True,
                "constraints_satisfied": True,
                "correct_answer_accepted": True,
                "alternate_correct_answer_accepted": True,
                "wrong_answer_rejected": True,
                "rendered": True,
                "render_sha256": render_sha256,
                "repeat_render_sha256": render_sha256,
                "warnings_count": 0,
                "errors_count": 0,
                "outbound_request_count": 0,
                "observation_sha256": hashlib.sha256(
                    f"observation-{index}".encode()
                ).hexdigest(),
                "provider_notes": "private provider detail",
            }
        )
    persisted = json.dumps(
        {
            "source": "COMPILED_ENGINE_SOURCE_MUST_NOT_LEAK",
            "native_receipt": {
                "source": "RECEIPT_SOURCE_MUST_NOT_LEAK",
                "observations": observations,
            },
        }
    )

    view = attach_native_seed_observations(_computation_report(), persisted)

    assert view is not None
    summaries = view["native_seed_observations"]
    assert len(summaries) == 25
    assert summaries[0] == {
        "seed": 0,
        "observed_variables": {"x": 100_000},
        "observation_sha256": observations[0]["observation_sha256"],
        "render_sha256": observations[0]["render_sha256"],
        "repeat_render_sha256": observations[0]["repeat_render_sha256"],
        "parameters_satisfied": True,
        "constraints_satisfied": True,
        "correct_answer_accepted": True,
        "alternate_correct_answer_accepted": True,
        "wrong_answer_rejected": True,
        "rendered": True,
        "warnings_count": 0,
        "errors_count": 0,
        "outbound_request_count": 0,
    }
    assert summaries[-1]["seed"] == 24
    serialized_summaries = json.dumps(summaries, sort_keys=True)
    assert correct_submission_sha256 not in serialized_summaries
    assert alternate_submission_sha256 not in serialized_summaries
    assert wrong_submission_sha256 not in serialized_summaries
    assert "COMPILED_ENGINE_SOURCE_MUST_NOT_LEAK" not in serialized_summaries
    assert "RECEIPT_SOURCE_MUST_NOT_LEAK" not in serialized_summaries
    assert "private provider detail" not in serialized_summaries

    rendered = _render_draft(view)

    assert "Native-engine seed observations (25, bounded to 25)" in rendered
    assert 'aria-label="Scrollable native-engine seed observations"' in rendered
    assert 'scope="col">Observed variables' in rendered
    assert 'aria-label="Native-engine checks for seed 0"' in rendered
    assert '"x": 100024' in rendered
    assert '"x": 100025' not in rendered
    assert observations[24]["observation_sha256"] in rendered
    assert observations[24]["render_sha256"] in rendered
    assert correct_submission_sha256 not in rendered
    assert alternate_submission_sha256 not in rendered
    assert wrong_submission_sha256 not in rendered
    assert "COMPILED_ENGINE_SOURCE_MUST_NOT_LEAK" not in rendered
    assert "RECEIPT_SOURCE_MUST_NOT_LEAK" not in rendered
    assert "private provider detail" not in rendered


def test_malformed_native_seed_observations_fail_closed_in_reviewer_view() -> None:
    valid_hash = "a" * 64
    persisted = {
        "native_receipt": {
            "observations": [
                {
                    "seed": True,
                    "observed_variables": {"x": 1},
                    "observation_sha256": valid_hash,
                    "render_sha256": valid_hash,
                    "repeat_render_sha256": valid_hash,
                },
                {
                    "seed": 2,
                    "observed_variables": {"x": 1},
                    "observation_sha256": "not-a-hash",
                    "render_sha256": valid_hash,
                    "repeat_render_sha256": valid_hash,
                },
            ]
        }
    }

    view = attach_native_seed_observations(_computation_report(), persisted)

    assert view is not None
    assert view["native_seed_observations"] == []
