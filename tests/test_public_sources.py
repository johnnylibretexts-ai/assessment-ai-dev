from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.content import (
    ContentAuthenticationError,
    ContentConfigurationError,
    ContentLimitError,
    ContentNotFoundError,
    ContentResponseError,
    ContentTransportError,
    PUBLIC_API_ORIGIN,
    PUBLIC_API_USER_AGENT,
    PublicLibreTextsContentAdapter,
    UnsafePublicSourceError,
    build_content_adapter,
)
from app.schemas import SourceType
from app.source_policy import (
    PUBLIC_LIBRETEXTS_LIBRARIES,
    PublicSourceValidationError,
    parse_public_source_url,
)


CHEM_URL = (
    "https://chem.libretexts.org/Bookshelves/Introductory_Chemistry/"
    "Fundamentals_of_General_Organic_and_Biological_Chemistry_%28LibreTexts%29/"
    "01%3A_Matter_and_Measurements"
)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "public_sources_enabled": True,
        "cxone_env_file": tmp_path / "missing.env",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def adapter(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    **overrides: object,
) -> PublicLibreTextsContentAdapter:
    return PublicLibreTextsContentAdapter(
        settings(tmp_path, **overrides),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("library", sorted(PUBLIC_LIBRETEXTS_LIBRARIES))
def test_every_official_public_library_is_allowed(library: str) -> None:
    source = parse_public_source_url(
        f"https://{library}.libretexts.org/Bookshelves/Test%20Book/Page"
    )
    assert source.library == library
    assert source.host == f"{library}.libretexts.org"
    assert source.path == "Bookshelves/Test Book/Page"
    assert source.identity == f"{library}.libretexts.org/Bookshelves/Test Book/Page"


@pytest.mark.parametrize(
    "url",
    [
        "http://chem.libretexts.org/Books/Page",
        "https://dev.libretexts.org/Sandboxes/johnnyphung/Page",
        "https://libretexts.org/Books/Page",
        "https://api.libretexts.org/endpoint/info",
        "https://batch.libretexts.org/Books/Page",
        "https://foo.chem.libretexts.org/Books/Page",
        "https://chem.libretexts.org.evil.example/Books/Page",
        "https://evilchem.libretexts.org/Books/Page",
        "https://user:password@chem.libretexts.org/Books/Page",
        "https://chem.libretexts.org:443/Books/Page",
        "https://chem.libretexts.org",
        "https://chem.libretexts.org/",
        "https://chem.libretexts.org/Books/../Private",
        "https://chem.libretexts.org/Books/%2e%2e/Private",
        "https://chem.libretexts.org/Books/%252e%252e/Private",
        "https://chem.libretexts.org/Books//Page",
        "https://chem.libretexts.org/Books/%2F/Page",
        "https://chem.libretexts.org/Books/%GG/Page",
        "https://chem.libretexts.org/Books/Page?bad=%GG",
        "https://chem.libretexts.org/Books/%FF/Page",
        "https://chem.libretexts.org/Books/%00/Page",
        "https://chem.libretexts.org/Books\\Page",
        "https://chem.libretexts.org/Books/Page\x00suffix",
        "https://[",
    ],
)
def test_hostile_or_ambiguous_public_urls_are_rejected(url: str) -> None:
    with pytest.raises(PublicSourceValidationError):
        parse_public_source_url(url)


def test_query_fragment_and_equivalent_escaping_share_identity() -> None:
    encoded = parse_public_source_url(f"{CHEM_URL}?utm=test#section")
    decoded = parse_public_source_url(
        CHEM_URL.replace("%28", "(").replace("%29", ")").replace("%3A", ":")
    )
    assert encoded.identity == decoded.identity
    assert "?" not in encoded.canonical_url
    assert "#" not in encoded.canonical_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "<h2>Matter</h2><p>Measurements compare a quantity with a standard.</p>",
        [
            "<h2>Matter</h2>",
            99,
            "<p>Measurements compare a quantity with a standard.</p>",
        ],
    ],
)
async def test_public_adapter_uses_only_fixed_proxy_calls_and_headers(
    tmp_path: Path, body: object
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={
                    "@id": 86187,
                    "title": "1: Matter and Measurements",
                    "uri.ui": f"{CHEM_URL}?ignored=yes#ignored",
                },
            )
        return httpx.Response(200, json={"body": body})

    async with adapter(tmp_path, handler) as content:
        page = await content.fetch_page(f"{CHEM_URL}?caller=yes#fragment")

    assert [request.method for request in requests] == ["PUT", "PUT"]
    assert [str(request.url) for request in requests] == [
        "https://api.libretexts.org/endpoint/info",
        "https://api.libretexts.org/endpoint/contents",
    ]
    payloads = [json.loads(request.content) for request in requests]
    assert payloads[0] == {
        "subdomain": "chem",
        "path": (
            "Bookshelves/Introductory_Chemistry/"
            "Fundamentals_of_General_Organic_and_Biological_Chemistry_(LibreTexts)/"
            "01:_Matter_and_Measurements"
        ),
        "dreamformat": "json",
    }
    assert payloads[1] == {**payloads[0], "mode": "view"}
    for request in requests:
        assert request.headers["origin"] == PUBLIC_API_ORIGIN
        assert request.headers["user-agent"] == PUBLIC_API_USER_AGENT
        assert "x-deki-token" not in request.headers
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers

    assert page.title == "1: Matter and Measurements"
    assert page.source.backend == "libretexts_public"
    assert page.source.page_id == "86187"
    assert page.source.path.startswith("chem.libretexts.org/Bookshelves/")
    assert page.source.canonical_url == CHEM_URL
    assert "Measurements compare" in page.plaintext
    assert page.plaintext[page.paragraphs[0].start : page.paragraphs[0].end]


