from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from app.computation_client import (
    ComputationServiceStatus,
    InProcessAssessmentComputationClient,
)
from app.parameterized import compile_parameterized_item
import evaluation.computation as computation_evaluation
from evaluation.computation import (
    ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST,
    ACCEPTED_BUILD08_BASE_COMMIT,
    CANARY_STAGE_ORDER,
    Build08CompatibilityQualification,
    CanaryStage,
    CanaryStageExecutionRequest,
    CanaryStageEventKind,
    CanaryStageReceipt,
    LocalCanaryStageArtifacts,
    NativeExecutionObservation,
    NativeObservedValue,
    WorkflowEvidenceTrustPolicy,
    _canonical_decimal,
    _canary_stage_contract_sha256,
    _sha256_json,
    build_computation_evaluation_manifest,
    build_computation_seed_plan,
    build_workflow_observation_evidence,
    execute_computation_native_plan,
    execute_workflow_positive_plan,
    execute_local_canary_stage,
    load_canary_stage_receipts,
    load_native_qualification_receipts,
    load_ucum_artifact_equivalence_attestation,
    load_workflow_positive_receipts,
    qualify_ucum_subset,
    run_offline_mutation_qualification,
    validate_build08_compatibility_receipts,
    validate_canary_stage_receipts,
)


