"""Disposable Assessment AI instance for browser-level regressions.

Nothing here talks to a real ADAPT. The publisher is rebuilt around
:class:`FakeAdapt` immediately after startup, so "publishing" records local
state and never issues an outbound request. The database is a throwaway SQLite
file supplied by the caller.

Runnable directly for manual inspection::

    .venv/bin/python -m tests.browser.harness --port 8999

which seeds the same fixtures the automated regressions use and serves them.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from pydantic import SecretStr

from app.adapt import AdaptCreateResult, FrameworkItem, ResolvedAlignment
from app.config import Settings
from app.db import DraftRepository, DraftWrite, Publication, PublicationState
from app.main import create_app
from app.publishing import PublicationService
from app.schemas import (
    BloomLevel,
    Choice,
    Concept,
    Critique,
    Difficulty,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    SourceInfo,
)

# A curated source, so framework mapping resolves and publication is reachable.
ISOTOPES_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
    "02%3A_Atoms_and_the_Periodic_Table/2.03%3A_Isotopes_and_Atomic_Weight"
)

REVIEWER = "browser-regression@example.org"


class FakeAdapt:
    """Records what would have been published instead of publishing it."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.resolve_calls = 0
        self.hint_sync_calls = 0
        self.question_id = 4242

    async def resolve_destination(self, **_kwargs: object) -> ResolvedAlignment:
        self.resolve_calls += 1
        return ResolvedAlignment(
            framework_id=7,
            framework_title=(
                "Fundamentals of General, Organic, and Biological Chemistry"
                " (LibreTexts)"
            ),
            chapter=FrameworkItem(id=20, text="Atoms and the Periodic Table"),
            topic=FrameworkItem(id=23, text="Isotopes and Atomic Weight"),
            chapter_stable_id="chapter-stable-id",
            topic_stable_id="topic-stable-id",
        )

    async def create_question(self, _payload: dict[str, object]) -> AdaptCreateResult:
        self.create_calls += 1
        return AdaptCreateResult(question_id=self.question_id, page_id=self.question_id)

    async def find_question_by_tag(self, _tag: str) -> AdaptCreateResult | None:
        return None

    async def sync_hint_rungs(
        self, _question_id: int, _payload: dict[str, object]
    ) -> None:
        self.hint_sync_calls += 1


class PortUnavailable(RuntimeError):
    """The chosen port was taken between selection and bind."""


def free_port() -> int:
    """Ask the kernel for a free port.

    Inherently racy: the socket must close before uvicorn can bind the same
    port, so anything else on the host may take it in between. Callers retry
    with a fresh port rather than failing the run -- see disposable_instance.
    """

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_settings(
    db_path: Path, port: int, qti_dir: Path, **overrides: Any
) -> Settings:
    # allowed_origin must match the port the browser actually loads, or every
    # form POST is refused by the same-origin check.
    #
    # ``overrides`` lets a regression opt into a flag the default fixture leaves
    # off -- the generation-options fieldset, for instance, only renders when
    # advanced_items_enabled is true. Defaults stay unchanged for every caller
    # that passes nothing.
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{db_path}",
        allowed_origin=f"http://127.0.0.1:{port}",
        adapt_publishing_enabled=True,
        adapt_password=SecretStr("fake-adapt-password-never-used"),
        adapt_folder_id=42,
        qti_storage_dir=qti_dir,
        ollama_api_key=None,
        **overrides,
    )


