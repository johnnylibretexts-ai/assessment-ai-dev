import json

import pytest
from pydantic import ValidationError

from app.adapt import AdaptDestination, build_assessment_payload, build_external_engine_payload
from app.parameterized import ParameterizedCompileError, compile_parameterized_item
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    Difficulty,
    HintLadderDraft,
    HintRungDraft,
    HintRungType,
    HotspotRegion,
    ItemResponse,
    ParameterVariable,
    ParameterizedItemSpec,
    QuestionDraft,
)
from app.db import DraftRepository, DraftWrite, init_database
from app.schemas import Concept, Critique, NormalizedPage, Paragraph, SourceInfo


def base_fields() -> dict[str, object]:
    return {
        "concept_label": "Conservation of energy",
        "stem": "Arrange the energy transformations in the correct sequence.",
        "explanation": "The cited passage describes this order.",
        "bloom": BloomLevel.APPLY,
        "difficulty": Difficulty.MEDIUM,
        "citation_paragraphs": [2],
    }


def test_ordering_schema_and_adapt_payload_preserve_stable_identifiers() -> None:
    draft = QuestionDraft(
        **base_fields(),
        item_type=AssessmentItemType.ORDERING,
        choices=[
            Choice(id="A", text="Potential energy", correct=False),
            Choice(id="B", text="Kinetic energy", correct=False),
            Choice(id="C", text="Thermal energy", correct=False),
        ],
        response=ItemResponse(correct_order=["A", "B", "C"]),
    )
    payload = build_assessment_payload(
        draft,
        destination=AdaptDestination(folder_id=1, author="Assessment AI", license="ccby"),
        source_url="https://chem.libretexts.org/Books/Page",
        title="Energy order",
    )
    qti = json.loads(str(payload["qti_json"]))
    assert qti["questionType"] == "ordering"
    assert qti["correctOrder"] == ["assessment-ai-a", "assessment-ai-b", "assessment-ai-c"]
    assert payload["correct_order"] == ["assessment-ai-a", "assessment-ai-b", "assessment-ai-c"]


def test_hotspot_requires_accessible_image_and_a_correct_region() -> None:
    fields = base_fields()
    fields["stem"] = "Select the area showing the highest kinetic energy."
    with pytest.raises(ValidationError):
        QuestionDraft(
            **fields,
            item_type=AssessmentItemType.IMAGE_HOTSPOT,
            response=ItemResponse(
                image_url="https://library.libretexts.dev/media/image.png",
                image_alt="Energy diagram",
                hotspot_regions=[
                    HotspotRegion(
                        id="A", label="Left region", shape="rectangle",
                        coordinates=[0.0, 0.0, 0.5, 0.5], correct=False,
                    )
                ],
            ),
        )


def parameter_spec(engine: str = "webwork") -> ParameterizedItemSpec:
    return ParameterizedItemSpec(
        engine=engine,
        variables=[
            ParameterVariable(name="mass", minimum=1, maximum=10, step=1),
            ParameterVariable(name="speed", minimum=2, maximum=12, step=2),
        ],
        constraints=["mass != speed"],
        prompt_template="Find the product of {mass} and {speed}.",
        answer_expression="mass * speed",
        explanation_template="Multiply {mass} by {speed}.",
        tolerance=0.01,
    )


def test_parameter_compiler_is_deterministic_across_25_bounded_seeds() -> None:
    first = compile_parameterized_item(parameter_spec(), validation_seeds=25)
    second = compile_parameterized_item(parameter_spec(), validation_seeds=25)
    assert first.source_sha256 == second.source_sha256
    assert first.previews == second.previews
    assert len(first.previews) == 25
    assert all(preview.answer == preview.variables["mass"] * preview.variables["speed"] for preview in first.previews)
    assert "DOCUMENT();" in first.source
    assert "$parameters_valid" in first.source
    assert "for (1..1000)" in first.source
    assert "unless $parameters_valid" in first.source


