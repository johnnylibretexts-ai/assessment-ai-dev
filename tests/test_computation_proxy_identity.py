from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi import Request
from fastapi.testclient import TestClient

from app.computation import (
    SCHEMA_VERSION,
    UCUM_ESSENCE_SHA256,
    UCUM_PROFILE,
    VALIDATOR_VERSION,
    AssessmentComputationBlueprint,
    AssessmentValidationReport,
    CheckStatus,
    ComputationDelivery,
    ComputationFamily,
    ComputationOperation,
    ComputationProfile,
    ComputationResult,
    ExpressionKind,
    ExpressionNode,
    ValidationCheck,
    ValidationStatus,
    canonical_blueprint_hash,
)
from app.config import COMPUTATION_PROXY_TOKEN_HEADER, Settings
from app.db import ComputationValidationWrite, DraftRepository, DraftWrite
from app.main import _trusted_computation_specialist_subject, create_app
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    SourceInfo,
)


PROXY_TOKEN = "proxy_token_0123456789abcdef0123456789abcdef"
SPECIALIST = "specialist@example.edu"
GENERAL_REVIEWER = "reviewer@example.edu"


def test_deploy_proxy_rebuilds_specialist_headers_from_trusted_context() -> None:
    caddyfile = (
        Path(__file__).resolve().parents[1] / "deploy" / "Caddyfile.assess-ai"
    ).read_text(encoding="utf-8")
    subject_header = (
        "{$ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER:"
        "X-Assessment-AI-Authenticated-Subject}"
    )
    directives = [
        "header_up -X-Assessment-AI-Proxy-Token",
        f"header_up -{subject_header}",
        "header_up X-Reviewer {http.auth.user.id}",
        f"header_up {subject_header} {{http.auth.user.id}}",
        (
            "header_up X-Assessment-AI-Proxy-Token "
            '"{$ASSESSMENT_AI_COMPUTATION_TRUSTED_PROXY_TOKEN}"'
        ),
    ]

    positions = [caddyfile.index(directive) for directive in directives]
    assert positions == sorted(positions)
    assert PROXY_TOKEN not in caddyfile


def _settings(
    tmp_path: Path,
    *,
    proxy_token: str | None = PROXY_TOKEN,
) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'proxy-identity.db'}",
        allowed_origin="http://testserver",
        computation_mode="assist",
        computation_family_allowlist="numeric",
        computation_specialist_subject_allowlist=SPECIALIST,
        computation_trusted_proxy_token=proxy_token,
    )


def _page() -> NormalizedPage:
    text = "Momentum equals mass times velocity."
    return NormalizedPage(
        title="Momentum",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url="https://phys.libretexts.org/Bookshelves/Test/Momentum",
            path="phys.libretexts.org/Bookshelves/Test/Momentum",
            page_id="proxy-test",
        ),
    )


