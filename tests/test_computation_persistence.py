from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

import app.db as db_module
from app.computation import (
    AssessmentComputationBlueprint,
    CheckStatus,
    ComputationValidationRequest,
    ExpressionNode,
    ValidationCheck,
    ValidationStatus,
    canonical_blueprint_hash,
    validate_computation,
)
from app.computation_workflow import validation_write
from app.db import (
    ComputationAttestation,
    ComputationAttestationWrite,
    ComputationEvidenceConflictError,
    ComputationEvidenceError,
    ComputationValidationRecord,
    ComputationValidationWrite,
    ConcurrentDraftUpdateError,
    Draft,
    DraftRepository,
    DraftWrite,
    PublicationComputationEvidence,
    PublicationComputationEvidenceWrite,
    init_database,
    utc_now,
    validate_computation_evidence,
)
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewStatus,
    SourceInfo,
)


def _page() -> NormalizedPage:
    text = "Momentum equals mass times velocity."
    return NormalizedPage(
        title="Momentum",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            canonical_url=(
                "https://dev.libretexts.org/Sandboxes/johnnyphung/Demo/Momentum"
            ),
            path="Sandboxes/johnnyphung/Demo/Momentum",
            page_id="comp-1",
        ),
    )


def _question(stem: str = "What is momentum for the stated mass and velocity?"):
    return QuestionDraft(
        concept_label="Momentum",
        stem=stem,
        choices=[
            Choice(id="A", text="6 kg m/s", correct=True),
            Choice(id="B", text="1.5 kg m/s", correct=False),
            Choice(id="C", text="5 kg m/s", correct=False),
            Choice(id="D", text="9 kg m/s", correct=False),
        ],
        explanation="Multiply the mass by the velocity.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


def _validation(
    marker: str = "initial",
    *,
    status: str = "validated",
    report_json: str | None = None,
) -> ComputationValidationWrite:
    blueprint = AssessmentComputationBlueprint(
        profile={"family": "numeric", "delivery": "numerical"},
        operation="evaluate",
        expression=ExpressionNode(
            kind="add",
            args=[_integer(2), _integer(3)],
        ),
    )
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=_integer(5),
        )
    )
    report = report.model_copy(
        update={
            "checks": [
                *report.checks,
                ValidationCheck(
                    code="presentation_binding",
                    status=CheckStatus.PASSED,
                    message="Learner-facing fields were server-bound.",
                ),
            ]
        }
    )
    if status == ValidationStatus.PARTIALLY_VALIDATED.value:
        report = report.model_copy(
            update={
                "status": ValidationStatus.PARTIALLY_VALIDATED,
                "checks": [
                    *report.checks,
                    ValidationCheck(
                        code="secondary",
                        status=CheckStatus.INCONCLUSIVE,
                        message="A secondary check is unavailable.",
                    ),
                ],
            }
        )
    elif status == ValidationStatus.UNSUPPORTED.value:
        report = report.model_copy(
            update={
                "status": ValidationStatus.UNSUPPORTED,
                "result": None,
                "checks": [
                    ValidationCheck(
                        code="supported_profile",
                        status=CheckStatus.INCONCLUSIVE,
                        message="This operation is outside the qualified profile.",
                    )
                ],
            }
        )
    elif status == ValidationStatus.VALIDATION_FAILED.value:
        report = report.model_copy(
            update={
                "status": ValidationStatus.VALIDATION_FAILED,
                "result": None,
                "checks": [
                    ValidationCheck(
                        code="computation",
                        status=CheckStatus.FAILED,
                        message="A material mismatch was detected.",
                    )
                ],
            }
        )
    report = report.model_copy(
        update={"limitations": [*report.limitations, f"test marker: {marker}"]}
    )
    value = validation_write(
        blueprint=blueprint,
        report=report,
        container_digest=f"sha256:{'1' * 64}",
        duration_ms=17,
        engine_validation=None,
    )
    return replace(value, report_json=report_json) if report_json else value


def _seed(
    repository: DraftRepository,
    *,
    computation_validation: ComputationValidationWrite | None = None,
) -> int:
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="computation-persistence-test",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Momentum",
                    description="Momentum is mass times velocity.",
                    source_paragraphs=[0],
                ),
                raw=_question(),
                critique=Critique(issues=[], revision_required=False),
                revised=_question(),
                computation_validation=computation_validation,
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


