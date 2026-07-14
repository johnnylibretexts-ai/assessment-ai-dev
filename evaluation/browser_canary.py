from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from app.db import DraftRepository, DraftWrite, init_database
from app.parameterized import compile_parameterized_item
from app.schemas import Concept, Critique, NormalizedPage, Paragraph, SourceInfo

from .fixtures import build_fixture_bundle


CANARY_MARKER = "build08-assessment-browser-canary"
PIPELINE_VERSION = "build08-browser-fixtures-v1"
RUN_ID = "b8000000-0000-4000-8000-000000000095"
SOURCE_PATH = "chem.libretexts.org/Bookshelves/Build08/Sealed_Browser_Fixture"


class BrowserCanaryError(ValueError):
    """Raised when the browser canary cannot be seeded safely."""


def seed_browser_canary(
    database_url: str,
    *,
    canary_marker: str,
) -> dict[str, Any]:
    """Seed the sealed 19-by-5 fixture matrix into a disposable SQLite DB."""

    if canary_marker != CANARY_MARKER:
        raise BrowserCanaryError("the exact BUILD-08 browser-canary marker is required")
    database_path = _sqlite_path(database_url)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    bundle = build_fixture_bundle()
    fixture_payload = bundle.model_dump(mode="json")
    fixture_sha256 = hashlib.sha256(
        json.dumps(
            fixture_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    database = init_database(database_url)
    repository = DraftRepository(database)
    try:
        existing = repository.list_drafts(current_sources_only=False)
        if existing:
            return _validate_existing_canary(
                existing,
                database_path=database_path,
                fixture_sha256=fixture_sha256,
            )

        source_text = (
            "A sealed BUILD-08 source states that total energy is conserved when "
            "energy changes form."
        )
        page = NormalizedPage(
            title="BUILD-08 sealed browser fixture",
            plaintext=source_text,
            htmlBody=f"<p>{source_text}</p>",
            paragraphs=[
                Paragraph(index=0, text=source_text, start=0, end=len(source_text))
            ],
            source=SourceInfo(
                backend="libretexts_public",
                canonical_url=f"https://{SOURCE_PATH}",
                path=SOURCE_PATH,
                page_id="build08-browser-fixture",
            ),
        )
        writes: list[DraftWrite] = []
        for position, case in enumerate(bundle.cases):
            draft = case.draft
            engine_validation = None
            if draft.response.parameterized is not None:
                compiled = compile_parameterized_item(
                    draft.response.parameterized,
                    validation_seeds=100,
                )
                engine_validation = {
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
                }
            writes.append(
                DraftWrite(
                    position=position,
                    concept=Concept(
                        label=draft.concept_label,
                        description=(
                            "Total energy remains constant while its form changes."
                        ),
                        source_paragraphs=[0],
                    ),
                    raw=draft,
                    critique=Critique(
                        issues=[],
                        distractor_flags=[],
                        revision_instructions=[],
                        revision_required=False,
                    ),
                    revised=draft,
                    hint_ladder=case.hint_ladder,
                    engine_validation=engine_validation,
                )
            )

        stored = repository.replace_generated_drafts(
            page=page,
            pipeline_version=PIPELINE_VERSION,
            drafts=writes,
            llm_calls=[],
            content_hash=fixture_sha256,
            run_id=RUN_ID,
        )
        if len(stored.draft_ids) != 95:
            raise BrowserCanaryError("canary seed did not persist exactly 95 drafts")
        seeded = repository.list_drafts(current_sources_only=False)
        return _validate_existing_canary(
            seeded,
            database_path=database_path,
            fixture_sha256=fixture_sha256,
        )
    finally:
        database.dispose()


def _validate_existing_canary(
    drafts: list[Any],
    *,
    database_path: Path,
    fixture_sha256: str,
) -> dict[str, Any]:
    bundle = build_fixture_bundle()
    ordered = sorted(drafts, key=lambda draft: (draft.position, draft.id))
    if len(ordered) != len(bundle.cases):
        raise BrowserCanaryError(
            "an existing database is not the exact 95-draft browser canary"
        )

    records: list[dict[str, Any]] = []
    for position, (stored, case) in enumerate(zip(ordered, bundle.cases, strict=True)):
        current = stored.current
        if (
            stored.position != position
            or stored.source.canonical_path != SOURCE_PATH
            or stored.source.pipeline_version != PIPELINE_VERSION
            or stored.source.content_hash != fixture_sha256
            or current.item_type != case.draft.item_type
            or current.context_type != case.draft.context_type
            or stored.current_hint_ladder is None
        ):
            raise BrowserCanaryError(
                "an existing database does not match the sealed browser fixture matrix"
            )
        if current.response.parameterized is not None:
            validation = stored.current_engine_validation
            if validation is None or validation.seed_count != 100:
                raise BrowserCanaryError(
                    "a parameterized browser fixture lacks its 100-seed validation"
                )
        records.append(
            {
                "fixture_id": case.fixture_id,
                "draft_id": stored.id,
                "item_type": current.item_type.value,
                "context_type": current.context_type.value,
                "path": f"/drafts/{stored.id}",
            }
        )

    return {
        "schema_version": "build08-browser-canary-v1",
        "canary_marker": CANARY_MARKER,
        "pipeline_version": PIPELINE_VERSION,
        "fixture_sha256": fixture_sha256,
        "database_path": str(database_path),
        "fixture_count": len(records),
        "item_type_count": len({record["item_type"] for record in records}),
        "context_type_count": len({record["context_type"] for record in records}),
        "drafts": records,
    }


def _sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise BrowserCanaryError("browser canaries require a file-backed SQLite URL")
    path = Path(url.database)
    if not path.is_absolute():
        raise BrowserCanaryError("browser canary SQLite paths must be absolute")
    return path.resolve()
