"""Conversation persistence for the demo assistant.

These models sit on their own declarative base rather than joining the one in
``app.db``. Two reasons: ``app.db`` is already very large, and keeping the
metadata separate means the assistant's schema is created only when the feature
is switched on -- deleting this package removes its tables from the world
without touching the record-of-truth schema.

Conversations are scoped to the proxy-asserted reviewer and are never read by
the generation pipeline.

At most one conversation per reviewer is ``active``, and that is enforced by a
partial unique index rather than by careful application code. Two concurrent
first-turn requests used to be able to read "no conversation yet" and both
insert, silently splitting one tester's history in half. The database now
refuses the second insert and the loser adopts the winner's conversation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    delete,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

MAX_TITLE_CHARS = 120
ACTIVE_CONVERSATION_INDEX = "uq_assistant_active_conversation"


class AssistantBase(DeclarativeBase):
    """Separate metadata so the assistant owns its own schema lifecycle."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssistantConversation(AssistantBase):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        # Partial: only ONE active row per reviewer, but any number of retired
        # ones, so "New" keeps prior conversations as a record.
        Index(
            ACTIVE_CONVERSATION_INDEX,
            "reviewer",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    title: Mapped[str] = mapped_column(
        String(MAX_TITLE_CHARS), default="", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class AssistantMessage(AssistantBase):
    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    page_route: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


def create_schema(engine: Engine) -> None:
    AssistantBase.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        _apply_sqlite_additive_migration(engine)


def _apply_sqlite_additive_migration(engine: Engine) -> None:
    """Bring a table created before ``active`` existed up to the current shape.

    ``create_all`` skips tables that already exist, indexes included, so a
    database written by the first release of this feature needs the column and
    the partial index added by hand. All three steps are idempotent.
    """

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("assistant_conversations")
    }
    with engine.begin() as connection:
        if "active" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE assistant_conversations "
                    "ADD COLUMN active BOOLEAN NOT NULL DEFAULT 1"
                )
            )
        # Any duplicates predating the index must be retired first, or creating
        # a unique index over them fails and the migration cannot complete.
        connection.execute(
            text(
                "UPDATE assistant_conversations SET active = 0 "
                "WHERE active = 1 AND id NOT IN ("
                "  SELECT MAX(id) FROM assistant_conversations "
                "  WHERE active = 1 GROUP BY reviewer"
                ")"
            )
        )
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_CONVERSATION_INDEX} "
                "ON assistant_conversations (reviewer) WHERE active = 1"
            )
        )


class AssistantStore:
    """Reviewer-scoped reads and writes over the two assistant tables."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _active_query(reviewer: str):
        return (
            select(AssistantConversation.id)
            .where(
                AssistantConversation.reviewer == reviewer,
                AssistantConversation.active.is_(True),
            )
            .order_by(AssistantConversation.id.desc())
            .limit(1)
        )

    def active_conversation_id(self, reviewer: str) -> int | None:
        with self._sessions() as session:
            return session.scalar(self._active_query(reviewer))

    def ensure_conversation(self, reviewer: str, *, title: str) -> int:
        """Return this reviewer's active conversation, creating one if needed.

        The check and the insert share a session, and the partial unique index
        settles the race the check cannot: if a concurrent request inserted
        first, our insert is rejected and we adopt theirs rather than starting a
        second conversation and splitting the transcript.
        """

        with self._sessions() as session:
            existing = session.scalar(self._active_query(reviewer))
            if existing is not None:
                return existing

            conversation = AssistantConversation(
                reviewer=reviewer, active=True, title=title[:MAX_TITLE_CHARS]
            )
            session.add(conversation)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                winner = session.scalar(self._active_query(reviewer))
                if winner is None:
                    raise
                return winner
            return conversation.id

    def start_conversation(self, reviewer: str) -> int:
        """Retire the active conversation and open a fresh one, atomically.

        Both statements land in one transaction, so there is never a moment
        where the reviewer has zero or two active conversations.
        """

        with self._sessions() as session:
            session.execute(
                update(AssistantConversation)
                .where(
                    AssistantConversation.reviewer == reviewer,
                    AssistantConversation.active.is_(True),
                )
                .values(active=False)
            )
            conversation = AssistantConversation(
                reviewer=reviewer, active=True, title=""
            )
            session.add(conversation)
            try:
                session.commit()
            except IntegrityError:
                # A concurrent reset already opened a fresh conversation. That
                # is the outcome this caller wanted, so adopt it.
                session.rollback()
                winner = session.scalar(self._active_query(reviewer))
                if winner is None:
                    raise
                return winner
            return conversation.id

    def messages(
        self, reviewer: str, *, limit: int | None = None
    ) -> list[AssistantMessage]:
        conversation_id = self.active_conversation_id(reviewer)
        if conversation_id is None:
            return []
        with self._sessions() as session:
            query = (
                select(AssistantMessage)
                .join(
                    AssistantConversation,
                    AssistantConversation.id == AssistantMessage.conversation_id,
                )
                # Scoped by reviewer as well as conversation id so a guessed id
                # cannot read someone else's conversation.
                .where(
                    AssistantMessage.conversation_id == conversation_id,
                    AssistantConversation.reviewer == reviewer,
                )
                .order_by(AssistantMessage.id.desc())
            )
            if limit is not None:
                query = query.limit(limit)
            rows = list(session.scalars(query))
        rows.reverse()
        return rows

    def append(
        self,
        *,
        reviewer: str,
        conversation_id: int,
        role: str,
        content: str,
        model: str = "",
        page_route: str = "",
        error_code: str = "",
    ) -> None:
        with self._sessions() as session:
            owner = session.scalar(
                select(AssistantConversation).where(
                    AssistantConversation.id == conversation_id,
                    AssistantConversation.reviewer == reviewer,
                )
            )
            if owner is None:
                raise LookupError("conversation does not belong to this reviewer")
            session.add(
                AssistantMessage(
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    model=model,
                    page_route=page_route[:512],
                    error_code=error_code[:80],
                )
            )
            if not owner.title and role == "user":
                owner.title = content[:MAX_TITLE_CHARS]
            owner.updated_at = _now()
            session.commit()

    def purge_reviewer(self, reviewer: str) -> None:
        """Remove every conversation for one reviewer. Used by tests."""

        with self._sessions() as session:
            ids: Sequence[int] = list(
                session.scalars(
                    select(AssistantConversation.id).where(
                        AssistantConversation.reviewer == reviewer
                    )
                )
            )
            if ids:
                session.execute(
                    delete(AssistantMessage).where(
                        AssistantMessage.conversation_id.in_(ids)
                    )
                )
                session.execute(
                    delete(AssistantConversation).where(
                        AssistantConversation.id.in_(ids)
                    )
                )
            session.commit()
