"""Browser-level regressions for the generation-options fieldset.

Auto mix discards ``item_types`` server-side (``jobs.py::_validated_item_types``
passes them through only when ``generation_mode == "selected"``), so leaving the
checkboxes clickable under Auto mix invites a request the app will ignore. The
lock that fixes this lives entirely in ``generation.js`` and ``styles.css``, and
its whole observable behaviour is computed state -- ``checkbox.disabled`` and an
applied opacity.

That is why these are browser regressions rather than assertions about the
served markup or the served script text. Asserting that a source file contains
``checkbox.disabled = locked`` proves the characters are on disk; it does not
prove a single checkbox is disabled in front of a user. The same gap let three
separate defects ship in the demo assistant, each passing every markup test.

The dimming is asserted through *computed opacity*, not through the presence of
the ``is-locked`` class, so that a class the stylesheet does not define fails
here. That exact failure -- JavaScript toggling a class no rule honours -- is
what left the assistant panel impossible to close.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright is required for browser regressions",
)
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect

from tests.browser.harness import (  # noqa: E402
    REVIEWER,
    disposable_instance,
)

CHECKBOXES = 'input[name="item_types"]'


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
    # The fieldset only renders when advanced items are enabled.
    with disposable_instance(tmp_path, advanced_items_enabled=True) as instance:
        yield instance


@pytest.fixture
def page(browser, workspace):  # type: ignore[no-untyped-def]
    context = browser.new_context(
        base_url=workspace["base_url"],
        extra_http_headers={"X-Reviewer": REVIEWER},
    )
    handle = context.new_page()
    try:
        yield handle
    finally:
        context.close()


def grid_opacity(page) -> float:  # type: ignore[no-untyped-def]
    return float(
        page.eval_on_selector(
            "#item-type-options",
            "el => getComputedStyle(el).opacity",
        )
    )


def test_auto_mix_locks_every_item_type_checkbox(page) -> None:
    """Auto mix ignores these boxes, so they must not accept input."""

    page.goto("/")

    # Auto is the default, and no interaction has happened yet: the very first
    # paint a tester sees must already be locked.
    expect(page.locator("#generation_mode")).to_have_value("auto")

    boxes = page.locator(CHECKBOXES)
    count = boxes.count()
    assert count > 0, "no item-type checkboxes rendered"
    for index in range(count):
        expect(boxes.nth(index)).to_be_disabled()

    # Visibly inactive, not merely inert. This fails if the is-locked rule is
    # missing from the stylesheet even though the class is applied.
    assert grid_opacity(page) < 1.0

    # And the help text explains why, rather than leaving the user to guess.
    expect(page.locator("#item-type-help")).to_contain_text("Auto mix chooses")


def test_choosing_types_unlocks_the_checkboxes(page) -> None:
    page.goto("/")
    page.select_option("#generation_mode", "selected")

    boxes = page.locator(CHECKBOXES)
    for index in range(boxes.count()):
        expect(boxes.nth(index)).to_be_enabled()

    assert grid_opacity(page) == 1.0

    # The box is genuinely operable, not just missing the disabled attribute.
    first = boxes.first
    first.check()
    expect(first).to_be_checked()


def test_switching_back_to_auto_locks_without_discarding_the_selection(
    page,
) -> None:
    """A mode toggle must not silently destroy the reviewer's picks."""

    page.goto("/")
    page.select_option("#generation_mode", "selected")

    boxes = page.locator(CHECKBOXES)
    boxes.nth(0).check()
    boxes.nth(1).check()

    page.select_option("#generation_mode", "auto")
    expect(boxes.nth(0)).to_be_disabled()
    # Still checked -- disabled, not cleared.
    expect(boxes.nth(0)).to_be_checked()
    expect(boxes.nth(1)).to_be_checked()

    # Coming back restores a usable, unchanged selection.
    page.select_option("#generation_mode", "selected")
    expect(boxes.nth(0)).to_be_enabled()
    expect(boxes.nth(0)).to_be_checked()
    expect(boxes.nth(1)).to_be_checked()
    assert grid_opacity(page) == 1.0


def test_locked_checkboxes_are_not_submitted_under_auto_mix(page) -> None:
    """The lock must match what the server does, not merely look like it.

    Disabled controls are omitted from the submission, so a locked box cannot
    contribute an ``item_types`` value -- which is precisely the behaviour the
    server already implements by discarding them.
    """

    page.goto("/")
    page.select_option("#generation_mode", "selected")
    page.locator(CHECKBOXES).first.check()
    page.select_option("#generation_mode", "auto")

    submitted = page.evaluate(
        """() => {
            const form = document.querySelector('.generate-form');
            return new FormData(form).getAll('item_types');
        }"""
    )
    assert submitted == []
