"""The declared steps of the publish path, and the module that runs them.

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


class PublicationStepContractError(RuntimeError):
    """A step declaration and its body disagree. A code defect, not a condition.

    Deliberately not a `ValueError`. The publish route catches `ValueError` and
    renders its text to the reviewer as a 422, so raising one here would show
    someone trying to publish a question a sentence about `StepSucceeded`, and
    would blame them for it. A broken contract is the service's fault and reads
    like one: it leaves a traceback for whoever wrote the step.
    """


class RetainsCurrentState:
    """A disposition that is a rule rather than a state: keep what was there.

    One step needs it. QTI finalization runs after ADAPT already holds the
    question and the rungs may or may not have synced, so naming either state
    would undo the other. Naming the pair instead would be a list to keep in
    step with the states that can actually reach it.
    """

    def __repr__(self) -> str:
        return "RETAINS_CURRENT"


RETAINS_CURRENT = RetainsCurrentState()


@dataclass(frozen=True)
class PublicationStep:
    """One step of the publish path, declared rather than described.

    `action` is the label the attempt is recorded under, so it is the value
    already in the `publication_attempts.action` column rather than a new name
    for the same step.
    """

    action: str
    # None for a step that advances nothing: a check either passes, leaving the
    # publication exactly where it was and appending no attempt, or it fails.
    # Two of the five are that shape.
    reaches: PublicationState | None
    leaves_on_failure: PublicationState | RetainsCurrentState
    # Only for a step whose external write may have landed without saying so.
    # None means the step has no such case, and a body reporting one is refused
    # rather than quietly filed under `leaves_on_failure` -- that would assert
    # the call did not land, which is the opposite of what the body said.
    leaves_when_ambiguous: PublicationState | None = None


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

    `ambiguous` is the one thing about a failure only the body can know: whether
    the write may have landed anyway. Which state *that* means is still the
    declaration's to say.
    """

    code: str
    message: str
    ambiguous: bool = False


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


# The publish path in the order it runs. All five are here; to add a sixth you
# declare it here, and its failure disposition is visible at the declaration
# rather than implied by the code around the call.
#
# The five disagree about what a failure leaves behind, and the disagreement is
# deliberate -- each comment below says why that step's answer is the right one
# for it. Reading them together is the point: this is the first place the five
# have ever been legible side by side.
#
# The recovery taken from `unknown` is not here. It is not a step: it rejoins
# this sequence partway rather than holding a position in it.
QTI_PREFLIGHT = PublicationStep(
    action="qti_preflight",
    # A check, not an advance: passing leaves the publication where it was and
    # writes no attempt. Preserved from the code it replaces; the append-only
    # log arguably wants the passing try in it, which is a deliberate change to
    # make rather than a side effect of moving the step.
    reaches=None,
    # Nothing external has happened yet, so `failed` costs nothing: the retry
    # re-enters at the top and re-runs the check.
    leaves_on_failure=PublicationState.FAILED,
)

IMATHAS_CREATE = PublicationStep(
    action="imathas_create",
    # The other silent step. What it produces is an engine question ID bound for
    # the ADAPT payload, which is neither a publication column nor evidence.
    reaches=None,
    # `failed` even though the bridge write may have landed, and that is not the
    # oversight it looks like: the bridge keys on `publication_key`, so a retry
    # after a write of unknown fate returns the same question rather than making
    # a second one. There is nothing ambiguous left for a disposition to hold.
    leaves_on_failure=PublicationState.FAILED,
)

ADAPT_CREATE = PublicationStep(
    action="adapt_create",
    reaches=PublicationState.ADAPT_CREATED,
    leaves_on_failure=PublicationState.FAILED,
    # The only step with two. A create that may have landed is not a create that
    # did not: `unknown` is what the reconcile path recovers from by asking ADAPT
    # what it holds, and collapsing it onto `failed` would send the retry back
    # through the create and duplicate the question.
    leaves_when_ambiguous=PublicationState.UNKNOWN,
)

ADAPT_HINT_SYNC = PublicationStep(
    action="adapt_hint_sync",
    reaches=PublicationState.HINTS_SYNCED,
    # Holds the state it was already in. The rungs are a second write against a
    # question ADAPT has already created, so a failure to sync them costs the
    # publication nothing it had -- it stays where it was and the retry syncs.
    leaves_on_failure=PublicationState.ADAPT_CREATED,
)

