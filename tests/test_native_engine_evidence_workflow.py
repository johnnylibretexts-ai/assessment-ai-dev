from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient

import app.computation_policy as computation_policy
import app.db as db_module
import app.main as main_module
import app.native_engine_runner as runner_module
import app.parameterized as parameterized_module
from app.computation import (
    AssessmentComputationBlueprint,
    ComputationProfile,
    ExpressionNode,
    ValidationStatus,
    VariableSpec,
    compute_blueprint,
)
from app.computation_client import (
    IN_PROCESS_RUNTIME_MANIFEST_SHA256,
    ComputationUnavailableError,
    InProcessAssessmentComputationClient,
)
from app.computation_policy import (
    QualifiedComputationRuntime,
    evaluate_computation_gate,
    require_computation_gate,
)
from app.computation_workflow import (
    build_computation_artifacts,
    revalidate_edited_draft,
)
from app.config import Settings
from app.db import (
    ComputationAttestationWrite,
    ComputationEvidenceError,
    DraftRepository,
    DraftWrite,
    draft_content_sha256,
    init_database,
    validate_computation_evidence,
)
from app.native_engine_runner import (
    NATIVE_RUNNER_RECEIPT_VERSION,
    NativeEngineRunnerReceipt,
    NativeEngineRunnerRequest,
    NativeRunnerTimeoutError,
    NativeRunnerUnavailableError,
    QualifiedNativeEngineRunner,
    build_native_seed_observation,
    seed_observations_sha256,
    seed_plan_sha256,
)
from app.native_engine_evidence import native_verification_plan
from app.parameterized import (
    TYPED_COMPUTATION_COMPILER_VERSION,
    QualifiedFormulaAdapterPromotion,
    typed_formula_submission,
    typed_parameterized_submission_pair,
)
from app.pipeline import ReviewService
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    COMPUTATION_RESULT_SLOT,
    COMPUTATION_TASK_SLOT,
    Concept,
    Critique,
    Difficulty,
    ItemResponse,
    NormalizedPage,
    Paragraph,
    ParameterVariable,
    ParameterizedItemSpec,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
)


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


def _symbol(name: str) -> ExpressionNode:
    return ExpressionNode(kind="symbol", symbol=name)


def _blueprint() -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="numeric", delivery="webwork"),
        operation="evaluate",
        expression=ExpressionNode(
            kind="add",
            args=[_symbol("a"), _integer(1)],
        ),
        variables=[
            VariableSpec(
                name="a",
                domain="integer",
                minimum=_integer(1),
                maximum=_integer(3),
                step=_integer(1),
            )
        ],
    )


def _formula_blueprint() -> AssessmentComputationBlueprint:
    x = _symbol("x")
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(family="algebraic", delivery="webwork"),
        operation="equivalent",
        expression=ExpressionNode(
            kind="pow",
            args=[
                ExpressionNode(kind="add", args=[x, _integer(1)]),
                _integer(2),
            ],
        ),
        comparison_expression=ExpressionNode(
            kind="add",
            args=[
                ExpressionNode(
                    kind="add",
                    args=[
                        ExpressionNode(kind="pow", args=[x, _integer(2)]),
                        ExpressionNode(
                            kind="mul",
                            args=[_integer(2), x],
                        ),
                    ],
                ),
                _integer(1),
            ],
        ),
        variables=[VariableSpec(name="x")],
    )


def _provider_draft() -> QuestionDraft:
    return QuestionDraft(
        item_type=AssessmentItemType.WEBWORK,
        concept_label="Computed quantity",
        stem=f"Use the stated relationship. {COMPUTATION_TASK_SLOT}",
        response=ItemResponse(
            parameterized=ParameterizedItemSpec(
                engine="webwork",
                variables=[ParameterVariable(name="z", minimum=1, maximum=3, step=1)],
                prompt_template="Provider value {z}.",
                answer_expression="z + 99",
                explanation_template="Provider explanation {z}.",
            )
        ),
        explanation=f"Apply the relationship. {COMPUTATION_RESULT_SLOT}",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _page() -> NormalizedPage:
    text = "A measured quantity is one more than the stated parameter."
    return NormalizedPage(
        title="Computed quantity",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            canonical_url=(
                "https://dev.libretexts.org/Sandboxes/johnnyphung/Demo/NativeReceipt"
            ),
            path="Sandboxes/johnnyphung/Demo/NativeReceipt",
            page_id="native-receipt-1",
        ),
    )


