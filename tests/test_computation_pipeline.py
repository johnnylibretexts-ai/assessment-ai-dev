from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

import app.computation_policy as computation_policy
import app.parameterized as parameterized_module
import app.pipeline as pipeline_module
from app.computation import (
    AssessmentComputationBlueprint,
    AssessmentValidationReport,
    ComputationProfile,
    ComputationValidationRequest,
    ExpressionNode,
    ValidationStatus,
)
from app.computation_client import (
    IN_PROCESS_RUNTIME_MANIFEST_SHA256,
    ComputationClientError,
    ComputationProtocolError,
    ComputationRequestTooLargeError,
    ComputationResponseTooLargeError,
    ComputationServiceError,
    ComputationTimeoutError,
    ComputationUnavailableError,
    InProcessAssessmentComputationClient,
)
from app.computation_workflow import ComputationWorkflowError
from app.computation_policy import QualifiedComputationRuntime
from app.db import DraftRepository, init_database
from app.llm import LLMAttemptMetadata, LLMCallMetadata, LLMResult
from app.pipeline import AssessmentPipeline, PIPELINE_VERSION
from app.parameterized import (
    TYPED_COMPUTATION_COMPILER_VERSION,
    QualifiedFormulaAdapterPromotion,
)
from app.schemas import (
    COMPUTATION_RESULT_SLOT,
    COMPUTATION_TASK_SLOT,
    AssessmentItemType,
    BloomLevel,
    Choice,
    Concept,
    ConceptBatch,
    ComputationQuestionDraft,
    Critique,
    Difficulty,
    GenerateRequest,
    ItemResponse,
    NormalizedPage,
    ParameterVariable,
    ParameterizedItemSpec,
    Paragraph,
    QuestionDraft,
    SourceInfo,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def test_formula_adapter_registry_versions_external_algebra_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ComputationProfile(family="algebraic", delivery="webwork")
    monkeypatch.setattr(
        pipeline_module,
        "formula_adapter_registry_sha256",
        lambda: "1" * 64,
    )
    first = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.WEBWORK,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
    )
    numeric_before = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.WEBWORK,),
        include_hint_ladder=False,
        computation_profile=ComputationProfile(
            family="numeric",
            delivery="webwork",
        ),
        computation_mode="assist",
    )
    monkeypatch.setattr(
        pipeline_module,
        "formula_adapter_registry_sha256",
        lambda: "2" * 64,
    )
    replacement = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.WEBWORK,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
    )
    numeric_after = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.WEBWORK,),
        include_hint_ladder=False,
        computation_profile=ComputationProfile(
            family="numeric",
            delivery="webwork",
        ),
        computation_mode="assist",
    )

    assert first != replacement
    assert numeric_before == numeric_after


def test_computation_runtime_identity_versions_cache_key_and_preserves_off_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ComputationProfile(family="numeric", delivery="numerical")
    digest = f"sha256:{'1' * 64}"
    image_reference = f"registry.example/computation@{digest}"
    monkeypatch.setattr(
        pipeline_module,
        "computation_runtime_promotion_sha256",
        lambda **_identity: "2" * 64,
    )
    baseline = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="3" * 64,
    )
    changed_manifest = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="4" * 64,
    )
    monkeypatch.setattr(
        pipeline_module,
        "computation_runtime_promotion_sha256",
        lambda **_identity: "5" * 64,
    )
    changed_promotion = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="3" * 64,
    )
    changed_image = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=(f"registry.example/computation@sha256:{'6' * 64}"),
        computation_container_digest=f"sha256:{'6' * 64}",
        runtime_manifest_sha256="3" * 64,
    )
    failed_readiness = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256=None,
        runtime_ready_failure_code="computation_unavailable",
    )

    assert (
        len(
            {
                baseline,
                changed_manifest,
                changed_promotion,
                changed_image,
                failed_readiness,
            }
        )
        == 5
    )
    assert (
        pipeline_module._request_pipeline_version(
            PIPELINE_VERSION,
            (AssessmentItemType.MULTIPLE_CHOICE,),
            include_hint_ladder=False,
            computation_profile=None,
            computation_mode="off",
            computation_image_reference=image_reference,
            computation_container_digest=digest,
            runtime_manifest_sha256="3" * 64,
        )
        == PIPELINE_VERSION
    )


