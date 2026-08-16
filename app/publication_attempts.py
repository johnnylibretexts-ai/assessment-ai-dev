"""The declared steps of the publish path, and the module that runs one.

`CONTEXT.md` fixes the vocabulary -- *publication*, *publication attempt* -- and
the module is named for the table the schema already has: `publication_attempts`.

A step of the publish path is **declared**, not written out: its name, the state a
success moves the publication to, and the state a failure leaves behind. The
module records the attempt and moves the state; the step body -- the external
call itself -- is passed in and stays where it lives. That division is the point.
One interface cannot honestly cover an HTTP create, a bridge request and a local
file write, so this module never sees any of them: a body reports what happened
and the module decides what that means for the record.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .db import Publication, PublicationState


@dataclass(frozen=True)
class PublicationStep:
    """One step of the publish path, declared rather than described.

    `action` is the label the attempt is recorded under, so it is the value
    already in the `publication_attempts.action` column rather than a new name
    for the same step.
    """

    action: str
    reaches: PublicationState
    # One state, which is all this step needs and deliberately not all the path
    # needs. Two of the four steps still to be ported do not fit it: an ADAPT
    # create leaves `unknown` when the call was ambiguous and `failed` when it
    # plainly did not land, and a QTI finalize retains whichever of two prior
    # states the publication was already in. Widen this when porting them --
    # collapsing either onto one state would change the disposition, which is
    # the one thing those tickets may not do.
    leaves_on_failure: PublicationState


@dataclass(frozen=True)
class StepSucceeded:
    """What a step body reports when its external call landed.

    `values` are the columns this step earns the right to write -- the module
    supplies the state and clears the error itself, so a body never names those.
    `response` is kept on the attempt as the evidence of what came back.
    """

    values: Mapping[str, Any] = field(default_factory=dict)
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StepFailed:
    """What a step body reports when its external call did not land.

    A code and a message, and deliberately nothing about state: where a failure
    leaves the publication is the declaration's answer, not the body's. That is
    what stops the disposition from being decided again at every step, which is
    how five steps came to disagree.
    """

    code: str
    message: str


StepOutcome = StepSucceeded | StepFailed


@dataclass(frozen=True)
class RecordedAttempt:
    """The publication after one recorded try, and whether it may go on.

    `succeeded` rather than reading the state back: two steps can share a
    disposition with the state one of them reaches, so the state alone does not
    say whether the sequence continues.
    """

    publication: Publication
    succeeded: bool


class PublicationRecords(Protocol):
    """The two writes this module makes. `DraftRepository` satisfies it.

    Narrow on purpose: a module that can only move a state and append an attempt
    cannot quietly grow a query, and a test can hand it a stand-in that is a
    dataclass rather than a database.
    """

    def update_publication(
        self,
        publication_id: int,
        *,
        state: PublicationState,
        **values: Any,
    ) -> Publication: ...

    def record_publication_attempt(
        self,
        publication_id: int,
        *,
        action: str,
        resulting_state: PublicationState,
        error_code: str | None = ...,
        error_message: str | None = ...,
        response: Mapping[str, Any] | None = ...,
    ) -> object:
        """Returns the appended attempt; this module never reads it back."""


# The publish path in the order it runs. To add a step you declare it here, and
# its failure disposition is visible at the declaration rather than implied by
# the code around the call. Steps arrive as they are ported: a declaration
# nothing runs is a claim the suite cannot check.
ADAPT_HINT_SYNC = PublicationStep(
    action="adapt_hint_sync",
    reaches=PublicationState.HINTS_SYNCED,
    # Holds the state it was already in. The rungs are a second write against a
    # question ADAPT has already created, so a failure to sync them costs the
    # publication nothing it had -- it stays where it was and the retry syncs.
    leaves_on_failure=PublicationState.ADAPT_CREATED,
)

PUBLICATION_STEPS: tuple[PublicationStep, ...] = (ADAPT_HINT_SYNC,)


async def run_publication_step(
    records: PublicationRecords,
    publication: Publication,
    step: PublicationStep,
    body: Callable[[], Awaitable[StepOutcome]],
) -> RecordedAttempt:
    """Run one step and record the try, whatever the body reports.

    The two branches write in opposite orders, and that is preserved rather than
    tidied. A failure ends the publication's run, so its publication is the one
    the caller hands back -- and `update_publication` re-reads the row with its
    attempts loaded, so appending the attempt first is what makes that returned
    record complete. A success continues into the next step, which reads state
    and identifiers off the publication and never its log, so nothing is gained
    by delaying the state move behind the attempt.
    """

    outcome = await body()
    if not isinstance(outcome, StepSucceeded | StepFailed):
        # Nothing type-checks a step body: CI runs ruff and pytest, no more. A
        # body whose only `return` sits inside an `if` reports `None`, and this
        # is reached *after* the external call has landed -- so the message
        # names the step and the contract rather than surfacing as an attribute
        # error on a value three frames from where it was written.
        raise TypeError(
            f"the {step.action} step body reported {outcome!r}; "
            "a step body returns StepSucceeded or StepFailed"
        )
    if isinstance(outcome, StepFailed):
        records.record_publication_attempt(
            publication.id,
            action=step.action,
            resulting_state=step.leaves_on_failure,
            error_code=outcome.code,
            error_message=outcome.message,
        )
        return RecordedAttempt(
            publication=records.update_publication(
                publication.id,
                state=step.leaves_on_failure,
                error_code=outcome.code,
                error_message=outcome.message,
            ),
            succeeded=False,
        )
    moved = records.update_publication(
        publication.id,
        state=step.reaches,
        error_code=None,
        error_message=None,
        **dict(outcome.values),
    )
    records.record_publication_attempt(
        publication.id,
        action=step.action,
        resulting_state=step.reaches,
        response=outcome.response,
    )
    return RecordedAttempt(publication=moved, succeeded=True)
