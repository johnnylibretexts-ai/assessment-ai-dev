from __future__ import annotations

from app.parameterized import COMPILER_VERSION, compile_parameterized_item
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    BowTieGroup,
    Choice,
    ClozeBlank,
    Difficulty,
    HighlightSegment,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    HotspotRegion,
    ItemContextType,
    ItemResponse,
    MatchingPair,
    MatrixRow,
    ParameterVariable,
    ParameterizedItemSpec,
    QuestionDraft,
)

from .models import FixtureBundle, FixtureCase, SeedPlanCase


def build_fixture_bundle() -> FixtureBundle:
    cases = [
        FixtureCase(
            fixture_id=f"build08-{item_type.value}-{context_type.value}",
            draft=build_draft(item_type, context_type),
            hint_ladder=build_hint_ladder(),
        )
        for item_type in AssessmentItemType
        for context_type in ItemContextType
    ]
    return FixtureBundle(cases=cases)


def build_draft(
    item_type: AssessmentItemType,
    context_type: ItemContextType = ItemContextType.STANDARD,
) -> QuestionDraft:
    stimulus = None
    set_key = None
    if context_type in {
        ItemContextType.SCENARIO,
        ItemContextType.CASE,
        ItemContextType.SHARED_STIMULUS,
    }:
        stimulus = "A sealed source describes a system in which energy is conserved."
    if context_type == ItemContextType.SHARED_STIMULUS:
        set_key = "build08-energy-set"

    values: dict[str, object] = {
        "item_type": item_type,
        "context_type": context_type,
        "concept_label": "Conservation of energy",
        "stem": "Use the cited source to identify the supported relationship.",
        "stimulus": stimulus,
        "set_key": set_key,
        "explanation": "The cited source states that total energy is conserved.",
        "bloom": BloomLevel.APPLY,
        "difficulty": Difficulty.MEDIUM,
        "citation_paragraphs": [0],
        "targeted_misconception": "Energy disappears when it changes form.",
    }

    if item_type == AssessmentItemType.MULTIPLE_CHOICE:
        values["choices"] = _choices(4, correct={0})
    elif item_type == AssessmentItemType.TRUE_FALSE:
        values["choices"] = [
            Choice(id="TRUE", text="True", correct=True),
            Choice(id="FALSE", text="False", correct=False),
        ]
    elif item_type == AssessmentItemType.NUMERICAL:
        values["response"] = ItemResponse(numeric_answer=42.0, numeric_tolerance=0.01)
    elif item_type in {
        AssessmentItemType.MULTIPLE_RESPONSE,
        AssessmentItemType.SELECT_ALL,
    }:
        values["choices"] = _choices(4, correct={0, 2})
    elif item_type == AssessmentItemType.SELECT_N:
        values["choices"] = _choices(4, correct={0, 2})
        values["response"] = ItemResponse(select_n=2)
    elif item_type == AssessmentItemType.FILL_IN_BLANK:
        values["response"] = ItemResponse(
            blanks=[ClozeBlank(id="BLANK1", correct=["conserved"])]
        )
    elif item_type in {
        AssessmentItemType.SELECT_CHOICE,
        AssessmentItemType.DROPDOWN,
    }:
        values["choices"] = _choices(3, correct={1})
    elif item_type == AssessmentItemType.MATCHING:
        values["response"] = ItemResponse(
            matching_pairs=[
                MatchingPair(
                    prompt_id="P1",
                    prompt="Kinetic energy",
                    target_id="T1",
                    target="Energy of motion",
                ),
                MatchingPair(
                    prompt_id="P2",
                    prompt="Potential energy",
                    target_id="T2",
                    target="Stored energy",
                ),
            ]
        )
    elif item_type == AssessmentItemType.ORDERING:
        values["choices"] = _choices(3, correct=set())
        values["response"] = ItemResponse(correct_order=["A", "B", "C"])
    elif item_type == AssessmentItemType.DRAG_DROP_CLOZE:
        values["response"] = ItemResponse(
            blanks=[
                ClozeBlank(
                    id="BLANK1",
                    correct=["kinetic"],
                    options=["kinetic", "chemical"],
                ),
                ClozeBlank(
                    id="BLANK2",
                    correct=["potential"],
                    options=["potential", "thermal"],
                ),
            ]
        )
    elif item_type == AssessmentItemType.IMAGE_HOTSPOT:
        values["response"] = ItemResponse(
            image_url=("https://assess-ai.libretexts.dev/media/build08-fixture.png"),
            image_alt="Diagram of an object moving down a frictionless ramp.",
            hotspot_regions=[
                HotspotRegion(
                    id="REGION1",
                    label="Moving object",
                    shape="rectangle",
                    coordinates=[0.1, 0.2, 0.3, 0.4],
                    correct=True,
                )
            ],
        )
    elif item_type in {
        AssessmentItemType.HIGHLIGHT_TEXT,
        AssessmentItemType.HIGHLIGHT_TABLE,
    }:
        values["response"] = ItemResponse(
            highlight_segments=[
                HighlightSegment(id="SEG1", text="Energy changes form", correct=False),
                HighlightSegment(
                    id="SEG2", text="Total energy is conserved", correct=True
                ),
            ]
        )
    elif item_type == AssessmentItemType.MATRIX:
        values["response"] = ItemResponse(
            matrix_columns=[
                Choice(id="KINETIC", text="Kinetic"),
                Choice(id="POTENTIAL", text="Potential"),
            ],
            matrix_rows=[
                MatrixRow(
                    id="ROW1",
                    text="Moving object",
                    correct_column_ids=["KINETIC"],
                ),
                MatrixRow(
                    id="ROW2",
                    text="Raised object",
                    correct_column_ids=["POTENTIAL"],
                ),
            ],
        )
    elif item_type == AssessmentItemType.BOW_TIE:
        values["specialist_review_required"] = True
        values["response"] = ItemResponse(
            bow_tie_actions=_bow_tie_group("ACTION"),
            bow_tie_condition=_bow_tie_group("CONDITION"),
            bow_tie_parameters=_bow_tie_group("PARAMETER"),
        )
    elif item_type in {
        AssessmentItemType.WEBWORK,
        AssessmentItemType.IMATHAS,
    }:
        values["specialist_review_required"] = True
        values["response"] = ItemResponse(
            parameterized=build_parameter_spec(item_type, 0)
        )
    else:  # pragma: no cover - enum additions must add a fixture above
        raise ValueError(f"missing fixture for {item_type.value}")
    return QuestionDraft.model_validate(values)


