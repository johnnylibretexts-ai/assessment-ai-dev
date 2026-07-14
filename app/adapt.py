from __future__ import annotations

import base64
import json
import time
from html import escape
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.catalog import chemistry_seed, curated_topics
from app.config import Settings
from app.schemas import AssessmentItemType, BowTieGroup, Choice, QuestionDraft


class FrameworkItem(BaseModel):
    id: int = Field(gt=0)
    text: str = Field(min_length=1)


class FrameworkAlignment(BaseModel):
    levels: list[FrameworkItem] = Field(default_factory=list)
    descriptors: list[FrameworkItem] = Field(default_factory=list)


class AdaptDestination(BaseModel):
    folder_id: int = Field(gt=0)
    author: str = Field(min_length=1)
    license: str = Field(min_length=1)
    license_version: str | None = None
    public: bool = False


def build_mcq_payload(
    draft: QuestionDraft,
    *,
    destination: AdaptDestination,
    source_url: str,
    title: str,
    alignment: FrameworkAlignment | None = None,
    tags: list[str] | None = None,
) -> dict[str, object]:
    """Build the verified ADAPT QTI-MCQ create payload without sending it.

    ADAPT validates the duplicated ``qti_prompt`` and ``qti_simple_choice_*`` fields in
    addition to ``qti_json``. Framework links are part of this same request; there is no
    separate framework-sync POST endpoint.
    """

    if draft.item_type != AssessmentItemType.MULTIPLE_CHOICE:
        raise ValueError("build_mcq_payload accepts only multiple-choice drafts")
    return build_assessment_payload(
        draft,
        destination=destination,
        source_url=source_url,
        title=title,
        alignment=alignment,
        tags=tags,
    )


def build_assessment_payload(
    draft: QuestionDraft,
    *,
    destination: AdaptDestination,
    source_url: str,
    title: str,
    alignment: FrameworkAlignment | None = None,
    tags: list[str] | None = None,
) -> dict[str, object]:
    """Map a reviewed typed item to ADAPT's verified create contract."""

    if draft.item_type in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}:
        raise ValueError("external-engine drafts use their dedicated publisher")

    prompt_html = f"<p>{escape(draft.stem)}</p>"
    payload: dict[str, object] = {
        "question_type": "assessment",
        "folder_id": destination.folder_id,
        "public": int(destination.public),
        "title": title,
        "author": destination.author,
        "tags": tags or [],
        "technology": "qti",
        "technology_id": None,
        "non_technology_text": None,
        "text_question": None,
        "a11y_technology": None,
        "a11y_technology_id": None,
        "answer_html": None,
        "solution_html": draft.explanation,
        "notes": "Generated as a human-reviewed draft by LibreTexts Assessment AI.",
        "hint": None,
        "license": destination.license,
        "license_version": destination.license_version,
        "source_url": source_url,
        "qti_prompt": prompt_html,
    }

    qti_json = _qti_json(draft, payload, prompt_html)
    payload["qti_json"] = json.dumps(
        qti_json, separators=(",", ":"), ensure_ascii=False
    )

    if alignment is not None:
        payload["framework_item_sync_question"] = alignment.model_dump()

    return payload