def _promotion() -> QualifiedNativeEngineRunner:
    return QualifiedNativeEngineRunner(
        runner_id="qualified-runner",
        runner_version="runner-v0",
        runner_manifest_sha256="1" * 64,
        runner_image_digest=f"sha256:{'2' * 64}",
        engine="webwork",
        compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        answer_kind="numeric",
        native_grader="MathObjects::Real::cmp",
        engine_image_digest=f"sha256:{'3' * 64}",
        adapter_image_digest=None,
        network_attestation_sha256="4" * 64,
        qualification_report_sha256="5" * 64,
        promotion_approval_sha256="6" * 64,
    )


def _formula_runner_promotion() -> QualifiedNativeEngineRunner:
    return replace(
        _promotion(),
        answer_kind="formula",
        native_grader="MathObjects::Formula::cmp",
    )


def _formula_adapter_promotion() -> QualifiedFormulaAdapterPromotion:
    return QualifiedFormulaAdapterPromotion(
        engine="webwork",
        production_compiler_versions=frozenset({TYPED_COMPUTATION_COMPILER_VERSION}),
        families=frozenset({"algebraic"}),
        operations=frozenset({"substitute", "expand", "factor", "equivalent"}),
        qualification_compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        qualification_manifest_sha256="7" * 64,
        qualification_report_sha256="8" * 64,
        engine_image_digest=f"sha256:{'3' * 64}",
        adapter_image_digest=None,
        native_grader="MathObjects::Formula::cmp",
        promotion_approval_sha256="9" * 64,
    )


