import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.adapt import (
    AdaptClient,
    AdaptDestination,
    AdaptPublishingError,
    FrameworkAlignment,
    FrameworkItem,
    build_assessment_payload,
    build_mcq_payload,
)
from app.catalog import chemistry_seed, suggested_topic
from app.config import Settings
from app.schemas import (
    AssessmentItemType,
    BloomLevel,
    Choice,
    ClozeBlank,
    Difficulty,
    HotspotRegion,
    ItemResponse,
    QuestionDraft,
)


def draft() -> QuestionDraft:
    return QuestionDraft(
        concept_label="Conservation of energy",
        stem="Which statement best describes <energy>?",
        choices=[
            Choice(id="A", text="It is conserved.", correct=True, feedback="Correct."),
            Choice(id="B", text="It disappears.", correct=False),
            Choice(id="C", text="It is matter.", correct=False),
            Choice(id="D", text="It has no units.", correct=False),
        ],
        explanation="The cited passage states that total energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[2],
    )


def test_builds_verified_adapt_mcq_shape_and_embeds_alignment() -> None:
    payload = build_mcq_payload(
        draft(),
        destination=AdaptDestination(
            folder_id=42, author="Assessment Reviewer", license="ccby", public=False
        ),
        source_url="https://dev.libretexts.org/Sandboxes/johnnyphung/book/page",
        title="Energy draft",
        alignment=FrameworkAlignment(
            levels=[FrameworkItem(id=10, text="Physics")],
            descriptors=[FrameworkItem(id=11, text="Explain energy conservation")],
        ),
    )

    assert payload["technology"] == "qti"
    assert payload["folder_id"] == 42
    assert (
        payload["qti_prompt"] == "<p>Which statement best describes &lt;energy&gt;?</p>"
    )
    assert [payload[f"qti_simple_choice_{i}"] for i in range(4)] == [
        "It is conserved.",
        "It disappears.",
        "It is matter.",
        "It has no units.",
    ]
    qti = json.loads(payload["qti_json"])
    assert qti["questionType"] == "multiple_choice"
    assert sum(choice["correctResponse"] for choice in qti["simpleChoice"]) == 1
    assert payload["framework_item_sync_question"] == {
        "levels": [{"id": 10, "text": "Physics"}],
        "descriptors": [{"id": 11, "text": "Explain energy conservation"}],
    }


def test_alignment_is_omitted_when_not_selected() -> None:
    payload = build_mcq_payload(
        draft(),
        destination=AdaptDestination(
            folder_id=42, author="Assessment Reviewer", license="ccby"
        ),
        source_url="https://dev.libretexts.org/Sandboxes/johnnyphung/book/page",
        title="Energy draft",
    )
    assert "framework_item_sync_question" not in payload


def test_fill_in_blank_payload_matches_adapt_positional_contract() -> None:
    fill = QuestionDraft(
        item_type=AssessmentItemType.FILL_IN_BLANK,
        concept_label="Conservation of energy",
        stem="Complete both statements.",
        explanation="The values are source-supported.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
        response=ItemResponse(
            blanks=[
                ClozeBlank(id="BLANK1", correct=["conserved"]),
                ClozeBlank(id="BLANK2", correct=["transformed"], case_sensitive=True),
            ]
        ),
    )
    payload = build_assessment_payload(
        fill,
        destination=AdaptDestination(
            folder_id=42, author="Assessment Reviewer", license="ccby"
        ),
        source_url="https://chem.libretexts.org/Bookshelves/example",
        title="Fill-in contract",
    )
    qti = json.loads(payload["qti_json"])

    assert qti["itemBody"]["textEntryInteraction"].endswith("<u></u> <u></u>")
    assert qti["responseDeclaration"]["correctResponse"] == [
        {"value": "conserved", "matchingType": "exact", "caseSensitive": "no"},
        {"value": "transformed", "matchingType": "exact", "caseSensitive": "yes"},
    ]


def test_item_response_normalizes_lossless_cloze_string_shorthand() -> None:
    response = ItemResponse.model_validate(
        {"blanks": ["conserved", "transformed"]}
    )

    assert [blank.id for blank in response.blanks] == ["B1", "B2"]
    assert [blank.correct for blank in response.blanks] == [
        ["conserved"],
        ["transformed"],
    ]


def test_select_n_is_inferred_only_when_missing_and_rejects_conflicts() -> None:
    payload = draft().model_dump(mode="json")
    payload.update(
        {
            "item_type": "select_n",
            "choices": [
                {"id": "A", "text": "First", "correct": True},
                {"id": "B", "text": "Second", "correct": False},
                {"id": "C", "text": "Third", "correct": True},
            ],
            "response": {},
        }
    )

    inferred = QuestionDraft.model_validate(payload)
    assert inferred.response.select_n == 2

    payload["response"] = {"select_n": 1}
    with pytest.raises(ValueError, match="select_n must equal"):
        QuestionDraft.model_validate(payload)


def test_structural_ids_are_uppercased_but_still_strictly_validated() -> None:
    region = HotspotRegion(
        id="region_1",
        label="Target",
        shape="rectangle",
        coordinates=[0.1, 0.2, 0.3, 0.4],
        correct=True,
    )
    assert region.id == "REGION_1"

    with pytest.raises(ValueError):
        HotspotRegion(
            id="still invalid!",
            label="Target",
            shape="rectangle",
            coordinates=[0.1, 0.2, 0.3, 0.4],
        )


