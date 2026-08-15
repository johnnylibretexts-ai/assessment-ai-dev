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
from .db import (
    Draft,
    EngineValidationRecord,
    HintLadderRecord,
    inspect_hint_grounding,
)
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
    # The decision, not the verdict. Evaluating the gate is the part that reads
    # the database, so the assembler does it; deciding whether an unallowed
    # decision blocks publication is the precondition's own business, and it
    # raises there rather than here.
    computation_decision: ComputationGateDecision
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
class HintLadderApproved:
    """Review gate: the current revision's hint ladder is grounded and approved.

    Three blockers, but they are alternatives in a chain rather than a set:
    a ladder is missing, or it needs repair, or it is unapproved. At most one
    can be the reviewer's next action, so the single-blocker interface holds
    without widening.

    Feature-flagged. With hint publication off the precondition stays in the
    list and returns nothing -- not even for a missing ladder, because there was
    nothing to generate.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        if not context.settings.hint_publication_enabled:
            return None
        hint_record = context.hint_record
        if hint_record is None:
            return PublicationBlocker(
                "hints_missing",
                "Generate and review the three-rung hint ladder.",
            )
        grounding = inspect_hint_grounding(context.draft.current, hint_record.ladder)
        if grounding:
            # A concept mismatch is not a citation problem, so naming it
            # "repair citations" sends the reviewer to the wrong field.
            citation_issues = [issue for issue in grounding if issue.rung is not None]
            return PublicationBlocker(
                "hints_need_repair",
                (
                    "Repair hint citations outside the current "
                    "question source before approval."
                    if citation_issues
                    else grounding[0].message
                ),
            )
        if any(rung.answer_leak_detected for rung in hint_record.ladder.rungs):
            return PublicationBlocker(
                "hints_need_repair",
                "Resolve possible answer leakage before hint approval.",
            )
        if hint_record.status != "approved":
            return PublicationBlocker(
                "hints_not_approved",
                "Approve all three current hint rungs.",
            )
        return None


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
class ComputationEvidenceAccepted:
    """The computation policy gate allows this revision to be published.

    The one precondition with a dynamic reason: it reports which piece of
    evidence was rejected, because "blocked" alone would send the reviewer
    hunting. That reason is the policy error's own message, and this is where
    the error is caught -- the assembler evaluates the gate but does not catch,
    so the whole condition reads in one place.

    Feature-flagged in the decision rather than here: off and assist both come
    back allowed, so the precondition stays in the list and returns nothing.
    """

    def evaluate(self, context: PublicationContext) -> PublicationBlocker | None:
        try:
            context.computation_decision.require_allowed(action="publication")
        except ComputationPolicyError as exc:
            return PublicationBlocker("computation_evidence_invalid", str(exc))
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
    HintLadderApproved(),
    EngineValidationPassed(),
    ComputationEvidenceAccepted(),
    FrameworkMapped(),
    LicenseResolved(),
    PublishingConfigured(),
)


def collect_precondition_blockers(
    context: PublicationContext,
) -> tuple[PublicationBlocker, ...]:
    """Run every enumerated precondition against the context, in list order.

    Every precondition is evaluated on every call and every blocker is
    collected: nothing short-circuits, so a reviewer sees all of them at once
    rather than one per round-trip.
    """

    return tuple(
        blocker
        for blocker in (
            precondition.evaluate(context) for precondition in PUBLICATION_PRECONDITIONS
        )
        if blocker is not None
    )
