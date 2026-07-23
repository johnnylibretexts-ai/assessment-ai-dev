from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.computation import (
    AssessmentComputationBlueprint,
    AssessmentValidationReport,
    ComputationValidationRequest,
    ExpressionNode,
    VariableSpec,
)
from app.computation_client import InProcessAssessmentComputationClient
from app.computation_workflow import legacy_unsupported_validation_write
from app.config import Settings
from app.db import DraftRepository, DraftWrite
from app.main import create_app
from app.parameterized import TYPED_COMPUTATION_COMPILER_VERSION
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Concept,
    Critique,
    Difficulty,
    ItemResponse,
    NormalizedPage,
    ParameterVariable,
    ParameterizedItemSpec,
    Paragraph,
    QuestionDraft,
    ReviewStatus,
    SourceInfo,
)


def _settings(
    tmp_path: Path,
    *,
    mode: str = "assist",
    families: str = "numeric",
) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'revalidation.db'}",
        allowed_origin="http://testserver",
        server_key="key",
        server_secret="secret",
        server_user="user",
        computation_mode=mode,
        computation_family_allowlist=families,
        computation_image_reference=(
            f"registry.example/assessment-computation@sha256:{'a' * 64}"
        ),
        parameterized_items_enabled=True,
        webwork_enabled=True,
    )


def _integer(value: int) -> ExpressionNode:
    return ExpressionNode(kind="integer", integer=value)


def _numeric_blueprint() -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile={"family": "numeric", "delivery": "numerical"},
        operation="evaluate",
        expression=ExpressionNode(
            kind="add",
            args=[_integer(2), _integer(3)],
        ),
    )


def _unit_blueprint() -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile={"family": "unit", "delivery": "numerical"},
        operation="convert_unit",
        expression=_integer(1),
        source_unit="m",
        target_unit="cm",
    )


