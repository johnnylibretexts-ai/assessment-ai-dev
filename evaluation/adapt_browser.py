from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from app.adapt import AdaptDestination, build_assessment_payload
from app.schemas import AssessmentItemType, ItemContextType, QuestionDraft

from .fixtures import build_draft


SCHEMA_VERSION = "build08-adapt-browser-manifest-v1"
CANARY_MARKER = "adapt-final-seed-disposable-clone"
SOURCE_URL = (
    "https://chem.libretexts.org/Bookshelves/BUILD08/Assessment_Browser_Qualification"
)


def build_adapt_browser_manifest() -> dict[str, Any]:
    destination = AdaptDestination(
        folder_id=1,
        author="LibreTexts Assessment AI",
        license="ccby",
        public=False,
    )
    records: list[dict[str, Any]] = []
    for item_type in AssessmentItemType:
        if item_type in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}:
            records.append(
                {
                    "fixture_id": f"build08-adapt-{item_type.value}",
                    "item_type": item_type.value,
                    "technology": item_type.value,
                    "existing_title": f"BUILD-08 final seed {item_type.value}-01",
                }
            )
            continue

        draft = build_draft(item_type, ItemContextType.STANDARD)
        payload = build_assessment_payload(
            draft,
            destination=destination,
            source_url=SOURCE_URL,
            title=f"BUILD-08 browser {item_type.value}",
            tags=["assessment-ai", f"build08-browser-{item_type.value}"],
        )
        qti = json.loads(str(payload["qti_json"]))
        if item_type == AssessmentItemType.IMAGE_HOTSPOT:
            qti["imageUrl"] = _fixture_image_data_url()
        records.append(
            {
                "fixture_id": f"build08-adapt-{item_type.value}",
                "item_type": item_type.value,
                "technology": "qti",
                "qti_type": qti["questionType"],
                "qti_json": qti,
                "expected_response": _expected_response(draft, qti),
            }
        )

    canonical = json.dumps(
        records, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return {
        "schema_version": SCHEMA_VERSION,
        "canary_marker": CANARY_MARKER,
        "item_type_count": len(records),
        "native_qti_count": sum(record["technology"] == "qti" for record in records),
        "external_engine_count": sum(
            record["technology"] in {"webwork", "imathas"} for record in records
        ),
        "items_sha256": hashlib.sha256(canonical).hexdigest(),
        "items": records,
    }


def _expected_response(draft: QuestionDraft, qti: dict[str, Any]) -> Any:
    item_type = draft.item_type
    if item_type in {AssessmentItemType.MULTIPLE_CHOICE, AssessmentItemType.TRUE_FALSE}:
        return next(choice["identifier"] for choice in qti["simpleChoice"] if choice["correctResponse"])
    if item_type == AssessmentItemType.NUMERICAL:
        return qti["correctResponse"]["value"]
    if item_type in {
        AssessmentItemType.MULTIPLE_RESPONSE,
        AssessmentItemType.SELECT_ALL,
        AssessmentItemType.SELECT_N,
    }:
        return [response["identifier"] for response in qti["responses"] if response["correctResponse"]]
    if item_type == AssessmentItemType.FILL_IN_BLANK:
        return [blank.correct[0] for blank in draft.response.blanks]
    if item_type in {AssessmentItemType.SELECT_CHOICE, AssessmentItemType.DROPDOWN}:
        return [
            next(
                response["identifier"]
                for response in qti["inline_choice_interactions"][interaction]
                if response["correctResponse"]
            )
            for interaction in qti["inline_choice_interactions"]
        ]
    if item_type == AssessmentItemType.MATCHING:
        return [
            {
                "identifier": term["identifier"],
                "chosenMatchIdentifier": term["matchingTermIdentifier"],
            }
            for term in qti["termsToMatch"]
        ]
    if item_type == AssessmentItemType.ORDERING:
        return qti["correctOrder"]
    if item_type == AssessmentItemType.DRAG_DROP_CLOZE:
        return [response["identifier"] for response in qti["correctResponses"]]
    if item_type == AssessmentItemType.IMAGE_HOTSPOT:
        return [region["id"] for region in qti["regions"] if region["correct"]]
    if item_type == AssessmentItemType.HIGHLIGHT_TEXT:
        return [response["identifier"] for response in qti["responses"] if response["correctResponse"]]
    if item_type == AssessmentItemType.HIGHLIGHT_TABLE:
        return [
            response["identifier"]
            for row in qti["rows"]
            for response in row["responses"]
            if response["correctResponse"]
        ]
    if item_type == AssessmentItemType.MATRIX:
        if qti["questionType"] == "matrix_multiple_choice":
            return [row["correctResponse"] for row in qti["rows"]]
        return [
            response["identifier"]
            for row in qti["rows"]
            for response in row["responses"]
            if response["correctResponse"]
        ]
    if item_type == AssessmentItemType.BOW_TIE:
        return {
            "actionsToTake": [item["identifier"] for item in qti["actionsToTake"]],
            "potentialConditions": [
                next(item["identifier"] for item in qti["potentialConditions"] if item["correctResponse"])
            ],
            "parametersToMonitor": [item["identifier"] for item in qti["parametersToMonitor"]],
        }
    raise ValueError(f"missing browser response for {item_type.value}")


def _fixture_image_data_url() -> str:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" '
        'role="img" aria-label="Object moving down a frictionless ramp">'
        '<rect width="640" height="360" fill="#f4f7fa"/>'
        '<path d="M80 300 L560 80" stroke="#334155" stroke-width="16"/>'
        '<circle cx="240" cy="225" r="38" fill="#006da3"/>'
        "</svg>"
    )
    encoded = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{encoded}"