def _receipt(
    request: NativeEngineRunnerRequest,
    promotion: QualifiedNativeEngineRunner,
    *,
    blueprint: AssessmentComputationBlueprint | None = None,
) -> NativeEngineRunnerReceipt:
    resolved_blueprint = blueprint or _blueprint()
    plan = native_verification_plan(
        resolved_blueprint,
        compute_blueprint(resolved_blueprint),
    )
    observations = []
    for index, seed in enumerate(request.seeds):
        values = {
            variable.name: int(variable.minimum) + (index % 3) * int(variable.step)
            for variable in plan.spec.variables
        }
        correct, wrong = typed_parameterized_submission_pair(
            plan.spec,
            plan.answer_expression,
            values,
        )
        alternate = (
            typed_formula_submission(
                plan.spec,
                plan.alternate_expression,
                values,
            )
            if plan.alternate_expression is not None
            else None
        )
        render_sha256 = hashlib.sha256(
            json.dumps(
                {"seed": seed, "values": values},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        observations.append(
            build_native_seed_observation(
                seed=seed,
                observed_variables=values,
                correct_submission_sha256=hashlib.sha256(correct.encode()).hexdigest(),
                alternate_correct_submission_sha256=(
                    hashlib.sha256(alternate.encode()).hexdigest()
                    if alternate is not None
                    else None
                ),
                alternate_correct_answer_accepted=(
                    True if alternate is not None else None
                ),
                wrong_submission_sha256=hashlib.sha256(wrong.encode()).hexdigest(),
                render_sha256=render_sha256,
                repeat_render_sha256=render_sha256,
            )
        )
    aggregate_render = runner_module._canonical_sha256(
        [item.render_sha256 for item in observations]
    )
    payload = {
        "schema_version": NATIVE_RUNNER_RECEIPT_VERSION,
        "runner_id": promotion.runner_id,
        "runner_version": promotion.runner_version,
        "runner_manifest_sha256": promotion.runner_manifest_sha256,
        "runner_image_digest": promotion.runner_image_digest,
        "qualification_report_sha256": promotion.qualification_report_sha256,
        "promotion_approval_sha256": promotion.promotion_approval_sha256,
        "engine": request.engine,
        "compiler_version": request.compiler_version,
        "answer_kind": request.answer_kind,
        "native_grader": promotion.native_grader,
        "formula_adapter_promotion": (
            request.formula_adapter_promotion.model_dump(
                mode="json",
                exclude_none=False,
            )
            if request.formula_adapter_promotion is not None
            else None
        ),
        "source_sha256": request.source_sha256,
        "blueprint_sha256": request.blueprint_sha256,
        "draft_sha256": request.draft_sha256,
        "request_sha256": request.request_sha256,
        "seeds": request.seeds,
        "seed_plan_sha256": seed_plan_sha256(request.seeds),
        "seed_receipts_sha256": seed_observations_sha256(observations),
        "seeds_validated": 25,
        "observations": [
            item.model_dump(mode="json", exclude_none=False) for item in observations
        ],
        "engine_image_digest": promotion.engine_image_digest,
        "adapter_image_digest": promotion.adapter_image_digest,
        "network_attestation_sha256": promotion.network_attestation_sha256,
        "correct_answer_accepted": True,
        "wrong_answer_rejected": True,
        "rendered": True,
        "render_sha256": aggregate_render,
        "repeat_render_sha256": aggregate_render,
        "warnings_count": 0,
        "errors_count": 0,
        "outbound_request_count": 0,
        "passed": True,
    }
    payload["receipt_sha256"] = runner_module._canonical_sha256(payload)
    return NativeEngineRunnerReceipt.model_validate(payload)


class _ReceiptRunner:
    def __init__(
        self,
        promotion: QualifiedNativeEngineRunner | None,
        *,
        failure: Exception | None = None,
        blueprint: AssessmentComputationBlueprint | None = None,
    ) -> None:
        self._promotion = promotion
        self._failure = failure
        self._blueprint = blueprint
        self.calls: list[NativeEngineRunnerRequest] = []

    @property
    def runner_id(self) -> str:
        return "qualified-runner"

    def qualification_for(
        self,
        *,
        engine: str,
        compiler_version: str,
        answer_kind: str,
    ) -> QualifiedNativeEngineRunner | None:
        promotion = self._promotion
        if promotion is None or (
            promotion.engine,
            promotion.compiler_version,
            promotion.answer_kind,
        ) != (engine, compiler_version, answer_kind):
            return None
        return promotion

    async def validate(
        self,
        request: NativeEngineRunnerRequest,
    ) -> NativeEngineRunnerReceipt:
        self.calls.append(request)
        if self._failure is not None:
            raise self._failure
        assert self._promotion is not None
        return _receipt(
            request,
            self._promotion,
            blueprint=self._blueprint,
        )

    async def aclose(self) -> None:
        return None


class _FinalValidationUnavailableClient(InProcessAssessmentComputationClient):
    async def validate(self, _request):
        raise ComputationUnavailableError("private final sidecar detail")


def _promote(
    monkeypatch: pytest.MonkeyPatch,
    promotion: QualifiedNativeEngineRunner,
    *,
    digest: str,
    image_reference: str,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "QUALIFIED_NATIVE_ENGINE_RUNNERS",
        MappingProxyType(
            {
                (
                    promotion.runner_id,
                    promotion.engine,
                    promotion.compiler_version,
                    promotion.answer_kind,
                ): promotion
            }
        ),
    )
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            digest: QualifiedComputationRuntime(
                image_reference=image_reference,
                container_digest=digest,
                runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
                qualification_report_sha256="b" * 64,
                families=frozenset({"numeric", "algebraic"}),
            )
        },
    )


async def _validated_artifacts(
    monkeypatch: pytest.MonkeyPatch,
):
    digest = f"sha256:{'c' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    promotion = _promotion()
    _promote(
        monkeypatch,
        promotion,
        digest=digest,
        image_reference=image_reference,
    )
    runner = _ReceiptRunner(promotion)
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=_blueprint(),
        draft=_provider_draft(),
        container_digest=digest,
        image_reference=image_reference,
        native_engine_runner=runner,
    )
    return artifacts, runner


