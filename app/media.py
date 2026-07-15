from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

from .config import Settings
from .schemas import NormalizedPage
from .source_policy import PUBLIC_LIBRETEXTS_HOSTS


class HotspotMediaError(ValueError):
    pass


class HotspotMediaStore:
    MAX_BYTES = 5_000_000
    MAX_PIXELS = 20_000_000
    ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
    GENERIC_BINARY_CONTENT_TYPE = "application/octet-stream"
    ALLOWED_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._dir = Path(settings.hotspot_media_dir)
        self._public_base = settings.hotspot_media_public_base.rstrip("/")
        self._transport = transport

    async def copy_from_page(self, requested_url: str, page: NormalizedPage) -> str:
        discovered = self._discovered_urls(page)
        canonical = _canonical_media_url(requested_url, page.source.canonical_url)
        if canonical not in discovered:
            raise HotspotMediaError(
                "Hotspot images must be selected from the validated source page."
            )
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=20.0,
            follow_redirects=False,
            headers={"User-Agent": "LibreTexts-Assessment-AI/0.3"},
        ) as client:
            request_url = canonical
            for redirect_count in range(2):
                try:
                    response_context = client.stream("GET", request_url)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    raise HotspotMediaError(
                        "The source image could not be retrieved."
                    ) from exc
                try:
                    async with response_context as response:
                        if response.status_code in self.ALLOWED_REDIRECT_STATUSES:
                            if redirect_count or "location" not in response.headers:
                                raise HotspotMediaError(
                                    "The source image redirect is not approved."
                                )
                            request_url = _canonical_media_cdn_redirect(
                                response.headers["location"]
                            )
                            continue
                        body = await self._read_image_response(response)
                        break
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    raise HotspotMediaError(
                        "The source image could not be retrieved."
                    ) from exc
            else:
                raise HotspotMediaError("The source image redirect is not approved.")
        if not body or len(body) > self.MAX_BYTES:
            raise HotspotMediaError("The source image is empty or too large.")
        try:
            with Image.open(io.BytesIO(body)) as source:
                if source.width * source.height > self.MAX_PIXELS:
                    raise HotspotMediaError(
                        "The source image dimensions are too large."
                    )
                source.load()
                cleaned = source.convert("RGBA" if "A" in source.getbands() else "RGB")
                output = io.BytesIO()
                cleaned.save(output, format="PNG", optimize=True)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise HotspotMediaError("The source image is invalid.") from exc
        sanitized = output.getvalue()
        digest = hashlib.sha256(sanitized).hexdigest()
        self._dir.mkdir(parents=True, exist_ok=True)
        destination = self._dir / f"{digest}.png"
        if not destination.exists():
            descriptor, temp_name = tempfile.mkstemp(prefix="hotspot-", dir=self._dir)
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(sanitized)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temp_name, destination)
                destination.chmod(0o644)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        return f"{self._public_base}/{destination.name}"

    async def _read_image_response(self, response: httpx.Response) -> bytes:
        if response.status_code != 200:
            raise HotspotMediaError("The source image could not be retrieved.")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {
            *self.ALLOWED_CONTENT_TYPES,
            self.GENERIC_BINARY_CONTENT_TYPE,
        }:
            raise HotspotMediaError(
                "Only PNG, JPEG, and WebP source images are supported."
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HotspotMediaError("The source image length is invalid.") from exc
            if declared_length < 1 or declared_length > self.MAX_BYTES:
                raise HotspotMediaError("The source image is empty or too large.")
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > self.MAX_BYTES:
                raise HotspotMediaError("The source image is empty or too large.")
        return bytes(chunks)

    @staticmethod
    def _discovered_urls(page: NormalizedPage) -> set[str]:
        return set(supported_page_image_urls(page))


def discovered_page_image_urls(page: NormalizedPage) -> tuple[str, ...]:
    """Return the exact, canonical image URLs present on a validated source page."""

    soup = BeautifulSoup(page.html_body, "html.parser")
    urls = set()
    for image in soup.find_all("img"):
        source = image.get("src")
        if isinstance(source, str):
            urls.add(_canonical_media_url(source, page.source.canonical_url))
    return tuple(sorted(urls))


def supported_page_image_urls(page: NormalizedPage) -> tuple[str, ...]:
    return tuple(
        url
        for url in discovered_page_image_urls(page)
        if urlsplit(url).path.casefold().endswith((".png", ".jpg", ".jpeg", ".webp"))
    )


def _canonical_media_url(value: str, source_url: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HotspotMediaError("A source image URL is required.")
    absolute = urljoin(source_url, value.strip())
    try:
        parsed = urlsplit(absolute)
    except ValueError as exc:
        raise HotspotMediaError("The source image URL is invalid.") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in PUBLIC_LIBRETEXTS_HOSTS
        or parsed.port is not None
        or parsed.username
        or parsed.password
        or not parsed.path
    ):
        raise HotspotMediaError("The source image host is not approved.")
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def _canonical_media_cdn_redirect(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (AttributeError, ValueError) as exc:
        raise HotspotMediaError("The source image redirect is not approved.") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host != "files.mtstatic.com"
        or port is not None
        or parsed.username
        or parsed.password
        or not parsed.path
    ):
        raise HotspotMediaError("The source image redirect is not approved.")
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))
