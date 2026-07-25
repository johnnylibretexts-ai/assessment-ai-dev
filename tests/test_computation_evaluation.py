from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from xml.etree import ElementTree

import pytest

from app.computation import compute_blueprint
from app.parameterized import compile_parameterized_item
import evaluation.computation as computation_evaluation
from evaluation.computation import (
    CATALOG_PATH,
    EXECUTIONS_PER_PLAN,
    AcceptanceMetrics,
    AlgebraNativeExecutionRequest,
    AlgebraNativeQualificationReceipt,
    ComputationEvaluationManifest,
    ComputationEvaluationObservation,
    EngineExecutionSummary,
    EvaluationSurface,
    FixtureDifficulty,
    FixtureSplit,
    MutationKind,
    NativeQualificationReceipt,
    NativeQualificationTrustPolicy,
    ObservedValidationState,
    PairedDraftEvidence,
    PairedReviewRecord,
    PairedStudyEvidenceLedger,
    PairedStudySummary,
    SafetyMonitorReceipt,
    SmeReviewRecord,
    SpikeSafetyEvidence,
    build_algebra_native_execution_plan,
    build_lineage_blueprint,
    build_computation_evaluation_manifest,
    build_computation_seed_plan,
    calculate_acceptance_metrics,
    load_algebra_native_qualification_receipts,
    load_native_qualification_receipts,
    load_native_qualification_trust_policy,
    load_sme_review_records,
    qualify_ucum_subset,
    run_offline_mutation_qualification,
    validate_algebra_native_qualification_receipts,
    validate_native_qualification_receipts,
    validate_paired_study_evidence,
    validate_sme_review_records,
)
from evaluation.cli import main as evaluation_main


def test_clean_room_manifest_has_required_matrix_and_provenance() -> None:
    manifest = build_computation_evaluation_manifest()

    assert len(manifest.computation_cases) == 60
    assert len(manifest.parameterized_lineages) == 20
    assert len(manifest.engine_twins) == 40
    assert len(manifest.algebra_native_plans) == 40
    assert len(manifest.mutations) == 200
    assert manifest.total_positive_surfaces == 100
    assert manifest.total_planned_engine_executions == 8_000
    assert manifest.total_planned_algebra_native_receipts == 40
    assert manifest.provenance.origin == "independently_authored_synthetic"
    assert manifest.provenance.review_status == "awaiting_sme_review"
    assert manifest.provenance.expert_approved is False
    assert manifest.provenance.contains_proprietary_data is False
    assert manifest.provenance.contains_wolfram_data is False
    assert manifest.provenance.wolfram_api_used is False

    for family in ("numeric", "algebraic", "unit"):
        cases = [case for case in manifest.computation_cases if case.family == family]
        _assert_distribution(cases)
    _assert_distribution(manifest.parameterized_lineages)
    for engine in ("webwork", "imathas"):
        _assert_distribution(
            [case for case in manifest.engine_twins if case.engine == engine]
        )


def test_manifest_and_fixture_hashes_are_deterministic() -> None:
    first = build_computation_evaluation_manifest()
    second = build_computation_evaluation_manifest()

    assert first.manifest_sha256 == second.manifest_sha256
    assert first.catalog_sha256 == second.catalog_sha256
    assert [case.fixture_sha256 for case in first.computation_cases] == [
        case.fixture_sha256 for case in second.computation_cases
    ]
    assert len({case.fixture_sha256 for case in first.computation_cases}) == 60
    assert len({case.lineage_sha256 for case in first.parameterized_lineages}) == 20

    tampered = first.model_dump(mode="json")
    tampered["computation_cases"][0]["expected_exact"] = "tampered"
    with pytest.raises(ValueError, match="fixture_sha256"):
        ComputationEvaluationManifest.model_validate(tampered)


def test_catalog_cannot_claim_expert_approval_or_wolfram_data(tmp_path) -> None:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    raw["provenance"]["expert_approved"] = True
    raw["provenance"]["contains_wolfram_data"] = True
    path = tmp_path / "invalid-fixtures.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        build_computation_evaluation_manifest(path)


def test_all_cases_materialize_as_typed_blueprints() -> None:
    manifest = build_computation_evaluation_manifest()

    for case in manifest.computation_cases:
        dumped = case.blueprint.model_dump(mode="json")
        assert dumped["schema_version"] == "assessment-computation-v0"
        assert isinstance(dumped["expression"], dict)
        assert "kind" in dumped["expression"]
        assert "code" not in json.dumps(dumped).lower()
        assert "http://" not in json.dumps(dumped).lower()
        assert "https://" not in json.dumps(dumped).lower()


def test_all_positive_computation_blueprints_execute_against_synthetic_oracles() -> (
    None
):
    manifest = build_computation_evaluation_manifest()

    for case in manifest.computation_cases:
        result = compute_blueprint(case.blueprint)
        if case.expected_solutions:
            assert result.solutions == case.expected_solutions
        elif case.expected_exact == "true":
            assert result.equivalent is True
        elif case.family in {"numeric", "unit"}:
            assert result.numeric_value is not None
            assert math.isclose(
                float(result.numeric_value),
                _expected_float(case.expected_exact),
                rel_tol=1e-9,
                abs_tol=1e-9,
            )
        else:
            assert result.canonical_expression


def test_twenty_typed_lineages_compile_to_both_engines() -> None:
    manifest = build_computation_evaluation_manifest()
    by_engine = {
        engine: [case for case in manifest.engine_twins if case.engine == engine]
        for engine in ("webwork", "imathas")
    }

    assert len(by_engine["webwork"]) == len(by_engine["imathas"]) == 20
    assert {case.lineage_id for case in by_engine["webwork"]} == {
        case.lineage_id for case in by_engine["imathas"]
    }
    assert all(case.answer_kind == "numeric" for case in manifest.engine_twins)
    assert all(
        case.qualification_status == "planned_native_execution"
        for case in manifest.engine_twins
    )
    assert all(len(case.source_sha256) == 64 for case in manifest.engine_twins)


def test_formula_profiles_do_not_claim_native_qualification() -> None:
    manifest = build_computation_evaluation_manifest()
    statuses = {
        (case.engine, case.operation): case.status
        for case in manifest.formula_qualification_cases
    }

    expected_operations = {"substitute", "expand", "factor", "equivalent"}
    assert statuses == {
        (engine, operation): "qualification_pending"
        for engine in ("webwork", "imathas")
        for operation in expected_operations
    }
    assert all(
        case.operation != "evaluate" for case in manifest.formula_qualification_cases
    )
    assert all(
        not case.native_receipts_present
        for case in manifest.formula_qualification_cases
    )