def _question() -> QuestionDraft:
    return QuestionDraft(
        concept_label="Momentum",
        stem="What is the momentum?",
        choices=[
            Choice(id="A", text="6 kg m/s", correct=True),
            Choice(id="B", text="3 kg m/s", correct=False),
            Choice(id="C", text="9 kg m/s", correct=False),
            Choice(id="D", text="12 kg m/s", correct=False),
        ],
        explanation="Multiply mass by velocity.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def _validation() -> ComputationValidationWrite:
    answer = ExpressionNode(kind=ExpressionKind.INTEGER, integer=6)
    distractor = ExpressionNode(kind=ExpressionKind.INTEGER, integer=3)
    distractor_two = ExpressionNode(kind=ExpressionKind.INTEGER, integer=9)
    distractor_three = ExpressionNode(kind=ExpressionKind.INTEGER, integer=12)
    blueprint = AssessmentComputationBlueprint(
        profile=ComputationProfile(
            family=ComputationFamily.NUMERIC,
            delivery=ComputationDelivery.MULTIPLE_CHOICE,
        ),
        operation=ComputationOperation.EVALUATE,
        expression=answer,
        choice_expressions=[
            answer,
            distractor,
            distractor_two,
            distractor_three,
        ],
    )
    blueprint_hash = canonical_blueprint_hash(blueprint)
    dependencies = {
        "sympy": "1.14.0",
        "pint": "0.25.3",
        "ucumvert": "0.3.2",
        "ucum_profile": UCUM_PROFILE,
        "ucum_essence_sha256": UCUM_ESSENCE_SHA256,
    }
    seeds = list(range(25))
    report = AssessmentValidationReport(
        status=ValidationStatus.PARTIALLY_VALIDATED,
        blueprint_hash=blueprint_hash,
        checks=[
            ValidationCheck(
                code="native_receipt",
                status=CheckStatus.INCONCLUSIVE,
                message="Canary receipt is not available.",
            )
        ],
        result=ComputationResult(
            blueprint_hash=blueprint_hash,
            operation=ComputationOperation.EVALUATE,
            exact_value="6",
            numeric_value="6",
            answer_expression=answer,
            correct_choice_index=0,
        ),
        seed_plan=seeds,
        limitations=["Native receipt remains pending."],
        dependencies=dependencies,
    )
    return ComputationValidationWrite(
        status=ValidationStatus.PARTIALLY_VALIDATED.value,
        schema_version=SCHEMA_VERSION,
        validator_revision=VALIDATOR_VERSION,
        blueprint_json=blueprint.model_dump_json(exclude_none=True),
        report_json=report.model_dump_json(exclude_none=True),
        dependency_versions_json=json.dumps(
            dependencies,
            sort_keys=True,
            separators=(",", ":"),
        ),
        container_digest=f"sha256:{'1' * 64}",
        seed_plan_json=json.dumps(seeds, separators=(",", ":")),
        duration_ms=3,
        engine_evidence_json='{"required":true}',
    )


def _seed(repository: DraftRepository) -> int:
    question = _question()
    stored = repository.replace_generated_drafts(
        page=_page(),
        pipeline_version="proxy-identity-test",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Momentum",
                    description="Momentum is mass times velocity.",
                    source_paragraphs=[0],
                ),
                raw=question,
                critique=Critique(issues=[], revision_required=False),
                revised=question,
                computation_validation=_validation(),
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


def _attestation_headers(
    settings: Settings,
    *,
    token: str | None,
    subject: str | None,
    reviewer: str = GENERAL_REVIEWER,
) -> dict[str, str]:
    headers = {
        "Origin": "http://testserver",
        "X-Reviewer": reviewer,
    }
    if token is not None:
        headers[COMPUTATION_PROXY_TOKEN_HEADER] = token
    if subject is not None:
        headers[settings.computation_specialist_subject_header] = subject
    return headers


def test_raw_reviewer_or_spoofed_subject_cannot_attest(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        draft_id = _seed(app.state.repository)
        attempts = [
            _attestation_headers(
                settings, token=None, subject=None, reviewer=SPECIALIST
            ),
            _attestation_headers(settings, token=None, subject=SPECIALIST),
            _attestation_headers(settings, token="x" * 40, subject=SPECIALIST),
            _attestation_headers(
                settings,
                token=PROXY_TOKEN,
                subject="not-allowlisted@example.edu",
            ),
        ]
        for headers in attempts:
            detail = client.get(f"/drafts/{draft_id}", headers=headers)
            assert "/computation/attest" not in detail.text

            response = client.post(
                f"/drafts/{draft_id}/computation/attest",
                data={
                    "rationale": (
                        "I independently checked the computation and its limitations."
                    )
                },
                headers=headers,
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert "authenticated" in response.headers["location"]

        assert (
            app.state.repository.list_current_computation_attestations(draft_id) == ()
        )


def test_valid_proxy_token_and_allowlisted_subject_create_attestation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    headers = _attestation_headers(
        settings,
        token=PROXY_TOKEN,
        subject=SPECIALIST,
    )
    with TestClient(app) as client:
        draft_id = _seed(app.state.repository)
        detail = client.get(f"/drafts/{draft_id}", headers=headers)
        assert detail.status_code == 200
        assert f'action="/drafts/{draft_id}/computation/attest"' in detail.text

        response = client.post(
            f"/drafts/{draft_id}/computation/attest",
            data={
                "rationale": (
                    "I independently checked the computation and its limitations."
                )
            },
            headers=headers,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "attestation+recorded" in response.headers["location"]

        attestations = app.state.repository.list_current_computation_attestations(
            draft_id
        )
        assert len(attestations) == 1
        assert attestations[0].specialist_identity == SPECIALIST
        assert attestations[0].specialist_identity != GENERAL_REVIEWER
        assert PROXY_TOKEN not in attestations[0].qualification_json


def test_general_reviewer_flow_remains_separate_from_proxy_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        draft_id = _seed(app.state.repository)
        response = client.post(
            f"/drafts/{draft_id}/review",
            data={
                "decision": "ready_to_publish",
                "bloom_confirmed": "true",
                "difficulty_confirmed": "true",
                "reviewer_notes": "Source and taxonomy checks completed.",
            },
            headers={
                "Origin": "http://testserver",
                "X-Reviewer": GENERAL_REVIEWER,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        stored = app.state.repository.require_draft(draft_id)
        assert stored.last_reviewed_by == GENERAL_REVIEWER
        assert stored.status.value == "ready_to_publish"


class _TrackingHeaders:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.accessed: list[str] = []

    def get(self, name: str, default: str = "") -> str:
        self.accessed.append(name)
        if name == "x-assessment-ai-authenticated-subject":
            raise AssertionError("subject was read before the token was accepted")
        return self.values.get(name, default)


def test_invalid_proxy_token_is_checked_before_subject_header(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    headers = _TrackingHeaders(
        {COMPUTATION_PROXY_TOKEN_HEADER: "wrong_proxy_token_0123456789abcdef"}
    )
    request = cast(Request, SimpleNamespace(headers=headers))

    assert _trusted_computation_specialist_subject(request, settings) is None
    assert headers.accessed == [COMPUTATION_PROXY_TOKEN_HEADER]


def test_missing_runtime_proxy_token_reads_no_request_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, proxy_token=None)
    headers = _TrackingHeaders({})
    request = cast(Request, SimpleNamespace(headers=headers))

    assert settings.computation_specialist_proxy_ready is False
    assert _trusted_computation_specialist_subject(request, settings) is None
    assert headers.accessed == []


def test_allowlist_without_runtime_proxy_token_cannot_expose_or_write_attestation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, proxy_token=None)
    app = create_app(settings)
    spoofed_headers = _attestation_headers(
        settings,
        token=PROXY_TOKEN,
        subject=SPECIALIST,
        reviewer=SPECIALIST,
    )
    with TestClient(app) as client:
        draft_id = _seed(app.state.repository)
        detail = client.get(f"/drafts/{draft_id}", headers=spoofed_headers)
        assert "/computation/attest" not in detail.text

        response = client.post(
            f"/drafts/{draft_id}/computation/attest",
            data={
                "rationale": (
                    "I independently checked the computation and its limitations."
                )
            },
            headers=spoofed_headers,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "authenticated" in response.headers["location"]
        assert (
            app.state.repository.list_current_computation_attestations(draft_id) == ()
        )
