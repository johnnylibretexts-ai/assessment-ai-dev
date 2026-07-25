from __future__ import annotations

import asyncio
import re
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapt import (
    AdaptAmbiguousError,
    AdaptCreateResult,
    AdaptPublishingError,
    FrameworkItem,
    ResolvedAlignment,
)
from app.catalog import alignment_for_source, suggested_topic
from app.config import Settings
from app.db import (
    DraftRepository,
    DraftWrite,
    EngineValidationRecord,
    PublicationState,
)
from app.main import create_app
from app.pipeline import ReviewService
from app.parameterized import compile_parameterized_item
from app import publishing as publishing_module
from app.publishing import PublicationService, PublicationValidationError
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    ItemResponse,
    NormalizedPage,
    ParameterVariable,
    ParameterizedItemSpec,
    Paragraph,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
)
from evaluation.publication_canary import (
    CANARY_MARKER,
    seed_publication_canary,
)


ISOTOPES_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
    "02%3A_Atoms_and_the_Periodic_Table/2.03%3A_Isotopes_and_Atomic_Weight"
)
MATHEMATICAL_METHODS_URL = (
    "https://chem.libretexts.org/Bookshelves/"
    "Physical_and_Theoretical_Chemistry_Textbook_Maps/"
    "Mathematical_Methods_in_Chemistry_%28Levitus%29/"
    "05%3A_Second_Order_Ordinary_Differential_Equations/"
    "5.01%3A_Second_Order_Ordinary_Differential_Equations"
)


def configured_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'publishing.db'}",
        allowed_origin="http://testserver",
        adapt_publishing_enabled=True,
        adapt_password=SecretStr("never-log-this-password"),
        adapt_folder_id=42,
        qti_storage_dir=tmp_path / "qti",
        ollama_api_key=None,
    )


def isotope_question(
    stem: str = "Which statement accurately describes isotopes?",
) -> QuestionDraft:
    return QuestionDraft(
        concept_label="Isotopes",
        stem=stem,
        choices=[
            Choice(id="A", text="Same protons, different neutrons.", correct=True),
            Choice(id="B", text="Different protons, same neutrons.", correct=False),
            Choice(id="C", text="Same protons and neutrons.", correct=False),
            Choice(id="D", text="Different protons and neutrons.", correct=False),
        ],
        explanation="Isotopes share an atomic number but differ in neutron count.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def seed_approved(repository: DraftRepository) -> int:
    text = "Isotopes have the same number of protons and different neutron counts."
    page = NormalizedPage(
        title="2.3: Isotopes and Atomic Weight",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=ISOTOPES_URL,
            path=(
                "chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
                "Fundamentals_of_General_Organic_and_Biological_Chemistry_(LibreTexts)/"
                "02:_Atoms_and_the_Periodic_Table/2.03:_Isotopes_and_Atomic_Weight"
            ),
            page_id="86190",
        ),
    )
    stored = repository.replace_generated_drafts(
        page=page,
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Isotopes",
                    description="Atoms with varied neutron counts.",
                    source_paragraphs=[0],
                ),
                raw=isotope_question(),
                critique=Critique(issues=[], revision_required=False),
                revised=isotope_question(),
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    ReviewService(repository).decide(
        draft_id,
        ReviewDecision(
            status=ReviewStatus.READY_TO_PUBLISH,
            bloom_confirmed=True,
            difficulty_confirmed=True,
            reviewer_notes="Labels and source checked.",
        ),
        reviewer="reviewer@example.org",
    )
    return draft_id


def approve_hint_ladder(repository: DraftRepository, draft_id: int) -> None:
    ladder = HintLadderDraft(
        concept_label="Isotopes",
        rungs=[
            HintRungDraft(
                rung=HintRungType.CONCEPTUAL,
                text="Recall which subatomic count defines an element.",
                citation_paragraphs=[0],
            ),
            HintRungDraft(
                rung=HintRungType.STRATEGIC,
                text="Compare atomic number with the count that changes mass.",
                citation_paragraphs=[0],
            ),
            HintRungDraft(
                rung=HintRungType.SPECIFIC,
                text="Keep the identity-defining count fixed and vary another nucleon count.",
                citation_paragraphs=[0],
            ),
        ],
    )
    repository.save_hint_ladder(
        draft_id,
        ladder,
        editor="hint-author@example.org",
    )
    repository.review_hint_ladder(
        draft_id,
        reviewer="hint-reviewer@example.org",
        confirmed_rungs=["conceptual", "strategic", "specific"],
        approved=True,
    )