def test_formula_qualification_plans_cover_partial_substitution_per_engine() -> None:
    manifest = build_computation_evaluation_manifest()
    partial_case = next(
        case
        for case in manifest.computation_cases
        if case.slug == "symbolic_coefficient_substitution"
    )

    assert manifest.catalog_revision == "2026-07-23.1"
    assert partial_case.difficulty == FixtureDifficulty.BASIC
    assert partial_case.blueprint.operation.value == "substitute"
    assert set(partial_case.blueprint.substitutions) == {"a"}
    result = compute_blueprint(partial_case.blueprint)
    assert result.canonical_expression == "2*x + 1"

    expected_operations = {"substitute", "expand", "factor", "equivalent"}
    for engine in ("webwork", "imathas"):
        formula_plans = [
            plan
            for plan in manifest.algebra_native_plans
            if plan.engine == engine and plan.answer_kind == "formula"
        ]
        assert {plan.operation for plan in formula_plans} == expected_operations
        assert all(plan.operation != "evaluate" for plan in formula_plans)
        partial_plan = next(
            plan for plan in formula_plans if plan.fixture_id == partial_case.fixture_id
        )
        assert partial_plan.operation == "substitute"
        assert partial_plan.response_symbols == ["x"]
        assert partial_plan.source_contract == "production_typed_ast_v0"


def test_manifest_rejects_formula_operation_coverage_without_substitute() -> None:
    manifest = build_computation_evaluation_manifest()
    tampered = manifest.model_dump(mode="json")
    plan = next(
        plan
        for plan in tampered["algebra_native_plans"]
        if plan["engine"] == "webwork"
        and plan["operation"] == "substitute"
        and plan["answer_kind"] == "formula"
    )
    plan["operation"] = "equivalent"
    identity = dict(plan)
    identity.pop("plan_sha256")
    plan["plan_sha256"] = computation_evaluation._sha256_json(identity)

    with pytest.raises(
        ValueError,
        match="formula qualification plans must cover substitute",
    ):
        ComputationEvaluationManifest.model_validate(tampered)


def test_legacy_formula_case_rejects_evaluate_operation() -> None:
    case = build_computation_evaluation_manifest().formula_qualification_cases[0]
    tampered = case.model_dump(mode="json")
    tampered["operation"] = "evaluate"

    with pytest.raises(ValueError, match="operation"):
        type(case).model_validate(tampered)


def test_all_algebra_cases_have_fixed_qualification_only_native_plans() -> None:
    manifest = build_computation_evaluation_manifest()
    plans_by_engine = {
        engine: [
            plan for plan in manifest.algebra_native_plans if plan.engine == engine
        ]
        for engine in ("webwork", "imathas")
    }

    assert all(len(plans) == 20 for plans in plans_by_engine.values())
    for plans in plans_by_engine.values():
        assert Counter(plan.answer_kind for plan in plans) == {
            "numeric": 3,
            "formula": 11,
            "solution_set": 6,
        }
    assert all(
        plan.coverage_scope == "learner_delivery_native_qualification"
        for plan in manifest.algebra_native_plans
    )
    assert all(
        not plan.production_delivery_eligible for plan in manifest.algebra_native_plans
    )
    assert all(
        not plan.native_receipts_present for plan in manifest.algebra_native_plans
    )
    assert all(
        plan.qualification_status == "planned_not_executed"
        for plan in manifest.algebra_native_plans
    )
    webwork_sources = "\n".join(plan.source for plan in plans_by_engine["webwork"])
    assert "Formula(" in webwork_sources
    assert "Set(" in webwork_sources
    assert "Real(" in webwork_sources
    assert {plan.compiler_version for plan in manifest.algebra_native_plans} == {
        "assessment-computation-typed-ast-v0",
        "assessment-computation-algebra-native-qualification-v0",
    }
    assert all(
        plan.source_contract
        == (
            "qualification_only_solution_set_v0"
            if plan.answer_kind == "solution_set"
            else "production_typed_ast_v0"
        )
        for plan in manifest.algebra_native_plans
    )
    imathas_sources = {
        plan.answer_kind: json.loads(plan.source) for plan in plans_by_engine["imathas"]
    }
    assert imathas_sources["formula"]["grader"] == "native_symbolic_equivalence_v0"
    assert imathas_sources["solution_set"]["grader"] == "native_solution_set_v0"
    assert imathas_sources["solution_set"]["qualification_only"] is True
    assert "qualification_only" not in imathas_sources["numeric"]


def test_algebra_native_execution_plan_is_sealed_and_nonproduction() -> None:
    manifest = build_computation_evaluation_manifest()
    first = build_algebra_native_execution_plan(
        run_id="synthetic-unit-test-only",
        imathas_namespace="acv0-canary-synthetic-test",
        manifest=manifest,
    )
    second = build_algebra_native_execution_plan(
        run_id="synthetic-unit-test-only",
        imathas_namespace="acv0-canary-synthetic-test",
        manifest=manifest,
    )

    assert first == second
    assert len(first) == 40
    assert all(request.qualification_only for request in first)
    assert all(
        request.imathas_namespace is None
        for request in first
        if request.engine == "webwork"
    )
    assert all(
        request.imathas_namespace == "acv0-canary-synthetic-test"
        for request in first
        if request.engine == "imathas"
    )


def test_every_positive_surface_has_semantic_and_safety_mutations() -> None:
    manifest = build_computation_evaluation_manifest()
    positive_ids = {case.fixture_id for case in manifest.computation_cases} | {
        case.fixture_id for case in manifest.engine_twins
    }

    assert len(positive_ids) == 100
    for fixture_id in positive_ids:
        mutations = [
            mutation
            for mutation in manifest.mutations
            if mutation.parent_fixture_id == fixture_id
        ]
        assert {mutation.kind for mutation in mutations} == {
            MutationKind.SEMANTIC,
            MutationKind.SAFETY_BOUNDARY,
        }
        assert all(mutation.critical for mutation in mutations)
        assert all(
            mutation.review_status == "awaiting_sme_review" for mutation in mutations
        )


def test_seed_plans_bind_four_thousand_plus_four_thousand_without_execution() -> None:
    manifest = build_computation_evaluation_manifest()
    summaries = {plan.plan_id: plan for plan in manifest.seed_plans}
    plan = build_computation_seed_plan(manifest=manifest)

    assert summaries["build08-compatibility"].planned_executions == 4_000
    assert summaries["assessment-computation-v0"].planned_executions == 4_000
    assert summaries["build08-compatibility"].plan_sha256 != (
        summaries["assessment-computation-v0"].plan_sha256
    )
    assert len(plan) == EXECUTIONS_PER_PLAN
    assert Counter(case.engine for case in plan) == {
        "webwork": 2_000,
        "imathas": 2_000,
    }
    assert {case.seed for case in plan} == set(range(1, 101))
    assert all(case.runtime_oracle == "native_engine" for case in plan)
    assert all(
        case.expected_answer_source == "engine_observed_parameters" for case in plan
    )
    assert all(case.execution_status == "planned_not_executed" for case in plan)


