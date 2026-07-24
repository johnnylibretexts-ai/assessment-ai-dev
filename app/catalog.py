from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import UUID

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
class CuratedFramework:
    stable_id_namespace: str
    title: str
    author: str
    descriptor_type: str
    description: str
    license: str
    license_version: str | None
    source_url: str
    seed_path: Path


@dataclass(frozen=True)
class CuratedTopic:
    stable_id: str
    title: str
    canonical_url: str
    chapter_stable_id: str
    chapter_title: str
    framework: CuratedFramework


@dataclass(frozen=True)
class CuratedAlignment:
    framework: CuratedFramework
    topic: CuratedTopic


def normalize_source_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    path = quote(unquote(parsed.path), safe="/").rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _catalog_paths() -> tuple[Path, ...]:
    return tuple(sorted(CATALOG_DIR.glob("*.json")))


def _load_seed(path: Path) -> dict[str, Any]:
    seed = json.loads(path.read_text())
    if seed.get("schema_version") != 1:
        raise ValueError(f"{path.name}: unsupported framework schema")
    framework = seed.get("framework")
    chapters = seed.get("chapters")
    if (
        not isinstance(framework, dict)
        or not isinstance(chapters, list)
        or not chapters
    ):
        raise ValueError(f"{path.name}: invalid framework catalog")
    required_framework = {
        "title",
        "author",
        "descriptor_type",
        "description",
        "license",
        "source_url",
    }
    missing = sorted(required_framework - set(framework))
    if missing:
        raise ValueError(
            f"{path.name}: missing framework field(s): {', '.join(missing)}"
        )
    namespace = str(seed.get("stable_id_namespace", ""))
    try:
        UUID(namespace)
    except ValueError:
        raise ValueError(f"{path.name}: invalid stable ID namespace") from None
    return seed


@lru_cache
def framework_seeds() -> tuple[tuple[Path, dict[str, Any]], ...]:
    seeds = tuple((path, _load_seed(path)) for path in _catalog_paths())
    if not seeds:
        raise ValueError("no curated framework catalogs are installed")
    return seeds


@lru_cache
def chemistry_seed() -> dict[str, Any]:
    """Compatibility accessor for the original GOB Chemistry seed."""

    return _load_seed(CHEMISTRY_SEED)


@lru_cache
def curated_frameworks() -> tuple[CuratedFramework, ...]:
    frameworks = tuple(
        CuratedFramework(
            stable_id_namespace=str(seed["stable_id_namespace"]),
            title=str(seed["framework"]["title"]),
            author=str(seed["framework"]["author"]),
            descriptor_type=str(seed["framework"]["descriptor_type"]),
            description=str(seed["framework"]["description"]),
            license=str(seed["framework"]["license"]),
            license_version=(
                str(seed["framework"]["license_version"])
                if seed["framework"].get("license_version") is not None
                else None
            ),
            source_url=str(seed["framework"]["source_url"]),
            seed_path=path,
        )
        for path, seed in framework_seeds()
    )
    titles = [item.title for item in frameworks]
    namespaces = [item.stable_id_namespace for item in frameworks]
    source_urls = [normalize_source_url(item.source_url) for item in frameworks]
    for label, values in (
        ("title", titles),
        ("namespace", namespaces),
        ("source URL", source_urls),
    ):
        duplicates = sorted({item for item in values if values.count(item) > 1})
        if duplicates:
            raise ValueError(
                f"duplicate curated framework {label}(s): {', '.join(duplicates)}"
            )
    return frameworks


@lru_cache
def curated_topics() -> tuple[CuratedTopic, ...]:
    frameworks_by_path = {
        framework.seed_path: framework for framework in curated_frameworks()
    }
    topics: list[CuratedTopic] = []
    chapter_ids: set[str] = set()
    topic_ids: set[str] = set()
    topic_urls: set[str] = set()
    for path, seed in framework_seeds():
        framework = frameworks_by_path[path]
        for chapter in seed["chapters"]:
            chapter_id = str(chapter["stable_id"])
            if chapter_id in chapter_ids:
                raise ValueError(f"duplicate curated chapter ID: {chapter_id}")
            UUID(chapter_id)
            chapter_ids.add(chapter_id)
            for topic in chapter["topics"]:
                topic_id = str(topic["stable_id"])
                normalized_url = normalize_source_url(str(topic["canonical_url"]))
                if topic_id in topic_ids:
                    raise ValueError(f"duplicate curated topic ID: {topic_id}")
                if normalized_url in topic_urls:
                    raise ValueError(f"duplicate curated topic URL: {normalized_url}")
                UUID(topic_id)
                topic_ids.add(topic_id)
                topic_urls.add(normalized_url)
                topics.append(
                    CuratedTopic(
                        stable_id=topic_id,
                        title=str(topic["title"]),
                        canonical_url=str(topic["canonical_url"]),
                        chapter_stable_id=chapter_id,
                        chapter_title=str(chapter["title"]),
                        framework=framework,
                    )
                )
    return tuple(topics)


def alignment_for_source(source_url: str) -> CuratedAlignment | None:
    normalized = normalize_source_url(source_url)
    topic = next(
        (
            item
            for item in curated_topics()
            if normalize_source_url(item.canonical_url) == normalized
        ),
        None,
    )
    if topic is None:
        return None
    return CuratedAlignment(framework=topic.framework, topic=topic)


def alignment_by_topic_id(topic_stable_id: str) -> CuratedAlignment | None:
    topic = next(
        (item for item in curated_topics() if item.stable_id == topic_stable_id),
        None,
    )
    if topic is None:
        return None
    return CuratedAlignment(framework=topic.framework, topic=topic)


def suggested_topic(source_url: str) -> CuratedTopic | None:
    """Compatibility wrapper returning the exact curated source mapping."""

    alignment = alignment_for_source(source_url)
    return alignment.topic if alignment is not None else None


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
    mappings = tuple(
        SourceLicense(
            code=framework.license,
            version=framework.license_version,
            label=_license_label(framework.license, framework.license_version),
            evidence_url=framework.source_url,
            mapping_prefix=framework.source_url,
        )
        for framework in curated_frameworks()
    )
    matches = [
        item
        for item in mappings
        if normalized == normalize_source_url(item.mapping_prefix)
        or normalized.startswith(normalize_source_url(item.mapping_prefix) + "/")
    ]
    return max(matches, key=lambda item: len(item.mapping_prefix), default=None)


def _license_label(code: str, version: str | None) -> str:
    labels = {
        "publicdomain": "Public domain",
        "ccby": "CC BY",
        "ccbync": "CC BY-NC",
        "ccbyncsa": "CC BY-NC-SA",
        "ccbysa": "CC BY-SA",
        "arr": "All rights reserved",
    }
    base = labels.get(code, code)
    return f"{base} {version}" if version else base
