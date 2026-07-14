from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from lxml import etree

from app.schemas import AssessmentItemType, QuestionDraft


QTI_EXPORTER_VERSION = "qti-3.0.1-assessment-items-v2"
QTI_NS = "http://www.imsglobal.org/xsd/imsqtiasi_v3p0"
CP_NS = "http://www.imsglobal.org/xsd/qti/qtiv3p0/imscp_v1p1"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
SCHEMA_DIR = Path(__file__).resolve().parent / "qti_schemas"
ITEM_SCHEMA = SCHEMA_DIR / "imsqti_itemv3p0p1_v1p0.xsd"
MANIFEST_SCHEMA = SCHEMA_DIR / "imsqtiv3p0_imscpv1p2_v1p0.xsd"
ITEM_SCHEMA_URL = (
    "https://purl.imsglobal.org/spec/qti/v3p0/schema/xsd/imsqti_itemv3p0p1_v1p0.xsd"
)
MANIFEST_SCHEMA_URL = (
    "https://purl.imsglobal.org/spec/qti/v3p0/schema/xsd/imsqtiv3p0_imscpv1p2_v1p0.xsd"
)
MATCH_CORRECT = "https://purl.imsglobal.org/spec/qti/v3p0/rptemplates/match_correct"
AAI_NS = "https://libretexts.dev/ns/assessment-ai/v1"


class QTIExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class QTIArtifact:
    path: Path
    sha256: str
    size: int


def _qti(name: str) -> str:
    return f"{{{QTI_NS}}}{name}"


def _cp(name: str) -> str:
    return f"{{{CP_NS}}}{name}"


def build_item_xml(
    draft: QuestionDraft,
    *,
    publication_key: str,
    title: str,
    metadata: dict[str, Any],
) -> bytes:
    root = etree.Element(
        _qti("qti-assessment-item"),
        nsmap={None: QTI_NS, "xsi": XSI_NS, "assessment-ai": AAI_NS},
        attrib={
            "identifier": f"assessment-ai-{publication_key}",
            "title": title,
            "time-dependent": "false",
            "tool-name": "LibreTexts Assessment AI",
            "tool-version": QTI_EXPORTER_VERSION,
            f"{{{XML_NS}}}lang": "en-US",
            f"{{{XSI_NS}}}schemaLocation": f"{QTI_NS} {ITEM_SCHEMA_URL}",
        },
    )
    cardinality, base_type, correct_values = _response_declaration(draft)
    response = etree.SubElement(
        root,
        _qti("qti-response-declaration"),
        identifier="RESPONSE",
        cardinality=cardinality,
        **{"base-type": base_type},
    )
    correct_response = etree.SubElement(response, _qti("qti-correct-response"))
    for value in correct_values:
        etree.SubElement(correct_response, _qti("qti-value")).text = value

    outcome = etree.SubElement(
        root,
        _qti("qti-outcome-declaration"),
        identifier="SCORE",
        cardinality="single",
        **{"base-type": "float"},
    )
    default = etree.SubElement(outcome, _qti("qti-default-value"))
    etree.SubElement(default, _qti("qti-value")).text = "0"

    feedback_outcome = etree.SubElement(
        root,
        _qti("qti-outcome-declaration"),
        identifier="FEEDBACK",
        cardinality="single",
        **{"base-type": "identifier"},
    )
    feedback_default = etree.SubElement(feedback_outcome, _qti("qti-default-value"))
    etree.SubElement(feedback_default, _qti("qti-value")).text = "GENERAL"

    body = etree.SubElement(root, _qti("qti-item-body"))
    _append_interaction(body, draft)

    provenance = etree.SubElement(
        body,
        _qti("qti-rubric-block"),
        view="author",
        use="instructions",
    )
    provenance_body = etree.SubElement(provenance, _qti("qti-content-body"))
    etree.SubElement(provenance_body, _qti("p")).text = (
        "Assessment AI provenance: "
        + json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    )

    etree.SubElement(
        root,
        _qti("qti-response-processing"),
        template=MATCH_CORRECT,
    )
    feedback = etree.SubElement(
        root,
        _qti("qti-modal-feedback"),
        **{
            "outcome-identifier": "FEEDBACK",
            "identifier": "GENERAL",
            "show-hide": "show",
            "title": "Explanation",
        },
    )
    feedback_body = etree.SubElement(feedback, _qti("qti-content-body"))
    etree.SubElement(feedback_body, _qti("p")).text = draft.explanation
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )


def _response_declaration(
    draft: QuestionDraft,
) -> tuple[str, str, list[str]]:
    if draft.item_type in {
        AssessmentItemType.MULTIPLE_CHOICE,
        AssessmentItemType.TRUE_FALSE,
        AssessmentItemType.SELECT_CHOICE,
        AssessmentItemType.DROPDOWN,
    }:
        correct = next(choice for choice in draft.choices if choice.correct)
        return "single", "identifier", [correct.id]
    if draft.item_type in {
        AssessmentItemType.MULTIPLE_RESPONSE,
        AssessmentItemType.SELECT_ALL,
        AssessmentItemType.SELECT_N,
    }:
        return (
            "multiple",
            "identifier",
            [choice.id for choice in draft.choices if choice.correct],
        )
    if draft.item_type == AssessmentItemType.ORDERING:
        return "ordered", "identifier", draft.response.correct_order
    if draft.item_type == AssessmentItemType.NUMERICAL:
        return "single", "float", [str(draft.response.numeric_answer)]
    serialized = json.dumps(
        draft.response.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "single", "string", [serialized]


def _append_interaction(body: etree._Element, draft: QuestionDraft) -> None:
    if draft.stimulus:
        etree.SubElement(
            body, _qti("p"), attrib={"class": "assessment-ai-stimulus"}
        ).text = draft.stimulus

    choice_types = {
        AssessmentItemType.MULTIPLE_CHOICE,
        AssessmentItemType.TRUE_FALSE,
        AssessmentItemType.MULTIPLE_RESPONSE,
        AssessmentItemType.SELECT_ALL,
        AssessmentItemType.SELECT_N,
        AssessmentItemType.SELECT_CHOICE,
        AssessmentItemType.DROPDOWN,
    }
    if draft.item_type in choice_types:
        multiple = draft.item_type in {
            AssessmentItemType.MULTIPLE_RESPONSE,
            AssessmentItemType.SELECT_ALL,
            AssessmentItemType.SELECT_N,
        }
        maximum = (
            draft.response.select_n
            if draft.item_type == AssessmentItemType.SELECT_N
            else len(draft.choices)
            if multiple
            else 1
        )
        interaction = etree.SubElement(
            body,
            _qti("qti-choice-interaction"),
            **{
                "response-identifier": "RESPONSE",
                "max-choices": str(maximum),
                "min-choices": str(
                    maximum if draft.item_type == AssessmentItemType.SELECT_N else 1
                ),
                "shuffle": "false",
            },
        )
        etree.SubElement(interaction, _qti("qti-prompt")).text = draft.stem
        for choice in draft.choices:
            etree.SubElement(
                interaction,
                _qti("qti-simple-choice"),
                identifier=choice.id,
            ).text = choice.text
        return

    if draft.item_type == AssessmentItemType.ORDERING:
        interaction = etree.SubElement(
            body,
            _qti("qti-order-interaction"),
            **{
                "response-identifier": "RESPONSE",
                "shuffle": "true",
                "orientation": "vertical",
            },
        )
        etree.SubElement(interaction, _qti("qti-prompt")).text = draft.stem
        for choice in draft.choices:
            etree.SubElement(
                interaction,
                _qti("qti-simple-choice"),
                identifier=choice.id,
            ).text = choice.text
        return

    if draft.item_type == AssessmentItemType.NUMERICAL:
        paragraph = etree.SubElement(body, _qti("p"))
        paragraph.text = draft.stem + " "
        etree.SubElement(
            paragraph,
            _qti("qti-text-entry-interaction"),
            **{"response-identifier": "RESPONSE", "expected-length": "12"},
        )
        return

    etree.SubElement(body, _qti("p")).text = draft.stem
    custom = etree.SubElement(
        body,
        _qti("qti-custom-interaction"),
        **{
            "response-identifier": "RESPONSE",
            "class": f"assessment-ai-{draft.item_type.value}",
        },
    )
    declaration = etree.SubElement(
        custom,
        f"{{{AAI_NS}}}interaction",
        type=draft.item_type.value,
        schema_version=draft.schema_version,
    )
    if draft.item_type in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}:
        declaration.set("requires-engine", draft.item_type.value)
    declaration.text = json.dumps(
        {
            "response": draft.response.model_dump(mode="json"),
            "choices": [choice.model_dump(mode="json") for choice in draft.choices],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_manifest_xml(*, publication_key: str, item_path: str) -> bytes:
    root = etree.Element(
        _cp("manifest"),
        nsmap={None: CP_NS, "xsi": XSI_NS},
        identifier=f"assessment-ai-{publication_key}",
        attrib={f"{{{XSI_NS}}}schemaLocation": f"{CP_NS} {MANIFEST_SCHEMA_URL}"},
    )
    metadata = etree.SubElement(root, _cp("metadata"))
    etree.SubElement(metadata, _cp("schema")).text = "QTI Item"
    etree.SubElement(metadata, _cp("schemaversion")).text = "3.0.0"
    etree.SubElement(root, _cp("organizations"))
    resources = etree.SubElement(root, _cp("resources"))
    resource = etree.SubElement(
        resources,
        _cp("resource"),
        identifier=f"resource-{publication_key}",
        type="imsqti_item_xmlv3p0",
        href=item_path,
    )
    etree.SubElement(resource, _cp("file"), href=item_path)
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )


def validate_qti_xml(item_xml: bytes, manifest_xml: bytes) -> None:
    try:
        item_schema, manifest_schema = _schemas()
        item_schema.assertValid(etree.fromstring(item_xml))
        manifest_schema.assertValid(etree.fromstring(manifest_xml))
    except (etree.XMLSchemaError, etree.DocumentInvalid, etree.XMLSyntaxError) as exc:
        raise QTIExportError("QTI 3.0.1 schema validation failed.") from exc


@lru_cache
def _schemas() -> tuple[etree.XMLSchema, etree.XMLSchema]:
    return (
        etree.XMLSchema(etree.parse(str(ITEM_SCHEMA))),
        etree.XMLSchema(etree.parse(str(MANIFEST_SCHEMA))),
    )


def preflight_qti(
    draft: QuestionDraft,
    *,
    publication_key: str,
    title: str,
    metadata: dict[str, Any],
) -> None:
    item_path = f"items/assessment-ai-{publication_key}.xml"
    validate_qti_xml(
        build_item_xml(
            draft,
            publication_key=publication_key,
            title=title,
            metadata={**metadata, "adapt_question_id": "pending"},
        ),
        build_manifest_xml(publication_key=publication_key, item_path=item_path),
    )


def write_qti_package(
    draft: QuestionDraft,
    *,
    publication_key: str,
    title: str,
    metadata: dict[str, Any],
    storage_dir: Path,
) -> QTIArtifact:
    item_path = f"items/assessment-ai-{publication_key}.xml"
    item_xml = build_item_xml(
        draft,
        publication_key=publication_key,
        title=title,
        metadata=metadata,
    )
    manifest_xml = build_manifest_xml(
        publication_key=publication_key, item_path=item_path
    )
    validate_qti_xml(item_xml, manifest_xml)
    storage_dir.mkdir(parents=True, exist_ok=True)
    destination = storage_dir / f"{publication_key}.zip"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{publication_key}.", suffix=".tmp", dir=storage_dir
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            _write_deterministic(archive, "imsmanifest.xml", manifest_xml)
            _write_deterministic(archive, item_path, item_xml)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    data = destination.read_bytes()
    return QTIArtifact(
        path=destination,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def _write_deterministic(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o100644 << 16
    entry.create_system = 3
    archive.writestr(entry, content)
