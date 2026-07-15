import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.media import (
    HotspotMediaError,
    HotspotMediaStore,
    discovered_page_image_urls,
    supported_page_image_urls,
)
from app.schemas import NormalizedPage, Paragraph, SourceInfo


def png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 10), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


def page(image_url: str) -> NormalizedPage:
    return NormalizedPage(
        title="Diagram",
        plaintext="A diagram.",
        htmlBody=f'<p>A diagram.</p><img src="{image_url}" alt="A diagram">',
        paragraphs=[Paragraph(index=0, text="A diagram.", start=0, end=10)],
        source=SourceInfo(
            backend="libretexts_public",
            canonical_url="https://chem.libretexts.org/Books/Page",
            path="chem.libretexts.org/Books/Page",
            page_id="1",
        ),
    )


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'media.db'}",
        hotspot_media_dir=tmp_path / "media",
    )


def test_discovered_page_image_urls_are_canonical_sorted_and_deduplicated() -> None:
    source_page = page("/media/b.png")
    source_page.html_body += (
        '<img src="https://chem.libretexts.org/media/a.png#fragment">'
        '<img src="/media/b.png">'
    )

    assert discovered_page_image_urls(source_page) == (
        "https://chem.libretexts.org/media/a.png",
        "https://chem.libretexts.org/media/b.png",
    )


def test_supported_page_image_urls_exclude_unsafe_image_formats() -> None:
    source_page = page("/media/diagram.jpg")
    source_page.html_body += (
        '<img src="/media/vector.svg">'
        '<img src="/media/animation.gif">'
        '<img src="/media/no-extension">'
        '<img src="/media/vector.svg.png">'
    )

    assert supported_page_image_urls(source_page) == (
        "https://chem.libretexts.org/media/diagram.jpg",
        "https://chem.libretexts.org/media/vector.svg.png",
    )


@pytest.mark.asyncio
async def test_hotspot_image_must_be_discovered_then_is_reencoded_locally(
    tmp_path: Path,
) -> None:
    source = "https://chem.libretexts.org/@api/deki/files/123/diagram.png"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == source
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            content=png(),
            headers={"content-type": "application/octet-stream"},
        )

    store = HotspotMediaStore(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    first = await store.copy_from_page(source, page(source))
    second = await store.copy_from_page(source + "#fragment", page(source))
    assert first == second
    assert first.startswith("https://assess-ai.libretexts.dev/media/")
    files = list((tmp_path / "media").glob("*.png"))
    assert len(files) == 1
    with Image.open(files[0]) as cleaned:
        assert cleaned.size == (20, 10)
        assert not cleaned.info


@pytest.mark.asyncio
async def test_hotspot_allows_one_pinned_mindtouch_cdn_redirect(
    tmp_path: Path,
) -> None:
    source = "https://chem.libretexts.org/@api/deki/files/123/diagram.png"
    cdn = "https://files.mtstatic.com/site_4334/123/0?Signature=test"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url == source:
            return httpx.Response(302, headers={"location": cdn})
        assert request.url == cdn
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    store = HotspotMediaStore(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    copied = await store.copy_from_page(source, page(source))

    assert copied.startswith("https://assess-ai.libretexts.dev/media/")
    assert calls == [source, cdn]


@pytest.mark.asyncio
async def test_hotspot_rejects_undiscovered_redirect_svg_and_hostile_hosts(
    tmp_path: Path,
) -> None:
    approved = "https://chem.libretexts.org/media/diagram.png"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302, headers={"location": "https://evil.example/image.png"}
        )

    store = HotspotMediaStore(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(HotspotMediaError):
        await store.copy_from_page("https://evil.example/image.png", page(approved))
    with pytest.raises(HotspotMediaError):
        await store.copy_from_page(
            "https://chem.libretexts.org/media/other.png", page(approved)
        )
    with pytest.raises(HotspotMediaError):
        await store.copy_from_page(approved, page(approved))
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "https://assess-ai.libretexts.dev/media/internal.png",
        "https://files.libretexts.net/private/internal.png",
        "https://files.mtstatic.com/site_4334/private/internal.png",
        "https://chem.libretexts.org.evil.example/media/diagram.png",
    ],
)
async def test_hotspot_rejects_non_library_hosts_before_network(
    tmp_path: Path, source: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    store = HotspotMediaStore(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(HotspotMediaError, match="host is not approved"):
        await store.copy_from_page(source, page(source))
    assert calls == 0


@pytest.mark.asyncio
async def test_hotspot_streaming_limit_rejects_oversized_body(
    tmp_path: Path,
) -> None:
    source = "https://chem.libretexts.org/media/diagram.png"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (HotspotMediaStore.MAX_BYTES + 1),
            headers={"content-type": "image/png"},
        )

    store = HotspotMediaStore(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(HotspotMediaError, match="too large"):
        await store.copy_from_page(source, page(source))


@pytest.mark.asyncio
async def test_hotspot_rejects_oversized_declared_length_without_reading_body(
    tmp_path: Path,
) -> None:
    source = "https://chem.libretexts.org/media/diagram.png"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=png(),
            headers={
                "content-type": "image/png",
                "content-length": str(HotspotMediaStore.MAX_BYTES + 1),
            },
        )

    store = HotspotMediaStore(
        settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(HotspotMediaError, match="too large"):
        await store.copy_from_page(source, page(source))
