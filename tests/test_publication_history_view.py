"""The publication history card's headline and tone, one state at a time.

The card used to decide its headline with four ``elif``s and an ``{% else %}``
written for ``pending``, so every state the chain did not name was announced as
"Publication pending". Two states landed there wrongly: ``hints_synced``, which
is ordinary and reachable, and a state outside ``PublicationState``, which ADR
0003 makes ``publish()`` refuse by name while the page hid it. These tests pin
the headline per state so a sixth state cannot quietly inherit a wrong one.

The colour had the same shape of bug one file over: three CSS rules naming
states literally and a neutral default catching everything else, so those same
two states read as ``pending`` in colour after the text stopped saying so. The
tone half of these tests pins the class the record carries, not the pixels --
the stylesheet cannot enumerate a state written by a successor build, so the
template has to hand it something matchable.
"""

from __future__ import annotations

import re
from typing import Any
from unittest import mock

from app.db import PublicationState
from app.main import (
    ANOMALOUS_PUBLICATION_TONE,
    BASE_DIR,
    PUBLICATION_STATE_HEADLINES,
    PUBLICATION_STATE_TONES,
    publication_state_headline,
    publication_state_tone,
    templates,
)

STYLESHEET = BASE_DIR / "static" / "styles.css"


def _draft(publications: list[dict[str, Any]]) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "id": 17,
        "status": "ready_to_publish",
        "status_label": "Approved — not yet published",
        "edit_count": 0,
        "source_title": "Synthetic publication source",
        "source_type": "public",
        "source_url": "https://chem.libretexts.org/example",
        "cited_paragraphs": [],
        "item_type": "multiple_choice",
        "item_type_label": "multiple choice",
        "context_type": "standard",
        "model_id": "test-model",
        "prompt_version": "test-prompt",
        "stimulus": None,
        "stem": "What is the computed value?",
        "choices": [{"id": "A", "text": "1.5", "correct": True}],
        "response": {},
        "explanation": "The exact result is three halves.",
        "critique_issues": [],
        "bloom": "apply",
        "difficulty": "intermediate",
        "bloom_confirmed": True,
        "difficulty_confirmed": True,
        "reviewer_notes": "",
        "specialist_review_required": False,
        "hint_ladder": None,
        "publications": publications,
    }
    draft["display"] = {
        "stem": draft["stem"],
        "stimulus": draft["stimulus"],
        "explanation": draft["explanation"],
        "choices": draft["choices"],
        "response": draft["response"],
    }
    return draft


def _publication(state: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 5,
        "edit_count": 0,
        "state": state,
        "adapt_question_id": None,
        "adapt_page_id": None,
        "framework_title": "Test framework",
        "topic": "Stoichiometry",
        "license_label": "CC BY 4.0",
        "finalized_at": None,
        "error_message": None,
        "is_current_edit": True,
    }
    record.update(overrides)
    return record


def _render(state: str, **overrides: Any) -> str:
    context: dict[str, Any] = {
        "draft": _draft([_publication(state, **overrides)]),
        "notice": None,
        "error": None,
        "active_form": None,
        "form_values": {},
        "form_errors": {},
        "bloom_options": ["apply"],
        "difficulty_options": ["intermediate"],
    }
    return templates.env.get_template("draft.html").render(**context)


def test_hints_synced_reads_as_synced_rungs_rather_than_pending() -> None:
    """`hints_synced` is reachable and ordinary, not a publication that has yet
    to start.

    `QTI_FINALIZE` declares `leaves_on_failure=RETAINS_CURRENT` so a
    finalization failure does not walk the publication backwards, which parks a
    publication whose rungs are already in ADAPT in `hints_synced`. Calling that
    "pending" tells the reviewer nothing has happened when ADAPT holds both the
    question and its hint rungs.
    """

    rendered = _render(PublicationState.HINTS_SYNCED.value, adapt_question_id=901)

    assert "ADAPT item created, hint rungs synced — QTI pending" in rendered
    assert "Publication pending" not in rendered
    assert "ADAPT question ID: <strong>901</strong>" in rendered


def test_unreadable_publication_state_is_named_and_shows_its_raw_value() -> None:
    """ADR 0003's refusal names the offending state; the page must not hide it.

    `publish()` refuses a state this build cannot read and sends the operator to
    look at the record. Rendering that record as "Publication pending" hid the
    one fact the refusal was pointing at, in the middle of the rollback window
    the ADR is about.
    """

    rendered = _render("quarantined_by_successor")

    # Asserted as one string because the raw value is already in the DOM as the
    # `state-*` class; a bare substring check would pass without the headline.
    assert "Unreadable publication state: quarantined_by_successor" in rendered
    assert "Publication pending" not in rendered


