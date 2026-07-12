from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapt import (
    AdaptAmbiguousError,
    AdaptCreateResult,
    FrameworkItem,
    ResolvedAlignment,
)
from app.catalog import suggested_topic
from app.config import Settings
from app.db import DraftRepository, DraftWrite, PublicationState
from app.main import create_app
from app.pipeline import ReviewService
from app.publishing import PublicationService
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
)


ISOTOPES_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
    "02%3A_Atoms_and_the_Periodic_Table/2.03%3A_Isotopes_and_Atomic_Weight"
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


class FakeAdapt:
    def __init__(self) -> None:
        self.create_calls = 0
        self.question_id = 501

    async def resolve_destination(self, **_kwargs: object) -> ResolvedAlignment:
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


def publishing_headers() -> dict[str, str]:
    return {
        "Origin": "http://testserver",
        "X-Reviewer": "reviewer@example.org",
    }


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
        assert "Approved — not yet published" in detail.text
        assert "Published to ADAPT" in detail.text
        assert "ADAPT question ID" in detail.text
        assert "Download QTI 3.0" in detail.text


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
