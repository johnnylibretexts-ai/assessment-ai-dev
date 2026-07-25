"""Rebuild what the tester is looking at, from the record rather than the pixels.

The panel sends only a route. The server reads the authoritative row behind that
route and summarizes it. This is why the assistant can answer "why is this
blocked?" -- the blocking reason lives in critique issues, hint grounding, and
engine validation, none of which is necessarily rendered on the page.

Deliberately not ``main._draft_detail``: importing it would be circular (main
mounts the assistant router), and it returns the entire item JSON, which is
bulk without being more informative for this purpose.
"""

from __future__ import annotations

import re
from typing import Any

from ..db import Draft, DraftNotFoundError, DraftRepository, inspect_hint_grounding

DRAFT_ROUTE = re.compile(r"^/drafts/(\d+)/?$")
JOB_ROUTE = re.compile(r"^/jobs/([A-Za-z0-9_-]{1,64})/?$")
MAX_STEM_CHARS = 600
MAX_ISSUES = 6


def _clean(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def page_context(
    route: str,
    repository: DraftRepository,
) -> str:
    """Return a compact description of the current page, or "" for none.

    Unknown routes are not an error -- the tester may be anywhere, and a missing
    context block simply means the assistant answers without it.
    """

    normalized = (route or "").strip()
    if not normalized.startswith("/"):
        return ""
    # Query strings carry nothing the summary needs and would defeat matching.
    normalized = normalized.split("?", 1)[0].split("#", 1)[0]

    if normalized in {"/", ""}:
        return _queue_context(repository)

    draft_match = DRAFT_ROUTE.match(normalized)
    if draft_match:
        return _draft_context(int(draft_match.group(1)), repository)

    job_match = JOB_ROUTE.match(normalized)
    if job_match:
        return _job_context(job_match.group(1), repository)

    return ""


def _queue_context(repository: DraftRepository) -> str:
    try:
        drafts = repository.list_drafts()
    except Exception:  # a context block must never break the answer
        return ""
    if not drafts:
        return "The reviewer is on the home page. The draft queue is currently empty."
    counts: dict[str, int] = {}
    for draft in drafts:
        counts[draft.status.value] = counts.get(draft.status.value, 0) + 1
    breakdown = ", ".join(
        f"{count} {status}" for status, count in sorted(counts.items())
    )
    return (
        "The reviewer is on the home page, which lists the draft queue and the "
        f"generation form. Queue: {len(drafts)} drafts ({breakdown})."
    )


def _draft_context(draft_id: int, repository: DraftRepository) -> str:
    try:
        draft = repository.require_draft(draft_id)
    except DraftNotFoundError:
        return ""
    except Exception:  # a context block must never break the answer
        return ""
    return _describe_draft(draft)


def _describe_draft(draft: Draft) -> str:
    current = draft.current
    lines: list[str] = [
        f"The reviewer is looking at draft {draft.id}.",
        f"- Status: {draft.status.value}",
        f"- Item type: {current.item_type.value}",
        f"- Concept: {_clean(current.concept_label)}",
        f"- Stem: {_clean(current.stem, MAX_STEM_CHARS)}",
        f"- Bloom level: {current.bloom.value} "
        f"(confirmed: {'yes' if draft.bloom_confirmed else 'no'})",
        f"- Difficulty: {current.difficulty.value} "
        f"(confirmed: {'yes' if draft.difficulty_confirmed else 'no'})",
        f"- Revision: v{max(int(draft.edit_count or 0), 0) + 1}",
        f"- Source page: {_clean(draft.source.title)} ({_clean(draft.source.canonical_url)})",
    ]

    issues = [
        _clean(issue)
        for issue in (draft.critique_json or {}).get("issues", [])[:MAX_ISSUES]
    ]
    if issues:
        lines.append("- Critique issues raised during revision:")
        lines.extend(f"  * {issue}" for issue in issues)

    hint_record = draft.current_hint_ladder
    if hint_record is not None:
        ladder = hint_record.ladder
        lines.append(f"- Hint ladder present, review status: {hint_record.status}")
        grounding = inspect_hint_grounding(current, ladder)
        for problem in list(grounding)[:MAX_ISSUES]:
            scope = problem.rung or "ladder"
            lines.append(f"  * grounding problem ({scope}): {_clean(problem.message)}")

    validation = draft.current_engine_validation
    if validation is not None:
        lines.append(f"- Engine validation: {getattr(validation, 'status', 'unknown')}")

    publications = list(draft.publications)
    if publications:
        latest = max(publications, key=lambda item: item.id)
        lines.append(f"- Latest publication attempt state: {latest.state}")
    else:
        lines.append("- Never published.")

    return "\n".join(lines)


def _job_context(job_id: str, repository: DraftRepository) -> str:
    try:
        job = repository.get_generation_job(job_id)
    except Exception:
        return ""
    if job is None:
        return ""
    lines = [
        "The reviewer is watching a generation job.",
        f"- Status: {job.status.value if hasattr(job.status, 'value') else job.status}",
        f"- Stage: {_clean(job.stage)}",
        f"- Progress: {job.progress}%",
    ]
    if getattr(job, "error_message", ""):
        lines.append(f"- Error: {_clean(job.error_message)}")
    return "\n".join(lines)