def test_pending_still_reads_as_publication_pending() -> None:
    """`pending` is the state the `else` was written for and keeps its wording."""

    rendered = _render(PublicationState.PENDING.value)

    assert "Publication pending" in rendered


def test_the_four_recognised_headlines_are_unchanged() -> None:
    assert "Published to ADAPT" in _render(PublicationState.SUCCEEDED.value)
    assert "ADAPT item created — QTI pending" in _render(
        PublicationState.ADAPT_CREATED.value
    )
    assert "Awaiting ADAPT reconciliation" in _render(PublicationState.UNKNOWN.value)
    assert "Publication failed" in _render(PublicationState.FAILED.value)


def test_every_publication_state_has_its_own_headline() -> None:
    """The structural half of the fix: a seventh state must not inherit one.

    The bug was an `{% else %}` acting as a default, so a mapping is only an
    improvement if an unlabelled state is caught here. It must not fall through
    to the unreadable headline either — that headline says "a successor build
    wrote this", which is a lie about a state this build recognises.
    """

    assert set(PUBLICATION_STATE_HEADLINES) == set(PublicationState)

    headlines = [publication_state_headline(state.value) for state in PublicationState]
    assert len(set(headlines)) == len(PublicationState)
    assert not any("Unreadable publication state" in headline for headline in headlines)


def test_a_recognised_state_without_a_headline_costs_one_card_not_the_page() -> None:
    """A missing entry must degrade one headline rather than 500 the page.

    A `KeyError` raised inside a template render takes out the whole of
    `GET /drafts/{id}`, so a later commit that adds a state and forgets its
    headline would make every draft that ever reached that state unviewable --
    worse than the one mislabelled card this change removes, and on precisely
    the page ADR 0003 sends an operator to. The test above is what forces the
    entry to exist; this is what the page does if that guard is ever bypassed.

    It must not borrow the unreadable wording. "Unreadable" means a successor
    build or a hand edit wrote the value, and saying that about a state this
    build declares would send the operator hunting for a rollback that never
    happened.
    """

    with mock.patch.dict(PUBLICATION_STATE_HEADLINES):
        del PUBLICATION_STATE_HEADLINES[PublicationState.FAILED]
        rendered = _render(PublicationState.FAILED.value)

    assert "Unrecognised publication state: failed" in rendered
    assert "Unreadable publication state" not in rendered


def test_an_unreadable_state_is_escaped_rather_than_rendered_as_markup() -> None:
    """The raw state reaches the DOM as text for the first time with this change.

    Before it appeared only inside the `state-*` class attribute. `state` is a
    plain `String(30)` written by a successor build or a hand edit, so it is
    exactly the kind of value that must not become markup. Autoescape does this
    today; the test exists so that a later `|safe` or `Markup` on the headline
    fails here instead of shipping.
    """

    rendered = _render("<img src=x onerror=alert(1)>")

    assert "<img src=x onerror=alert(1)>" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered


def _record_classes(state: str, **overrides: Any) -> set[str]:
    """The classes one publication record carries, as the browser sees them."""

    match = re.search(
        r'<article class="(publication-record[^"]*)"', _render(state, **overrides)
    )
    assert match is not None, "the publication history rendered no record"
    return set(match.group(1).split())


def _stylesheet() -> str:
    """The stylesheet's rules, with its comments removed.

    The assertions below are substring checks for selectors that must or must
    not be there, and the rules they are about are the ones worth a comment. A
    comment naming a selector that was deliberately removed would fail them with
    a message reading "the rule came back", and the only way to keep the tests
    passing would be to write the comment around its own subject.
    """

    return re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(encoding="utf-8"), flags=re.S)


def test_hints_synced_carries_the_same_in_flight_tone_as_adapt_created() -> None:
    """Two adjacent in-flight states of one publish run must not look different.

    `adapt_created` and `hints_synced` are consecutive: the same run passes
    through the first on its way to the second. The stylesheet named the first
    and not the second, so the further-along state was the one that looked like
    nothing had happened -- and looked identical to `pending`, which is the one
    state where nothing really has.
    """

    adapt_created = _record_classes(PublicationState.ADAPT_CREATED.value)
    hints_synced = _record_classes(PublicationState.HINTS_SYNCED.value)

    assert "tone-in-flight" in adapt_created
    assert "tone-in-flight" in hints_synced
    assert hints_synced - {"state-hints_synced"} == adapt_created - {
        "state-adapt_created"
    }
    assert "tone-pending" not in hints_synced


