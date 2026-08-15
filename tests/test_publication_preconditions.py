from __future__ import annotations

from app.config import Settings
from app.db import Draft
from app.publication_preconditions import (
    PublicationContext,
    QuestionRevisionApproved,
    collect_precondition_blockers,
)
from app.schemas import ReviewStatus


def context_for(status: ReviewStatus) -> PublicationContext:
    """Build the assembled value by hand -- no repository, no database.

    This is the point of the context: the assembler gathers, the preconditions
    judge. If this helper ever needs a session, a precondition has started
    fetching its own data.
    """

    return PublicationContext(
        draft=Draft(status=status),
        settings=Settings(_env_file=None),
        hint_record=None,
        engine_validation=None,
        computation_decision=None,
        computation_error=None,
        alignment=None,
        license_resolved=False,
    )


def test_an_unapproved_revision_blocks_with_the_reviewer_s_next_action() -> None:
    blocker = QuestionRevisionApproved().evaluate(
        context_for(ReviewStatus.READY_FOR_REVIEW)
    )

    assert blocker is not None
    assert blocker.code == "question_not_approved"
    assert blocker.message == "Approve the current question revision."


def test_an_approved_revision_emits_no_blocker() -> None:
    assert (
        QuestionRevisionApproved().evaluate(context_for(ReviewStatus.READY_TO_PUBLISH))
        is None
    )


def test_the_ported_precondition_is_reached_through_the_enumerated_list() -> None:
    """Defining a precondition is not enough; readiness must run it from the list."""

    blockers = collect_precondition_blockers(context_for(ReviewStatus.READY_FOR_REVIEW))

    assert [blocker.code for blocker in blockers][:1] == ["question_not_approved"]


def test_the_enumerated_list_stays_silent_when_every_precondition_is_satisfied() -> (
    None
):
    assert (
        collect_precondition_blockers(context_for(ReviewStatus.READY_TO_PUBLISH)) == ()
    )
