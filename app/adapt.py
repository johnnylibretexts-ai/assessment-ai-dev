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
from app.schemas import QuestionDraft


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

    prompt_html = f"<p>{escape(draft.stem)}</p>"
    simple_choices: list[dict[str, object]] = []
    feedback: dict[str, str] = {}
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

    for index, choice in enumerate(draft.choices):
        identifier = f"assessment-ai-{choice.id.lower()}"
        simple_choices.append(
            {
                "identifier": identifier,
                "value": escape(choice.text),
                "correctResponse": choice.correct,
            }
        )
        payload[f"qti_simple_choice_{index}"] = choice.text
        if choice.feedback:
            feedback[identifier] = escape(choice.feedback)

    qti_json: dict[str, object] = {
        "questionType": "multiple_choice",
        "prompt": prompt_html,
        "simpleChoice": simple_choices,
    }
    if feedback:
        qti_json["feedback"] = feedback
    payload["qti_json"] = json.dumps(
        qti_json, separators=(",", ":"), ensure_ascii=False
    )

    if alignment is not None:
        payload["framework_item_sync_question"] = alignment.model_dump()

    return payload


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
