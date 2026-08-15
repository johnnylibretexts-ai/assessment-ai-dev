"""The defect this whole effort exists to prevent, made impossible to reintroduce.

`test_publication_preconditions.py` judges preconditions in isolation, with no
database. This file asks the one question that cannot be answered there: does
the *readiness check* actually run every precondition in the list?

The expectation is derived from `PUBLICATION_PRECONDITIONS` rather than written
out, so an eighth precondition is covered the day it is added. A test that
asserted a fixed set of blocker codes would pass forever while a new condition
went unevaluated -- which is exactly the failure `docs/adr/0002` describes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app import publication_preconditions
from app.adapt import AdaptClient
from app.config import Settings
from app.db import (
    DraftRepository,
    DraftWrite,
    init_database,
)
from app.publication_preconditions import (
    PUBLICATION_PRECONDITIONS,
    PublicationBlocker,
    PublicationContext,
    PublicationPrecondition,
)
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
    SourceInfo,
)


@dataclass
class RecordingPrecondition:
    """A precondition that answers exactly as the real one, and remembers being asked.

    Wrapping rather than replacing keeps readiness producing its usual blockers,
    so this observes the real evaluation instead of a rehearsal of it.
    """

    inner: PublicationPrecondition
    contexts: list[PublicationContext] = field(default_factory=list)

    @property
    def name(self) -> str:
        return type(self.inner).__name__

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        self.contexts.append(context)
        return self.inner.evaluate(context)


def seeded_service(tmp_path: Path) -> tuple[PublicationService, int]:
    """A real repository holding one draft, and the service that reads it."""

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reachability.db'}",
        qti_storage_dir=tmp_path / "qti",
        ollama_api_key=None,
    )
    repository = DraftRepository(init_database(settings.database_url))
    question = QuestionDraft(
        concept_label="Conservation of energy",
        stem="Which statement describes conservation of energy?",
        choices=[
            Choice(id="A", text="Total energy stays constant.", correct=True),
            Choice(id="B", text="Energy disappears.", correct=False),
            Choice(id="C", text="Energy is matter.", correct=False),
            Choice(id="D", text="Energy has no units.", correct=False),
        ],
        explanation="The cited passage states that total energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[2],
    )
    page = NormalizedPage(
        title="Energy",
        plaintext="Energy remains constant.",
        htmlBody="<p>Energy remains constant.</p>",
        paragraphs=[
            Paragraph(index=2, text="Energy remains constant.", start=0, end=24)
        ],
        source=SourceInfo(
            canonical_url="https://chem.libretexts.org/Books/Energy",
            path="chem.libretexts.org/Books/Energy",
        ),
    )
    stored = repository.replace_generated_drafts(
        page=page,
        pipeline_version="test-reachability-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Conservation of energy",
                    description="Energy remains constant.",
                    source_paragraphs=[2],
                ),
                raw=question,
                critique=Critique(revision_required=False),
                revised=question,
            )
        ],
        llm_calls=[],
    )
    service = PublicationService(settings, repository, AdaptClient(settings))
    return service, stored.draft_ids[0]


def test_readiness_evaluates_every_enumerated_precondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a precondition the publish path ignores has to fail the suite.

    Every entry in the list is wrapped, then a real readiness check is run
    against a real draft. An entry with no recorded call was in the enumeration
    but never asked -- the silent desync that made this refactor worth doing,
    and the exact shape a partial evaluation of the list would take.
    """

    spies = tuple(
        RecordingPrecondition(precondition)
        for precondition in PUBLICATION_PRECONDITIONS
    )
    monkeypatch.setattr(
        publication_preconditions,
        "PUBLICATION_PRECONDITIONS",
        spies,
    )
    service, draft_id = seeded_service(tmp_path)

    service.readiness(draft_id, license_resolved=False)

    assert [spy.name for spy in spies if not spy.contexts] == []


def test_readiness_evaluates_each_precondition_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One assembled context, consulted once by each precondition.

    Twice would mean the list is being walked more than once per check, which is
    how the computation gate's repository reads got paid for twice before the
    context existed.
    """

    spies = tuple(
        RecordingPrecondition(precondition)
        for precondition in PUBLICATION_PRECONDITIONS
    )
    monkeypatch.setattr(
        publication_preconditions,
        "PUBLICATION_PRECONDITIONS",
        spies,
    )
    service, draft_id = seeded_service(tmp_path)

    service.readiness(draft_id, license_resolved=False)

    assert {spy.name: len(spy.contexts) for spy in spies} == {
        spy.name: 1 for spy in spies
    }
    # One context, assembled once and handed to all of them.
    assembled = {id(context) for spy in spies for context in spy.contexts}
    assert len(assembled) == 1