def test_an_unreadable_state_is_toned_without_the_stylesheet_naming_it() -> None:
    """The record ADR 0003's refusal sends an operator to must be findable.

    A selector cannot enumerate what it does not know: the state is a raw
    `String(30)` a successor build or a hand edit wrote, so `.state-<whatever>`
    is a rule nobody could have written in advance. The template derives the
    tone instead, from the same three-way answer the headline uses, and the
    stylesheet matches on that -- never on the raw value.
    """

    classes = _record_classes("quarantined_by_successor")

    assert f"tone-{ANOMALOUS_PUBLICATION_TONE}" in classes
    assert "tone-pending" not in classes
    # #15 deliberately left `state-*` carrying the raw value: it is what a
    # reviewer reads the real state off the DOM with. The tone is added beside
    # it, not instead of it.
    assert "state-quarantined_by_successor" in classes
    assert "quarantined_by_successor" not in _stylesheet()


def test_pending_keeps_the_neutral_default() -> None:
    """`pending` is the one state where nothing has happened yet, so it stays
    uncoloured -- it earns the neutral card at the top of the rules rather than
    a tone rule of its own."""

    assert "tone-pending" in _record_classes(PublicationState.PENDING.value)
    assert ".tone-pending" not in _stylesheet()


def test_every_publication_state_has_a_tone() -> None:
    """The structural half, as with the headlines: a seventh state is a missing
    key, not a card that silently reads as `pending`."""

    assert set(PUBLICATION_STATE_TONES) == set(PublicationState)
    assert ANOMALOUS_PUBLICATION_TONE not in PUBLICATION_STATE_TONES.values()

    # The four the issue leaves alone, pinned so a later tone edit has to be
    # deliberate about them.
    assert publication_state_tone(PublicationState.SUCCEEDED.value) == "succeeded"
    assert publication_state_tone(PublicationState.FAILED.value) == "failed"
    assert publication_state_tone(PublicationState.UNKNOWN.value) == "in-flight"
    assert publication_state_tone(PublicationState.ADAPT_CREATED.value) == "in-flight"


def test_every_tone_the_page_can_emit_is_styled_except_pending() -> None:
    """A tone with no rule is the bug this fixes, one level up.

    `hints_synced` was not mis-styled; it was styled by nothing, and the neutral
    default made that look intentional. Emitting a tone the stylesheet has never
    heard of would reproduce that exactly, so every tone but `pending` -- which
    wants the default -- must have a rule.
    """

    stylesheet = _stylesheet()
    emitted = set(PUBLICATION_STATE_TONES.values()) | {ANOMALOUS_PUBLICATION_TONE}

    for tone in emitted - {"pending"}:
        assert f".publication-record.tone-{tone}" in stylesheet

    # One source of truth for the colour. A `state-*` rule surviving beside the
    # tone rules is how the two drift apart.
    assert ".publication-record.state-" not in stylesheet


def test_the_headline_and_the_tone_agree_on_what_is_unreadable() -> None:
    """One card cannot be named one thing and coloured another.

    Both answers start from the same question -- is this a state this build can
    read -- and both ask ``_readable_publication_state``. Written out twice
    instead, one copy could later be taught to tolerate a variant value (a
    ``strip()``, a case fold, an alias for a renamed state) while the other
    stayed strict, and the same record would read "Published to ADAPT" while
    wearing the card that means nobody here can read it.
    """

    values: list[Any] = [state.value for state in PublicationState]
    # Near misses, because a tolerant copy is what would part company here.
    values += ["quarantined_by_successor", " succeeded", "SUCCEEDED", "", None, 7]

    for value in values:
        unreadable = publication_state_headline(value).startswith(
            "Unreadable publication state:"
        )
        anomalous = publication_state_tone(value) == ANOMALOUS_PUBLICATION_TONE
        assert unreadable == anomalous, value


def test_a_recognised_state_without_a_tone_stays_distinct_from_pending() -> None:
    """A missing entry must degrade to "look at this", not to "nothing yet".

    Same reasoning as the headline fallback: the exhaustiveness test above is
    what forces the entry to exist, and this is only what the card does if that
    guard is bypassed. Falling back to the neutral default would hide the very
    record whose state nothing here could explain.
    """

    with mock.patch.dict(PUBLICATION_STATE_TONES):
        del PUBLICATION_STATE_TONES[PublicationState.FAILED]
        classes = _record_classes(PublicationState.FAILED.value)

    assert f"tone-{ANOMALOUS_PUBLICATION_TONE}" in classes
    assert "tone-pending" not in classes
