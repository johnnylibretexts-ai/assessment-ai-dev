from __future__ import annotations

import pytest

from app.schemas import AssessmentItemType
from app.parameterized import compile_parameterized_item
from evaluation.adapt_seed import build_adapt_seed_items, finalize_seed_receipts
from evaluation.fixtures import build_parameter_spec
from evaluation.models import AdaptSeedAttestation, EngineProbeReceipt


SHA = "a" * 64
IMAGE = "sha256:" + "b" * 64


def test_finalize_seed_receipts_requires_exact_refreshed_canary_evidence() -> None:
    probe = _probe()
    attestation = _attestation()

    receipts = finalize_seed_receipts([probe], [attestation])

    assert len(receipts) == 1
    assert receipts[0].persisted_grade_match
    assert receipts[0].outbound_request_count == 0

    with pytest.raises(ValueError, match="refreshed ADAPT grade"):
        finalize_seed_receipts(
            [probe], [attestation.model_copy(update={"persisted_score": 0.5})]
        )
    with pytest.raises(ValueError, match="identical keys"):
        finalize_seed_receipts([probe], [])
    with pytest.raises(ValueError, match="ADAPT canary check"):
        finalize_seed_receipts(
            [probe], [attestation.model_copy(update={"hint_mode_off": False})]
        )


def test_build_adapt_seed_items_binds_all_sources_and_imathas_objects() -> None:
    probes: list[EngineProbeReceipt] = []
    imathas_ids: dict[str, int] = {}
    for item_type in (AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS):
        for index in range(20):
            item_id = f"{item_type.value}-{index + 1:02d}"
            compiled = compile_parameterized_item(
                build_parameter_spec(item_type, index), validation_seeds=100
            )
            technology_id = index + 2
            object_sha = (
                _sha(f"imathas-question:{technology_id}")
                if item_type == AssessmentItemType.IMATHAS
                else None
            )
            if object_sha is not None:
                imathas_ids[item_id] = technology_id
            probes.append(
                _probe().model_copy(
                    update={
                        "item_id": item_id,
                        "item_type": item_type,
                        "source_sha256": compiled.source_sha256,
                        "endpoint_host": (
                            "imathas.libretexts.dev"
                            if item_type == AssessmentItemType.IMATHAS
                            else "wwrenderer.libretexts.dev"
                        ),
                        "engine_object_sha256": object_sha,
                        "object_idempotent_observed": (
                            True if object_sha is not None else None
                        ),
                    }
                )
            )

    items = build_adapt_seed_items(probes, imathas_ids=imathas_ids)

    assert len(items) == 40
    assert sum(item.technology_id is not None for item in items) == 20
    with pytest.raises(ValueError, match="missing IMathAS object mapping"):
        build_adapt_seed_items(probes, imathas_ids={})


def _probe() -> EngineProbeReceipt:
    return EngineProbeReceipt(
        run_id="build08-test",
        item_id="webwork-01",
        item_type=AssessmentItemType.WEBWORK,
        seed=1,
        compiler_version="parameterized-dsl-v1",
        source_sha256=SHA,
        endpoint_host="wwrenderer.libretexts.dev",
        engine_image_sha256=IMAGE,
        network_isolation_attestation_sha256=SHA,
        runtime_values_sha256=SHA,
        semantic_render_sha256=SHA,
        deterministic=True,
        constraints_satisfied=True,
        rendered=True,
        render_duration_ms=10,
        warning_count=0,
        error_count=0,
        expected_answer_accepted=True,
        wrong_answer_rejected=True,
        expected_score=1,
        wrong_score=0,
        remaining_checks=[
            "persisted_grade_match",
            "object_idempotent",
            "cross_owner_access_blocked",
        ],
    )


def _attestation() -> AdaptSeedAttestation:
    return AdaptSeedAttestation(
        run_id="build08-test",
        item_id="webwork-01",
        item_type=AssessmentItemType.WEBWORK,
        seed=1,
        compiler_version="parameterized-dsl-v1",
        source_sha256=SHA,
        adapt_image_sha256=IMAGE,
        clone_backup_sha256=SHA,
        adapt_question_id=101,
        adapt_assignment_id=201,
        adapt_submission_id=301,
        expected_score=1,
        persisted_score=1,
        submission_count=1,
        grade_refreshed=True,
        object_idempotent=True,
        cross_owner_access_blocked=True,
        canary_network_internal=True,
        hint_mode_off=True,
    )


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()
