from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.catalog import CuratedAlignment, CuratedFramework, CuratedTopic
from app.computation_policy import ComputationPolicyError
from app.config import Settings
from app.db import Draft, EngineValidationRecord
from app.publication_preconditions import (
    EngineValidationPassed,
    FrameworkMapped,
    LicenseResolved,
    PublicationContext,
    PublishingConfigured,
    QuestionRevisionApproved,
    collect_precondition_blockers,
)
from app.publishing import collect_readiness_blockers
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    Difficulty,
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
        "computation_decision": None,
        "computation_error": None,
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

    blockers = collect_readiness_blockers(
        context(
            draft=Draft(
                status=ReviewStatus.READY_FOR_REVIEW,
                current_json=question(AssessmentItemType.WEBWORK).model_dump(
                    mode="json"
                ),
            ),
            settings=Settings(_env_file=None, hint_generation_enabled=True),
            computation_error=ComputationPolicyError("Computation evidence is stale."),
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

    The two absent codes are the hint ladder and the computation evidence, which
    are ported separately and still run inline.
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
