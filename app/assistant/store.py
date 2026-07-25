"""Conversation persistence for the demo assistant.

These models sit on their own declarative base rather than joining the one in
``app.db``. Two reasons: ``app.db`` is already very large, and keeping the
metadata separate means the assistant's schema is created only when the feature
is switched on -- deleting this package removes its tables from the world
without touching the record-of-truth schema.

Conversations are scoped to the proxy-asserted reviewer and are never read by
the generation pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

MAX_TITLE_CHARS = 120


class AssistantBase(DeclarativeBase):
    """Separate metadata so the assistant owns its own schema lifecycle."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssistantConversation(AssistantBase):
    __tablename__ = "assistant_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
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


class AssistantStore:
    """Reviewer-scoped reads and writes over the two assistant tables."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def active_conversation_id(self, reviewer: str) -> int | None:
        with self._sessions() as session:
            return session.scalar(
                select(AssistantConversation.id)
                .where(AssistantConversation.reviewer == reviewer)
                .order_by(AssistantConversation.id.desc())
                .limit(1)
            )

    def ensure_conversation(self, reviewer: str, *, title: str) -> int:
        existing = self.active_conversation_id(reviewer)
        if existing is not None:
            return existing
        with self._sessions() as session:
            conversation = AssistantConversation(
                reviewer=reviewer, title=title[:MAX_TITLE_CHARS]
            )
            session.add(conversation)
            session.commit()
            return conversation.id

    def start_conversation(self, reviewer: str) -> int:
        """Begin a fresh conversation, leaving prior ones intact as a record."""

        with self._sessions() as session:
            conversation = AssistantConversation(reviewer=reviewer, title="")
            session.add(conversation)
            session.commit()
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
