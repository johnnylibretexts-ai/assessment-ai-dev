from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.catalog import CuratedAlignment, CuratedFramework, CuratedTopic
from app.computation_policy import ComputationGateDecision, ComputationPolicyError
from app.config import Settings
from app.db import Draft, EngineValidationRecord, HintLadderRecord
from app.publication_preconditions import (
    ComputationEvidenceAccepted,
    EngineValidationPassed,
    FrameworkMapped,
    HintLadderApproved,
    LicenseResolved,
    PublicationContext,
    PublishingConfigured,
    QuestionRevisionApproved,
    collect_precondition_blockers,
)
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    Difficulty,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    ItemResponse,
    ParameterizedItemSpec,
    ParameterVariable,
    QuestionDraft,
    ReviewStatus,
)


def question(
    item_type: AssessmentItemType = AssessmentItemType.MULTIPLE_CHOICE,
) -> QuestionDraft:
    """The smallest valid draft of the given type.

    External-engine types are the only ones that carry a parameterized spec, and
    the schema rejects them without one, so the engine precondition cannot be
    exercised with a bare multiple-choice draft.
    """

    external = item_type in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}
    return QuestionDraft(
        item_type=item_type,
        concept_label="Conservation of energy",
        stem="Find the product of the mass and the speed.",
        explanation="The cited passage gives the relationship.",
        bloom=BloomLevel.APPLY,
        difficulty=Difficulty.MEDIUM,
        citation_paragraphs=[2],
        choices=(
            []
            if external
            else [
                Choice(id="A", text="The product.", correct=True),
                Choice(id="B", text="The sum.", correct=False),
                Choice(id="C", text="The difference.", correct=False),
                Choice(id="D", text="The quotient.", correct=False),
            ]
        ),
        response=(
            ItemResponse(
                parameterized=ParameterizedItemSpec(
                    engine=item_type.value,
                    variables=[
                        ParameterVariable(name="mass", minimum=1, maximum=10, step=1),
                        ParameterVariable(name="speed", minimum=2, maximum=12, step=2),
                    ],
                    prompt_template="Find the product of {mass} and {speed}.",
                    answer_expression="mass * speed",
                    explanation_template="Multiply {mass} by {speed}.",
                    tolerance=0.01,
                )
            )
            if external
            else ItemResponse()
        ),
    )


def context(**overrides: Any) -> PublicationContext:
    """Build the assembled value by hand -- no repository, no database.

    This is the point of the context: the assembler gathers, the preconditions
    judge. If this helper ever needs a session, a precondition has started
    fetching its own data.

    The defaults are the worst case: nothing approved, nothing validated,
    nothing mapped, nothing configured. A test overrides only the field whose
    precondition it is about.
    """

    fields: dict[str, Any] = {
        "draft": Draft(
            status=ReviewStatus.READY_FOR_REVIEW,
            current_json=question().model_dump(mode="json"),
        ),
        "settings": Settings(_env_file=None),
        "hint_record": None,
        "engine_validation": None,
        "computation_decision": gate_decision(
            mode="off",
            reason_code="mode_not_enforced",
            message="Computation policy is not enforced.",
        ),
        "alignment": None,
        "license_resolved": False,
    }
    return PublicationContext(**{**fields, **overrides})


def context_for(status: ReviewStatus) -> PublicationContext:
    return context(
        draft=Draft(status=status, current_json=question().model_dump(mode="json"))
    )


def test_readiness_reports_every_blocker_in_the_order_it_always_has() -> None:
    """The reported order is user-visible and must survive the port.

    `publish()` raises with `blockers[0].message`, and the readiness panel lists
    them top to bottom, so a precondition moving into the enumerated list must
    not move within this sequence. Every condition is unsatisfied here, which is
    the only way to see the whole order at once.
    """

    blockers = collect_precondition_blockers(
        context(
            draft=Draft(
                status=ReviewStatus.READY_FOR_REVIEW,
                current_json=question(AssessmentItemType.WEBWORK).model_dump(
                    mode="json"
                ),
            ),
            settings=Settings(_env_file=None, hint_generation_enabled=True),
            computation_decision=gate_decision(
                allowed=False, message="Computation evidence is stale."
            ),
        )
    )

    assert [blocker.code for blocker in blockers] == [
        "question_not_approved",
        "hints_missing",
        "engine_validation_missing",
        "computation_evidence_invalid",
        "framework_unmapped",
        "license_unresolved",
        "publishing_not_configured",
    ]


