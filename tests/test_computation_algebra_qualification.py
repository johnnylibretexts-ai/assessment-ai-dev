from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from pathlib import Path

import pytest

from evaluation.cli import main as evaluation_main
from evaluation.computation import (
    AlgebraNativeExecutionObservation,
    AlgebraNativeExecutionRequest,
    ComputationEvaluationObservation,
    EvaluationSurface,
    MutationKind,
    NativeQualificationReport,
    NativeQualificationTrustPolicy,
    ObservedValidationState,
    QualificationMergedObservationEvidence,
    WorkflowObservationEvidence,
    build_algebra_native_execution_plan,
    build_computation_evaluation_manifest,
    build_qualification_merged_observation_evidence,
    calculate_acceptance_metrics,
    execute_algebra_native_plan,
    load_algebra_native_execution_plan,
    load_algebra_native_qualification_receipts,
    run_offline_mutation_qualification,
    validate_algebra_native_qualification_receipts,
)
from evaluation import computation as computation_evaluation


RUN_ID = "synthetic-algebra-executor-test"
NAMESPACE = "acv0-canary-synthetic-algebra"
WEBWORK_DIGEST = f"sha256:{'1' * 64}"
IMATHAS_DIGEST = f"sha256:{'4' * 64}"
ADAPTER_DIGEST = f"sha256:{'5' * 64}"
NETWORK_ATTESTATION = "2" * 64