def test_cli_writes_manifest_and_planned_seed_ledger(tmp_path) -> None:
    manifest_path = tmp_path / "computation-manifest.json"
    seed_path = tmp_path / "computation-seeds.jsonl"

    assert (
        evaluation_main(["build-computation-fixtures", "--output", str(manifest_path)])
        == 0
    )
    assert (
        evaluation_main(
            [
                "build-computation-seed-plan",
                "--output",
                str(seed_path),
                "--run-id",
                "test-planned-run",
            ]
        )
        == 0
    )

    manifest = ComputationEvaluationManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    assert manifest.provenance.review_status == "awaiting_sme_review"
    lines = seed_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4_000
    assert {
        json.loads(lines[index])["execution_status"] for index in (0, len(lines) - 1)
    } == {"planned_not_executed"}


def test_acceptance_metrics_keep_unexecuted_spike_fail_closed() -> None:
    manifest = build_computation_evaluation_manifest()
    observations = _passing_observations(manifest)

    metrics = calculate_acceptance_metrics(observations, manifest=manifest)

    assert not metrics.fixture_gate_passed
    assert not metrics.engine_gate_passed
    assert not metrics.paired_study_gate_passed
    assert not metrics.sme_review_gate_passed
    assert not metrics.ucum_gate_passed
    assert not metrics.safety_gate_passed
    assert not metrics.promotion_gate_passed
    assert not metrics.evidence_bound
    assert not metrics.spike_passed
    assert metrics.materially_bad_validated_count == 0
    assert metrics.critical_mutation_recall == 1
    assert metrics.supported_coverage == 1


def test_acceptance_metrics_reject_passing_aggregate_assertions() -> None:
    manifest = build_computation_evaluation_manifest()
    metrics = calculate_acceptance_metrics(
        _passing_observations(manifest),
        manifest=manifest,
        engine_execution=EngineExecutionSummary(
            build08_executed=4_000,
            build08_passed=4_000,
            computation_executed=4_000,
            computation_passed=4_000,
        ),
        paired_study=PairedStudySummary(
            control_count=50,
            treatment_count=50,
            treatment_correct_without_edit_rate=0.96,
            control_defect_rate=0.20,
            treatment_defect_rate=0.10,
            control_median_correction_seconds=100,
            treatment_median_correction_seconds=79,
            source_grounding_delta=-0.05,
            pedagogy_delta=0,
            reviewer_approval_complete=True,
        ),
    )

    assert metrics == AcceptanceMetrics.model_validate(metrics.model_dump())
    assert not metrics.fixture_gate_passed
    assert not metrics.engine_gate_passed
    assert not metrics.paired_study_gate_passed
    assert not metrics.promotion_gate_passed
    assert not metrics.spike_passed
    assert any(
        "aggregate execution or study summaries are descriptive only" in failure
        for failure in metrics.failures
    )


def test_acceptance_metrics_reject_false_validation_and_missed_mutation() -> None:
    manifest = build_computation_evaluation_manifest()
    observations = _passing_observations(manifest)
    first_mutation = next(
        index
        for index, observation in enumerate(observations)
        if observation.kind != MutationKind.POSITIVE
    )
    observations[first_mutation] = observations[first_mutation].model_copy(
        update={
            "state": ObservedValidationState.VALIDATED,
            "critical_defect_detected": False,
        }
    )

    metrics = calculate_acceptance_metrics(observations, manifest=manifest)

    assert not metrics.fixture_gate_passed
    assert metrics.materially_bad_validated_count == 1
    assert metrics.critical_mutation_recall < 1
    assert any("materially incorrect" in failure for failure in metrics.failures)
    assert any("critical mutation" in failure for failure in metrics.failures)


def test_ucum_qualified_report_cannot_omit_raw_case_receipts() -> None:
    payload = qualify_ucum_subset().model_dump(mode="json")
    payload.update(
        {
            "runtime_integrity_status": "verified",
            "artifact_status": "checksum_verified",
            "artifact_identity": "pinned_ucum_java_mirror",
            "artifact_byte_count": 36_782,
            "artifact_sha256_expected": (
                computation_evaluation.PINNED_UCUM_FUNCTIONAL_TEST_SHA256
            ),
            "artifact_sha256_observed": (
                computation_evaluation.PINNED_UCUM_FUNCTIONAL_TEST_SHA256
            ),
            "official_cases_discovered": 573,
            "subset_cases_selected": (computation_evaluation.PINNED_UCUM_SUBSET_CASES),
            "subset_cases_executed": (computation_evaluation.PINNED_UCUM_SUBSET_CASES),
            "subset_cases_passed": (computation_evaluation.PINNED_UCUM_SUBSET_CASES),
            "official_functional_tests_status": "subset_passed",
            "libretexts_unit_corpus_passed": 20,
            "qualification_status": "passed",
            "subset_qualified": True,
            "official_attachment_equivalence": "verified",
            "case_receipts": [],
        }
    )
    payload.pop("report_sha256")
    payload["report_sha256"] = computation_evaluation._sha256_json(payload)

    with pytest.raises(ValueError, match="every unique passed case receipt"):
        computation_evaluation.UcumQualificationReport.model_validate(payload)


def test_sme_review_ledger_is_append_only_and_fail_closed_when_absent() -> None:
    manifest = build_computation_evaluation_manifest()

    report = validate_sme_review_records([], manifest=manifest)

    assert report.execution_status == "not_run"
    assert report.expected_targets == 280
    assert report.reviewed_targets == 0
    assert report.missing_targets == 280
    assert not report.qualified


def test_sme_review_record_is_bound_to_exact_fixture_hash() -> None:
    manifest = build_computation_evaluation_manifest()
    case = manifest.computation_cases[0]
    content = {
        "schema_version": "assessment-computation-sme-review-v0",
        "record_id": "review-001",
        "manifest_sha256": manifest.manifest_sha256,
        "reviewer_subject_sha256": "1" * 64,
        "reviewer_attestation_sha256": "2" * 64,
        "target_type": "computation_fixture",
        "target_id": case.fixture_id,
        "target_sha256": "0" * 64,
        "decision": "approved",
        "rationale": "Independent subject-matter review approved this case.",
    }
    content["record_sha256"] = computation_evaluation._sha256_json(content)
    record = SmeReviewRecord.model_validate(content)

    report = validate_sme_review_records([record], manifest=manifest)

    assert report.execution_status == "failed"
    assert report.unexpected_targets == 1
    assert report.approved_targets == 0
    assert not report.qualified


