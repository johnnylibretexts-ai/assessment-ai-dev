from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.db import DraftRepository, init_database
from app.schemas import AssessmentItemType, ItemContextType
from evaluation.browser_canary import (
    CANARY_MARKER,
    BrowserCanaryError,
    seed_browser_canary,
)
from evaluation.adapt_browser import build_adapt_browser_manifest


def _database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def test_browser_canary_requires_exact_marker(tmp_path: Path) -> None:
    with pytest.raises(BrowserCanaryError, match="exact BUILD-08"):
        seed_browser_canary(
            _database_url(tmp_path / "canary.db"),
            canary_marker="",
        )


def test_adapt_browser_manifest_covers_all_item_types_and_safe_media() -> None:
    manifest = build_adapt_browser_manifest()
    assert manifest["item_type_count"] == 19
    assert manifest["native_qti_count"] == 17
    assert manifest["external_engine_count"] == 2
    assert len({item["item_type"] for item in manifest["items"]}) == 19
    assert (
        manifest["items_sha256"]
        == hashlib.sha256(
            json.dumps(
                manifest["items"],
                separators=(",", ":"),
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    assert all(
        item["expected_response"] is not None
        for item in manifest["items"]
        if item["technology"] == "qti"
    )
    hotspot = next(
        item for item in manifest["items"] if item["item_type"] == "image_hotspot"
    )
    assert hotspot["qti_json"]["imageUrl"].startswith("data:image/svg+xml;base64,")
    for item_type in ("select_choice", "dropdown"):
        choice = next(
            item for item in manifest["items"] if item["item_type"] == item_type
        )
        assert isinstance(choice["qti_json"]["itemBody"], str)
        assert choice["qti_json"]["itemBody"].endswith("[select]")
        assert set(choice["qti_json"]["inline_choice_interactions"]) == {"select"}
        responses = choice["qti_json"]["inline_choice_interactions"]["select"]
        assert all(
            set(response) == {"value", "text", "correctResponse"}
            for response in responses
        )
        assert choice["expected_response"] == [
            response["value"] for response in responses if response["correctResponse"]
        ]


def test_browser_canary_requires_absolute_file_backed_sqlite() -> None:
    with pytest.raises(BrowserCanaryError, match="file-backed SQLite"):
        seed_browser_canary("sqlite:///:memory:", canary_marker=CANARY_MARKER)
    with pytest.raises(BrowserCanaryError, match="must be absolute"):
        seed_browser_canary("sqlite:///relative.db", canary_marker=CANARY_MARKER)
    with pytest.raises(BrowserCanaryError, match="file-backed SQLite"):
        seed_browser_canary(
            "postgresql://localhost/assessment",
            canary_marker=CANARY_MARKER,
        )


def test_browser_canary_seeds_exact_matrix_and_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "canary.db"
    database_url = _database_url(database_path)

    first = seed_browser_canary(database_url, canary_marker=CANARY_MARKER)
    second = seed_browser_canary(database_url, canary_marker=CANARY_MARKER)

    assert first == second
    assert first["fixture_count"] == 95
    assert first["item_type_count"] == 19
    assert first["context_type_count"] == 5
    assert len({record["fixture_id"] for record in first["drafts"]}) == 95
    assert {record["item_type"] for record in first["drafts"]} == {
        item_type.value for item_type in AssessmentItemType
    }
    assert {record["context_type"] for record in first["drafts"]} == {
        context_type.value for context_type in ItemContextType
    }

    database = init_database(database_url)
    try:
        drafts = DraftRepository(database).list_drafts(current_sources_only=False)
        assert len(drafts) == 95
        assert all(draft.current_hint_ladder is not None for draft in drafts)
        parameterized = [
            draft
            for draft in drafts
            if draft.current.item_type
            in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}
        ]
        assert len(parameterized) == 10
        assert all(
            draft.current_engine_validation is not None
            and draft.current_engine_validation.seed_count == 100
            for draft in parameterized
        )
    finally:
        database.dispose()


def test_browser_canary_rejects_an_unrelated_existing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "canary.db"
    database = init_database(_database_url(database_path))
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO generation_jobs "
                "(id, source_type, source_locator, request_json, reviewer_identity, "
                "status, stage, progress, draft_ids_json, created_at, updated_at) "
                "VALUES ('unrelated', 'public', 'example', '{}', 'tester', "
                "'pending', 'queued', 0, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    finally:
        database.dispose()

    # A schema-only or job-only DB contains no drafts, so seeding remains safe.
    manifest = seed_browser_canary(
        _database_url(database_path),
        canary_marker=CANARY_MARKER,
    )
    assert manifest["fixture_count"] == 95


def test_question_edit_carries_hint_ladder_forward_unapproved(tmp_path: Path) -> None:
    database_path = tmp_path / "canary.db"
    database_url = _database_url(database_path)
    manifest = seed_browser_canary(database_url, canary_marker=CANARY_MARKER)
    draft_id = manifest["drafts"][0]["draft_id"]

    database = init_database(database_url)
    repository = DraftRepository(database)
    try:
        before = repository.require_draft(draft_id)
        assert before.current_hint_ladder is not None
        approved = repository.review_hint_ladder(
            draft_id,
            reviewer="build08-reviewer",
            confirmed_rungs=["conceptual", "strategic", "specific"],
            approved=True,
            notes="Approved before question edit.",
        )
        assert approved.status == "approved"

        repository.edit_draft(
            draft_id,
            before.current,
            editor="build08-reviewer",
            notes="Exercise approval invalidation.",
        )
        after = repository.require_draft(draft_id)

        assert after.edit_count == 1
        assert after.current_hint_ladder is not None
        assert after.current_hint_ladder.edit_count == 1
        assert after.current_hint_ladder.status == "ready_for_review"
        assert set(after.current_hint_ladder.confirmations_json.values()) == {False}
        assert after.current_hint_ladder.ladder == approved.ladder
        assert repository.require_hint_ladder(approved.id).status == "approved"
    finally:
        database.dispose()