@pytest.fixture
def store(tmp_path: Path):
    database = init_database(f"sqlite:///{tmp_path / 'computation.db'}")
    yield database, DraftRepository(database)
    database.dispose()


def _publication_values(draft: Draft, *, publication_key: str) -> dict[str, object]:
    return {
        "draft_id": draft.id,
        "edit_count": draft.edit_count,
        "question_snapshot_json": draft.current_json,
        "source_snapshot_json": {"source_id": draft.source_snapshot_id},
        "reviewer_identity": "reviewer@example.edu",
        "approved_at": utc_now(),
        "destination_folder_id": 42,
        "destination_folder_name": "Canary",
        "author": "LibreTexts",
        "public": False,
        "license": "CC BY",
        "license_version": "4.0",
        "license_label": "CC BY 4.0",
        "license_evidence_url": "https://example.invalid/license-evidence",
        "framework_id": 7,
        "framework_title": "Physics",
        "alignment_json": {"topic": "momentum"},
        "stable_topic_ids_json": ["momentum"],
        "hint_ladder_snapshot_json": None,
        "publication_key": publication_key,
        "payload_hash": "b" * 64,
        "payload_mapper_version": "test-1",
        "qti_exporter_version": "test-1",
    }


def test_generation_atomically_stores_an_exact_current_report(store) -> None:
    database, repository = store
    value = _validation()

    draft_id = _seed(repository, computation_validation=value)

    current = repository.get_current_computation_validation(draft_id)
    assert current is not None
    assert current.edit_count == 0
    assert current.status == "validated"
    assert current.is_current is True
    assert current.blueprint_sha256 == canonical_blueprint_hash(
        AssessmentComputationBlueprint.model_validate(json.loads(value.blueprint_json))
    )
    assert (
        current.report_sha256 == hashlib.sha256(value.report_json.encode()).hexdigest()
    )
    assert (
        repository.get_current_computation_validation(
            draft_id,
            draft_sha256=current.draft_sha256,
            report_sha256=current.report_sha256,
        )
        == current
    )

    table_names = set(inspect(database.engine).get_table_names())
    assert {
        "computation_validation_records",
        "computation_attestations",
        "publication_computation_evidence",
    }.issubset(table_names)
    assert "computation_status" not in {
        column["name"] for column in inspect(database.engine).get_columns("drafts")
    }


def test_invalid_atomic_generation_rolls_back_draft_and_report(store) -> None:
    database, repository = store
    invalid = replace(_validation(), report_json="{not-json")

    with pytest.raises(ComputationEvidenceError, match="valid JSON"):
        _seed(repository, computation_validation=invalid)

    assert repository.list_drafts(current_sources_only=False) == []
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Draft)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(ComputationValidationRecord)
            )
            == 0
        )


def test_unsupported_evidence_rejects_any_material_failed_check(store) -> None:
    _database, repository = store
    value = _validation(status=ValidationStatus.UNSUPPORTED.value)
    report = json.loads(value.report_json)
    report["checks"].append(
        {
            "code": "runtime_identity",
            "status": "failed",
            "message": "The promoted runtime identity did not match.",
            "details": {},
        }
    )
    mixed = replace(
        value,
        report_json=json.dumps(report, sort_keys=True, separators=(",", ":")),
    )

    with pytest.raises(ComputationEvidenceError, match="no failed checks"):
        _seed(repository, computation_validation=mixed)


def test_typed_evidence_rejects_missing_presentation_binding(store) -> None:
    _database, repository = store
    value = _validation()
    report = json.loads(value.report_json)
    report["checks"] = [
        check for check in report["checks"] if check["code"] != "presentation_binding"
    ]
    invalid = replace(
        value,
        report_json=json.dumps(report, sort_keys=True, separators=(",", ":")),
    )

    draft_id = _seed(repository, computation_validation=invalid)
    stored = repository.get_current_computation_validation(draft_id)
    assert stored is not None
    with pytest.raises(ComputationEvidenceError, match="presentation_binding"):
        validate_computation_evidence(stored, require_authorizable=True)


