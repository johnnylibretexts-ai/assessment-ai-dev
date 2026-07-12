from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapt import (
    AdaptAmbiguousError,
    AdaptClient,
    AdaptDestination,
    AdaptPublishingError,
    ResolvedAlignment,
    build_assessment_payload,
    build_external_engine_payload,
)
from app.catalog import SourceLicense, source_license
from app.config import Settings
from app.engines import EnginePublishingError, IMathASBridgeClient
from app.db import (
    Draft,
    DraftRepository,
    Publication,
    PublicationState,
    utc_now,
)
from app.qti import (
    QTI_EXPORTER_VERSION,
    QTIExportError,
    preflight_qti,
    write_qti_package,
)
from app.parameterized import compile_parameterized_item
from app.schemas import AssessmentItemType, QuestionDraft, ReviewStatus


PAYLOAD_MAPPER_VERSION = "adapt-assessment-items-v3"


class PublicationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class LicenseSelection:
    code: str
    version: str | None
    label: str
    evidence_url: str


class PublicationService:
    def __init__(
        self,
        settings: Settings,
        repository: DraftRepository,
        adapt: AdaptClient,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._adapt = adapt
        self._imathas = IMathASBridgeClient(settings)

    async def publish(
        self,
        draft_id: int,
        *,
        publisher: str,
        topic_stable_id: str,
        alignment_confirmed: bool,
        selected_license: LicenseSelection | None = None,
        license_confirmed: bool = False,
    ) -> Publication:
        if self._settings.adapt_publishing_status != "configured":
            raise PublicationValidationError(
                "ADAPT publishing is not configured for this service."
            )
        draft = self._repository.require_draft(draft_id)
        if draft.status is not ReviewStatus.READY_TO_PUBLISH:
            raise PublicationValidationError(
                "Approve the draft before publishing it to ADAPT."
            )
        if draft.current.item_type in {
            AssessmentItemType.WEBWORK,
            AssessmentItemType.IMATHAS,
        }:
            validation = draft.current_engine_validation
            if (
                validation is None
                or validation.status != "passed"
                or validation.seed_count < 25
            ):
                raise PublicationValidationError(
                    "The parameterized item needs a successful 25-seed engine validation."
                )
        hint_snapshot = self._approved_hint_snapshot(draft)
        if not alignment_confirmed:
            raise PublicationValidationError(
                "Confirm the curated framework topic before publishing."
            )
        license_selection = self._resolve_license(
            draft,
            selected=selected_license,
            manually_confirmed=license_confirmed,
        )
        alignment = await self._adapt.resolve_destination(
            license_code=license_selection.code,
            topic_stable_id=topic_stable_id,
        )
        title = self._title(draft)
        destination = AdaptDestination(
            folder_id=self._settings.adapt_folder_id,
            author=self._settings.adapt_author,
            license=license_selection.code,
            license_version=license_selection.version,
            public=self._settings.adapt_public,
        )
        key_material = self._key_material(
            draft,
            destination=destination,
            license_selection=license_selection,
            alignment=alignment,
            hint_snapshot=hint_snapshot,
        )
        publication_key = _sha256_json(key_material)
        tag = f"assessment-ai-{publication_key}"
        tags = ["assessment-ai", tag]
        if draft.current.set_key:
            tags.append(f"assessment-ai-set-{draft.current.set_key}")
        compiled = None
        if draft.current.item_type in {
            AssessmentItemType.WEBWORK,
            AssessmentItemType.IMATHAS,
        }:
            assert draft.current.response.parameterized is not None
            compiled = compile_parameterized_item(
                draft.current.response.parameterized, validation_seeds=25
            )
            payload_hash = _sha256_json(
                {
                    "engine": draft.current.item_type.value,
                    "source_sha256": compiled.source_sha256,
                    "destination": destination.model_dump(mode="json"),
                    "alignment": alignment.payload.model_dump(mode="json"),
                    "tags": tags,
                }
            )
        else:
            payload = build_assessment_payload(
                draft.current,
                destination=destination,
                source_url=draft.source.canonical_url,
                title=title,
                alignment=alignment.payload,
                tags=tags,
            )
            payload_hash = _sha256_json(payload)
        metadata = self._metadata(
            draft,
            license_selection=license_selection,
            alignment=alignment,
        )
        publication, reserved = self._repository.create_or_get_publication(
            {
                "draft_id": draft.id,
                "edit_count": draft.edit_count,
                "question_snapshot_json": draft.current.model_dump(mode="json"),
                "source_snapshot_json": self._source_snapshot(draft),
                "reviewer_identity": draft.last_reviewed_by or publisher,
                "approved_at": draft.last_reviewed_at or utc_now(),
                "destination_folder_id": destination.folder_id,
                "destination_folder_name": self._settings.adapt_folder_name,
                "author": destination.author,
                "public": destination.public,
                "license": license_selection.code,
                "license_version": license_selection.version,
                "license_label": license_selection.label,
                "license_evidence_url": license_selection.evidence_url,
                "framework_id": alignment.framework_id,
                "framework_title": alignment.framework_title,
                "alignment_json": alignment.model_dump(mode="json"),
                "stable_topic_ids_json": [
                    alignment.chapter_stable_id,
                    alignment.topic_stable_id,
                ],
                "hint_ladder_snapshot_json": hint_snapshot,
                "publication_key": publication_key,
                "payload_hash": payload_hash,
                "payload_mapper_version": PAYLOAD_MAPPER_VERSION,
                "qti_exporter_version": QTI_EXPORTER_VERSION,
                "state": PublicationState.PENDING.value,
            }
        )
        if (
            publication.draft_id != draft.id
            or publication.edit_count != draft.edit_count
        ):
            raise PublicationValidationError(
                "The publication key conflicts with a different draft revision."
            )
        if publication.state == PublicationState.SUCCEEDED.value:
            return publication
        if publication.state in {
            PublicationState.ADAPT_CREATED.value,
            PublicationState.HINTS_SYNCED.value,
        }:
            return await self._continue_after_adapt(
                publication, title=title, metadata=metadata
            )
        if publication.state == PublicationState.UNKNOWN.value:
            return await self._reconcile(
                publication, tag=tag, title=title, metadata=metadata
            )
        if not reserved and publication.state == PublicationState.PENDING.value:
            return publication

        try:
            preflight_qti(
                draft.current,
                publication_key=publication_key,
                title=title,
                metadata=metadata,
            )
        except QTIExportError as exc:
            failed = self._repository.update_publication(
                publication.id,
                state=PublicationState.FAILED,
                error_code="qti_preflight_failed",
                error_message=str(exc),
            )
            self._repository.record_publication_attempt(
                publication.id,
                action="qti_preflight",
                resulting_state=PublicationState.FAILED,
                error_code="qti_preflight_failed",
                error_message=str(exc),
            )
            return failed

        if compiled is not None:
            technology_id = None
            if draft.current.item_type == AssessmentItemType.IMATHAS:
                try:
                    engine_question = await self._imathas.create_question(
                        publication_key=publication_key,
                        description=title,
                        author=destination.author,
                        source=compiled.source,
                        source_url=draft.source.canonical_url,
                    )
                    technology_id = engine_question.question_id
                except EnginePublishingError as exc:
                    failed = self._repository.update_publication(
                        publication.id,
                        state=PublicationState.FAILED,
                        error_code=exc.code,
                        error_message=str(exc),
                    )
                    self._repository.record_publication_attempt(
                        publication.id,
                        action="imathas_create",
                        resulting_state=PublicationState.FAILED,
                        error_code=exc.code,
                        error_message=str(exc),
                    )
                    return failed
            payload = build_external_engine_payload(
                draft.current,
                destination=destination,
                source_url=draft.source.canonical_url,
                title=title,
                engine_source=compiled.source,
                technology_id=technology_id,
                alignment=alignment.payload,
                tags=tags,
                imathas_base_url=self._settings.imathas_base_url,
            )

        try:
            created = await self._adapt.create_question(payload)
        except AdaptAmbiguousError as exc:
            unknown = self._repository.update_publication(
                publication.id,
                state=PublicationState.UNKNOWN,
                error_code=exc.code,
                error_message=str(exc),
            )
            self._repository.record_publication_attempt(
                publication.id,
                action="adapt_create",
                resulting_state=PublicationState.UNKNOWN,
                error_code=exc.code,
                error_message=str(exc),
            )
            return unknown
        except AdaptPublishingError as exc:
            failed = self._repository.update_publication(
                publication.id,
                state=PublicationState.FAILED,
                error_code=exc.code,
                error_message=str(exc),
            )
            self._repository.record_publication_attempt(
                publication.id,
                action="adapt_create",
                resulting_state=PublicationState.FAILED,
                error_code=exc.code,
                error_message=str(exc),
            )
            return failed

        publication = self._repository.update_publication(
            publication.id,
            state=PublicationState.ADAPT_CREATED,
            adapt_question_id=created.question_id,
            adapt_page_id=created.page_id,
            error_code=None,
            error_message=None,
        )
        self._repository.record_publication_attempt(
            publication.id,
            action="adapt_create",
            resulting_state=PublicationState.ADAPT_CREATED,
            response={
                "question_id": created.question_id,
                "page_id": created.page_id,
            },
        )
        return await self._continue_after_adapt(
            publication, title=title, metadata=metadata
        )

    async def _reconcile(
        self,
        publication: Publication,
        *,
        tag: str,
        title: str,
        metadata: dict[str, Any],
    ) -> Publication:
        try:
            match = await self._adapt.find_question_by_tag(tag)
        except AdaptPublishingError as exc:
            self._repository.record_publication_attempt(
                publication.id,
                action="adapt_reconcile",
                resulting_state=PublicationState.UNKNOWN,
                error_code=exc.code,
                error_message=str(exc),
            )
            return self._repository.update_publication(
                publication.id,
                state=PublicationState.UNKNOWN,
                error_code=exc.code,
                error_message=str(exc),
            )
        if match is None:
            message = (
                "ADAPT has not exposed a question with this publication tag. "
                "No duplicate create request was sent."
            )
            self._repository.record_publication_attempt(
                publication.id,
                action="adapt_reconcile",
                resulting_state=PublicationState.UNKNOWN,
                error_code="adapt_not_yet_reconciled",
                error_message=message,
            )
            return self._repository.update_publication(
                publication.id,
                state=PublicationState.UNKNOWN,
                error_code="adapt_not_yet_reconciled",
                error_message=message,
            )
        reconciled = self._repository.update_publication(
            publication.id,
            state=PublicationState.ADAPT_CREATED,
            adapt_question_id=match.question_id,
            adapt_page_id=match.page_id,
            error_code=None,
            error_message=None,
        )
        self._repository.record_publication_attempt(
            publication.id,
            action="adapt_reconcile",
            resulting_state=PublicationState.ADAPT_CREATED,
            response={"question_id": match.question_id, "page_id": match.page_id},
        )
        return await self._continue_after_adapt(
            reconciled, title=title, metadata=metadata
        )

    async def _continue_after_adapt(
        self,
        publication: Publication,
        *,
        title: str,
        metadata: dict[str, Any],
    ) -> Publication:
        if publication.adapt_question_id is None:
            raise PublicationValidationError(
                "The ADAPT question ID is required before publication can continue."
            )
        if (
            publication.hint_ladder_snapshot_json is not None
            and publication.hints_synced_at is None
        ):
            try:
                await self._adapt.sync_hint_rungs(
                    publication.adapt_question_id,
                    {
                        "publication_key": publication.publication_key,
                        "concept_type": "question",
                        "concept_id": 0,
                        "ladder": publication.hint_ladder_snapshot_json,
                    },
                )
            except AdaptPublishingError as exc:
                self._repository.record_publication_attempt(
                    publication.id,
                    action="adapt_hint_sync",
                    resulting_state=PublicationState.ADAPT_CREATED,
                    error_code=exc.code,
                    error_message=str(exc),
                )
                return self._repository.update_publication(
                    publication.id,
                    state=PublicationState.ADAPT_CREATED,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            publication = self._repository.update_publication(
                publication.id,
                state=PublicationState.HINTS_SYNCED,
                hints_synced_at=utc_now(),
                error_code=None,
                error_message=None,
            )
            self._repository.record_publication_attempt(
                publication.id,
                action="adapt_hint_sync",
                resulting_state=PublicationState.HINTS_SYNCED,
                response={"synced": True},
            )
        return self._finalize_qti(publication, title=title, metadata=metadata)

    def _finalize_qti(
        self,
        publication: Publication,
        *,
        title: str,
        metadata: dict[str, Any],
    ) -> Publication:
        if publication.adapt_question_id is None:
            raise PublicationValidationError(
                "The ADAPT question ID is required before QTI finalization."
            )
        try:
            artifact = write_qti_package(
                QuestionDraft.model_validate(publication.question_snapshot_json),
                publication_key=publication.publication_key,
                title=title,
                metadata={
                    **metadata,
                    "adapt_question_id": publication.adapt_question_id,
                    "adapt_page_id": publication.adapt_page_id,
                },
                storage_dir=Path(self._settings.qti_storage_dir),
            )
        except (QTIExportError, OSError):
            message = "ADAPT created the question, but QTI finalization failed."
            retained_state = (
                PublicationState.HINTS_SYNCED
                if publication.state == PublicationState.HINTS_SYNCED.value
                else PublicationState.ADAPT_CREATED
            )
            self._repository.record_publication_attempt(
                publication.id,
                action="qti_finalize",
                resulting_state=retained_state,
                error_code="qti_finalize_failed",
                error_message=message,
            )
            return self._repository.update_publication(
                publication.id,
                state=retained_state,
                error_code="qti_finalize_failed",
                error_message=message,
            )
        succeeded = self._repository.update_publication(
            publication.id,
            state=PublicationState.SUCCEEDED,
            qti_path=str(artifact.path),
            qti_sha256=artifact.sha256,
            qti_size=artifact.size,
            finalized_at=utc_now(),
            error_code=None,
            error_message=None,
        )
        self._repository.record_publication_attempt(
            publication.id,
            action="qti_finalize",
            resulting_state=PublicationState.SUCCEEDED,
            response={"sha256": artifact.sha256, "size": artifact.size},
        )
        return succeeded

    @staticmethod
    def _resolve_license(
        draft: Draft,
        *,
        selected: LicenseSelection | None,
        manually_confirmed: bool,
    ) -> LicenseSelection:
        mapped: SourceLicense | None = source_license(draft.source.canonical_url)
        if mapped is not None:
            if selected is not None and (
                selected.code != mapped.code or selected.version != mapped.version
            ):
                raise PublicationValidationError(
                    "This source has a verified license mapping that cannot be overridden."
                )
            return LicenseSelection(
                code=mapped.code,
                version=mapped.version,
                label=mapped.label,
                evidence_url=mapped.evidence_url,
            )
        if selected is None or not manually_confirmed:
            raise PublicationValidationError(
                "Select and confirm a source license before publishing this unmapped page."
            )
        if not selected.code or not selected.label or not selected.evidence_url:
            raise PublicationValidationError(
                "Manual license selections require a code, label, and evidence URL."
            )
        return selected

    def _key_material(
        self,
        draft: Draft,
        *,
        destination: AdaptDestination,
        license_selection: LicenseSelection,
        alignment: ResolvedAlignment,
        hint_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "question": draft.current.model_dump(mode="json"),
            "source_content_hash": draft.source.content_hash,
            "edit_count": draft.edit_count,
            "destination": {
                "folder_id": destination.folder_id,
                "folder_name": self._settings.adapt_folder_name,
                "author": destination.author,
                "public": destination.public,
            },
            "license": {
                "code": license_selection.code,
                "version": license_selection.version,
            },
            "alignment": {
                "framework_title": alignment.framework_title,
                "chapter_stable_id": alignment.chapter_stable_id,
                "topic_stable_id": alignment.topic_stable_id,
            },
            "hint_ladder": hint_snapshot,
            "payload_mapper_version": PAYLOAD_MAPPER_VERSION,
            "qti_exporter_version": QTI_EXPORTER_VERSION,
        }

    @staticmethod
    def _source_snapshot(draft: Draft) -> dict[str, Any]:
        return {
            "id": draft.source.id,
            "canonical_path": draft.source.canonical_path,
            "canonical_url": draft.source.canonical_url,
            "page_id": draft.source.page_id,
            "title": draft.source.title,
            "content_hash": draft.source.content_hash,
            "html_body": draft.source.html_body,
            "plaintext": draft.source.plaintext,
            "paragraphs": draft.source.paragraphs_json,
        }

    @staticmethod
    def _title(draft: Draft) -> str:
        return f"{draft.source.title} — {draft.current.concept_label}"[:1_000]

    @staticmethod
    def _metadata(
        draft: Draft,
        *,
        license_selection: LicenseSelection,
        alignment: ResolvedAlignment,
    ) -> dict[str, Any]:
        cited = {
            int(item["index"]): item["text"] for item in draft.source.paragraphs_json
        }
        revision = next(
            (
                call
                for call in sorted(
                    draft.llm_calls, key=lambda item: item.id, reverse=True
                )
                if call.stage == "revision"
            ),
            None,
        )
        return {
            "bloom": draft.current.bloom.value,
            "difficulty": draft.current.difficulty.value,
            "license": license_selection.label,
            "license_code": license_selection.code,
            "license_version": license_selection.version,
            "canonical_source": draft.source.canonical_url,
            "cited_paragraphs": [
                {"index": index, "text": cited[index]}
                for index in draft.current.citation_paragraphs
                if index in cited
            ],
            "model_id": revision.model_id if revision else "unknown model",
            "prompt_version": (
                revision.prompt_version if revision else draft.tool_version
            ),
            "reviewer": draft.last_reviewed_by,
            "reviewed_at": _iso(draft.last_reviewed_at),
            "framework_id": alignment.framework_id,
            "framework_title": alignment.framework_title,
            "chapter": alignment.chapter.text,
            "topic": alignment.topic.text,
            "stable_topic_id": alignment.topic_stable_id,
            "hint_ladder": (
                draft.current_hint_ladder.ladder.model_dump(mode="json")
                if draft.current_hint_ladder is not None
                else None
            ),
        }

    def _approved_hint_snapshot(self, draft: Draft) -> dict[str, Any] | None:
        if not self._settings.hint_generation_enabled:
            return None
        record = draft.current_hint_ladder
        if record is None:
            raise PublicationValidationError(
                "Generate and approve the three-rung hint ladder before publishing."
            )
        if record.status != "approved":
            raise PublicationValidationError(
                "Approve all three hint rungs before publishing."
            )
        return {
            "version": record.version,
            "rungs": record.ladder.model_dump(mode="json")["rungs"],
            "reviewed_by": record.reviewed_by,
            "reviewed_at": _iso(record.reviewed_at),
        }


def _sha256_json(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
