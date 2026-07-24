from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import SecretStr

from .config import Settings
from .schemas import (
    NormalizedPage,
    Paragraph,
    SourceInfo,
    SourceLicenseMetadata,
    SourceType,
    TocNode,
)
from .source_policy import (
    PublicSourceLocation,
    PublicSourceValidationError,
    parse_public_source_url,
)


CXONE_HOST = "dev.libretexts.org"
SANDBOX_ROOT = "Sandboxes/johnnyphung"
CXONE_API_BASE = f"https://{CXONE_HOST}/@api/deki"
PUBLIC_API_BASE = "https://api.libretexts.org/endpoint"
PUBLIC_API_ORIGIN = "https://libretexts.org"
PUBLIC_API_USER_AGENT = "LibreTexts-Assessment-AI/0.2"

DEFAULT_TOC_MAX_DEPTH = 8
DEFAULT_TOC_MAX_PAGES = 500
ABSOLUTE_TOC_MAX_DEPTH = 16
ABSOLUTE_TOC_MAX_PAGES = 1_000

_CREDENTIAL_NAMES = ("SERVER_KEY", "SERVER_SECRET", "SERVER_USER")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_NON_CONTENT_TAGS = ("script", "style", "noscript", "template", "svg")
_BLOCK_TAGS = (
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "details",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "summary",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
)
_LICENSE_LABELS = {
    "publicdomain": "Public domain",
    "ccby": "CC BY",
    "ccbync": "CC BY-NC",
    "ccbyncsa": "CC BY-NC-SA",
    "ccbysa": "CC BY-SA",
    "arr": "All rights reserved",
}


class ContentAdapterError(RuntimeError):
    """Safe base error for content ingestion failures."""

    code = "content_adapter_error"


class ContentConfigurationError(ContentAdapterError):
    code = "content_configuration_error"


class UnsafeSandboxPathError(ContentAdapterError):
    code = "unsafe_sandbox_path"


class UnsafePublicSourceError(ContentAdapterError):
    code = "unsafe_public_source"


class ContentAuthenticationError(ContentAdapterError):
    code = "content_authentication_error"


class ContentNotFoundError(ContentAdapterError):
    code = "content_not_found"


class ContentTransportError(ContentAdapterError):
    code = "content_transport_error"


class ContentResponseError(ContentAdapterError):
    code = "content_response_error"


class ContentLimitError(ContentAdapterError):
    code = "content_limit_error"


@dataclass(frozen=True, slots=True)
class _Subpage:
    title: str
    path: str
    page_id: str | None
    has_children: bool | None