def hint_ladder(
    *,
    concept_label: str = "Conservation of energy",
    citation_paragraphs: list[int] | None = None,
    answer_leak_detected: bool = False,
) -> HintLadderDraft:
    """A three-rung ladder, grounded in the same paragraph the question cites."""

    cited = citation_paragraphs if citation_paragraphs is not None else [2]
    return HintLadderDraft(
        concept_label=concept_label,
        rungs=[
            HintRungDraft(
                rung=rung,
                text=f"A {rung.value} nudge towards the relationship.",
                citation_paragraphs=list(cited),
                answer_leak_detected=answer_leak_detected,
            )
            for rung in (
                HintRungType.CONCEPTUAL,
                HintRungType.STRATEGIC,
                HintRungType.SPECIFIC,
            )
        ],
    )


def hint_context(
    *,
    hints_enabled: bool = True,
    ladder: HintLadderDraft | None = None,
    status: str = "approved",
    record: bool = True,
) -> PublicationContext:
    return context(
        settings=Settings(_env_file=None, hint_generation_enabled=hints_enabled),
        hint_record=(
            HintLadderRecord(
                ladder_json=(
                    ladder if ladder is not None else hint_ladder()
                ).model_dump(mode="json"),
                status=status,
            )
            if record
            else None
        ),
    )


def test_a_draft_with_no_hint_ladder_at_all_blocks() -> None:
    blocker = HintLadderApproved().evaluate(hint_context(record=False))

    assert blocker is not None
    assert blocker.code == "hints_missing"
    assert blocker.message == "Generate and review the three-rung hint ladder."


def test_a_hint_citing_outside_the_question_source_blocks_for_repair() -> None:
    blocker = HintLadderApproved().evaluate(
        hint_context(ladder=hint_ladder(citation_paragraphs=[9]))
    )

    assert blocker is not None
    assert blocker.code == "hints_need_repair"
    assert blocker.message == (
        "Repair hint citations outside the current question source before approval."
    )


def test_a_ladder_that_changed_the_concept_says_so_rather_than_naming_citations() -> (
    None
):
    """A concept mismatch is not a citation problem.

    The generic repair message would send the reviewer to the wrong field, so
    this branch keeps the grounding issue's own words.
    """

    blocker = HintLadderApproved().evaluate(
        hint_context(ladder=hint_ladder(concept_label="Momentum"))
    )

    assert blocker is not None
    assert blocker.code == "hints_need_repair"
    assert blocker.message == "A hint ladder cannot change the selected concept."


def test_a_possible_answer_leak_blocks_before_approval_is_considered() -> None:
    blocker = HintLadderApproved().evaluate(
        hint_context(ladder=hint_ladder(answer_leak_detected=True), status="approved")
    )

    assert blocker is not None
    assert blocker.code == "hints_need_repair"
    assert blocker.message == "Resolve possible answer leakage before hint approval."


def test_a_grounded_but_unapproved_ladder_blocks_on_approval() -> None:
    blocker = HintLadderApproved().evaluate(hint_context(status="ready_for_review"))

    assert blocker is not None
    assert blocker.code == "hints_not_approved"
    assert blocker.message == "Approve all three current hint rungs."


def test_an_approved_grounded_ladder_emits_no_blocker() -> None:
    assert HintLadderApproved().evaluate(hint_context()) is None


def test_the_hint_precondition_emits_one_blocker_even_when_all_three_apply() -> None:
    """The three hint blockers are alternatives, so the single-blocker interface holds.

    This ladder is ungrounded, leaks the answer and is unapproved at once. Only
    the first condition in the chain is reported -- widening the interface to
    return several would change what the reviewer sees.
    """

    blocker = HintLadderApproved().evaluate(
        hint_context(
            ladder=hint_ladder(citation_paragraphs=[9], answer_leak_detected=True),
            status="ready_for_review",
        )
    )

    assert blocker is not None
    assert blocker.code == "hints_need_repair"
    assert blocker.message == (
        "Repair hint citations outside the current question source before approval."
    )