def test_runtime_registry_state_versions_absent_and_malformed_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ComputationProfile(family="numeric", delivery="numerical")
    digest = f"sha256:{'8' * 64}"
    image_reference = f"registry.example/computation@{digest}"

    monkeypatch.setattr(computation_policy, "QUALIFIED_COMPUTATION_RUNTIMES", {})
    absent = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="1" * 64,
    )
    malformed = QualifiedComputationRuntime(
        image_reference=image_reference,
        container_digest=digest,
        runtime_manifest_sha256="1" * 64,
        qualification_report_sha256="2" * 64,
        families={"numeric"},  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {digest: malformed},
    )
    first_malformed = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="1" * 64,
    )
    assert first_malformed == pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="1" * 64,
    )
    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            digest: QualifiedComputationRuntime(
                image_reference=image_reference,
                container_digest=digest,
                runtime_manifest_sha256="1" * 64,
                qualification_report_sha256="not-a-sha256",
                families=frozenset({"numeric"}),
            )
        },
    )
    second_malformed = pipeline_module._request_pipeline_version(
        PIPELINE_VERSION,
        (AssessmentItemType.NUMERICAL,),
        include_hint_ladder=False,
        computation_profile=profile,
        computation_mode="assist",
        computation_image_reference=image_reference,
        computation_container_digest=digest,
        runtime_manifest_sha256="1" * 64,
    )

    assert len({absent, first_malformed, second_malformed}) == 3


class FakeContent:
    def __init__(self, value: NormalizedPage) -> None:
        self.value = value

    async def fetch_page(self, _locator: str) -> NormalizedPage:
        return self.value


class FakeLLM:
    provider_name = "fake-computation-provider"

    def __init__(self, responses: Sequence[BaseModel]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, type[BaseModel], str]] = []

    async def complete(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[ModelT]:
        self.calls.append((prompt, schema, prompt_version))
        if not self.responses:
            raise AssertionError("fake LLM response queue exhausted")
        value = self.responses.pop(0)
        assert isinstance(value, schema)
        raw = json.dumps(value.model_dump(mode="json"), sort_keys=True)
        attempt = LLMAttemptMetadata(
            attempt=1,
            raw_response=raw,
            response_metadata={"eval_count": 1},
        )
        return LLMResult(
            value=value,
            metadata=LLMCallMetadata(
                model="deterministic-fake",
                prompt_version=prompt_version,
                attempt=1,
                raw_response=raw,
                response_metadata={"eval_count": 1},
                attempts=(attempt,),
            ),
        )


class _FailingValidationClient(InProcessAssessmentComputationClient):
    def __init__(
        self,
        error: ComputationClientError,
        *,
        fail_on_call: int = 1,
    ) -> None:
        self.error = error
        self.fail_on_call = fail_on_call
        self.validation_calls = 0

    async def validate(
        self,
        request: ComputationValidationRequest,
    ) -> AssessmentValidationReport:
        self.validation_calls += 1
        if self.validation_calls == self.fail_on_call:
            raise self.error
        return await super().validate(request)


class _CountingReadyClient(InProcessAssessmentComputationClient):
    def __init__(self) -> None:
        self.ready_calls = 0

    async def ready(self):
        self.ready_calls += 1
        return await super().ready()


class _ManifestReadyClient(_CountingReadyClient):
    def __init__(self, manifest_sha256: str) -> None:
        super().__init__()
        self.manifest_sha256 = manifest_sha256

    async def ready(self):
        status = await super().ready()
        return status.model_copy(
            update={"runtime_manifest_sha256": self.manifest_sha256}
        )


class _FailingReadyClient(InProcessAssessmentComputationClient):
    def __init__(self) -> None:
        self.ready_calls = 0

    async def ready(self):
        self.ready_calls += 1
        raise ComputationUnavailableError("private readiness detail")


def _page() -> NormalizedPage:
    text = "Two objects plus three objects make five objects."
    return NormalizedPage(
        title="Addition",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            canonical_url=(
                "https://dev.libretexts.org/Sandboxes/johnnyphung/Demo/Addition"
            ),
            path="/Sandboxes/johnnyphung/Demo/Addition/",
            page_id="computation-pipeline-1",
        ),
    )