def test_cli_builds_importable_exact_forty_request_plan(tmp_path: Path) -> None:
    output = tmp_path / "algebra-plan.jsonl"

    assert (
        evaluation_main(
            [
                "build-computation-algebra-native-plan",
                "--run-id",
                RUN_ID,
                "--imathas-namespace",
                NAMESPACE,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    loaded = load_algebra_native_execution_plan(output)
    manifest = build_computation_evaluation_manifest()
    sealed = {plan.plan_id: plan for plan in manifest.algebra_native_plans}
    assert len(loaded) == 40
    assert Counter(request.answer_kind for request in loaded) == {
        "numeric": 6,
        "formula": 22,
        "solution_set": 12,
    }
    assert all(request.source == sealed[request.plan_id].source for request in loaded)
    assert all(
        request.source_sha256 == sealed[request.plan_id].source_sha256
        for request in loaded
    )


def test_algebra_plan_loader_rejects_an_incomplete_plan(tmp_path: Path) -> None:
    output = tmp_path / "partial-plan.jsonl"
    plan = build_algebra_native_execution_plan(
        run_id=RUN_ID,
        imathas_namespace=NAMESPACE,
    )
    output.write_text(
        "".join(request.model_dump_json() + "\n" for request in plan[:-1]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly 40"):
        load_algebra_native_execution_plan(output)


def test_algebra_executor_mints_and_validates_all_forty_receipts(
    tmp_path: Path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    plan = build_algebra_native_execution_plan(
        run_id=RUN_ID,
        imathas_namespace=NAMESPACE,
        manifest=manifest,
    )
    output = tmp_path / "algebra-receipts.jsonl"

    first = asyncio.run(
        execute_algebra_native_plan(
            plan,
            output=output,
            executor=_passing_observation,
            engine="webwork",
            run_id=RUN_ID,
            webwork_engine_image_digest=WEBWORK_DIGEST,
            imathas_engine_image_digest=IMATHAS_DIGEST,
            imathas_adapter_image_digest=ADAPTER_DIGEST,
            network_attestation_sha256=NETWORK_ATTESTATION,
            imathas_namespace=NAMESPACE,
            concurrency=3,
            manifest=manifest,
        )
    )
    second = asyncio.run(
        execute_algebra_native_plan(
            plan,
            output=output,
            executor=_passing_observation,
            engine="imathas",
            run_id=RUN_ID,
            webwork_engine_image_digest=WEBWORK_DIGEST,
            imathas_engine_image_digest=IMATHAS_DIGEST,
            imathas_adapter_image_digest=ADAPTER_DIGEST,
            network_attestation_sha256=NETWORK_ATTESTATION,
            imathas_namespace=NAMESPACE,
            concurrency=3,
            manifest=manifest,
        )
    )
    resumed = asyncio.run(
        execute_algebra_native_plan(
            plan,
            output=output,
            executor=_passing_observation,
            engine="imathas",
            run_id=RUN_ID,
            webwork_engine_image_digest=WEBWORK_DIGEST,
            imathas_engine_image_digest=IMATHAS_DIGEST,
            imathas_adapter_image_digest=ADAPTER_DIGEST,
            network_attestation_sha256=NETWORK_ATTESTATION,
            imathas_namespace=NAMESPACE,
            manifest=manifest,
        )
    )

    assert first == (20, 20)
    assert second == (20, 20)
    assert resumed == (0, 0)
    loaded = load_algebra_native_qualification_receipts(output)
    policy = _trust_policy(hashlib.sha256(output.read_bytes()).hexdigest())
    report = validate_algebra_native_qualification_receipts(
        loaded,
        manifest=manifest,
        trust_policy=policy,
    )
    assert report.qualified
    assert report.valid_receipts == 40
    assert report.production_delivery_enabled is False
    assert report.issues == []

    native_report = _native_report(manifest.manifest_sha256, policy)
    metrics = calculate_acceptance_metrics(
        [],
        manifest=manifest,
        native_qualification=native_report,
        algebra_native_qualification=report,
    )
    assert not metrics.algebra_native_gate_passed
    assert not metrics.engine_gate_passed
    assert not metrics.spike_passed


def test_hash_bound_merge_makes_all_positive_coverage_reachable(
    tmp_path: Path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    mutation_report = run_offline_mutation_qualification(manifest)
    workflow = _partial_workflow_evidence(manifest, mutation_report.report_sha256)
    algebra_report = _execute_algebra_report(tmp_path, manifest)
    native_report = _native_report(
        manifest.manifest_sha256,
        _trust_policy(algebra_report.raw_receipt_ledger_sha256 or "3" * 64),
    )

    merged = build_qualification_merged_observation_evidence(
        workflow,
        native_qualification=native_report,
        algebra_native_qualification=algebra_report,
        manifest=manifest,
    )

    raw_by_id = {record.case_id: record for record in workflow.observations}
    merged_by_id = {record.case_id: record for record in merged.observations}
    upgraded = set(merged.upgraded_case_report_sha256s)
    assert len(upgraded) == 60
    assert all(
        raw_by_id[case_id].state == ObservedValidationState.PARTIALLY_VALIDATED
        for case_id in upgraded
    )
    assert all(
        merged_by_id[case_id].state == ObservedValidationState.VALIDATED
        for case_id in upgraded
    )
    assert not merged.solution_set_production_delivery_enabled

    metrics = calculate_acceptance_metrics(
        workflow.observations,
        manifest=manifest,
        workflow_evidence=workflow,
        qualification_merged_evidence=merged,
        mutation_qualification=mutation_report,
        native_qualification=native_report,
        algebra_native_qualification=algebra_report,
    )
    assert metrics.supported_coverage == 1
    assert metrics.coverage_by_surface["algebraic"] == 1
    assert metrics.coverage_by_surface["webwork"] == 1
    assert metrics.coverage_by_surface["imathas"] == 1
    assert metrics.algebra_native_gate_passed
    assert not metrics.engine_gate_passed
    assert not metrics.spike_passed

    tampered_payload = merged.model_dump(mode="json")
    tampered_payload["native_report_sha256"] = "0" * 64
    tampered_payload["evidence_sha256"] = computation_evaluation._sha256_json(
        {
            key: value
            for key, value in tampered_payload.items()
            if key != "evidence_sha256"
        }
    )
    tampered = QualificationMergedObservationEvidence.model_validate(tampered_payload)
    rejected = calculate_acceptance_metrics(
        workflow.observations,
        manifest=manifest,
        workflow_evidence=workflow,
        qualification_merged_evidence=tampered,
        mutation_qualification=mutation_report,
        native_qualification=native_report,
        algebra_native_qualification=algebra_report,
    )
    assert not rejected.algebra_native_gate_passed
    assert not rejected.evidence_bound

    fabricated_payload = merged.model_dump(mode="json")
    fabricated_record = next(
        record
        for record in fabricated_payload["observations"]
        if record["kind"] != MutationKind.POSITIVE.value
    )
    fabricated_record["state"] = ObservedValidationState.VALIDATED.value
    fabricated_payload["observations_sha256"] = computation_evaluation._sha256_json(
        fabricated_payload["observations"]
    )
    fabricated_payload["evidence_sha256"] = computation_evaluation._sha256_json(
        {
            key: value
            for key, value in fabricated_payload.items()
            if key != "evidence_sha256"
        }
    )
    fabricated = QualificationMergedObservationEvidence.model_validate(
        fabricated_payload
    )
    fabricated_rejected = calculate_acceptance_metrics(
        workflow.observations,
        manifest=manifest,
        workflow_evidence=workflow,
        qualification_merged_evidence=fabricated,
        mutation_qualification=mutation_report,
        native_qualification=native_report,
        algebra_native_qualification=algebra_report,
    )
    assert not fabricated_rejected.algebra_native_gate_passed
    assert not fabricated_rejected.evidence_bound


def test_algebra_executor_rejects_unstable_native_render(tmp_path: Path) -> None:
    plan = build_algebra_native_execution_plan(
        run_id=RUN_ID,
        imathas_namespace=NAMESPACE,
    )

    async def unstable(
        request: AlgebraNativeExecutionRequest,
    ) -> AlgebraNativeExecutionObservation:
        observation = await _passing_observation(request)
        payload = observation.model_dump(mode="json")
        payload["repeat_render_sha256"] = "9" * 64
        payload["observation_sha256"] = computation_evaluation._sha256_json(
            {
                key: value
                for key, value in payload.items()
                if key != "observation_sha256"
            }
        )
        return AlgebraNativeExecutionObservation.model_validate(payload)

    with pytest.raises(ValueError, match="repeat_render_mismatch"):
        asyncio.run(
            execute_algebra_native_plan(
                plan,
                output=tmp_path / "rejected.jsonl",
                executor=unstable,
                engine="webwork",
                run_id=RUN_ID,
                webwork_engine_image_digest=WEBWORK_DIGEST,
                imathas_engine_image_digest=IMATHAS_DIGEST,
                imathas_adapter_image_digest=ADAPTER_DIGEST,
                network_attestation_sha256=NETWORK_ATTESTATION,
                imathas_namespace=NAMESPACE,
                max_cases=1,
            )
        )


async def _passing_observation(
    request: AlgebraNativeExecutionRequest,
) -> AlgebraNativeExecutionObservation:
    render_sha256 = hashlib.sha256(request.source.encode("utf-8")).hexdigest()
    content = {
        "schema_version": "assessment-computation-algebra-native-observation-v0",
        "qualification_only": True,
        "request_sha256": request.request_sha256,
        "plan_id": request.plan_id,
        "engine": request.engine,
        "engine_image_digest": (
            WEBWORK_DIGEST if request.engine == "webwork" else IMATHAS_DIGEST
        ),
        "adapter_image_digest": (
            ADAPTER_DIGEST if request.engine == "imathas" else None
        ),
        "imathas_namespace": request.imathas_namespace,
        "imathas_object_id": (
            f"algebra-{request.fixture_id}" if request.engine == "imathas" else None
        ),
        "network_attestation_sha256": NETWORK_ATTESTATION,
        "raw_engine_evidence_sha256": hashlib.sha256(
            f"raw:{request.request_sha256}".encode("utf-8")
        ).hexdigest(),
        "constraints_satisfied": True,
        "correct_answer_accepted": True,
        "alternate_correct_answer_accepted": (
            None if request.answer_kind == "numeric" else True
        ),
        "wrong_answer_rejected": True,
        "rendered": True,
        "render_sha256": render_sha256,
        "repeat_render_sha256": render_sha256,
        "warnings": [],
        "errors": [],
        "outbound_request_count": 0,
    }
    content["observation_sha256"] = computation_evaluation._sha256_json(content)
    return AlgebraNativeExecutionObservation.model_validate(content)


def _trust_policy(raw_ledger_sha256: str) -> NativeQualificationTrustPolicy:
    content = {
        "schema_version": "assessment-computation-native-trust-policy-v0",
        "run_id": RUN_ID,
        "endpoint_profile": "isolated_canary_v0",
        "webwork_engine_image_digest": WEBWORK_DIGEST,
        "imathas_engine_image_digest": IMATHAS_DIGEST,
        "imathas_adapter_image_digest": ADAPTER_DIGEST,
        "receipt_ledger_raw_sha256": raw_ledger_sha256,
        "imathas_namespace": NAMESPACE,
        "imathas_namespace_disposable": True,
        "imathas_namespace_created_attestation_sha256": "7" * 64,
        "imathas_namespace_cleanup_status": "verified",
        "imathas_namespace_cleanup_attestation_sha256": "8" * 64,
        "network_attestation_sha256": NETWORK_ATTESTATION,
        "operator_attestation_sha256": "6" * 64,
    }
    content["policy_sha256"] = computation_evaluation._sha256_json(content)
    return NativeQualificationTrustPolicy.model_validate(content)


def _native_report(
    manifest_sha256: str,
    policy: NativeQualificationTrustPolicy,
) -> NativeQualificationReport:
    content = {
        "schema_version": "assessment-computation-native-report-v0",
        "manifest_sha256": manifest_sha256,
        "expected_receipts": 4_000,
        "imported_receipts": 4_000,
        "valid_receipts": 4_000,
        "invalid_receipts": 0,
        "missing_receipts": 0,
        "duplicate_receipts": 0,
        "unexpected_receipts": 0,
        "execution_status": "passed",
        "qualified": True,
        "trust_policy_sha256": policy.policy_sha256,
        "trust_policy_applied": True,
        "qualification_run_id": policy.run_id,
        "endpoint_profile": policy.endpoint_profile,
        "webwork_engine_image_digest": policy.webwork_engine_image_digest,
        "imathas_engine_image_digest": policy.imathas_engine_image_digest,
        "imathas_adapter_image_digest": policy.imathas_adapter_image_digest,
        "network_attestation_sha256": policy.network_attestation_sha256,
        "raw_receipt_ledger_sha256": "3" * 64,
        "raw_receipt_ledger_byte_count": 1,
        "imathas_namespace": policy.imathas_namespace,
        "imathas_namespace_cleanup_status": "verified",
        "imathas_namespace_cleanup_attestation_sha256": (
            policy.imathas_namespace_cleanup_attestation_sha256
        ),
        "receipt_ledger_sha256": "9" * 64,
        "imported_execution_claims": 4_000,
        "issues": [],
        "limitations": [
            "Synthetic model fixture.",
            "No external engine was contacted.",
        ],
    }
    content["report_sha256"] = computation_evaluation._sha256_json(content)
    return NativeQualificationReport.model_validate(content)


def _execute_algebra_report(tmp_path: Path, manifest):
    plan = build_algebra_native_execution_plan(
        run_id=RUN_ID,
        imathas_namespace=NAMESPACE,
        manifest=manifest,
    )
    output = tmp_path / "merged-algebra-receipts.jsonl"
    for engine in ("webwork", "imathas"):
        asyncio.run(
            execute_algebra_native_plan(
                plan,
                output=output,
                executor=_passing_observation,
                engine=engine,
                run_id=RUN_ID,
                webwork_engine_image_digest=WEBWORK_DIGEST,
                imathas_engine_image_digest=IMATHAS_DIGEST,
                imathas_adapter_image_digest=ADAPTER_DIGEST,
                network_attestation_sha256=NETWORK_ATTESTATION,
                imathas_namespace=NAMESPACE,
                manifest=manifest,
            )
        )
    policy = _trust_policy(hashlib.sha256(output.read_bytes()).hexdigest())
    return validate_algebra_native_qualification_receipts(
        load_algebra_native_qualification_receipts(output),
        manifest=manifest,
        trust_policy=policy,
    )


def _partial_workflow_evidence(manifest, mutation_report_sha256: str):
    algebra_ids = {
        case.fixture_id
        for case in manifest.computation_cases
        if case.family == "algebraic"
    }
    engine_ids = {case.fixture_id for case in manifest.engine_twins}
    positives = [
        ComputationEvaluationObservation(
            case_id=case.fixture_id,
            surface=EvaluationSurface(case.family),
            kind=MutationKind.POSITIVE,
            state=(
                ObservedValidationState.PARTIALLY_VALIDATED
                if case.fixture_id in algebra_ids
                else ObservedValidationState.VALIDATED
            ),
            oracle_match=True,
            critical_defect_detected=False,
            deterministic_replay=True,
        )
        for case in manifest.computation_cases
    ]
    positives.extend(
        ComputationEvaluationObservation(
            case_id=case.fixture_id,
            surface=EvaluationSurface(case.engine),
            kind=MutationKind.POSITIVE,
            state=ObservedValidationState.PARTIALLY_VALIDATED,
            oracle_match=True,
            critical_defect_detected=False,
            deterministic_replay=True,
        )
        for case in manifest.engine_twins
    )
    mutations = [
        ComputationEvaluationObservation(
            case_id=case.mutation_id,
            surface=case.surface,
            kind=case.kind,
            state=ObservedValidationState.VALIDATION_FAILED,
            oracle_match=False,
            critical_defect_detected=True,
            deterministic_replay=True,
        )
        for case in manifest.mutations
    ]
    observations = positives + mutations
    observation_payload = [
        observation.model_dump(mode="json") for observation in observations
    ]
    positive_hashes = {
        case_id: hashlib.sha256(f"workflow:{case_id}".encode("utf-8")).hexdigest()
        for case_id in sorted(
            {case.fixture_id for case in manifest.computation_cases} | engine_ids
        )
    }
    content = {
        "schema_version": "assessment-computation-workflow-observations-v0",
        "manifest_sha256": manifest.manifest_sha256,
        "runner_version": "assessment-computation-workflow-runner-v0",
        "transport": "unix_socket",
        "computation_runtime_manifest_sha256": "a" * 64,
        "observations": observation_payload,
        "positive_workflow_receipt_sha256s": positive_hashes,
        "positive_receipt_ledger_raw_sha256": "b" * 64,
        "positive_receipt_ledger_raw_byte_count": 1,
        "trust_policy_sha256": "c" * 64,
        "trust_policy_applied": True,
        "mutation_report_sha256": mutation_report_sha256,
        "operator_attestation_sha256": "d" * 64,
        "observations_sha256": computation_evaluation._sha256_json(observation_payload),
    }
    content["evidence_sha256"] = computation_evaluation._sha256_json(content)
    return WorkflowObservationEvidence.model_validate(content)