def seed_approved_webwork(repository: DraftRepository) -> tuple[int, str, str]:
    text = "A numerical product multiplies one factor by another factor."
    page = NormalizedPage(
        title="2.3: Isotopes and Atomic Weight",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=ISOTOPES_URL,
            path=(
                "chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
                "Fundamentals_of_General_Organic_and_Biological_Chemistry_(LibreTexts)/"
                "02:_Atoms_and_the_Periodic_Table/2.03:_Isotopes_and_Atomic_Weight"
            ),
            page_id="86190-webwork",
        ),
    )
    spec = ParameterizedItemSpec(
        engine="webwork",
        variables=[
            ParameterVariable(name="mass", minimum=1, maximum=10, step=1),
            ParameterVariable(name="speed", minimum=2, maximum=12, step=2),
        ],
        constraints=["mass != speed"],
        prompt_template="Find the product of {mass} and {speed}.",
        answer_expression="mass * speed",
        explanation_template="Multiply {mass} by {speed}.",
        tolerance=0.01,
    )
    question = QuestionDraft(
        item_type=AssessmentItemType.WEBWORK,
        concept_label="Numerical products",
        stem="Find the product of the displayed factors.",
        response=ItemResponse(parameterized=spec),
        explanation="Multiply the two displayed factors.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
        specialist_review_required=True,
    )
    compiled = compile_parameterized_item(spec, validation_seeds=25)
    stored = repository.replace_generated_drafts(
        page=page,
        pipeline_version="test-webwork-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Numerical products",
                    description="Multiply two numerical factors.",
                    source_paragraphs=[0],
                ),
                raw=question,
                critique=Critique(issues=[], revision_required=False),
                revised=question,
                engine_validation={
                    "engine": compiled.engine,
                    "compiler_version": compiled.compiler_version,
                    "source_sha256": compiled.source_sha256,
                    "seed_count": len(compiled.previews),
                    "previews": [
                        {
                            "seed": preview.seed,
                            "variables": preview.variables,
                            "prompt": preview.prompt,
                            "answer": preview.answer,
                            "explanation": preview.explanation,
                        }
                        for preview in compiled.previews
                    ],
                },
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    ReviewService(repository).decide(
        draft_id,
        ReviewDecision(
            status=ReviewStatus.READY_TO_PUBLISH,
            bloom_confirmed=True,
            difficulty_confirmed=True,
            reviewer_notes="Labels, source, and engine evidence checked.",
        ),
        reviewer="reviewer@example.org",
    )
    return draft_id, compiled.compiler_version, compiled.source_sha256


class FakeAdapt:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.create_calls = 0
        self.question_id = 501
        self.hint_sync_calls = 0
        self.hint_payload: dict[str, object] | None = None

    async def resolve_destination(self, **_kwargs: object) -> ResolvedAlignment:
        self.resolve_calls += 1
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        return ResolvedAlignment(
            framework_id=7,
            framework_title=(
                "Fundamentals of General, Organic, and Biological Chemistry (LibreTexts)"
            ),
            chapter=FrameworkItem(id=20, text=topic.chapter_title),
            topic=FrameworkItem(id=23, text=topic.title),
            chapter_stable_id=topic.chapter_stable_id,
            topic_stable_id=topic.stable_id,
        )

    async def create_question(self, _payload: dict[str, object]) -> AdaptCreateResult:
        self.create_calls += 1
        return AdaptCreateResult(
            question_id=self.question_id,
            page_id=self.question_id,
        )

    async def find_question_by_tag(self, _tag: str) -> AdaptCreateResult | None:
        return None

    async def sync_hint_rungs(
        self, _question_id: int, payload: dict[str, object]
    ) -> None:
        self.hint_sync_calls += 1
        self.hint_payload = payload


def publishing_headers() -> dict[str, str]:
    return {
        "Origin": "http://testserver",
        "X-Reviewer": "reviewer@example.org",
    }


UNCURATED_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "A_Book_Outside_Every_Curated_Framework/"
    "02%3A_Atoms_and_the_Periodic_Table/2.03%3A_Isotopes_and_Atomic_Weight"
)


