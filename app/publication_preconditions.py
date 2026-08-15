"""The enumerated list of publication preconditions, and the value they judge.

`CONTEXT.md` fixes the vocabulary -- *publication precondition*, *blocker*,
*review gate* -- and ADR 0002 records why this is one list of preconditions
rather than a registry over the three things people call gates.

The one rule that keeps the list cheap to extend: **preconditions do not fetch
data.** The assembler gathers, the preconditions judge. That is what lets a
precondition be tested by constructing `PublicationContext` directly, with no
database behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .catalog import CuratedAlignment
from .computation_policy import ComputationGateDecision, ComputationPolicyError
from .config import Settings
from .db import Draft, EngineValidationRecord, HintLadderRecord
from .schemas import ReviewStatus


@dataclass(frozen=True)
class PublicationBlocker:
    """The reason a draft cannot be published, in terms a reviewer can act on."""

    code: str
    message: str


@dataclass(frozen=True)
class PublicationContext:
    """Everything the preconditions collectively need, assembled once.

    `draft.current_hint_ladder` and `draft.current_engine_validation` walk lazy
    relationships, and the computation gate performs five repository reads.
    Resolving all of them here means evaluating a precondition issues no query
    -- and that a test can hand one this value instead of a session.
    """

    draft: Draft
    settings: Settings
    hint_record: HintLadderRecord | None
    engine_validation: EngineValidationRecord | None
    # The gate either returns a decision or raises, so exactly one of the next
    # two is set: `publish()` needs the decision itself, and the computation
    # precondition needs the error to phrase its blocker.
    computation_decision: ComputationGateDecision | None
    computation_error: ComputationPolicyError | None
    alignment: CuratedAlignment | None
    license_resolved: bool


class PublicationPrecondition(Protocol):
    """One condition that must hold before a draft may be published."""

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        """Return the blocker this condition emits, or None when satisfied."""


@dataclass(frozen=True)
class QuestionRevisionApproved:
    """Review gate: the current revision carries a recorded human approval.

    Revision-scoped -- editing the draft moves it off `ready_to_publish`, so an
    approval recorded against an earlier revision stops counting on its own.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        if context.draft.status == ReviewStatus.READY_TO_PUBLISH:
            return None
        return PublicationBlocker(
            "question_not_approved",
            "Approve the current question revision.",
        )


# The enumeration. To add a publication precondition you add it here, and there
# is nowhere else it could go. Order is the order blockers are reported in, so
# entries are appended rather than inserted. A precondition disabled by
# configuration stays in the list and returns None -- omitting it would create a
# second place that decides which preconditions exist.
PUBLICATION_PRECONDITIONS: tuple[PublicationPrecondition, ...] = (
    QuestionRevisionApproved(),
)


def collect_precondition_blockers(
    context: PublicationContext,
) -> tuple[PublicationBlocker, ...]:
    """Run every enumerated precondition against the context, in list order."""

    return tuple(
        blocker
        for blocker in (
            precondition.evaluate(context) for precondition in PUBLICATION_PRECONDITIONS
        )
        if blocker is not None
    )