def source_page(variant: str = "") -> NormalizedPage:
    text = (
        "Isotopes are atoms of the same element that contain different numbers"
        " of neutrons."
    )
    second = "The mass number is the sum of protons and neutrons in the nucleus."
    return NormalizedPage(
        title="2.3: Isotopes and Atomic Weight",
        plaintext=f"{text}\n\n{second}",
        htmlBody=f"<p>{text}</p><p>{second}</p>",
        paragraphs=[
            Paragraph(index=0, text=text, start=0, end=len(text)),
            Paragraph(
                index=1,
                text=second,
                start=len(text) + 2,
                end=len(text) + 2 + len(second),
            ),
        ],
        source=SourceInfo(
            backend="libretexts_public",
            # replace_generated_drafts() replaces every draft for a given page,
            # so distinct fixtures need distinct sources or the second seed
            # silently overwrites the first.
            canonical_url=f"{ISOTOPES_URL}{variant}",
            path=(
                "chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
                "Fundamentals_of_General_Organic_and_Biological_Chemistry_"
                "(LibreTexts)/02:_Atoms_and_the_Periodic_Table/"
                f"2.03:_Isotopes_and_Atomic_Weight{variant}"
            ),
            page_id=f"86190{variant}",
        ),
    )


def question(
    stem: str = "Which statement accurately describes isotopes?",
) -> QuestionDraft:
    return QuestionDraft(
        concept_label="Isotopes",
        stem=stem,
        choices=[
            Choice(id="A", text="Same protons, different neutrons.", correct=True),
            Choice(id="B", text="Different protons, same neutrons.", correct=False),
            Choice(id="C", text="Same protons and same neutrons.", correct=False),
            Choice(id="D", text="Different protons and neutrons.", correct=False),
        ],
        explanation=(
            "Isotopes share a proton count, which fixes the element, but differ"
            " in neutron count, which changes the mass number."
        ),
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
    )


def hint_ladder() -> HintLadderDraft:
    return HintLadderDraft(
        concept_label="Isotopes",
        rungs=[
            HintRungDraft(
                rung=HintRungType.CONCEPTUAL,
                text="Start from what makes two atoms the same element.",
                citation_paragraphs=[0],
            ),
            HintRungDraft(
                rung=HintRungType.STRATEGIC,
                text="The proton count fixes identity; neutrons may vary.",
                citation_paragraphs=[0],
            ),
            HintRungDraft(
                rung=HintRungType.SPECIFIC,
                text="Compare each option against that rule in turn.",
                citation_paragraphs=[0],
            ),
        ],
    )


# The seeded question cites paragraph 0 only, so every hint must too. Paragraph
# 1 exists on the page but is outside the item source, which makes it the
# natural "valid-looking but ungrounded" value for the failed-edit regression.
ALLOWED_PARAGRAPH = 0
UNGROUNDED_PARAGRAPH = 1


def seed_draft(repository: DraftRepository, variant: str = "") -> int:
    stored = repository.replace_generated_drafts(
        page=source_page(variant),
        pipeline_version="browser-regression-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Isotopes",
                    description="Same element, differing neutron counts.",
                    source_paragraphs=[0],
                ),
                raw=question("An earlier, vaguer stem about isotopes?"),
                critique=Critique(
                    issues=["The first stem was ambiguous."],
                    revision_required=True,
                ),
                revised=question(),
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    repository.save_hint_ladder(draft_id, hint_ladder(), editor=REVIEWER)
    return draft_id


def mark_published(
    repository: DraftRepository, draft_id: int, *, adapt_question_id: int = 4242
) -> None:
    """Record a completed publication without contacting ADAPT."""

    draft = repository.require_draft(draft_id)
    publication, created = repository.create_or_get_publication(
        {
            "draft_id": draft.id,
            "edit_count": draft.edit_count,
            "question_snapshot_json": draft.current_json,
            "source_snapshot_json": {"source_id": draft.source_snapshot_id},
            "reviewer_identity": REVIEWER,
            "approved_at": datetime.now(UTC),
            "destination_folder_id": 42,
            "destination_folder_name": "Assessment AI — Approved",
            "author": "LibreTexts",
            "public": True,
            "license": "CC BY-NC-SA",
            "license_version": "4.0",
            "license_label": "CC BY-NC-SA 4.0",
            "license_evidence_url": "https://example.invalid/license",
            "framework_id": 7,
            "framework_title": (
                "Fundamentals of General, Organic, and Biological Chemistry"
                " (LibreTexts)"
            ),
            "alignment_json": {
                "topic": {"text": "Isotopes and Atomic Weight", "stable_id": "topic"}
            },
            "stable_topic_ids_json": ["topic"],
            "hint_ladder_snapshot_json": None,
            "publication_key": f"{draft.id:064x}",
            "payload_hash": f"{adapt_question_id:064x}",
            "payload_mapper_version": "browser-regression-1",
            "qti_exporter_version": "browser-regression-1",
        }
    )
    if not created:  # pragma: no cover - defensive
        raise RuntimeError("publication already existed for this revision")
    repository.update_publication(
        publication.id,
        state=PublicationState.SUCCEEDED,
        adapt_question_id=adapt_question_id,
        adapt_page_id=adapt_question_id,
        finalized_at=datetime.now(UTC),
    )