def _secret_value(value: SecretStr | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value().strip()
    return str(value).strip()


def _read_credential_file(path: Path) -> dict[str, str]:
    """Read only the three CXone credential fields from a dotenv-style file."""

    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContentConfigurationError(
            "The configured CXone credential file could not be read."
        ) from exc

    credentials: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _CREDENTIAL_NAMES:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        credentials[key] = value.strip()
    return credentials


def _load_credentials(settings: Settings) -> tuple[str, str, str]:
    """Load credentials without ever formatting their values into an error."""

    credentials = {
        "SERVER_KEY": _secret_value(settings.server_key),
        "SERVER_SECRET": _secret_value(settings.server_secret),
        "SERVER_USER": _secret_value(settings.server_user),
    }

    # A compose env_file exposes these bare names directly to the process. Explicit
    # Settings values remain authoritative when both forms are present.
    for name in _CREDENTIAL_NAMES:
        if not credentials[name]:
            credentials[name] = os.environ.get(name, "").strip()

    if any(not credentials[name] for name in _CREDENTIAL_NAMES):
        override = os.environ.get("CXONE_ENV_FILE", "").strip()
        env_path = Path(override) if override else settings.cxone_env_file
        file_credentials = _read_credential_file(env_path)
        for name in _CREDENTIAL_NAMES:
            if not credentials[name]:
                credentials[name] = file_credentials.get(name, "").strip()

    missing = [name for name in _CREDENTIAL_NAMES if not credentials[name]]
    if missing:
        raise ContentConfigurationError(
            "Missing required CXone credentials: " + ", ".join(missing) + "."
        )

    return (
        credentials["SERVER_KEY"],
        credentials["SERVER_SECRET"],
        credentials["SERVER_USER"],
    )


def _validate_sandbox_path(raw_path: str) -> str:
    """Return a canonical sandbox path, rejecting ambiguous input before encoding."""

    if not isinstance(raw_path, str):
        raise UnsafeSandboxPathError("A sandbox page path is required.")
    path = raw_path.strip()
    if not path or _CONTROL_CHARACTER.search(path):
        raise UnsafeSandboxPathError("The sandbox page path is invalid.")
    if path.startswith("//") or "\\" in path or "%" in path:
        raise UnsafeSandboxPathError("The sandbox page path is invalid.")

    try:
        parsed = urlsplit(path)
    except ValueError as exc:
        raise UnsafeSandboxPathError("The sandbox page path is invalid.") from exc
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise UnsafeSandboxPathError("Pass a sandbox path, not a URL.")

    path = path.strip("/")
    segments = path.split("/")
    if any(not segment or segment.strip() in {".", ".."} for segment in segments):
        raise UnsafeSandboxPathError("The sandbox page path is invalid.")

    root_segments = SANDBOX_ROOT.split("/")
    if len(segments) < len(root_segments) or [
        segment.casefold() for segment in segments[: len(root_segments)]
    ] != [segment.casefold() for segment in root_segments]:
        raise UnsafeSandboxPathError(
            "The page path must stay inside Sandboxes/johnnyphung."
        )

    suffix = segments[len(root_segments) :]
    return "/".join([*root_segments, *suffix])


def _page_id(path: str) -> str:
    """Return the MindTouch ``=`` page id with two rounds of percent encoding."""

    return "=" + quote(quote(path, safe=""), safe="")


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        text = value.get("#text")
        return text.strip() if isinstance(text, str) else ""
    return ""


def _page_identifier(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        rendered = str(value).strip()
        return rendered or None
    return None


def _child_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _source_license_from_tags(
    document: Mapping[str, Any], *, evidence_url: str
) -> SourceLicenseMetadata | None:
    """Return a verified license only when page tags are unambiguous and supported."""

    raw_entries = document.get("tag", [])
    if isinstance(raw_entries, Mapping):
        entries = [raw_entries]
    elif isinstance(raw_entries, list):
        entries = [entry for entry in raw_entries if isinstance(entry, Mapping)]
    else:
        return None

    values: set[str] = set()
    for entry in entries:
        value = _string_value(entry.get("@value")) or _string_value(
            entry.get("title")
        )
        if value:
            values.add(value.strip().casefold())

    license_codes = {
        value.removeprefix("license:")
        for value in values
        if value.startswith("license:")
    }
    versions = {
        value.removeprefix("licenseversion:")
        for value in values
        if value.startswith("licenseversion:")
    }
    if len(license_codes) != 1 or len(versions) > 1:
        return None

    code = next(iter(license_codes))
    base_label = _LICENSE_LABELS.get(code)
    if base_label is None:
        return None

    version: str | None = None
    if versions:
        raw_version = next(iter(versions)).strip()
        if re.fullmatch(r"\d{2}", raw_version):
            version = f"{raw_version[0]}.{raw_version[1]}"
        elif re.fullmatch(r"\d+(?:\.\d+)+", raw_version):
            version = raw_version
        else:
            return None
    if code.startswith("cc") and version is None:
        return None

    label = (
        f"{base_label} {version}"
        if version is not None and code.startswith("cc")
        else base_label
    )
    return SourceLicenseMetadata(
        code=code,
        version=version,
        label=label,
        evidence_url=evidence_url,
    )


def _normalize_html(html_body: str) -> tuple[str, list[Paragraph]]:
    soup = BeautifulSoup(html_body, "html.parser")
    for tag in soup.find_all(_NON_CONTENT_TAGS):
        tag.decompose()

    for image in soup.find_all("img"):
        alt = " ".join(str(image.get("alt") or "").split())
        image.replace_with(f" [Image: {alt}] " if alt else " ")
    for line_break in soup.find_all("br"):
        line_break.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")

    paragraphs_text: list[str] = []
    for line in soup.get_text("", strip=False).replace("\xa0", " ").splitlines():
        normalized = " ".join(line.split())
        if normalized:
            paragraphs_text.append(normalized)

    if not paragraphs_text:
        raise ContentResponseError("The source page did not contain readable text.")

    plaintext = "\n\n".join(paragraphs_text)
    paragraphs: list[Paragraph] = []
    cursor = 0
    for index, text in enumerate(paragraphs_text):
        start = cursor
        end = start + len(text)
        paragraphs.append(Paragraph(index=index, text=text, start=start, end=end))
        cursor = end + 2
    return plaintext, paragraphs


class CXoneSandboxContentAdapter:
    """GET-only content reader hard-pinned to the owner's CXone dev sandbox."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if settings.cxone_host.casefold() != CXONE_HOST:
            raise ContentConfigurationError(
                "The CXone host is not the pinned dev host."
            )
        if settings.sandbox_root.casefold() != SANDBOX_ROOT.casefold():
            raise ContentConfigurationError(
                "The CXone sandbox root is not the pinned sandbox."
            )

        self._settings = settings
        self._server_key, self._server_secret, self._server_user = _load_credentials(
            settings
        )
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
        )
        self._closed = False

    async def __aenter__(self) -> CXoneSandboxContentAdapter:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    def _token(self) -> str:
        epoch = int(self._clock())
        message = f"{self._server_key}{epoch}={self._server_user}".encode()
        digest = hmac.new(
            self._server_secret.encode(), message, hashlib.sha256
        ).hexdigest()
        return f"{self._server_key}_{epoch}_={self._server_user}_{digest}"

    async def _get_json(
        self,
        path: str,
        *,
        suffix: str = "",
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise ContentTransportError("The CXone content adapter is closed.")

        safe_path = _validate_sandbox_path(path)
        # suffix is never caller-controlled; only the three fixed methods below use it.
        url = f"{CXONE_API_BASE}/pages/{_page_id(safe_path)}{suffix}"
        query = {"dream.out.format": "json"}
        if params:
            query.update(params)

        try:
            response = await self._client.get(
                url,
                params=query,
                headers={
                    "Accept": "application/json",
                    "X-Deki-Token": self._token(),
                },
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise ContentTransportError("The CXone content request failed.") from exc

        if response.status_code in {401, 403}:
            raise ContentAuthenticationError(
                "CXone rejected the configured content credentials."
            )
        if response.status_code == 404:
            raise ContentNotFoundError("The requested sandbox page was not found.")
        if response.status_code != 200:
            raise ContentTransportError("CXone returned an unexpected response status.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ContentResponseError("CXone returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise ContentResponseError("CXone returned an unexpected JSON document.")
        return payload

    async def _page_info(self, path: str) -> dict[str, Any]:
        return await self._get_json(path)

    async def _contents(self, path: str) -> str:
        payload = await self._get_json(
            path, suffix="/contents", params={"mode": "view"}
        )
        body = payload.get("body")
        if isinstance(body, str):
            return body
        if isinstance(body, list):
            return "".join(part for part in body if isinstance(part, str))
        if body is None:
            return ""
        raise ContentResponseError("CXone returned an unexpected page body.")

    async def _subpages(self, path: str) -> list[_Subpage]:
        payload = await self._get_json(path, suffix="/subpages")
        raw_subpages = payload.get("page.subpage")
        if raw_subpages is None:
            raw_subpages = []
        elif isinstance(raw_subpages, dict):
            raw_subpages = [raw_subpages]
        if not isinstance(raw_subpages, list):
            raise ContentResponseError("CXone returned malformed subpage data.")

        children: list[_Subpage] = []
        for raw_child in raw_subpages:
            if not isinstance(raw_child, dict):
                raise ContentResponseError("CXone returned malformed subpage data.")
            child_path = _string_value(raw_child.get("path"))
            if not child_path:
                raise ContentResponseError("A CXone subpage did not include a path.")
            try:
                child_path = _validate_sandbox_path(child_path)
            except UnsafeSandboxPathError as exc:
                raise ContentResponseError(
                    "CXone returned a subpage outside the configured sandbox."
                ) from exc
            children.append(
                _Subpage(
                    title=_string_value(raw_child.get("title"))
                    or child_path.rsplit("/", 1)[-1],
                    path=child_path,
                    page_id=_page_identifier(raw_child.get("@id")),
                    has_children=_child_flag(raw_child.get("@subpages")),
                )
            )
        return children

    async def fetch_page(self, sandbox_path: str) -> NormalizedPage:
        path = _validate_sandbox_path(sandbox_path)
        info = await self._page_info(path)
        html_body = await self._contents(path)
        if not html_body.strip():
            raise ContentResponseError("The CXone page body was empty.")

        plaintext, paragraphs = _normalize_html(html_body)
        if len(plaintext) > self._settings.max_source_chars:
            raise ContentLimitError(
                "The CXone page exceeds the configured source limit."
            )

        title = (
            _string_value(info.get("title"))
            or _string_value(info.get("@title"))
            or path.rsplit("/", 1)[-1]
        )
        canonical_url = f"https://{CXONE_HOST}/{quote(path, safe='/')}"
        return NormalizedPage(
            title=title,
            plaintext=plaintext,
            htmlBody=html_body,
            paragraphs=paragraphs,
            source=SourceInfo(
                canonical_url=canonical_url,
                path=path,
                page_id=_page_identifier(info.get("@id")),
            ),
        )

    async def fetch_toc(
        self,
        sandbox_path: str,
        *,
        max_depth: int = DEFAULT_TOC_MAX_DEPTH,
        max_pages: int = DEFAULT_TOC_MAX_PAGES,
    ) -> TocNode:
        if not isinstance(max_depth, int) or isinstance(max_depth, bool):
            raise ContentLimitError("The TOC depth limit is invalid.")
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise ContentLimitError("The TOC page limit is invalid.")
        if not 0 <= max_depth <= ABSOLUTE_TOC_MAX_DEPTH:
            raise ContentLimitError("The TOC depth limit is outside the allowed range.")
        if not 1 <= max_pages <= ABSOLUTE_TOC_MAX_PAGES:
            raise ContentLimitError("The TOC page limit is outside the allowed range.")

        root_path = _validate_sandbox_path(sandbox_path)
        root_info = await self._page_info(root_path)
        root_title = (
            _string_value(root_info.get("title"))
            or _string_value(root_info.get("@title"))
            or root_path.rsplit("/", 1)[-1]
        )
        page_count = 1

        async def build(
            path: str,
            title: str,
            page_id: str | None,
            depth: int,
            ancestors: frozenset[str],
            has_children: bool | None = None,
        ) -> TocNode:
            nonlocal page_count
            node = TocNode(title=title, path=path, page_id=page_id)
            if depth >= max_depth or has_children is False:
                return node

            children = await self._subpages(path)
            built_children: list[TocNode] = []
            for child in children:
                if child.path in ancestors:
                    raise ContentResponseError(
                        "CXone returned a cyclic table of contents."
                    )
                page_count += 1
                if page_count > max_pages:
                    raise ContentLimitError("The CXone table of contents is too large.")
                built_children.append(
                    await build(
                        child.path,
                        child.title,
                        child.page_id,
                        depth + 1,
                        ancestors | {child.path},
                        child.has_children,
                    )
                )
            node.children = built_children
            return node

        return await build(
            root_path,
            root_title,
            _page_identifier(root_info.get("@id")),
            0,
            frozenset({root_path}),
        )


class PublicLibreTextsContentAdapter:
    """Credential-free reader pinned to the official LibreTexts read proxy."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.public_sources_enabled:
            raise ContentConfigurationError(
                "Public LibreTexts page sources are not enabled yet."
            )
        self._settings = settings
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": PUBLIC_API_ORIGIN,
                "User-Agent": PUBLIC_API_USER_AGENT,
            },
        )
        self._closed = False

    async def __aenter__(self) -> PublicLibreTextsContentAdapter:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def _put_json(
        self, endpoint: str, source: PublicSourceLocation, *, mode: str | None = None
    ) -> dict[str, Any]:
        if self._closed:
            raise ContentTransportError("The public content adapter is closed.")

        payload: dict[str, str] = {
            "subdomain": source.library,
            "path": source.path,
            "dreamformat": "json",
        }
        if mode is not None:
            payload["mode"] = mode
        try:
            response = await self._client.put(
                f"{PUBLIC_API_BASE}/{endpoint}",
                json=payload,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise ContentTransportError(
                "The LibreTexts public content proxy could not be reached."
            ) from exc

        if response.status_code in {401, 403}:
            raise ContentAuthenticationError(
                "The LibreTexts public content proxy refused this page request."
            )
        if response.status_code == 404:
            raise ContentNotFoundError("The requested public page was not found.")
        if response.status_code == 429:
            raise ContentTransportError(
                "The LibreTexts public content proxy is temporarily rate limited."
            )
        if response.status_code != 200:
            raise ContentTransportError(
                "The LibreTexts public content proxy is temporarily unavailable."
            )

        try:
            document = response.json()
        except ValueError as exc:
            raise ContentResponseError(
                "The LibreTexts public content proxy returned malformed JSON."
            ) from exc
        if not isinstance(document, dict):
            raise ContentResponseError(
                "The LibreTexts public content proxy returned unexpected data."
            )
        return document

    async def fetch_page(self, source_url: str) -> NormalizedPage:
        try:
            source = parse_public_source_url(source_url)
        except PublicSourceValidationError as exc:
            raise UnsafePublicSourceError(str(exc)) from exc

        info = await self._put_json("info", source)
        contents = await self._put_json("contents", source, mode="view")
        try:
            tags = await self._put_json("tags", source)
        except ContentAdapterError:
            # License metadata enhances the publication boundary but must not make
            # otherwise readable source content impossible to generate from.
            tags = {}

        body = contents.get("body")
        if isinstance(body, str):
            html_body = body
        elif isinstance(body, list):
            html_body = "".join(part for part in body if isinstance(part, str))
        elif body is None:
            html_body = ""
        else:
            raise ContentResponseError(
                "The LibreTexts public content proxy returned an unexpected page body."
            )
        if not html_body.strip():
            raise ContentResponseError("The public LibreTexts page body was empty.")

        plaintext, paragraphs = _normalize_html(html_body)
        if len(plaintext) > self._settings.max_source_chars:
            raise ContentLimitError(
                "The public LibreTexts page exceeds the configured source limit."
            )

        canonical_url = source.canonical_url
        uri = info.get("uri")
        uri_ui = uri.get("ui") if isinstance(uri, Mapping) else info.get("uri.ui")
        if isinstance(uri_ui, str) and uri_ui.strip():
            try:
                proxy_location = parse_public_source_url(uri_ui)
            except PublicSourceValidationError as exc:
                raise ContentResponseError(
                    "The public proxy returned an invalid canonical page URL."
                ) from exc
            if proxy_location.identity != source.identity:
                raise ContentResponseError(
                    "The public proxy returned a mismatched canonical page URL."
                )
            canonical_url = proxy_location.canonical_url

        title = (
            _string_value(info.get("title"))
            or _string_value(info.get("@title"))
            or _string_value(contents.get("title"))
            or _string_value(contents.get("@title"))
            or source.path.rsplit("/", 1)[-1]
        )
        return NormalizedPage(
            title=title,
            plaintext=plaintext,
            htmlBody=html_body,
            paragraphs=paragraphs,
            source=SourceInfo(
                backend="libretexts_public",
                canonical_url=canonical_url,
                path=source.identity,
                page_id=_page_identifier(info.get("@id")),
                license=_source_license_from_tags(
                    tags,
                    evidence_url=canonical_url,
                ),
            ),
        )

    async def fetch_license(self, source_url: str) -> SourceLicenseMetadata | None:
        """Read only page tags so existing source snapshots can be backfilled."""

        try:
            source = parse_public_source_url(source_url)
        except PublicSourceValidationError as exc:
            raise UnsafePublicSourceError(str(exc)) from exc
        tags = await self._put_json("tags", source)
        return _source_license_from_tags(
            tags,
            evidence_url=source.canonical_url,
        )


def build_content_adapter(
    settings: Settings, source_type: SourceType | str
) -> CXoneSandboxContentAdapter | PublicLibreTextsContentAdapter:
    try:
        selected = SourceType(source_type)
    except ValueError as exc:
        raise ContentConfigurationError("Choose a supported source type.") from exc
    if selected is SourceType.PUBLIC:
        return PublicLibreTextsContentAdapter(settings)
    if not settings.sandbox_sources_enabled:
        raise ContentConfigurationError(
            "Dev sandbox sources are disabled for this service."
        )
    return CXoneSandboxContentAdapter(settings)


__all__ = [
    "ContentAdapterError",
    "ContentAuthenticationError",
    "ContentConfigurationError",
    "ContentLimitError",
    "ContentNotFoundError",
    "ContentResponseError",
    "ContentTransportError",
    "CXoneSandboxContentAdapter",
    "PublicLibreTextsContentAdapter",
    "UnsafeSandboxPathError",
    "UnsafePublicSourceError",
    "build_content_adapter",
]
