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

from app.schemas import QuestionDraft


QTI_EXPORTER_VERSION = "qti-3.0.1-v1"
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
        nsmap={None: QTI_NS, "xsi": XSI_NS},
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
    response = etree.SubElement(
        root,
        _qti("qti-response-declaration"),
        identifier="RESPONSE",
        cardinality="single",
        **{"base-type": "identifier"},
    )
    correct_response = etree.SubElement(response, _qti("qti-correct-response"))
    correct = next(choice for choice in draft.choices if choice.correct)
    etree.SubElement(correct_response, _qti("qti-value")).text = correct.id

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
    interaction = etree.SubElement(
        body,
        _qti("qti-choice-interaction"),
        **{
            "response-identifier": "RESPONSE",
            "max-choices": "1",
            "min-choices": "1",
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