def _concepts() -> ConceptBatch:
    return ConceptBatch(
        concepts=[
            Concept(
                label="Adding quantities",
                description="Combine two and three to obtain the total.",
                source_paragraphs=[0],
            )
        ]
    )


def _provider_question(stem: str) -> QuestionDraft:
    return QuestionDraft(
        concept_label="Adding quantities",
        stem=stem,
        choices=[
            Choice(id="A", text="Provider says 999", correct=True),
            Choice(id="B", text="Provider says 998", correct=False),
            Choice(id="C", text="Provider says 997", correct=False),
            Choice(id="D", text="Provider says 996", correct=False),
        ],
        explanation="The provider's answer-bearing explanation is deliberately wrong.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _provider_computation_question(stem: str) -> ComputationQuestionDraft:
    provider = _provider_question(stem)
    choices = [
        choice.model_copy(
            update={
                "feedback": (
                    "This response uses a source-grounded relationship."
                    if choice.id == "B"
                    else "This response reflects a plausible misconception."
                )
            }
        )
        for choice in provider.choices
    ]
    return ComputationQuestionDraft.model_validate(
        provider.model_copy(
            update={
                "stem": f"{stem} {COMPUTATION_TASK_SLOT}",
                "choices": choices,
                "explanation": (
                    f"Use the source-grounded framing. {COMPUTATION_RESULT_SLOT}"
                ),
                "targeted_misconception": (
                    "Learners may choose an operation from surface wording."
                ),
            },
            deep=True,
        ).model_dump(mode="json")
    )


def _provider_external_question(stem: str, *, engine: str = "webwork") -> QuestionDraft:
    return QuestionDraft(
        item_type=AssessmentItemType(engine),
        concept_label="Adding quantities",
        stem=stem,
        response=ItemResponse(
            parameterized=ParameterizedItemSpec(
                engine=engine,
                variables=[
                    ParameterVariable(
                        name="a",
                        minimum=1,
                        maximum=3,
                        step=1,
                    )
                ],
                prompt_template="Provider uses {a}.",
                answer_expression="a + 999",
                explanation_template="Provider adds 999 to {a}.",
            )
        ),
        explanation="The provider's formula claim is unqualified.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _provider_webwork_question(stem: str) -> QuestionDraft:
    return _provider_external_question(stem, engine="webwork")


def _provider_external_computation_question(
    stem: str,
    *,
    engine: str,
) -> ComputationQuestionDraft:
    provider = _provider_external_question(stem, engine=engine)
    return ComputationQuestionDraft.model_validate(
        provider.model_copy(
            update={
                "stem": f"{stem} {COMPUTATION_TASK_SLOT}",
                "explanation": (
                    f"Use the source-grounded relationship. {COMPUTATION_RESULT_SLOT}"
                ),
                "targeted_misconception": (
                    "Learners may preserve a structure without applying the "
                    "requested operation."
                ),
            },
            deep=True,
        ).model_dump(mode="json")
    )


def _critique() -> Critique:
    return Critique(
        issues=["Provider arithmetic may be wrong."],
        revision_instructions=["Use the frozen computation."],
        revision_required=True,
    )


def _legacy_responses() -> list[BaseModel]:
    return [
        _concepts(),
        _provider_question("What total does the source state?"),
        _critique(),
        _provider_question("What is the combined total?"),
    ]


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


def _blueprint() -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(
            family="numeric",
            delivery="multiple_choice",
        ),
        operation="evaluate",
        expression=ExpressionNode(kind="add", args=[_integer(2), _integer(3)]),
        choice_expressions=[
            _integer(4),
            _integer(5),
            _integer(6),
            _integer(7),
        ],
    )


def _unqualified_formula_blueprint(
    *,
    delivery: str = "webwork",
    operation: str = "expand",
) -> AssessmentComputationBlueprint:
    if operation == "substitute":
        return AssessmentComputationBlueprint(
            profile=ComputationProfile(
                family="algebraic",
                delivery=delivery,
            ),
            operation="substitute",
            expression=ExpressionNode(
                kind="add",
                args=[
                    ExpressionNode(
                        kind="mul",
                        args=[
                            ExpressionNode(kind="symbol", symbol="a"),
                            ExpressionNode(kind="symbol", symbol="x"),
                        ],
                    ),
                    _integer(1),
                ],
            ),
            variables=[{"name": "a"}, {"name": "x"}],
            substitutions={"a": _integer(2)},
        )
    return AssessmentComputationBlueprint(
        profile=ComputationProfile(
            family="algebraic",
            delivery=delivery,
        ),
        operation="expand",
        expression=ExpressionNode(
            kind="mul",
            args=[
                ExpressionNode(
                    kind="add",
                    args=[
                        ExpressionNode(kind="symbol", symbol="x"),
                        _integer(1),
                    ],
                ),
                ExpressionNode(
                    kind="add",
                    args=[
                        ExpressionNode(kind="symbol", symbol="x"),
                        _integer(2),
                    ],
                ),
            ],
        ),
        variables=[
            {
                "name": "a",
                "domain": "integer",
                "minimum": _integer(1),
                "maximum": _integer(3),
                "step": _integer(1),
            },
            {"name": "x"},
        ],
    )


def _qualified_formula_promotion(
    engine: str,
) -> QualifiedFormulaAdapterPromotion:
    return QualifiedFormulaAdapterPromotion(
        engine=engine,
        production_compiler_versions=frozenset({TYPED_COMPUTATION_COMPILER_VERSION}),
        families=frozenset({"algebraic"}),
        operations=frozenset({"substitute", "expand", "factor", "equivalent"}),
        qualification_compiler_version=TYPED_COMPUTATION_COMPILER_VERSION,
        qualification_manifest_sha256="1" * 64,
        qualification_report_sha256="2" * 64,
        engine_image_digest=f"sha256:{'4' * 64}",
        adapter_image_digest=(f"sha256:{'5' * 64}" if engine == "imathas" else None),
        native_grader=(
            "native_symbolic_equivalence_v0"
            if engine == "imathas"
            else "MathObjects::Formula::cmp"
        ),
        promotion_approval_sha256="3" * 64,
    )


def test_off_mode_provider_contract_and_default_request_match_build08() -> None:
    schema_payload = json.dumps(
        QuestionDraft.model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    request_payload = json.dumps(
        GenerateRequest(
            source_type="sandbox",
            source_locator="x",
        ).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert hashlib.sha256(schema_payload).hexdigest() == (
        "91001dbdb19d0cca1f1d72605c795e9f8f9240b95745dc9688e23ccf5160ab95"
    )
    assert hashlib.sha256(request_payload).hexdigest() == (
        "e799ae62e75bec10595fb0bbcb71dca3398fd49739db70f75c72da21bde548d0"
    )


@pytest.fixture
def store(tmp_path: Path):
    database = init_database(f"sqlite:///{tmp_path / 'pipeline.db'}")
    yield database, DraftRepository(database)
    database.dispose()


@pytest.mark.asyncio
async def test_off_mode_preserves_exact_legacy_sequence_output_and_no_report(
    store,
) -> None:
    _database, repository = store
    revised = _provider_question("What is the combined total?")
    llm = FakeLLM(
        [
            _concepts(),
            _provider_question("What total does the source state?"),
            _critique(),
            revised,
        ]
    )
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="off",
        computation_families=("numeric",),
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_count=1,
        include_hint_ladder=False,
    )

    assert [schema for _, schema, _ in llm.calls] == [
        ConceptBatch,
        QuestionDraft,
        Critique,
        QuestionDraft,
    ]
    assert len(llm.calls) == 4
    assert outcome.pipeline_version == PIPELINE_VERSION
    stored = repository.require_draft(outcome.draft_id)
    assert stored.current == revised
    assert stored.current_json == revised.model_dump(mode="json")
    assert repository.get_current_computation_validation(outcome.draft_id) is None
    source = repository.get_source(outcome.source_id)
    assert source is not None
    assert [
        call.stage for call in sorted(source.llm_calls, key=lambda call: call.id)
    ] == [
        "concept_extraction",
        "initial_draft",
        "critique",
        "revision",
    ]


@pytest.mark.asyncio
async def test_assist_profile_is_blueprint_first_and_server_binds_ground_truth(
    store,
) -> None:
    _database, repository = store
    blueprint = _blueprint()
    llm = FakeLLM(
        [
            _concepts(),
            blueprint,
            _provider_computation_question("Provider initial stem."),
            _critique(),
            _provider_computation_question("Provider revised stem."),
        ]
    )
    computation_client = _CountingReadyClient()
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=computation_client,
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=f"sha256:{'a' * 64}",
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        item_count=1,
        include_hint_ladder=False,
        computation_profile=blueprint.profile,
    )

    assert [schema for _, schema, _ in llm.calls] == [
        ConceptBatch,
        AssessmentComputationBlueprint,
        ComputationQuestionDraft,
        Critique,
        ComputationQuestionDraft,
    ]
    source = repository.get_source(outcome.source_id)
    assert source is not None
    assert [
        call.stage for call in sorted(source.llm_calls, key=lambda call: call.id)
    ] == [
        "concept_extraction",
        "computation_blueprint",
        "initial_draft",
        "critique",
        "revision",
    ]
    stored = repository.require_draft(outcome.draft_id)
    assert [choice.text for choice in stored.current.choices] == ["4", "5", "6", "7"]
    assert [choice.correct for choice in stored.current.choices] == [
        False,
        True,
        False,
        False,
    ]
    assert "999" not in stored.current_json["choices"][0]["text"]
    assert stored.current.stem.startswith("Provider revised stem.")
    assert 'For the source concept "Adding quantities":' in stored.current.stem
    assert stored.current.explanation.startswith("Use the source-grounded framing.")
    assert stored.current.explanation.endswith("Computed result: 5.")
    assert stored.current.targeted_misconception == (
        "Learners may choose an operation from surface wording."
    )
    assert stored.current.choices[1].feedback is not None
    assert "source-grounded relationship" in stored.current.choices[1].feedback
    assert "matches the deterministic computed result" in (
        stored.current.choices[1].feedback
    )
    assert "frozen_computation" in llm.calls[2][0]
    assert "frozen_computation" in llm.calls[3][0]
    assert "frozen_computation" in llm.calls[4][0]

    report = repository.get_current_computation_validation(outcome.draft_id)
    assert report is not None
    assert report.status == "partially_validated"
    assert report.is_current is True
    assert report.edit_count == 0
    persisted = json.loads(report.report_json)
    persisted_blueprint = json.loads(report.blueprint_json)
    assert persisted_blueprint["source_concept_label"] == "Adding quantities"
    assert persisted["result"]["correct_choice_index"] == 1
    assert persisted["status"] == "partially_validated"

    cached = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        item_count=1,
        include_hint_ladder=False,
        computation_profile=blueprint.profile,
    )
    assert cached.draft_id == outcome.draft_id
    assert computation_client.ready_calls == 2
    assert len(llm.calls) == 5