async def _validated_formula_artifacts(
    monkeypatch: pytest.MonkeyPatch,
):
    digest = f"sha256:{'c' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    blueprint = _formula_blueprint()
    runner_promotion = _formula_runner_promotion()
    _promote(
        monkeypatch,
        runner_promotion,
        digest=digest,
        image_reference=image_reference,
    )
    adapter_promotion = _formula_adapter_promotion()
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        MappingProxyType(
            {
                (
                    "webwork",
                    TYPED_COMPUTATION_COMPILER_VERSION,
                ): adapter_promotion
            }
        ),
    )
    runner = _ReceiptRunner(
        runner_promotion,
        blueprint=blueprint,
    )
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=_provider_draft(),
        container_digest=digest,
        image_reference=image_reference,
        native_engine_runner=runner,
    )
    return artifacts, runner


def _persist_artifacts(
    repository: DraftRepository,
    *,
    artifacts,
    pipeline_version: str,
) -> int:
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version=pipeline_version,
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="One more than the parameter.",
                    source_paragraphs=[0],
                ),
                raw=_provider_draft(),
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                engine_validation=artifacts.engine_validation,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


@pytest.mark.asyncio
async def test_exact_native_receipt_promotes_report_and_persists_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, runner = await _validated_artifacts(monkeypatch)

    assert artifacts.report.status == ValidationStatus.VALIDATED
    assert len(runner.calls) == 1
    request = runner.calls[0]
    assert request.seeds == artifacts.report.seed_plan
    assert "probes" not in request.model_fields_set
    native_check = next(
        check for check in artifacts.report.checks if check.code == "native_engine"
    )
    assert native_check.status.value == "passed"
    persisted_engine = json.loads(artifacts.persistence.engine_evidence_json)
    assert persisted_engine["native_receipt"]["request_sha256"] == (
        request.request_sha256
    )
    assert len(persisted_engine["native_receipt"]["observations"]) == 25
    assert request.formula_adapter_promotion is None
    assert persisted_engine["native_receipt"]["formula_adapter_promotion"] is None

    db_verifications: list[str] = []
    original_db_verifier = db_module.verify_native_engine_observations

    def observe_db_verification(**kwargs: object) -> None:
        receipt = kwargs["receipt"]
        assert isinstance(receipt, NativeEngineRunnerReceipt)
        db_verifications.append(receipt.receipt_sha256)
        original_db_verifier(**kwargs)

    monkeypatch.setattr(
        db_module,
        "verify_native_engine_observations",
        observe_db_verification,
    )
    database = init_database(f"sqlite:///{tmp_path / 'native.db'}")
    repository = DraftRepository(database)
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="native-receipt-v0",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="One more than the parameter.",
                    source_paragraphs=[0],
                ),
                raw=_provider_draft(),
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                engine_validation=artifacts.engine_validation,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    record = repository.get_current_computation_validation(stored.draft_ids[0])
    assert record is not None
    assert (
        validate_computation_evidence(
            record,
            require_authorizable=True,
        ).status
        == ValidationStatus.VALIDATED
    )
    assert db_verifications == [
        persisted_engine["native_receipt"]["receipt_sha256"],
        persisted_engine["native_receipt"]["receipt_sha256"],
    ]
    database.dispose()


