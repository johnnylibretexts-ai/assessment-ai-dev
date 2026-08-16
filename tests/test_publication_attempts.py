"""The publication attempts module, judged without a publish path behind it.

`test_publishing.py` proves the publish path still behaves; this file proves the
module that now runs every one of its steps. The step body here is a fake
callable and the repository is a recording stand-in, so what is under test is
exactly the part the module owns: the attempt it records, the state it moves the
publication to, and the disposition it applies when the body reports failure.

Each declaration's own shape is exercised against the real constant rather than
an invented one, so these double as the readable statement of what each of the
five steps does with a failure.

No database. `run_publication_step` never queries -- it calls two repository
methods and returns -- so a test that stood one up would be paying for
infrastructure it makes no assertion about.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from app import publication_attempts
from app.db import Publication, PublicationState
from app.publication_attempts import (
    ADAPT_CREATE,
    ADAPT_HINT_SYNC,
    IMATHAS_CREATE,
    PUBLICATION_STEPS,
    QTI_FINALIZE,
    QTI_PREFLIGHT,
    PublicationStep,
    PublicationStepContractError,
    StepFailed,
    StepOutcome,
    StepSucceeded,
    run_publication_step,
    steps_after,
)


SYNCED_AT = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


@dataclass
class RecordedCall:
    """One repository call the module made, in the order it made it."""

    name: str
    publication_id: int
    arguments: dict[str, Any]


@dataclass
class RecordingRepository:
    """Answers like the real repository and remembers what it was asked.

    `update_publication` mutates and returns the same publication the real one
    re-reads, which is enough for the module: it passes that object back to its
    caller and never inspects the write.
    """

    publication: Publication
    calls: list[RecordedCall] = field(default_factory=list)

    def update_publication(
        self,
        publication_id: int,
        *,
        state: PublicationState,
        **values: Any,
    ) -> Publication:
        self.calls.append(
            RecordedCall(
                "update_publication",
                publication_id,
                {"state": state, **values},
            )
        )
        self.publication.state = state.value
        for name, value in values.items():
            setattr(self.publication, name, value)
        return self.publication

    def record_publication_attempt(
        self,
        publication_id: int,
        *,
        action: str,
        resulting_state: PublicationState,
        error_code: str | None = None,
        error_message: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> object:
        self.calls.append(
            RecordedCall(
                "record_publication_attempt",
                publication_id,
                {
                    "action": action,
                    "resulting_state": resulting_state,
                    "error_code": error_code,
                    "error_message": error_message,
                    "response": response,
                },
            )
        )
        return None


def publication_at(state: PublicationState) -> Publication:
    """A detached publication row in the state a step would find it in."""

    return Publication(id=17, state=state.value)


def body_returning(outcome: StepOutcome) -> Callable[[], Awaitable[StepOutcome]]:
    """A fake step body: whatever the external call would have reported."""

    async def run() -> StepOutcome:
        return outcome

    return run


@pytest.mark.asyncio
async def test_a_success_moves_the_state_before_recording_the_attempt() -> None:
    """The success half of what the module owns, in the order it owns it.

    State first, then the attempt, which is the opposite order to a failure --
    pinned here because the two are easy to tidy into one and should not be. The
    step that follows a success reads state and identifiers off the publication
    and never its attempt log, so the state move is what the next step is waiting
    for. On the failure branch the publication is the caller's final answer, and
    the order flips for that reason.
    """

    repository = RecordingRepository(publication_at(PublicationState.ADAPT_CREATED))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        ADAPT_HINT_SYNC,
        body_returning(
            StepSucceeded(
                values={"hints_synced_at": SYNCED_AT},
                response={"synced": True},
            )
        ),
    )

    assert attempt.succeeded
    assert attempt.publication.state == PublicationState.HINTS_SYNCED.value
    assert attempt.publication.hints_synced_at == SYNCED_AT
    assert repository.calls == [
        RecordedCall(
            "update_publication",
            17,
            {
                "state": PublicationState.HINTS_SYNCED,
                "hints_synced_at": SYNCED_AT,
                "error_code": None,
                "error_message": None,
            },
        ),
        RecordedCall(
            "record_publication_attempt",
            17,
            {
                "action": "adapt_hint_sync",
                "resulting_state": PublicationState.HINTS_SYNCED,
                "error_code": None,
                "error_message": None,
                "response": {"synced": True},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_a_failure_leaves_the_state_the_declaration_names() -> None:
    """The disposition comes from the declaration, not from the body.

    The body reports what went wrong and nothing about where that leaves the
    publication -- which is the whole reason the disposition is declared. This
    step holds `adapt_created`, so the publication comes back in the state it
    entered in, carrying the error, and the caller is told not to go on.
    """

    repository = RecordingRepository(publication_at(PublicationState.ADAPT_CREATED))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        ADAPT_HINT_SYNC,
        body_returning(
            StepFailed(code="adapt_unavailable", message="ADAPT is unavailable.")
        ),
    )

    assert not attempt.succeeded
    assert attempt.publication.state == PublicationState.ADAPT_CREATED.value
    assert attempt.publication.error_code == "adapt_unavailable"
    assert attempt.publication.error_message == "ADAPT is unavailable."
    assert repository.calls == [
        RecordedCall(
            "record_publication_attempt",
            17,
            {
                "action": "adapt_hint_sync",
                "resulting_state": PublicationState.ADAPT_CREATED,
                "error_code": "adapt_unavailable",
                "error_message": "ADAPT is unavailable.",
                "response": None,
            },
        ),
        RecordedCall(
            "update_publication",
            17,
            {
                "state": PublicationState.ADAPT_CREATED,
                "error_code": "adapt_unavailable",
                "error_message": "ADAPT is unavailable.",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_a_failure_writes_none_of_the_columns_a_success_would() -> None:
    """A step that did not land does not get to claim what landing would write.

    `hints_synced_at` is the timestamp meaning "the rungs are in ADAPT". The
    module reaches the failure disposition without any of the values a body
    would have earned, so there is no path by which a failed sync stamps it.
    """

    repository = RecordingRepository(publication_at(PublicationState.ADAPT_CREATED))

    await run_publication_step(
        repository,
        repository.publication,
        ADAPT_HINT_SYNC,
        body_returning(StepFailed(code="adapt_timeout", message="No response.")),
    )

    assert repository.publication.hints_synced_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "found",
    [PublicationState.ADAPT_CREATED, PublicationState.HINTS_SYNCED],
)
async def test_a_retaining_step_leaves_whichever_state_it_found(
    found: PublicationState,
) -> None:
    """The last step's disposition is a rule, not a state: keep what was there.

    QTI finalization happens after ADAPT already holds the question, and the
    rungs may or may not have synced first. Naming either state would undo the
    other -- writing `adapt_created` over a publication whose hints had synced
    would lose that fact and re-sync them on the retry.
    """

    repository = RecordingRepository(publication_at(found))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        QTI_FINALIZE,
        body_returning(
            StepFailed(code="qti_finalize_failed", message="Storage is full.")
        ),
    )

    assert not attempt.succeeded
    assert attempt.publication.state == found.value
    assert repository.calls[0].arguments["resulting_state"] == found


@pytest.mark.asyncio
async def test_a_call_that_may_have_landed_gets_its_own_disposition() -> None:
    """The one step whose failure splits in two, and why it has to.

    A create that timed out may already exist in ADAPT. Calling that `failed`
    would send the retry back through the create and duplicate the question;
    `unknown` is the state the reconcile path recovers from by asking ADAPT what
    it actually holds. The body reports only that the answer was ambiguous --
    which state that means is still the declaration's to say.
    """

    repository = RecordingRepository(publication_at(PublicationState.PENDING))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        ADAPT_CREATE,
        body_returning(
            StepFailed(
                code="adapt_no_response",
                message="ADAPT did not answer.",
                ambiguous=True,
            )
        ),
    )

    assert not attempt.succeeded
    assert attempt.publication.state == PublicationState.UNKNOWN.value
    assert repository.calls[0].arguments["resulting_state"] == PublicationState.UNKNOWN


@pytest.mark.asyncio
async def test_the_same_step_lands_on_failed_when_the_call_plainly_did_not() -> None:
    """The other half of that split, from the same declaration."""

    repository = RecordingRepository(publication_at(PublicationState.PENDING))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        ADAPT_CREATE,
        body_returning(
            StepFailed(code="adapt_unavailable", message="ADAPT is unavailable.")
        ),
    )

    assert attempt.publication.state == PublicationState.FAILED.value
    assert repository.calls[0].arguments["resulting_state"] == PublicationState.FAILED


@pytest.mark.asyncio
async def test_an_ambiguous_report_a_step_cannot_answer_is_refused() -> None:
    """Silence here would assert the call did not land, which is the wrong half.

    A step that declares no ambiguous disposition has no answer for "it may have
    landed". Falling back on the definite one would quietly claim the opposite
    of what the body said, and that claim is the duplicate-create bug.
    """

    repository = RecordingRepository(publication_at(PublicationState.ADAPT_CREATED))

    with pytest.raises(PublicationStepContractError, match="adapt_hint_sync"):
        await run_publication_step(
            repository,
            repository.publication,
            ADAPT_HINT_SYNC,
            body_returning(
                StepFailed(code="timeout", message="No answer.", ambiguous=True)
            ),
        )

    assert repository.calls == []


@pytest.mark.asyncio
async def test_a_step_that_advances_nothing_still_records_the_try() -> None:
    """Two of the five steps are checks rather than advances. They still count.

    `reaches` says whether a step moves the publication on, and that is now all
    it says. A preflight that passes is a try that happened, and the log is only
    worth reading as a history if every real try is a row in it -- so the module
    appends one and moves no state.

    The state it records is the one the publication was already in. That is not
    a placeholder for a missing answer: `resulting_state` means the state the
    publication is in *after* this try, which is what the column already holds
    for every advance and every failure.
    """

    repository = RecordingRepository(publication_at(PublicationState.PENDING))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        QTI_PREFLIGHT,
        body_returning(StepSucceeded()),
    )

    assert attempt.succeeded
    assert attempt.publication.state == PublicationState.PENDING.value
    assert repository.calls == [
        RecordedCall(
            "record_publication_attempt",
            17,
            {
                "action": "qti_preflight",
                "resulting_state": PublicationState.PENDING,
                "error_code": None,
                "error_message": None,
                "response": None,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_a_step_that_advances_nothing_keeps_the_evidence_it_reports() -> None:
    """A step with no state to reach still has a row to hang evidence on.

    The bridge create is the case this exists for: it produces an engine
    question ID that is stored in no publication column anywhere, so without
    this the log says the bridge was called and never says what it made. The
    attempt row already has `response_json`, so the evidence costs nothing but
    the decision to keep it.
    """

    repository = RecordingRepository(publication_at(PublicationState.PENDING))

    attempt = await run_publication_step(
        repository,
        repository.publication,
        IMATHAS_CREATE,
        body_returning(StepSucceeded(response={"question_id": 8801})),
    )

    assert attempt.succeeded
    assert attempt.publication.state == PublicationState.PENDING.value
    assert repository.calls == [
        RecordedCall(
            "record_publication_attempt",
            17,
            {
                "action": "imathas_create",
                "resulting_state": PublicationState.PENDING,
                "error_code": None,
                "error_message": None,
                "response": {"question_id": 8801},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_a_step_that_advances_nothing_refuses_columns_to_write() -> None:
    """Values are still a mismatch: there is no state write to carry them.

    A response is evidence and belongs on the attempt. Values are columns on the
    publication, and a step that moves the publication nowhere makes no write to
    put them in -- so discarding them silently would lose something the body
    meant to persist, and the mismatch is named instead.
    """

    repository = RecordingRepository(publication_at(PublicationState.PENDING))

    with pytest.raises(PublicationStepContractError, match="qti_preflight"):
        await run_publication_step(
            repository,
            repository.publication,
            QTI_PREFLIGHT,
            body_returning(StepSucceeded(values={"qti_sha256": "abc"})),
        )

    assert repository.calls == []


@pytest.mark.asyncio
async def test_a_body_that_reports_neither_outcome_is_refused_by_name() -> None:
    """A body that falls off its own end must not read as a success.

    The likely shape is a `return` that only fires inside an `if`, which reports
    `None` -- and by the time the module sees it the external call has already
    happened. Nothing in CI type-checks a body, so the module says which step
    broke its contract instead of failing on an attribute three frames away.
    """

    repository = RecordingRepository(publication_at(PublicationState.ADAPT_CREATED))

    async def reports_nothing() -> Any:
        return None

    with pytest.raises(PublicationStepContractError, match="adapt_hint_sync"):
        await run_publication_step(
            repository,
            repository.publication,
            ADAPT_HINT_SYNC,
            reports_nothing,
        )

    # Nothing written: no state move, and no attempt claiming one.
    assert repository.calls == []


def test_a_broken_contract_is_not_reviewer_facing_validation() -> None:
    """The one thing about that exception's type that must not be convenient.

    The publish route catches `ValueError` and renders its text to the reviewer
    as a 422. A contract break is the service's fault, and dressing it as
    validation would tell someone publishing a question that their draft was
    wrong -- in a sentence about `StepSucceeded`. Making it a `ValueError`
    subclass would be a natural tidy-up, so it fails here instead.
    """

    assert not isinstance(PublicationStepContractError("broken"), ValueError)


@pytest.mark.parametrize(
    ("state", "left"),
    [
        (PublicationState.ADAPT_CREATED, ("adapt_hint_sync", "qti_finalize")),
        (PublicationState.HINTS_SYNCED, ("qti_finalize",)),
        (PublicationState.SUCCEEDED, ()),
    ],
)
def test_a_state_a_step_reached_says_what_is_left_to_run(
    state: PublicationState,
    left: tuple[str, ...],
) -> None:
    """Where a resume re-enters, derived rather than restated.

    A publication's state names the step that put it there, and everything after
    that step in the list is what has not run. Resume asking the list this is
    what stops the order existing a second time in a chain of `if`s that has to
    be kept in step with the first by hand.

    An empty answer is not "nothing to resume" -- it is the last step reporting
    that the publication is finished.
    """

    remaining = steps_after(state.value)

    assert remaining is not None
    assert tuple(step.action for step in remaining) == left


@pytest.mark.parametrize(
    "state",
    [
        PublicationState.PENDING.value,
        PublicationState.FAILED.value,
        PublicationState.UNKNOWN.value,
        "awaiting_engine_binding",
    ],
)
def test_a_state_no_step_reached_is_not_a_position_in_the_sequence(
    state: str,
) -> None:
    """None and an empty tuple are different answers, and the dispatch needs both.

    Reserved-but-not-started, failed, and the state whose recovery rejoins the
    sequence partway are not places in it -- neither is a state written by a
    build this one is not. Answering `()` for any of them would say "finished"
    and hand back a publication that had done nothing.
    """

    assert steps_after(state) is None


def test_no_two_steps_reach_the_same_state() -> None:
    """What makes reading the order off the list unambiguous in the first place.

    `steps_after` finds the step a state names and takes what follows. Two steps
    reaching one state would make that lookup a coin toss between two re-entry
    points, and it would resolve silently -- to whichever was declared first.
    """

    reached = [step.reaches for step in PUBLICATION_STEPS if step.reaches is not None]

    assert len(reached) == len(set(reached))


def test_every_declared_step_is_in_the_ordered_list() -> None:
    """A step declared but left out of the list would be an order nobody reads.

    The expectation is derived from the module rather than written out, so the
    four steps still to be ported are covered the day each is declared. `resume`
    is meant to read `PUBLICATION_STEPS` for the sequence; a constant that never
    reached it would put a step in the publish path and nowhere in the order.
    """

    declared = {
        name: value
        for name, value in vars(publication_attempts).items()
        if isinstance(value, PublicationStep)
    }

    # Without this the scan finding nothing would read as everything passing.
    assert ADAPT_HINT_SYNC in declared.values()
    assert [
        name for name, step in declared.items() if step not in PUBLICATION_STEPS
    ] == []