def seed_unapproved_uncurated(repository: DraftRepository) -> int:
    """Seed a draft that is neither approved nor license-mapped."""

    text = "Isotopes have the same number of protons and different neutron counts."
    page = NormalizedPage(
        title="2.3: Isotopes and Atomic Weight",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=UNCURATED_URL,
            path=(
                "chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
                "A_Book_Outside_Every_Curated_Framework/"
                "02:_Atoms_and_the_Periodic_Table/2.03:_Isotopes_and_Atomic_Weight"
            ),
            page_id="86191",
        ),
    )
    stored = repository.replace_generated_drafts(
        page=page,
        pipeline_version="test-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Isotopes",
                    description="Atoms with varied neutron counts.",
                    source_paragraphs=[0],
                ),
                raw=isotope_question(),
                critique=Critique(issues=[], revision_required=False),
                revised=isotope_question(),
            )
        ],
        llm_calls=[],
    )
    return stored.draft_ids[0]


@pytest.mark.asyncio
async def test_publication_reports_the_unapproved_revision_before_the_license(
    tmp_path: Path,
) -> None:
    """Readiness blockers outrank license resolution in the publish path.

    Resolving the license first made an unapproved draft on an unmapped page
    report "select a license" — the wrong next action, and not the blocker the
    readiness panel shows first.
    """

    config = configured_settings(tmp_path)
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_unapproved_uncurated(repository)
        readiness = PublicationService(config, repository, fake).readiness(
            draft_id,
            license_resolved=False,
        )
        assert readiness.blockers[0].code == "question_not_approved"

        with pytest.raises(
            PublicationValidationError,
            match=re.escape("Approve the current question revision."),
        ):
            await PublicationService(config, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id="any-topic",
                alignment_confirmed=True,
            )

        assert fake.resolve_calls == 0
        assert fake.create_calls == 0