@pytest.mark.asyncio
async def test_formula_promotion_revocation_blocks_policy_and_atomic_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, runner = await _validated_formula_artifacts(monkeypatch)

    assert artifacts.report.status == ValidationStatus.VALIDATED
    assert len(runner.calls) == 1
    request = runner.calls[0]
    assert request.formula_adapter_promotion is not None
    assert request.formula_adapter_promotion.family == "algebraic"
    assert request.formula_adapter_promotion.operation == "equivalent"
    engine_payload = json.loads(artifacts.persistence.engine_evidence_json)
    receipt_payload = engine_payload["native_receipt"]
    assert (
        engine_payload["formula_adapter_promotion"]
        == (receipt_payload["formula_adapter_promotion"])
    )
    assert receipt_payload["formula_adapter_promotion"] == (
        request.formula_adapter_promotion.model_dump(
            mode="json",
            exclude_none=False,
        )
    )
    assert all(
        observation["alternate_correct_submission_sha256"] is not None
        and observation["alternate_correct_answer_accepted"] is True
        and observation["alternate_correct_submission_sha256"]
        != observation["correct_submission_sha256"]
        for observation in receipt_payload["observations"]
    )

    digest = f"sha256:{'c' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'formula-revocation.db'}",
        hotspot_media_dir=tmp_path / "media",
        computation_mode="enforce",
        computation_image_reference=image_reference,
        computation_family_allowlist="algebraic",
    )
    database = init_database(settings.database_url)
    repository = DraftRepository(database)
    draft_id = _persist_artifacts(
        repository,
        artifacts=artifacts,
        pipeline_version="native-formula-revocation-v0",
    )
    record = repository.get_current_computation_validation(draft_id)
    assert record is not None
    assert (
        validate_computation_evidence(
            record,
            require_authorizable=True,
        ).status
        == ValidationStatus.VALIDATED
    )
    decision = require_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
        action="approval",
    )
    assert decision.formula_adapter_promotion_sha256 == (
        request.formula_adapter_promotion.identity_sha256
    )

    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        MappingProxyType({}),
    )

    with pytest.raises(
        ComputationEvidenceError,
        match="unavailable or revoked",
    ):
        validate_computation_evidence(
            record,
            require_authorizable=True,
        )
    blocked = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
    )
    assert blocked.allowed is False
    assert blocked.reason_code == "invalid_computation_evidence"
    with pytest.raises(
        ComputationEvidenceError,
        match="unavailable or revoked",
    ):
        ReviewService(repository, settings).decide(
            draft_id,
            ReviewDecision(
                status=ReviewStatus.READY_TO_PUBLISH,
                bloom_confirmed=True,
                difficulty_confirmed=True,
                reviewer_notes="Revoked formula evidence must not approve.",
            ),
            reviewer="reviewer@example.edu",
            computation_binding=decision.atomic_binding(settings),
        )
    assert repository.require_draft(draft_id).status == ReviewStatus.READY_FOR_REVIEW
    database.dispose()


@pytest.mark.asyncio
async def test_formula_promotion_replacement_under_same_key_invalidates_old_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, _runner = await _validated_formula_artifacts(monkeypatch)
    database = init_database(f"sqlite:///{tmp_path / 'formula-replacement.db'}")
    repository = DraftRepository(database)
    draft_id = _persist_artifacts(
        repository,
        artifacts=artifacts,
        pipeline_version="native-formula-replacement-v0",
    )
    record = repository.get_current_computation_validation(draft_id)
    assert record is not None

    replacement = replace(
        _formula_adapter_promotion(),
        qualification_report_sha256="a" * 64,
    )
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        MappingProxyType(
            {
                (
                    "webwork",
                    TYPED_COMPUTATION_COMPILER_VERSION,
                ): replacement
            }
        ),
    )

    assert (
        validate_computation_evidence(
            record,
            require_authorizable=False,
        ).status
        == ValidationStatus.VALIDATED
    )
    with pytest.raises(
        ComputationEvidenceError,
        match="does not match its current promotion",
    ):
        validate_computation_evidence(
            record,
            require_authorizable=True,
        )
    database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revoke", "same_key_replacement"])
