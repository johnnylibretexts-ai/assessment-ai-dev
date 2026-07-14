from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy.engine import make_url

from app.adapt import AdaptAmbiguousError, AdaptClient, AdaptPublishingError
from app.catalog import suggested_topic
from app.config import Settings
from app.db import DraftRepository, DraftWrite, PublicationState, init_database
from app.parameterized import compile_parameterized_item
from app.pipeline import ReviewService
from app.publishing import PublicationService
from app.qti import validate_qti_xml
from app.schemas import (
    AssessmentItemType,
    Concept,
    Critique,
    ItemContextType,
    NormalizedPage,
    Paragraph,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
    SourceInfo,
)

from .fixtures import build_draft, build_hint_ladder


CANARY_MARKER = "build08-assessment-publication-canary"
PIPELINE_VERSION = "build08-publication-fixtures-v1"
RUN_ID = "b8000000-0000-4000-8000-000000000120"
SOURCE_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
    "01%3A_Matter_and_Measurements/1.11%3A_Temperature_Heat_and_Energy"
)
REVIEWER = "build08-publication-reviewer"
CONFIRMED_RUNGS = ["conceptual", "strategic", "specific"]


class PublicationCanaryError(ValueError):
    """Raised when the isolated publication fixture or evidence is invalid."""


def seed_publication_canary(
    database_url: str,
    *,
    canary_marker: str,
) -> dict[str, Any]:
    """Seed and approve 19 publication fixtures plus one recovery probe."""

    _require_marker(canary_marker)
    database_path = _sqlite_path(database_url)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    drafts = [
        build_draft(item_type, ItemContextType.STANDARD)
        for item_type in AssessmentItemType
    ]
    recovery = build_draft(
        AssessmentItemType.MULTIPLE_CHOICE,
        ItemContextType.STANDARD,
    ).model_copy(
        update={
            "stem": (
                "Use the cited source to identify the relationship in the "
                "reconciliation recovery probe."
            )
        }
    )
    all_drafts = [*drafts, recovery]
    fixture_sha256 = _sha256_json(
        [draft.model_dump(mode="json") for draft in all_drafts]
    )

    database = init_database(database_url)
    repository = DraftRepository(database)
    try:
        existing = repository.list_drafts(current_sources_only=False)
        if not existing:
            page = _source_page()
            writes = [
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
                    hint_ladder=build_hint_ladder(),
                    engine_validation=_engine_validation(draft),
                )
                for position, draft in enumerate(all_drafts)
            ]
            stored = repository.replace_generated_drafts(
                page=page,
                pipeline_version=PIPELINE_VERSION,
                drafts=writes,
                llm_calls=[],
                content_hash=fixture_sha256,
                run_id=RUN_ID,
            )
            if len(stored.draft_ids) != 20:
                raise PublicationCanaryError(
                    "publication canary did not persist exactly 20 drafts"
                )

        ordered = _validate_fixture_drafts(
            repository,
            expected=all_drafts,
            fixture_sha256=fixture_sha256,
        )
        review = ReviewService(repository)
        for draft in ordered:
            hint = draft.current_hint_ladder
            if hint is None:
                raise PublicationCanaryError("publication fixture lacks a hint ladder")
            if hint.status != "approved":
                repository.review_hint_ladder(
                    draft.id,
                    reviewer=REVIEWER,
                    confirmed_rungs=CONFIRMED_RUNGS,
                    approved=True,
                    notes="Sealed BUILD-08 publication fixture approval.",
                )
            if draft.status is not ReviewStatus.READY_TO_PUBLISH:
                review.decide(
                    draft.id,
                    ReviewDecision(
                        status=ReviewStatus.READY_TO_PUBLISH,
                        bloom_confirmed=True,
                        difficulty_confirmed=True,
                        reviewer_notes=(
                            "Sealed BUILD-08 publication fixture approval."
                        ),
                    ),
                    reviewer=REVIEWER,
                )

        ordered = _validate_fixture_drafts(
            repository,
            expected=all_drafts,
            fixture_sha256=fixture_sha256,
            require_approved=True,
        )
        topic = suggested_topic(SOURCE_URL)
        if topic is None:
            raise PublicationCanaryError("publication source lacks a curated topic")
        records = [
            {
                "fixture_id": f"build08-publication-{draft.current.item_type.value}",
                "draft_id": draft.id,
                "item_type": draft.current.item_type.value,
                "path": f"/drafts/{draft.id}",
                "publication_count": len(
                    repository.require_draft(draft.id).publications
                ),
            }
            for draft in ordered[: len(AssessmentItemType)]
        ]
        recovery_draft = ordered[-1]
        return {
            "schema_version": "build08-publication-canary-v1",
            "canary_marker": CANARY_MARKER,
            "pipeline_version": PIPELINE_VERSION,
            "fixture_sha256": fixture_sha256,
            "database_path": str(database_path),
            "topic_stable_id": topic.stable_id,
            "fixture_count": len(records),
            "item_type_count": len({record["item_type"] for record in records}),
            "drafts": records,
            "recovery_probe": {
                "fixture_id": "build08-publication-reconciliation-probe",
                "draft_id": recovery_draft.id,
                "item_type": recovery_draft.current.item_type.value,
                "path": f"/drafts/{recovery_draft.id}",
                "publication_count": len(
                    repository.require_draft(recovery_draft.id).publications
                ),
            },
        }
    finally:
        database.dispose()


