"""Demo assistant: a support chatbot for people trying Assessment AI out.

Entirely separate from generation, review, and publishing. It holds no tools,
touches no draft, and reaches no external system beyond its own LLM provider.
Off by default; deleting this package and the ``assistant_enabled`` setting
removes the feature completely.
"""

from __future__ import annotations

from ..config import Settings
from ..llm import provider_is_ready
from .routes import build_assistant_router
from .service import AssistantService
from .store import AssistantStore, create_schema

__all__ = [
    "AssistantService",
    "AssistantStore",
    "assistant_status",
    "build_assistant_router",
    "create_schema",
]


def assistant_status(settings: Settings) -> str:
    """Report assistant readiness the way ``webwork_status`` reports engines.

    The assistant borrows generation's provider configuration, so a flag with no
    usable provider is ``misconfigured`` rather than ``enabled`` -- otherwise
    ``/healthz`` would claim a working chatbot that cannot answer anything.
    """

    if not settings.assistant_enabled:
        return "disabled"
    ready = any(provider_is_ready(settings, name) for name in settings.llm_providers)
    return "enabled" if ready else "misconfigured"