async def test_partial_formula_evidence_rechecks_promotion_before_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    mutation: str,
) -> None:
    digest = f"sha256:{'c' * 64}"
    image_reference = f"registry.example/assessment-computation@{digest}"
    blueprint = _formula_blueprint()
    runner_promotion = _formula_runner_promotion()
    _promote(
        monkeypatch,
        runner_promotion,
        digest=digest,
        image_reference=image_reference,
    )
    adapter_promotion = _formula_adapter_promotion()
    key = ("webwork", TYPED_COMPUTATION_COMPILER_VERSION)
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        MappingProxyType({key: adapter_promotion}),
    )
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=blueprint,
        draft=_provider_draft(),
        container_digest=digest,
        image_reference=image_reference,
        native_engine_runner=_ReceiptRunner(
            runner_promotion,
            failure=NativeRunnerUnavailableError("private unavailable detail"),
            blueprint=blueprint,
        ),
    )
    assert artifacts.report.status == ValidationStatus.PARTIALLY_VALIDATED
    engine_payload = json.loads(artifacts.persistence.engine_evidence_json)
    assert "native_receipt" not in engine_payload
    assert engine_payload["answer_kind"] == "formula"
    assert engine_payload["formula_adapter_promotion"]["identity_sha256"]

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / f'partial-{mutation}.db'}",
        hotspot_media_dir=tmp_path / "media",
        computation_mode="enforce",
        computation_image_reference=image_reference,
        computation_family_allowlist="algebraic",
        computation_specialist_subject_allowlist="trusted-specialist",
    )
    database = init_database(settings.database_url)
    repository = DraftRepository(database)
    draft_id = _persist_artifacts(
        repository,
        artifacts=artifacts,
        pipeline_version=f"partial-formula-{mutation}-v0",
    )
    current = repository.get_current_computation_validation(draft_id)
    assert current is not None
    repository.append_computation_attestation(
        draft_id,
        edit_count=0,
        report_sha256=current.report_sha256,
        attestation=ComputationAttestationWrite(
            specialist_identity="trusted-specialist",
            rationale="The partial formula item was independently checked.",
        ),
    )
    decision = require_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
        action="approval",
    )
    assert decision.allowed is True
    assert (
        decision.formula_adapter_promotion_sha256
        == (engine_payload["formula_adapter_promotion"]["identity_sha256"])
    )

    replacement = (
        {}
        if mutation == "revoke"
        else {
            key: replace(
                adapter_promotion,
                qualification_report_sha256="a" * 64,
            )
        }
    )
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        MappingProxyType(replacement),
    )
    blocked = evaluate_computation_gate(
        settings,
        repository,
        repository.require_draft(draft_id),
    )
    assert blocked.allowed is False
    assert blocked.reason_code == "invalid_computation_evidence"
    with pytest.raises(ComputationEvidenceError):
        ReviewService(repository, settings).decide(
            draft_id,
            ReviewDecision(
                status=ReviewStatus.READY_TO_PUBLISH,
                bloom_confirmed=True,
                difficulty_confirmed=True,
                reviewer_notes="Stale formula promotion must not authorize.",
            ),
            reviewer="reviewer@example.edu",
            computation_binding=decision.atomic_binding(settings),
        )
    assert repository.require_draft(draft_id).status == ReviewStatus.READY_FOR_REVIEW
    database.dispose()