@pytest.mark.asyncio
async def test_typed_native_executor_derives_receipt_from_sealed_request(
    tmp_path: Path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    case = next(
        case
        for case in build_computation_seed_plan(
            "typed-executor-test", manifest=manifest
        )
        if case.engine == "webwork"
    )
    output = tmp_path / "receipts.jsonl"

    async def fake_runner(request):
        preview = compile_parameterized_item(
            request.parameterized_spec,
            validation_seeds=1,
            validation_seed_values=[request.seed],
        ).previews[0]
        values = {
            name: (
                NativeObservedValue(kind="integer", integer=int(value))
                if isinstance(value, int)
                else NativeObservedValue(
                    kind="decimal", decimal=_canonical_decimal(value)
                )
            )
            for name, value in preview.variables.items()
        }
        correct = _canonical_decimal(preview.answer)
        wrong = _canonical_decimal(float(preview.answer) + 1)
        content = {
            "schema_version": "assessment-computation-native-observation-v0",
            "request_sha256": request.request_sha256,
            "item_id": request.item_id,
            "engine": request.engine,
            "seed": request.seed,
            "engine_image_digest": f"sha256:{'1' * 64}",
            "adapter_image_digest": None,
            "network_attestation_sha256": "2" * 64,
            "imathas_namespace": None,
            "imathas_object_id": None,
            "engine_observed_values": {
                name: value.model_dump(mode="json") for name, value in values.items()
            },
            "engine_observed_correct_answer": correct,
            "engine_observed_wrong_answer": wrong,
            "constraints_satisfied": True,
            "correct_answer_accepted": True,
            "wrong_answer_rejected": True,
            "rendered": True,
            "render_sha256": "3" * 64,
            "repeat_render_sha256": "3" * 64,
            "warnings": [],
            "errors": [],
            "outbound_request_count": 0,
            "raw_engine_evidence_sha256": "4" * 64,
        }
        content["observation_sha256"] = _sha256_json(content)
        return NativeExecutionObservation.model_validate(content)

    attempted, written = await execute_computation_native_plan(
        [case],
        output=output,
        executor=fake_runner,
        engine="webwork",
        run_id="typed-executor-test",
        webwork_engine_image_digest=f"sha256:{'1' * 64}",
        imathas_engine_image_digest=f"sha256:{'5' * 64}",
        imathas_adapter_image_digest=f"sha256:{'6' * 64}",
        network_attestation_sha256="2" * 64,
        imathas_namespace="acv0-canary-typed-executor-test",
        manifest=manifest,
    )

    receipts = load_native_qualification_receipts(output)
    assert (attempted, written, len(receipts)) == (1, 1, 1)
    assert receipts.records[0].item_id == case.item_id
    assert receipts.records[0].source_sha256 == case.source_sha256


@pytest.mark.asyncio
async def test_workflow_evidence_is_derived_from_executed_service_receipts(
    tmp_path: Path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    ledger_path = tmp_path / "workflow.jsonl"
    runtime_manifest_sha256 = "9" * 64
    client = _ObservedRuntimeTestClient(runtime_manifest_sha256)
    rows = await computation_evaluation._execute_workflow_positive_plan_with_client(
        client=client,
        output=ledger_path,
        run_id="workflow-receipt-test",
        manifest=manifest,
        expected_runtime_manifest_sha256=runtime_manifest_sha256,
    )
    assert len(rows) == 100
    assert client.compute_calls == 100
    assert client.validation_calls == 200
    assert {
        row.state.value
        for row in rows
        if row.surface.value in {"algebraic", "webwork", "imathas"}
    } == {"partially_validated"}
    assert all(row.oracle_match and row.deterministic_replay for row in rows)

    loaded = load_workflow_positive_receipts(ledger_path)
    mutation_report = run_offline_mutation_qualification(manifest)
    policy_content = {
        "schema_version": "assessment-computation-workflow-trust-policy-v0",
        "run_id": "workflow-receipt-test",
        "manifest_sha256": manifest.manifest_sha256,
        "runner_version": "assessment-computation-workflow-runner-v0",
        "transport": "unix_socket",
        "computation_runtime_manifest_sha256": runtime_manifest_sha256,
        "positive_receipt_ledger_raw_sha256": loaded.raw_ledger_sha256,
        "mutation_report_sha256": mutation_report.report_sha256,
        "operator_attestation_sha256": "a" * 64,
    }
    policy_content["policy_sha256"] = _sha256_json(policy_content)
    evidence = build_workflow_observation_evidence(
        loaded,
        mutation_qualification=mutation_report,
        trust_policy=WorkflowEvidenceTrustPolicy.model_validate(policy_content),
        manifest=manifest,
    )

    assert len(evidence.observations) == 300
    assert (
        evidence.positive_receipt_ledger_raw_sha256
        == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    )
    assert evidence.trust_policy_applied
    assert evidence.computation_runtime_manifest_sha256 == runtime_manifest_sha256

    raw_rows = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    state_fabrication = [dict(row) for row in raw_rows]
    state_fabrication[0]["state"] = "validation_failed"
    state_fabrication[0]["receipt_sha256"] = _sha256_json(
        {
            key: value
            for key, value in state_fabrication[0].items()
            if key != "receipt_sha256"
        }
    )
    state_path = tmp_path / "fabricated-state.jsonl"
    state_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in state_fabrication
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="workflow positive receipt line 1"):
        load_workflow_positive_receipts(state_path)

    oracle_fabrication = [dict(row) for row in raw_rows]
    oracle_fabrication[0]["oracle_match"] = False
    oracle_fabrication[0]["receipt_sha256"] = _sha256_json(
        {
            key: value
            for key, value in oracle_fabrication[0].items()
            if key != "receipt_sha256"
        }
    )
    oracle_path = tmp_path / "fabricated-oracle.jsonl"
    oracle_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in oracle_fabrication
        ),
        encoding="utf-8",
    )
    oracle_ledger = load_workflow_positive_receipts(oracle_path)
    oracle_policy_content = {
        **policy_content,
        "positive_receipt_ledger_raw_sha256": oracle_ledger.raw_ledger_sha256,
    }
    oracle_policy_content["policy_sha256"] = _sha256_json(
        {
            key: value
            for key, value in oracle_policy_content.items()
            if key != "policy_sha256"
        }
    )
    with pytest.raises(ValueError, match="oracle claim does not match evidence"):
        build_workflow_observation_evidence(
            oracle_ledger,
            mutation_qualification=mutation_report,
            trust_policy=WorkflowEvidenceTrustPolicy.model_validate(
                oracle_policy_content
            ),
            manifest=manifest,
        )