def test_parameter_compiler_rejects_calls_attributes_and_unknown_names() -> None:
    for expression in ["__import__('os')", "mass.real", "mass + secret"]:
        spec = parameter_spec().model_copy(update={"answer_expression": expression})
        with pytest.raises(ParameterizedCompileError):
            compile_parameterized_item(spec)


def test_external_webwork_payload_contains_only_compiled_pg_source() -> None:
    draft = QuestionDraft(
        **base_fields(),
        item_type=AssessmentItemType.WEBWORK,
        response=ItemResponse(parameterized=parameter_spec()),
        specialist_review_required=True,
    )
    compiled = compile_parameterized_item(parameter_spec())
    payload = build_external_engine_payload(
        draft,
        destination=AdaptDestination(folder_id=1, author="Assessment AI", license="ccby"),
        source_url="https://math.libretexts.org/Books/Page",
        title="Parameterized product",
        engine_source=compiled.source,
    )
    assert payload["technology"] == "webwork"
    assert payload["new_auto_graded_code"] == "webwork"
    assert payload["webwork_code"] == compiled.source
    assert "system(" not in compiled.source


def test_hint_ladder_requires_exactly_one_independently_cited_rung_of_each_type() -> None:
    ladder = HintLadderDraft(
        concept_label="Energy",
        rungs=[
            HintRungDraft(rung=HintRungType.CONCEPTUAL, text="Recall conservation.", citation_paragraphs=[1]),
            HintRungDraft(rung=HintRungType.STRATEGIC, text="Track each transfer.", citation_paragraphs=[2]),
            HintRungDraft(rung=HintRungType.SPECIFIC, text="Compare before and after.", citation_paragraphs=[3]),
        ],
    )
    assert [rung.rung for rung in ladder.rungs] == list(HintRungType)
    with pytest.raises(ValidationError):
        HintLadderDraft(
            concept_label="Energy",
            rungs=[ladder.rungs[0], ladder.rungs[0], ladder.rungs[2]],
        )


def test_hint_storage_marks_verbatim_correct_answer_as_a_leak(tmp_path) -> None:
    database = init_database(f"sqlite:///{tmp_path / 'hints.db'}")
    repository = DraftRepository(database)
    fields = base_fields()
    fields["stem"] = "Which statement describes energy?"
    question = QuestionDraft(
        **fields,
        item_type=AssessmentItemType.MULTIPLE_CHOICE,
        choices=[
            Choice(id="A", text="Energy is conserved in the system.", correct=True),
            Choice(id="B", text="Energy disappears.", correct=False),
            Choice(id="C", text="Energy is matter.", correct=False),
            Choice(id="D", text="Energy has no units.", correct=False),
        ],
    )
    concept = Concept(
        label="Conservation of energy", description="Energy remains constant.", source_paragraphs=[2]
    )
    critique = Critique(revision_required=False)
    page = NormalizedPage(
        title="Energy", plaintext="Energy remains constant.", htmlBody="<p>Energy remains constant.</p>",
        paragraphs=[Paragraph(index=2, text="Energy remains constant.", start=0, end=24)],
        source=SourceInfo(canonical_url="https://chem.libretexts.org/Books/Energy", path="chem.libretexts.org/Books/Energy"),
    )
    stored = repository.replace_generated_drafts(
        page=page,
        pipeline_version="test-hints-v1",
        drafts=[DraftWrite(position=0, concept=concept, raw=question, critique=critique, revised=question)],
        llm_calls=[],
    )
    ladder = HintLadderDraft(
        concept_label="Conservation of energy",
        rungs=[
            HintRungDraft(rung=HintRungType.CONCEPTUAL, text="Recall conservation.", citation_paragraphs=[2]),
            HintRungDraft(rung=HintRungType.STRATEGIC, text="Track the system boundary.", citation_paragraphs=[2]),
            HintRungDraft(rung=HintRungType.SPECIFIC, text="Energy is conserved in the system.", citation_paragraphs=[2]),
        ],
    )
    record = repository.save_hint_ladder(stored.draft_ids[0], ladder, editor="reviewer")
    assert record.ladder.rungs[2].answer_leak_detected is True