@pytest.mark.asyncio
async def test_persistence_rejects_receipt_compiler_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, _runner = await _validated_artifacts(monkeypatch)
    engine_payload = json.loads(artifacts.persistence.engine_evidence_json)
    engine_payload["source_sha256"] = "0" * 64
    tampered = replace(
        artifacts.persistence,
        engine_evidence_json=json.dumps(
            engine_payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    database = init_database(f"sqlite:///{tmp_path / 'tampered.db'}")
    repository = DraftRepository(database)
    with pytest.raises(
        ComputationEvidenceError,
        match="persisted compiler artifact",
    ):
        repository.replace_generated_drafts(
            page=_page(),
            pipeline_version="native-receipt-tampered-v0",
            drafts=[
                DraftWrite(
                    position=0,
                    concept=Concept(
                        label="Computed quantity",
                        description="One more than the parameter.",
                        source_paragraphs=[0],
                    ),
                    raw=_provider_draft(),
                    critique=Critique(issues=[], revision_required=False),
                    revised=artifacts.draft,
                    engine_validation=artifacts.engine_validation,
                    computation_validation=tampered,
                )
            ],
            llm_calls=[],
        )
    database.dispose()


@pytest.mark.asyncio
async def test_persistence_rejects_receipt_bound_to_another_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, _runner = await _validated_artifacts(monkeypatch)
    changed = artifacts.draft.model_copy(
        update={"stem": f"{artifacts.draft.stem} Review copy."},
        deep=True,
    )
    database = init_database(f"sqlite:///{tmp_path / 'wrong-draft.db'}")
    repository = DraftRepository(database)
    with pytest.raises(
        ComputationEvidenceError,
        match="exact draft, blueprint",
    ):
        repository.replace_generated_drafts(
            page=_page(),
            pipeline_version="native-receipt-wrong-draft-v0",
            drafts=[
                DraftWrite(
                    position=0,
                    concept=Concept(
                        label="Computed quantity",
                        description="One more than the parameter.",
                        source_paragraphs=[0],
                    ),
                    raw=_provider_draft(),
                    critique=Critique(issues=[], revision_required=False),
                    revised=changed,
                    engine_validation=artifacts.engine_validation,
                    computation_validation=artifacts.persistence,
                )
            ],
            llm_calls=[],
        )
    database.dispose()


@pytest.mark.asyncio
async def test_persistence_rejects_runtime_receipt_without_registry_promotion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, _runner = await _validated_artifacts(monkeypatch)
    monkeypatch.setattr(
        runner_module,
        "QUALIFIED_NATIVE_ENGINE_RUNNERS",
        MappingProxyType({}),
    )
    database = init_database(f"sqlite:///{tmp_path / 'unqualified.db'}")
    repository = DraftRepository(database)
    with pytest.raises(
        ComputationEvidenceError,
        match="source-controlled qualification",
    ):
        repository.replace_generated_drafts(
            page=_page(),
            pipeline_version="native-receipt-unqualified-v0",
            drafts=[
                DraftWrite(
                    position=0,
                    concept=Concept(
                        label="Computed quantity",
                        description="One more than the parameter.",
                        source_paragraphs=[0],
                    ),
                    raw=_provider_draft(),
                    critique=Critique(issues=[], revision_required=False),
                    revised=artifacts.draft,
                    engine_validation=artifacts.engine_validation,
                    computation_validation=artifacts.persistence,
                )
            ],
            llm_calls=[],
        )
    database.dispose()


@pytest.mark.asyncio
async def test_external_edit_refreshes_native_receipt_for_new_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    artifacts, _initial_runner = await _validated_artifacts(monkeypatch)
    database = init_database(f"sqlite:///{tmp_path / 'edit.db'}")
    repository = DraftRepository(database)
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="native-receipt-edit-v0",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="One more than the parameter.",
                    source_paragraphs=[0],
                ),
                raw=_provider_draft(),
                critique=Critique(issues=[], revision_required=False),
                revised=artifacts.draft,
                engine_validation=artifacts.engine_validation,
                computation_validation=artifacts.persistence,
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    before = repository.require_draft(draft_id)
    prior = repository.get_current_computation_validation(draft_id)
    assert prior is not None
    runner = _ReceiptRunner(_promotion())

    rebound, refreshed = await revalidate_edited_draft(
        client=InProcessAssessmentComputationClient(),
        current_record=prior,
        draft=_provider_draft(),
        container_digest=f"sha256:{'c' * 64}",
        image_reference=(f"registry.example/assessment-computation@sha256:{'c' * 64}"),
        native_engine_runner=runner,
    )
    assert refreshed.status == ValidationStatus.VALIDATED.value
    assert len(runner.calls) == 1
    repository.edit_draft(
        draft_id,
        rebound,
        editor="faculty@example.edu",
        computation_validation=refreshed,
        expected_edit_count=before.edit_count,
        expected_draft_sha256=draft_content_sha256(before.current_json),
    )

    current = repository.get_current_computation_validation(draft_id)
    history = repository.list_computation_validations(draft_id)
    assert current is not None and current.edit_count == 1
    assert current.status == ValidationStatus.VALIDATED.value
    assert len(history) == 2
    assert history[0].is_current is False
    database.dispose()


@pytest.mark.asyncio
async def test_native_runner_unavailable_is_partial_but_timeout_fails_closed() -> None:
    promotion = _promotion()
    unavailable = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=_blueprint(),
        draft=_provider_draft(),
        container_digest="unavailable",
        native_engine_runner=_ReceiptRunner(
            promotion,
            failure=NativeRunnerUnavailableError("private detail"),
        ),
    )
    assert unavailable.report.status == ValidationStatus.PARTIALLY_VALIDATED
    native = next(
        check for check in unavailable.report.checks if check.code == "native_engine"
    )
    assert native.status.value == "inconclusive"
    assert native.details == {"reason": "native_runner_unavailable"}
    assert "native_receipt" not in json.loads(
        unavailable.persistence.engine_evidence_json
    )

    timeout = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=_blueprint(),
        draft=_provider_draft(),
        container_digest="unavailable",
        native_engine_runner=_ReceiptRunner(
            promotion,
            failure=NativeRunnerTimeoutError("private timeout detail"),
        ),
    )
    assert timeout.report.status == ValidationStatus.VALIDATION_FAILED
    assert timeout.report.result is None
    assert json.loads(timeout.persistence.engine_evidence_json) == {}
    assert "private timeout detail" not in timeout.persistence.report_json


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_path", ["native_timeout", "final_sidecar"])
async def test_formula_material_failures_persist_without_compiler_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    failure_path: str,
) -> None:
    blueprint = _formula_blueprint()
    adapter_promotion = _formula_adapter_promotion()
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        MappingProxyType(
            {
                (
                    "webwork",
                    TYPED_COMPUTATION_COMPILER_VERSION,
                ): adapter_promotion
            }
        ),
    )
    client = (
        _FinalValidationUnavailableClient()
        if failure_path == "final_sidecar"
        else InProcessAssessmentComputationClient()
    )
    runner = (
        None
        if failure_path == "final_sidecar"
        else _ReceiptRunner(
            _formula_runner_promotion(),
            failure=NativeRunnerTimeoutError("private formula timeout detail"),
            blueprint=blueprint,
        )
    )
    artifacts = await build_computation_artifacts(
        client=client,
        blueprint=blueprint,
        draft=_provider_draft(),
        container_digest="unavailable",
        native_engine_runner=runner,
    )
    assert artifacts.report.status == ValidationStatus.VALIDATION_FAILED
    assert artifacts.report.result is None
    assert artifacts.engine_validation is None
    assert json.loads(artifacts.persistence.engine_evidence_json) == {}

    database = init_database(f"sqlite:///{tmp_path / f'formula-{failure_path}.db'}")
    repository = DraftRepository(database)
    draft_id = _persist_artifacts(
        repository,
        artifacts=artifacts,
        pipeline_version=f"formula-{failure_path}-v0",
    )
    record = repository.get_current_computation_validation(draft_id)
    assert record is not None
    assert (
        validate_computation_evidence(
            record,
            require_authorizable=False,
        ).status
        == ValidationStatus.VALIDATION_FAILED
    )
    database.dispose()