def test_sme_review_qualification_binds_raw_ledger_and_unique_attestations(
    tmp_path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    records: list[SmeReviewRecord] = []
    for index, case in enumerate(manifest.computation_cases[:2], start=1):
        content = {
            "schema_version": "assessment-computation-sme-review-v0",
            "record_id": f"review-{index:03d}",
            "manifest_sha256": manifest.manifest_sha256,
            "reviewer_subject_sha256": "1" * 64,
            "reviewer_attestation_sha256": "2" * 64,
            "target_type": "computation_fixture",
            "target_id": case.fixture_id,
            "target_sha256": case.fixture_sha256,
            "decision": "approved",
            "rationale": "Independent subject-matter review approved this case.",
        }
        content["record_sha256"] = computation_evaluation._sha256_json(content)
        records.append(SmeReviewRecord.model_validate(content))
    ledger_path = tmp_path / "sme-reviews.jsonl"
    ledger_path.write_text(
        "".join(record.model_dump_json() + "\n" for record in records),
        encoding="utf-8",
    )

    loaded = load_sme_review_records(ledger_path)
    report = validate_sme_review_records(loaded, manifest=manifest)

    assert report.raw_ledger_bound
    assert (
        report.raw_review_ledger_sha256
        == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    )
    assert report.unique_record_hashes == 2
    assert report.unique_reviewer_attestation_hashes == 1
    assert report.execution_status == "failed"
    assert not report.qualified


def test_paired_study_qualification_recomputes_raw_two_reviewer_metrics() -> None:
    ledger = _build_passing_paired_study_ledger()

    report = validate_paired_study_evidence(ledger)

    assert report.concept_count == 50
    assert report.draft_count == 100
    assert report.provider_call_count == 100
    assert report.reviewer_count == 2
    assert report.record_count == 200
    assert report.treatment_correct_without_edit_rate == 1
    assert report.treatment_defect_rate == 0
    assert report.control_defect_rate == 1
    assert report.treatment_median_correction_seconds == 70
    assert report.control_median_correction_seconds == 100
    assert report.execution_status == "passed"
    assert report.qualified


def test_paired_study_rejects_review_detached_from_exact_arm_draft() -> None:
    ledger = _build_passing_paired_study_ledger()
    payload = ledger.model_dump(mode="json")
    payload["records"][0]["draft_sha256"] = "f" * 64
    record = payload["records"][0]
    record["record_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )
    payload["records_sha256"] = computation_evaluation._sha256_json(payload["records"])
    payload["ledger_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in payload.items() if key != "ledger_sha256"}
    )
    tampered = PairedStudyEvidenceLedger.model_validate(payload)

    report = validate_paired_study_evidence(tampered)

    assert report.execution_status == "failed"
    assert not report.qualified
    assert any("exact arm draft" in failure for failure in report.failures)


def test_safety_evidence_requires_independent_receipt_for_each_invariant() -> None:
    evidence = _build_synthetic_safety_evidence()
    payload = evidence.model_dump(mode="json")
    payload["monitor_receipts"][1] = payload["monitor_receipts"][0]
    payload["monitor_receipts_sha256"] = computation_evaluation._sha256_json(
        payload["monitor_receipts"]
    )
    payload["evidence_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in payload.items() if key != "evidence_sha256"}
    )

    with pytest.raises(ValueError, match="every stopping invariant"):
        SpikeSafetyEvidence.model_validate(payload)


def test_offline_runner_materializes_and_detects_all_critical_mutations() -> None:
    manifest = build_computation_evaluation_manifest()
    report = run_offline_mutation_qualification()

    assert report.execution_status == "executed_offline"
    assert report.planned_mutations == 200
    assert report.executed_mutations == 200
    assert report.detected_mutations == 200
    assert report.all_critical_mutations_detected
    assert report.network_calls == 0
    assert report.native_engine_executions == 0
    assert len({receipt.mutation_id for receipt in report.receipts}) == 200
    assert Counter(receipt.detection_layer.value for receipt in report.receipts) == {
        "typed_schema": 100,
        "typed_validator": 60,
        "sealed_hash_binding": 40,
    }
    assert all(receipt.detected for receipt in report.receipts)
    expected_payload_hashes = {
        mutation.mutation_id: mutation.expected_mutated_payload_sha256
        for mutation in manifest.mutations
    }
    assert all(
        receipt.mutated_payload_sha256 == expected_payload_hashes[receipt.mutation_id]
        for receipt in report.receipts
    )
    assert all(
        receipt.observed_state
        in {
            ObservedValidationState.VALIDATION_FAILED,
            ObservedValidationState.UNSUPPORTED,
        }
        for receipt in report.receipts
    )


def test_native_receipt_validator_is_not_run_without_receipts() -> None:
    report = validate_native_qualification_receipts([])

    assert report.execution_status == "not_run"
    assert report.imported_receipts == 0
    assert report.imported_execution_claims == 0
    assert report.missing_receipts == 4_000
    assert not report.qualified


def test_algebra_native_receipt_gate_is_not_run_without_receipts() -> None:
    report = validate_algebra_native_qualification_receipts([])

    assert report.execution_status == "not_run"
    assert report.imported_receipts == 0
    assert report.missing_receipts == 40
    assert not report.qualified
    assert not report.production_delivery_enabled


def test_algebra_native_receipt_gate_requires_all_forty_bound_receipts(
    tmp_path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    requests = build_algebra_native_execution_plan(
        run_id="synthetic-unit-test-only",
        imathas_namespace="acv0-canary-synthetic-test",
        manifest=manifest,
    )
    receipts = [
        _build_test_algebra_native_receipt(request, index=index)
        for index, request in enumerate(requests, start=1)
    ]
    ledger = tmp_path / "algebra-native-receipts.jsonl"
    ledger.write_text(
        "".join(receipt.model_dump_json() + "\n" for receipt in receipts),
        encoding="utf-8",
    )
    policy = _build_test_native_trust_policy(
        hashlib.sha256(ledger.read_bytes()).hexdigest()
    )

    report = validate_algebra_native_qualification_receipts(
        load_algebra_native_qualification_receipts(ledger),
        manifest=manifest,
        trust_policy=policy,
    )

    assert report.execution_status == "passed"
    assert report.valid_receipts == 40
    assert report.missing_receipts == 0
    assert report.qualified
    assert not report.production_delivery_enabled
    assert report.issues == []


def test_algebra_native_receipt_gate_rejects_native_grading_mismatch() -> None:
    manifest = build_computation_evaluation_manifest()
    request = build_algebra_native_execution_plan(
        run_id="synthetic-unit-test-only",
        imathas_namespace="acv0-canary-synthetic-test",
        manifest=manifest,
    )[0]
    receipt = _build_test_algebra_native_receipt(request, index=1)
    payload = receipt.model_dump(mode="json")
    payload["correct_answer_accepted"] = False
    payload["receipt_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )
    rejected = AlgebraNativeQualificationReceipt.model_validate(payload)

    report = validate_algebra_native_qualification_receipts(
        [rejected],
        manifest=manifest,
        trust_policy=_build_test_native_trust_policy(),
    )

    assert report.execution_status == "failed"
    assert report.valid_receipts == 0
    assert "native_grading_mismatch" in {issue.code for issue in report.issues}
    assert not report.qualified


def test_native_receipt_binds_exact_typed_lineage_and_stays_partial(
    tmp_path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    receipt = _build_test_native_receipt(manifest)
    ledger = tmp_path / "native-receipts.jsonl"
    ledger.write_text(receipt.model_dump_json() + "\n", encoding="utf-8")
    trust_policy = _build_test_native_trust_policy(
        hashlib.sha256(ledger.read_bytes()).hexdigest()
    )

    imported = load_native_qualification_receipts(ledger)
    report = validate_native_qualification_receipts(
        imported,
        manifest=manifest,
        trust_policy=trust_policy,
    )

    assert len(imported) == 1
    assert report.execution_status == "partial"
    assert report.valid_receipts == 1
    assert report.missing_receipts == 3_999
    assert (
        report.raw_receipt_ledger_sha256
        == hashlib.sha256(ledger.read_bytes()).hexdigest()
    )
    assert report.issues == []
    assert not report.qualified


def test_native_receipt_policy_rejects_reformatted_raw_ledger(tmp_path) -> None:
    manifest = build_computation_evaluation_manifest()
    receipt = _build_test_native_receipt(manifest)
    original = tmp_path / "native-original.jsonl"
    original.write_text(receipt.model_dump_json() + "\n", encoding="utf-8")
    policy = _build_test_native_trust_policy(
        hashlib.sha256(original.read_bytes()).hexdigest()
    )
    reformatted = tmp_path / "native-reformatted.jsonl"
    reformatted.write_text(
        json.dumps(receipt.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    report = validate_native_qualification_receipts(
        load_native_qualification_receipts(reformatted),
        manifest=manifest,
        trust_policy=policy,
    )

    assert report.execution_status == "failed"
    assert "raw_receipt_ledger_hash_mismatch" in {issue.code for issue in report.issues}
    assert not report.qualified


def test_native_receipt_rejects_rehashed_source_tampering() -> None:
    manifest = build_computation_evaluation_manifest()
    receipt = _build_test_native_receipt(manifest)
    trust_policy = _build_test_native_trust_policy()
    payload = receipt.model_dump(mode="json")
    payload["source_sha256"] = "0" * 64
    payload["receipt_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )
    tampered = NativeQualificationReceipt.model_validate(payload)

    report = validate_native_qualification_receipts(
        [tampered],
        manifest=manifest,
        trust_policy=trust_policy,
    )

    assert report.execution_status == "failed"
    assert report.valid_receipts == 0
    assert report.invalid_receipts == 1
    assert "compiler_source_binding_mismatch" in {issue.code for issue in report.issues}
    assert "raw_receipt_ledger_unbound" in {issue.code for issue in report.issues}
    assert not report.qualified


def test_native_receipts_cannot_qualify_without_operator_pinned_policy() -> None:
    manifest = build_computation_evaluation_manifest()
    receipt = _build_test_native_receipt(manifest)

    report = validate_native_qualification_receipts([receipt], manifest=manifest)

    assert report.execution_status == "failed"
    assert not report.trust_policy_applied
    assert report.trust_policy_sha256 is None
    assert "operator_trust_policy_missing" in {issue.code for issue in report.issues}
    assert not report.qualified


def test_native_receipt_must_match_operator_pinned_image_digest() -> None:
    manifest = build_computation_evaluation_manifest()
    receipt = _build_test_native_receipt(manifest)
    policy_payload = _build_test_native_trust_policy().model_dump(mode="json")
    policy_payload["webwork_engine_image_digest"] = f"sha256:{'9' * 64}"
    policy_payload["policy_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in policy_payload.items() if key != "policy_sha256"}
    )
    policy = NativeQualificationTrustPolicy.model_validate(policy_payload)

    report = validate_native_qualification_receipts(
        [receipt],
        manifest=manifest,
        trust_policy=policy,
    )

    assert report.execution_status == "failed"
    assert "operator_trust_policy_binding_mismatch" in {
        issue.code for issue in report.issues
    }
    assert not report.qualified


def test_native_receipt_self_hash_binds_engine_observed_values() -> None:
    manifest = build_computation_evaluation_manifest()
    receipt = _build_test_native_receipt(manifest)
    payload = receipt.model_dump(mode="json")
    first_name = next(iter(payload["engine_observed_values"]))
    payload["engine_observed_values"][first_name] = {
        "kind": "integer",
        "integer": 999,
        "decimal": None,
    }

    with pytest.raises(ValueError, match="engine_observed_values_sha256"):
        NativeQualificationReceipt.model_validate(payload)


def test_imathas_receipt_requires_disposable_namespace_identity() -> None:
    manifest = build_computation_evaluation_manifest()
    payload = _build_test_native_receipt(manifest).model_dump(mode="json")
    payload["item_id"] = payload["item_id"].replace("webwork", "imathas")
    payload["engine"] = "imathas"
    payload["adapter_image_digest"] = f"sha256:{'5' * 64}"
    payload["receipt_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )

    with pytest.raises(ValueError, match="namespace"):
        NativeQualificationReceipt.model_validate(payload)


def test_native_policy_cannot_claim_pending_imathas_cleanup() -> None:
    payload = _build_test_native_trust_policy().model_dump(mode="json")
    payload["imathas_namespace_cleanup_status"] = "pending"
    payload["policy_sha256"] = computation_evaluation._sha256_json(
        {key: value for key, value in payload.items() if key != "policy_sha256"}
    )

    with pytest.raises(ValueError, match="verified"):
        NativeQualificationTrustPolicy.model_validate(payload)


def test_ucum_harness_is_not_run_without_artifact_and_runs_local_corpus() -> None:
    report = qualify_ucum_subset()

    assert report.artifact_status == "not_supplied"
    assert report.artifact_identity == "absent"
    assert report.official_functional_tests_status == "not_run"
    assert report.subset_cases_executed == 0
    assert report.libretexts_unit_corpus_passed == 20
    assert report.runtime_integrity_status == "verified"
    assert report.qualification_status == "not_run"
    assert not report.subset_qualified
    assert not report.official_functional_test_conformance_claimed
    assert not report.full_ucum_conformance_claimed


def test_ucum_harness_checksums_and_executes_only_local_named_subset(
    tmp_path,
) -> None:
    artifact = tmp_path / "UcumFunctionalTests.xml"
    raw = (
        b"<ucumTests>"
        b'<validation><case id="v1" unit="m" valid="true"/>'
        b'<case id="v2" unit="ft" valid="false"/></validation>'
        b'<conversion><case id="c1" value="1" srcUnit="m" '
        b'dstUnit="cm" outcome="100"/>'
        b'<case id="3-111a" value="6.3" srcUnit="s/m.mg" '
        b'dstUnit="s.m-1.g" outcome="0.0063"/></conversion>'
        b"</ucumTests>"
    )
    artifact.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    report = qualify_ucum_subset(artifact, expected_sha256=digest)

    assert report.artifact_status == "checksum_verified"
    assert report.artifact_identity == "unrecognized_checksum_pinned_artifact"
    assert report.official_cases_discovered == 4
    assert report.subset_cases_selected == 4
    assert report.subset_cases_executed == 4
    assert report.subset_cases_passed == 4
    assert report.qualification_status == "partial"
    assert not report.subset_qualified
    assert all(case.status == "passed" for case in report.case_receipts)
    left_associative = next(
        case for case in report.case_receipts if "3-111a" in case.test_id
    )
    assert left_associative.observed_value == "0.0063"

    mismatch = qualify_ucum_subset(artifact, expected_sha256="0" * 64)
    assert mismatch.artifact_status == "checksum_mismatch"
    assert mismatch.subset_cases_executed == 0
    assert mismatch.qualification_status == "failed"
    assert not mismatch.subset_qualified


def test_ucum_pinned_artifact_selection_contract_is_exact_and_offline(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sections = {
        "validation": [
            *[
                ElementTree.Element(
                    "case",
                    {"id": f"selected-v-{index}", "unit": "m", "valid": "true"},
                )
                for index in range(173)
            ],
            *[
                ElementTree.Element(
                    "case",
                    {"id": f"excluded-v-{index}", "unit": "ft", "valid": "true"},
                )
                for index in range(356)
            ],
        ],
        "displaynamegeneration": [
            ElementTree.Element("case", {"id": f"display-{index}"})
            for index in range(9)
        ],
        "conversion": [
            *[
                ElementTree.Element(
                    "case",
                    {
                        "id": f"selected-c-{index}",
                        "value": "1",
                        "srcUnit": "m",
                        "dstUnit": "cm",
                        "outcome": "100",
                    },
                )
                for index in range(17)
            ],
            *[
                ElementTree.Element(
                    "case",
                    {
                        "id": f"excluded-c-{index}",
                        "value": "1",
                        "srcUnit": "ft",
                        "dstUnit": "in",
                        "outcome": "12",
                    },
                )
                for index in range(13)
            ],
        ],
        "multiplication": [
            ElementTree.Element("case", {"id": f"multiply-{index}"})
            for index in range(2)
        ],
        "division": [
            ElementTree.Element("case", {"id": f"divide-{index}"}) for index in range(3)
        ],
    }
    root = ElementTree.Element("ucumTests")
    for section_name, cases in sections.items():
        section = ElementTree.SubElement(root, section_name)
        section.extend(cases)
    raw = ElementTree.tostring(root, encoding="utf-8")
    artifact = tmp_path / "synthetic-pinned-contract.xml"
    artifact.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        computation_evaluation,
        "PINNED_UCUM_FUNCTIONAL_TEST_SHA256",
        digest,
    )
    monkeypatch.setattr(
        computation_evaluation,
        "PINNED_UCUM_FUNCTIONAL_TEST_BYTES",
        len(raw),
    )

    report = qualify_ucum_subset(artifact, expected_sha256=digest)

    assert report.artifact_identity == "pinned_ucum_java_mirror"
    assert report.official_cases_discovered == 573
    assert report.subset_cases_selected == 190
    assert report.subset_cases_executed == 190
    assert report.subset_cases_passed == 190
    assert Counter(receipt.case_kind for receipt in report.case_receipts) == {
        "validation": 173,
        "conversion": 17,
    }
    assert report.official_functional_tests_status == "subset_passed"
    assert report.qualification_status == "partial"
    assert not report.subset_qualified


def test_ucum_harness_rejects_entity_bearing_xml_before_execution(tmp_path) -> None:
    artifact = tmp_path / "unsafe.xml"
    raw = b'<!DOCTYPE x [<!ENTITY probe "blocked">]><ucumTests />'
    artifact.write_bytes(raw)

    report = qualify_ucum_subset(
        artifact,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )

    assert report.artifact_status == "invalid"
    assert report.subset_cases_executed == 0
    assert report.official_functional_tests_status == "not_run"
    assert not report.subset_qualified


def test_qualification_cli_preserves_not_run_statuses(tmp_path) -> None:
    native_ledger = tmp_path / "native.jsonl"
    native_ledger.write_text("", encoding="utf-8")
    native_report = tmp_path / "native-report.json"
    trust_policy_path = tmp_path / "native-trust-policy.json"
    ucum_report = tmp_path / "ucum-report.json"
    trust_policy_path.write_text(
        _build_test_native_trust_policy().model_dump_json(),
        encoding="utf-8",
    )
    assert (
        load_native_qualification_trust_policy(trust_policy_path).policy_sha256
        == _build_test_native_trust_policy().policy_sha256
    )

    assert (
        evaluation_main(
            [
                "validate-computation-native-receipts",
                str(native_ledger),
                "--trust-policy",
                str(trust_policy_path),
                "--output",
                str(native_report),
            ]
        )
        == 1
    )
    assert (
        evaluation_main(["qualify-computation-ucum", "--output", str(ucum_report)]) == 1
    )
    assert json.loads(native_report.read_text())["execution_status"] == "not_run"
    assert json.loads(ucum_report.read_text())["qualification_status"] == "not_run"


def _assert_distribution(cases: list[object]) -> None:
    assert Counter(case.difficulty for case in cases) == {
        FixtureDifficulty.BASIC: 8,
        FixtureDifficulty.INTERMEDIATE: 8,
        FixtureDifficulty.BOUNDARY: 4,
    }
    assert Counter(case.split for case in cases) == {
        FixtureSplit.DEVELOPMENT: 14,
        FixtureSplit.SEALED: 6,
    }


def _passing_observations(
    manifest: ComputationEvaluationManifest,
) -> list[ComputationEvaluationObservation]:
    positives = [
        ComputationEvaluationObservation(
            case_id=case.fixture_id,
            surface=EvaluationSurface(case.family),
            kind=MutationKind.POSITIVE,
            state=ObservedValidationState.VALIDATED,
            oracle_match=True,
            critical_defect_detected=False,
            deterministic_replay=True,
        )
        for case in manifest.computation_cases
    ] + [
        ComputationEvaluationObservation(
            case_id=case.fixture_id,
            surface=EvaluationSurface(case.engine),
            kind=MutationKind.POSITIVE,
            state=ObservedValidationState.VALIDATED,
            oracle_match=True,
            critical_defect_detected=False,
            deterministic_replay=True,
        )
        for case in manifest.engine_twins
    ]
    mutations = [
        ComputationEvaluationObservation(
            case_id=case.mutation_id,
            surface=case.surface,
            kind=case.kind,
            state=case.expected_states[0],
            oracle_match=False,
            critical_defect_detected=True,
            deterministic_replay=True,
        )
        for case in manifest.mutations
    ]
    return positives + mutations


def _expected_float(value: str | None) -> float:
    if value is None:
        raise AssertionError("numeric fixture is missing its synthetic oracle")
    if value == "pi":
        return math.pi
    if value == "2*pi":
        return 2 * math.pi
    if "/" in value:
        numerator, denominator = value.split("/", maxsplit=1)
        return int(numerator) / int(denominator)
    return float(value)


def _build_test_algebra_native_receipt(
    request: AlgebraNativeExecutionRequest,
    *,
    index: int,
) -> AlgebraNativeQualificationReceipt:
    plan_digest = hashlib.sha256(request.plan_id.encode()).hexdigest()
    content = {
        "schema_version": "assessment-computation-algebra-native-receipt-v0",
        "qualification_only": True,
        "run_id": request.run_id,
        "endpoint_profile": "isolated_canary_v0",
        "manifest_sha256": request.manifest_sha256,
        "request_sha256": request.request_sha256,
        "plan_id": request.plan_id,
        "plan_sha256": request.plan_sha256,
        "fixture_id": request.fixture_id,
        "fixture_sha256": request.fixture_sha256,
        "engine": request.engine,
        "operation": request.operation,
        "answer_kind": request.answer_kind,
        "source_contract": request.source_contract,
        "compiler_version": request.compiler_version,
        "source_sha256": request.source_sha256,
        "correct_submission_sha256": hashlib.sha256(
            request.correct_submission.encode()
        ).hexdigest(),
        "alternate_correct_submission_sha256": (
            hashlib.sha256(request.alternate_correct_submission.encode()).hexdigest()
            if request.alternate_correct_submission is not None
            else None
        ),
        "wrong_submission_sha256": hashlib.sha256(
            request.wrong_submission.encode()
        ).hexdigest(),
        "engine_image_digest": (
            f"sha256:{'1' * 64}"
            if request.engine == "webwork"
            else f"sha256:{'4' * 64}"
        ),
        "adapter_image_digest": (
            f"sha256:{'5' * 64}" if request.engine == "imathas" else None
        ),
        "imathas_namespace": request.imathas_namespace,
        "imathas_object_id": (
            f"synthetic-algebra-object-{index:02d}"
            if request.engine == "imathas"
            else None
        ),
        "network_attestation_sha256": "2" * 64,
        "raw_engine_evidence_sha256": plan_digest,
        "constraints_satisfied": True,
        "correct_answer_accepted": True,
        "alternate_correct_answer_accepted": (
            None if request.answer_kind == "numeric" else True
        ),
        "wrong_answer_rejected": True,
        "rendered": True,
        "render_sha256": plan_digest,
        "repeat_render_sha256": plan_digest,
        "warnings": [],
        "errors": [],
        "outbound_request_count": 0,
        "execution_status": "executed",
    }
    content["receipt_sha256"] = computation_evaluation._sha256_json(content)
    return AlgebraNativeQualificationReceipt.model_validate(content)


def _build_test_native_receipt(
    manifest: ComputationEvaluationManifest,
) -> NativeQualificationReceipt:
    twin = next(case for case in manifest.engine_twins if case.engine == "webwork")
    lineage = next(
        case
        for case in manifest.parameterized_lineages
        if case.lineage_id == twin.lineage_id
    )
    preview = compile_parameterized_item(
        twin.parameterized_spec,
        validation_seeds=1,
        validation_seed_values=[1],
    ).previews[0]
    observed_values = {
        name: (
            {"kind": "integer", "integer": int(value), "decimal": None}
            if isinstance(value, int)
            else {
                "kind": "decimal",
                "integer": None,
                "decimal": computation_evaluation._canonical_decimal(value),
            }
        )
        for name, value in preview.variables.items()
    }
    correct_answer = float(preview.answer)
    wrong_answer = correct_answer + max(twin.parameterized_spec.tolerance * 2, 1)
    plan_sha256 = next(
        plan.plan_sha256
        for plan in manifest.seed_plans
        if plan.plan_id == "assessment-computation-v0"
    )
    content = {
        "schema_version": "assessment-computation-native-receipt-v0",
        "run_id": "synthetic-unit-test-only",
        "endpoint_profile": "isolated_canary_v0",
        "manifest_sha256": manifest.manifest_sha256,
        "plan_sha256": plan_sha256,
        "item_id": twin.fixture_id,
        "fixture_sha256": twin.fixture_sha256,
        "lineage_id": twin.lineage_id,
        "lineage_sha256": lineage.lineage_sha256,
        "lineage_blueprint_sha256": computation_evaluation.canonical_blueprint_hash(
            build_lineage_blueprint(twin, lineage)
        ),
        "engine": twin.engine,
        "answer_kind": twin.answer_kind,
        "seed": 1,
        "compiler_version": twin.compiler_version,
        "source_sha256": twin.source_sha256,
        "engine_image_digest": f"sha256:{'1' * 64}",
        "adapter_image_digest": None,
        "imathas_namespace": None,
        "imathas_object_id": None,
        "network_attestation_sha256": "2" * 64,
        "engine_observed_values": observed_values,
        "engine_observed_values_sha256": computation_evaluation._sha256_json(
            observed_values
        ),
        "engine_observed_correct_answer": (
            computation_evaluation._canonical_decimal(correct_answer)
        ),
        "engine_observed_wrong_answer": (
            computation_evaluation._canonical_decimal(wrong_answer)
        ),
        "constraints_satisfied": True,
        "correct_answer_accepted": True,
        "wrong_answer_rejected": True,
        "rendered": True,
        "render_sha256": "3" * 64,
        "repeat_render_sha256": "3" * 64,
        "warnings": [],
        "errors": [],
        "outbound_request_count": 0,
        "execution_status": "executed",
    }
    content["receipt_sha256"] = computation_evaluation._sha256_json(content)
    return NativeQualificationReceipt.model_validate(content)


def _build_test_native_trust_policy(
    raw_receipt_ledger_sha256: str = "0" * 64,
) -> NativeQualificationTrustPolicy:
    content = {
        "schema_version": "assessment-computation-native-trust-policy-v0",
        "run_id": "synthetic-unit-test-only",
        "endpoint_profile": "isolated_canary_v0",
        "webwork_engine_image_digest": f"sha256:{'1' * 64}",
        "imathas_engine_image_digest": f"sha256:{'4' * 64}",
        "imathas_adapter_image_digest": f"sha256:{'5' * 64}",
        "receipt_ledger_raw_sha256": raw_receipt_ledger_sha256,
        "imathas_namespace": "acv0-canary-synthetic-test",
        "imathas_namespace_disposable": True,
        "imathas_namespace_created_attestation_sha256": "7" * 64,
        "imathas_namespace_cleanup_status": "verified",
        "imathas_namespace_cleanup_attestation_sha256": "8" * 64,
        "network_attestation_sha256": "2" * 64,
        "operator_attestation_sha256": "6" * 64,
    }
    content["policy_sha256"] = computation_evaluation._sha256_json(content)
    return NativeQualificationTrustPolicy.model_validate(content)


def _build_passing_paired_study_ledger() -> PairedStudyEvidenceLedger:
    reviewers = [
        hashlib.sha256(b"reviewer-a").hexdigest(),
        hashlib.sha256(b"reviewer-b").hexdigest(),
    ]
    provider_settings_sha256 = hashlib.sha256(b"fixed-provider-settings").hexdigest()
    drafts: list[PairedDraftEvidence] = []
    draft_by_key: dict[tuple[str, str], PairedDraftEvidence] = {}
    for index in range(50):
        concept_sha256 = hashlib.sha256(f"sealed-{index}".encode()).hexdigest()
        source_sha256 = hashlib.sha256(f"source-{index}".encode()).hexdigest()
        for arm in ("control", "treatment"):
            draft_sha256 = hashlib.sha256(f"draft-{index}-{arm}".encode()).hexdigest()
            provider_receipt = hashlib.sha256(
                f"provider-{index}-{arm}".encode()
            ).hexdigest()
            content = {
                "schema_version": "assessment-computation-paired-draft-v0",
                "sealed_concept_sha256": concept_sha256,
                "arm": arm,
                "source_sha256": source_sha256,
                "draft_sha256": draft_sha256,
                "computation_blueprint_sha256": (
                    hashlib.sha256(f"blueprint-{index}".encode()).hexdigest()
                    if arm == "treatment"
                    else None
                ),
                "computation_report_sha256": (
                    hashlib.sha256(f"report-{index}".encode()).hexdigest()
                    if arm == "treatment"
                    else None
                ),
                "provider_settings_sha256": provider_settings_sha256,
                "provider_call_receipt_sha256s": [provider_receipt],
                "provider_cost_usd": "0.10",
            }
            content["evidence_sha256"] = computation_evaluation._sha256_json(content)
            draft = PairedDraftEvidence.model_validate(content)
            drafts.append(draft)
            draft_by_key[(concept_sha256, arm)] = draft
    records: list[PairedReviewRecord] = []
    for index in range(50):
        concept_sha256 = hashlib.sha256(f"sealed-{index}".encode()).hexdigest()
        for reviewer_sha256 in reviewers:
            for arm in ("control", "treatment"):
                draft = draft_by_key[(concept_sha256, arm)]
                content = {
                    "schema_version": "assessment-computation-paired-review-v0",
                    "sealed_concept_sha256": concept_sha256,
                    "arm": arm,
                    "draft_sha256": draft.draft_sha256,
                    "draft_evidence_sha256": draft.evidence_sha256,
                    "reviewer_subject_sha256": reviewer_sha256,
                    "computationally_correct_without_edit": arm == "treatment",
                    "material_computation_defect": arm == "control",
                    "correction_seconds": 100 if arm == "control" else 70,
                    "source_grounding_score": 90,
                    "pedagogy_score": 90,
                }
                content["record_sha256"] = computation_evaluation._sha256_json(content)
                records.append(PairedReviewRecord.model_validate(content))
    drafts_payload = [draft.model_dump(mode="json") for draft in drafts]
    records_payload = [record.model_dump(mode="json") for record in records]
    concepts = sorted({draft.sealed_concept_sha256 for draft in drafts})
    provider_receipts = sorted(
        receipt for draft in drafts for receipt in draft.provider_call_receipt_sha256s
    )
    content = {
        "schema_version": "assessment-computation-paired-study-v0",
        "study_id": "synthetic-paired-study-test",
        "manifest_sha256": "e" * 64,
        "candidate_image_digest": f"sha256:{'f' * 64}",
        "computation_image_digest": f"sha256:{'0' * 64}",
        "sealed_concept_set_sha256": computation_evaluation._sha256_json(concepts),
        "randomization_attestation_sha256": "2" * 64,
        "blinding_attestation_sha256": "3" * 64,
        "fixed_provider_settings_sha256": provider_settings_sha256,
        "provider_call_ledger_sha256": computation_evaluation._sha256_json(
            provider_receipts
        ),
        "provider_cost_usd": "10.00",
        "drafts": drafts_payload,
        "drafts_sha256": computation_evaluation._sha256_json(drafts_payload),
        "records": records_payload,
        "records_sha256": computation_evaluation._sha256_json(records_payload),
    }
    content["ledger_sha256"] = computation_evaluation._sha256_json(content)
    return PairedStudyEvidenceLedger.model_validate(content)


def _build_synthetic_safety_evidence() -> SpikeSafetyEvidence:
    categories = (
        "publication",
        "grading",
        "permission",
        "migration_loss",
        "network",
        "file_access",
        "process_escape",
        "cross_draft",
    )
    receipts: list[SafetyMonitorReceipt] = []
    for index, category in enumerate(categories):
        content = {
            "schema_version": "assessment-computation-safety-monitor-v0",
            "category": category,
            "evidence_source": (
                "network_monitor" if category == "network" else "canary_test_runner"
            ),
            "observation_window_sha256": hashlib.sha256(
                f"window-{index}".encode()
            ).hexdigest(),
            "raw_event_ledger_sha256": hashlib.sha256(
                f"events-{index}".encode()
            ).hexdigest(),
            "independent_observer_attestation_sha256": hashlib.sha256(
                f"observer-{index}".encode()
            ).hexdigest(),
            "observed_events": 0,
        }
        content["receipt_sha256"] = computation_evaluation._sha256_json(content)
        receipts.append(SafetyMonitorReceipt.model_validate(content))
    receipt_payload = [receipt.model_dump(mode="json") for receipt in receipts]
    content = {
        "schema_version": "assessment-computation-safety-evidence-v0",
        "run_id": "synthetic-safety-unit-test",
        "manifest_sha256": "1" * 64,
        "candidate_image_digest": f"sha256:{'2' * 64}",
        "computation_image_digest": f"sha256:{'3' * 64}",
        "network_attestation_sha256": "4" * 64,
        "cloned_database_snapshot_sha256": "5" * 64,
        "off_mode_parity_receipt_sha256": "6" * 64,
        "backup_restore_receipt_sha256": "7" * 64,
        "rollback_receipt_sha256": "8" * 64,
        "fake_publication_adapter_receipt_sha256": "9" * 64,
        "isolation_test_report_sha256": "a" * 64,
        "evidence_ledger_sha256": "b" * 64,
        "operator_attestation_sha256": "c" * 64,
        "independent_observer_attestation_sha256": "d" * 64,
        "monitor_receipts": receipt_payload,
        "monitor_receipts_sha256": computation_evaluation._sha256_json(receipt_payload),
        "execution_status": "completed",
        "publication_events": 0,
        "grading_events": 0,
        "permission_events": 0,
        "migration_loss_events": 0,
        "network_events": 0,
        "file_access_events": 0,
        "process_escape_events": 0,
        "cross_draft_events": 0,
    }
    content["evidence_sha256"] = computation_evaluation._sha256_json(content)
    return SpikeSafetyEvidence.model_validate(content)
