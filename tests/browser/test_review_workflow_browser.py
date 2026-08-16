"""Browser-level regressions for the review and publishing workflow.

These cover the failures that prompted the 2026-07-24 fix and that server-side
tests structurally cannot catch, because they depend on rendered markup and on
client-side behaviour in review-workflow.js:

* a failed form must not discard the reviewer's work;
* approval must survive a reload;
* unsaved hint edits must disable approval;
* the current revision and an earlier published revision must be labelled
  distinctly and must never both claim to be published;
* a published revision must not offer a second publish action.

Everything runs against a disposable SQLite database and a FakeAdapt, so no
outbound ADAPT request is ever made. The reviewer identity normally injected by
oauth2-proxy is supplied as an extra header on the browser context.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterator

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright is required for browser regressions",
)
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect

from tests.browser.harness import (  # noqa: E402
    ALLOWED_PARAGRAPH,
    REVIEWER,
    UNGROUNDED_PARAGRAPH,
    disposable_instance,
    force_publication_state,
    mark_published,
    seed_draft,
)

RUNGS = ("conceptual", "strategic", "specific")


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[dict]:
    with disposable_instance(tmp_path) as instance:
        yield instance


@pytest.fixture
def page(browser, workspace):  # type: ignore[no-untyped-def]
    context = browser.new_context(
        base_url=workspace["base_url"],
        # oauth2-proxy supplies this in production; without it every request
        # is refused with 403 "Trusted reviewer identity required".
        extra_http_headers={"X-Reviewer": REVIEWER},
    )
    handle = context.new_page()
    try:
        yield handle
    finally:
        context.close()


def approve_hints(page) -> None:  # type: ignore[no-untyped-def]
    for rung in RUNGS:
        page.check(f'input[name="{rung}_confirmed"]')
    page.click("[data-hint-approve]")


def test_failed_hint_edit_preserves_every_field(page, workspace) -> None:
    """A rejected save must return the reviewer's text, not the stored text."""

    draft_id = seed_draft(workspace["repository"])
    page.goto(f"/drafts/{draft_id}")

    edited = "My carefully reworded conceptual hint that must not be lost."
    note = "Reworded for clarity before the citation slipped."
    page.fill('textarea[name="conceptual_text"]', edited)
    page.fill('form[data-hint-edit-form] textarea[name="reviewer_notes"]', note)
    # Grounded-looking but outside the item source, so the save is rejected.
    page.fill('input[name="specific_citations"]', str(UNGROUNDED_PARAGRAPH))
    page.click('form[data-hint-edit-form] button[type="submit"]')

    # Still on the draft, with the submitted values intact.
    assert f"/drafts/{draft_id}" in page.url
    expect(page.locator('textarea[name="conceptual_text"]')).to_have_value(edited)
    expect(
        page.locator('form[data-hint-edit-form] textarea[name="reviewer_notes"]')
    ).to_have_value(note)
    expect(page.locator('input[name="specific_citations"]')).to_have_value(
        str(UNGROUNDED_PARAGRAPH)
    )

    # The offending field is marked, and the error names the allowed paragraphs.
    expect(page.locator('input[name="specific_citations"]')).to_have_attribute(
        "aria-invalid", "true"
    )
    error = page.locator("#hint-specific-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text(str(ALLOWED_PARAGRAPH))

    # And nothing was persisted. This must be a fresh GET, not reload(): the
    # rejected save renders at the POST URL, so reloading re-submits the form
    # and shows the same rejected values back -- which looks like persistence
    # but proves nothing.
    page.goto(f"/drafts/{draft_id}")
    expect(page.locator('textarea[name="conceptual_text"]')).not_to_have_value(edited)
    expect(page.locator('input[name="specific_citations"]')).to_have_value(
        str(ALLOWED_PARAGRAPH)
    )


def test_hint_approval_persists_across_reload(page, workspace) -> None:
    draft_id = seed_draft(workspace["repository"])
    page.goto(f"/drafts/{draft_id}")

    approve_hints(page)
    summary = page.locator(".approval-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("approved")

    # The regression that started this work: approval appeared to reset.
    page.reload()
    expect(page.locator(".approval-summary")).to_be_visible()
    expect(page.locator(".approval-summary")).to_contain_text(REVIEWER)
    # The approval form is replaced by the summary, not shown alongside it.
    expect(page.locator("[data-hint-approve]")).to_have_count(0)


def test_unsaved_hint_edits_disable_approval(page, workspace) -> None:
    """review-workflow.js must block approving text the server has not seen."""

    draft_id = seed_draft(workspace["repository"])
    page.goto(f"/drafts/{draft_id}")

    approve_button = page.locator("[data-hint-approve]")
    for rung in RUNGS:
        page.check(f'input[name="{rung}_confirmed"]')
    expect(approve_button).to_be_enabled()

    page.fill('textarea[name="strategic_text"]', "Unsaved wording change.")
    page.locator('textarea[name="strategic_text"]').dispatch_event("input")
    expect(approve_button).to_be_disabled()
    expect(page.locator("[data-hint-dirty-message]")).to_be_visible()

    # The dirty flag latches: review-workflow.js sets it on any input event and
    # never compares against the saved values, so typing the original text back
    # does NOT re-enable approval. That diverges from manual test D in the
    # 2026-07-24 handoff ("returning the saved text restores the clean state"),
    # but it errs safe -- it over-blocks approval rather than under-blocking it
    # -- so this pins the implemented behaviour, not the planned behaviour. If
    # the plan wins later, this expectation is the thing to change.
    page.fill(
        'textarea[name="strategic_text"]',
        "The proton count fixes identity; neutrons may vary.",
    )
    page.locator('textarea[name="strategic_text"]').dispatch_event("input")
    expect(approve_button).to_be_disabled()

    # A reload discards the unsaved edit and restores an approvable state.
    page.reload()
    for rung in RUNGS:
        page.check(f'input[name="{rung}_confirmed"]')
    expect(page.locator("[data-hint-approve]")).to_be_enabled()


def test_published_revision_offers_no_duplicate_publish(page, workspace) -> None:
    draft_id = seed_draft(workspace["repository"])
    mark_published(workspace["repository"], draft_id, adapt_question_id=4242)

    page.goto(f"/drafts/{draft_id}")
    body = page.locator("body")
    expect(body).to_contain_text("Published")
    expect(body).to_contain_text("4242")
    # No second publish action for a revision that is already published.
    expect(page.locator('form[action$="/publish"]')).to_have_count(0)
    # And no outbound call happened as a side effect of merely viewing it.
    assert workspace["fake"].create_calls == 0


def _paint_of_publication_state(page, workspace, state: str) -> dict:  # type: ignore[no-untyped-def]
    """What the browser actually paints a publication card in this state as."""

    repository = workspace["repository"]
    draft_id = seed_draft(repository, variant=state)
    mark_published(repository, draft_id)
    force_publication_state(workspace["app"], draft_id, state)

    page.goto(f"/drafts/{draft_id}")
    return page.locator("article.publication-record").first.evaluate(
        """node => {
            const style = getComputedStyle(node);
            return {
                background: style.backgroundColor,
                borderColour: style.borderTopColor,
                borderStyle: style.borderTopStyle,
            };
        }"""
    )


def test_an_unreadable_publication_state_is_painted_apart_from_every_other(
    page, workspace
) -> None:
    """The cascade, which is the half a rendered-class assertion cannot see.

    Two things could still leave the card ADR 0003 sends an operator to find
    looking like one of the ordinary ones. The stylesheet could lack a rule for
    the tone, and the neutral default would make that look deliberate -- the
    original `hints_synced` bug. Or the raw state could inject a second tone
    class: `state-{{ publication.state }}` escapes quotes but not whitespace, so
    a successor build writing `x tone-succeeded` puts a green tone class on that
    same card. Only an unreadable state can do that, since every state this build
    declares is a single token.

    Asserted on the colours rather than on the whole computed paint, because
    `border-style` is declared by the anomalous rule alone: a card painted
    entirely as "succeeded" keeps a dashed border, so a border-style assertion
    passes while the operator sees a green card.
    """

    ordinary = {
        state: _paint_of_publication_state(page, workspace, state)
        for state in ("pending", "succeeded", "adapt_created", "failed")
    }
    unreadable = _paint_of_publication_state(
        page, workspace, "quarantined tone-succeeded"
    )
    expect(page.locator("body")).to_contain_text("Unreadable publication state")

    def colours(paint: dict) -> dict:
        return {key: paint[key] for key in ("background", "borderColour")}

    for state, paint in ordinary.items():
        assert colours(paint) != colours(unreadable), state
    # Dashed as well as differently coloured, so the card is still the odd one
    # out for a reviewer who cannot rely on hue.
    assert unreadable["borderStyle"] == "dashed"
    assert ordinary["pending"]["borderStyle"] == "solid"


def test_earlier_publication_does_not_claim_the_current_revision(
    page, workspace
) -> None:
    """The exact contradiction reviewers reported: two states at once."""

    repository = workspace["repository"]
    draft_id = seed_draft(repository)
    mark_published(repository, draft_id, adapt_question_id=4242)

    # Edit, creating a newer unpublished revision.
    draft = repository.require_draft(draft_id)
    revised = dict(draft.current_json)
    revised["stem"] = "A revised stem that has not been published yet."
    repository.edit_draft(draft_id, revised, editor=REVIEWER, notes="revised")

    page.goto(f"/drafts/{draft_id}")
    body = page.locator("body")
    # The earlier revision stays identified as published, with its ADAPT id.
    expect(body).to_contain_text("4242")
    # The current revision must be labelled not published -- and the two
    # statements must be about different revisions, never the same one.
    expect(body).to_contain_text(re.compile("not yet published", re.IGNORECASE))
    assert workspace["fake"].create_calls == 0

    queue = page.goto("/")
    assert queue is not None
    expect(page.locator("body")).to_contain_text(
        re.compile("not yet published", re.IGNORECASE)
    )