@pytest.mark.asyncio
async def test_unqualified_runner_is_inconclusive_and_never_called() -> None:
    runner = _ReceiptRunner(None)
    artifacts = await build_computation_artifacts(
        client=InProcessAssessmentComputationClient(),
        blueprint=_blueprint(),
        draft=_provider_draft(),
        container_digest="unavailable",
        native_engine_runner=runner,
    )
    assert artifacts.report.status == ValidationStatus.PARTIALLY_VALIDATED
    assert runner.calls == []
    native = next(
        check for check in artifacts.report.checks if check.code == "native_engine"
    )
    assert native.details == {"reason": "native_runner_unqualified"}


def test_off_mode_never_constructs_native_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def forbidden_constructor(*_args, **_kwargs):
        raise AssertionError("off mode constructed the native runner")

    monkeypatch.setattr(
        main_module,
        "UnixSocketNativeEngineRunner",
        forbidden_constructor,
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'off.db'}",
        hotspot_media_dir=tmp_path / "media",
        computation_mode="off",
        computation_native_runner_socket_path=("/run/assessment-native/runner.sock"),
        computation_native_runner_id="qualified-runner",
    )
    app = main_module.create_app(settings)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert "assessment_computation" not in response.json()
        assert app.state.native_engine_runner is None