async def run_publication_recovery_probe(
    database_url: str,
    *,
    canary_marker: str,
    settings: Settings,
) -> dict[str, Any]:
    """Prove ownership fail-closed and real ADAPT reconciliation without a duplicate."""

    _require_marker(canary_marker)
    manifest = seed_publication_canary(
        database_url,
        canary_marker=canary_marker,
    )
    draft_id = int(manifest["recovery_probe"]["draft_id"])
    topic_stable_id = str(manifest["topic_stable_id"])
    database = init_database(database_url)
    repository = DraftRepository(database)
    try:
        draft = repository.require_draft(draft_id)
        if draft.publications:
            raise PublicationCanaryError(
                "recovery probe is one-shot and already has publication history"
            )

        wrong_settings = settings.model_copy(
            update={"adapt_folder_id": int(settings.adapt_folder_id or 0) + 100_000}
        )
        wrong_adapt = AdaptClient(wrong_settings)
        try:
            try:
                await PublicationService(
                    wrong_settings,
                    repository,
                    wrong_adapt,
                ).publish(
                    draft_id,
                    publisher=REVIEWER,
                    topic_stable_id=topic_stable_id,
                    alignment_confirmed=True,
                )
            except AdaptPublishingError as exc:
                ownership_code = exc.code
            else:  # pragma: no cover - live probe must fail closed
                raise PublicationCanaryError(
                    "unowned ADAPT destination unexpectedly accepted publication"
                )
        finally:
            await wrong_adapt.aclose()
        if ownership_code != "adapt_folder_mismatch":
            raise PublicationCanaryError(
                f"ownership probe returned unexpected code {ownership_code!r}"
            )
        if repository.require_draft(draft_id).publications:
            raise PublicationCanaryError(
                "ownership denial created partial publication history"
            )

        real_adapt = AdaptClient(settings)
        dropping = _DropAfterCreateAdapt(real_adapt)
        try:
            service = PublicationService(settings, repository, dropping)
            arguments = {
                "publisher": REVIEWER,
                "topic_stable_id": topic_stable_id,
                "alignment_confirmed": True,
            }
            unknown = await service.publish(draft_id, **arguments)
            if (
                unknown.state != PublicationState.UNKNOWN.value
                or unknown.adapt_question_id is not None
                or unknown.qti_path is not None
            ):
                raise PublicationCanaryError(
                    "ambiguous create did not retain the expected no-partial local state"
                )
            recovered = await service.publish(draft_id, **arguments)
            repeated = await service.publish(draft_id, **arguments)
            if recovered.state != PublicationState.SUCCEEDED.value:
                raise PublicationCanaryError("ambiguous create did not reconcile")
            if repeated.id != recovered.id or dropping.create_calls != 1:
                raise PublicationCanaryError(
                    "reconciled publication was not idempotent"
                )
            tag = f"assessment-ai-{recovered.publication_key}"
            match = await real_adapt.find_question_by_tag(tag)
            if match is None or match.question_id != recovered.adapt_question_id:
                raise PublicationCanaryError(
                    "reconciled ADAPT question does not match the publication ledger"
                )
        finally:
            await real_adapt.aclose()

        persisted = repository.require_publication(recovered.id)
        actions = [attempt.action for attempt in persisted.attempts]
        if actions != [
            "adapt_create",
            "adapt_reconcile",
            "adapt_hint_sync",
            "qti_finalize",
        ]:
            raise PublicationCanaryError(
                f"unexpected recovery attempt sequence: {actions!r}"
            )
        return {
            "schema_version": "build08-publication-recovery-v1",
            "canary_marker": CANARY_MARKER,
            "ownership_denial": {
                "error_code": ownership_code,
                "publication_rows_after_denial": 0,
            },
            "ambiguous_create": {
                "initial_state": PublicationState.UNKNOWN.value,
                "create_calls": dropping.create_calls,
                "final_state": persisted.state,
                "publication_id": persisted.id,
                "adapt_question_id": persisted.adapt_question_id,
                "qti_sha256": persisted.qti_sha256,
                "attempt_actions": actions,
            },
            "passed": True,
        }
    finally:
        database.dispose()