def build_external_engine_payload(
    draft: QuestionDraft,
    *,
    destination: AdaptDestination,
    source_url: str,
    title: str,
    engine_source: str,
    technology_id: str | int | None = None,
    alignment: FrameworkAlignment | None = None,
    tags: list[str] | None = None,
    imathas_base_url: str = "https://imathas.libretexts.dev",
) -> dict[str, object]:
    """Map a constrained, prevalidated external-engine item to ADAPT."""
    if draft.item_type not in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}:
        raise ValueError("an external-engine item is required")
    technology = draft.item_type.value
    payload: dict[str, object] = {
        "question_type": "assessment",
        "folder_id": destination.folder_id,
        "public": int(destination.public),
        "title": title,
        "author": destination.author,
        "tags": tags or [],
        "technology": technology,
        "technology_id": technology_id,
        "non_technology_text": None,
        "text_question": f"<p>{escape(draft.stem)}</p>",
        "a11y_technology": None,
        "a11y_technology_id": None,
        "answer_html": None,
        "solution_html": draft.explanation,
        "notes": "Generated from a constrained parameter specification by LibreTexts Assessment AI.",
        "hint": None,
        "license": destination.license,
        "license_version": destination.license_version,
        "source_url": source_url,
        "qti_json": None,
    }
    if technology == "webwork":
        payload.update(
            {
                "new_auto_graded_code": "webwork",
                "webwork_code": engine_source,
            }
        )
    else:
        if technology_id is None:
            raise ValueError("IMathAS publication requires a local question ID")
        payload["technology_id"] = int(technology_id)
        payload["technology_iframe"] = (
            f'<iframe class="imathas_problem" src="{imathas_base_url.rstrip("/")}'
            f'/adapt/embedq2.php?id={int(technology_id)}"></iframe>'
        )
    if alignment is not None:
        payload["framework_item_sync_question"] = alignment.model_dump()
    return payload


