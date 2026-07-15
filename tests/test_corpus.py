from __future__ import annotations

import hashlib

import pytest

from app.content import ContentTransportError
from app.schemas import NormalizedPage, Paragraph, SourceInfo
from app.source_policy import parse_public_source_url
from evaluation.corpus import (
    BUILD05_TREE_SHA256,
    CorpusSource,
    CorpusSourceCatalog,
    build_corpus_manifest,
)
from evaluation.models import DomainStratum


def _catalog() -> CorpusSourceCatalog:
    sources: list[CorpusSource] = []
    for stratum in DomainStratum:
        library = {
            DomainStratum.CHEMISTRY: "chem",
            DomainStratum.BIOLOGY: "bio",
            DomainStratum.MATHEMATICS: "math",
            DomainStratum.MEDICINE_HEALTH: "med",
            DomainStratum.HUMANITIES_SOCIAL: "socialsci",
            DomainStratum.SPANISH_FRENCH: "human",
        }[stratum]
        for index in range(8):
            sources.append(
                CorpusSource(
                    page_key=f"{stratum.value}-{index + 1:02d}",
                    stratum=stratum,
                    canonical_url=(
                        f"https://{library}.libretexts.org/Books/Test/Page_{index + 1}"
                    ),
                    license="CC BY 4.0",
                    selection_basis="build05_page",
                    build05_path=f"{stratum.value}/{index + 1}/index.html",
                )
            )
    return CorpusSourceCatalog(
        build05_tree_sha256=BUILD05_TREE_SHA256,
        sources=sources,
    )


def _page(source_url: str) -> NormalizedPage:
    source = parse_public_source_url(source_url)
    text = f"Title for {source.identity}"
    second = "A second paragraph."
    return NormalizedPage(
        title=text,
        plaintext=f"{text}\n\n{second}",
        htmlBody=f"<h1>{text}</h1><p>{second}</p>",
        paragraphs=[
            Paragraph(index=0, text=text, start=0, end=len(text)),
            Paragraph(
                index=1,
                text=second,
                start=len(text) + 2,
                end=len(text) + 2 + len(second),
            ),
        ],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url=source.canonical_url,
            path=source.identity,
            page_id=hashlib.sha256(source.identity.encode()).hexdigest()[:8],
        ),
    )


@pytest.mark.asyncio
async def test_corpus_builder_fetches_balanced_read_only_sources() -> None:
    observed: list[str] = []

    async def fetch(source_url: str) -> NormalizedPage:
        observed.append(source_url)
        return _page(source_url)

    manifest = await build_corpus_manifest(_catalog(), fetch)

    assert len(observed) == 48
    assert len(manifest.pages) == 48
    assert len({page.content_sha256 for page in manifest.pages}) == 48
    assert all(len(page.paragraph_sha256) == 2 for page in manifest.pages)
    assert all(len(set(page.paragraph_sha256)) == 2 for page in manifest.pages)


@pytest.mark.asyncio
async def test_corpus_builder_retries_bounded_transport_failure() -> None:
    attempts = 0

    async def fetch(source_url: str) -> NormalizedPage:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ContentTransportError("temporary")
        return _page(source_url)

    manifest = await build_corpus_manifest(
        _catalog(), fetch, retry_attempts=2, retry_delay_seconds=0
    )

    assert len(manifest.pages) == 48
    assert attempts == 49


def test_corpus_catalog_rejects_non_cc_license() -> None:
    with pytest.raises(ValueError, match="Creative Commons"):
        CorpusSource(
            page_key="invalid-license",
            stratum=DomainStratum.CHEMISTRY,
            canonical_url="https://chem.libretexts.org/Books/Test/Page",
            license="All rights reserved",
            selection_basis="build05_page",
            build05_path="chemistry/page/index.html",
        )