async def validate_publication_canary(
    database_url: str,
    *,
    canary_marker: str,
    settings: Settings,
) -> dict[str, Any]:
    """Validate all 19 live publications, QTI packages, ownership, and idempotency."""

    _require_marker(canary_marker)
    manifest = seed_publication_canary(database_url, canary_marker=canary_marker)
    database = init_database(database_url)
    repository = DraftRepository(database)
    adapt = AdaptClient(settings)
    try:
        owned = await adapt.list_owned_questions()
        records: list[dict[str, Any]] = []
        seen_question_ids: set[int] = set()
        seen_publication_ids: set[int] = set()
        for fixture in manifest["drafts"]:
            draft = repository.require_draft(int(fixture["draft_id"]))
            if len(draft.publications) != 1:
                raise PublicationCanaryError(
                    f"{draft.current.item_type.value} requires exactly one publication"
                )
            publication = draft.publications[0]
            if publication.state != PublicationState.SUCCEEDED.value:
                raise PublicationCanaryError(
                    f"{draft.current.item_type.value} publication did not succeed"
                )
            if (
                publication.id in seen_publication_ids
                or publication.adapt_question_id is None
                or publication.adapt_question_id in seen_question_ids
            ):
                raise PublicationCanaryError("publication identifiers are not unique")
            seen_publication_ids.add(publication.id)
            seen_question_ids.add(publication.adapt_question_id)
            qti_path = Path(str(publication.qti_path))
            qti_sha256 = _validate_qti_package(qti_path)
            if qti_sha256 != publication.qti_sha256:
                raise PublicationCanaryError(
                    "stored QTI digest does not match the package"
                )

            tag = f"assessment-ai-{publication.publication_key}"
            matches = [item for item in owned if tag in item.get("tags", [])]
            if len(matches) != 1:
                raise PublicationCanaryError(
                    f"{draft.current.item_type.value} does not have one owned ADAPT match"
                )
            question = matches[0]
            if int(question["id"]) != publication.adapt_question_id:
                raise PublicationCanaryError("ADAPT and publication IDs disagree")
            expected_technology = (
                draft.current.item_type.value
                if draft.current.item_type
                in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}
                else "qti"
            )
            if question.get("technology") != expected_technology:
                raise PublicationCanaryError(
                    f"{draft.current.item_type.value} has the wrong ADAPT technology"
                )
            if int(question.get("folder_id", 0)) != int(settings.adapt_folder_id or 0):
                raise PublicationCanaryError("ADAPT question escaped the owned folder")
            records.append(
                {
                    "fixture_id": fixture["fixture_id"],
                    "item_type": draft.current.item_type.value,
                    "publication_id": publication.id,
                    "adapt_question_id": publication.adapt_question_id,
                    "technology": expected_technology,
                    "qti_sha256": qti_sha256,
                    "attempt_actions": [
                        attempt.action for attempt in publication.attempts
                    ],
                }
            )
        if len(records) != 19 or len(seen_question_ids) != 19:
            raise PublicationCanaryError(
                "publication validation did not cover 19 types"
            )
        return {
            "schema_version": "build08-publication-validation-v1",
            "canary_marker": CANARY_MARKER,
            "fixture_sha256": manifest["fixture_sha256"],
            "fixture_count": len(records),
            "item_type_count": len({record["item_type"] for record in records}),
            "owned_adapt_question_count": len(seen_question_ids),
            "qti_package_count": len(records),
            "records": records,
            "passed": True,
        }
    finally:
        await adapt.aclose()
        database.dispose()


