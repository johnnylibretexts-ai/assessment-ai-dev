from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote

from .db import DraftRepository, inspect_hint_grounding


def audit_current_hints(repository: DraftRepository) -> list[dict[str, object]]:
    """Inspect only the newest hint version bound to each current draft edit."""

    findings: list[dict[str, object]] = []
    for draft in repository.list_drafts(current_sources_only=False):
        record = draft.current_hint_ladder
        if record is None:
            continue
        for issue in inspect_hint_grounding(draft.current, record.ladder):
            findings.append(
                {
                    "draft_id": draft.id,
                    "edit_count": draft.edit_count,
                    "hint_version": record.version,
                    "rung": issue.rung,
                    "allowed_citations": list(issue.allowed_paragraphs),
                    "invalid_citations": list(issue.invalid_paragraphs),
                    "message": issue.message,
                }
            )
    return sorted(
        findings,
        key=lambda item: (
            int(item["draft_id"]),
            str(item["rung"]),
        ),
    )


def read_only_sqlite_url(database_url: str) -> str:
    """Convert an existing SQLite URL to an immutable mode=ro connection URL."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(
            "The audit currently supports read-only SQLite databases only."
        )
    raw_path = unquote(database_url.removeprefix(prefix).split("?", 1)[0])
    if raw_path in {"", ":memory:"}:
        raise ValueError("The audit requires an existing on-disk SQLite database.")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise ValueError(f"Database not found: {path}")
    # Percent-encode before building the URI: a database filename containing
    # a reserved character such as "?" or "%" would otherwise be truncated or
    # misparsed, silently pointing at a different database.
    encoded = quote(path.as_posix(), safe="/")
    return f"sqlite:///file:{encoded}?mode=ro&uri=true"