@pytest.mark.asyncio
async def test_public_url_is_rejected_before_any_network_request(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with adapter(tmp_path, handler) as content:
        with pytest.raises(UnsafePublicSourceError):
            await content.fetch_page("https://chem.libretexts.org.evil.test/Page")
    assert calls == 0


@pytest.mark.asyncio
async def test_public_adapter_rejects_mismatched_proxy_canonical_url(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={"uri": {"ui": "https://math.libretexts.org/Books/Other"}},
            )
        return httpx.Response(200, json={"body": "<p>Readable source.</p>"})

    async with adapter(tmp_path, handler) as content:
        with pytest.raises(ContentResponseError, match="mismatched"):
            await content.fetch_page(CHEM_URL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (403, ContentAuthenticationError, "refused"),
        (404, ContentNotFoundError, "not found"),
        (429, ContentTransportError, "rate limited"),
        (500, ContentTransportError, "unavailable"),
        (302, ContentTransportError, "unavailable"),
    ],
)
async def test_public_proxy_status_errors_are_friendly_and_do_not_redirect(
    tmp_path: Path,
    status: int,
    error_type: type[Exception],
    message: str,
) -> None:
    secret_body = "proxy-private-detail"
    content = adapter(
        tmp_path,
        lambda _request: httpx.Response(
            status,
            text=secret_body,
            headers={"location": "https://evil.example"},
        ),
    )
    try:
        with pytest.raises(error_type, match=message) as caught:
            await content.fetch_page(CHEM_URL)
    finally:
        await content.aclose()
    assert secret_body not in str(caught.value)


@pytest.mark.asyncio
async def test_public_proxy_malformed_json_and_body_are_rejected(
    tmp_path: Path,
) -> None:
    async with adapter(
        tmp_path, lambda _request: httpx.Response(200, text="not-json-private")
    ) as content:
        with pytest.raises(ContentResponseError, match="malformed JSON"):
            await content.fetch_page(CHEM_URL)

    def malformed_body(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"body": {"unexpected": True}})

    async with adapter(tmp_path, malformed_body) as content:
        with pytest.raises(ContentResponseError, match="page body"):
            await content.fetch_page(CHEM_URL)


@pytest.mark.asyncio
async def test_public_source_size_limit_is_enforced(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"body": f"<p>{'x' * 1001}</p>"})

    async with adapter(tmp_path, handler, max_source_chars=1000) as content:
        with pytest.raises(ContentLimitError):
            await content.fetch_page(CHEM_URL)


def test_public_adapter_is_disabled_by_default(tmp_path: Path) -> None:
    with pytest.raises(ContentConfigurationError, match="not enabled"):
        PublicLibreTextsContentAdapter(settings(tmp_path, public_sources_enabled=False))


def test_content_factory_blocks_sandbox_sources_by_default(tmp_path: Path) -> None:
    with pytest.raises(ContentConfigurationError, match="sandbox sources are disabled"):
        build_content_adapter(settings(tmp_path), SourceType.SANDBOX)