def test_fill_in_blank_payload_rejects_lossy_multiple_answers() -> None:
    fill = QuestionDraft(
        item_type=AssessmentItemType.FILL_IN_BLANK,
        concept_label="Conservation of energy",
        stem="Complete the statement.",
        explanation="The value is source-supported.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[0],
        response=ItemResponse(
            blanks=[ClozeBlank(id="BLANK1", correct=["conserved", "preserved"])]
        ),
    )

    with pytest.raises(ValueError, match="exactly one accepted value"):
        build_assessment_payload(
            fill,
            destination=AdaptDestination(
                folder_id=42, author="Assessment Reviewer", license="ccby"
            ),
            source_url="https://chem.libretexts.org/Bookshelves/example",
            title="Fill-in contract",
        )


def adapt_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'adapt.db'}",
        adapt_publishing_enabled=True,
        adapt_password=SecretStr("top-secret-adapt-password"),
        adapt_folder_id=42,
    )


def test_internal_publication_targets_require_exact_canary_marker(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="qualification-canary marker"):
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'canary.db'}",
            adapt_base_url="http://adapt-browser/api",
        )
    with pytest.raises(ValueError, match="qualification-canary marker"):
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'canary.db'}",
            imathas_bridge_api_url="http://build08-imathas-bridge-browser:8000",
        )

    configured = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'canary.db'}",
        qualification_canary_marker="build08-assessment-publication-canary",
        adapt_base_url="http://adapt-browser/api",
        imathas_bridge_api_url="http://build08-imathas-bridge-browser:8000",
    )
    assert configured.adapt_base_url == "http://adapt-browser/api"
    assert (
        configured.resolved_imathas_bridge_api_url
        == "http://build08-imathas-bridge-browser:8000"
    )
    assert (
        configured.resolved_imathas_bridge_questions_url
        == "http://build08-imathas-bridge-browser:8000/v1/questions"
    )
    assert configured.imathas_status == "disabled"
    assert configured.imathas_publishing_status == "misconfigured"


@pytest.mark.asyncio
async def test_adapt_client_caches_jwt_and_reauthenticates_once_after_401(
    tmp_path: Path,
) -> None:
    calls = {"login": 0, "questions": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            calls["login"] += 1
            return httpx.Response(
                200,
                json={
                    "token": f"token-{calls['login']}",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/api/questions":
            calls["questions"] += 1
            if calls["questions"] == 1:
                return httpx.Response(401, json={"message": "expired"})
            assert request.headers["authorization"] == "Bearer token-2"
            return httpx.Response(200, json={"type": "success", "my_questions": []})
        raise AssertionError(request.url)

    client = AdaptClient(
        adapt_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        assert await client.find_question_by_tag("assessment-ai-key") is None
        assert await client.find_question_by_tag("assessment-ai-key") is None
    finally:
        await client.aclose()
    assert calls == {"login": 2, "questions": 3}


@pytest.mark.asyncio
async def test_adapt_client_validates_owned_folder_license_framework_and_text(
    tmp_path: Path,
) -> None:
    topic = suggested_topic(
        "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
        "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
        "02%3A_Atoms_and_the_Periodic_Table/2.03%3A_Isotopes_and_Atomic_Weight"
    )
    assert topic is not None
    framework = chemistry_seed()["framework"]

    def handler(request: httpx.Request) -> httpx.Response:
        responses = {
            "/api/login": {
                "token": "jwt",
                "expires_in": 3600,
            },
            "/api/saved-questions-folders/options/my-questions-folders": {
                "type": "success",
                "my_questions_folders": [
                    {"id": 42, "name": "Assessment AI — Approved"}
                ],
            },
            "/api/questions/valid-licenses": {"licenses": ["ccbyncsa"]},
            "/api/frameworks": {
                "type": "success",
                "frameworks": [
                    {
                        "id": 7,
                        "title": framework["title"],
                        "source_url": framework["source_url"],
                    }
                ],
            },
            "/api/frameworks/7": {
                "type": "success",
                "framework_levels": [
                    {
                        "id": 20,
                        "level": 1,
                        "parent_id": 0,
                        "title": topic.chapter_title,
                    },
                    {
                        "id": 23,
                        "level": 2,
                        "parent_id": 20,
                        "title": topic.title,
                    },
                ],
            },
        }
        return httpx.Response(200, json=responses[request.url.path])

    client = AdaptClient(
        adapt_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        resolved = await client.resolve_destination(
            license_code="ccbyncsa", topic_stable_id=topic.stable_id
        )
    finally:
        await client.aclose()
    assert resolved.framework_id == 7
    assert resolved.chapter.id == 20
    assert resolved.topic.id == 23
    assert resolved.topic_stable_id == topic.stable_id


@pytest.mark.asyncio
async def test_adapt_client_errors_never_expose_password_or_raw_body(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"top-secret-adapt-password" in request.content
        return httpx.Response(
            401,
            text="top-secret-adapt-password raw provider internals",
        )

    client = AdaptClient(
        adapt_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(AdaptPublishingError) as caught:
            await client.find_question_by_tag("tag")
    finally:
        await client.aclose()
    assert "top-secret-adapt-password" not in str(caught.value)
    assert "raw provider internals" not in str(caught.value)
