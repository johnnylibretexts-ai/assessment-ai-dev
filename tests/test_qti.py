from __future__ import annotations

import json
import zipfile
from pathlib import Path

from lxml import etree

from app.qti import QTI_NS, write_qti_package
from app.schemas import BloomLevel, Choice, Difficulty, QuestionDraft


def draft() -> QuestionDraft:
    return QuestionDraft(
        concept_label="Conservation of energy",
        stem="Which statement best describes energy?",
        choices=[
            Choice(id="A", text="It is conserved.", correct=True),
            Choice(id="B", text="It disappears.", correct=False),
            Choice(id="C", text="It is matter.", correct=False),
            Choice(id="D", text="It has no units.", correct=False),
        ],
        explanation="The cited passage states that total energy is conserved.",
        bloom=BloomLevel.UNDERSTAND,
        difficulty=Difficulty.EASY,
        citation_paragraphs=[2],
    )


def test_qti_package_is_schema_valid_deterministic_and_contains_provenance(
    tmp_path: Path,
) -> None:
    metadata = {
        "bloom": "understand",
        "difficulty": "easy",
        "license": "CC BY-NC-SA 3.0",
        "canonical_source": "https://chem.libretexts.org/example",
        "cited_paragraphs": [{"index": 2, "text": "Energy is conserved."}],
        "model_id": "gemini-2.5-flash",
        "prompt_version": "mcq-revision-v1",
        "reviewer": "reviewer@example.org",
        "reviewed_at": "2026-07-11T12:00:00+00:00",
        "stable_topic_id": "f429caaa-4f77-52a7-89e2-eb939b700f64",
        "adapt_question_id": 321,
    }
    first = write_qti_package(
        draft(),
        publication_key="a" * 64,
        title="Energy question",
        metadata=metadata,
        storage_dir=tmp_path,
    )
    first_bytes = first.path.read_bytes()
    second = write_qti_package(
        draft(),
        publication_key="a" * 64,
        title="Energy question",
        metadata=metadata,
        storage_dir=tmp_path,
    )

    assert second.sha256 == first.sha256
    assert second.path.read_bytes() == first_bytes
    with zipfile.ZipFile(second.path) as archive:
        assert archive.namelist() == [
            "imsmanifest.xml",
            f"items/assessment-ai-{'a' * 64}.xml",
        ]
        item = etree.fromstring(archive.read(archive.namelist()[1]))
        correct = item.find(
            f"{{{QTI_NS}}}qti-response-declaration/"
            f"{{{QTI_NS}}}qti-correct-response/{{{QTI_NS}}}qti-value"
        )
        assert correct is not None and correct.text == "A"
        processing = item.find(f"{{{QTI_NS}}}qti-response-processing")
        assert processing is not None
        assert processing.get("template", "").endswith("/match_correct")
        rendered = etree.tostring(item, encoding="unicode")
        assert draft().explanation in rendered
        assert json.dumps(metadata, sort_keys=True, separators=(",", ":")) in rendered
