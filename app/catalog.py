from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .schemas import SourceLicenseMetadata


CATALOG_DIR = Path(__file__).resolve().parent / "catalogs"
CHEMISTRY_SEED = CATALOG_DIR / "fundamentals-gob-chemistry-v1.json"


@dataclass(frozen=True)
class SourceLicense:
    code: str
    version: str | None
    label: str
    evidence_url: str
    mapping_prefix: str


@dataclass(frozen=True)
class CuratedTopic:
    stable_id: str
    title: str
    canonical_url: str
    chapter_stable_id: str
    chapter_title: str


def normalize_source_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    path = quote(unquote(parsed.path), safe="/").rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


@lru_cache
def chemistry_seed() -> dict[str, Any]:
    return json.loads(CHEMISTRY_SEED.read_text())


@lru_cache
def curated_topics() -> tuple[CuratedTopic, ...]:
    topics: list[CuratedTopic] = []
    for chapter in chemistry_seed()["chapters"]:
        for topic in chapter["topics"]:
            topics.append(
                CuratedTopic(
                    stable_id=topic["stable_id"],
                    title=topic["title"],
                    canonical_url=topic["canonical_url"],
                    chapter_stable_id=chapter["stable_id"],
                    chapter_title=chapter["title"],
                )
            )
    return tuple(topics)


def suggested_topic(source_url: str) -> CuratedTopic | None:
    normalized = normalize_source_url(source_url)
    return next(
        (
            topic
            for topic in curated_topics()
            if normalize_source_url(topic.canonical_url) == normalized
        ),
        None,
    )


def source_license(
    source_url: str,
    metadata: SourceLicenseMetadata | None = None,
) -> SourceLicense | None:
    if metadata is not None:
        return SourceLicense(
            code=metadata.code,
            version=metadata.version,
            label=metadata.label,
            evidence_url=metadata.evidence_url,
            mapping_prefix=source_url,
        )
    normalized = normalize_source_url(source_url)
    mappings = (
        SourceLicense(
            code="ccbyncsa",
            version="3.0",
            label="CC BY-NC-SA 3.0",
            evidence_url=chemistry_seed()["framework"]["source_url"],
            mapping_prefix=chemistry_seed()["framework"]["source_url"],
        ),
    )
    matches = [
        item
        for item in mappings
        if normalized == normalize_source_url(item.mapping_prefix)
        or normalized.startswith(normalize_source_url(item.mapping_prefix) + "/")
    ]
    return max(matches, key=lambda item: len(item.mapping_prefix), default=None)