def _qti_json(
    draft: QuestionDraft,
    payload: dict[str, object],
    prompt_html: str,
) -> dict[str, object]:
    item_type = draft.item_type
    if item_type in {
        AssessmentItemType.MULTIPLE_CHOICE,
        AssessmentItemType.TRUE_FALSE,
    }:
        choices, feedback = _choice_responses(draft.choices, payload)
        qti: dict[str, object] = {
            "questionType": item_type.value,
            "prompt": prompt_html,
            "simpleChoice": choices,
        }
        if feedback:
            qti["feedback"] = feedback
        return qti

    if item_type in {
        AssessmentItemType.MULTIPLE_RESPONSE,
        AssessmentItemType.SELECT_ALL,
        AssessmentItemType.SELECT_N,
    }:
        responses = _responses(draft.choices)
        question_type = (
            "multiple_response_select_n"
            if item_type == AssessmentItemType.SELECT_N
            else "multiple_response_select_all_that_apply"
        )
        qti = {
            "questionType": question_type,
            "prompt": prompt_html,
            "responses": responses,
        }
        if item_type == AssessmentItemType.SELECT_N:
            qti["numberToSelect"] = draft.response.select_n
        payload["responses"] = responses
        return qti

    if item_type == AssessmentItemType.MATCHING:
        terms = []
        possible = []
        for index, pair in enumerate(draft.response.matching_pairs):
            prompt_id = f"assessment-ai-{pair.prompt_id.lower()}"
            target_id = f"assessment-ai-{pair.target_id.lower()}"
            terms.append(
                {
                    "identifier": prompt_id,
                    "termToMatch": pair.prompt,
                    "matchingTermIdentifier": target_id,
                    "feedback": "",
                }
            )
            possible.append(
                {"identifier": target_id, "matchingTerm": pair.target}
            )
            payload[f"qti_matching_term_to_match_{index}"] = pair.prompt
            payload[f"qti_matching_matching_term_{index}"] = pair.target
        return {
            "questionType": "matching",
            "prompt": prompt_html,
            "termsToMatch": terms,
            "possibleMatches": possible,
        }

    if item_type == AssessmentItemType.NUMERICAL:
        payload["correct_response"] = draft.response.numeric_answer
        payload["margin_of_error"] = draft.response.numeric_tolerance
        return {
            "questionType": "numerical",
            "prompt": prompt_html,
            "correctResponse": {
                "value": draft.response.numeric_answer,
                "marginOfError": draft.response.numeric_tolerance,
            },
            "feedback": {"any": draft.explanation, "correct": "", "incorrect": ""},
        }

    if item_type == AssessmentItemType.ORDERING:
        responses = _responses(draft.choices)
        correct_order = [
            f"assessment-ai-{identifier.lower()}"
            for identifier in draft.response.correct_order
        ]
        payload["responses"] = responses
        payload["correct_order"] = correct_order
        return {
            "questionType": "ordering",
            "prompt": prompt_html,
            "responses": responses,
            "correctOrder": correct_order,
        }

    if item_type == AssessmentItemType.IMAGE_HOTSPOT:
        regions = [region.model_dump(mode="json") for region in draft.response.hotspot_regions]
        payload["image_url"] = draft.response.image_url
        payload["image_alt"] = draft.response.image_alt
        payload["hotspot_regions"] = regions
        return {
            "questionType": "image_hotspot",
            "prompt": prompt_html,
            "imageUrl": draft.response.image_url,
            "imageAlt": draft.response.image_alt,
            "regions": regions,
        }

    if item_type in {
        AssessmentItemType.HIGHLIGHT_TEXT,
        AssessmentItemType.HIGHLIGHT_TABLE,
    }:
        responses = [
            {
                "identifier": f"assessment-ai-{segment.id.lower()}",
                "text": segment.text,
                "correctResponse": segment.correct,
            }
            for segment in draft.response.highlight_segments
        ]
        marked_prompt = " ".join(
            f"[{escape(segment.text)}]" for segment in draft.response.highlight_segments
        )
        if item_type == AssessmentItemType.HIGHLIGHT_TABLE:
            rows = [{"header": "Source", "prompt": marked_prompt, "responses": responses}]
            headers = ["Section", "Text"]
            payload["colHeaders"] = headers
            payload["rows"] = rows
            return {
                "questionType": "highlight_table",
                "prompt": prompt_html,
                "colHeaders": headers,
                "rows": rows,
            }
        payload["qti_prompt"] = marked_prompt
        payload["responses"] = responses
        return {
            "questionType": "highlight_text",
            "prompt": marked_prompt,
            "responses": responses,
        }

    if item_type == AssessmentItemType.MATRIX:
        columns = draft.response.matrix_columns
        headers = ["Response", *[choice.text for choice in columns]]
        column_index = {choice.id: index for index, choice in enumerate(columns)}
        multiple = any(len(row.correct_column_ids) > 1 for row in draft.response.matrix_rows)
        if multiple:
            rows = [
                {
                    "header": row.text,
                    "responses": [
                        {
                            "identifier": f"assessment-ai-{row.id.lower()}-{column.id.lower()}",
                            "correctResponse": column.id in row.correct_column_ids,
                        }
                        for column in columns
                    ],
                }
                for row in draft.response.matrix_rows
            ]
            qti = {
                "questionType": "matrix_multiple_response",
                "prompt": prompt_html,
                "colHeaders": headers,
                "rows": rows,
            }
            payload["colHeaders"] = headers
        else:
            rows = [
                {
                    "label": row.text,
                    "correctResponse": column_index[row.correct_column_ids[0]],
                }
                for row in draft.response.matrix_rows
            ]
            qti = {
                "questionType": "matrix_multiple_choice",
                "prompt": prompt_html,
                "headers": headers,
                "rows": rows,
            }
            payload["headers"] = headers
        payload["rows"] = rows
        return qti

    if item_type == AssessmentItemType.BOW_TIE:
        actions = _bow_tie_responses(draft.response.bow_tie_actions)
        conditions = _bow_tie_responses(draft.response.bow_tie_condition)
        parameters = _bow_tie_responses(draft.response.bow_tie_parameters)
        payload["actions_to_take"] = actions
        payload["potential_conditions"] = conditions
        payload["parameters_to_monitor"] = parameters
        return {
            "questionType": "bow_tie",
            "prompt": prompt_html,
            "actionsToTake": actions,
            "potentialConditions": conditions,
            "parametersToMonitor": parameters,
        }

    if item_type == AssessmentItemType.DRAG_DROP_CLOZE:
        correct = [
            {
                "identifier": f"assessment-ai-{blank.id.lower()}",
                "value": blank.correct[0],
            }
            for blank in draft.response.blanks
        ]
        correct_values = {item["value"] for item in correct}
        distractors = [
            {
                "identifier": f"assessment-ai-distractor-{index}",
                "value": value,
            }
            for index, value in enumerate(
                dict.fromkeys(
                    option
                    for blank in draft.response.blanks
                    for option in blank.options
                    if option not in correct_values
                )
            )
        ]
        cloze_prompt = prompt_html + " " + " ".join(
            "[select]" for _ in draft.response.blanks
        )
        payload["qti_prompt"] = cloze_prompt
        payload["correct_responses"] = correct
        payload["distractors"] = distractors
        return {
            "questionType": "drag_and_drop_cloze",
            "prompt": cloze_prompt,
            "correctResponses": correct,
            "distractors": distractors,
        }

    if item_type in {
        AssessmentItemType.FILL_IN_BLANK,
        AssessmentItemType.SELECT_CHOICE,
        AssessmentItemType.DROPDOWN,
    }:
        return _inline_interaction(draft, payload, prompt_html)

    raise ValueError(f"ADAPT mapping is not implemented for {item_type.value}")