def test_typed_evidence_rejects_fabricated_external_validated_status(store) -> None:
    _database, repository = store
    blueprint = AssessmentComputationBlueprint(
        profile={"family": "numeric", "delivery": "webwork"},
        operation="evaluate",
        expression=ExpressionNode(
            kind="add",
            args=[_integer(2), _integer(3)],
        ),
    )
    report = validate_computation(
        ComputationValidationRequest(
            blueprint=blueprint,
            candidate_expression=_integer(5),
        )
    )
    forged_checks = [
        (
            check.model_copy(
                update={
                    "status": CheckStatus.PASSED,
                    "message": "Caller claims a native receipt passed.",
                }
            )
            if check.code == "native_engine"
            else check
        )
        for check in report.checks
    ]
    forged_checks.append(
        ValidationCheck(
            code="presentation_binding",
            status=CheckStatus.PASSED,
            message="Learner-facing fields were server-bound.",
        )
    )
    forged = report.model_copy(
        update={
            "status": ValidationStatus.VALIDATED,
            "checks": forged_checks,
        }
    )
    value = validation_write(
        blueprint=blueprint,
        report=forged,
        container_digest=f"sha256:{'1' * 64}",
        duration_ms=5,
        engine_validation=None,
    )

    with pytest.raises(
        ComputationEvidenceError,
        match="exact persisted receipt",
    ):
        _seed(repository, computation_validation=value)


def test_append_is_idempotent_but_hash_rebinding_is_rejected(store) -> None:
    _database, repository = store
    draft_id = _seed(repository)
    value = _validation()

    first = repository.append_computation_validation(
        draft_id,
        edit_count=0,
        record=value,
    )
    duplicate = repository.append_computation_validation(
        draft_id,
        edit_count=0,
        record=value,
    )

    assert duplicate.id == first.id
    assert (
        repository.get_computation_validation(
            draft_id,
            edit_count=0,
            report_sha256=first.report_sha256,
        )
        == first
    )
    changed_status = _validation(
        status="partially_validated",
        report_json=value.report_json,
    )
    with pytest.raises(
        ComputationEvidenceError,
        match="status column does not match",
    ):
        repository.append_computation_validation(
            draft_id,
            edit_count=0,
            record=changed_status,
        )
    with pytest.raises(ComputationEvidenceError, match="edit_count"):
        repository.append_computation_validation(
            draft_id,
            edit_count=1,
            record=_validation("wrong-edit"),
        )


def test_concurrent_identical_validation_appends_return_one_record(
    store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository = store
    draft_id = _seed(repository)
    value = _validation("concurrent-duplicate")
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    calls = 0
    original = db_module._new_computation_validation_record

    def synchronized_candidate(*args, **kwargs):
        nonlocal calls
        candidate = original(*args, **kwargs)
        with lock:
            calls += 1
            should_wait = calls <= 2
        if should_wait:
            barrier.wait(timeout=5)
        return candidate

    monkeypatch.setattr(
        db_module,
        "_new_computation_validation_record",
        synchronized_candidate,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                repository.append_computation_validation,
                draft_id,
                edit_count=0,
                record=value,
            )
            for _ in range(2)
        ]
        records = [future.result(timeout=10) for future in futures]

    assert records[0].id == records[1].id
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ComputationValidationRecord)
            )
            == 1
        )


