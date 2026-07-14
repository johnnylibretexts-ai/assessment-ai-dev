from __future__ import annotations

from pathlib import Path

import pytest

from app.db import DraftRepository, init_database
from app.schemas import AssessmentItemType, ReviewStatus
from evaluation.publication_canary import (
    CANARY_MARKER,
    PublicationCanaryError,
    seed_publication_canary,
)


def _database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def test_publication_canary_requires_exact_marker(tmp_path: Path) -> None:
    with pytest.raises(PublicationCanaryError, match="exact BUILD-08"):
        seed_publication_canary(
            _database_url(tmp_path / "publication.db"),
            canary_marker="",
        )


def test_publication_canary_seeds_approved_19_type_matrix_and_recovery_probe(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "publication.db"
    database_url = _database_url(database_path)

    first = seed_publication_canary(database_url, canary_marker=CANARY_MARKER)
    second = seed_publication_canary(database_url, canary_marker=CANARY_MARKER)

    assert first == second
    assert first["fixture_count"] == 19
    assert first["item_type_count"] == 19
    assert {record["item_type"] for record in first["drafts"]} == {
        item_type.value for item_type in AssessmentItemType
    }
    assert first["recovery_probe"]["item_type"] == "multiple_choice"
    assert first["recovery_probe"]["draft_id"] not in {
        record["draft_id"] for record in first["drafts"]
    }

    database = init_database(database_url)
    repository = DraftRepository(database)
    try:
        drafts = repository.list_drafts(current_sources_only=False)
        assert len(drafts) == 20
        assert all(draft.status == ReviewStatus.READY_TO_PUBLISH for draft in drafts)
        assert all(
            draft.current_hint_ladder is not None
            and draft.current_hint_ladder.status == "approved"
            for draft in drafts
        )
        assert all(draft.bloom_confirmed for draft in drafts)
        assert all(draft.difficulty_confirmed for draft in drafts)
        engines = [
            draft
            for draft in drafts
            if draft.current.item_type
            in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}
        ]
        assert len(engines) == 2
        assert all(
            draft.current_engine_validation is not None
            and draft.current_engine_validation.seed_count == 100
            for draft in engines
        )
    finally:
        database.dispose()