@pytest.mark.asyncio
async def test_readiness_failure_misses_old_cache_persists_failure_and_recovery_misses(
    store,
) -> None:
    _database, repository = store
    blueprint = _blueprint()
    digest = f"sha256:{'a' * 64}"
    image_reference = f"registry.example/computation@{digest}"

    first_client = _ManifestReadyClient("1" * 64)
    first = AssessmentPipeline(
        FakeContent(_page()),
        FakeLLM(
            [
                _concepts(),
                blueprint,
                _provider_computation_question("First initial stem."),
                _critique(),
                _provider_computation_question("First revised stem."),
            ]
        ),
        repository,
        computation_client=first_client,
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=digest,
        computation_image_reference=image_reference,
    )
    first_outcome = await first.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        computation_profile=blueprint.profile,
    )
    assert first_client.ready_calls == 1

    failed_client = _FailingReadyClient()
    failed = AssessmentPipeline(
        FakeContent(_page()),
        FakeLLM(
            [
                _concepts(),
                blueprint,
                _provider_question("Failed readiness initial stem."),
                _critique(),
                _provider_question("Failed readiness revised stem."),
            ]
        ),
        repository,
        computation_client=failed_client,
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=digest,
        computation_image_reference=image_reference,
    )
    failed_outcome = await failed.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        computation_profile=blueprint.profile,
    )
    failed_record = repository.get_current_computation_validation(
        failed_outcome.draft_id
    )
    assert failed_client.ready_calls == 2
    assert failed_record is not None
    assert failed_record.status == ValidationStatus.VALIDATION_FAILED.value
    assert json.loads(failed_record.report_json)["checks"][0]["code"] == (
        "computation_unavailable"
    )

    recovery_client = _ManifestReadyClient("2" * 64)
    recovered = AssessmentPipeline(
        FakeContent(_page()),
        FakeLLM(
            [
                _concepts(),
                blueprint,
                _provider_computation_question("Recovered initial stem."),
                _critique(),
                _provider_computation_question("Recovered revised stem."),
            ]
        ),
        repository,
        computation_client=recovery_client,
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=digest,
        computation_image_reference=image_reference,
    )
    recovered_outcome = await recovered.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        computation_profile=blueprint.profile,
    )

    assert recovery_client.ready_calls == 1
    assert (
        len(
            {
                first_outcome.pipeline_version,
                failed_outcome.pipeline_version,
                recovered_outcome.pipeline_version,
            }
        )
        == 3
    )
    assert (
        len(
            {
                first_outcome.draft_id,
                failed_outcome.draft_id,
                recovered_outcome.draft_id,
            }
        )
        == 3
    )