def test_hints_disabled_by_configuration_block_nothing() -> None:
    """The precondition stays in the list with the flag off and stays silent.

    Not even a missing ladder blocks: with hint publication off there is nothing
    to have generated.
    """

    assert HintLadderApproved().evaluate(hint_context(hints_enabled=False)) is None
    assert (
        HintLadderApproved().evaluate(hint_context(hints_enabled=False, record=False))
        is None
    )
    assert (
        HintLadderApproved().evaluate(
            hint_context(
                hints_enabled=False,
                ladder=hint_ladder(citation_paragraphs=[9]),
                status="ready_for_review",
            )
        )
        is None
    )


def gate_decision(
    *,
    mode: str = "enforce",
    allowed: bool = True,
    message: str = "Computation evidence is current.",
    reason_code: str = "evidence_current",
) -> ComputationGateDecision:
    return ComputationGateDecision(
        mode=mode,
        scoped=mode == "enforce",
        allowed=allowed,
        validation_status="passed" if allowed else None,
        reason_code=reason_code,
        message=message,
    )


def test_a_gate_decision_that_blocks_publication_keeps_its_own_words() -> None:
    """The one precondition whose reason is dynamic.

    Every other blocker states a fixed next action; this one has to say which
    piece of evidence was rejected, or the reviewer is left hunting.
    """

    blocker = ComputationEvidenceAccepted().evaluate(
        context(
            computation_decision=gate_decision(
                allowed=False,
                reason_code="evidence_missing",
                message="No accepted evidence exists for this revision.",
            )
        )
    )

    assert blocker is not None
    assert blocker.code == "computation_evidence_invalid"
    assert blocker.message == (
        "Computation evidence blocks publication: "
        "No accepted evidence exists for this revision."
    )


def test_the_computation_precondition_catches_its_own_policy_error() -> None:
    """The catch belongs to the precondition, not to the assembler.

    The same decision raises when asked directly -- so the precondition is
    demonstrably the thing turning that exception into a blocker, rather than
    reading an error some other module already caught for it.
    """

    decision = gate_decision(allowed=False, message="Evidence is stale.")

    with pytest.raises(ComputationPolicyError):
        decision.require_allowed(action="publication")

    assert (
        ComputationEvidenceAccepted().evaluate(context(computation_decision=decision))
        is not None
    )


def test_accepted_computation_evidence_emits_no_blocker() -> None:
    assert (
        ComputationEvidenceAccepted().evaluate(
            context(computation_decision=gate_decision(allowed=True))
        )
        is None
    )


def test_computation_policy_that_is_not_enforced_blocks_nothing() -> None:
    """The precondition stays in the list with the policy off and stays silent.

    Off and assist both come back allowed, so the flag state is expressed in the
    decision rather than in a second branch here.
    """

    for mode in ("off", "assist"):
        assert (
            ComputationEvidenceAccepted().evaluate(
                context(
                    computation_decision=gate_decision(
                        mode=mode,
                        allowed=True,
                        reason_code="mode_not_enforced",
                        message="Computation policy is not enforced.",
                    )
                )
            )
            is None
        )


def engine_context(
    *,
    item_type: AssessmentItemType = AssessmentItemType.WEBWORK,
    validation: EngineValidationRecord | None = None,
) -> PublicationContext:
    return context(
        draft=Draft(
            status=ReviewStatus.READY_TO_PUBLISH,
            current_json=question(item_type).model_dump(mode="json"),
        ),
        engine_validation=validation,
    )


def test_an_external_engine_draft_without_validation_evidence_blocks() -> None:
    blocker = EngineValidationPassed().evaluate(engine_context())

    assert blocker is not None
    assert blocker.code == "engine_validation_missing"
    assert blocker.message == "Complete a successful 25-seed engine validation."


def test_validation_evidence_that_failed_or_ran_short_blocks_like_none_at_all() -> None:
    """Evidence exists but does not clear the bar -- 24 seeds is not 25."""

    failed = EngineValidationRecord(status="failed", seed_count=25)
    short = EngineValidationRecord(status="passed", seed_count=24)

    assert (
        EngineValidationPassed().evaluate(engine_context(validation=failed)) is not None
    )
    assert (
        EngineValidationPassed().evaluate(engine_context(validation=short)) is not None
    )


def test_a_passing_twenty_five_seed_validation_satisfies_the_precondition() -> None:
    passed = EngineValidationRecord(status="passed", seed_count=25)

    assert EngineValidationPassed().evaluate(engine_context(validation=passed)) is None


