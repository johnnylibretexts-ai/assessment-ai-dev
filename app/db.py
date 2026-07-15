from __future__ import annotations

import hashlib
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

from pydantic import BaseModel
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

from .schemas import (
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
class DraftWrite:
    position: int
    concept: Concept | Mapping[str, Any]
    raw: QuestionDraft | Mapping[str, Any]
    critique: Critique | Mapping[str, Any]
    revised: QuestionDraft | Mapping[str, Any]
    hint_ladder: HintLadderDraft | Mapping[str, Any] | None = None
    engine_validation: Mapping[str, Any] | None = None


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
                if draft_write.engine_validation is None:
                    continue
                validation = dict(draft_write.engine_validation)
                session.add(
                    EngineValidationRecord(
                        draft_id=draft_ids_by_position[draft_write.position],
                        edit_count=0,
                        engine=str(validation["engine"]),
                        compiler_version=str(validation["compiler_version"]),
                        source_sha256=str(validation["source_sha256"]),
                        seed_count=int(validation["seed_count"]),
                        previews_json=list(validation["previews"]),
                        status="passed",
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
    ) -> Draft:
        actor = _actor(editor)
        validated = QuestionDraft.model_validate(_json_value(updated_draft))
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                draft = session.get(Draft, draft_id)
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
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
                validation = _engine_validation_for(validated)
                if validation is not None:
                    session.add(
                        EngineValidationRecord(
                            draft_id=draft.id,
                            edit_count=draft.edit_count,
                            **validation,
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
    ) -> Draft:
        """Atomically apply API input while retaining independent gate audit events."""

        actor = _actor(reviewer)
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                draft = session.get(Draft, draft_id)
                if draft is None:
                    raise DraftNotFoundError(f"draft {draft_id} was not found")
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
        except StaleDataError:
            raise ConcurrentDraftUpdateError(
                "the draft changed during review; reload it before submitting a decision"
            ) from None
        return self.require_draft(draft_id)


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
