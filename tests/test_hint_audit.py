from pathlib import Path

from app.db import Database, DraftRepository, DraftWrite, init_database
from app.hint_audit import audit_current_hints, read_only_sqlite_url
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


def _question(citations: list[int]) -> QuestionDraft:
    return QuestionDraft(
        concept_label="Energy",
        stem="Which statement describes energy?",
        choices=[
            Choice(id="A", text="It is conserved.", correct=True),
            Choice(id="B", text="It vanishes.", correct=False),
            Choice(id="C", text="It is matter.", correct=False),
            Choice(id="D", text="It has no units.", correct=False),
        ],
        explanation="Energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=citations,
    )


def test_audit_reports_only_the_current_hint_version_without_mutating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.db"
    database_url = f"sqlite:///{path}"
    database = init_database(database_url)
    repository = DraftRepository(database)
    text = "Energy is conserved. A system boundary defines what is tracked."
    stored = repository.replace_generated_drafts(
        page=NormalizedPage(
            title="Energy",
            plaintext=text,
            htmlBody=f"<p>{text}</p>",
            paragraphs=[
                Paragraph(index=0, text="Energy is conserved.", start=0, end=20),
                Paragraph(
                    index=1,
                    text="A system boundary defines what is tracked.",
                    start=21,
                    end=len(text),
                ),
            ],
            source=SourceInfo(
                canonical_url="https://chem.libretexts.org/Books/Energy",
                path="chem.libretexts.org/Books/Energy",
            ),
        ),
        pipeline_version="test-audit-v1",
        drafts=[
            DraftWrite(
                position=0,
                concept=Concept(
                    label="Energy",
                    description="Energy tracking.",
                    source_paragraphs=[0, 1],
                ),
                raw=_question([0, 1]),
                critique=Critique(revision_required=False),
                revised=_question([0, 1]),
            )
        ],
        llm_calls=[],
    )
    draft_id = stored.draft_ids[0]
    repository.save_hint_ladder(
        draft_id,
        HintLadderDraft(
            concept_label="Energy",
            rungs=[
                HintRungDraft(
                    rung=rung,
                    text=f"Use {rung.value} reasoning.",
                    citation_paragraphs=[1],
                )
                for rung in HintRungType
            ],
        ),
        editor="reviewer",
    )
    repository.edit_draft(
        draft_id,
        _question([0]),
        editor="reviewer",
        notes="Narrow the exact source.",
    )

    before = repository.require_draft(draft_id)
    findings = audit_current_hints(repository)
    after = repository.require_draft(draft_id)

    assert [(item["rung"], item["invalid_citations"]) for item in findings] == [
        ("conceptual", [1]),
        ("specific", [1]),
        ("strategic", [1]),
    ]
    assert before.current_hint_ladder is not None
    assert after.current_hint_ladder is not None
    assert after.current_hint_ladder.id == before.current_hint_ladder.id
    assert after.current_hint_ladder.status == "needs_repair"
    database.dispose()

    read_only = Database(read_only_sqlite_url(database_url))
    try:
        assert len(audit_current_hints(DraftRepository(read_only))) == 3
    finally:
        read_only.dispose()
