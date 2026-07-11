from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit


PUBLIC_LIBRETEXTS_LIBRARIES = frozenset(
    {
        "bio",
        "biz",
        "chem",
        "eng",
        "espanol",
        "geo",
        "human",
        "k12",
        "math",
        "med",
        "phys",
        "socialsci",
        "stats",
        "workforce",
    }
)
PUBLIC_LIBRETEXTS_HOSTS = frozenset(
    f"{library}.libretexts.org" for library in PUBLIC_LIBRETEXTS_LIBRARIES
)

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_MALFORMED_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


class PublicSourceValidationError(ValueError):
    """A public source URL or stored identity violates the fixed policy."""


@dataclass(frozen=True, slots=True)
class PublicSourceLocation:
    library: str
    host: str
    path: str
    identity: str
    canonical_url: str


def parse_public_source_url(raw_url: str) -> PublicSourceLocation:
    """Validate and canonicalize one public LibreTexts page URL."""

    if not isinstance(raw_url, str):
        raise PublicSourceValidationError("A public LibreTexts page URL is required.")
    value = raw_url.strip()
    if not value or _CONTROL_CHARACTER.search(value):
        raise PublicSourceValidationError("The public page URL is invalid.")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PublicSourceValidationError("The public page URL is invalid.") from exc

    if parsed.scheme != "https":
        raise PublicSourceValidationError("Public sources must use HTTPS.")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise PublicSourceValidationError(
            "Public page URLs cannot include credentials or a custom port."
        )

    host = (parsed.hostname or "").casefold()
    if host not in PUBLIC_LIBRETEXTS_HOSTS:
        raise PublicSourceValidationError(
            "Choose a page from an approved public LibreTexts library."
        )
    if _MALFORMED_ESCAPE.search(value):
        raise PublicSourceValidationError(
            "The public page path has malformed escaping."
        )

    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PublicSourceValidationError(
            "The public page path has malformed escaping."
        ) from exc
    path = decoded_path.strip("/")
    if not path or "%" in path or "\\" in path or _CONTROL_CHARACTER.search(path):
        raise PublicSourceValidationError("The public page path is invalid.")
    segments = path.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise PublicSourceValidationError("The public page path is invalid.")

    library = host.removesuffix(".libretexts.org")
    identity = f"{host}/{path}"
    encoded_path = quote(path, safe="/")
    return PublicSourceLocation(
        library=library,
        host=host,
        path=path,
        identity=identity,
        canonical_url=f"https://{host}/{encoded_path}",
    )


def canonicalize_public_identity(raw_identity: str) -> str:
    """Validate a stored host-qualified identity without accepting a URL."""

    if not isinstance(raw_identity, str):
        raise PublicSourceValidationError("source path must identify a page")
    identity = raw_identity.strip().strip("/")
    if not identity or _CONTROL_CHARACTER.search(identity) or "\\" in identity:
        raise PublicSourceValidationError("source path must identify a page")
    host, separator, path = identity.partition("/")
    if not separator or host.casefold() not in PUBLIC_LIBRETEXTS_HOSTS:
        raise PublicSourceValidationError(
            "source path must identify an approved LibreTexts page"
        )
    if "%" in path:
        raise PublicSourceValidationError("public source identity must be decoded")
    segments = path.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise PublicSourceValidationError("source path must not contain traversal")
    return f"{host.casefold()}/{path}"


__all__ = [
    "PUBLIC_LIBRETEXTS_HOSTS",
    "PUBLIC_LIBRETEXTS_LIBRARIES",
    "PublicSourceLocation",
    "PublicSourceValidationError",
    "canonicalize_public_identity",
    "parse_public_source_url",
]