QTI_FINALIZE = PublicationStep(
    action="qti_finalize",
    reaches=PublicationState.SUCCEEDED,
    # Retains whichever state it found. ADAPT already holds the question by now,
    # so failing here must not walk the publication backwards -- and writing
    # `adapt_created` over one whose rungs had synced would lose that and
    # re-sync them on the retry.
    leaves_on_failure=RETAINS_CURRENT,
)

PUBLICATION_STEPS: tuple[PublicationStep, ...] = (
    QTI_PREFLIGHT,
    IMATHAS_CREATE,
    ADAPT_CREATE,
    ADAPT_HINT_SYNC,
    QTI_FINALIZE,
)


def steps_after(state: str) -> tuple[PublicationStep, ...] | None:
    """The steps still to run for a publication in this state.

    A publication's state names the step that put it there, so everything after
    that step in the list is what has not run. This is the whole reason the order
    is data: resume reads it here instead of restating the sequence in a chain of
    `if`s that has to be kept in step with the forward path by hand.

    `None` means no step reaches this state, so it is not a position in the
    sequence and where it resumes is not this function's question. That covers
    `pending` (reserved, nothing run yet), `failed`, `unknown` -- whose recovery
    rejoins the sequence partway rather than occupying a place in it -- and any
    state this build cannot read at all.

    An empty tuple is a different answer from `None`: the last step reached this
    state, and the publication is finished.
    """

    for index, step in enumerate(PUBLICATION_STEPS):
        if step.reaches is not None and step.reaches.value == state:
            return PUBLICATION_STEPS[index + 1 :]
    return None


def _disposition(
    step: PublicationStep,
    publication: Publication,
    outcome: StepFailed,
) -> PublicationState:
    """The state this failure leaves behind, read off the declaration."""

    if outcome.ambiguous:
        if step.leaves_when_ambiguous is None:
            raise PublicationStepContractError(
                f"the {step.action} step declares no disposition for a call that "
                "may have landed, and its body reported one"
            )
        return step.leaves_when_ambiguous
    leaves = step.leaves_on_failure
    if isinstance(leaves, RetainsCurrentState):
        try:
            return PublicationState(publication.state)
        except ValueError as exc:
            # Unreachable today: every path into a retaining step arrives in one
            # of two recognised states. This is only the step declining to
            # invent a state to keep -- ADR 0003's refusal is a different thing,
            # belongs to the resume dispatch, and lands with #13. Raised as a
            # contract error rather than let out as the bare `ValueError`, which
            # the publish route would render to the reviewer as a 422 reading
            # "not a valid PublicationState".
            raise PublicationStepContractError(
                f"the {step.action} step retains the state it found, and "
                f"{publication.state!r} is not one this build can read"
            ) from exc
    return leaves


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
        raise PublicationStepContractError(
            f"the {step.action} step body reported {outcome!r}; "
            "a step body returns StepSucceeded or StepFailed"
        )
    if isinstance(outcome, StepFailed):
        leaves = _disposition(step, publication, outcome)
        records.record_publication_attempt(
            publication.id,
            action=step.action,
            resulting_state=leaves,
            error_code=outcome.code,
            error_message=outcome.message,
        )
        return RecordedAttempt(
            publication=records.update_publication(
                publication.id,
                state=leaves,
                error_code=outcome.code,
                error_message=outcome.message,
            ),
            succeeded=False,
        )
    if step.reaches is None:
        if outcome.values or outcome.response is not None:
            # There is no write to hang them on, and discarding them would lose
            # evidence the body meant to keep.
            raise PublicationStepContractError(
                f"the {step.action} step reaches no state, so its body may not "
                "report values or a response"
            )
        return RecordedAttempt(publication=publication, succeeded=True)
    try:
        moved = records.update_publication(
            publication.id,
            state=step.reaches,
            error_code=None,
            error_message=None,
            **dict(outcome.values),
        )
    except ValueError as exc:
        # The repository refuses a column it does not allow a publication update
        # to name. Same reason as above for not letting it out as a `ValueError`
        # -- but note what is already lost when this fires: the external call
        # landed, and the attempt row is written after this, so the log has no
        # record that it happened. Whoever hits this has to look at the far end.
        raise PublicationStepContractError(
            f"the {step.action} step body reported values the repository will "
            f"not write: {exc}"
        ) from exc
    records.record_publication_attempt(
        publication.id,
        action=step.action,
        resulting_state=step.reaches,
        response=outcome.response,
    )
    return RecordedAttempt(publication=moved, succeeded=True)