@pytest.mark.asyncio
async def test_declared_malformed_runtime_misses_absent_runtime_cache(
    store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, repository = store
    blueprint = _blueprint()
    digest = f"sha256:{'d' * 64}"
    image_reference = f"registry.example/computation@{digest}"
    monkeypatch.setattr(computation_policy, "QUALIFIED_COMPUTATION_RUNTIMES", {})
    first = AssessmentPipeline(
        FakeContent(_page()),
        FakeLLM(
            [
                _concepts(),
                blueprint,
                _provider_computation_question("Absent runtime initial."),
                _critique(),
                _provider_computation_question("Absent runtime revised."),
            ]
        ),
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=digest,
        computation_image_reference=image_reference,
    )
    first_outcome = await first.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        computation_profile=blueprint.profile,
    )
    first_record = repository.get_current_computation_validation(first_outcome.draft_id)
    assert first_record is not None
    assert first_record.status == ValidationStatus.PARTIALLY_VALIDATED.value

    monkeypatch.setattr(
        computation_policy,
        "QUALIFIED_COMPUTATION_RUNTIMES",
        {
            digest: QualifiedComputationRuntime(
                image_reference=image_reference,
                container_digest=digest,
                runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
                qualification_report_sha256="e" * 64,
                families={"numeric"},  # type: ignore[arg-type]
            )
        },
    )
    malformed = AssessmentPipeline(
        FakeContent(_page()),
        FakeLLM(
            [
                _concepts(),
                blueprint,
                _provider_question("Malformed runtime initial."),
                _critique(),
                _provider_question("Malformed runtime revised."),
            ]
        ),
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=digest,
        computation_image_reference=image_reference,
    )
    malformed_outcome = await malformed.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        computation_profile=blueprint.profile,
    )
    malformed_record = repository.get_current_computation_validation(
        malformed_outcome.draft_id
    )
    assert malformed_record is not None
    assert malformed_record.status == ValidationStatus.VALIDATION_FAILED.value
    assert malformed_outcome.draft_id != first_outcome.draft_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        (ComputationTimeoutError, "computation_timeout"),
        (ComputationUnavailableError, "computation_unavailable"),
        (ComputationProtocolError, "computation_invalid_response"),
        (ComputationRequestTooLargeError, "computation_request_too_large"),
        (ComputationResponseTooLargeError, "computation_response_too_large"),
        (ComputationServiceError, "computation_service_error"),
    ],
)
async def test_generation_sidecar_preflight_failure_persists_failed_report(
    store,
    error_type: type[ComputationClientError],
    expected_code: str,
) -> None:
    _database, repository = store
    blueprint = _blueprint()
    llm = FakeLLM(
        [
            _concepts(),
            blueprint,
            _provider_question("Provider initial unresolved stem."),
            _critique(),
            _provider_question("Provider revised unresolved stem."),
        ]
    )
    client = _FailingValidationClient(
        error_type("do not persist /private/runtime/details")
    )
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=client,
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=f"sha256:{'a' * 64}",
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        item_count=1,
        include_hint_ladder=False,
        computation_profile=blueprint.profile,
    )

    stored = repository.require_draft(outcome.draft_id)
    record = repository.get_current_computation_validation(outcome.draft_id)
    assert record is not None
    assert record.status == "validation_failed"
    assert record.edit_count == 0
    assert record.is_current is True
    assert stored.current == _provider_question("Provider revised unresolved stem.")
    assert stored.current_engine_validation is None
    report = json.loads(record.report_json)
    assert report.get("result") is None
    assert len(report["seed_plan"]) == 25
    assert report["checks"] == [
        {
            "code": expected_code,
            "details": {"phase": "generation_preflight"},
            "message": report["checks"][0]["message"],
            "status": "failed",
        }
    ]
    assert "validation failed closed" in report["checks"][0]["message"]
    assert "/private/runtime/details" not in record.report_json
    assert all(
        "unresolved_computation" in prompt
        for prompt, schema, _version in llm.calls
        if schema in {QuestionDraft, Critique}
    )


