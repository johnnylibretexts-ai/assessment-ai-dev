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
from app.catalog import (
    CuratedAlignment,
    SourceLicense,
    alignment_for_source,
    source_license,
)
from app.computation_policy import ComputationGateDecision, evaluate_computation_gate
from app.computation import AssessmentComputationBlueprint, deterministic_seeds
from app.computation_workflow import (
    parameterized_spec_from_blueprint,
    parameterized_typed_inputs,
)
from app.config import Settings
from app.db import (
    ComputationEvidenceError,
    Draft,
    DraftRepository,
    EngineValidationBinding,
    HintLadderBinding,
    Publication,
    PublicationComputationEvidenceWrite,
    PublicationState,
    ReviewTransitionError,
    approved_hint_ladder_snapshot,
    draft_content_sha256,
    hint_ladder_evidence_sha256,
    inspect_hint_grounding,
    utc_now,
    validate_computation_evidence,
    validate_computation_engine_binding,
)
from app.engines import EnginePublishingError, IMathASBridgeClient
from app.qti import (
    QTI_EXPORTER_VERSION,
    QTIExportError,
    preflight_qti,
    write_qti_package,
)
from app.parameterized import (
    CompiledParameterizedItem,
    ParameterizedCompileError,
    compile_parameterized_item,
    compile_typed_parameterized_item,
)
from app.publication_attempts import (
    ADAPT_HINT_SYNC,
    StepFailed,
    StepOutcome,
    StepSucceeded,
    run_publication_step,
)
from app.publication_preconditions import (
    PublicationBlocker,
    PublicationContext,
    collect_precondition_blockers,
)
from app.schemas import AssessmentItemType, QuestionDraft


PAYLOAD_MAPPER_VERSION = "adapt-assessment-items-v3"


class PublicationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PublicationReadiness:
    alignment: CuratedAlignment | None
    blockers: tuple[PublicationBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def model_dump(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "alignment": (
                {
                    "framework_title": self.alignment.framework.title,
                    "framework_source_url": self.alignment.framework.source_url,
                    "topic_stable_id": self.alignment.topic.stable_id,
                    "topic_title": self.alignment.topic.title,
                    "chapter_title": self.alignment.topic.chapter_title,
                }
                if self.alignment is not None
                else None
            ),
            "blockers": [
                {"code": blocker.code, "message": blocker.message}
                for blocker in self.blockers
            ],
        }


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

    def readiness(
        self,
        draft_id: int,
        *,
        license_resolved: bool,
    ) -> PublicationReadiness:
        draft = self._repository.require_draft(draft_id)
        return self._local_readiness(
            draft,
            license_resolved=license_resolved,
        )

    def _local_readiness(
        self,
        draft: Draft,
        *,
        license_resolved: bool,
    ) -> PublicationReadiness:
        readiness, _ = self._evaluate_readiness(
            draft,
            license_resolved=license_resolved,
        )
        return readiness

    def _assemble_context(
        self,
        draft: Draft,
        *,
        license_resolved: bool,
    ) -> PublicationContext:
        """Gather what every publication precondition needs, exactly once.

        Preconditions judge; they do not fetch. The computation gate's five
        repository reads happen here, so evaluating the list issues no query of
        its own.

        `evaluate_computation_gate`, not `require_computation_gate`: the reads
        belong here and the raise belongs to `ComputationEvidenceAccepted`. An
        unallowed decision is data, and turning it into a blocker is the
        precondition's job -- catching it here would split one condition across
        two modules.

        Deliberately not substitutable: there is no assembler interface and no
        fake. A precondition test constructs `PublicationContext` directly.
        """

        return PublicationContext(
            draft=draft,
            settings=self._settings,
            hint_record=draft.current_hint_ladder,
            engine_validation=draft.current_engine_validation,
            computation_decision=evaluate_computation_gate(
                self._settings,
                self._repository,
                draft,
            ),
            alignment=alignment_for_source(draft.source.canonical_url),
            license_resolved=license_resolved,
        )

    def _evaluate_readiness(
        self,
        draft: Draft,
        *,
        license_resolved: bool,
    ) -> tuple[PublicationReadiness, ComputationGateDecision]:
        """Collect every blocker, and return the gate decision alongside them.

        `publish()` needs the decision itself, not just "did the gate block".
        Assembling the context once per evaluation keeps the gate evaluated
        exactly once per publish attempt instead of running its five repository
        reads twice.

        The decision is always present now, including when it blocks -- readiness
        having no blockers is what tells `publish()` it was allowed.
        """

        context = self._assemble_context(draft, license_resolved=license_resolved)
        return (
            PublicationReadiness(
                alignment=context.alignment,
                blockers=collect_precondition_blockers(context),
            ),
            context.computation_decision,
        )

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
        draft = self._repository.require_draft(draft_id)
        # Readiness first: a reviewer with several blockers should be told to
        # approve the revision before being told to pick a license, which is
        # the order the readiness panel already shows them in.
        readiness, computation_decision = self._evaluate_readiness(
            draft,
            license_resolved=True,
        )
        if readiness.blockers:
            raise PublicationValidationError(readiness.blockers[0].message)
        license_selection = self._resolve_license(
            draft,
            selected=selected_license,
            manually_confirmed=license_confirmed,
        )
        if readiness.alignment is None:
            # Not an assert: `python -O` strips those, which would turn this
            # guard into an AttributeError on the publication path.
            raise PublicationValidationError(
                "This source does not yet have a curated framework topic."
            )
        expected_alignment = readiness.alignment
        if topic_stable_id != expected_alignment.topic.stable_id:
            raise PublicationValidationError(
                "The selected framework topic does not match this source."
            )
        if not computation_decision.allowed:
            # Unreachable while `ComputationEvidenceAccepted` turns every
            # unallowed decision into a blocker above, but not an assert:
            # `python -O` strips those, and publishing past a blocking
            # computation decision is exactly the corrupt record the
            # precondition list exists to prevent. The wording matches
            # `require_allowed` so the two cannot drift apart.
            raise PublicationValidationError(
                f"Computation evidence blocks publication: "
                f"{computation_decision.message}"
            )
        compiled = None
        engine_binding = None
        if computation_decision.mode == "enforce":
            compiled, engine_binding = self._verified_external_compilation(
                draft,
                computation_decision=computation_decision,
            )
        hint_snapshot, hint_binding = self._approved_hint_snapshot(
            draft,
            bind_evidence=computation_decision.mode == "enforce",
        )
        if not alignment_confirmed:
            raise PublicationValidationError(
                "Confirm the curated framework topic before publishing."
            )
        alignment = await self._adapt.resolve_destination(
            license_code=license_selection.code,
            topic_stable_id=expected_alignment.topic.stable_id,
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
            computation_decision=computation_decision,
            engine_binding=engine_binding,
        )
        publication_key = _sha256_json(key_material)
        tag = f"assessment-ai-{publication_key}"
        tags = ["assessment-ai", tag]
        if draft.current.set_key:
            tags.append(f"assessment-ai-set-{draft.current.set_key}")
        if computation_decision.mode != "enforce":
            # Preserve the accepted BUILD-08 ordering and gate surface exactly:
            # the existing engine receipt was checked above, lookup has already
            # completed, and publication does not bind new computation evidence.
            compiled, engine_binding = self._verified_external_compilation(
                draft,
                computation_decision=computation_decision,
            )
        if compiled is not None:
            engine_payload_identity = {
                "engine": compiled.engine,
                "source_sha256": compiled.source_sha256,
                "destination": destination.model_dump(mode="json"),
                "alignment": alignment.payload.model_dump(mode="json"),
                "tags": tags,
            }
            if computation_decision.enforced:
                engine_payload_identity["compiler_version"] = compiled.compiler_version
            payload_hash = _sha256_json(engine_payload_identity)
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
            computation_decision=computation_decision,
            engine_binding=engine_binding,
        )
        publication_values = {
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
        try:
            computation_evidence = (
                PublicationComputationEvidenceWrite(
                    report_sha256=computation_decision.report_sha256,
                    attestation_sha256s=(computation_decision.attestation_sha256s),
                    engine_validation=engine_binding,
                )
                if computation_decision.mode == "enforce"
                and computation_decision.scoped
                and computation_decision.report_sha256 is not None
                else None
            )
            if computation_decision.mode == "enforce":
                publication, reserved = (
                    self._repository.create_or_get_publication_guarded(
                        publication_values,
                        computation_binding=computation_decision.atomic_binding(
                            self._settings
                        ),
                        computation_evidence=computation_evidence,
                        engine_binding=engine_binding,
                        hint_binding=hint_binding,
                    )
                )
            else:
                publication, reserved = self._repository.create_or_get_publication(
                    publication_values
                )
        except (ComputationEvidenceError, ReviewTransitionError) as exc:
            raise PublicationValidationError(
                "The approved draft or its computation evidence changed before "
                "publication reservation; no external create request was sent."
            ) from exc
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
            question_id = publication.adapt_question_id
            payload = {
                "publication_key": publication.publication_key,
                "concept_type": "question",
                "concept_id": 0,
                "ladder": publication.hint_ladder_snapshot_json,
            }

            async def sync_hint_rungs() -> StepOutcome:
                """The step body: the ADAPT call, and nothing but.

                What it reports is what happened at ADAPT. Where that leaves the
                publication is `ADAPT_HINT_SYNC`'s answer, not this closure's --
                the reason the disposition is declared once instead of decided
                again at every step.
                """

                try:
                    await self._adapt.sync_hint_rungs(question_id, payload)
                except AdaptPublishingError as exc:
                    return StepFailed(code=exc.code, message=str(exc))
                return StepSucceeded(
                    values={"hints_synced_at": utc_now()},
                    response={"synced": True},
                )

            attempt = await run_publication_step(
                self._repository,
                publication,
                ADAPT_HINT_SYNC,
                sync_hint_rungs,
            )
            if not attempt.succeeded:
                return attempt.publication
            publication = attempt.publication
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

    def _verified_external_compilation(
        self,
        draft: Draft,
        *,
        computation_decision: ComputationGateDecision,
    ) -> tuple[CompiledParameterizedItem | None, EngineValidationBinding | None]:
        if draft.current.item_type not in {
            AssessmentItemType.WEBWORK,
            AssessmentItemType.IMATHAS,
        }:
            return None, None
        validation = draft.current_engine_validation
        if (
            validation is None
            or validation.status != "passed"
            or validation.seed_count < 25
        ):
            raise PublicationValidationError(
                "The parameterized item needs a successful 25-seed engine validation."
            )
        parameterized = draft.current.response.parameterized
        if parameterized is None:
            raise PublicationValidationError(
                "The external-engine item is missing its typed parameterized spec."
            )
        if (
            computation_decision.mode == "off"
            and parameterized.compiler_profile == "legacy"
        ):
            # BUILD-08 compiled the current structured spec after destination
            # lookup and relied only on its existing passed/25-seed gate.
            return (
                compile_parameterized_item(
                    parameterized,
                    validation_seeds=25,
                ),
                None,
            )
        draft_hash = draft_content_sha256(draft.current_json)
        current_computation = None
        try:
            if parameterized.compiler_profile == "assessment_computation_v0":
                current_computation = (
                    self._repository.get_current_computation_validation(
                        draft.id,
                        expected_edit_count=draft.edit_count,
                        draft_sha256=draft_hash,
                    )
                )
                if current_computation is None:
                    raise ComputationEvidenceError(
                        "typed external compilation requires its current blueprint"
                    )
                report = validate_computation_evidence(current_computation)
                if report.result is None:
                    raise ComputationEvidenceError(
                        "typed external compilation requires a frozen result"
                    )
                blueprint = AssessmentComputationBlueprint.model_validate_json(
                    current_computation.blueprint_json
                )
                expected_spec = parameterized_spec_from_blueprint(
                    blueprint,
                    report.result,
                )
                if parameterized != expected_spec:
                    raise ComputationEvidenceError(
                        "external specification drifted from its typed blueprint"
                    )
                answer_expression, constraints = parameterized_typed_inputs(
                    blueprint,
                    report.result,
                )
                compiled = compile_typed_parameterized_item(
                    parameterized,
                    answer_expression=answer_expression,
                    constraints=constraints,
                    validation_seeds=25,
                    validation_seed_values=deterministic_seeds(blueprint),
                )
            else:
                compiled = compile_parameterized_item(
                    parameterized,
                    validation_seeds=25,
                )
        except (ComputationEvidenceError, ParameterizedCompileError, ValueError) as exc:
            raise PublicationValidationError(
                "The external-engine item no longer compiles; revalidate it "
                "before publication."
            ) from exc
        if computation_decision.mode in {"off", "assist"}:
            # A computation-owned draft is always compiled from its typed
            # blueprint, including after an operator returns the feature to
            # off. Neither off nor assist turns that reconstruction into a new
            # approval/publication authorization gate or atomic binding.
            if parameterized.compiler_profile == "assessment_computation_v0":
                current_draft = self._repository.require_draft(draft.id)
                current_validation = current_draft.current_engine_validation
                expected_previews = [
                    {
                        "seed": preview.seed,
                        "variables": preview.variables,
                        "prompt": preview.prompt,
                        "answer": preview.answer,
                        "explanation": preview.explanation,
                    }
                    for preview in compiled.previews
                ]
                if (
                    current_draft.edit_count != draft.edit_count
                    or draft_content_sha256(current_draft.current_json) != draft_hash
                    or current_validation is None
                    or current_validation.draft_id != draft.id
                    or current_validation.edit_count != draft.edit_count
                    or current_validation.status != "passed"
                    or current_validation.engine != compiled.engine
                    or current_validation.compiler_version != compiled.compiler_version
                    or current_validation.source_sha256 != compiled.source_sha256
                    or current_validation.seed_count != len(compiled.previews)
                    or current_validation.previews_json != expected_previews
                ):
                    raise PublicationValidationError(
                        "The freshly compiled engine artifact no longer matches "
                        "the exact current engine validation; revalidate it "
                        "before publication."
                    )
            return compiled, None
        binding = EngineValidationBinding(
            validation_record_id=validation.id,
            draft_id=draft.id,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=draft_hash,
            engine=compiled.engine,
            compiler_version=compiled.compiler_version,
            source_sha256=compiled.source_sha256,
            seed_count=validation.seed_count,
        )
        if (
            validation.draft_id != draft.id
            or validation.edit_count != draft.edit_count
            or validation.engine != binding.engine
            or validation.compiler_version != binding.compiler_version
            or validation.source_sha256 != binding.source_sha256
        ):
            raise PublicationValidationError(
                "The current engine evidence no longer matches the freshly "
                "compiled exact artifact; revalidate it before publication."
            )
        if computation_decision.enforced:
            report_hash = computation_decision.report_sha256
            current = current_computation
            if current is None and report_hash is not None:
                current = self._repository.get_current_computation_validation(
                    draft.id,
                    expected_edit_count=draft.edit_count,
                    draft_sha256=draft_hash,
                    report_sha256=report_hash,
                )
            if (
                current is None
                or report_hash is None
                or current.report_sha256 != report_hash
            ):
                raise PublicationValidationError(
                    "The computation report changed before engine publication "
                    "preflight; reload and retry."
                )
            try:
                validate_computation_engine_binding(current, binding)
            except ComputationEvidenceError as exc:
                raise PublicationValidationError(
                    "The computation report's engine evidence no longer matches "
                    "the compiled artifact; revalidate it before publication."
                ) from exc
        return compiled, binding

    @staticmethod
    def _resolve_license(
        draft: Draft,
        *,
        selected: LicenseSelection | None,
        manually_confirmed: bool,
    ) -> LicenseSelection:
        mapped: SourceLicense | None = source_license(
            draft.source.canonical_url,
            draft.source.license_metadata,
        )
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
        computation_decision: ComputationGateDecision,
        engine_binding: EngineValidationBinding | None,
    ) -> dict[str, Any]:
        material = {
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
        computation_evidence = computation_decision.publication_evidence
        if computation_evidence is not None:
            material["computation_evidence"] = computation_evidence
        if engine_binding is not None and computation_decision.enforced:
            material["engine_validation"] = engine_binding.artifact_identity
        return material

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
        computation_decision: ComputationGateDecision,
        engine_binding: EngineValidationBinding | None,
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
        metadata = {
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
        computation_evidence = computation_decision.publication_evidence
        if computation_evidence is not None:
            metadata["computation_evidence"] = computation_evidence
        if engine_binding is not None and computation_decision.enforced:
            metadata["engine_validation"] = engine_binding.artifact_identity
        return metadata

    def _approved_hint_snapshot(
        self,
        draft: Draft,
        *,
        bind_evidence: bool,
    ) -> tuple[dict[str, Any] | None, HintLadderBinding | None]:
        if not self._settings.hint_publication_enabled:
            return None, None
        record = draft.current_hint_ladder
        if record is None:
            raise PublicationValidationError(
                "Generate and approve the three-rung hint ladder before publishing."
            )
        grounding = inspect_hint_grounding(draft.current, record.ladder)
        if grounding:
            raise PublicationValidationError(grounding[0].message)
        if any(rung.answer_leak_detected for rung in record.ladder.rungs):
            raise PublicationValidationError(
                "Resolve answer-leak flags before publishing the hint ladder."
            )
        if record.status != "approved":
            raise PublicationValidationError(
                "Approve all three hint rungs before publishing."
            )
        if not bind_evidence:
            # Keep BUILD-08's exact snapshot representation in off/assist,
            # including its original datetime serialization.
            snapshot = {
                "version": record.version,
                "rungs": record.ladder.model_dump(mode="json")["rungs"],
                "reviewed_by": record.reviewed_by,
                "reviewed_at": _iso(record.reviewed_at),
            }
            return snapshot, None
        snapshot = approved_hint_ladder_snapshot(record)
        return snapshot, HintLadderBinding(
            record_id=record.id,
            draft_id=record.draft_id,
            expected_edit_count=record.edit_count,
            version=record.version,
            evidence_sha256=hint_ladder_evidence_sha256(record),
        )


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