class _ObservedRuntimeTestClient:
    def __init__(self, runtime_manifest_sha256: str) -> None:
        self._runtime_manifest_sha256 = runtime_manifest_sha256
        self._client = InProcessAssessmentComputationClient()
        self.compute_calls = 0
        self.validation_calls = 0

    async def compute(self, blueprint):
        self.compute_calls += 1
        return await self._client.compute(blueprint)

    async def validate(self, request):
        self.validation_calls += 1
        return await self._client.validate(request)

    async def ready(self) -> ComputationServiceStatus:
        return ComputationServiceStatus(
            status="ready",
            service="assessment-computation",
            schema_version="assessment-computation-v0",
            runtime_manifest_sha256=self._runtime_manifest_sha256,
        )


@pytest.mark.asyncio
async def test_workflow_executor_accepts_no_url_or_regular_file_transport(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "not-a-socket"
    regular_file.write_text("inert", encoding="utf-8")
    with pytest.raises(ValueError, match="not a Unix socket"):
        await execute_workflow_positive_plan(
            socket_path=regular_file,
            expected_runtime_manifest_sha256="9" * 64,
            output=tmp_path / "receipts.jsonl",
            run_id="workflow-transport-test",
        )
    with pytest.raises(ValueError, match="absolute non-symlink"):
        await execute_workflow_positive_plan(
            socket_path=Path("https:/invalid.example/runner"),
            expected_runtime_manifest_sha256="9" * 64,
            output=tmp_path / "receipts.jsonl",
            run_id="workflow-transport-test",
        )


def test_ucum_equivalence_requires_imported_exact_artifact_identity(
    tmp_path: Path,
) -> None:
    content = {
        "schema_version": "assessment-computation-ucum-artifact-equivalence-v0",
        "ucum_version": "2.2",
        "comparison_method": "byte_for_byte",
        "official_attachment_sha256": (
            "3b3feb9d8ecfe8958da69b1afcd25572e3f8d80a36bfcbcda4834a10a0eeef0d"
        ),
        "official_attachment_byte_count": 36_782,
        "local_artifact_sha256": (
            "3b3feb9d8ecfe8958da69b1afcd25572e3f8d80a36bfcbcda4834a10a0eeef0d"
        ),
        "local_artifact_byte_count": 36_782,
        "official_release_record_sha256": "1" * 64,
        "comparison_ledger_sha256": "2" * 64,
        "reviewer_subject_sha256": "3" * 64,
        "reviewer_attestation_sha256": "4" * 64,
        "decision": "equivalent",
        "rationale": "Reviewer compared the official attachment and local artifact.",
    }
    content["attestation_sha256"] = _sha256_json(content)
    path = tmp_path / "equivalence.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    loaded = load_ucum_artifact_equivalence_attestation(path)
    artifact = tmp_path / "not-the-pinned-artifact.xml"
    artifact.write_bytes(b"<ucumTests />")

    with pytest.raises(ValueError, match="does not match the local artifact"):
        qualify_ucum_subset(
            artifact,
            expected_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            equivalence_attestation=loaded,
        )


def _canary_rows(manifest) -> list[CanaryStageReceipt]:
    baseline = "b" * 64
    rows: list[CanaryStageReceipt] = []
    for sequence, stage in enumerate(CANARY_STAGE_ORDER, start=1):
        specialist_evidence = None
        if stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS:
            specialist_evidence = {
                "schema_version": (
                    "assessment-computation-specialist-attestation-canary-v0"
                ),
                "request_sha256": hashlib.sha256(
                    b"enforce-specialist-request"
                ).hexdigest(),
                "run_id": "canary-stage-test",
                "sequence": sequence,
                "stage": stage,
                "authenticated_allowlisted_accept_count": 1,
                "missing_or_invalid_token_rejection_count": 1,
                "client_subject_spoof_rejection_count": 1,
                "reviewer_identity_only_rejection_count": 1,
                "accepted_attestation_sha256": hashlib.sha256(
                    b"accepted-attestation"
                ).hexdigest(),
                "proxy_header_stripping_receipt_sha256": hashlib.sha256(
                    b"proxy-header-stripping"
                ).hexdigest(),
                "negative_path_ledger_sha256": hashlib.sha256(
                    b"specialist-negative-paths"
                ).hexdigest(),
            }
            specialist_evidence["evidence_sha256"] = _sha256_json(specialist_evidence)
        family = (
            stage.value.removeprefix("assist_")
            if stage
            in {
                CanaryStage.ASSIST_NUMERIC,
                CanaryStage.ASSIST_ALGEBRAIC,
                CanaryStage.ASSIST_UNIT,
            }
            else None
        )
        mode = (
            "offline"
            if stage == CanaryStage.OFFLINE_CORPUS_SECURITY
            else "assist"
            if family is not None
            else "enforce"
            if stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
            else "off"
        )
        content = {
            "schema_version": "assessment-computation-canary-stage-v0",
            "run_id": "canary-stage-test",
            "manifest_sha256": manifest.manifest_sha256,
            "accepted_build08_base_commit": ACCEPTED_BUILD08_BASE_COMMIT,
            "prior_image_digest": ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST,
            "candidate_image_digest": f"sha256:{'1' * 64}",
            "computation_image_digest": f"sha256:{'2' * 64}",
            "sequence": sequence,
            "stage": stage,
            "mode": mode,
            "family": family,
            "cloned_database_snapshot_sha256": (
                None if stage == CanaryStage.OFFLINE_CORPUS_SECURITY else "3" * 64
            ),
            "input_state_sha256": (
                baseline
                if stage == CanaryStage.CANDIDATE_OFF_PARITY
                else f"{sequence:064x}"
            ),
            "output_state_sha256": (
                baseline
                if stage
                in {
                    CanaryStage.CANDIDATE_OFF_PARITY,
                    CanaryStage.PRIOR_IMAGE_ROLLBACK,
                    CanaryStage.RETURN_OFF,
                }
                else f"{sequence:064x}"
                if stage == CanaryStage.BACKUP_RESTORE
                else f"{sequence + 20:064x}"
            ),
            "executor_revision": "assessment-computation-local-canary-executor-v0",
            "command_argv_sha256": hashlib.sha256(
                f"command-{sequence}".encode()
            ).hexdigest(),
            "executable_sha256": hashlib.sha256(
                f"executable-{sequence}".encode()
            ).hexdigest(),
            "command_exit_code": 0,
            "stdout_sha256": hashlib.sha256(f"stdout-{sequence}".encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(f"stderr-{sequence}".encode()).hexdigest(),
            "stage_contract_sha256": _canary_stage_contract_sha256(stage),
            "stage_event_kind": {
                CanaryStage.OFFLINE_CORPUS_SECURITY: (
                    CanaryStageEventKind.OFFLINE_FAILURE_COUNT
                ),
                CanaryStage.CANDIDATE_OFF_PARITY: (
                    CanaryStageEventKind.OFF_MODE_EFFECT_COUNT
                ),
                CanaryStage.BACKUP_RESTORE: (
                    CanaryStageEventKind.BACKUP_RESTORE_MISMATCH_COUNT
                ),
                CanaryStage.PRIOR_IMAGE_ROLLBACK: (
                    CanaryStageEventKind.PRIOR_IMAGE_ROLLBACK_MISMATCH_COUNT
                ),
                CanaryStage.ASSIST_NUMERIC: (
                    CanaryStageEventKind.ASSIST_NUMERIC_REPORT_COUNT
                ),
                CanaryStage.ASSIST_ALGEBRAIC: (
                    CanaryStageEventKind.ASSIST_ALGEBRAIC_REPORT_COUNT
                ),
                CanaryStage.ASSIST_UNIT: (
                    CanaryStageEventKind.ASSIST_UNIT_REPORT_COUNT
                ),
                CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS: (
                    CanaryStageEventKind.ENFORCE_FAKE_ADAPTER_SCENARIO_COUNT
                ),
                CanaryStage.COMPATIBILITY_SHADOW_380: (
                    CanaryStageEventKind.COMPATIBILITY_SHADOW_DRAFT_COUNT
                ),
                CanaryStage.RETURN_OFF: (CanaryStageEventKind.RETURN_OFF_EFFECT_COUNT),
            }[stage],
            "rollback_observed_image_digest": (
                ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST
                if stage == CanaryStage.PRIOR_IMAGE_ROLLBACK
                else None
            ),
            "raw_event_ledger_sha256": hashlib.sha256(
                f"events-{sequence}".encode()
            ).hexdigest(),
            "raw_event_ledger_byte_count": 100 + sequence,
            "raw_event_count": 2,
            "independent_observer_attestation_sha256": hashlib.sha256(
                f"observer-{sequence}".encode()
            ).hexdigest(),
            "fake_publication_adapter_used": (
                stage == CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS
            ),
            "specialist_attestation_evidence": specialist_evidence,
            "specialist_attestation_paths_exercised": (specialist_evidence is not None),
            "real_publication_attempt_count": 0,
            "passed": True,
        }
        content["receipt_sha256"] = _sha256_json(content)
        rows.append(CanaryStageReceipt.model_validate(content))
    return rows


def test_enforce_canary_event_requires_explicit_specialist_path_evidence() -> None:
    request_sha256 = hashlib.sha256(b"enforce-request").hexdigest()
    content = {
        "schema_version": "assessment-computation-canary-event-v0",
        "request_sha256": request_sha256,
        "run_id": "enforce-specialist-event-test",
        "sequence": 8,
        "stage": CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS,
        "kind": CanaryStageEventKind.ENFORCE_FAKE_ADAPTER_SCENARIO_COUNT,
        "observed_count": 1,
        "observed_image_digest": None,
        "evidence_sha256": hashlib.sha256(b"stage-evidence").hexdigest(),
    }
    content["event_sha256"] = _sha256_json(content)

    with pytest.raises(ValueError, match="explicit specialist attestation"):
        computation_evaluation.CanaryStageEvent.model_validate(content)

    specialist_evidence = {
        "schema_version": "assessment-computation-specialist-attestation-canary-v0",
        "request_sha256": request_sha256,
        "run_id": content["run_id"],
        "sequence": 8,
        "stage": CanaryStage.ENFORCE_FAKE_ADAPTERS_ATTESTATIONS,
        "authenticated_allowlisted_accept_count": 1,
        "missing_or_invalid_token_rejection_count": 1,
        "client_subject_spoof_rejection_count": 1,
        "reviewer_identity_only_rejection_count": 1,
        "accepted_attestation_sha256": hashlib.sha256(
            b"accepted-attestation-event"
        ).hexdigest(),
        "proxy_header_stripping_receipt_sha256": hashlib.sha256(
            b"proxy-strip-event"
        ).hexdigest(),
        "negative_path_ledger_sha256": hashlib.sha256(
            b"negative-path-event"
        ).hexdigest(),
    }
    specialist_evidence["evidence_sha256"] = _sha256_json(specialist_evidence)
    content["specialist_attestation_evidence"] = specialist_evidence
    content["event_sha256"] = _sha256_json(
        {key: value for key, value in content.items() if key != "event_sha256"}
    )

    event = computation_evaluation.CanaryStageEvent.model_validate(content)
    assert event.specialist_attestation_evidence is not None
    assert (
        event.specialist_attestation_evidence.proxy_header_stripping_receipt_sha256
        == specialist_evidence["proxy_header_stripping_receipt_sha256"]
    )


def test_canary_validator_requires_all_stages_and_return_to_off(
    tmp_path: Path,
) -> None:
    manifest = build_computation_evaluation_manifest()
    rows = _canary_rows(manifest)
    path = tmp_path / "canary.jsonl"
    path.write_text(
        "".join(row.model_dump_json() + "\n" for row in rows),
        encoding="utf-8",
    )

    report = validate_canary_stage_receipts(
        load_canary_stage_receipts(path),
        manifest=manifest,
    )

    assert report.qualified
    assert report.imported_stages == 10
    assert (
        report.raw_stage_ledger_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_canary_validator_rejects_cross_stage_evidence_reuse(tmp_path: Path) -> None:
    manifest = build_computation_evaluation_manifest()
    rows = _canary_rows(manifest)
    reused: list[CanaryStageReceipt] = []
    for row in rows:
        content = row.model_dump(mode="json", exclude={"receipt_sha256"})
        content["raw_event_ledger_sha256"] = "d" * 64
        content["independent_observer_attestation_sha256"] = "e" * 64
        content["receipt_sha256"] = _sha256_json(content)
        reused.append(CanaryStageReceipt.model_validate(content))
    path = tmp_path / "reused-evidence.jsonl"
    path.write_text(
        "".join(row.model_dump_json() + "\n" for row in reused),
        encoding="utf-8",
    )

    report = validate_canary_stage_receipts(
        load_canary_stage_receipts(path),
        manifest=manifest,
    )

    assert not report.qualified
    assert "reused_stage_event_ledger" in report.issues
    assert "reused_stage_observer_attestation" in report.issues

    cross_scoped = _canary_rows(manifest)
    first_content = cross_scoped[0].model_dump(mode="json", exclude={"receipt_sha256"})
    first_content["independent_observer_attestation_sha256"] = cross_scoped[
        1
    ].raw_event_ledger_sha256
    first_content["receipt_sha256"] = _sha256_json(first_content)
    cross_scoped[0] = CanaryStageReceipt.model_validate(first_content)
    cross_path = tmp_path / "cross-scoped-evidence.jsonl"
    cross_path.write_text(
        "".join(row.model_dump_json() + "\n" for row in cross_scoped),
        encoding="utf-8",
    )

    cross_report = validate_canary_stage_receipts(
        load_canary_stage_receipts(cross_path),
        manifest=manifest,
    )

    assert not cross_report.qualified
    assert "event_and_observer_evidence_not_independent" in cross_report.issues


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accepted_build08_base_commit", "0" * 40),
        ("prior_image_digest", f"sha256:{'0' * 64}"),
    ],
)
def test_canary_receipt_rejects_wrong_build08_identity(
    field: str,
    value: str,
) -> None:
    manifest = build_computation_evaluation_manifest()
    content = _canary_rows(manifest)[0].model_dump(
        mode="json", exclude={"receipt_sha256"}
    )
    content[field] = value
    content["receipt_sha256"] = _sha256_json(content)

    with pytest.raises(ValueError):
        CanaryStageReceipt.model_validate(content)


def test_canary_rollback_requires_observed_prior_image() -> None:
    manifest = build_computation_evaluation_manifest()
    rollback = _canary_rows(manifest)[3]
    content = rollback.model_dump(mode="json", exclude={"receipt_sha256"})
    content["rollback_observed_image_digest"] = None
    content["receipt_sha256"] = _sha256_json(content)

    with pytest.raises(ValueError, match="did not observe"):
        CanaryStageReceipt.model_validate(content)


@pytest.mark.asyncio
async def test_local_canary_executor_derives_receipt_from_command_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "disposable"
    root.mkdir()
    (root / ".assessment-computation-canary-disposable-v0").write_text(
        "assessment-computation-canary-disposable-v0\n",
        encoding="utf-8",
    )
    input_state = root / "input-state.json"
    input_state.write_text('{"mode":"off"}\n', encoding="utf-8")
    output_state = root / "output-state.json"
    event_ledger = root / "events.jsonl"
    observer = root / "observer.json"
    script = root / "stage-wrapper"
    script.write_text(
        f"#!{sys.executable}\n"
        + """import hashlib
import json
import os
import pathlib
import sys

def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

input_path, output_path, event_path, observer_path, run_id, candidate, computation = (
    sys.argv[1:]
)
request_sha = os.environ["ASSESSMENT_COMPUTATION_CANARY_REQUEST_SHA256"]
sequence = int(os.environ["ASSESSMENT_COMPUTATION_CANARY_SEQUENCE"])
stage = os.environ["ASSESSMENT_COMPUTATION_CANARY_STAGE"]
input_bytes = pathlib.Path(input_path).read_bytes()
pathlib.Path(output_path).write_bytes(input_bytes)
events = []
for kind, evidence in (
    ("off_mode_effect_count", "stage-evidence"),
    ("real_publication_attempt_count", "publication-evidence"),
):
    event = {
        "schema_version": "assessment-computation-canary-event-v0",
        "request_sha256": request_sha,
        "run_id": run_id,
        "sequence": sequence,
        "stage": stage,
        "kind": kind,
        "observed_count": 0,
        "observed_image_digest": None,
        "specialist_attestation_evidence": None,
        "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
    }
    event["event_sha256"] = digest(event)
    events.append(event)
raw_events = "".join(
    json.dumps(event, sort_keys=True, separators=(",", ":")) + "\\n"
    for event in events
).encode()
pathlib.Path(event_path).write_bytes(raw_events)
state_sha = hashlib.sha256(input_bytes).hexdigest()
attestation = {
    "schema_version": "assessment-computation-canary-observer-v0",
    "request_sha256": request_sha,
    "run_id": run_id,
    "accepted_build08_base_commit": (
        "8497aad448d18c49d967480134eff9f80a444bd0"
    ),
    "prior_image_digest": (
        "sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed"
    ),
    "candidate_image_digest": candidate,
    "computation_image_digest": computation,
    "sequence": sequence,
    "stage": stage,
    "input_state_sha256": state_sha,
    "output_state_sha256": state_sha,
    "raw_event_ledger_sha256": hashlib.sha256(raw_events).hexdigest(),
    "raw_event_ledger_byte_count": len(raw_events),
    "observer_subject_sha256": hashlib.sha256(b"observer-subject").hexdigest(),
    "observation_artifact_sha256": hashlib.sha256(
        b"independent-observation"
    ).hexdigest(),
    "decision": "passed",
}
attestation["attestation_sha256"] = digest(attestation)
pathlib.Path(observer_path).write_text(
    json.dumps(attestation, sort_keys=True, separators=(",", ":"))
)
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    command = [
        str(script),
        str(input_state),
        str(output_state),
        str(event_ledger),
        str(observer),
        "local-canary-test",
        f"sha256:{'1' * 64}",
        f"sha256:{'2' * 64}",
    ]
    request_content = {
        "schema_version": "assessment-computation-canary-execution-v0",
        "run_id": "local-canary-test",
        "manifest_sha256": "f" * 64,
        "accepted_build08_base_commit": ACCEPTED_BUILD08_BASE_COMMIT,
        "prior_image_digest": ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST,
        "candidate_image_digest": f"sha256:{'1' * 64}",
        "computation_image_digest": f"sha256:{'2' * 64}",
        "sequence": 2,
        "stage": CanaryStage.CANDIDATE_OFF_PARITY,
        "mode": "off",
        "family": None,
        "cloned_database_snapshot_sha256": "3" * 64,
        "command_argv": command,
        "command_argv_sha256": _sha256_json(command),
        "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "stage_contract_sha256": _canary_stage_contract_sha256(
            CanaryStage.CANDIDATE_OFF_PARITY
        ),
    }
    request_content["request_sha256"] = _sha256_json(request_content)
    request = CanaryStageExecutionRequest.model_validate(request_content)
    drifted_content = request.model_dump(mode="json", exclude={"request_sha256"})
    drifted_content["executable_sha256"] = "0" * 64
    drifted_content["request_sha256"] = _sha256_json(drifted_content)

    with pytest.raises(ValueError, match="executable bytes"):
        await execute_local_canary_stage(
            CanaryStageExecutionRequest.model_validate(drifted_content),
            LocalCanaryStageArtifacts(
                working_directory=root,
                input_state=input_state,
                output_state=output_state,
                raw_event_ledger=event_ledger,
                observer_attestation=observer,
            ),
            disposable_root=root,
        )

    receipt = await execute_local_canary_stage(
        request,
        LocalCanaryStageArtifacts(
            working_directory=root,
            input_state=input_state,
            output_state=output_state,
            raw_event_ledger=event_ledger,
            observer_attestation=observer,
        ),
        disposable_root=root,
    )

    assert receipt.stage == CanaryStage.CANDIDATE_OFF_PARITY
    assert receipt.input_state_sha256 == receipt.output_state_sha256
    assert receipt.command_exit_code == 0
    assert receipt.raw_event_count == 2


@pytest.mark.asyncio
async def test_local_canary_timeout_kills_the_exact_process_group(
    tmp_path: Path,
) -> None:
    root = tmp_path / "disposable"
    root.mkdir()
    (root / ".assessment-computation-canary-disposable-v0").write_text(
        "assessment-computation-canary-disposable-v0\n",
        encoding="utf-8",
    )
    input_state = root / "input-state.json"
    input_state.write_text('{"mode":"offline"}\n', encoding="utf-8")
    child_pid_path = root / "child.pid"
    script = root / "stage-wrapper"
    script.write_text(
        f"#!{sys.executable}\n"
        + """import pathlib
import subprocess
import sys
import time

child = subprocess.Popen(
    ["/bin/sleep", "60"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    command = [str(script), str(child_pid_path)]
    request_content = {
        "schema_version": "assessment-computation-canary-execution-v0",
        "run_id": "local-canary-timeout",
        "manifest_sha256": "f" * 64,
        "accepted_build08_base_commit": ACCEPTED_BUILD08_BASE_COMMIT,
        "prior_image_digest": ACCEPTED_BUILD08_ASSESSMENT_AI_IMAGE_DIGEST,
        "candidate_image_digest": f"sha256:{'1' * 64}",
        "computation_image_digest": f"sha256:{'2' * 64}",
        "sequence": 1,
        "stage": CanaryStage.OFFLINE_CORPUS_SECURITY,
        "mode": "offline",
        "family": None,
        "cloned_database_snapshot_sha256": None,
        "command_argv": command,
        "command_argv_sha256": _sha256_json(command),
        "executable_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "stage_contract_sha256": _canary_stage_contract_sha256(
            CanaryStage.OFFLINE_CORPUS_SECURITY
        ),
    }
    request_content["request_sha256"] = _sha256_json(request_content)

    with pytest.raises(ValueError, match="timed out"):
        await execute_local_canary_stage(
            CanaryStageExecutionRequest.model_validate(request_content),
            LocalCanaryStageArtifacts(
                working_directory=root,
                input_state=input_state,
                output_state=root / "output-state.json",
                raw_event_ledger=root / "events.jsonl",
                observer_attestation=root / "observer.json",
                timeout_seconds=1,
            ),
            disposable_root=root,
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("local canary descendant survived its exact process-group timeout")


def test_build08_compatibility_cannot_be_claimed_by_old_aggregate() -> None:
    report = validate_build08_compatibility_receipts(None, None, None)
    assert report.execution_status == "not_run"
    assert not report.qualified

    with pytest.raises(ValueError):
        Build08CompatibilityQualification.model_validate(
            {
                "schema_version": ("assessment-computation-build08-native-evidence-v0"),
                "run_id": "forged",
                "plan_sha256": "1" * 64,
                "receipt_ledger_sha256": "2" * 64,
                "webwork_engine_image_digest": f"sha256:{'3' * 64}",
                "imathas_engine_image_digest": f"sha256:{'4' * 64}",
                "imathas_adapter_image_digest": f"sha256:{'5' * 64}",
                "network_attestation_sha256": "6" * 64,
                "operator_attestation_sha256": "7" * 64,
                "executed": 4_000,
                "passed": 4_000,
                "warning_count": 0,
                "error_count": 0,
                "qualified": True,
                "evidence_sha256": "8" * 64,
            }
        )