class _DropAfterCreateAdapt:
    def __init__(self, delegate: AdaptClient) -> None:
        self._delegate = delegate
        self.create_calls = 0

    async def resolve_destination(self, **kwargs: Any) -> Any:
        return await self._delegate.resolve_destination(**kwargs)

    async def create_question(self, payload: dict[str, Any]) -> Any:
        self.create_calls += 1
        await self._delegate.create_question(payload)
        raise AdaptAmbiguousError(
            "The qualification probe intentionally discarded the create response.",
            code="adapt_qualification_response_drop",
        )

    async def find_question_by_tag(self, tag: str) -> Any:
        return await self._delegate.find_question_by_tag(tag)

    async def sync_hint_rungs(self, question_id: int, payload: dict[str, Any]) -> None:
        await self._delegate.sync_hint_rungs(question_id, payload)


def _source_page() -> NormalizedPage:
    text = (
        "A sealed BUILD-08 source states that total energy is conserved when "
        "energy changes form."
    )
    parsed = urlsplit(SOURCE_URL)
    return NormalizedPage(
        title="1.11: Temperature, Heat, and Energy",
        plaintext=text,
        htmlBody=f"<p>{text}</p>",
        paragraphs=[Paragraph(index=0, text=text, start=0, end=len(text))],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=SOURCE_URL,
            path=f"{parsed.netloc}{unquote(parsed.path)}",
            page_id="build08-publication-fixture",
        ),
    )


def _engine_validation(draft: QuestionDraft) -> dict[str, Any] | None:
    if draft.response.parameterized is None:
        return None
    compiled = compile_parameterized_item(
        draft.response.parameterized,
        validation_seeds=100,
    )
    return {
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


def _validate_fixture_drafts(
    repository: DraftRepository,
    *,
    expected: list[QuestionDraft],
    fixture_sha256: str,
    require_approved: bool = False,
) -> list[Any]:
    ordered = sorted(
        repository.list_drafts(current_sources_only=False),
        key=lambda draft: (draft.position, draft.id),
    )
    if len(ordered) != len(expected):
        raise PublicationCanaryError(
            "an existing database is not the exact publication canary"
        )
    for position, (stored, expected_draft) in enumerate(
        zip(ordered, expected, strict=True)
    ):
        if (
            stored.position != position
            or stored.source.pipeline_version != PIPELINE_VERSION
            or stored.source.content_hash != fixture_sha256
            or stored.current.model_dump(mode="json")
            != expected_draft.model_dump(mode="json")
        ):
            raise PublicationCanaryError(
                "an existing database does not match the sealed publication fixtures"
            )
        if stored.current_hint_ladder is None:
            raise PublicationCanaryError("publication fixture lacks a hint ladder")
        if stored.current.response.parameterized is not None:
            validation = stored.current_engine_validation
            if validation is None or validation.seed_count != 100:
                raise PublicationCanaryError(
                    "a parameterized publication fixture lacks 100-seed validation"
                )
        if require_approved and (
            stored.status is not ReviewStatus.READY_TO_PUBLISH
            or stored.current_hint_ladder.status != "approved"
            or not stored.bloom_confirmed
            or not stored.difficulty_confirmed
        ):
            raise PublicationCanaryError("publication fixture is not fully approved")
    return ordered


def _validate_qti_package(path: Path) -> str:
    if not path.is_file():
        raise PublicationCanaryError("QTI package is missing")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if (
                len(names) != 2
                or "imsmanifest.xml" not in names
                or any(
                    name.startswith(("/", "../")) or "/../" in name for name in names
                )
            ):
                raise PublicationCanaryError("QTI package has unsafe contents")
            item_name = next(name for name in names if name != "imsmanifest.xml")
            validate_qti_xml(
                archive.read(item_name),
                archive.read("imsmanifest.xml"),
            )
    except zipfile.BadZipFile as exc:
        raise PublicationCanaryError("QTI package is not a valid ZIP") from exc
    return digest


def _sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise PublicationCanaryError(
            "publication canaries require a file-backed SQLite URL"
        )
    path = Path(url.database)
    if not path.is_absolute():
        raise PublicationCanaryError("publication canary SQLite paths must be absolute")
    return path.resolve()


def _require_marker(value: str) -> None:
    if value != CANARY_MARKER:
        raise PublicationCanaryError(
            "the exact BUILD-08 publication-canary marker is required"
        )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
