from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import re
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError, OperationalError

from .computation import (
    QUALIFIED_PINT_VERSION,
    QUALIFIED_SYMPY_VERSION,
    QUALIFIED_UCUMVERT_VERSION,
    SCHEMA_VERSION as COMPUTATION_SCHEMA_VERSION,
    UCUM_ESSENCE_SHA256,
    UCUM_PROFILE,
    VALIDATOR_VERSION,
    AssessmentComputationBlueprint,
    AssessmentValidationReport,
    CheckStatus,
    FormulaAdapterPromotionEvidence,
    NativeEngineEvidence,
    ValidationStatus,
    canonical_blueprint_hash,
)
from .native_engine_runner import (
    NativeEngineRunnerReceipt,
    qualified_native_engine_runner,
    seed_plan_sha256,
)
from .native_engine_evidence import (
    NativeEvidenceVerificationError,
    verify_native_engine_observations,
)
from .parameterized import (
    formula_adapter_promotion_identity,
    formula_adapter_promotion_sha256 as current_formula_adapter_promotion_sha256,
    qualified_formula_adapter,
)
from .schemas import (
    AssessmentItemType,
    Concept,
    Critique,
    HintLadderDraft,
    NormalizedPage,
    QuestionDraft,
    ReviewDecision,
    ReviewStatus,
)
from .source_policy import PublicSourceValidationError, canonicalize_public_identity


PIPELINE_TOOL_NAME = "LibreTexts Assessment AI"
PIPELINE_TOOL_VENDOR = "LibreTexts"
SANDBOX_ROOT_PARTS = ("Sandboxes", "johnnyphung")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonicalize_source_path(path: str) -> str:
    """Return one stable sandbox path or host-qualified public page identity."""

    raw_path = path.strip()
    stripped = raw_path.replace("\\", "/")
    if not stripped:
        raise ValueError("source path must not be blank")
    raw_parts = PurePosixPath(stripped).parts
    if ".." in raw_parts:
        raise ValueError("source path must not contain traversal segments")
    normalized = posixpath.normpath(f"/{stripped.lstrip('/')}").lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../"):
        raise ValueError("source path must identify a page")
    parts = PurePosixPath(normalized).parts
    if len(parts) >= 2 and tuple(part.casefold() for part in parts[:2]) == (
        "sandboxes",
        "johnnyphung",
    ):
        return str(PurePosixPath(*SANDBOX_ROOT_PARTS, *parts[2:]))
    try:
        return canonicalize_public_identity(raw_path)
    except PublicSourceValidationError as exc:
        raise ValueError(str(exc)) from exc


def normalized_page_hash(page: NormalizedPage) -> str:
    """Hash source content, independent of fetch time and transport metadata."""

    digest = hashlib.sha256()
    content = {
        "title": page.title,
        "plaintext": page.plaintext,
        "htmlBody": page.html_body,
        "paragraphs": [
            paragraph.model_dump(mode="json") for paragraph in page.paragraphs
        ],
    }
    digest.update(_stable_json_bytes(content))
    return digest.hexdigest()


def _stable_json_bytes(value: Any) -> bytes:
    import json

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class Base(DeclarativeBase):
    pass


review_status_enum = SAEnum(
    ReviewStatus,
    name="review_status",
    native_enum=False,
    validate_strings=True,
    values_callable=lambda enum_type: [item.value for item in enum_type],
)


class GenerationJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GenerationJob(Base):
    """Durable queue record; workers can safely resume pending jobs."""

    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_locator: Mapped[str] = mapped_column(String(4_096), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reviewer_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default=GenerationJobStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(String(80), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draft_ids_json: Mapped[list[int]] = mapped_column(
        JSON, default=list, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    @property
    def draft_ids(self) -> tuple[int, ...]:
        return tuple(int(item) for item in self.draft_ids_json)


class SourceSnapshot(Base):
    """Immutable-by-key source identity with the latest fetched representation."""

    __tablename__ = "source_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "canonical_path",
            "content_hash",
            "pipeline_version",
            name="uq_source_snapshot_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_path: Mapped[str] = mapped_column(
        String(2_048), nullable=False, index=True
    )
    canonical_url: Mapped[str] = mapped_column(String(4_096), nullable=False)
    backend: Mapped[str] = mapped_column(String(100), nullable=False)
    page_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    plaintext: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    paragraphs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    last_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    drafts: Mapped[list[Draft]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    llm_calls: Mapped[list[LLMCall]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Draft(Base):
    """Generated item plus immutable generation and mutable human-review records."""

    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint(
            "source_snapshot_id", "position", name="uq_draft_source_position"
        ),
        CheckConstraint(
            "status <> 'ready_to_publish' OR "
            "(bloom_confirmed = 1 AND difficulty_confirmed = 1)",
            name="ck_publish_ready_requires_review_gates",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    concept_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    critique_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    revised_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    current_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    status: Mapped[ReviewStatus] = mapped_column(
        review_status_enum,
        default=ReviewStatus.READY_FOR_REVIEW,
        nullable=False,
        index=True,
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(30), default="draft", nullable=False
    )
    tool_name: Mapped[str] = mapped_column(
        String(255), default=PIPELINE_TOOL_NAME, nullable=False
    )
    tool_version: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_vendor: Mapped[str] = mapped_column(
        String(255), default=PIPELINE_TOOL_VENDOR, nullable=False
    )

    bloom_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    bloom_confirmed_by: Mapped[str | None] = mapped_column(String(255))
    bloom_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    difficulty_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    difficulty_confirmed_by: Mapped[str | None] = mapped_column(String(255))
    difficulty_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    reviewer_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_reviewed_by: Mapped[str | None] = mapped_column(String(255))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_by: Mapped[str | None] = mapped_column(String(255))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_history_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    version_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": version_id}

    source: Mapped[SourceSnapshot] = relationship(back_populates="drafts")
    llm_calls: Mapped[list[LLMCall]] = relationship(
        back_populates="draft",
        passive_deletes=True,
    )
    publications: Mapped[list[Publication]] = relationship(
        back_populates="draft",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    hint_ladders: Mapped[list[HintLadderRecord]] = relationship(
        back_populates="draft",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    engine_validations: Mapped[list[EngineValidationRecord]] = relationship(
        back_populates="draft",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def current(self) -> QuestionDraft:
        return QuestionDraft.model_validate(self.current_json)

    @property
    def current_hint_ladder(self) -> HintLadderRecord | None:
        candidates = [
            ladder
            for ladder in self.hint_ladders
            if ladder.edit_count == self.edit_count
        ]
        return max(candidates, key=lambda item: item.version, default=None)

    @property
    def current_engine_validation(self) -> EngineValidationRecord | None:
        candidates = [
            record
            for record in self.engine_validations
            if record.edit_count == self.edit_count
        ]
        return max(candidates, key=lambda item: item.id, default=None)


class HintLadderRecord(Base):
    """Append-only reviewed hint ladder for one immutable draft edit."""

    __tablename__ = "hint_ladders"
    __table_args__ = (
        UniqueConstraint(
            "draft_id",
            "edit_count",
            "version",
            name="uq_hint_ladder_draft_edit_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ladder_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    confirmations_json: Mapped[dict[str, bool]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30), default="ready_for_review", nullable=False, index=True
    )
    reviewer_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    draft: Mapped[Draft] = relationship(back_populates="hint_ladders")

    @property
    def ladder(self) -> HintLadderDraft:
        return HintLadderDraft.model_validate(self.ladder_json)


class EngineValidationRecord(Base):
    """Immutable fixed-seed validation evidence for an external-engine draft."""

    __tablename__ = "engine_validation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    engine: Mapped[str] = mapped_column(String(20), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    previews_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    draft: Mapped[Draft] = relationship(back_populates="engine_validations")


class ComputationValidationRecord(Base):
    """Append-only computation evidence bound to one exact draft edit."""

    __tablename__ = "computation_validation_records"
    __table_args__ = (
        UniqueConstraint(
            "draft_id",
            "edit_count",
            "evidence_sha256",
            name="uq_computation_validation_draft_edit_evidence",
        ),
        CheckConstraint(
            "duration_ms >= 0",
            name="ck_computation_validation_duration_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    blueprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    validator_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    blueprint_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    container_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    seed_plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ComputationAttestation(Base):
    """Append-only specialist attestation for one validation report hash."""

    __tablename__ = "computation_attestations"
    __table_args__ = (
        UniqueConstraint(
            "attestation_sha256",
            name="uq_computation_attestation_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    validation_record_id: Mapped[int] = mapped_column(
        ForeignKey("computation_validation_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    specialist_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    qualification_json: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ComputationApprovalEvidence(Base):
    """Append-only enforce-mode approval bound to one exact draft version."""

    __tablename__ = "computation_approval_evidence"
    __table_args__ = (
        UniqueConstraint(
            "approval_sha256",
            name="uq_computation_approval_evidence_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_version_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    binding_json: Mapped[str] = mapped_column(Text, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reviewer_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approval_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class LLMCall(Base):
    """Audit-safe structured generation provenance for one model call."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("drafts.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    response_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    successful_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    source: Mapped[SourceSnapshot] = relationship(back_populates="llm_calls")
    draft: Mapped[Draft | None] = relationship(back_populates="llm_calls")


class PublicationState(StrEnum):
    PENDING = "pending"
    UNKNOWN = "unknown"
    ADAPT_CREATED = "adapt_created"
    HINTS_SYNCED = "hints_synced"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Publication(Base):
    """Immutable publication snapshot plus a small recoverable state machine."""

    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("publication_key", name="uq_publication_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    question_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reviewer_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    destination_folder_id: Mapped[int] = mapped_column(Integer, nullable=False)
    destination_folder_name: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str] = mapped_column(String(500), nullable=False)
    public: Mapped[bool] = mapped_column(Boolean, nullable=False)
    license: Mapped[str] = mapped_column(String(100), nullable=False)
    license_version: Mapped[str | None] = mapped_column(String(100))
    license_label: Mapped[str] = mapped_column(String(255), nullable=False)
    license_evidence_url: Mapped[str] = mapped_column(String(4_096), nullable=False)

    framework_id: Mapped[int] = mapped_column(Integer, nullable=False)
    framework_title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    alignment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    stable_topic_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    hint_ladder_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    publication_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_mapper_version: Mapped[str] = mapped_column(String(100), nullable=False)
    qti_exporter_version: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), default=PublicationState.PENDING.value, nullable=False, index=True
    )
    adapt_question_id: Mapped[int | None] = mapped_column(Integer, index=True)
    adapt_page_id: Mapped[int | None] = mapped_column(Integer)
    hints_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qti_path: Mapped[str | None] = mapped_column(String(4_096))
    qti_sha256: Mapped[str | None] = mapped_column(String(64))
    qti_size: Mapped[int | None] = mapped_column(Integer)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1_000))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    draft: Mapped[Draft] = relationship(back_populates="publications")
    attempts: Mapped[list[PublicationAttempt]] = relationship(
        back_populates="publication",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PublicationComputationEvidence(Base):
    """Immutable computation evidence captured for one publication snapshot."""

    __tablename__ = "publication_computation_evidence"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            name="uq_publication_computation_evidence_publication",
        ),
        UniqueConstraint(
            "snapshot_sha256",
            name="uq_publication_computation_evidence_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_record_id: Mapped[int] = mapped_column(
        ForeignKey("computation_validation_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(40), nullable=False)
    attestation_sha256s_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class PublicationAttempt(Base):
    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint(
            "publication_id", "attempt_number", name="uq_publication_attempt_number"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resulting_state: Mapped[str] = mapped_column(String(30), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1_000))
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    publication: Mapped[Publication] = relationship(back_populates="attempts")


@dataclass(frozen=True)
class ComputationValidationWrite:
    """Opaque serialized computation output ready for append-only persistence."""

    status: str
    schema_version: str
    validator_revision: str
    blueprint_json: str
    report_json: str
    container_digest: str
    dependency_versions_json: str = "{}"
    seed_plan_json: str = "[]"
    duration_ms: int = 0
    engine_evidence_json: str = "{}"


@dataclass(frozen=True)
class ComputationValidationRead:
    id: int
    draft_id: int
    edit_count: int
    source_sha256: str
    draft_sha256: str
    blueprint_sha256: str
    report_sha256: str
    evidence_sha256: str
    status: str
    schema_version: str
    validator_revision: str
    blueprint_json: str
    report_json: str
    dependency_versions_json: str
    container_digest: str
    seed_plan_json: str
    duration_ms: int
    engine_evidence_json: str
    created_at: datetime
    is_current: bool


@dataclass(frozen=True)
class ComputationAttestationWrite:
    specialist_identity: str
    rationale: str
    qualification_json: str = "{}"


@dataclass(frozen=True)
class ComputationAttestationRead:
    id: int
    validation_record_id: int
    draft_id: int
    edit_count: int
    report_sha256: str
    specialist_identity: str
    rationale: str
    qualification_json: str
    attestation_sha256: str
    created_at: datetime
    is_current: bool


@dataclass(frozen=True)
class EngineValidationBinding:
    """Exact external-engine artifact approved for one draft revision."""

    validation_record_id: int
    draft_id: int
    expected_edit_count: int
    expected_draft_sha256: str
    engine: str
    compiler_version: str
    source_sha256: str
    seed_count: int

    @property
    def artifact_identity(self) -> dict[str, str]:
        return {
            "engine": self.engine,
            "compiler_version": self.compiler_version,
            "source_sha256": self.source_sha256,
        }

    @property
    def snapshot(self) -> dict[str, Any]:
        return {
            "validation_record_id": self.validation_record_id,
            "draft_id": self.draft_id,
            "edit_count": self.expected_edit_count,
            "draft_sha256": self.expected_draft_sha256,
            **self.artifact_identity,
            "seed_count": self.seed_count,
        }


@dataclass(frozen=True)
class HintLadderBinding:
    """Exact approved hint artifact selected before publication lookup."""

    record_id: int
    draft_id: int
    expected_edit_count: int
    version: int
    evidence_sha256: str


def approved_hint_ladder_snapshot(record: HintLadderRecord) -> dict[str, Any]:
    """Return the exact learner-facing hint snapshot bound at publication."""

    return {
        "version": record.version,
        "rungs": record.ladder.model_dump(mode="json")["rungs"],
        "reviewed_by": record.reviewed_by,
        "reviewed_at": (
            _as_utc(record.reviewed_at).isoformat()
            if record.reviewed_at is not None
            else None
        ),
    }


def hint_ladder_evidence_sha256(record: HintLadderRecord) -> str:
    """Hash every immutable/review field that makes a hint version publishable."""

    material = {
        "id": record.id,
        "draft_id": record.draft_id,
        "edit_count": record.edit_count,
        "version": record.version,
        "ladder_json": record.ladder_json,
        "confirmations_json": record.confirmations_json,
        "status": record.status,
        "reviewer_notes": record.reviewer_notes,
        "reviewed_by": record.reviewed_by,
        "reviewed_at": (
            _as_utc(record.reviewed_at).isoformat()
            if record.reviewed_at is not None
            else None
        ),
        "created_at": _as_utc(record.created_at).isoformat(),
    }
    return hashlib.sha256(_stable_json_bytes(material)).hexdigest()


@dataclass(frozen=True)
class PublicationComputationEvidenceWrite:
    report_sha256: str
    attestation_sha256s: tuple[str, ...] = ()
    engine_validation: EngineValidationBinding | None = None


@dataclass(frozen=True)
class PublicationComputationEvidenceRead:
    id: int
    publication_id: int
    validation_record_id: int
    draft_id: int
    edit_count: int
    report_sha256: str
    validation_status: str
    attestation_sha256s: tuple[str, ...]
    snapshot_json: str
    snapshot_sha256: str
    created_at: datetime


@dataclass(frozen=True)
class ComputationGateBinding:
    """Expected enforce-mode evidence for one exact detached draft revision."""

    expected_edit_count: int
    expected_draft_sha256: str
    scoped: bool
    validation_record_id: int | None = None
    evidence_sha256: str | None = None
    validation_status: str | None = None
    report_sha256: str | None = None
    formula_adapter_promotion_sha256: str | None = None
    runtime_image_reference: str | None = None
    runtime_container_digest: str | None = None
    runtime_promotion_sha256: str | None = None
    attestation_sha256s: tuple[str, ...] = ()
    trusted_specialist_subjects: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftWrite:
    position: int
    concept: Concept | Mapping[str, Any]
    raw: QuestionDraft | Mapping[str, Any]
    critique: Critique | Mapping[str, Any]
    revised: QuestionDraft | Mapping[str, Any]
    hint_ladder: HintLadderDraft | Mapping[str, Any] | None = None
    engine_validation: Mapping[str, Any] | None = None
    computation_validation: ComputationValidationWrite | None = None


@dataclass(frozen=True)
class LLMCallWrite:
    stage: str
    provider: str
    model_id: str
    prompt_version: str
    prompt: str
    request: Mapping[str, Any]
    response: Mapping[str, Any] | BaseModel
    raw_response: str
    response_metadata: Mapping[str, Any]
    attempts: Sequence[Mapping[str, Any] | BaseModel]
    successful_attempt: int
    draft_position: int | None = None


@dataclass(frozen=True)
class StoredGeneration:
    source_id: int
    draft_ids: tuple[int, ...]
    run_id: str
    content_hash: str
    canonical_path: str


class Database:
    """Small engine/session owner suitable for FastAPI lifespan management."""

    def __init__(self, database_url: str) -> None:
        engine_kwargs: dict[str, Any] = {"future": True}
        if database_url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            if database_url in {"sqlite://", "sqlite:///:memory:"}:
                engine_kwargs["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **engine_kwargs)
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            self._apply_sqlite_additive_migrations()

    def _apply_sqlite_additive_migrations(self) -> None:
        existing = {
            column["name"]
            for column in inspect(self.engine).get_columns("publications")
        }
        additions = {
            "hint_ladder_snapshot_json": "JSON",
            "hints_synced_at": "DATETIME",
        }
        with self.engine.begin() as connection:
            for name, data_type in additions.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE publications ADD COLUMN {name} {data_type}")
                    )

    def dispose(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session


def init_database(database_url: str) -> Database:
    database = Database(database_url)
    database.create_schema()
    return database


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class DraftNotFoundError(LookupError):
    pass


class ReviewTransitionError(ValueError):
    pass


class ReviewGateError(ReviewTransitionError):
    pass


class ConcurrentDraftUpdateError(ReviewTransitionError):
    pass


class DraftGroundingError(ReviewTransitionError):
    pass


class ComputationEvidenceError(ValueError):
    pass


class ComputationEvidenceConflictError(ComputationEvidenceError):
    pass


def _completed_generation_for_source(
    session: Session,
    source: SourceSnapshot,
) -> StoredGeneration | None:
    draft_ids = tuple(
        session.scalars(
            select(Draft.id)
            .where(Draft.source_snapshot_id == source.id)
            .order_by(Draft.position, Draft.id)
        )
    )
    if not draft_ids:
        return None
    return StoredGeneration(
        source_id=source.id,
        draft_ids=draft_ids,
        run_id=source.last_run_id,
        content_hash=source.content_hash,
        canonical_path=source.canonical_path,
    )


class DraftRepository:
    """Transactional persistence boundary used by generation and review services."""

    def __init__(self, sessions: sessionmaker[Session] | Database) -> None:
        self._sessions = (
            sessions.session_factory if isinstance(sessions, Database) else sessions
        )

    def create_generation_job(
        self,
        *,
        source_type: str,
        source_locator: str,
        request: Mapping[str, Any],
        reviewer: str,
    ) -> GenerationJob:
        job_id = str(uuid4())
        with self._sessions.begin() as session:
            session.add(
                GenerationJob(
                    id=job_id,
                    source_type=source_type,
                    source_locator=source_locator,
                    request_json=_json_value(request),
                    reviewer_identity=_actor(reviewer),
                )
            )
        return self.require_generation_job(job_id)

    def get_generation_job(self, job_id: str) -> GenerationJob | None:
        with self._sessions() as session:
            return session.get(GenerationJob, job_id)

    def require_generation_job(self, job_id: str) -> GenerationJob:
        job = self.get_generation_job(job_id)
        if job is None:
            raise DraftNotFoundError("generation job was not found")
        return job

    def claim_next_generation_job(self) -> GenerationJob | None:
        with self._sessions.begin() as session:
            job = session.scalar(
                select(GenerationJob)
                .where(GenerationJob.status == GenerationJobStatus.PENDING.value)
                .order_by(GenerationJob.created_at, GenerationJob.id)
            )
            if job is None:
                return None
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job.id,
                    GenerationJob.status == GenerationJobStatus.PENDING.value,
                )
                .values(
                    status=GenerationJobStatus.RUNNING.value,
                    stage="fetching_source",
                    progress=5,
                    started_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            if result.rowcount != 1:
                return None
            job_id = job.id
        return self.require_generation_job(job_id)

    def update_generation_job(
        self,
        job_id: str,
        *,
        status: GenerationJobStatus | None = None,
        stage: str | None = None,
        progress: int | None = None,
        draft_ids: Sequence[int] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> GenerationJob:
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("job progress must be between 0 and 100")
        with self._sessions.begin() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise DraftNotFoundError("generation job was not found")
            if status is not None:
                job.status = status.value
            if stage is not None:
                job.stage = stage[:80]
            if progress is not None:
                job.progress = progress
            if draft_ids is not None:
                job.draft_ids_json = [int(item) for item in draft_ids]
            job.error_code = error_code
            job.error_message = (
                " ".join(error_message.split())[:500] if error_message else None
            )
            job.updated_at = utc_now()
            if status in {
                GenerationJobStatus.SUCCEEDED,
                GenerationJobStatus.FAILED,
            }:
                job.completed_at = utc_now()
        return self.require_generation_job(job_id)

    def requeue_interrupted_generation_jobs(self) -> int:
        with self._sessions.begin() as session:
            result = session.execute(
                update(GenerationJob)
                .where(GenerationJob.status == GenerationJobStatus.RUNNING.value)
                .values(
                    status=GenerationJobStatus.PENDING.value,
                    stage="queued_after_restart",
                    progress=0,
                    started_at=None,
                    updated_at=utc_now(),
                )
            )
            return int(result.rowcount or 0)

    def find_completed_generation(
        self,
        *,
        canonical_path: str,
        content_hash: str,
        pipeline_version: str,
    ) -> StoredGeneration | None:
        """Return an existing completed idempotency key without mutating review state."""

        normalized_path = canonicalize_source_path(canonical_path)
        with self._sessions() as session:
            source = session.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.canonical_path == normalized_path,
                    SourceSnapshot.content_hash == content_hash,
                    SourceSnapshot.pipeline_version == pipeline_version,
                )
            )
            if source is None:
                return None
            return _completed_generation_for_source(session, source)

    def _wait_for_completed_generation(
        self,
        *,
        canonical_path: str,
        content_hash: str,
        pipeline_version: str,
    ) -> StoredGeneration | None:
        """Read the winning transaction after an identical-key write conflict."""

        for delay in (0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8):
            if delay:
                time.sleep(delay)
            completed = self.find_completed_generation(
                canonical_path=canonical_path,
                content_hash=content_hash,
                pipeline_version=pipeline_version,
            )
            if completed is not None:
                return completed
        return None

    def replace_generated_drafts(
        self,
        *,
        page: NormalizedPage,
        pipeline_version: str,
        drafts: Sequence[DraftWrite],
        llm_calls: Sequence[LLMCallWrite],
        content_hash: str | None = None,
        run_id: str | None = None,
    ) -> StoredGeneration:
        """Atomically create a draft set or return the completed identical key."""

        if not pipeline_version.strip():
            raise ValueError("pipeline_version must not be blank")
        if not drafts:
            raise ValueError("a completed generation must contain at least one draft")
        positions = [draft.position for draft in drafts]
        if len(positions) != len(set(positions)) or any(
            position < 0 for position in positions
        ):
            raise ValueError("draft positions must be unique non-negative integers")

        canonical_path = canonicalize_source_path(page.source.path)
        resolved_hash = content_hash or normalized_page_hash(page)
        if len(resolved_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 hex digest")
        resolved_run_id = run_id or str(uuid4())
        now = utc_now()

        with self._sessions.begin() as session:
            source = session.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.canonical_path == canonical_path,
                    SourceSnapshot.content_hash == resolved_hash,
                    SourceSnapshot.pipeline_version == pipeline_version,
                )
            )
            if source is not None:
                completed = _completed_generation_for_source(session, source)
                if completed is not None:
                    return completed
            if source is None:
                source = SourceSnapshot(
                    canonical_path=canonical_path,
                    canonical_url=page.source.canonical_url,
                    backend=page.source.backend,
                    page_id=page.source.page_id,
                    title=page.title,
                    plaintext=page.plaintext,
                    html_body=page.html_body,
                    paragraphs_json=[
                        paragraph.model_dump(mode="json")
                        for paragraph in page.paragraphs
                    ],
                    content_hash=resolved_hash,
                    pipeline_version=pipeline_version,
                    is_current=True,
                    last_run_id=resolved_run_id,
                )
                session.add(source)
                try:
                    session.flush()
                except (IntegrityError, OperationalError):
                    session.rollback()
                    completed = self._wait_for_completed_generation(
                        canonical_path=canonical_path,
                        content_hash=resolved_hash,
                        pipeline_version=pipeline_version,
                    )
                    if completed is not None:
                        return completed
                    raise
            else:
                source.canonical_url = page.source.canonical_url
                source.backend = page.source.backend
                source.page_id = page.source.page_id
                source.title = page.title
                source.plaintext = page.plaintext
                source.html_body = page.html_body
                source.paragraphs_json = [
                    paragraph.model_dump(mode="json") for paragraph in page.paragraphs
                ]
                source.is_current = True
                source.last_run_id = resolved_run_id
                source.updated_at = now

            session.execute(
                update(SourceSnapshot)
                .where(
                    SourceSnapshot.canonical_path == canonical_path,
                    SourceSnapshot.id != source.id,
                    SourceSnapshot.is_current.is_(True),
                )
                .values(is_current=False, updated_at=now)
            )

            # Completed keys return above. Cleanup is only for an incomplete row
            # left without a draft set; it can never erase review state/history.
            session.execute(
                delete(LLMCall).where(LLMCall.source_snapshot_id == source.id)
            )
            session.execute(delete(Draft).where(Draft.source_snapshot_id == source.id))
            try:
                session.flush()
            except (IntegrityError, OperationalError):
                session.rollback()
                completed = self._wait_for_completed_generation(
                    canonical_path=canonical_path,
                    content_hash=resolved_hash,
                    pipeline_version=pipeline_version,
                )
                if completed is not None:
                    return completed
                raise

            draft_ids_by_position: dict[int, int] = {}
            stored_drafts: list[Draft] = []
            for draft_write in sorted(drafts, key=lambda item: item.position):
                concept = Concept.model_validate(_json_value(draft_write.concept))
                raw = QuestionDraft.model_validate(_json_value(draft_write.raw))
                critique = Critique.model_validate(_json_value(draft_write.critique))
                revised = QuestionDraft.model_validate(_json_value(draft_write.revised))
                generated_event = {
                    "event": "generated",
                    "actor": "assessment-ai",
                    "at": now.isoformat(),
                    "run_id": resolved_run_id,
                }
                stored = Draft(
                    source_snapshot_id=source.id,
                    position=draft_write.position,
                    concept_json=concept.model_dump(mode="json"),
                    raw_json=raw.model_dump(mode="json"),
                    critique_json=critique.model_dump(mode="json"),
                    revised_json=revised.model_dump(mode="json"),
                    current_json=revised.model_dump(mode="json"),
                    status=ReviewStatus.READY_FOR_REVIEW,
                    lifecycle_status="draft",
                    tool_name=PIPELINE_TOOL_NAME,
                    tool_version=pipeline_version,
                    tool_vendor=PIPELINE_TOOL_VENDOR,
                    review_history_json=[generated_event],
                )
                session.add(stored)
                stored_drafts.append(stored)
            session.flush()
            stored_drafts_by_position = {
                stored.position: stored for stored in stored_drafts
            }
            for stored in stored_drafts:
                draft_ids_by_position[stored.position] = stored.id

            for draft_write in drafts:
                if draft_write.hint_ladder is None:
                    continue
                ladder = HintLadderDraft.model_validate(
                    _json_value(draft_write.hint_ladder)
                )
                ladder = analyze_hint_leaks(draft_write.revised, ladder)
                session.add(
                    HintLadderRecord(
                        draft_id=draft_ids_by_position[draft_write.position],
                        edit_count=0,
                        version=1,
                        ladder_json=ladder.model_dump(mode="json"),
                        confirmations_json={
                            rung.rung.value: False for rung in ladder.rungs
                        },
                    )
                )

            for draft_write in drafts:
                if draft_write.computation_validation is None:
                    continue
                stored = stored_drafts_by_position[draft_write.position]
                session.add(
                    _new_computation_validation_record(
                        stored,
                        edit_count=0,
                        source_sha256=source.content_hash,
                        value=draft_write.computation_validation,
                    )
                )

            for draft_write in drafts:
                stored = stored_drafts_by_position[draft_write.position]
                parameterized = stored.current.response.parameterized
                computation_owned_external = (
                    stored.current.item_type
                    in {
                        AssessmentItemType.WEBWORK,
                        AssessmentItemType.IMATHAS,
                    }
                    and parameterized is not None
                    and parameterized.compiler_profile == "assessment_computation_v0"
                )
                if computation_owned_external:
                    # Computation-owned external drafts have one authoritative
                    # compiler receipt.  Derive the compatibility row from the
                    # already validated computation envelope instead of trusting
                    # the parallel DraftWrite field.
                    validation = (
                        _engine_validation_from_computation_evidence(
                            stored.current,
                            draft_write.computation_validation,
                        )
                        if draft_write.computation_validation is not None
                        else None
                    )
                elif draft_write.engine_validation is not None:
                    legacy_validation = dict(draft_write.engine_validation)
                    validation = {
                        "engine": str(legacy_validation["engine"]),
                        "compiler_version": str(legacy_validation["compiler_version"]),
                        "source_sha256": str(legacy_validation["source_sha256"]),
                        "seed_count": int(legacy_validation["seed_count"]),
                        "previews_json": list(legacy_validation["previews"]),
                        "status": "passed",
                    }
                else:
                    validation = None
                if validation is None:
                    continue
                session.add(
                    EngineValidationRecord(
                        draft_id=draft_ids_by_position[draft_write.position],
                        edit_count=0,
                        **validation,
                    )
                )

            for call in llm_calls:
                if (
                    call.draft_position is not None
                    and call.draft_position not in draft_ids_by_position
                ):
                    raise ValueError(
                        f"LLM call refers to unknown draft position {call.draft_position}"
                    )
                prompt_hash = hashlib.sha256(call.prompt.encode("utf-8")).hexdigest()
                session.add(
                    LLMCall(
                        source_snapshot_id=source.id,
                        draft_id=(
                            draft_ids_by_position[call.draft_position]
                            if call.draft_position is not None
                            else None
                        ),
                        run_id=resolved_run_id,
                        stage=call.stage,
                        provider=call.provider,
                        model_id=call.model_id,
                        prompt_version=call.prompt_version,
                        prompt_hash=prompt_hash,
                        prompt=call.prompt,
                        request_json=_json_value(call.request),
                        response_json=_json_value(call.response),
                        raw_response=call.raw_response,
                        response_metadata_json=_json_value(call.response_metadata),
                        attempts_json=[
                            _json_value(attempt) for attempt in call.attempts
                        ],
                        successful_attempt=call.successful_attempt,
                    )
                )

            return StoredGeneration(
                source_id=source.id,
                draft_ids=tuple(
                    draft_ids_by_position[position]
                    for position in sorted(draft_ids_by_position)
                ),
                run_id=resolved_run_id,
                content_hash=resolved_hash,
                canonical_path=canonical_path,
            )

    def get_source(self, source_id: int) -> SourceSnapshot | None:
        with self._sessions() as session:
            return session.scalar(
                select(SourceSnapshot)
                .options(
                    selectinload(SourceSnapshot.drafts),
                    selectinload(SourceSnapshot.llm_calls),
                )
                .where(SourceSnapshot.id == source_id)
            )

    def get_draft(self, draft_id: int) -> Draft | None:
        with self._sessions() as session:
            return session.scalar(
                select(Draft)
                .options(
                    selectinload(Draft.source),
                    selectinload(Draft.llm_calls),
                    selectinload(Draft.publications),
                    selectinload(Draft.hint_ladders),
                    selectinload(Draft.engine_validations),
                )
                .where(Draft.id == draft_id)
            )

    def require_draft(self, draft_id: int) -> Draft:
        draft = self.get_draft(draft_id)
        if draft is None:
            raise DraftNotFoundError(f"draft {draft_id} was not found")
        return draft

    def list_drafts(
        self,
        *,
        status: ReviewStatus | None = None,
        current_sources_only: bool = True,
    ) -> list[Draft]:
        query = (
            select(Draft)
            .join(Draft.source)
            .options(
                selectinload(Draft.source),
                selectinload(Draft.llm_calls),
                selectinload(Draft.hint_ladders),
                selectinload(Draft.engine_validations),
            )
            .order_by(SourceSnapshot.updated_at.desc(), Draft.position, Draft.id)
        )
        if status is not None:
            query = query.where(Draft.status == status)
        if current_sources_only:
            query = query.where(SourceSnapshot.is_current.is_(True))
        with self._sessions() as session:
            return list(session.scalars(query).unique())

    def edit_draft(
        self,
        draft_id: int,
        updated_draft: QuestionDraft | Mapping[str, Any],
        *,
        editor: str,
        notes: str = "",
        computation_validation: ComputationValidationWrite | None = None,
        expected_edit_count: int | None = None,
        expected_draft_sha256: str | None = None,
    ) -> Draft:
        actor = _actor(editor)
        validated = QuestionDraft.model_validate(_json_value(updated_draft))
        if (expected_edit_count is None) != (expected_draft_sha256 is None):
            raise ValueError(
                "expected_edit_count and expected_draft_sha256 must be supplied together"
            )
        expected_hash = (
            _sha256_digest(expected_draft_sha256, "expected_draft_sha256")
            if expected_draft_sha256 is not None
            else None
        )
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                draft = (
                    session.scalar(
                        select(Draft).where(Draft.id == draft_id).with_for_update()
                    )
                    if expected_edit_count is not None
                    else session.get(Draft, draft_id)
                )
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
                if expected_edit_count is not None and (
                    draft.edit_count != expected_edit_count
                    or _draft_sha256(draft.current_json) != expected_hash
                ):
                    raise ConcurrentDraftUpdateError(
                        "the draft changed during computation revalidation; reload it "
                        "before retrying"
                    )
                previous_hint_ladder = session.scalar(
                    select(HintLadderRecord)
                    .where(
                        HintLadderRecord.draft_id == draft.id,
                        HintLadderRecord.edit_count == draft.edit_count,
                    )
                    .order_by(HintLadderRecord.version.desc())
                )
                _validate_persisted_question(draft, validated)
                before = draft.current_json
                draft.current_json = validated.model_dump(mode="json")
                draft.edited_by = actor
                draft.edited_at = now
                draft.edit_count += 1
                draft.reviewer_notes = notes
                draft.status = ReviewStatus.READY_FOR_REVIEW
                _clear_confirmations(draft)
                if previous_hint_ladder is not None:
                    ladder = HintLadderDraft.model_validate(
                        previous_hint_ladder.ladder_json
                    )
                    session.add(
                        HintLadderRecord(
                            draft_id=draft.id,
                            edit_count=draft.edit_count,
                            version=1,
                            ladder_json=ladder.model_dump(mode="json"),
                            confirmations_json={
                                rung.rung.value: False for rung in ladder.rungs
                            },
                            status="ready_for_review",
                            reviewer_notes="Question edited; hint approval reset.",
                        )
                    )
                validation = (
                    _engine_validation_from_computation_evidence(
                        validated,
                        computation_validation,
                    )
                    if computation_validation is not None
                    else _engine_validation_for(validated)
                )
                if validation is not None:
                    session.add(
                        EngineValidationRecord(
                            draft_id=draft.id,
                            edit_count=draft.edit_count,
                            **validation,
                        )
                    )
                if computation_validation is not None:
                    session.add(
                        _new_computation_validation_record(
                            draft,
                            edit_count=draft.edit_count,
                            source_sha256=draft.source.content_hash,
                            value=computation_validation,
                        )
                    )
                draft.review_history_json = [
                    *draft.review_history_json,
                    {
                        "event": "edited",
                        "actor": actor,
                        "at": now.isoformat(),
                        "notes": notes,
                        "before": before,
                        "after": draft.current_json,
                    },
                ]
        except StaleDataError:
            raise ConcurrentDraftUpdateError(
                "the draft changed during editing; reload it before saving"
            ) from None
        return self.require_draft(draft_id)

    def append_computation_validation(
        self,
        draft_id: int,
        *,
        edit_count: int,
        record: ComputationValidationWrite,
    ) -> ComputationValidationRead:
        """Append evidence only when it matches the draft's current edit."""

        try:
            with self._sessions.begin() as session:
                draft = session.scalar(
                    select(Draft)
                    .options(selectinload(Draft.source))
                    .where(Draft.id == draft_id)
                    .with_for_update()
                )
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
                if draft.edit_count != edit_count:
                    raise ComputationEvidenceError(
                        "computation evidence edit_count does not match the current draft"
                    )
                candidate = _new_computation_validation_record(
                    draft,
                    edit_count=edit_count,
                    source_sha256=draft.source.content_hash,
                    value=record,
                )
                parameterized = draft.current.response.parameterized
                engine_validation = (
                    _engine_validation_from_computation_evidence(
                        draft.current,
                        record,
                    )
                    if draft.current.item_type
                    in {
                        AssessmentItemType.WEBWORK,
                        AssessmentItemType.IMATHAS,
                    }
                    and parameterized is not None
                    and parameterized.compiler_profile == "assessment_computation_v0"
                    else None
                )
                existing = session.scalar(
                    select(ComputationValidationRecord).where(
                        ComputationValidationRecord.draft_id == draft.id,
                        ComputationValidationRecord.edit_count == edit_count,
                        ComputationValidationRecord.evidence_sha256
                        == candidate.evidence_sha256,
                    )
                )
                if existing is not None:
                    if not _same_computation_validation(existing, candidate):
                        raise ComputationEvidenceConflictError(
                            "the evidence hash is already bound to different evidence"
                        )
                    current = _current_computation_validation_record(session, draft)
                    return _computation_validation_read(
                        existing,
                        is_current=current is not None and current.id == existing.id,
                    )
                now = utc_now()
                draft.updated_at = now
                if draft.status == ReviewStatus.READY_TO_PUBLISH:
                    _transition_draft(
                        draft,
                        ReviewStatus.READY_FOR_REVIEW,
                        actor="assessment-computation-validator",
                        notes=(
                            "New computation evidence was appended; the prior human "
                            "approval is stale and must be renewed."
                        ),
                        now=now,
                    )
                session.add(candidate)
                if engine_validation is not None:
                    session.add(
                        EngineValidationRecord(
                            draft_id=draft.id,
                            edit_count=draft.edit_count,
                            **engine_validation,
                        )
                    )
                session.flush()
                result = _computation_validation_read(candidate, is_current=True)
        except (IntegrityError, StaleDataError) as exc:
            recovered = self._recover_computation_validation_race(
                draft_id,
                edit_count=edit_count,
                record=record,
            )
            if recovered is not None:
                return recovered
            if isinstance(exc, StaleDataError):
                raise ConcurrentDraftUpdateError(
                    "the draft changed while computation evidence was appended; "
                    "reload and retry"
                ) from None
            raise
        return result

    def _recover_computation_validation_race(
        self,
        draft_id: int,
        *,
        edit_count: int,
        record: ComputationValidationWrite,
    ) -> ComputationValidationRead | None:
        """Return only an exact concurrent duplicate that is still current."""

        with self._sessions() as session:
            draft = session.scalar(
                select(Draft)
                .options(selectinload(Draft.source))
                .where(Draft.id == draft_id)
            )
            if draft is None or draft.edit_count != edit_count:
                return None
            candidate = _new_computation_validation_record(
                draft,
                edit_count=edit_count,
                source_sha256=draft.source.content_hash,
                value=record,
            )
            existing = session.scalar(
                select(ComputationValidationRecord).where(
                    ComputationValidationRecord.draft_id == draft.id,
                    ComputationValidationRecord.edit_count == edit_count,
                    ComputationValidationRecord.evidence_sha256
                    == candidate.evidence_sha256,
                )
            )
            if existing is None:
                return None
            if not _same_computation_validation(existing, candidate):
                raise ComputationEvidenceConflictError(
                    "the evidence hash is already bound to different evidence"
                )
            current = _current_computation_validation_record(session, draft)
            if current is None or current.id != existing.id:
                return None
            return _computation_validation_read(existing, is_current=True)

    def get_computation_validation(
        self,
        draft_id: int,
        *,
        edit_count: int,
        report_sha256: str,
    ) -> ComputationValidationRead | None:
        """Read one exact historical record without making it current again."""

        report_hash = _sha256_digest(report_sha256, "report_sha256")
        with self._sessions() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                return None
            record = session.scalar(
                select(ComputationValidationRecord)
                .where(
                    ComputationValidationRecord.draft_id == draft_id,
                    ComputationValidationRecord.edit_count == edit_count,
                    ComputationValidationRecord.report_sha256 == report_hash,
                )
                .order_by(ComputationValidationRecord.id.desc())
            )
            if record is None:
                return None
            current = _current_computation_validation_record(session, draft)
            return _computation_validation_read(
                record,
                is_current=current is not None and current.id == record.id,
            )

    def get_current_computation_validation(
        self,
        draft_id: int,
        *,
        expected_edit_count: int | None = None,
        draft_sha256: str | None = None,
        report_sha256: str | None = None,
    ) -> ComputationValidationRead | None:
        """Return only the latest report for the exact current draft content."""

        expected_draft_hash = (
            _sha256_digest(draft_sha256, "draft_sha256")
            if draft_sha256 is not None
            else None
        )
        expected_report_hash = (
            _sha256_digest(report_sha256, "report_sha256")
            if report_sha256 is not None
            else None
        )
        with self._sessions() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                return None
            if (
                expected_edit_count is not None
                and draft.edit_count != expected_edit_count
            ):
                return None
            current_draft_hash = _draft_sha256(draft.current_json)
            if (
                expected_draft_hash is not None
                and expected_draft_hash != current_draft_hash
            ):
                return None
            record = _current_computation_validation_record(session, draft)
            if record is None:
                return None
            if (
                expected_report_hash is not None
                and expected_report_hash != record.report_sha256
            ):
                return None
            return _computation_validation_read(record, is_current=True)

    def draft_revision_matches(
        self,
        draft_id: int,
        *,
        edit_count: int,
        draft_sha256: str,
        status: ReviewStatus | None = None,
    ) -> bool:
        """Check a detached draft binding without accepting a newer revision."""

        expected_hash = _sha256_digest(draft_sha256, "draft_sha256")
        with self._sessions() as session:
            draft = session.get(Draft, draft_id)
            return bool(
                draft is not None
                and draft.edit_count == edit_count
                and _draft_sha256(draft.current_json) == expected_hash
                and (status is None or draft.status == status)
            )

    def list_computation_validations(
        self,
        draft_id: int,
    ) -> tuple[ComputationValidationRead, ...]:
        with self._sessions() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise DraftNotFoundError(f"draft {draft_id} was not found")
            records = tuple(
                session.scalars(
                    select(ComputationValidationRecord)
                    .where(ComputationValidationRecord.draft_id == draft_id)
                    .order_by(
                        ComputationValidationRecord.edit_count,
                        ComputationValidationRecord.id,
                    )
                )
            )
            current = _current_computation_validation_record(session, draft)
            current_id = current.id if current is not None else None
            return tuple(
                _computation_validation_read(
                    record,
                    is_current=record.id == current_id,
                )
                for record in records
            )

    def append_computation_attestation(
        self,
        draft_id: int,
        *,
        edit_count: int,
        report_sha256: str,
        attestation: ComputationAttestationWrite,
    ) -> ComputationAttestationRead:
        """Bind one specialist attestation to the exact current report."""

        report_hash = _sha256_digest(report_sha256, "report_sha256")
        specialist = _actor(attestation.specialist_identity)
        rationale = attestation.rationale.strip()
        if not rationale:
            raise ComputationEvidenceError(
                "computation attestation rationale must not be blank"
            )
        qualification_json = _opaque_json_string(
            attestation.qualification_json,
            "qualification_json",
        )
        try:
            with self._sessions.begin() as session:
                draft = session.scalar(
                    select(Draft).where(Draft.id == draft_id).with_for_update()
                )
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
                if draft.edit_count != edit_count:
                    raise ComputationEvidenceError(
                        "computation attestation edit_count does not match the "
                        "current draft"
                    )
                validation = _current_computation_validation_record(session, draft)
                if validation is None or validation.report_sha256 != report_hash:
                    raise ComputationEvidenceError(
                        "computation attestation must reference the current report hash"
                    )
                attestation_hash = _computation_attestation_sha256(
                    validation_record_id=validation.id,
                    draft_id=draft.id,
                    edit_count=edit_count,
                    report_sha256=report_hash,
                    specialist_identity=specialist,
                    rationale=rationale,
                    qualification_json=qualification_json,
                )
                existing = session.scalar(
                    select(ComputationAttestation).where(
                        ComputationAttestation.attestation_sha256 == attestation_hash
                    )
                )
                if existing is not None:
                    return _computation_attestation_read(existing, is_current=True)
                stored = ComputationAttestation(
                    validation_record_id=validation.id,
                    draft_id=draft.id,
                    edit_count=edit_count,
                    report_sha256=report_hash,
                    specialist_identity=specialist,
                    rationale=rationale,
                    qualification_json=qualification_json,
                    attestation_sha256=attestation_hash,
                )
                now = utc_now()
                draft.updated_at = now
                if draft.status == ReviewStatus.READY_TO_PUBLISH:
                    _transition_draft(
                        draft,
                        ReviewStatus.READY_FOR_REVIEW,
                        actor="assessment-computation-attestor",
                        notes=(
                            "New computation attestation evidence was appended; the "
                            "prior human approval is stale and must be renewed."
                        ),
                        now=now,
                    )
                session.add(stored)
                session.flush()
                result = _computation_attestation_read(stored, is_current=True)
        except (IntegrityError, StaleDataError) as exc:
            recovered = self._recover_computation_attestation_race(
                draft_id,
                edit_count=edit_count,
                report_sha256=report_hash,
                specialist_identity=specialist,
                rationale=rationale,
                qualification_json=qualification_json,
            )
            if recovered is not None:
                return recovered
            if isinstance(exc, StaleDataError):
                raise ConcurrentDraftUpdateError(
                    "the draft changed while computation attestation evidence was "
                    "appended; reload and retry"
                ) from None
            raise
        return result

    def _recover_computation_attestation_race(
        self,
        draft_id: int,
        *,
        edit_count: int,
        report_sha256: str,
        specialist_identity: str,
        rationale: str,
        qualification_json: str,
    ) -> ComputationAttestationRead | None:
        """Return only an exact concurrent attestation duplicate."""

        with self._sessions() as session:
            draft = session.get(Draft, draft_id)
            if draft is None or draft.edit_count != edit_count:
                return None
            validation = _current_computation_validation_record(session, draft)
            if validation is None or validation.report_sha256 != report_sha256:
                return None
            attestation_hash = _computation_attestation_sha256(
                validation_record_id=validation.id,
                draft_id=draft.id,
                edit_count=edit_count,
                report_sha256=report_sha256,
                specialist_identity=specialist_identity,
                rationale=rationale,
                qualification_json=qualification_json,
            )
            existing = session.scalar(
                select(ComputationAttestation).where(
                    ComputationAttestation.attestation_sha256 == attestation_hash
                )
            )
            if (
                existing is None
                or existing.validation_record_id != validation.id
                or existing.draft_id != draft.id
                or existing.edit_count != edit_count
                or existing.report_sha256 != report_sha256
                or existing.specialist_identity != specialist_identity
                or existing.rationale != rationale
                or existing.qualification_json != qualification_json
            ):
                return None
            return _computation_attestation_read(existing, is_current=True)

    def get_computation_attestation(
        self,
        attestation_sha256: str,
    ) -> ComputationAttestationRead | None:
        attestation_hash = _sha256_digest(
            attestation_sha256,
            "attestation_sha256",
        )
        with self._sessions() as session:
            stored = session.scalar(
                select(ComputationAttestation).where(
                    ComputationAttestation.attestation_sha256 == attestation_hash
                )
            )
            if stored is None:
                return None
            draft = session.get(Draft, stored.draft_id)
            current = (
                _current_computation_validation_record(session, draft)
                if draft is not None
                else None
            )
            return _computation_attestation_read(
                stored,
                is_current=(
                    current is not None
                    and current.id == stored.validation_record_id
                    and current.report_sha256 == stored.report_sha256
                ),
            )

    def list_current_computation_attestations(
        self,
        draft_id: int,
        *,
        report_sha256: str | None = None,
    ) -> tuple[ComputationAttestationRead, ...]:
        expected_report_hash = (
            _sha256_digest(report_sha256, "report_sha256")
            if report_sha256 is not None
            else None
        )
        with self._sessions() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise DraftNotFoundError(f"draft {draft_id} was not found")
            validation = _current_computation_validation_record(session, draft)
            if validation is None:
                return ()
            if (
                expected_report_hash is not None
                and validation.report_sha256 != expected_report_hash
            ):
                return ()
            records = session.scalars(
                select(ComputationAttestation)
                .where(
                    ComputationAttestation.validation_record_id == validation.id,
                    ComputationAttestation.draft_id == draft.id,
                    ComputationAttestation.edit_count == draft.edit_count,
                    ComputationAttestation.report_sha256 == validation.report_sha256,
                )
                .order_by(ComputationAttestation.id)
            )
            return tuple(
                _computation_attestation_read(record, is_current=True)
                for record in records
            )

    def save_hint_ladder(
        self,
        draft_id: int,
        ladder: HintLadderDraft | Mapping[str, Any],
        *,
        editor: str,
        notes: str = "",
    ) -> HintLadderRecord:
        actor = _actor(editor)
        validated = HintLadderDraft.model_validate(_json_value(ladder))
        with self._sessions.begin() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise DraftNotFoundError(f"draft {draft_id} was not found")
            question = QuestionDraft.model_validate(draft.current_json)
            validated = analyze_hint_leaks(question, validated)
            _validate_hint_ladder(question, validated)
            previous = session.scalars(
                select(HintLadderRecord).where(
                    HintLadderRecord.draft_id == draft.id,
                    HintLadderRecord.edit_count == draft.edit_count,
                )
            ).all()
            version = max((item.version for item in previous), default=0) + 1
            record = HintLadderRecord(
                draft_id=draft.id,
                edit_count=draft.edit_count,
                version=version,
                ladder_json=validated.model_dump(mode="json"),
                confirmations_json={rung.rung.value: False for rung in validated.rungs},
                status="ready_for_review",
                reviewer_notes=notes,
                reviewed_by=actor,
                reviewed_at=utc_now(),
            )
            session.add(record)
            session.flush()
            record_id = record.id
        return self.require_hint_ladder(record_id)

    def require_hint_ladder(self, ladder_id: int) -> HintLadderRecord:
        with self._sessions() as session:
            record = session.get(HintLadderRecord, ladder_id)
            if record is None:
                raise DraftNotFoundError(f"hint ladder {ladder_id} was not found")
            return record

    def review_hint_ladder(
        self,
        draft_id: int,
        *,
        reviewer: str,
        confirmed_rungs: Sequence[str],
        approved: bool,
        notes: str = "",
    ) -> HintLadderRecord:
        actor = _actor(reviewer)
        expected = {"conceptual", "strategic", "specific"}
        confirmed = set(confirmed_rungs)
        if not confirmed.issubset(expected):
            raise ReviewGateError("unknown hint-rung confirmation")
        with self._sessions.begin() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise DraftNotFoundError(f"draft {draft_id} was not found")
            record = session.scalar(
                select(HintLadderRecord)
                .where(
                    HintLadderRecord.draft_id == draft.id,
                    HintLadderRecord.edit_count == draft.edit_count,
                )
                .order_by(HintLadderRecord.version.desc())
            )
            if record is None:
                raise ReviewGateError("generate a hint ladder before reviewing it")
            ladder = HintLadderDraft.model_validate(record.ladder_json)
            _validate_hint_ladder(
                QuestionDraft.model_validate(draft.current_json), ladder
            )
            if approved and confirmed != expected:
                raise ReviewGateError("confirm all three hint rungs before approval")
            if approved and any(rung.answer_leak_detected for rung in ladder.rungs):
                raise ReviewGateError("resolve answer-leak flags before hint approval")
            record.confirmations_json = {
                rung: rung in confirmed for rung in sorted(expected)
            }
            record.status = "approved" if approved else "ready_for_review"
            record.reviewer_notes = notes
            record.reviewed_by = actor
            record.reviewed_at = utc_now()
            record_id = record.id
        return self.require_hint_ladder(record_id)

    def create_or_get_publication(
        self, values: Mapping[str, Any]
    ) -> tuple[Publication, bool]:
        publication_key = str(values["publication_key"])
        with self._sessions() as session:
            existing = session.scalar(
                select(Publication).where(
                    Publication.publication_key == publication_key
                )
            )
            if existing is not None:
                return existing, False
        try:
            with self._sessions.begin() as session:
                publication = Publication(**dict(values))
                session.add(publication)
                session.flush()
                publication_id = publication.id
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(Publication).where(
                        Publication.publication_key == publication_key
                    )
                )
                if existing is None:
                    raise
                return existing, False
        return self.require_publication(publication_id), True

    def create_or_get_publication_guarded(
        self,
        values: Mapping[str, Any],
        *,
        computation_binding: ComputationGateBinding | None,
        computation_evidence: PublicationComputationEvidenceWrite | None,
        engine_binding: EngineValidationBinding | None = None,
        hint_binding: HintLadderBinding | None = None,
    ) -> tuple[Publication, bool]:
        """Atomically reserve publication and freeze exact validated evidence."""

        publication_key = str(values["publication_key"])
        if computation_binding is None:
            if computation_evidence is not None:
                raise ComputationEvidenceError(
                    "computation evidence requires an enforce-mode binding"
                )
        else:
            _validate_publication_evidence_binding(
                computation_binding,
                computation_evidence,
                engine_binding=engine_binding,
            )
        hint_snapshot = values.get("hint_ladder_snapshot_json")
        if (hint_snapshot is None) != (hint_binding is None):
            raise ComputationEvidenceError(
                "hint snapshot and exact hint binding must be supplied together"
            )
        if (
            computation_binding is None
            and engine_binding is None
            and hint_binding is None
        ):
            raise ComputationEvidenceError(
                "guarded publication requires computation, engine, or hint evidence"
            )
        try:
            with self._sessions.begin() as session:
                draft = session.scalar(
                    select(Draft)
                    .where(Draft.id == int(values["draft_id"]))
                    .with_for_update()
                )
                if draft is None:
                    raise DraftNotFoundError(
                        f"draft {values['draft_id']} was not found"
                    )
                if draft.status != ReviewStatus.READY_TO_PUBLISH:
                    raise ReviewGateError(
                        "the exact draft revision is no longer approved"
                    )
                validation = (
                    _verify_computation_gate_binding(
                        session,
                        draft,
                        computation_binding,
                    )
                    if computation_binding is not None
                    else None
                )
                if computation_binding is not None:
                    _verify_computation_approval_evidence(
                        session,
                        draft,
                        computation_binding,
                    )
                _verify_engine_validation_binding(
                    session,
                    draft,
                    engine_binding,
                    computation_validation=validation,
                )
                _verify_hint_ladder_binding(
                    session,
                    draft,
                    hint_binding,
                    snapshot=hint_snapshot,
                )
                existing = session.scalar(
                    select(Publication)
                    .where(Publication.publication_key == publication_key)
                    .with_for_update()
                )
                reserved = existing is None
                publication = existing or Publication(**dict(values))
                if existing is None:
                    session.add(publication)
                    session.flush()
                _verify_publication_revision(publication, draft)
                if computation_binding is not None and computation_binding.scoped:
                    if computation_evidence is None or validation is None:
                        raise ComputationEvidenceError(
                            "scoped publication requires computation evidence"
                        )
                    _store_publication_computation_evidence(
                        session,
                        publication,
                        validation,
                        computation_evidence,
                    )
                elif computation_evidence is not None:
                    raise ComputationEvidenceError(
                        "unscoped publication cannot freeze computation evidence"
                    )
                publication_id = publication.id
        except IntegrityError:
            # A concurrent reservation may win the unique publication key. Recheck
            # the complete guard in a new transaction before returning its row.
            with self._sessions.begin() as session:
                draft = session.scalar(
                    select(Draft)
                    .where(Draft.id == int(values["draft_id"]))
                    .with_for_update()
                )
                existing = session.scalar(
                    select(Publication)
                    .where(Publication.publication_key == publication_key)
                    .with_for_update()
                )
                if draft is None or existing is None:
                    raise
                if draft.status != ReviewStatus.READY_TO_PUBLISH:
                    raise ReviewGateError(
                        "the exact draft revision is no longer approved"
                    )
                validation = (
                    _verify_computation_gate_binding(
                        session,
                        draft,
                        computation_binding,
                    )
                    if computation_binding is not None
                    else None
                )
                if computation_binding is not None:
                    _verify_computation_approval_evidence(
                        session,
                        draft,
                        computation_binding,
                    )
                _verify_engine_validation_binding(
                    session,
                    draft,
                    engine_binding,
                    computation_validation=validation,
                )
                _verify_hint_ladder_binding(
                    session,
                    draft,
                    hint_binding,
                    snapshot=hint_snapshot,
                )
                _verify_publication_revision(existing, draft)
                if computation_binding is not None and computation_binding.scoped:
                    if computation_evidence is None or validation is None:
                        raise ComputationEvidenceError(
                            "scoped publication requires computation evidence"
                        )
                    _store_publication_computation_evidence(
                        session,
                        existing,
                        validation,
                        computation_evidence,
                    )
                elif computation_evidence is not None:
                    raise ComputationEvidenceError(
                        "unscoped publication cannot freeze computation evidence"
                    )
                publication_id = existing.id
                reserved = False
        return self.require_publication(publication_id), reserved

    def get_publication(self, publication_id: int) -> Publication | None:
        with self._sessions() as session:
            return session.scalar(
                select(Publication)
                .options(selectinload(Publication.attempts))
                .where(Publication.id == publication_id)
            )

    def require_publication(self, publication_id: int) -> Publication:
        publication = self.get_publication(publication_id)
        if publication is None:
            raise DraftNotFoundError(f"publication {publication_id} was not found")
        return publication

    def snapshot_publication_computation_evidence(
        self,
        publication_id: int,
        evidence: PublicationComputationEvidenceWrite,
    ) -> PublicationComputationEvidenceRead:
        """Freeze exact validation and attestation evidence for a publication."""

        report_hash = _sha256_digest(evidence.report_sha256, "report_sha256")
        attestation_hashes = tuple(
            _sha256_digest(value, "attestation_sha256")
            for value in evidence.attestation_sha256s
        )
        if len(attestation_hashes) != len(set(attestation_hashes)):
            raise ComputationEvidenceError(
                "publication evidence contains duplicate attestation hashes"
            )
        ordered_hashes = tuple(sorted(attestation_hashes))

        with self._sessions.begin() as session:
            publication = session.scalar(
                select(Publication)
                .where(Publication.id == publication_id)
                .with_for_update()
            )
            if publication is None:
                raise DraftNotFoundError(f"publication {publication_id} was not found")
            existing = session.scalar(
                select(PublicationComputationEvidence).where(
                    PublicationComputationEvidence.publication_id == publication.id
                )
            )
            if existing is not None:
                existing_hashes = tuple(
                    str(value)
                    for value in json.loads(existing.attestation_sha256s_json)
                )
                try:
                    existing_snapshot = json.loads(existing.snapshot_json)
                    existing_engine = existing_snapshot.get("engine_validation")
                except (AttributeError, TypeError, ValueError):
                    existing_engine = object()
                expected_engine = (
                    evidence.engine_validation.snapshot
                    if evidence.engine_validation is not None
                    else None
                )
                if (
                    existing.report_sha256 != report_hash
                    or existing_hashes != ordered_hashes
                    or existing_engine != expected_engine
                ):
                    raise ComputationEvidenceConflictError(
                        "publication computation evidence is already frozen"
                    )
                return _publication_computation_evidence_read(existing)

            publication_draft_hash = _draft_sha256(publication.question_snapshot_json)
            validation = session.scalar(
                select(ComputationValidationRecord)
                .where(
                    ComputationValidationRecord.draft_id == publication.draft_id,
                    ComputationValidationRecord.edit_count == publication.edit_count,
                    ComputationValidationRecord.draft_sha256 == publication_draft_hash,
                    ComputationValidationRecord.report_sha256 == report_hash,
                )
                .order_by(ComputationValidationRecord.id.desc())
            )
            if validation is None:
                raise ComputationEvidenceError(
                    "publication evidence must reference a report for the "
                    "publication's exact draft edit"
                )
            latest_validation = _latest_computation_validation_record(
                session,
                draft_id=publication.draft_id,
                edit_count=publication.edit_count,
                draft_sha256=publication_draft_hash,
            )
            if latest_validation is None or latest_validation.id != validation.id:
                raise ComputationEvidenceError(
                    "publication evidence must reference the latest report for "
                    "the publication's exact draft edit"
                )
            validate_computation_evidence(
                _computation_validation_read(validation, is_current=True),
                require_authorizable=True,
            )
            draft = session.scalar(
                select(Draft).where(Draft.id == publication.draft_id).with_for_update()
            )
            if draft is None:
                raise DraftNotFoundError(f"draft {publication.draft_id} was not found")
            _verify_engine_validation_binding(
                session,
                draft,
                evidence.engine_validation,
                computation_validation=validation,
            )
            attestations: tuple[ComputationAttestation, ...]
            if ordered_hashes:
                attestations = tuple(
                    session.scalars(
                        select(ComputationAttestation)
                        .where(
                            ComputationAttestation.attestation_sha256.in_(
                                ordered_hashes
                            )
                        )
                        .order_by(ComputationAttestation.attestation_sha256)
                    )
                )
                if len(attestations) != len(ordered_hashes):
                    raise ComputationEvidenceError(
                        "publication evidence references an unknown attestation"
                    )
                if any(
                    item.validation_record_id != validation.id
                    or item.draft_id != publication.draft_id
                    or item.edit_count != publication.edit_count
                    or item.report_sha256 != report_hash
                    for item in attestations
                ):
                    raise ComputationEvidenceError(
                        "publication attestations must bind to the exact report"
                    )
            else:
                attestations = ()

            snapshot_json = _publication_computation_snapshot_json(
                publication,
                validation,
                attestations,
                engine_binding=evidence.engine_validation,
            )
            snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            stored = PublicationComputationEvidence(
                publication_id=publication.id,
                validation_record_id=validation.id,
                draft_id=publication.draft_id,
                edit_count=publication.edit_count,
                report_sha256=report_hash,
                validation_status=validation.status,
                attestation_sha256s_json=json.dumps(
                    ordered_hashes,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                snapshot_json=snapshot_json,
                snapshot_sha256=snapshot_hash,
            )
            session.add(stored)
            session.flush()
            return _publication_computation_evidence_read(stored)

    def get_publication_computation_evidence(
        self,
        publication_id: int,
    ) -> PublicationComputationEvidenceRead | None:
        with self._sessions() as session:
            stored = session.scalar(
                select(PublicationComputationEvidence).where(
                    PublicationComputationEvidence.publication_id == publication_id
                )
            )
            return (
                _publication_computation_evidence_read(stored)
                if stored is not None
                else None
            )

    def update_publication(
        self,
        publication_id: int,
        *,
        state: PublicationState,
        **values: Any,
    ) -> Publication:
        allowed = {
            "adapt_question_id",
            "adapt_page_id",
            "hints_synced_at",
            "qti_path",
            "qti_sha256",
            "qti_size",
            "finalized_at",
            "error_code",
            "error_message",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported publication update: {sorted(unknown)}")
        with self._sessions.begin() as session:
            publication = session.get(Publication, publication_id)
            if publication is None:
                raise DraftNotFoundError(f"publication {publication_id} was not found")
            publication.state = state.value
            for name, value in values.items():
                setattr(publication, name, value)
            publication.updated_at = utc_now()
        return self.require_publication(publication_id)

    def record_publication_attempt(
        self,
        publication_id: int,
        *,
        action: str,
        resulting_state: PublicationState,
        http_status: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        response: Mapping[str, Any] | None = None,
    ) -> PublicationAttempt:
        with self._sessions.begin() as session:
            publication = session.get(Publication, publication_id)
            if publication is None:
                raise DraftNotFoundError(f"publication {publication_id} was not found")
            publication.attempt_count += 1
            attempt = PublicationAttempt(
                publication_id=publication.id,
                attempt_number=publication.attempt_count,
                action=action,
                resulting_state=resulting_state.value,
                http_status=http_status,
                error_code=error_code,
                error_message=error_message,
                response_json=_json_value(response) if response is not None else None,
            )
            session.add(attempt)
            session.flush()
            attempt_id = attempt.id
        with self._sessions() as session:
            return session.get(PublicationAttempt, attempt_id)

    def confirm_bloom(
        self, draft_id: int, *, reviewer: str, confirmed: bool = True
    ) -> Draft:
        return self._set_confirmation(
            draft_id, gate="bloom", reviewer=reviewer, confirmed=confirmed
        )

    def confirm_difficulty(
        self, draft_id: int, *, reviewer: str, confirmed: bool = True
    ) -> Draft:
        return self._set_confirmation(
            draft_id, gate="difficulty", reviewer=reviewer, confirmed=confirmed
        )

    def _set_confirmation(
        self,
        draft_id: int,
        *,
        gate: str,
        reviewer: str,
        confirmed: bool,
    ) -> Draft:
        if gate not in {"bloom", "difficulty"}:
            raise ValueError("unknown review gate")
        actor = _actor(reviewer)
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                draft = session.get(Draft, draft_id)
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
                if draft.status == ReviewStatus.REJECTED:
                    raise ReviewTransitionError(
                        "reopen a rejected draft before confirming it"
                    )
                _record_confirmation(
                    draft,
                    gate=gate,
                    actor=actor,
                    confirmed=confirmed,
                    now=now,
                )
                if not confirmed and draft.status == ReviewStatus.READY_TO_PUBLISH:
                    _transition_draft(
                        draft,
                        ReviewStatus.READY_FOR_REVIEW,
                        actor=actor,
                        notes="A required human confirmation was revoked.",
                        now=now,
                    )
        except StaleDataError:
            raise ConcurrentDraftUpdateError(
                "the draft changed during review; reload it before confirming"
            ) from None
        return self.require_draft(draft_id)

    def transition_status(
        self,
        draft_id: int,
        status: ReviewStatus,
        *,
        reviewer: str,
        notes: str = "",
    ) -> Draft:
        actor = _actor(reviewer)
        target = ReviewStatus(status)
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                draft = session.get(Draft, draft_id)
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
                _transition_draft(
                    draft,
                    target,
                    actor=actor,
                    notes=notes,
                    now=now,
                )
        except StaleDataError:
            raise ConcurrentDraftUpdateError(
                "the draft changed during review; reload it before changing status"
            ) from None
        return self.require_draft(draft_id)

    def apply_review_decision(
        self,
        draft_id: int,
        decision: ReviewDecision,
        *,
        reviewer: str,
        computation_binding: ComputationGateBinding | None = None,
    ) -> Draft:
        """Atomically apply API input while retaining independent gate audit events."""

        actor = _actor(reviewer)
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                draft = (
                    session.scalar(
                        select(Draft).where(Draft.id == draft_id).with_for_update()
                    )
                    if computation_binding is not None
                    else session.get(Draft, draft_id)
                )
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
                if computation_binding is not None:
                    _verify_computation_gate_binding(
                        session,
                        draft,
                        computation_binding,
                    )
                if decision.status != ReviewStatus.REJECTED:
                    _record_confirmation(
                        draft,
                        gate="bloom",
                        actor=actor,
                        confirmed=decision.bloom_confirmed,
                        now=now,
                    )
                    _record_confirmation(
                        draft,
                        gate="difficulty",
                        actor=actor,
                        confirmed=decision.difficulty_confirmed,
                        now=now,
                    )
                _transition_draft(
                    draft,
                    decision.status,
                    actor=actor,
                    notes=decision.reviewer_notes,
                    now=now,
                )
                if (
                    computation_binding is not None
                    and decision.status == ReviewStatus.READY_TO_PUBLISH
                ):
                    session.flush()
                    _store_computation_approval_evidence(
                        session,
                        draft,
                        computation_binding,
                        reviewer=actor,
                        approved_at=now,
                    )
        except StaleDataError:
            raise ConcurrentDraftUpdateError(
                "the draft changed during review; reload it before submitting a decision"
            ) from None
        return self.require_draft(draft_id)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _sha256_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ComputationEvidenceError(
            f"{field_name} must be a lowercase SHA-256 hex digest"
        )
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ComputationEvidenceError(
            f"{field_name} must be a lowercase SHA-256 hex digest"
        )
    return normalized


def _opaque_json_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComputationEvidenceError(f"{field_name} must be a JSON string")
    try:
        json.loads(value)
    except (TypeError, ValueError):
        raise ComputationEvidenceError(
            f"{field_name} must contain valid JSON"
        ) from None
    return value


def _bounded_computation_text(value: str, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ComputationEvidenceError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ComputationEvidenceError(f"{field_name} must not be blank")
    if len(normalized) > limit:
        raise ComputationEvidenceError(
            f"{field_name} exceeds the {limit}-character persistence limit"
        )
    return normalized


def draft_content_sha256(current_json: Mapping[str, Any]) -> str:
    """Return the canonical hash used to bind evidence to draft content."""

    return hashlib.sha256(_stable_json_bytes(current_json)).hexdigest()


def _draft_sha256(current_json: Mapping[str, Any]) -> str:
    return draft_content_sha256(current_json)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _TypedComputationEvidence:
    blueprint: AssessmentComputationBlueprint | None
    report: AssessmentValidationReport
    blueprint_sha256: str
    report_sha256: str


_QUALIFIED_COMPUTATION_DEPENDENCIES = {
    "sympy": QUALIFIED_SYMPY_VERSION,
    "pint": QUALIFIED_PINT_VERSION,
    "ucumvert": QUALIFIED_UCUMVERT_VERSION,
    "ucum_profile": UCUM_PROFILE,
    "ucum_essence_sha256": UCUM_ESSENCE_SHA256,
}
_NOT_APPLICABLE_BLUEPRINT = {
    "schema_version": COMPUTATION_SCHEMA_VERSION,
    "profile": None,
}
_LEGACY_UNSUPPORTED_BLUEPRINT = {
    "schema_version": COMPUTATION_SCHEMA_VERSION,
    "reason": "legacy_without_blueprint",
}


def validate_computation_evidence(
    record: ComputationValidationRead,
    *,
    require_authorizable: bool = False,
) -> AssessmentValidationReport:
    """Revalidate stored typed evidence before an enforce-mode decision."""

    expected_evidence_hash = _computation_validation_evidence_sha256(record)
    stored_evidence_hash = _sha256_digest(
        record.evidence_sha256,
        "evidence_sha256",
    )
    if not hmac.compare_digest(expected_evidence_hash, stored_evidence_hash):
        raise ComputationEvidenceError(
            "stored evidence hash does not match the immutable validation envelope"
        )
    typed = _validate_typed_computation_evidence(
        status=record.status,
        schema_version=record.schema_version,
        validator_revision=record.validator_revision,
        blueprint_json=record.blueprint_json,
        report_json=record.report_json,
        dependency_versions_json=record.dependency_versions_json,
        seed_plan_json=record.seed_plan_json,
        engine_evidence_json=record.engine_evidence_json,
        container_digest=record.container_digest,
        require_authorizable=require_authorizable,
        expected_draft_sha256=record.draft_sha256,
    )
    if typed.blueprint_sha256 != record.blueprint_sha256:
        raise ComputationEvidenceError(
            "stored blueprint hash does not match the typed blueprint"
        )
    if typed.report_sha256 != record.report_sha256:
        raise ComputationEvidenceError(
            "stored report hash does not match the typed validation report"
        )
    return typed.report


def computation_formula_adapter_promotion_sha256(
    record: ComputationValidationRead,
) -> str | None:
    """Return the persisted formula-promotion binding for an atomic gate."""

    try:
        payload = json.loads(
            _opaque_json_string(
                record.engine_evidence_json,
                "engine_evidence_json",
            )
        )
        if not isinstance(payload, dict):
            raise ValueError
        top_level = payload.get("formula_adapter_promotion")
        nested_receipt = payload.get("native_receipt")
        if top_level is None:
            if nested_receipt is not None:
                receipt = NativeEngineRunnerReceipt.model_validate(nested_receipt)
                if receipt.formula_adapter_promotion is not None:
                    raise ValueError
            return None
        promotion = FormulaAdapterPromotionEvidence.model_validate(top_level)
        if nested_receipt is not None:
            receipt = NativeEngineRunnerReceipt.model_validate(nested_receipt)
            if receipt.formula_adapter_promotion != promotion:
                raise ValueError
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
        raise ComputationEvidenceError(
            "formula promotion binding requires one consistent typed identity"
        ) from None
    return promotion.identity_sha256


def _validate_typed_computation_evidence(
    *,
    status: str,
    schema_version: str,
    validator_revision: str,
    blueprint_json: str,
    report_json: str,
    dependency_versions_json: str,
    seed_plan_json: str,
    engine_evidence_json: str,
    container_digest: str,
    require_authorizable: bool,
    expected_draft_sha256: str | None = None,
) -> _TypedComputationEvidence:
    raw_status = _bounded_computation_text(status, "status", 40)
    raw_schema = _bounded_computation_text(schema_version, "schema_version", 100)
    raw_validator = _bounded_computation_text(
        validator_revision, "validator_revision", 100
    )
    blueprint_text = _opaque_json_string(blueprint_json, "blueprint_json")
    report_text = _opaque_json_string(report_json, "report_json")
    dependencies_text = _opaque_json_string(
        dependency_versions_json,
        "dependency_versions_json",
    )
    seed_plan_text = _opaque_json_string(seed_plan_json, "seed_plan_json")
    engine_evidence_text = _opaque_json_string(
        engine_evidence_json,
        "engine_evidence_json",
    )
    try:
        report_payload = json.loads(report_text)
        report = AssessmentValidationReport.model_validate(report_payload)
    except (TypeError, ValueError, ValidationError):
        raise ComputationEvidenceError(
            "report_json must be a typed AssessmentValidationReport"
        ) from None
    if raw_status != report.status.value:
        raise ComputationEvidenceError(
            "computation status column does not match report status"
        )
    if raw_schema != report.schema_version or raw_schema != COMPUTATION_SCHEMA_VERSION:
        raise ComputationEvidenceError(
            "computation schema version does not match the typed report"
        )
    if raw_validator != report.validator_version or raw_validator != VALIDATOR_VERSION:
        raise ComputationEvidenceError(
            "validator revision does not match the typed report"
        )

    blueprint_payload = json.loads(blueprint_text)
    blueprint: AssessmentComputationBlueprint | None
    try:
        blueprint = AssessmentComputationBlueprint.model_validate(blueprint_payload)
    except (TypeError, ValueError, ValidationError):
        blueprint = None
    if blueprint is not None:
        blueprint_hash = canonical_blueprint_hash(blueprint)
        if blueprint.schema_version != raw_schema:
            raise ComputationEvidenceError(
                "blueprint schema version does not match the report"
            )
        if report.reason is not None:
            raise ComputationEvidenceError(
                "typed blueprint reports cannot carry a legacy reason"
            )
    elif (
        report.status == ValidationStatus.NOT_APPLICABLE
        and blueprint_payload == _NOT_APPLICABLE_BLUEPRINT
    ) or (
        report.status == ValidationStatus.UNSUPPORTED
        and blueprint_payload == _LEGACY_UNSUPPORTED_BLUEPRINT
        and report.reason == "legacy_without_blueprint"
        and any(check.code == "legacy_without_blueprint" for check in report.checks)
    ):
        blueprint_hash = hashlib.sha256(
            _stable_json_bytes(blueprint_payload)
        ).hexdigest()
    else:
        raise ComputationEvidenceError(
            "blueprint_json must be a typed AssessmentComputationBlueprint"
        )
    if report.blueprint_hash != blueprint_hash:
        raise ComputationEvidenceError(
            "validation report blueprint hash does not match blueprint_json"
        )
    if report.result is not None:
        if (
            report.result.schema_version != raw_schema
            or report.result.blueprint_hash != blueprint_hash
        ):
            raise ComputationEvidenceError(
                "computation result is not bound to the typed blueprint"
            )

    dependencies = json.loads(dependencies_text)
    seed_plan = json.loads(seed_plan_text)
    engine_evidence = json.loads(engine_evidence_text)
    if not isinstance(dependencies, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in dependencies.items()
    ):
        raise ComputationEvidenceError(
            "dependency_versions_json must be a string-to-string object"
        )
    if dependencies != report.dependencies:
        raise ComputationEvidenceError(
            "dependency versions do not match the typed report"
        )
    if seed_plan != report.seed_plan:
        raise ComputationEvidenceError("seed plan does not match the typed report")
    if not isinstance(engine_evidence, dict):
        raise ComputationEvidenceError("engine_evidence_json must be an object")
    _validate_report_semantics(
        report,
        blueprint=blueprint,
        require_authorizable=require_authorizable,
    )
    _validate_native_engine_evidence(
        report,
        blueprint=blueprint,
        engine_evidence=engine_evidence,
        expected_draft_sha256=expected_draft_sha256,
        require_authorizable=require_authorizable,
    )

    if require_authorizable:
        if blueprint is None:
            raise ComputationEvidenceError(
                "legacy or not-applicable evidence cannot authorize enforce mode"
            )
        if report.status not in {
            ValidationStatus.VALIDATED,
            ValidationStatus.PARTIALLY_VALIDATED,
            ValidationStatus.UNSUPPORTED,
        }:
            raise ComputationEvidenceError(
                "validation status cannot authorize enforce mode"
            )
        if dependencies != _QUALIFIED_COMPUTATION_DEPENDENCIES:
            raise ComputationEvidenceError(
                "dependency evidence does not match the qualified runtime"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", container_digest):
            raise ComputationEvidenceError(
                "a qualified container digest is required in enforce mode"
            )

    return _TypedComputationEvidence(
        blueprint=blueprint,
        report=report,
        blueprint_sha256=blueprint_hash,
        report_sha256=hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
    )


def _validate_report_semantics(
    report: AssessmentValidationReport,
    *,
    blueprint: AssessmentComputationBlueprint | None,
    require_authorizable: bool,
) -> None:
    has_blueprint = blueprint is not None
    check_codes = [check.code for check in report.checks]
    if len(check_codes) != len(set(check_codes)):
        raise ComputationEvidenceError(
            "typed computation reports cannot contain duplicate check codes"
        )
    check_statuses = [check.status for check in report.checks]
    if report.status == ValidationStatus.VALIDATED:
        if (
            report.result is None
            or not report.checks
            or any(status != CheckStatus.PASSED for status in check_statuses)
        ):
            raise ComputationEvidenceError(
                "validated reports require a result and only passed checks"
            )
    elif report.status == ValidationStatus.PARTIALLY_VALIDATED:
        if (
            report.result is None
            or CheckStatus.FAILED in check_statuses
            or CheckStatus.INCONCLUSIVE not in check_statuses
        ):
            raise ComputationEvidenceError(
                "partially_validated reports require a result, no failed check, "
                "and at least one inconclusive check"
            )
    elif report.status == ValidationStatus.UNSUPPORTED:
        if (
            report.result is not None
            or CheckStatus.INCONCLUSIVE not in check_statuses
            or CheckStatus.FAILED in check_statuses
        ):
            raise ComputationEvidenceError(
                "unsupported reports require no result, at least one inconclusive "
                "check, and no failed checks"
            )
    elif report.status == ValidationStatus.VALIDATION_FAILED:
        if report.result is not None or CheckStatus.FAILED not in check_statuses:
            raise ComputationEvidenceError(
                "validation_failed reports require no result and a failed check"
            )
    elif report.status == ValidationStatus.NOT_APPLICABLE:
        if report.result is not None or report.seed_plan or has_blueprint:
            raise ComputationEvidenceError(
                "not_applicable reports cannot contain a result, seeds, or blueprint"
            )
    if has_blueprint and len(report.seed_plan) != 25:
        raise ComputationEvidenceError(
            "typed computation reports require exactly 25 deterministic seeds"
        )
    if blueprint is not None:
        checks_by_code = {check.code: check.status for check in report.checks}
        external = blueprint.profile.delivery.value in {"webwork", "imathas"}
        native_status = checks_by_code.get("native_engine")
        if external and report.status == ValidationStatus.VALIDATED:
            if native_status != CheckStatus.PASSED:
                raise ComputationEvidenceError(
                    "validated external-engine reports require a passed "
                    "server-trusted native-engine receipt"
                )
        elif external and report.status == ValidationStatus.PARTIALLY_VALIDATED:
            if native_status not in {
                CheckStatus.PASSED,
                CheckStatus.INCONCLUSIVE,
            }:
                raise ComputationEvidenceError(
                    "partially validated external-engine reports require passed "
                    "or inconclusive native-engine evidence"
                )
        elif not external and native_status is not None:
            raise ComputationEvidenceError(
                "native-engine checks are invalid for nonexternal delivery"
            )
    if (
        require_authorizable
        and blueprint is not None
        and report.status
        in {
            ValidationStatus.VALIDATED,
            ValidationStatus.PARTIALLY_VALIDATED,
        }
    ):
        checks_by_code = {check.code: check.status for check in report.checks}
        required_passes = {"computation", "presentation_binding"}
        if blueprint.profile.delivery.value == "multiple_choice":
            required_passes.add("choices")
        else:
            required_passes.add("candidate")
        if blueprint.operation.value == "equivalent":
            required_passes.add("equivalence")
            required_passes.discard("candidate")
        elif blueprint.operation.value == "solve":
            required_passes.add("candidate_solutions")
            required_passes.discard("candidate")
        if blueprint.profile.family.value == "unit":
            required_passes.update({"unit_dimensions", "unit_round_trip"})
        if any(variable.minimum is not None for variable in blueprint.variables):
            required_passes.add("deterministic_samples")
        missing = sorted(
            code
            for code in required_passes
            if checks_by_code.get(code) != CheckStatus.PASSED
        )
        if missing:
            raise ComputationEvidenceError(
                "computation report is missing required passed checks: "
                + ", ".join(missing)
            )


def _validate_native_engine_evidence(
    report: AssessmentValidationReport,
    *,
    blueprint: AssessmentComputationBlueprint | None,
    engine_evidence: Mapping[str, Any],
    expected_draft_sha256: str | None,
    require_authorizable: bool,
) -> None:
    """Bind a passed native check to the exact persisted qualified receipt.

    The receipt is trusted only because the application acquired it from the
    configured local Unix socket and verified it against the source-controlled
    promotion registry.  Persistence independently rechecks every immutable
    identity so caller-supplied report JSON cannot promote itself.
    """

    native_checks = [check for check in report.checks if check.code == "native_engine"]
    nested_receipt = engine_evidence.get("native_receipt")
    formula_evidence = _validate_formula_adapter_evidence(
        report,
        blueprint=blueprint,
        engine_evidence=engine_evidence,
        require_authorizable=require_authorizable,
    )
    if blueprint is None or blueprint.profile.delivery.value not in {
        "webwork",
        "imathas",
    }:
        if native_checks or nested_receipt is not None:
            raise ComputationEvidenceError(
                "native-engine evidence is invalid without an external typed blueprint"
            )
        return

    if len(native_checks) > 1:
        raise ComputationEvidenceError(
            "typed computation reports cannot contain duplicate native checks"
        )
    native_check = native_checks[0] if native_checks else None
    if native_check is None or native_check.status != CheckStatus.PASSED:
        if nested_receipt is not None:
            raise ComputationEvidenceError(
                "persisted native receipt requires a passed native-engine check"
            )
        return
    if nested_receipt is None:
        raise ComputationEvidenceError(
            "passed native-engine check requires its exact persisted receipt"
        )

    try:
        receipt = NativeEngineRunnerReceipt.model_validate(nested_receipt)
        observed_evidence = NativeEngineEvidence.model_validate(native_check.details)
    except (TypeError, ValueError, ValidationError):
        raise ComputationEvidenceError(
            "native-engine evidence must contain a complete typed receipt"
        ) from None
    expected_evidence = _native_evidence_for_receipt(receipt)
    if observed_evidence != expected_evidence:
        raise ComputationEvidenceError(
            "native-engine report details do not match the persisted receipt"
        )

    expected_draft_hash = (
        _sha256_digest(expected_draft_sha256, "expected_draft_sha256")
        if expected_draft_sha256 is not None
        else None
    )
    if (
        receipt.engine != blueprint.profile.delivery.value
        or receipt.blueprint_sha256 != canonical_blueprint_hash(blueprint)
        or (
            expected_draft_hash is not None
            and receipt.draft_sha256 != expected_draft_hash
        )
        or receipt.seeds != report.seed_plan
        or receipt.seed_plan_sha256 != seed_plan_sha256(report.seed_plan)
    ):
        raise ComputationEvidenceError(
            "native-engine receipt does not match the exact draft, blueprint, "
            "or deterministic seed plan"
        )

    previews = engine_evidence.get("previews")
    preview_seeds = (
        [preview.get("seed") for preview in previews]
        if isinstance(previews, list)
        and all(isinstance(preview, dict) for preview in previews)
        else None
    )
    if (
        engine_evidence.get("engine") != receipt.engine
        or engine_evidence.get("compiler_version") != receipt.compiler_version
        or engine_evidence.get("source_sha256") != receipt.source_sha256
        or engine_evidence.get("seed_count") != 25
        or len(receipt.seeds) != 25
        or len(set(receipt.seeds)) != 25
        or preview_seeds != report.seed_plan
    ):
        raise ComputationEvidenceError(
            "native-engine receipt does not match the persisted compiler artifact"
        )
    if report.result is None:
        raise ComputationEvidenceError(
            "passed native-engine receipt requires typed computation ground truth"
        )
    try:
        verify_native_engine_observations(
            blueprint=blueprint,
            result=report.result,
            receipt=receipt,
        )
    except NativeEvidenceVerificationError:
        raise ComputationEvidenceError(
            "native-engine observations do not match typed ground truth"
        ) from None

    if receipt.answer_kind == "formula":
        if (
            formula_evidence is None
            or receipt.formula_adapter_promotion != formula_evidence
            or formula_evidence.family != blueprint.profile.family.value
            or formula_evidence.operation != blueprint.operation.value
        ):
            raise ComputationEvidenceError(
                "formula receipt does not match the independently persisted "
                "promotion identity"
            )
    elif formula_evidence is not None:
        raise ComputationEvidenceError(
            "numeric native receipt cannot carry formula promotion evidence"
        )

    promotion = qualified_native_engine_runner(
        runner_id=receipt.runner_id,
        engine=receipt.engine,
        compiler_version=receipt.compiler_version,
        answer_kind=receipt.answer_kind,
    )
    if promotion is None:
        raise ComputationEvidenceError(
            "native-engine receipt is not bound to a source-controlled qualification"
        )
    promoted_identity = {
        "runner_id": promotion.runner_id,
        "runner_version": promotion.runner_version,
        "runner_manifest_sha256": promotion.runner_manifest_sha256,
        "runner_image_digest": promotion.runner_image_digest,
        "qualification_report_sha256": promotion.qualification_report_sha256,
        "promotion_approval_sha256": promotion.promotion_approval_sha256,
        "engine": promotion.engine,
        "compiler_version": promotion.compiler_version,
        "answer_kind": promotion.answer_kind,
        "native_grader": promotion.native_grader,
        "engine_image_digest": promotion.engine_image_digest,
        "adapter_image_digest": promotion.adapter_image_digest,
        "network_attestation_sha256": promotion.network_attestation_sha256,
    }
    observed_identity = {field: getattr(receipt, field) for field in promoted_identity}
    if observed_identity != promoted_identity:
        raise ComputationEvidenceError(
            "native-engine receipt identity does not match its reviewed promotion"
        )


def _validate_formula_adapter_evidence(
    report: AssessmentValidationReport,
    *,
    blueprint: AssessmentComputationBlueprint | None,
    engine_evidence: Mapping[str, Any],
    require_authorizable: bool,
) -> FormulaAdapterPromotionEvidence | None:
    """Validate formula promotion independently of native-runner availability."""

    raw_promotion = engine_evidence.get("formula_adapter_promotion")
    artifact_fields = {
        "engine",
        "compiler_version",
        "source_sha256",
        "seed_count",
        "previews",
        "answer_kind",
    }
    artifact_present = any(field in engine_evidence for field in artifact_fields)
    external = blueprint is not None and blueprint.profile.delivery.value in {
        "webwork",
        "imathas",
    }
    if not artifact_present:
        if raw_promotion is not None:
            raise ComputationEvidenceError(
                "formula promotion evidence requires a compiled engine artifact"
            )
        return None
    if not external or blueprint is None or report.result is None:
        if raw_promotion is not None:
            raise ComputationEvidenceError(
                "formula promotion evidence requires a computed external item"
            )
        return None

    referenced = _expression_symbol_names(
        report.result.answer_expression or blueprint.expression
    )
    response_symbols = {
        variable.name
        for variable in blueprint.variables
        if (
            variable.minimum is None
            and variable.name in referenced
            and variable.name not in blueprint.substitutions
        )
    }
    expected_answer_kind = "formula" if response_symbols else "numeric"
    if engine_evidence.get("answer_kind") != expected_answer_kind:
        raise ComputationEvidenceError(
            "compiled answer kind does not match the typed computation blueprint"
        )
    if expected_answer_kind == "numeric":
        if raw_promotion is not None:
            raise ComputationEvidenceError(
                "numeric compiler artifact cannot carry formula promotion evidence"
            )
        return None
    if raw_promotion is None:
        raise ComputationEvidenceError(
            "formula compiler artifact requires its exact persisted promotion identity"
        )
    try:
        promotion = FormulaAdapterPromotionEvidence.model_validate(raw_promotion)
    except (TypeError, ValueError, ValidationError):
        raise ComputationEvidenceError(
            "formula compiler artifact has malformed promotion evidence"
        ) from None
    if (
        promotion.engine != engine_evidence.get("engine")
        or promotion.compiler_version != engine_evidence.get("compiler_version")
        or promotion.engine != blueprint.profile.delivery.value
        or promotion.family != blueprint.profile.family.value
        or promotion.operation != blueprint.operation.value
    ):
        raise ComputationEvidenceError(
            "formula promotion identity does not match its typed compiler artifact"
        )
    if require_authorizable:
        current = qualified_formula_adapter(
            promotion.engine,
            promotion.compiler_version,
            family=blueprint.profile.family.value,
            operation=blueprint.operation.value,
        )
        if current is None:
            raise ComputationEvidenceError(
                "formula adapter promotion is unavailable or revoked"
            )
        expected = formula_adapter_promotion_identity(
            current,
            compiler_version=promotion.compiler_version,
            family=blueprint.profile.family.value,
            operation=blueprint.operation.value,
        )
        expected["identity_sha256"] = current_formula_adapter_promotion_sha256(
            current,
            compiler_version=promotion.compiler_version,
            family=blueprint.profile.family.value,
            operation=blueprint.operation.value,
        )
        if promotion.model_dump(mode="json", exclude_none=False) != expected:
            raise ComputationEvidenceError(
                "formula promotion identity does not match its current promotion"
            )
    return promotion


def _expression_symbol_names(node: Any) -> set[str]:
    """Return symbol names from a previously validated typed expression node."""

    names: set[str] = set()
    symbol = getattr(node, "symbol", None)
    if isinstance(symbol, str):
        names.add(symbol)
    for child in getattr(node, "args", ()):
        names.update(_expression_symbol_names(child))
    return names


def _native_evidence_for_receipt(
    receipt: NativeEngineRunnerReceipt,
) -> NativeEngineEvidence:
    return NativeEngineEvidence(
        engine=receipt.engine,
        compiler_version=receipt.compiler_version,
        source_sha256=receipt.source_sha256,
        receipt_sha256=receipt.receipt_sha256,
        seeds_validated=receipt.seeds_validated,
        passed=receipt.passed,
        server_verified=True,
        answer_kind=receipt.answer_kind,
        native_grader=receipt.native_grader,
        blueprint_sha256=receipt.blueprint_sha256,
        draft_sha256=receipt.draft_sha256,
        request_sha256=receipt.request_sha256,
        seed_plan_sha256=receipt.seed_plan_sha256,
        seed_receipts_sha256=receipt.seed_receipts_sha256,
        runner_id=receipt.runner_id,
        runner_version=receipt.runner_version,
        runner_manifest_sha256=receipt.runner_manifest_sha256,
        runner_image_digest=receipt.runner_image_digest,
        qualification_report_sha256=receipt.qualification_report_sha256,
        promotion_approval_sha256=receipt.promotion_approval_sha256,
        engine_image_digest=receipt.engine_image_digest,
        adapter_image_digest=receipt.adapter_image_digest,
        formula_adapter_promotion=receipt.formula_adapter_promotion,
        network_attestation_sha256=receipt.network_attestation_sha256,
        correct_answer_accepted=receipt.correct_answer_accepted,
        wrong_answer_rejected=receipt.wrong_answer_rejected,
        rendered=receipt.rendered,
        render_sha256=receipt.render_sha256,
        repeat_render_sha256=receipt.repeat_render_sha256,
        warnings_count=receipt.warnings_count,
        errors_count=receipt.errors_count,
        outbound_request_count=receipt.outbound_request_count,
    )


def _new_computation_validation_record(
    draft: Draft,
    *,
    edit_count: int,
    source_sha256: str,
    value: ComputationValidationWrite,
) -> ComputationValidationRecord:
    if edit_count < 0:
        raise ComputationEvidenceError("edit_count must not be negative")
    if (
        isinstance(value.duration_ms, bool)
        or not isinstance(value.duration_ms, int)
        or value.duration_ms < 0
    ):
        raise ComputationEvidenceError("duration_ms must be a non-negative integer")
    typed = _validate_typed_computation_evidence(
        status=value.status,
        schema_version=value.schema_version,
        validator_revision=value.validator_revision,
        blueprint_json=value.blueprint_json,
        report_json=value.report_json,
        dependency_versions_json=value.dependency_versions_json,
        seed_plan_json=value.seed_plan_json,
        engine_evidence_json=value.engine_evidence_json,
        container_digest=value.container_digest,
        require_authorizable=False,
        expected_draft_sha256=_draft_sha256(draft.current_json),
    )
    record = ComputationValidationRecord(
        draft_id=draft.id,
        edit_count=edit_count,
        source_sha256=_sha256_digest(source_sha256, "source_sha256"),
        draft_sha256=_draft_sha256(draft.current_json),
        blueprint_sha256=typed.blueprint_sha256,
        report_sha256=typed.report_sha256,
        evidence_sha256="0" * 64,
        status=typed.report.status.value,
        schema_version=typed.report.schema_version,
        validator_revision=typed.report.validator_version,
        blueprint_json=value.blueprint_json,
        report_json=value.report_json,
        dependency_versions_json=value.dependency_versions_json,
        container_digest=_bounded_computation_text(
            value.container_digest,
            "container_digest",
            255,
        ),
        seed_plan_json=value.seed_plan_json,
        duration_ms=int(value.duration_ms),
        engine_evidence_json=value.engine_evidence_json,
    )
    record.evidence_sha256 = _computation_validation_evidence_sha256(record)
    return record


def _computation_validation_evidence_sha256(
    record: ComputationValidationRecord | ComputationValidationRead,
) -> str:
    """Hash the complete immutable validator envelope, excluding DB metadata."""

    material = {
        "draft_id": record.draft_id,
        "edit_count": record.edit_count,
        "source_sha256": record.source_sha256,
        "draft_sha256": record.draft_sha256,
        "blueprint_sha256": record.blueprint_sha256,
        "report_sha256": record.report_sha256,
        "status": record.status,
        "schema_version": record.schema_version,
        "validator_revision": record.validator_revision,
        "blueprint_json": record.blueprint_json,
        "report_json": record.report_json,
        "dependency_versions_json": record.dependency_versions_json,
        "container_digest": record.container_digest,
        "seed_plan_json": record.seed_plan_json,
        "duration_ms": record.duration_ms,
        "engine_evidence_json": record.engine_evidence_json,
    }
    return hashlib.sha256(_stable_json_bytes(material)).hexdigest()


def _computation_attestation_sha256(
    *,
    validation_record_id: int,
    draft_id: int,
    edit_count: int,
    report_sha256: str,
    specialist_identity: str,
    rationale: str,
    qualification_json: str,
) -> str:
    return hashlib.sha256(
        _stable_json_bytes(
            {
                "validation_record_id": validation_record_id,
                "draft_id": draft_id,
                "edit_count": edit_count,
                "report_sha256": report_sha256,
                "specialist_identity": specialist_identity,
                "rationale": rationale,
                "qualification_json": qualification_json,
            }
        )
    ).hexdigest()


def _same_computation_validation(
    left: ComputationValidationRecord,
    right: ComputationValidationRecord,
) -> bool:
    fields = (
        "draft_id",
        "edit_count",
        "source_sha256",
        "draft_sha256",
        "blueprint_sha256",
        "report_sha256",
        "evidence_sha256",
        "status",
        "schema_version",
        "validator_revision",
        "blueprint_json",
        "report_json",
        "dependency_versions_json",
        "container_digest",
        "seed_plan_json",
        "duration_ms",
        "engine_evidence_json",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _current_computation_validation_record(
    session: Session,
    draft: Draft,
) -> ComputationValidationRecord | None:
    return _latest_computation_validation_record(
        session,
        draft_id=draft.id,
        edit_count=draft.edit_count,
        draft_sha256=_draft_sha256(draft.current_json),
    )


def _latest_computation_validation_record(
    session: Session,
    *,
    draft_id: int,
    edit_count: int,
    draft_sha256: str,
) -> ComputationValidationRecord | None:
    return session.scalar(
        select(ComputationValidationRecord)
        .where(
            ComputationValidationRecord.draft_id == draft_id,
            ComputationValidationRecord.edit_count == edit_count,
            ComputationValidationRecord.draft_sha256 == draft_sha256,
        )
        .order_by(ComputationValidationRecord.id.desc())
    )


def validate_computation_engine_binding(
    record: ComputationValidationRead,
    binding: EngineValidationBinding,
) -> None:
    """Verify report engine evidence against one freshly compiled artifact."""

    if (
        record.draft_id != binding.draft_id
        or record.edit_count != binding.expected_edit_count
        or record.draft_sha256
        != _sha256_digest(binding.expected_draft_sha256, "expected_draft_sha256")
    ):
        raise ComputationEvidenceError(
            "computation engine evidence does not match the exact draft revision"
        )
    validate_computation_evidence(record, require_authorizable=True)
    _validate_engine_evidence_json(record.engine_evidence_json, binding)


def _verify_engine_validation_binding(
    session: Session,
    draft: Draft,
    binding: EngineValidationBinding | None,
    *,
    computation_validation: ComputationValidationRecord | None,
) -> EngineValidationRecord | None:
    """Recheck the exact compiled artifact while the draft row is locked."""

    question = QuestionDraft.model_validate(draft.current_json)
    external = question.item_type in {
        AssessmentItemType.WEBWORK,
        AssessmentItemType.IMATHAS,
    }
    if not external:
        if binding is not None:
            raise ComputationEvidenceError(
                "nonexternal publication cannot include engine evidence"
            )
        return None
    if binding is None:
        raise ReviewGateError(
            "external-engine publication requires exact compiled-source evidence"
        )
    expected_draft_hash = _sha256_digest(
        binding.expected_draft_sha256,
        "expected_draft_sha256",
    )
    if (
        binding.draft_id != draft.id
        or binding.expected_edit_count != draft.edit_count
        or expected_draft_hash != _draft_sha256(draft.current_json)
    ):
        raise ConcurrentDraftUpdateError(
            "the draft changed after engine validation; reload and retry"
        )
    current = session.scalar(
        select(EngineValidationRecord)
        .where(
            EngineValidationRecord.draft_id == draft.id,
            EngineValidationRecord.edit_count == draft.edit_count,
        )
        .order_by(EngineValidationRecord.id.desc())
        .limit(1)
        .with_for_update()
    )
    if current is None:
        raise ReviewGateError(
            "external-engine publication requires current engine validation"
        )
    if (
        isinstance(binding.validation_record_id, bool)
        or binding.validation_record_id < 1
        or current.id != binding.validation_record_id
        or current.status != "passed"
        or current.seed_count < 25
        or current.seed_count != binding.seed_count
        or current.engine != binding.engine
        or current.compiler_version != binding.compiler_version
        or current.source_sha256
        != _sha256_digest(binding.source_sha256, "source_sha256")
        or binding.engine != question.item_type.value
    ):
        raise ConcurrentDraftUpdateError(
            "engine validation or compiled source changed; reload and revalidate"
        )
    if computation_validation is not None:
        _validate_engine_evidence_json(
            computation_validation.engine_evidence_json,
            binding,
        )
    return current


def _verify_hint_ladder_binding(
    session: Session,
    draft: Draft,
    binding: HintLadderBinding | None,
    *,
    snapshot: Any,
) -> HintLadderRecord | None:
    """Recheck the newest approved hint version while the draft row is locked."""

    if binding is None:
        if snapshot is not None:
            raise ComputationEvidenceError(
                "hint snapshot requires an exact hint-ladder binding"
            )
        return None
    expected_evidence_hash = _sha256_digest(
        binding.evidence_sha256,
        "hint_evidence_sha256",
    )
    current = session.scalar(
        select(HintLadderRecord)
        .where(
            HintLadderRecord.draft_id == draft.id,
            HintLadderRecord.edit_count == draft.edit_count,
        )
        .order_by(HintLadderRecord.version.desc(), HintLadderRecord.id.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        current is None
        or binding.record_id != current.id
        or binding.draft_id != draft.id
        or binding.expected_edit_count != draft.edit_count
        or binding.version != current.version
        or hint_ladder_evidence_sha256(current) != expected_evidence_hash
    ):
        raise ConcurrentDraftUpdateError(
            "hint approval changed; reload and review the current ladder"
        )
    confirmations = current.confirmations_json
    required = {"conceptual", "strategic", "specific"}
    if (
        current.status != "approved"
        or not isinstance(confirmations, dict)
        or set(confirmations) != required
        or any(confirmations.get(rung) is not True for rung in required)
    ):
        raise ReviewGateError(
            "the current three-rung hint ladder is not fully approved"
        )
    try:
        ladder = HintLadderDraft.model_validate(current.ladder_json)
        _validate_hint_ladder(
            QuestionDraft.model_validate(draft.current_json),
            ladder,
        )
    except (DraftGroundingError, ValidationError) as exc:
        raise ReviewGateError(
            "the current hint ladder failed typed grounding validation"
        ) from exc
    if any(rung.answer_leak_detected for rung in ladder.rungs):
        raise ReviewGateError(
            "the current hint ladder contains an unresolved answer-leak flag"
        )
    if snapshot != approved_hint_ladder_snapshot(current):
        raise ConcurrentDraftUpdateError(
            "hint publication snapshot changed; reload and retry"
        )
    return current


def _validate_engine_evidence_json(
    engine_evidence_json: str,
    binding: EngineValidationBinding,
) -> None:
    try:
        payload = json.loads(
            _opaque_json_string(engine_evidence_json, "engine_evidence_json")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ComputationEvidenceError(
            "computation engine evidence must be a JSON object"
        ) from None
    if not isinstance(payload, dict):
        raise ComputationEvidenceError(
            "computation engine evidence must be a JSON object"
        )
    observed = {
        "engine": payload.get("engine"),
        "compiler_version": payload.get("compiler_version"),
        "source_sha256": payload.get("source_sha256"),
    }
    if observed != binding.artifact_identity:
        raise ComputationEvidenceError(
            "computation engine evidence does not match the compiled artifact"
        )


def _verify_computation_gate_binding(
    session: Session,
    draft: Draft,
    binding: ComputationGateBinding,
) -> ComputationValidationRecord | None:
    """Fail closed unless an enforce decision still matches inside the write txn."""

    expected_hash = _sha256_digest(
        binding.expected_draft_sha256,
        "expected_draft_sha256",
    )
    if (
        draft.edit_count != binding.expected_edit_count
        or _draft_sha256(draft.current_json) != expected_hash
    ):
        raise ConcurrentDraftUpdateError(
            "the draft changed after computation validation; reload and retry"
        )
    question = QuestionDraft.model_validate(draft.current_json)
    current = _current_computation_validation_record(session, draft)
    computational_type = question.item_type in {
        AssessmentItemType.NUMERICAL,
        AssessmentItemType.WEBWORK,
        AssessmentItemType.IMATHAS,
    }

    if not binding.scoped:
        if (
            binding.formula_adapter_promotion_sha256 is not None
            or binding.runtime_image_reference is not None
            or binding.runtime_container_digest is not None
            or binding.runtime_promotion_sha256 is not None
        ):
            raise ComputationEvidenceError(
                "unscoped computation binding cannot claim promotion identities"
            )
        if computational_type:
            raise ReviewGateError(
                "a computational item requires current enforce-mode evidence"
            )
        if binding.report_sha256 is None:
            if (
                binding.validation_record_id is not None
                or binding.evidence_sha256 is not None
            ):
                raise ComputationEvidenceError(
                    "unscoped no-report binding cannot include evidence identity"
                )
            any_evidence = session.scalar(
                select(ComputationValidationRecord.id)
                .where(ComputationValidationRecord.draft_id == draft.id)
                .limit(1)
            )
            if any_evidence is not None:
                raise ConcurrentDraftUpdateError(
                    "computation evidence changed; reload and retry"
                )
            return None
        expected_report_hash = _sha256_digest(
            binding.report_sha256,
            "report_sha256",
        )
        expected_evidence_hash = (
            _sha256_digest(binding.evidence_sha256, "evidence_sha256")
            if binding.evidence_sha256 is not None
            else None
        )
        if (
            current is None
            or binding.validation_record_id is None
            or expected_evidence_hash is None
            or current.id != binding.validation_record_id
            or current.report_sha256 != expected_report_hash
            or current.evidence_sha256 != expected_evidence_hash
        ):
            raise ConcurrentDraftUpdateError(
                "computation evidence changed; reload and retry"
            )
        report = validate_computation_evidence(
            _computation_validation_read(current, is_current=True)
        )
        if report.status != ValidationStatus.NOT_APPLICABLE:
            raise ReviewGateError("unscoped enforce evidence must be not_applicable")
        return current

    if (
        current is None
        or binding.report_sha256 is None
        or binding.validation_record_id is None
        or binding.evidence_sha256 is None
    ):
        raise ReviewGateError(
            "current computation evidence is required in enforce mode"
        )
    expected_report_hash = _sha256_digest(
        binding.report_sha256,
        "report_sha256",
    )
    expected_evidence_hash = _sha256_digest(
        binding.evidence_sha256,
        "evidence_sha256",
    )
    if (
        current.id != binding.validation_record_id
        or current.report_sha256 != expected_report_hash
        or current.evidence_sha256 != expected_evidence_hash
    ):
        raise ConcurrentDraftUpdateError(
            "computation evidence changed; reload and retry"
        )
    report = validate_computation_evidence(
        _computation_validation_read(current, is_current=True),
        require_authorizable=True,
    )
    current_formula_promotion_sha256 = computation_formula_adapter_promotion_sha256(
        _computation_validation_read(current, is_current=True)
    )
    expected_formula_promotion_sha256 = (
        _sha256_digest(
            binding.formula_adapter_promotion_sha256,
            "formula_adapter_promotion_sha256",
        )
        if binding.formula_adapter_promotion_sha256 is not None
        else None
    )
    if current_formula_promotion_sha256 != expected_formula_promotion_sha256:
        raise ConcurrentDraftUpdateError(
            "formula adapter promotion changed; reload and retry"
        )
    if (
        binding.runtime_image_reference is None
        or binding.runtime_container_digest is None
        or binding.runtime_promotion_sha256 is None
    ):
        raise ComputationEvidenceError(
            "scoped computation binding requires an exact runtime promotion"
        )
    runtime_image_reference = _bounded_computation_text(
        binding.runtime_image_reference,
        "runtime_image_reference",
        255,
    )
    runtime_container_digest = _bounded_computation_text(
        binding.runtime_container_digest,
        "runtime_container_digest",
        255,
    )
    expected_runtime_promotion_sha256 = _sha256_digest(
        binding.runtime_promotion_sha256,
        "runtime_promotion_sha256",
    )
    # Local import avoids a module cycle: policy depends on the persistence
    # contracts above, while the transaction must re-resolve current promotion
    # state after the draft row is locked.
    from .computation_policy import computation_runtime_promotion_sha256

    current_runtime_promotion_sha256 = computation_runtime_promotion_sha256(
        image_reference=runtime_image_reference,
        container_digest=runtime_container_digest,
    )
    if current_runtime_promotion_sha256 != expected_runtime_promotion_sha256:
        raise ConcurrentDraftUpdateError(
            "computation runtime promotion changed; reload and retry"
        )
    if (
        binding.validation_status is None
        or report.status.value != binding.validation_status
    ):
        raise ConcurrentDraftUpdateError(
            "computation validation status changed; reload and retry"
        )

    expected_attestations = tuple(
        sorted(
            _sha256_digest(value, "attestation_sha256")
            for value in binding.attestation_sha256s
        )
    )
    if len(expected_attestations) != len(set(expected_attestations)):
        raise ComputationEvidenceError(
            "computation gate contains duplicate attestation hashes"
        )
    if report.status in {
        ValidationStatus.PARTIALLY_VALIDATED,
        ValidationStatus.UNSUPPORTED,
    }:
        if not expected_attestations:
            raise ReviewGateError(
                "a trusted computation-specialist attestation is required"
            )
    elif expected_attestations:
        raise ComputationEvidenceError(
            "attestations may bind only partially_validated or unsupported reports"
        )
    if expected_attestations:
        attestations = tuple(
            session.scalars(
                select(ComputationAttestation)
                .where(
                    ComputationAttestation.attestation_sha256.in_(expected_attestations)
                )
                .order_by(ComputationAttestation.attestation_sha256)
            )
        )
        trusted = frozenset(binding.trusted_specialist_subjects)
        if len(attestations) != len(expected_attestations) or any(
            item.validation_record_id != current.id
            or item.draft_id != draft.id
            or item.edit_count != draft.edit_count
            or item.report_sha256 != current.report_sha256
            or item.specialist_identity not in trusted
            for item in attestations
        ):
            raise ReviewGateError(
                "computation attestation is stale or no longer trusted"
            )
    return current


def _computation_gate_binding_payload(
    binding: ComputationGateBinding,
) -> dict[str, Any]:
    return {
        "expected_edit_count": binding.expected_edit_count,
        "expected_draft_sha256": binding.expected_draft_sha256,
        "scoped": binding.scoped,
        "validation_record_id": binding.validation_record_id,
        "evidence_sha256": binding.evidence_sha256,
        "validation_status": binding.validation_status,
        "report_sha256": binding.report_sha256,
        "formula_adapter_promotion_sha256": (binding.formula_adapter_promotion_sha256),
        "runtime_image_reference": binding.runtime_image_reference,
        "runtime_container_digest": binding.runtime_container_digest,
        "runtime_promotion_sha256": binding.runtime_promotion_sha256,
        "attestation_sha256s": list(binding.attestation_sha256s),
        "trusted_specialist_subjects": list(binding.trusted_specialist_subjects),
    }


def _computation_gate_binding_json(
    binding: ComputationGateBinding,
) -> tuple[str, str]:
    payload = _computation_gate_binding_payload(binding)
    encoded = _stable_json_bytes(payload)
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _store_computation_approval_evidence(
    session: Session,
    draft: Draft,
    binding: ComputationGateBinding,
    *,
    reviewer: str,
    approved_at: datetime,
) -> None:
    binding_json, binding_sha256 = _computation_gate_binding_json(binding)
    material = {
        "draft_id": draft.id,
        "edit_count": draft.edit_count,
        "draft_version_id": draft.version_id,
        "binding_sha256": binding_sha256,
        "reviewer_identity": reviewer,
        "approved_at": _as_utc(approved_at).isoformat(),
    }
    session.add(
        ComputationApprovalEvidence(
            draft_id=draft.id,
            edit_count=draft.edit_count,
            draft_version_id=draft.version_id,
            binding_json=binding_json,
            binding_sha256=binding_sha256,
            reviewer_identity=reviewer,
            approved_at=approved_at,
            approval_sha256=hashlib.sha256(_stable_json_bytes(material)).hexdigest(),
        )
    )


def _verify_computation_approval_evidence(
    session: Session,
    draft: Draft,
    binding: ComputationGateBinding,
) -> None:
    binding_json, binding_sha256 = _computation_gate_binding_json(binding)
    approval = session.scalar(
        select(ComputationApprovalEvidence)
        .where(
            ComputationApprovalEvidence.draft_id == draft.id,
            ComputationApprovalEvidence.edit_count == draft.edit_count,
            ComputationApprovalEvidence.draft_version_id == draft.version_id,
            ComputationApprovalEvidence.binding_sha256 == binding_sha256,
        )
        .order_by(ComputationApprovalEvidence.id.desc())
    )
    if (
        approval is None
        or approval.binding_json != binding_json
        or approval.reviewer_identity != draft.last_reviewed_by
        or draft.last_reviewed_at is None
        or _as_utc(approval.approved_at) != _as_utc(draft.last_reviewed_at)
    ):
        raise ReviewGateError(
            "the draft must be approved under the current enforce-mode "
            "computation evidence"
        )


def _computation_validation_read(
    record: ComputationValidationRecord,
    *,
    is_current: bool,
) -> ComputationValidationRead:
    return ComputationValidationRead(
        id=record.id,
        draft_id=record.draft_id,
        edit_count=record.edit_count,
        source_sha256=record.source_sha256,
        draft_sha256=record.draft_sha256,
        blueprint_sha256=record.blueprint_sha256,
        report_sha256=record.report_sha256,
        evidence_sha256=record.evidence_sha256,
        status=record.status,
        schema_version=record.schema_version,
        validator_revision=record.validator_revision,
        blueprint_json=record.blueprint_json,
        report_json=record.report_json,
        dependency_versions_json=record.dependency_versions_json,
        container_digest=record.container_digest,
        seed_plan_json=record.seed_plan_json,
        duration_ms=record.duration_ms,
        engine_evidence_json=record.engine_evidence_json,
        created_at=_as_utc(record.created_at),
        is_current=is_current,
    )


def _computation_attestation_read(
    record: ComputationAttestation,
    *,
    is_current: bool,
) -> ComputationAttestationRead:
    return ComputationAttestationRead(
        id=record.id,
        validation_record_id=record.validation_record_id,
        draft_id=record.draft_id,
        edit_count=record.edit_count,
        report_sha256=record.report_sha256,
        specialist_identity=record.specialist_identity,
        rationale=record.rationale,
        qualification_json=record.qualification_json,
        attestation_sha256=record.attestation_sha256,
        created_at=_as_utc(record.created_at),
        is_current=is_current,
    )


def _validate_publication_evidence_binding(
    binding: ComputationGateBinding,
    evidence: PublicationComputationEvidenceWrite | None,
    *,
    engine_binding: EngineValidationBinding | None,
) -> None:
    if not binding.scoped:
        if evidence is not None:
            raise ComputationEvidenceError(
                "unscoped publication cannot include computation evidence"
            )
        return
    if (
        evidence is None
        or binding.report_sha256 is None
        or _sha256_digest(evidence.report_sha256, "report_sha256")
        != _sha256_digest(binding.report_sha256, "report_sha256")
    ):
        raise ComputationEvidenceError(
            "publication evidence does not match the enforce decision report"
        )
    evidence_attestations = tuple(
        sorted(
            _sha256_digest(value, "attestation_sha256")
            for value in evidence.attestation_sha256s
        )
    )
    binding_attestations = tuple(
        sorted(
            _sha256_digest(value, "attestation_sha256")
            for value in binding.attestation_sha256s
        )
    )
    if evidence_attestations != binding_attestations:
        raise ComputationEvidenceError(
            "publication attestations do not match the enforce decision"
        )
    if evidence is not None and evidence.engine_validation != engine_binding:
        raise ComputationEvidenceError(
            "publication engine evidence does not match the compiled artifact"
        )


def _verify_publication_revision(publication: Publication, draft: Draft) -> None:
    if (
        publication.draft_id != draft.id
        or publication.edit_count != draft.edit_count
        or _draft_sha256(publication.question_snapshot_json)
        != _draft_sha256(draft.current_json)
    ):
        raise ConcurrentDraftUpdateError(
            "publication reservation does not match the current approved draft"
        )


def _store_publication_computation_evidence(
    session: Session,
    publication: Publication,
    validation: ComputationValidationRecord,
    evidence: PublicationComputationEvidenceWrite,
) -> PublicationComputationEvidence:
    report_hash = _sha256_digest(evidence.report_sha256, "report_sha256")
    ordered_hashes = tuple(
        sorted(
            _sha256_digest(value, "attestation_sha256")
            for value in evidence.attestation_sha256s
        )
    )
    if len(ordered_hashes) != len(set(ordered_hashes)):
        raise ComputationEvidenceError(
            "publication evidence contains duplicate attestation hashes"
        )
    if (
        validation.draft_id != publication.draft_id
        or validation.edit_count != publication.edit_count
        or validation.draft_sha256 != _draft_sha256(publication.question_snapshot_json)
        or validation.report_sha256 != report_hash
    ):
        raise ComputationEvidenceError(
            "publication evidence does not match the exact draft revision"
        )
    validate_computation_evidence(
        _computation_validation_read(validation, is_current=True),
        require_authorizable=True,
    )
    draft = session.scalar(
        select(Draft).where(Draft.id == publication.draft_id).with_for_update()
    )
    if draft is None:
        raise DraftNotFoundError(f"draft {publication.draft_id} was not found")
    _verify_engine_validation_binding(
        session,
        draft,
        evidence.engine_validation,
        computation_validation=validation,
    )
    attestations: tuple[ComputationAttestation, ...] = ()
    if ordered_hashes:
        attestations = tuple(
            session.scalars(
                select(ComputationAttestation)
                .where(ComputationAttestation.attestation_sha256.in_(ordered_hashes))
                .order_by(ComputationAttestation.attestation_sha256)
            )
        )
        if len(attestations) != len(ordered_hashes) or any(
            item.validation_record_id != validation.id
            or item.draft_id != publication.draft_id
            or item.edit_count != publication.edit_count
            or item.report_sha256 != report_hash
            for item in attestations
        ):
            raise ComputationEvidenceError(
                "publication attestations must bind to the exact report"
            )

    snapshot_json = _publication_computation_snapshot_json(
        publication,
        validation,
        attestations,
        engine_binding=evidence.engine_validation,
    )
    snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(PublicationComputationEvidence).where(
            PublicationComputationEvidence.publication_id == publication.id
        )
    )
    if existing is not None:
        if existing.snapshot_sha256 != snapshot_hash:
            raise ComputationEvidenceConflictError(
                "publication computation evidence is already frozen"
            )
        return existing
    stored = PublicationComputationEvidence(
        publication_id=publication.id,
        validation_record_id=validation.id,
        draft_id=publication.draft_id,
        edit_count=publication.edit_count,
        report_sha256=report_hash,
        validation_status=validation.status,
        attestation_sha256s_json=json.dumps(
            ordered_hashes,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        snapshot_json=snapshot_json,
        snapshot_sha256=snapshot_hash,
    )
    session.add(stored)
    session.flush()
    return stored


def _publication_computation_snapshot_json(
    publication: Publication,
    validation: ComputationValidationRecord,
    attestations: Sequence[ComputationAttestation],
    *,
    engine_binding: EngineValidationBinding | None = None,
) -> str:
    snapshot = {
        "publication": {
            "id": publication.id,
            "publication_key": publication.publication_key,
            "payload_hash": publication.payload_hash,
            "draft_id": publication.draft_id,
            "edit_count": publication.edit_count,
        },
        "validation": {
            "id": validation.id,
            "source_sha256": validation.source_sha256,
            "draft_sha256": validation.draft_sha256,
            "blueprint_sha256": validation.blueprint_sha256,
            "report_sha256": validation.report_sha256,
            "evidence_sha256": validation.evidence_sha256,
            "status": validation.status,
            "schema_version": validation.schema_version,
            "validator_revision": validation.validator_revision,
            "blueprint_json": validation.blueprint_json,
            "report_json": validation.report_json,
            "dependency_versions_json": validation.dependency_versions_json,
            "container_digest": validation.container_digest,
            "seed_plan_json": validation.seed_plan_json,
            "duration_ms": validation.duration_ms,
            "engine_evidence_json": validation.engine_evidence_json,
            "created_at": _as_utc(validation.created_at),
        },
        "attestations": [
            {
                "id": item.id,
                "attestation_sha256": item.attestation_sha256,
                "specialist_identity": item.specialist_identity,
                "rationale": item.rationale,
                "qualification_json": item.qualification_json,
                "created_at": _as_utc(item.created_at),
            }
            for item in attestations
        ],
        "engine_validation": (
            engine_binding.snapshot if engine_binding is not None else None
        ),
    }
    return _stable_json_bytes(snapshot).decode("utf-8")


def _publication_computation_evidence_read(
    record: PublicationComputationEvidence,
) -> PublicationComputationEvidenceRead:
    parsed_hashes = json.loads(record.attestation_sha256s_json)
    return PublicationComputationEvidenceRead(
        id=record.id,
        publication_id=record.publication_id,
        validation_record_id=record.validation_record_id,
        draft_id=record.draft_id,
        edit_count=record.edit_count,
        report_sha256=record.report_sha256,
        validation_status=record.validation_status,
        attestation_sha256s=tuple(str(value) for value in parsed_hashes),
        snapshot_json=record.snapshot_json,
        snapshot_sha256=record.snapshot_sha256,
        created_at=_as_utc(record.created_at),
    )


_ALLOWED_TRANSITIONS: dict[ReviewStatus, set[ReviewStatus]] = {
    ReviewStatus.DRAFT: {ReviewStatus.READY_FOR_REVIEW, ReviewStatus.REJECTED},
    ReviewStatus.READY_FOR_REVIEW: {
        ReviewStatus.READY_TO_PUBLISH,
        ReviewStatus.REJECTED,
    },
    ReviewStatus.READY_TO_PUBLISH: {
        ReviewStatus.READY_FOR_REVIEW,
        ReviewStatus.REJECTED,
    },
    ReviewStatus.REJECTED: {ReviewStatus.READY_FOR_REVIEW},
}


def _record_confirmation(
    draft: Draft,
    *,
    gate: str,
    actor: str,
    confirmed: bool,
    now: datetime,
) -> None:
    setattr(draft, f"{gate}_confirmed", confirmed)
    setattr(draft, f"{gate}_confirmed_by", actor if confirmed else None)
    setattr(draft, f"{gate}_confirmed_at", now if confirmed else None)
    draft.last_reviewed_by = actor
    draft.last_reviewed_at = now
    draft.review_history_json = [
        *draft.review_history_json,
        {
            "event": f"{gate}_confirmation",
            "actor": actor,
            "at": now.isoformat(),
            "confirmed": confirmed,
        },
    ]


def _transition_draft(
    draft: Draft,
    target: ReviewStatus,
    *,
    actor: str,
    notes: str,
    now: datetime,
) -> None:
    current = draft.status
    if target != current and target not in _ALLOWED_TRANSITIONS[current]:
        raise ReviewTransitionError(
            f"cannot transition a draft from {current.value} to {target.value}"
        )
    if target == ReviewStatus.READY_TO_PUBLISH:
        _validate_persisted_question(
            draft,
            QuestionDraft.model_validate(draft.current_json),
        )
        if not (draft.bloom_confirmed and draft.difficulty_confirmed):
            raise ReviewGateError(
                "Bloom and difficulty must each be confirmed before publication readiness"
            )
    if (
        target
        in {
            ReviewStatus.DRAFT,
            ReviewStatus.READY_FOR_REVIEW,
            ReviewStatus.REJECTED,
        }
        and target != current
    ):
        _clear_confirmations(draft)
    draft.status = target
    draft.reviewer_notes = notes
    draft.last_reviewed_by = actor
    draft.last_reviewed_at = now
    draft.review_history_json = [
        *draft.review_history_json,
        {
            "event": "status_changed",
            "actor": actor,
            "at": now.isoformat(),
            "from": current.value,
            "to": target.value,
            "notes": notes,
        },
    ]


def _validate_persisted_question(draft: Draft, question: QuestionDraft) -> None:
    concept = Concept.model_validate(draft.concept_json)
    if question.concept_label.strip().casefold() != concept.label.strip().casefold():
        raise DraftGroundingError("an edited draft cannot change its selected concept")
    try:
        source_paragraphs = {
            int(paragraph["index"]) for paragraph in draft.source.paragraphs_json
        }
    except (KeyError, TypeError, ValueError):
        raise DraftGroundingError(
            "the stored source paragraph provenance is invalid"
        ) from None
    citations = set(question.citation_paragraphs)
    unavailable = citations - source_paragraphs
    if unavailable:
        raise DraftGroundingError(
            "draft cites unavailable source paragraph(s): "
            + ", ".join(str(index) for index in sorted(unavailable))
        )
    outside_concept = citations - set(concept.source_paragraphs)
    if outside_concept:
        raise DraftGroundingError(
            "draft cites paragraph(s) outside its selected concept source: "
            + ", ".join(str(index) for index in sorted(outside_concept))
        )


def _validate_hint_ladder(question: QuestionDraft, ladder: HintLadderDraft) -> None:
    if (
        ladder.concept_label.strip().casefold()
        != question.concept_label.strip().casefold()
    ):
        raise DraftGroundingError("a hint ladder cannot change the selected concept")
    allowed = set(question.citation_paragraphs)
    for rung in ladder.rungs:
        unavailable = set(rung.citation_paragraphs) - allowed
        if unavailable:
            raise DraftGroundingError(
                f"{rung.rung.value} hint cites paragraph(s) outside the item source: "
                + ", ".join(str(index) for index in sorted(unavailable))
            )


def analyze_hint_leaks(
    question: QuestionDraft, ladder: HintLadderDraft
) -> HintLadderDraft:
    analyzed = ladder.model_copy(deep=True)
    answer_fragments = _answer_fragments(question)
    for rung in analyzed.rungs:
        normalized_hint = _normalized_answer_text(rung.text)
        detected = any(
            fragment and fragment in normalized_hint for fragment in answer_fragments
        )
        rung.answer_leak_detected = rung.answer_leak_detected or detected
    return analyzed


def _engine_validation_for(question: QuestionDraft) -> dict[str, Any] | None:
    if question.item_type.value not in {"webwork", "imathas"}:
        return None
    from .parameterized import compile_parameterized_item

    assert question.response.parameterized is not None
    compiled = compile_parameterized_item(
        question.response.parameterized, validation_seeds=25
    )
    return {
        "engine": compiled.engine,
        "compiler_version": compiled.compiler_version,
        "source_sha256": compiled.source_sha256,
        "seed_count": len(compiled.previews),
        "previews_json": [
            {
                "seed": preview.seed,
                "variables": preview.variables,
                "prompt": preview.prompt,
                "answer": preview.answer,
                "explanation": preview.explanation,
            }
            for preview in compiled.previews
        ],
        "status": "passed",
    }


def _engine_validation_from_computation_evidence(
    question: QuestionDraft,
    evidence: ComputationValidationWrite,
) -> dict[str, Any] | None:
    """Persist the exact typed compiler artifact carried by computation evidence.

    Recompiling a computation-owned external item through the legacy string DSL
    would create a different compiler version and source hash. The computation
    workflow already produced immutable typed-AST source and previews; validate
    that bounded server-generated payload here and use it for the matching
    engine-validation row.
    """

    if question.item_type not in {
        AssessmentItemType.WEBWORK,
        AssessmentItemType.IMATHAS,
    }:
        return None
    try:
        payload = json.loads(
            _opaque_json_string(
                evidence.engine_evidence_json,
                "engine_evidence_json",
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ComputationEvidenceError(
            "computation engine evidence must be a JSON object"
        ) from None
    if payload == {}:
        return None
    if not isinstance(payload, dict):
        raise ComputationEvidenceError(
            "computation engine evidence must be a JSON object"
        )
    engine = payload.get("engine")
    compiler_version = payload.get("compiler_version")
    source_sha256 = payload.get("source_sha256")
    seed_count = payload.get("seed_count")
    previews = payload.get("previews")
    if (
        engine != question.item_type.value
        or not isinstance(compiler_version, str)
        or not 1 <= len(compiler_version) <= 100
        or not isinstance(source_sha256, str)
        or not _SHA256_PATTERN.fullmatch(source_sha256)
        or isinstance(seed_count, bool)
        or not isinstance(seed_count, int)
        or seed_count < 25
        or not isinstance(previews, list)
        or len(previews) != seed_count
        or any(not isinstance(preview, dict) for preview in previews)
    ):
        raise ComputationEvidenceError(
            "computation engine evidence is not a complete typed compiler receipt"
        )
    return {
        "engine": engine,
        "compiler_version": compiler_version,
        "source_sha256": source_sha256,
        "seed_count": seed_count,
        "previews_json": previews,
        "status": "passed",
    }


def _answer_fragments(question: QuestionDraft) -> set[str]:
    fragments: set[str] = set()
    for choice in question.choices:
        if choice.correct:
            normalized = _normalized_answer_text(choice.text)
            if len(normalized) >= 8:
                fragments.add(normalized)
    response = question.response
    if response.numeric_answer is not None:
        fragments.add(_normalized_answer_text(f"{response.numeric_answer:g}"))
    for blank in response.blanks:
        for answer in blank.correct:
            normalized = _normalized_answer_text(answer)
            if len(normalized) >= 4:
                fragments.add(normalized)
    for pair in response.matching_pairs:
        normalized = _normalized_answer_text(pair.target)
        if len(normalized) >= 8:
            fragments.add(normalized)
    return fragments


def _normalized_answer_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9.+-]+", value.casefold()))


def _clear_confirmations(draft: Draft) -> None:
    draft.bloom_confirmed = False
    draft.bloom_confirmed_by = None
    draft.bloom_confirmed_at = None
    draft.difficulty_confirmed = False
    draft.difficulty_confirmed_by = None
    draft.difficulty_confirmed_at = None


def _actor(value: str) -> str:
    actor = value.strip()
    if not actor:
        raise ValueError("review actor must not be blank")
    if len(actor) > 255:
        raise ValueError("review actor is too long")
    return actor