@pytest.mark.asyncio
async def test_publication_evaluates_the_computation_gate_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate performs repository reads, so publish must not run it twice."""

    config = configured_settings(tmp_path)
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)
        approve_hint_ladder(repository, draft_id)

        calls = 0
        original = publishing_module.require_computation_gate

        def counting_gate(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            publishing_module,
            "require_computation_gate",
            counting_gate,
        )
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        publication = await PublicationService(config, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert publication.state == PublicationState.SUCCEEDED.value
        assert calls == 1


@pytest.mark.asyncio
async def test_publish_rejects_a_forged_topic_from_another_framework(
    tmp_path: Path,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        draft_id = seed_approved(app.state.repository)
        forged = alignment_for_source(MATHEMATICAL_METHODS_URL)
        assert forged is not None

        with pytest.raises(
            PublicationValidationError,
            match="does not match this source",
        ):
            await PublicationService(
                config,
                app.state.repository,
                fake,
            ).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=forged.topic.stable_id,
                alignment_confirmed=True,
            )

        assert fake.resolve_calls == 0
        assert fake.create_calls == 0


def test_publish_route_is_idempotent_and_qti_download_is_protected(
    tmp_path: Path,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app) as client:
        draft_id = seed_approved(app.state.repository)
        app.state.publisher = PublicationService(config, app.state.repository, fake)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        data = {
            "topic_stable_id": topic.stable_id,
            "alignment_confirmed": "true",
        }

        first = client.post(
            f"/drafts/{draft_id}/publish",
            data=data,
            headers=publishing_headers(),
            follow_redirects=False,
        )
        second = client.post(
            f"/drafts/{draft_id}/publish",
            data=data,
            headers=publishing_headers(),
            follow_redirects=False,
        )
        assert first.status_code == 303
        assert "Published+to+ADAPT" in first.headers["location"]
        assert second.status_code == 303
        assert fake.create_calls == 1

        draft = app.state.repository.require_draft(draft_id)
        assert len(draft.publications) == 1
        publication = draft.publications[0]
        assert publication.state == PublicationState.SUCCEEDED.value
        assert publication.adapt_question_id == 501
        assert publication.license == "ccbyncsa"
        assert publication.license_version == "3.0"

        blocked = client.get(
            f"/drafts/{draft_id}/publications/{publication.id}/qti",
            headers={"Origin": "http://testserver"},
        )
        assert blocked.status_code == 403
        download = client.get(
            f"/drafts/{draft_id}/publications/{publication.id}/qti",
            headers=publishing_headers(),
        )
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(Path(publication.qti_path)) as archive:
            assert "imsmanifest.xml" in archive.namelist()

        detail = client.get(f"/drafts/{draft_id}")
        assert "Approved — not yet published" not in detail.text
        assert "Published to ADAPT" in detail.text
        assert "ADAPT question ID: 501" in detail.text
        assert "This publication is bound to the current draft revision." in detail.text
        assert f'action="/drafts/{draft_id}/publish"' not in detail.text
        assert "Download QTI 3.0" in detail.text


@pytest.mark.asyncio
async def test_off_mode_preserves_build08_compiler_drift_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        repository = app.state.repository
        draft_id, _compiler_version, _source_sha256 = seed_approved_webwork(repository)
        draft = repository.require_draft(draft_id)
        assert draft.current.response.parameterized is not None
        freshly_compiled = compile_parameterized_item(
            draft.current.response.parameterized,
            validation_seeds=25,
        )
        drifted = replace(
            freshly_compiled,
            compiler_version=f"{freshly_compiled.compiler_version}-deployed-drift",
        )
        monkeypatch.setattr(
            "app.publishing.compile_parameterized_item",
            lambda *_args, **_kwargs: drifted,
        )
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None

        publication = await PublicationService(config, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert publication.state == PublicationState.SUCCEEDED.value
        assert fake.resolve_calls == 1
        assert fake.create_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "assist"])
async def test_non_enforce_modes_preserve_build08_engine_race_semantics(
    tmp_path: Path,
    mode: str,
) -> None:
    config = configured_settings(tmp_path).model_copy(update={"computation_mode": mode})
    app = create_app(config)
    with TestClient(app):
        repository = app.state.repository
        draft_id, _compiler_version, _source_sha256 = seed_approved_webwork(repository)
        original = repository.require_draft(draft_id).current_engine_validation
        assert original is not None

        class RacingAdapt(FakeAdapt):
            async def resolve_destination(
                self,
                **kwargs: object,
            ) -> ResolvedAlignment:
                with app.state.database.session_factory.begin() as session:
                    session.add(
                        EngineValidationRecord(
                            draft_id=original.draft_id,
                            edit_count=original.edit_count,
                            engine=original.engine,
                            compiler_version=f"{original.compiler_version}-raced",
                            source_sha256="2" * 64,
                            seed_count=original.seed_count,
                            previews_json=original.previews_json,
                            status="passed",
                        )
                    )
                return await super().resolve_destination(**kwargs)

        fake = RacingAdapt()
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        publication = await PublicationService(config, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert fake.resolve_calls == 1
        assert fake.create_calls == 1
        assert publication.state == PublicationState.SUCCEEDED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "assist"])
async def test_non_enforce_modes_preserve_build08_hint_race_semantics(
    tmp_path: Path,
    mode: str,
) -> None:
    config = configured_settings(tmp_path).model_copy(
        update={
            "computation_mode": mode,
            "hint_generation_enabled": True,
        }
    )
    app = create_app(config)
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)
        approve_hint_ladder(repository, draft_id)

        class RacingHintAdapt(FakeAdapt):
            async def resolve_destination(
                self,
                **kwargs: object,
            ) -> ResolvedAlignment:
                repository.save_hint_ladder(
                    draft_id,
                    HintLadderDraft(
                        concept_label="Isotopes",
                        rungs=[
                            HintRungDraft(
                                rung=HintRungType.CONCEPTUAL,
                                text="Start from the definition of atomic identity.",
                                citation_paragraphs=[0],
                            ),
                            HintRungDraft(
                                rung=HintRungType.STRATEGIC,
                                text="Separate identity from total nucleon count.",
                                citation_paragraphs=[0],
                            ),
                            HintRungDraft(
                                rung=HintRungType.SPECIFIC,
                                text="Compare the two relevant particle counts.",
                                citation_paragraphs=[0],
                            ),
                        ],
                    ),
                    editor="racing-hint-author@example.org",
                )
                return await super().resolve_destination(**kwargs)

        fake = RacingHintAdapt()
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        publication = await PublicationService(config, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert fake.resolve_calls == 1
        assert fake.create_calls == 1
        assert publication.state == PublicationState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_enforce_mode_hint_race_blocks_atomic_publication_reservation(
    tmp_path: Path,
) -> None:
    config = configured_settings(tmp_path).model_copy(
        update={
            "computation_mode": "enforce",
            "hint_generation_enabled": True,
        }
    )
    app = create_app(config)
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)
        approve_hint_ladder(repository, draft_id)

        class RacingHintAdapt(FakeAdapt):
            async def resolve_destination(
                self,
                **kwargs: object,
            ) -> ResolvedAlignment:
                repository.save_hint_ladder(
                    draft_id,
                    HintLadderDraft(
                        concept_label="Isotopes",
                        rungs=[
                            HintRungDraft(
                                rung=HintRungType.CONCEPTUAL,
                                text="Start from the definition of atomic identity.",
                                citation_paragraphs=[0],
                            ),
                            HintRungDraft(
                                rung=HintRungType.STRATEGIC,
                                text="Separate identity from total nucleon count.",
                                citation_paragraphs=[0],
                            ),
                            HintRungDraft(
                                rung=HintRungType.SPECIFIC,
                                text="Compare the two relevant particle counts.",
                                citation_paragraphs=[0],
                            ),
                        ],
                    ),
                    editor="racing-hint-author@example.org",
                )
                return await super().resolve_destination(**kwargs)

        fake = RacingHintAdapt()
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        with pytest.raises(
            PublicationValidationError,
            match="changed before publication reservation",
        ):
            await PublicationService(config, repository, fake).publish(
                draft_id,
                publisher="reviewer@example.org",
                topic_stable_id=topic.stable_id,
                alignment_confirmed=True,
            )

        assert fake.resolve_calls == 1
        assert fake.create_calls == 0
        assert repository.require_draft(draft_id).publications == []


@pytest.mark.asyncio
async def test_off_mode_preserves_external_publication_key_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        repository = app.state.repository
        draft_id, _compiler_version, _source_sha256 = seed_approved_webwork(repository)
        service = PublicationService(config, repository, fake)
        captured: dict[str, object] = {}
        original = service._key_material

        def capture_key_material(*args: object, **kwargs: object) -> dict[str, object]:
            material = original(*args, **kwargs)  # type: ignore[arg-type]
            captured.update(material)
            return material

        monkeypatch.setattr(service, "_key_material", capture_key_material)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        publication = await service.publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert publication.state == PublicationState.SUCCEEDED.value
        assert "engine_validation" not in captured
        assert fake.resolve_calls == 1
        assert fake.create_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "assist"])
async def test_non_enforce_publication_does_not_read_or_bind_computation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    config = configured_settings(tmp_path).model_copy(update={"computation_mode": mode})
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        repository = app.state.repository
        draft_id, _compiler_version, _source_sha256 = seed_approved_webwork(repository)

        def unexpected(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("non-enforce publication touched computation evidence")

        monkeypatch.setattr(
            repository,
            "get_current_computation_validation",
            unexpected,
        )
        monkeypatch.setattr(
            repository,
            "list_computation_validations",
            unexpected,
        )
        monkeypatch.setattr(
            repository,
            "create_or_get_publication_guarded",
            unexpected,
        )
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None

        publication = await PublicationService(config, repository, fake).publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )

        assert publication.state == PublicationState.SUCCEEDED.value
        assert fake.resolve_calls == 1
        assert fake.create_calls == 1


@pytest.mark.asyncio
async def test_qualification_canary_publishes_approved_hints_with_flag_false(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'qualification-canary.db'}"
    manifest = seed_publication_canary(
        database_url,
        canary_marker=CANARY_MARKER,
    )
    config = configured_settings(tmp_path).model_copy(
        update={
            "database_url": database_url,
            "qualification_canary_marker": CANARY_MARKER,
            "hint_generation_enabled": False,
        }
    )
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        service = PublicationService(config, app.state.repository, fake)
        published = await service.publish(
            int(manifest["recovery_probe"]["draft_id"]),
            publisher="build08-publication-reviewer",
            topic_stable_id=str(manifest["topic_stable_id"]),
            alignment_confirmed=True,
        )
        persisted = app.state.repository.require_publication(published.id)

    assert config.hint_generation_enabled is False
    assert published.state == PublicationState.SUCCEEDED.value
    assert published.hints_synced_at is not None
    assert fake.hint_sync_calls == 1
    assert fake.hint_payload is not None
    assert len(fake.hint_payload["ladder"]["rungs"]) == 3
    assert [attempt.action for attempt in persisted.attempts] == [
        "adapt_create",
        "adapt_hint_sync",
        "qti_finalize",
    ]


@pytest.mark.asyncio
async def test_unknown_create_is_reconciled_without_a_second_post(
    tmp_path: Path,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)

        class AmbiguousAdapt(FakeAdapt):
            async def create_question(
                self, _payload: dict[str, object]
            ) -> AdaptCreateResult:
                self.create_calls += 1
                raise AdaptAmbiguousError("No response.", code="adapt_no_response")

            async def find_question_by_tag(self, _tag: str) -> AdaptCreateResult | None:
                return AdaptCreateResult(question_id=901, page_id=901)

        fake = AmbiguousAdapt()
        service = PublicationService(config, repository, fake)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        first = await service.publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )
        assert first.state == PublicationState.UNKNOWN.value
        second = await service.publish(
            draft_id,
            publisher="reviewer@example.org",
            topic_stable_id=topic.stable_id,
            alignment_confirmed=True,
        )
        assert second.state == PublicationState.SUCCEEDED.value
        assert second.adapt_question_id == 901
        assert fake.create_calls == 1


@pytest.mark.asyncio
async def test_adapt_outage_is_retryable_without_partial_publication(
    tmp_path: Path,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)

        class FailingOnceAdapt(FakeAdapt):
            async def create_question(
                self, payload: dict[str, object]
            ) -> AdaptCreateResult:
                self.create_calls += 1
                if self.create_calls == 1:
                    raise AdaptPublishingError(
                        "ADAPT is temporarily unavailable.",
                        code="adapt_unavailable",
                    )
                return AdaptCreateResult(question_id=711, page_id=711)

        fake = FailingOnceAdapt()
        service = PublicationService(config, repository, fake)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        arguments = {
            "publisher": "reviewer@example.org",
            "topic_stable_id": topic.stable_id,
            "alignment_confirmed": True,
        }

        failed = await service.publish(draft_id, **arguments)
        assert failed.state == PublicationState.FAILED.value
        assert failed.error_code == "adapt_unavailable"
        assert failed.adapt_question_id is None
        assert failed.qti_path is None
        assert len(repository.require_draft(draft_id).publications) == 1

        recovered = await service.publish(draft_id, **arguments)
        assert recovered.state == PublicationState.SUCCEEDED.value
        assert recovered.adapt_question_id == 711
        assert recovered.qti_path is not None
        assert fake.create_calls == 2
        assert len(repository.require_draft(draft_id).publications) == 1


@pytest.mark.asyncio
async def test_qti_storage_outage_resumes_without_duplicate_adapt_create(
    tmp_path: Path,
) -> None:
    blocked_storage = tmp_path / "blocked-qti-storage"
    blocked_storage.write_text("not a directory", encoding="utf-8")
    config = configured_settings(tmp_path).model_copy(
        update={"qti_storage_dir": blocked_storage}
    )
    app = create_app(config)
    fake = FakeAdapt()
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)
        service = PublicationService(config, repository, fake)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        arguments = {
            "publisher": "reviewer@example.org",
            "topic_stable_id": topic.stable_id,
            "alignment_confirmed": True,
        }

        retained = await service.publish(draft_id, **arguments)
        assert retained.state == PublicationState.ADAPT_CREATED.value
        assert retained.error_code == "qti_finalize_failed"
        assert retained.adapt_question_id == 501
        assert retained.qti_path is None
        assert fake.create_calls == 1

        blocked_storage.unlink()
        completed = await service.publish(draft_id, **arguments)
        assert completed.state == PublicationState.SUCCEEDED.value
        assert completed.adapt_question_id == 501
        assert completed.qti_path is not None
        assert fake.create_calls == 1
        assert len(repository.require_draft(draft_id).publications) == 1


@pytest.mark.asyncio
async def test_concurrent_publication_reservations_send_one_adapt_create(
    tmp_path: Path,
) -> None:
    config = configured_settings(tmp_path)
    app = create_app(config)
    with TestClient(app):
        repository = app.state.repository
        draft_id = seed_approved(repository)

        class BlockingAdapt(FakeAdapt):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def create_question(
                self, payload: dict[str, object]
            ) -> AdaptCreateResult:
                self.create_calls += 1
                self.entered.set()
                await self.release.wait()
                return AdaptCreateResult(question_id=700, page_id=700)

        fake = BlockingAdapt()
        service = PublicationService(config, repository, fake)
        topic = suggested_topic(ISOTOPES_URL)
        assert topic is not None
        arguments = {
            "publisher": "reviewer@example.org",
            "topic_stable_id": topic.stable_id,
            "alignment_confirmed": True,
        }
        first_task = asyncio.create_task(service.publish(draft_id, **arguments))
        await fake.entered.wait()
        concurrent = await service.publish(draft_id, **arguments)
        assert concurrent.state == PublicationState.PENDING.value
        fake.release.set()
        completed = await first_task
        assert completed.state == PublicationState.SUCCEEDED.value
        assert fake.create_calls == 1