def test_a_draft_that_runs_no_external_engine_needs_no_validation() -> None:
    """The precondition stays in the list for every draft and judges only its own."""

    assert (
        EngineValidationPassed().evaluate(
            engine_context(item_type=AssessmentItemType.MULTIPLE_CHOICE)
        )
        is None
    )


def curated_alignment() -> CuratedAlignment:
    """A mapped source, built by hand rather than looked up in the catalog."""

    framework = CuratedFramework(
        stable_id_namespace="libretexts.curated.chemistry",
        title="Introductory Chemistry",
        author="LibreTexts",
        descriptor_type="topic",
        description="A curated chapter and topic tree.",
        license="ccby",
        license_version="4.0",
        source_url="https://chem.libretexts.org/Bookshelves/Introductory_Chemistry",
        seed_path=Path("data/frameworks/introductory-chemistry.json"),
    )
    return CuratedAlignment(
        framework=framework,
        topic=CuratedTopic(
            stable_id="libretexts.curated.chemistry.isotopes",
            title="Isotopes and Atomic Weight",
            canonical_url="https://chem.libretexts.org/Bookshelves/Isotopes",
            chapter_stable_id="libretexts.curated.chemistry.atoms",
            chapter_title="Atoms and the Periodic Table",
            framework=framework,
        ),
    )


def test_a_source_outside_every_curated_framework_blocks() -> None:
    blocker = FrameworkMapped().evaluate(context(alignment=None))

    assert blocker is not None
    assert blocker.code == "framework_unmapped"
    assert blocker.message == "This source does not yet have a curated framework topic."


def test_a_mapped_source_emits_no_framework_blocker() -> None:
    assert FrameworkMapped().evaluate(context(alignment=curated_alignment())) is None


def test_an_unresolved_license_blocks_with_the_reviewer_s_next_action() -> None:
    blocker = LicenseResolved().evaluate(context(license_resolved=False))

    assert blocker is not None
    assert blocker.code == "license_unresolved"
    assert blocker.message == "Verify and select the source license."


def test_a_resolved_license_emits_no_blocker() -> None:
    assert LicenseResolved().evaluate(context(license_resolved=True)) is None


def test_publishing_that_is_not_configured_blocks_every_draft() -> None:
    """Not the reviewer's fault, and not fixable from the review page.

    The blocker still belongs in the list: a draft that cannot reach ADAPT is
    not publishable, and saying so beats a failure at the far end of the
    publish path.
    """

    blocker = PublishingConfigured().evaluate(
        context(settings=Settings(_env_file=None))
    )

    assert blocker is not None
    assert blocker.code == "publishing_not_configured"
    assert blocker.message == "ADAPT publishing is not configured for this service."


def test_configured_publishing_emits_no_blocker() -> None:
    configured = Settings(
        _env_file=None,
        adapt_publishing_enabled=True,
        adapt_password=SecretStr("never-log-this-password"),
        adapt_folder_id=42,
    )

    assert PublishingConfigured().evaluate(context(settings=configured)) is None


def test_publishing_that_is_enabled_but_incomplete_blocks_like_disabled_publishing() -> (
    None
):
    """`misconfigured` is not `configured` -- enabling the flag is not enough."""

    incomplete = Settings(_env_file=None, adapt_publishing_enabled=True)

    assert PublishingConfigured().evaluate(context(settings=incomplete)) is not None


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


def test_every_ported_precondition_is_reached_through_the_enumerated_list() -> None:
    """Defining a precondition is not enough; readiness must run it from the list.

    Three of the seven are satisfied by this context rather than absent from the
    list: hint publication is off, the draft runs no external engine, and an
    unenforced computation policy comes back allowed.
    """

    blockers = collect_precondition_blockers(context_for(ReviewStatus.READY_FOR_REVIEW))

    assert [blocker.code for blocker in blockers] == [
        "question_not_approved",
        "framework_unmapped",
        "license_unresolved",
        "publishing_not_configured",
    ]


def test_the_enumerated_list_stays_silent_when_every_precondition_is_satisfied() -> (
    None
):
    satisfied = context(
        draft=Draft(
            status=ReviewStatus.READY_TO_PUBLISH,
            current_json=question().model_dump(mode="json"),
        ),
        settings=Settings(
            _env_file=None,
            adapt_publishing_enabled=True,
            adapt_password=SecretStr("never-log-this-password"),
            adapt_folder_id=42,
        ),
        alignment=curated_alignment(),
        license_resolved=True,
    )

    assert collect_precondition_blockers(satisfied) == ()
