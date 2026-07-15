from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.content import ContentTransportError, PublicLibreTextsContentAdapter
from app.config import Settings
from app.schemas import NormalizedPage
from app.source_policy import PublicSourceValidationError, parse_public_source_url

from .models import CorpusManifest, CorpusPage, DomainStratum


BUILD05_TREE_SHA256 = "99ddd21ddac269d69f75d1f5e3b4182079980882830e38ad3cb28617f66702d6"


class CorpusSource(BaseModel):
    page_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    stratum: DomainStratum
    canonical_url: str = Field(min_length=1, max_length=4_096)
    license: str = Field(min_length=2, max_length=200)
    selection_basis: Literal["build05_page", "build05_catalog_link"]
    build05_path: str = Field(min_length=1, max_length=1_024)

    @model_validator(mode="after")
    def validate_source(self) -> "CorpusSource":
        try:
            location = parse_public_source_url(self.canonical_url)
        except PublicSourceValidationError as exc:
            raise ValueError("corpus source is not an approved public page") from exc
        self.canonical_url = location.canonical_url

        path = PurePosixPath(self.build05_path)
        if path.is_absolute() or ".." in path.parts or path.name != "index.html":
            raise ValueError("build05_path must be a relative index.html path")
        if not self.license.startswith("CC "):
            raise ValueError(
                "corpus source requires an explicit Creative Commons license"
            )
        return self


class CorpusSourceCatalog(BaseModel):
    schema_version: Literal["build08-corpus-sources-v1"] = "build08-corpus-sources-v1"
    build05_tree_sha256: Literal[BUILD05_TREE_SHA256] = BUILD05_TREE_SHA256
    sources: list[CorpusSource] = Field(min_length=48, max_length=48)

    @model_validator(mode="after")
    def validate_distribution(self) -> "CorpusSourceCatalog":
        for field_name, values in {
            "page_key": [source.page_key for source in self.sources],
            "canonical_url": [source.canonical_url for source in self.sources],
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"corpus source {field_name} values must be unique")
        counts = Counter(source.stratum for source in self.sources)
        if any(counts[stratum] != 8 for stratum in DomainStratum):
            raise ValueError("corpus sources require exactly eight pages per stratum")
        return self


FetchPage = Callable[[str], Awaitable[NormalizedPage]]


def load_corpus_source_catalog(path: Path) -> CorpusSourceCatalog:
    return CorpusSourceCatalog.model_validate_json(path.read_text(encoding="utf-8"))


async def build_corpus_manifest(
    catalog: CorpusSourceCatalog,
    fetch_page: FetchPage,
    *,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> CorpusManifest:
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be at least one")

    pages: list[CorpusPage] = []
    for source in catalog.sources:
        page = await _fetch_with_retry(
            fetch_page,
            source.canonical_url,
            retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        expected = parse_public_source_url(source.canonical_url)
        observed = parse_public_source_url(page.source.canonical_url)
        if (
            expected.identity != observed.identity
            or page.source.path != expected.identity
        ):
            raise ValueError(f"source identity mismatch for {source.page_key}")

        pages.append(
            CorpusPage(
                page_key=source.page_key,
                stratum=source.stratum,
                title=page.title,
                canonical_url=observed.canonical_url,
                source_identity=observed.identity,
                source_page_id=page.source.page_id,
                license=source.license,
                content_sha256=_sha256(page.plaintext),
                paragraph_sha256=[
                    _sha256(f"{paragraph.index}\0{paragraph.text}")
                    for paragraph in page.paragraphs
                ],
            )
        )
    return CorpusManifest(pages=pages)


async def build_public_corpus_manifest(
    catalog: CorpusSourceCatalog,
    settings: Settings,
    *,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> CorpusManifest:
    public_settings = settings.model_copy(update={"public_sources_enabled": True})
    async with PublicLibreTextsContentAdapter(public_settings) as adapter:
        return await build_corpus_manifest(
            catalog,
            adapter.fetch_page,
            retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )


async def _fetch_with_retry(
    fetch_page: FetchPage,
    source_url: str,
    *,
    retry_attempts: int,
    retry_delay_seconds: float,
) -> NormalizedPage:
    for attempt in range(1, retry_attempts + 1):
        try:
            return await fetch_page(source_url)
        except ContentTransportError:
            if attempt == retry_attempts:
                raise
            await asyncio.sleep(retry_delay_seconds * attempt)
    raise AssertionError("unreachable")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BUILD05_TREE_SHA256",
    "CorpusSource",
    "CorpusSourceCatalog",
    "build_corpus_manifest",
    "build_public_corpus_manifest",
    "load_corpus_source_catalog",
]