def test_validation_append_loses_concurrent_approval_race_closed(
    store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, repository = store
    draft_id = _seed(repository, computation_validation=_validation("initial"))
    repository.confirm_bloom(draft_id, reviewer="reviewer@example.edu")
    repository.confirm_difficulty(draft_id, reviewer="reviewer@example.edu")
    repository.transition_status(
        draft_id,
        ReviewStatus.READY_TO_PUBLISH,
        reviewer="reviewer@example.edu",
    )
    reached = threading.Event()
    release = threading.Event()
    original = db_module._new_computation_validation_record

    def paused_candidate(*args, **kwargs):
        candidate = original(*args, **kwargs)
        reached.set()
        assert release.wait(timeout=5)
        return candidate

    monkeypatch.setattr(
        db_module,
        "_new_computation_validation_record",
        paused_candidate,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            repository.append_computation_validation,
            draft_id,
            edit_count=0,
            record=replace(
                _validation("new-runtime"),
                container_digest=f"sha256:{'2' * 64}",
            ),
        )
        assert reached.wait(timeout=5)
        repository.transition_status(
            draft_id,
            ReviewStatus.READY_TO_PUBLISH,
            reviewer="second-reviewer@example.edu",
            notes="Concurrent enforce approval.",
        )
        release.set()
        with pytest.raises(ConcurrentDraftUpdateError):
            future.result(timeout=10)

    assert repository.require_draft(draft_id).status == ReviewStatus.READY_TO_PUBLISH
    assert len(repository.list_computation_validations(draft_id)) == 1


def test_same_report_can_refresh_runtime_evidence_and_revokes_prior_approval(
    store,
) -> None:
    _database, repository = store
    value = _validation()
    draft_id = _seed(repository, computation_validation=value)
    repository.confirm_bloom(
        draft_id,
        reviewer="reviewer@example.edu",
    )
    repository.confirm_difficulty(
        draft_id,
        reviewer="reviewer@example.edu",
    )
    repository.transition_status(
        draft_id,
        ReviewStatus.READY_TO_PUBLISH,
        reviewer="reviewer@example.edu",
    )
    first = repository.get_current_computation_validation(draft_id)
    assert first is not None

    refreshed = repository.append_computation_validation(
        draft_id,
        edit_count=0,
        record=replace(
            value,
            container_digest=f"sha256:{'2' * 64}",
        ),
    )

    assert refreshed.id != first.id
    assert refreshed.report_sha256 == first.report_sha256
    assert refreshed.evidence_sha256 != first.evidence_sha256
    assert (
        repository.get_computation_validation(
            draft_id,
            edit_count=0,
            report_sha256=first.report_sha256,
        )
        == refreshed
    )
    history = repository.list_computation_validations(draft_id)
    assert [item.is_current for item in history] == [False, True]
    draft = repository.require_draft(draft_id)
    assert draft.status == ReviewStatus.READY_FOR_REVIEW
    assert draft.bloom_confirmed is False
    assert draft.difficulty_confirmed is False
    assert draft.review_history_json[-1]["event"] == "status_changed"


def test_edit_invalidates_old_evidence_and_can_atomically_append_a_new_report(
    store,
) -> None:
    database, repository = store
    draft_id = _seed(repository, computation_validation=_validation("old"))
    old = repository.get_current_computation_validation(draft_id)
    assert old is not None
    attestation = repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=old.report_sha256,
        attestation=ComputationAttestationWrite(
            specialist_identity="specialist@example.edu",
            rationale="The qualified checks cover this bounded item.",
            qualification_json='{"profile":"numeric-v0"}',
        ),
    )

    with pytest.raises(ComputationEvidenceError, match="valid JSON"):
        repository.edit_draft(
            draft_id,
            _question("This failed edit must roll back."),
            editor="editor@example.edu",
            computation_validation=replace(
                _validation("invalid-edit"),
                report_json="{not-json",
            ),
        )
    unchanged = repository.require_draft(draft_id)
    assert unchanged.edit_count == 0
    assert (
        repository.get_current_computation_validation(draft_id).report_sha256
        == old.report_sha256
    )

    edited = repository.edit_draft(
        draft_id,
        _question("Calculate momentum from the given mass and velocity."),
        editor="editor@example.edu",
        computation_validation=_validation("edited"),
    )

    assert edited.edit_count == 1
    current = repository.get_current_computation_validation(draft_id)
    assert current is not None
    assert current.edit_count == 1
    assert current.report_sha256 != old.report_sha256
    historical = repository.get_computation_validation(
        draft_id,
        edit_count=0,
        report_sha256=old.report_sha256,
    )
    assert historical is not None
    assert historical.is_current is False
    old_attestation = repository.get_computation_attestation(
        attestation.attestation_sha256
    )
    assert old_attestation is not None
    assert old_attestation.is_current is False
    assert repository.list_current_computation_attestations(draft_id) == ()

    with database.session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ComputationValidationRecord)
            )
            == 2
        )
        assert (
            session.scalar(select(func.count()).select_from(ComputationAttestation))
            == 1
        )