def force_publication_state(app: Any, draft_id: int, state: str) -> None:
    """Write a publication state around the repository, as a successor build would.

    ``update_publication`` takes the enum, so a state this build cannot read is
    only representable by writing the column directly -- which is also the only
    way one arrives in production: a rollback to a build whose enum is older, or
    a hand-edited row. The column is a plain ``String(30)`` with no constraint.
    """

    repository: DraftRepository = app.state.repository
    publications = repository.require_draft(draft_id).publications
    if len(publications) != 1:  # pragma: no cover - defensive
        raise RuntimeError(f"expected exactly one publication, got {len(publications)}")
    with app.state.database.session_factory.begin() as session:
        stored = session.get(Publication, publications[0].id)
        if stored is None:  # pragma: no cover - defensive
            raise RuntimeError("publication vanished between read and write")
        stored.state = state


class LiveServer:
    """A uvicorn instance on a real port, so a browser can drive it."""

    def __init__(self, app: Any, port: int) -> None:
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                # uvicorn raises inside the thread when the port is taken. Say
                # so immediately: waiting out the full timeout would report
                # "did not start in time", which points at the wrong cause.
                raise PortUnavailable(
                    f"server thread exited before binding {self.base_url}"
                )
            time.sleep(0.05)
        raise RuntimeError("live server did not start in time")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@contextmanager
def disposable_instance(
    tmp_path: Path, port: int | None = None, **setting_overrides: Any
) -> Iterator[dict]:
    """Start a seeded, fake-publishing Assessment AI and yield its handles."""

    # The port is baked into allowed_origin, so losing a port race means
    # rebuilding Settings and the app, not merely re-binding. An explicit port
    # is never retried -- the caller asked for that one specifically.
    attempts = 1 if port else 3
    settings = None
    server = None
    app = None
    for attempt in range(1, attempts + 1):
        chosen = port or free_port()
        settings = build_settings(
            tmp_path / "browser-regression.db",
            chosen,
            tmp_path / "qti",
            **setting_overrides,
        )
        app = create_app(settings)
        server = LiveServer(app, chosen)
        try:
            server.start()
            break
        except PortUnavailable:
            server.stop()
            if attempt == attempts:
                raise
    if server is None or settings is None or app is None:  # pragma: no cover
        raise RuntimeError("failed to start a disposable instance")
    try:
        fake = FakeAdapt()
        # Rebuild the publisher around the fake BEFORE anything can publish.
        app.state.publisher = PublicationService(settings, app.state.repository, fake)
        repository = app.state.repository
        yield {
            "base_url": server.base_url,
            "app": app,
            "repository": repository,
            "fake": fake,
            "settings": settings,
        }
    finally:
        server.stop()


def main() -> None:  # pragma: no cover - manual inspection helper
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8999)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="assessment-ai-browser-"))
    with disposable_instance(tmp, port=args.port) as instance:
        repository = instance["repository"]
        plain = seed_draft(repository)
        published = seed_draft(repository)
        mark_published(repository, published)
        print(f"  database:  {tmp}")
        print(f"  serving:   {instance['base_url']}")
        print(f"  drafts:    unpublished={plain} published={published}")
        print("  NOTE: requests need the header X-Reviewer: " + REVIEWER)
        print("  Ctrl-C to stop.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":  # pragma: no cover
    main()