def _choice_responses(
    choices: list[Choice], payload: dict[str, object]
) -> tuple[list[dict[str, object]], dict[str, str]]:
    responses = _responses(choices)
    feedback: dict[str, str] = {}
    for index, (choice, response) in enumerate(zip(choices, responses, strict=True)):
        payload[f"qti_simple_choice_{index}"] = choice.text
        if choice.feedback:
            feedback[str(response["identifier"])] = escape(choice.feedback)
    return responses, feedback


def _responses(choices: list[Choice]) -> list[dict[str, object]]:
    return [
        {
            "identifier": f"assessment-ai-{choice.id.lower()}",
            "value": escape(choice.text),
            "correctResponse": choice.correct,
        }
        for choice in choices
    ]


def _bow_tie_responses(group: BowTieGroup | None) -> list[dict[str, object]]:
    if group is None:
        raise ValueError("bow-tie response group is missing")
    return _responses(group.choices)


def _inline_interaction(
    draft: QuestionDraft,
    payload: dict[str, object],
    prompt_html: str,
) -> dict[str, object]:
    if draft.item_type == AssessmentItemType.FILL_IN_BLANK:
        if any(len(blank.correct) != 1 for blank in draft.response.blanks):
            raise ValueError(
                "ADAPT fill-in-the-blank publication requires exactly one accepted value per blank"
            )
        interactions = [
            {
                "value": blank.correct[0],
                "matchingType": "exact",
                "caseSensitive": "yes" if blank.case_sensitive else "no",
            }
            for blank in draft.response.blanks
        ]
        item_body = {
            "textEntryInteraction": prompt_html
            + " "
            + " ".join("<u></u>" for _blank in draft.response.blanks)
        }
        payload["qti_item_body"] = item_body
        payload["qti_text_entry_interactions"] = interactions
        return {
            "questionType": "fill_in_the_blank",
            "itemBody": item_body,
            "responseDeclaration": {"correctResponse": interactions},
        }
    responses = _responses(draft.choices)
    interaction_id = "RESPONSE"
    inline = {interaction_id: responses}
    item_body = {"inlineChoiceInteraction": f"{prompt_html} [select]"}
    payload["qti_item_body"] = item_body
    payload[f"qti_select_choice_{interaction_id}"] = responses
    return {
        "questionType": "select_choice",
        "itemBody": item_body,
        "inline_choice_interactions": inline,
    }


class AdaptPublishingError(RuntimeError):
    def __init__(self, message: str, *, code: str = "adapt_error") -> None:
        super().__init__(message)
        self.code = code


class AdaptAmbiguousError(AdaptPublishingError):
    """The create request may have reached ADAPT and must be reconciled."""


class ResolvedAlignment(BaseModel):
    framework_id: int = Field(gt=0)
    framework_title: str
    chapter: FrameworkItem
    topic: FrameworkItem
    chapter_stable_id: str
    topic_stable_id: str

    @property
    def payload(self) -> FrameworkAlignment:
        return FrameworkAlignment(levels=[self.chapter, self.topic])


class AdaptCreateResult(BaseModel):
    question_id: int = Field(gt=0)
    page_id: int = Field(gt=0)