def build_hint_ladder() -> HintLadderDraft:
    return HintLadderDraft(
        concept_label="Conservation of energy",
        rungs=[
            HintRungDraft(
                rung=HintRungType.CONCEPTUAL,
                text="Recall how the source defines the total for the system.",
                citation_paragraphs=[0],
            ),
            HintRungDraft(
                rung=HintRungType.STRATEGIC,
                text="Track each energy form before and after the described change.",
                citation_paragraphs=[0],
            ),
            HintRungDraft(
                rung=HintRungType.SPECIFIC,
                text="Compare the sum of the forms at the two stated moments.",
                citation_paragraphs=[0],
            ),
        ],
    )


def build_parameter_spec(
    item_type: AssessmentItemType,
    index: int,
) -> ParameterizedItemSpec:
    if item_type not in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}:
        raise ValueError("parameter fixtures require WeBWorK or IMathAS")
    offset = index + 1
    return ParameterizedItemSpec(
        engine=item_type.value,
        variables=[
            ParameterVariable(name="mass", minimum=offset, maximum=offset + 8, step=1),
            ParameterVariable(name="speed", minimum=2, maximum=10, step=1),
        ],
        prompt_template="Find momentum for mass {mass} and speed {speed}.",
        answer_expression="mass * speed",
        explanation_template="Multiply {mass} by {speed}.",
        constraints=["mass != speed"],
        tolerance=0.001,
        units="kg m/s",
    )


def build_seed_plan(run_id: str = "build08-disabled-seed-plan") -> list[SeedPlanCase]:
    plan: list[SeedPlanCase] = []
    for item_type in (AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS):
        for item_index in range(20):
            compiled = compile_parameterized_item(
                build_parameter_spec(item_type, item_index), validation_seeds=100
            )
            item_id = f"{item_type.value}-{item_index + 1:02d}"
            for preview in compiled.previews:
                wrong_answer = preview.answer + max(1.0, abs(preview.answer) * 0.1)
                plan.append(
                    SeedPlanCase(
                        run_id=run_id,
                        item_id=item_id,
                        item_type=item_type,
                        seed=preview.seed,
                        compiler_version=COMPILER_VERSION,
                        source_sha256=compiled.source_sha256,
                        expected_prompt=preview.prompt,
                        expected_answer=preview.answer,
                        wrong_answer=wrong_answer,
                    )
                )
    return plan


def _choices(count: int, *, correct: set[int]) -> list[Choice]:
    labels = [
        "Supported relationship",
        "Distractor one",
        "Distractor two",
        "Distractor three",
    ]
    return [
        Choice(id=chr(ord("A") + index), text=labels[index], correct=index in correct)
        for index in range(count)
    ]


def _bow_tie_group(prefix: str) -> BowTieGroup:
    return BowTieGroup(
        choices=[
            Choice(id=f"{prefix}_A", text=f"Supported {prefix.lower()}", correct=True),
            Choice(id=f"{prefix}_B", text=f"Unsupported {prefix.lower()}"),
        ],
        required_selections=1,
    )
