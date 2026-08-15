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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .catalog import CuratedAlignment
from .computation_policy import ComputationGateDecision, ComputationPolicyError
from .config import Settings
from .db import Draft, EngineValidationRecord, HintLadderRecord
from .schemas import AssessmentItemType, ReviewStatus


@dataclass(frozen=True)
class PublicationBlocker:
    """The reason a draft cannot be published, in terms a reviewer can act on."""

    code: str
    message: str


@dataclass(frozen=True)
class PublicationContext:
    """Everything the preconditions collectively need, assembled once.

    The computation gate performs five repository reads; resolving it here means
    evaluating a precondition issues no query, and that a test can hand one this
    value instead of a session.

    The hint and engine records cost nothing to resolve -- `get_draft`
    `selectinload`s both collections, and `current_hint_ladder` /
    `current_engine_validation` only pick the newest row for the current edit
    out of what is already in memory. They are carried here for uniformity, not
    for a saving: a precondition takes one value and reads fields off it.
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


@dataclass(frozen=True)
class EngineValidationPassed:
    """An external-engine draft carries passing fixed-seed validation evidence.

    Only WeBWorK and IMathAS drafts run an external engine; every other item
    type satisfies this without evidence. Twenty-five seeds is the bar the
    validation run itself is built around, so evidence that stopped short counts
    as no evidence at all.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        if context.draft.current.item_type not in {
            AssessmentItemType.WEBWORK,
            AssessmentItemType.IMATHAS,
        }:
            return None
        validation = context.engine_validation
        if (
            validation is None
            or validation.status != "passed"
            or validation.seed_count < 25
        ):
            return PublicationBlocker(
                "engine_validation_missing",
                "Complete a successful 25-seed engine validation.",
            )
        return None


@dataclass(frozen=True)
class FrameworkMapped:
    """The draft's source has a curated framework topic to publish against.

    Alignment is looked up from the source URL, so this is a property of the
    page the draft came from rather than anything a reviewer can set on the
    draft itself.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        if context.alignment is not None:
            return None
        return PublicationBlocker(
            "framework_unmapped",
            "This source does not yet have a curated framework topic.",
        )


@dataclass(frozen=True)
class LicenseResolved:
    """The source license has been verified and selected.

    The caller decides what "resolved" means -- the review page passes what the
    reviewer has chosen so far, and `publish()` passes True because it resolves
    the license itself immediately afterwards.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        if context.license_resolved:
            return None
        return PublicationBlocker(
            "license_unresolved",
            "Verify and select the source license.",
        )


@dataclass(frozen=True)
class PublishingConfigured:
    """This service has the ADAPT credentials and destination it publishes with.

    A statement about the deployment rather than the draft, so no reviewer
    action clears it. It still belongs in the list: a draft that cannot reach
    ADAPT is not publishable, and saying so up front beats failing at the far
    end of the publish path.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        if context.settings.adapt_publishing_status == "configured":
            return None
        return PublicationBlocker(
            "publishing_not_configured",
            "ADAPT publishing is not configured for this service.",
        )


# The enumeration. To add a publication precondition you add it here, and there
# is nowhere else it could go. Order is the order blockers are reported in, so
# entries are appended rather than inserted. A precondition disabled by
# configuration stays in the list and returns None -- omitting it would create a
# second place that decides which preconditions exist.
PUBLICATION_PRECONDITIONS: tuple[PublicationPrecondition, ...] = (
    QuestionRevisionApproved(),
    EngineValidationPassed(),
    FrameworkMapped(),
    LicenseResolved(),
    PublishingConfigured(),
)


# Temporary scaffolding, deleted when the hint ladder and computation conditions
# are ported. Both still live inline in `publishing.py` and both fall in the
# middle of the reported order -- hints after the question review, computation
# after the engine validation -- and blocker order is user-visible. So readiness
# runs the list in the three stretches those two carve it into. These are slices
# of the one list, never a second enumeration: together they always cover it, so
# a precondition appended to `PUBLICATION_PRECONDITIONS` is still reached.
#
# Inserting is the case to be careful with, and it is exactly what porting the
# last two conditions does: an entry added anywhere but the end silently shifts
# these boundaries, which reorders reported blockers. Update the indices in the
# same edit. `test_readiness_reports_every_blocker_in_the_order_it_always_has`
# is the backstop that fails if you do not.
PRECONDITIONS_BEFORE_HINT_LADDER = PUBLICATION_PRECONDITIONS[:1]
PRECONDITIONS_BETWEEN_HINT_LADDER_AND_COMPUTATION = PUBLICATION_PRECONDITIONS[1:2]
PRECONDITIONS_AFTER_COMPUTATION = PUBLICATION_PRECONDITIONS[2:]


def collect_precondition_blockers(
    context: PublicationContext,
    preconditions: Sequence[PublicationPrecondition] = PUBLICATION_PRECONDITIONS,
) -> tuple[PublicationBlocker, ...]:
    """Run every enumerated precondition against the context, in list order.

    `preconditions` exists only for the scaffolding above: readiness passes the
    stretches of the list it must interleave the two unported conditions with.
    It is not a seam -- nothing substitutes a different set of preconditions.
    """

    return tuple(
        blocker
        for blocker in (
            precondition.evaluate(context) for precondition in preconditions
        )
        if blocker is not None
    )