def test_attestation_requires_the_exact_current_edit_and_report(store) -> None:
    _database, repository = store
    draft_id = _seed(repository, computation_validation=_validation())
    current = repository.get_current_computation_validation(draft_id)
    assert current is not None
    value = ComputationAttestationWrite(
        specialist_identity="specialist@example.edu",
        rationale="Unsupported portions were independently reviewed.",
    )

    with pytest.raises(ComputationEvidenceError, match="edit_count"):
        repository.append_computation_attestation(
            draft_id,
            edit_count=1,
            report_sha256=current.report_sha256,
            attestation=value,
        )
    with pytest.raises(ComputationEvidenceError, match="current report hash"):
        repository.append_computation_attestation(
            draft_id,
            edit_count=0,
            report_sha256="f" * 64,
            attestation=value,
        )

    first = repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=current.report_sha256,
        attestation=value,
    )
    duplicate = repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=current.report_sha256,
        attestation=value,
    )
    assert duplicate.id == first.id
    assert repository.list_current_computation_attestations(
        draft_id,
        report_sha256=current.report_sha256,
    ) == (first,)


def test_concurrent_identical_attestations_return_one_record(
    store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, repository = store
    draft_id = _seed(repository, computation_validation=_validation())
    current = repository.get_current_computation_validation(draft_id)
    assert current is not None
    value = ComputationAttestationWrite(
        specialist_identity="specialist@example.edu",
        rationale="The same exact specialist review was submitted twice.",
    )
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    calls = 0
    original = db_module._computation_attestation_sha256

    def synchronized_hash(**kwargs):
        nonlocal calls
        digest = original(**kwargs)
        with lock:
            calls += 1
            should_wait = calls <= 2
        if should_wait:
            barrier.wait(timeout=5)
        return digest

    monkeypatch.setattr(
        db_module,
        "_computation_attestation_sha256",
        synchronized_hash,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                repository.append_computation_attestation,
                draft_id,
                edit_count=0,
                report_sha256=current.report_sha256,
                attestation=value,
            )
            for _ in range(2)
        ]
        records = [future.result(timeout=10) for future in futures]

    assert records[0].id == records[1].id
    with database.session() as session:
        assert (
            session.scalar(select(func.count()).select_from(ComputationAttestation))
            == 1
        )


def test_publication_snapshot_is_immutable_and_exactly_bound(store) -> None:
    database, repository = store
    draft_id = _seed(
        repository,
        computation_validation=_validation(status="partially_validated"),
    )
    validation = repository.get_current_computation_validation(draft_id)
    assert validation is not None
    stale_validation = validation
    validation = repository.append_computation_validation(
        draft_id,
        edit_count=0,
        record=_validation("publication-retry", status="partially_validated"),
    )
    attestation = repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=validation.report_sha256,
        attestation=ComputationAttestationWrite(
            specialist_identity="specialist@example.edu",
            rationale="The uncovered native-engine check was manually verified.",
        ),
    )
    draft = repository.require_draft(draft_id)
    publication, created = repository.create_or_get_publication(
        _publication_values(draft, publication_key="a" * 64)
    )
    assert created is True
    with pytest.raises(ComputationEvidenceError, match="latest report"):
        repository.snapshot_publication_computation_evidence(
            publication.id,
            PublicationComputationEvidenceWrite(
                report_sha256=stale_validation.report_sha256,
            ),
        )
    write = PublicationComputationEvidenceWrite(
        report_sha256=validation.report_sha256,
        attestation_sha256s=(attestation.attestation_sha256,),
    )

    frozen = repository.snapshot_publication_computation_evidence(
        publication.id,
        write,
    )
    duplicate = repository.snapshot_publication_computation_evidence(
        publication.id,
        write,
    )

    assert duplicate == frozen
    assert frozen.validation_status == "partially_validated"
    assert frozen.attestation_sha256s == (attestation.attestation_sha256,)
    snapshot = json.loads(frozen.snapshot_json)
    assert snapshot["validation"]["report_json"] == validation.report_json
    assert (
        snapshot["attestations"][0]["attestation_sha256"]
        == attestation.attestation_sha256
    )
    assert repository.get_publication_computation_evidence(publication.id) == frozen

    second_attestation = repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=validation.report_sha256,
        attestation=ComputationAttestationWrite(
            specialist_identity="second-specialist@example.edu",
            rationale="A second independent check.",
        ),
    )
    with pytest.raises(ComputationEvidenceConflictError, match="already frozen"):
        repository.snapshot_publication_computation_evidence(
            publication.id,
            PublicationComputationEvidenceWrite(
                report_sha256=validation.report_sha256,
                attestation_sha256s=(
                    attestation.attestation_sha256,
                    second_attestation.attestation_sha256,
                ),
            ),
        )

    with database.session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PublicationComputationEvidence)
            )
            == 1
        )