@pytest.mark.asyncio
async def test_generation_final_sidecar_failure_keeps_server_bound_content(
    store,
) -> None:
    _database, repository = store
    blueprint = _blueprint()
    llm = FakeLLM(
        [
            _concepts(),
            blueprint,
            _provider_computation_question("Provider initial stem."),
            _critique(),
            _provider_computation_question("Provider revised stem."),
        ]
    )
    client = _FailingValidationClient(
        ComputationServiceError("do not persist execution internals"),
        fail_on_call=2,
    )
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=client,
        computation_mode="assist",
        computation_families=("numeric",),
        computation_container_digest=f"sha256:{'b' * 64}",
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.MULTIPLE_CHOICE],
        item_count=1,
        include_hint_ladder=False,
        computation_profile=blueprint.profile,
    )

    stored = repository.require_draft(outcome.draft_id)
    record = repository.get_current_computation_validation(outcome.draft_id)
    assert record is not None
    assert record.status == "validation_failed"
    assert [choice.text for choice in stored.current.choices] == ["4", "5", "6", "7"]
    assert [choice.correct for choice in stored.current.choices] == [
        False,
        True,
        False,
        False,
    ]
    assert stored.current_engine_validation is None
    report = json.loads(record.report_json)
    assert report["checks"][0]["code"] == "computation_service_error"
    assert report["checks"][0]["details"] == {"phase": "final_validation"}
    assert "execution internals" not in record.report_json