def _webwork_blueprint() -> AssessmentComputationBlueprint:
    return AssessmentComputationBlueprint(
        profile={"family": "numeric", "delivery": "webwork"},
        operation="evaluate",
        expression=ExpressionNode(
            kind="add",
            args=[ExpressionNode(kind="symbol", symbol="a"), _integer(1)],
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


def _page() -> NormalizedPage:
    text = "A numerical quantity can be evaluated from stated values."
    return NormalizedPage(
        title="Typed computation migration",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=(
                "https://chem.libretexts.org/Bookshelves/Test/"
                "Typed_Computation_Migration"
            ),
            path="chem.libretexts.org/Bookshelves/Test/Typed_Computation_Migration",
            page_id="typed-computation-migration",
        ),
    )


def _legacy_question() -> QuestionDraft:
    return QuestionDraft(
        item_type=AssessmentItemType.NUMERICAL,
        concept_label="Computed quantity",
        stem="What is the legacy numerical answer?",
        response=ItemResponse(numeric_answer=999, numeric_tolerance=0),
        explanation="This legacy explanation has no typed computation evidence.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _legacy_webwork_question() -> QuestionDraft:
    return QuestionDraft(
        item_type=AssessmentItemType.WEBWORK,
        concept_label="Computed quantity",
        stem="What is the legacy parameterized answer?",
        response=ItemResponse(
            parameterized=ParameterizedItemSpec(
                engine="webwork",
                variables=[
                    ParameterVariable(
                        name="a",
                        minimum=1,
                        maximum=3,
                        step=1,
                    )
                ],
                prompt_template="Evaluate {a}.",
                answer_expression="a + 999",
                explanation_template="Add 999 to {a}.",
            )
        ),
        explanation="This legacy explanation has no typed computation evidence.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _seed_legacy(
    repository: DraftRepository,
    *,
    persisted_legacy_evidence: bool = True,
) -> int:
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="legacy-computation-revalidation-test",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Computed quantity",
                    description="Evaluate a stated numerical quantity.",
                    source_paragraphs=[0],
                ),
                raw=_legacy_question(),
                critique=Critique(issues=[], revision_required=False),
                revised=_legacy_question(),
                computation_validation=(
                    legacy_unsupported_validation_write()
                    if persisted_legacy_evidence
                    else None
                ),
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


def _post_headers() -> dict[str, str]:
    return {
        "Origin": "http://testserver",
        "X-Reviewer": "faculty@example.edu",
    }


class _ConcurrentEditClient(InProcessAssessmentComputationClient):
    def __init__(self, repository: DraftRepository, draft_id: int) -> None:
        self._repository = repository
        self._draft_id = draft_id
        self._edited = False

    async def validate(
        self,
        request: ComputationValidationRequest,
    ) -> AssessmentValidationReport:
        if not self._edited:
            self._edited = True
            current = self._repository.require_draft(self._draft_id).current
            self._repository.edit_draft(
                self._draft_id,
                current.model_copy(
                    update={
                        "stem": "Concurrent reviewer edit remains current.",
                        "response": ItemResponse(
                            numeric_answer=321,
                            numeric_tolerance=0,
                        ),
                    },
                    deep=True,
                ),
                editor="other-faculty@example.edu",
            )
        return await super().validate(request)


def test_legacy_revalidation_rebinds_and_appends_evidence_atomically(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.computation_client = InProcessAssessmentComputationClient()
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository)
        legacy = repository.get_current_computation_validation(draft_id)
        assert legacy is not None
        repository.confirm_bloom(draft_id, reviewer="faculty@example.edu")
        repository.confirm_difficulty(draft_id, reviewer="faculty@example.edu")
        repository.transition_status(
            draft_id,
            ReviewStatus.READY_TO_PUBLISH,
            reviewer="faculty@example.edu",
        )

        response = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _numeric_blueprint().model_dump_json(
                    exclude_none=True
                ),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert response.status_code == 303
        rebound = repository.require_draft(draft_id)
        current = repository.get_current_computation_validation(draft_id)
        historical = repository.get_computation_validation(
            draft_id,
            edit_count=0,
            report_sha256=legacy.report_sha256,
        )
        assert rebound.edit_count == 1
        assert rebound.status == ReviewStatus.READY_FOR_REVIEW
        assert rebound.bloom_confirmed is False
        assert rebound.difficulty_confirmed is False
        assert rebound.current.response.numeric_answer == 5
        assert rebound.current.stem.startswith("What is the legacy numerical answer?")
        assert (
            'For the source concept "Computed quantity": Evaluate (2 + 3).'
            in rebound.current.stem
        )
        assert current is not None
        assert current.status == "partially_validated"
        assert current.edit_count == 1
        assert current.is_current is True
        assert json.loads(current.blueprint_json)["schema_version"] == (
            "assessment-computation-v0"
        )
        assert historical is not None and historical.is_current is False
        assert rebound.publications == []


def test_legacy_external_revalidation_persists_exact_typed_compiler_receipt(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.computation_client = InProcessAssessmentComputationClient()
        repository: DraftRepository = app.state.repository
        question = _legacy_webwork_question()
        stored = repository.replace_generated_drafts(
            page=_page(),
            pipeline_version="legacy-external-revalidation-test",
            drafts=[
                DraftWrite(
                    position=0,
                    concept=Concept(
                        label="Computed quantity",
                        description="Evaluate a parameterized numerical quantity.",
                        source_paragraphs=[0],
                    ),
                    raw=question,
                    critique=Critique(issues=[], revision_required=False),
                    revised=question,
                    computation_validation=legacy_unsupported_validation_write(),
                )
            ],
            llm_calls=[],
        )
        draft_id = stored.draft_ids[0]

        response = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _webwork_blueprint().model_dump_json(
                    exclude_none=True
                ),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" not in response.headers["location"], response.headers[
            "location"
        ]
        draft = repository.require_draft(draft_id)
        computation = repository.get_current_computation_validation(draft_id)
        engine = draft.current_engine_validation
        assert computation is not None
        assert computation.status == "partially_validated"
        assert engine is not None
        evidence = json.loads(computation.engine_evidence_json)
        assert engine.compiler_version == TYPED_COMPUTATION_COMPILER_VERSION
        assert engine.compiler_version == evidence["compiler_version"]
        assert engine.source_sha256 == evidence["source_sha256"]
        assert engine.seed_count == 25


def test_legacy_revalidation_migrates_a_pre_v0_draft_without_a_prior_record(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.computation_client = InProcessAssessmentComputationClient()
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository, persisted_legacy_evidence=False)
        assert repository.get_current_computation_validation(draft_id) is None

        response = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _numeric_blueprint().model_dump_json(
                    exclude_none=True
                ),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert response.status_code == 303
        current = repository.get_current_computation_validation(draft_id)
        assert current is not None and current.edit_count == 1
        assert current.status == "partially_validated"
        assert len(repository.list_computation_validations(draft_id)) == 1


def test_legacy_revalidation_does_not_overwrite_a_concurrent_edit(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository)
        app.state.computation_client = _ConcurrentEditClient(repository, draft_id)

        response = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _numeric_blueprint().model_dump_json(
                    exclude_none=True
                ),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        stored = repository.require_draft(draft_id)
        assert stored.edit_count == 1
        assert stored.current.stem == "Concurrent reviewer edit remains current."
        assert stored.current.response.numeric_answer == 321
        assert repository.get_current_computation_validation(draft_id) is None
        assert len(repository.list_computation_validations(draft_id)) == 1


def test_legacy_revalidation_rejects_untyped_fields_and_preserves_revision(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.computation_client = InProcessAssessmentComputationClient()
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository)
        payload = _numeric_blueprint().model_dump(mode="json", exclude_none=True)
        payload["expression"]["source"] = "__import__('os').system('id')"

        response = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": json.dumps(payload),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        stored = repository.require_draft(draft_id)
        current = repository.get_current_computation_validation(draft_id)
        assert stored.edit_count == 0
        assert stored.current.response.numeric_answer == 999
        assert current is not None
        assert json.loads(current.report_json)["reason"] == "legacy_without_blueprint"


def test_legacy_revalidation_requires_allowlisted_family_and_current_revision(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.computation_client = InProcessAssessmentComputationClient()
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository)

        wrong_family = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _unit_blueprint().model_dump_json(exclude_none=True),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )
        stale_revision = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _numeric_blueprint().model_dump_json(
                    exclude_none=True
                ),
                "expected_edit_count": "7",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert wrong_family.status_code == 303
        assert stale_revision.status_code == 303
        assert repository.require_draft(draft_id).edit_count == 0


def test_legacy_revalidation_cannot_replace_an_existing_typed_blueprint(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.computation_client = InProcessAssessmentComputationClient()
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository)
        request_data = {
            "blueprint_json": _numeric_blueprint().model_dump_json(exclude_none=True),
            "expected_edit_count": "0",
        }

        first = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data=request_data,
            headers=_post_headers(),
            follow_redirects=False,
        )
        second = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={**request_data, "expected_edit_count": "1"},
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert first.status_code == 303
        assert second.status_code == 303
        assert "error=" in second.headers["location"]
        assert repository.require_draft(draft_id).edit_count == 1
        assert len(repository.list_computation_validations(draft_id)) == 2


def test_legacy_revalidation_is_unavailable_when_computation_is_off(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path, mode="off", families=""))
    with TestClient(app) as client:
        repository: DraftRepository = app.state.repository
        draft_id = _seed_legacy(repository)

        response = client.post(
            f"/drafts/{draft_id}/computation/revalidate",
            data={
                "blueprint_json": _numeric_blueprint().model_dump_json(
                    exclude_none=True
                ),
                "expected_edit_count": "0",
            },
            headers=_post_headers(),
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert repository.require_draft(draft_id).edit_count == 0
