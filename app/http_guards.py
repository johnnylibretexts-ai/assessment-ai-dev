"""Request guards shared by the review UI and the demo assistant.

These moved out of ``app.main`` so a second router can enforce the same origin
and identity posture without importing ``main`` -- which would be circular,
since ``main`` mounts that router.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException, Request

from .config import Settings


def reviewer_identity(request: Request) -> str:
    """Return the proxy-asserted reviewer, or fail closed.

    The value is set by the authenticating proxy in front of the app
    (``X-Auth-Request-User`` copied to ``X-Reviewer``). The app never derives it
    from anything the browser controls.
    """

    value = request.headers.get("x-reviewer", "").strip()
    if not value:
        raise HTTPException(
            status_code=403, detail="Trusted reviewer identity required"
        )
    return value[:255]


def require_same_origin(request: Request, settings: Settings) -> None:
    expected = settings.allowed_origin.rstrip("/")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != expected:
        raise HTTPException(
            status_code=403, detail="Cross-origin form submission refused"
        )
    referer = request.headers.get("referer")
    if not origin and not referer:
        raise HTTPException(status_code=403, detail="Form origin required")
    if not origin and referer:
        parsed = urlparse(referer)
        supplied = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if supplied != expected:
            raise HTTPException(
                status_code=403, detail="Cross-origin form submission refused"
            )