@pytest.mark.asyncio
async def test_assist_without_profile_adds_not_applicable_without_provider_call(
    store,
) -> None:
    _database, repository = store
    llm = FakeLLM(_legacy_responses())
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="assist",
        computation_families=("numeric",),
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_count=1,
        include_hint_ladder=False,
    )

    assert [schema for _, schema, _ in llm.calls] == [
        ConceptBatch,
        QuestionDraft,
        Critique,
        QuestionDraft,
    ]
    report = repository.get_current_computation_validation(outcome.draft_id)
    assert report is not None
    assert report.status == "not_applicable"
    assert json.loads(report.report_json)["status"] == "not_applicable"


@pytest.mark.asyncio
async def test_unqualified_formula_persists_unsupported_without_engine_pass(
    store,
) -> None:
    _database, repository = store
    blueprint = _unqualified_formula_blueprint()
    initial = _provider_webwork_question("Provider initial formula.")
    revised = _provider_webwork_question("Provider revised formula.")
    llm = FakeLLM([_concepts(), blueprint, initial, _critique(), revised])
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="assist",
        computation_families=("algebraic",),
        computation_container_digest=f"sha256:{'a' * 64}",
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType.WEBWORK],
        item_count=1,
        include_hint_ladder=False,
        computation_profile=blueprint.profile,
    )

    stored = repository.require_draft(outcome.draft_id)
    report = repository.get_current_computation_validation(outcome.draft_id)
    assert report is not None
    assert report.status == "unsupported"
    assert stored.current == revised
    assert stored.current_engine_validation is None
    payload = json.loads(report.report_json)
    assert payload.get("result") is None
    assert any(
        check["code"] == "delivery_adapter" and check["status"] == "inconclusive"
        for check in payload["checks"]
    )
    assert all(
        "unresolved_computation" in prompt
        for prompt, schema, _version in llm.calls
        if schema in {QuestionDraft, Critique}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["webwork", "imathas"])