class AdaptClient:
    """Authenticated, narrowly scoped client for the dev ADAPT question API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=f"{settings.adapt_base_url.rstrip('/')}/",
            timeout=settings.adapt_timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "LibreTexts-Assessment-AI/0.3",
            },
        )
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _authenticate(self, *, force: bool = False) -> str:
        if not force and self._token and self._token_expires_at - 30 > time.time():
            return self._token
        password = self._settings.adapt_password
        if password is None or not password.get_secret_value().strip():
            raise AdaptPublishingError(
                "ADAPT publishing credentials are not configured.",
                code="adapt_misconfigured",
            )
        try:
            response = await self._client.post(
                "login",
                json={
                    "email": self._settings.adapt_email,
                    "password": password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise AdaptPublishingError(
                "ADAPT authentication is temporarily unavailable.",
                code="adapt_auth_unavailable",
            ) from exc
        if response.status_code != 200:
            raise AdaptPublishingError(
                "ADAPT rejected the publishing service credentials.",
                code="adapt_auth_failed",
            )
        data = self._json_object(response, code="adapt_auth_invalid")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise AdaptPublishingError(
                "ADAPT returned an invalid authentication response.",
                code="adapt_auth_invalid",
            )
        expires_in = data.get("expires_in")
        self._token = token
        self._token_expires_at = (
            time.time() + float(expires_in)
            if isinstance(expires_in, (int, float)) and expires_in > 0
            else self._jwt_expiry(token)
        )
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        ambiguous_on_failure: bool = False,
    ) -> dict[str, Any]:
        token = await self._authenticate()
        for auth_attempt in range(2):
            try:
                response = await self._client.request(
                    method,
                    path.lstrip("/"),
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error_type = (
                    AdaptAmbiguousError
                    if ambiguous_on_failure
                    else AdaptPublishingError
                )
                raise error_type(
                    "ADAPT did not return a response. The publication will be reconciled before any retry.",
                    code="adapt_no_response",
                ) from exc
            if response.status_code == 401 and auth_attempt == 0:
                token = await self._authenticate(force=True)
                continue
            break
        if response.status_code >= 500 and ambiguous_on_failure:
            raise AdaptAmbiguousError(
                "ADAPT returned a server error after the create request. The publication will be reconciled before any retry.",
                code="adapt_server_unknown",
            )
        if response.status_code >= 400:
            code = f"adapt_http_{response.status_code}"
            messages = {
                403: "ADAPT refused this publishing operation.",
                404: "The configured ADAPT publishing destination was not found.",
                422: "ADAPT rejected the question payload.",
                429: "ADAPT is rate limiting publishing requests. Try again later.",
            }
            raise AdaptPublishingError(
                messages.get(
                    response.status_code,
                    "ADAPT could not complete the publishing operation.",
                ),
                code=code,
            )
        data = self._json_object(response)
        if data.get("type") == "error":
            raise AdaptPublishingError(
                "ADAPT rejected the publishing operation.",
                code="adapt_application_error",
            )
        return data

    async def resolve_destination(
        self,
        *,
        license_code: str,
        topic_stable_id: str,
    ) -> ResolvedAlignment:
        folders = await self._request(
            "GET", "saved-questions-folders/options/my-questions-folders"
        )
        owned = folders.get("my_questions_folders", [])
        if not any(
            isinstance(item, dict)
            and item.get("id") == self._settings.adapt_folder_id
            and item.get("name") == self._settings.adapt_folder_name
            for item in owned
        ):
            raise AdaptPublishingError(
                "The configured ADAPT folder is not owned by the publishing service.",
                code="adapt_folder_mismatch",
            )

        licenses = await self._request("GET", "questions/valid-licenses")
        if license_code not in licenses.get("licenses", []):
            raise AdaptPublishingError(
                "The selected source license is not supported by ADAPT.",
                code="adapt_license_mismatch",
            )

        local_topic = next(
            (item for item in curated_topics() if item.stable_id == topic_stable_id),
            None,
        )
        if local_topic is None:
            raise AdaptPublishingError(
                "The selected framework topic is not in the curated catalog.",
                code="adapt_topic_unknown",
            )
        framework_properties = chemistry_seed()["framework"]
        frameworks = await self._request("GET", "frameworks")
        framework = next(
            (
                item
                for item in frameworks.get("frameworks", [])
                if item.get("title") == framework_properties["title"]
                and item.get("source_url") == framework_properties["source_url"]
            ),
            None,
        )
        if framework is None:
            raise AdaptPublishingError(
                "The curated Chemistry framework has not been provisioned in ADAPT.",
                code="adapt_framework_missing",
            )
        framework_id = int(framework["id"])
        tree = await self._request("GET", f"frameworks/{framework_id}")
        levels = tree.get("framework_levels", [])
        chapter = next(
            (
                item
                for item in levels
                if int(item.get("level", 0)) == 1
                and int(item.get("parent_id", -1)) == 0
                and item.get("title") == local_topic.chapter_title
            ),
            None,
        )
        topic = next(
            (
                item
                for item in levels
                if chapter is not None
                and int(item.get("level", 0)) == 2
                and int(item.get("parent_id", -1)) == int(chapter["id"])
                and item.get("title") == local_topic.title
            ),
            None,
        )
        if chapter is None or topic is None:
            raise AdaptPublishingError(
                "The selected curated topic does not match the provisioned ADAPT framework.",
                code="adapt_framework_mismatch",
            )
        return ResolvedAlignment(
            framework_id=framework_id,
            framework_title=framework_properties["title"],
            chapter=FrameworkItem(id=int(chapter["id"]), text=chapter["title"]),
            topic=FrameworkItem(id=int(topic["id"]), text=topic["title"]),
            chapter_stable_id=local_topic.chapter_stable_id,
            topic_stable_id=local_topic.stable_id,
        )

    async def create_question(self, payload: dict[str, Any]) -> AdaptCreateResult:
        data = await self._request(
            "POST", "questions", payload=payload, ambiguous_on_failure=True
        )
        try:
            return AdaptCreateResult.model_validate(data)
        except Exception as exc:
            raise AdaptAmbiguousError(
                "ADAPT created or accepted the request but did not return question identifiers. The publication will be reconciled.",
                code="adapt_missing_ids",
            ) from exc

    async def sync_hint_rungs(
        self, question_id: int, payload: dict[str, Any]
    ) -> None:
        data = await self._request(
            "PUT", f"questions/{question_id}/hint-rungs", payload=payload
        )
        if data.get("type") != "success":
            raise AdaptPublishingError(
                "ADAPT did not confirm the hint ladder update.",
                code="adapt_hint_sync_invalid",
            )

    async def find_question_by_tag(self, tag: str) -> AdaptCreateResult | None:
        data = await self._request("GET", "questions")
        matches = [
            item
            for item in data.get("my_questions", [])
            if isinstance(item, dict) and tag in item.get("tags", [])
        ]
        if len(matches) > 1:
            raise AdaptPublishingError(
                "ADAPT contains duplicate questions for the publication key.",
                code="adapt_duplicate_publication",
            )
        if not matches:
            return None
        item = matches[0]
        return AdaptCreateResult(
            question_id=int(item["id"]),
            page_id=int(item["page_id"]),
        )

    @staticmethod
    def _json_object(
        response: httpx.Response, *, code: str = "adapt_invalid_response"
    ) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AdaptPublishingError(
                "ADAPT returned an invalid response.", code=code
            ) from exc
        if not isinstance(data, dict):
            raise AdaptPublishingError("ADAPT returned an invalid response.", code=code)
        return data

    @staticmethod
    def _jwt_expiry(token: str) -> float:
        try:
            segment = token.split(".")[1]
            segment += "=" * (-len(segment) % 4)
            payload = json.loads(base64.urlsafe_b64decode(segment))
            exp = float(payload["exp"])
            return exp
        except (ValueError, KeyError, IndexError, json.JSONDecodeError):
            return time.time() + 300