@pytest.mark.parametrize("operation", ["expand", "substitute"])
async def test_promoted_typed_formula_pipeline_never_reenters_legacy_compiler(
    store,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    operation: str,
) -> None:
    _database, repository = store
    promotion = _qualified_formula_promotion(engine)
    monkeypatch.setattr(
        parameterized_module,
        "QUALIFIED_FORMULA_ADAPTERS",
        {(engine, TYPED_COMPUTATION_COMPILER_VERSION): promotion},
    )
    blueprint = _unqualified_formula_blueprint(
        delivery=engine,
        operation=operation,
    )
    initial = _provider_external_computation_question(
        "Use the source relationship.",
        engine=engine,
    )
    revised = _provider_external_computation_question(
        "Apply the requested relationship.",
        engine=engine,
    )
    llm = FakeLLM([_concepts(), blueprint, initial, _critique(), revised])
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="assist",
        computation_families=("algebraic",),
        computation_container_digest=f"sha256:{'a' * 64}",
    )

    outcome = await pipeline.generate(
        "/Sandboxes/johnnyphung/Demo/Addition",
        item_types=[AssessmentItemType(engine)],
        item_count=1,
        include_hint_ladder=False,
        computation_profile=blueprint.profile,
    )

    stored = repository.require_draft(outcome.draft_id)
    parameterized = stored.current.response.parameterized
    report = repository.get_current_computation_validation(outcome.draft_id)
    assert parameterized is not None
    assert parameterized.compiler_profile == "assessment_computation_v0"
    assert parameterized.answer_kind == "formula"
    assert stored.current_engine_validation is not None
    assert (
        stored.current_engine_validation.compiler_version
        == TYPED_COMPUTATION_COMPILER_VERSION
    )
    assert report is not None
    assert report.status == "partially_validated"
    evidence = json.loads(report.engine_evidence_json)
    assert evidence["formula_adapter_promotion"]["operation"] == operation
    assert "native_receipt" not in evidence


@pytest.mark.asyncio
async def test_profile_delivery_mismatch_is_rejected_before_provider_call(
    store,
) -> None:
    _database, repository = store
    llm = FakeLLM([])
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode="assist",
        computation_families=("numeric",),
    )

    with pytest.raises(ComputationWorkflowError, match="does not match"):
        await pipeline.generate(
            "/Sandboxes/johnnyphung/Demo/Addition",
            item_types=[AssessmentItemType.NUMERICAL],
            item_count=1,
            include_hint_ladder=False,
            computation_profile=ComputationProfile(
                family="numeric",
                delivery="multiple_choice",
            ),
        )

    assert llm.calls == []
    assert repository.list_drafts(current_sources_only=False) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "families", "message"),
    [
        ("off", ("numeric",), "disabled"),
        ("assist", (), "not allowlisted"),
    ],
)
async def test_profile_is_rejected_when_off_or_family_allowlist_is_empty(
    store,
    mode: str,
    families: tuple[str, ...],
    message: str,
) -> None:
    _database, repository = store
    llm = FakeLLM([])
    pipeline = AssessmentPipeline(
        FakeContent(_page()),
        llm,
        repository,
        computation_client=InProcessAssessmentComputationClient(),
        computation_mode=mode,
        computation_families=families,
    )

    with pytest.raises(ComputationWorkflowError, match=message):
        await pipeline.generate(
            "/Sandboxes/johnnyphung/Demo/Addition",
            item_count=1,
            include_hint_ladder=False,
            computation_profile=ComputationProfile(
                family="numeric",
                delivery="multiple_choice",
            ),
        )

    assert llm.calls == []
    assert repository.list_drafts(current_sources_only=False) == []
