"""Tests for the LibreTexts catalog seed generator.

The generator's output decides which source pages can be published to ADAPT and
what identity their alignments carry, so these tests care about two things
above all: that a generated seed actually loads through the real validator in
``app/catalog.py``, and that page identity never drifts.

No test here touches the network. The HTTP layer is injected, and the Deki
payloads are the shapes the live API really returns -- including the awkward
ones (a single subpage collapsed into a bare object, ``{'#text': ...}`` fields).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID

import pytest

from app import catalog

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    """Import the generator by path -- ``scripts/`` is not an installed package."""

    path = REPO_ROOT / "scripts" / "generate_catalog_seed.py"
    spec = importlib.util.spec_from_file_location("generate_catalog_seed", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


BOOK = "https://chem.libretexts.org/Bookshelves/Fake/Example_Book"


def _subpage(title: str, url: str, *, children: bool = True) -> dict:
    return {"title": title, "uri.ui": url, "@subpages": "true" if children else "false"}


def _fake_http(pages: dict[str, list[dict]]):
    """An http_get that serves canned subpage payloads keyed by page URL."""

    calls: list[str] = []

    def http_get(api_url: str) -> str:
        calls.append(api_url)
        for page_url, subpages in pages.items():
            if gen.subpages_api_url(page_url) == api_url:
                return json.dumps({"page.subpage": subpages})
        return json.dumps({"page.subpage": []})

    http_get.calls = calls  # type: ignore[attr-defined]
    return http_get


def _two_chapter_book() -> dict[str, list[dict]]:
    ch1, ch2 = f"{BOOK}/01%3A_One", f"{BOOK}/02%3A_Two"
    return {
        BOOK: [
            _subpage("Front Matter", f"{BOOK}/00%3A_Front_Matter"),
            _subpage("1: One", ch1),
            _subpage("2: Two", ch2),
            _subpage("Back Matter", f"{BOOK}/zz%3A_Back_Matter"),
        ],
        ch1: [
            _subpage("1.1: First", f"{ch1}/1.01%3A_First", children=False),
            _subpage("1.2: Second", f"{ch1}/1.02%3A_Second", children=False),
        ],
        ch2: [_subpage("2.1: Third", f"{ch2}/2.01%3A_Third", children=False)],
    }


def _build(pages: dict[str, list[dict]] | None = None, **kwargs) -> dict:
    outline = gen.fetch_outline(BOOK, http_get=_fake_http(pages or _two_chapter_book()))
    return gen.build_seed(
        outline,
        **{
            "author": "A. Author",
            "license_code": "ccbyncsa",
            "license_version": "4.0",
            **kwargs,
        },
    )


# --------------------------------------------------------------------------
# Identity: the properties that keep already-published alignments resolving.
# --------------------------------------------------------------------------


def test_normalization_matches_the_application():
    """The generator duplicates normalize_source_url; it must not drift.

    Identity is computed from normalized URLs on one side and matched against
    them on the other, so a divergence here would silently make published
    topics unfindable.
    """

    for url in (
        "HTTPS://Chem.LibreTexts.org/Bookshelves/A/B/",
        "https://chem.libretexts.org/Bookshelves/A/01%3A_Matter",
        "https://chem.libretexts.org/Bookshelves/A/01:_Matter?x=1#frag",
        BOOK,
    ):
        assert gen.normalize_source_url(url) == catalog.normalize_source_url(url)


def test_stable_ids_are_a_pure_function_of_the_url():
    """Regenerating an unchanged book must reproduce every id exactly."""

    first, second = _build(), _build()
    assert first == second

    ids = gen.topic_ids(first)
    assert len(set(ids.values())) == len(ids), "ids must be unique per topic"


def test_ids_survive_cosmetic_url_differences():
    """A trailing slash or upper-case host must not mint a new identity."""

    namespace = gen.namespace_for(BOOK)
    assert gen.namespace_for(BOOK + "/") == namespace
    assert gen.namespace_for(BOOK.replace("chem.", "CHEM.")) == namespace


def test_ids_are_encoding_independent():
    """Percent-encoding must not change a page's identity.

    This is the bug that shipped in the first draft. The Deki API returns
    ``Mathematical_Methods_in_Chemistry_(Levitus)`` with literal parentheses,
    while every curated seed stores ``..._%28Levitus%29``. Hashing the raw
    string gave the same page two different ids depending on which source it
    came from, so a regenerated seed silently orphaned published alignments --
    and it only surfaced when a generated seed was diffed against a real one.
    """

    namespace = gen.namespace_for(BOOK)
    encoded = "https://chem.libretexts.org/Bookshelves/A/Book_%28Author%29/01%3A_One"
    literal = "https://chem.libretexts.org/Bookshelves/A/Book_(Author)/01:_One"

    assert gen.stable_id_for(namespace, encoded) == gen.stable_id_for(
        namespace, literal
    )
    assert gen.stable_id_for(namespace, encoded) == gen.stable_id_for(
        namespace, encoded + "/"
    )


def test_seed_stores_normalized_urls():
    """The stored URL must not vary with the API's encoding of the day."""

    book = "https://chem.libretexts.org/Bookshelves/Fake/Example_Book"
    chapter = f"{book}/01%3A_One"
    literal_chapter = f"{book}/01:_One"
    pages = {
        book: [_subpage("1: One", literal_chapter)],
        literal_chapter: [
            _subpage("1.1: A", f"{literal_chapter}/1.01:_A", children=False)
        ],
    }
    seed = _build(pages)

    stored = seed["chapters"][0]["canonical_url"]
    assert stored == catalog.normalize_source_url(chapter)
    assert stored == catalog.normalize_source_url(literal_chapter)


def test_reproduces_the_installed_levitus_seed_identity():
    """Golden test against a real curated seed.

    The Levitus catalog was authored by hand with this scheme. If the generator
    reproduces its namespace and every stable_id from URLs alone, the scheme is
    implemented correctly -- this is the strongest evidence available without
    hitting the network.
    """

    seed_path = (
        REPO_ROOT / "app" / "catalogs" / "mathematical-methods-chemistry-v1.json"
    )
    if not seed_path.exists():  # pragma: no cover - seed always ships today
        pytest.skip("Levitus seed not installed")
    seed = json.loads(seed_path.read_text())

    namespace = gen.namespace_for(seed["framework"]["source_url"])
    assert str(namespace) == seed["stable_id_namespace"]

    checked = 0
    for chapter in seed["chapters"]:
        assert (
            gen.stable_id_for(namespace, chapter["canonical_url"])
            == chapter["stable_id"]
        )
        checked += 1
        for topic in chapter["topics"]:
            assert (
                gen.stable_id_for(namespace, topic["canonical_url"])
                == topic["stable_id"]
            )
            checked += 1
    assert checked >= 6


# --------------------------------------------------------------------------
# The output has to be loadable by the real validator, not just well-shaped.
# --------------------------------------------------------------------------


def test_generated_seed_loads_through_the_real_validator(tmp_path):
    """Round-trip through ``app.catalog._load_seed``.

    Asserting on our own JSON only proves the generator agrees with itself.
    This runs the application's actual loader, so a schema change on either
    side fails here rather than at deploy time.
    """

    path = tmp_path / "generated-v1.json"
    path.write_text(gen.render(_build()))

    loaded = catalog._load_seed(path)
    assert loaded["schema_version"] == 1
    assert loaded["chapters"]
    UUID(loaded["stable_id_namespace"])


def test_generated_seed_survives_catalog_topic_construction(tmp_path, monkeypatch):
    """Load a generated seed the way the app does, end to end.

    ``curated_topics()`` enforces the uniqueness rules that ``_load_seed`` does
    not -- duplicate chapter ids, duplicate topic ids, duplicate topic URLs.
    """

    path = tmp_path / "generated-v1.json"
    path.write_text(gen.render(_build()))

    for cached in (
        catalog.framework_seeds,
        catalog.curated_frameworks,
        catalog.curated_topics,
        catalog._catalog_paths,
    ):
        cached.cache_clear()
    monkeypatch.setattr(catalog, "_catalog_paths", lambda: (path,))
    try:
        topics = catalog.curated_topics()
        assert len(topics) == 3
        assert {topic.title for topic in topics} == {
            "1.1: First",
            "1.2: Second",
            "2.1: Third",
        }
        # The whole point: a generated topic resolves an alignment by URL.
        alignment = catalog.alignment_for_source(topics[0].canonical_url)
        assert alignment is not None
        assert alignment.topic.stable_id == topics[0].stable_id
    finally:
        for cached in (
            catalog.framework_seeds,
            catalog.curated_frameworks,
            catalog.curated_topics,
        ):
            cached.cache_clear()


# --------------------------------------------------------------------------
# Outline construction.
# --------------------------------------------------------------------------


def test_front_and_back_matter_are_excluded():
    seed = _build()
    titles = [chapter["title"] for chapter in seed["chapters"]]
    assert titles == ["1: One", "2: Two"]


def test_ordering_is_preserved_and_one_based():
    seed = _build()
    assert [chapter["order"] for chapter in seed["chapters"]] == [1, 2]
    assert [topic["order"] for topic in seed["chapters"][0]["topics"]] == [1, 2]


def test_chapters_with_no_pages_are_dropped():
    """An empty chapter would contribute nothing and read as a gap in numbering."""

    pages = _two_chapter_book()
    pages[f"{BOOK}/02%3A_Two"] = []
    seed = _build(pages)
    assert [chapter["title"] for chapter in seed["chapters"]] == ["1: One"]
    assert seed["chapters"][0]["order"] == 1


def test_duplicate_topic_urls_are_dropped_not_shipped():
    """catalog.py rejects an entire catalog for one duplicate topic URL.

    Books do repeat a page across chapters, so dropping the repeat keeps the
    other 250 topics publishable instead of failing the whole seed.
    """

    pages = _two_chapter_book()
    shared = f"{BOOK}/01%3A_One/1.01%3A_First"
    pages[f"{BOOK}/02%3A_Two"] = [_subpage("2.1: Repeat", shared, children=False)]
    seed = _build(pages)

    urls = [t["canonical_url"] for c in seed["chapters"] for t in c["topics"]]
    assert len(urls) == len(set(urls))
    assert [c["title"] for c in seed["chapters"]] == ["1: One"]


def test_trailing_slash_duplicate_is_caught_by_normalization():
    """The same page with a trailing slash is the same page."""

    pages = _two_chapter_book()
    pages[f"{BOOK}/02%3A_Two"] = [
        _subpage("2.1: Repeat", f"{BOOK}/01%3A_One/1.01%3A_First/", children=False)
    ]
    seed = _build(pages)
    assert sum(len(c["topics"]) for c in seed["chapters"]) == 2


# --------------------------------------------------------------------------
# API address construction and payload parsing.
# --------------------------------------------------------------------------


def test_subpages_url_double_encodes_the_path():
    """Deki addresses a page by path only when the path is encoded again."""

    url = gen.subpages_api_url("https://chem.libretexts.org/Bookshelves/A/01%3A_Matter")
    assert url.startswith("https://chem.libretexts.org/@api/deki/pages/=")
    assert "Bookshelves%2FA%2F01%253A_Matter" in url
    assert url.endswith("/subpages?dream.out.format=json")


@pytest.mark.parametrize("value", ["", "not-a-url", "/relative/only", "https://"])
def test_subpages_url_rejects_non_absolute_input(value):
    with pytest.raises(gen.SeedGeneratorError):
        gen.subpages_api_url(value)


def test_single_subpage_is_returned_as_a_bare_object():
    """Deki collapses a one-element list; treating it as a list loses the page."""

    payload = json.dumps({"page.subpage": _subpage("1: Only", f"{BOOK}/01%3A_Only")})
    pages = gen.parse_subpages(payload, context=BOOK)
    assert [page.title for page in pages] == ["1: Only"]


def test_text_fields_may_be_wrapped_objects():
    payload = json.dumps(
        {
            "page.subpage": [
                {
                    "title": {"#text": "1: Wrapped"},
                    "uri.ui": {"#text": f"{BOOK}/01%3A_Wrapped"},
                    "@subpages": "false",
                }
            ]
        }
    )
    pages = gen.parse_subpages(payload, context=BOOK)
    assert pages[0].title == "1: Wrapped"
    assert pages[0].canonical_url == f"{BOOK}/01%3A_Wrapped"


def test_rows_missing_a_title_or_url_are_skipped_not_fatal():
    """One malformed row must not cost the other 250 pages."""

    payload = json.dumps(
        {
            "page.subpage": [
                {"title": "1: Good", "uri.ui": f"{BOOK}/01%3A_Good"},
                {"title": "2: No URL"},
                {"uri.ui": f"{BOOK}/03%3A_No_Title"},
                "not even an object",
            ]
        }
    )
    assert [page.title for page in gen.parse_subpages(payload, context=BOOK)] == [
        "1: Good"
    ]


@pytest.mark.parametrize("payload", ["", "<html>nope</html>", "[1,2,3]", "null"])
def test_malformed_api_responses_raise_a_clear_error(payload):
    with pytest.raises(gen.SeedGeneratorError):
        gen.parse_subpages(payload, context=BOOK)


def test_empty_book_is_an_error_not_an_empty_seed():
    """catalog.py rejects a chapterless seed, so fail before writing one."""

    with pytest.raises(gen.SeedGeneratorError, match="no content chapters"):
        gen.fetch_outline(BOOK, http_get=_fake_http({BOOK: []}))


def test_book_of_only_front_matter_is_an_error():
    with pytest.raises(gen.SeedGeneratorError, match="no content chapters"):
        gen.fetch_outline(
            BOOK,
            http_get=_fake_http({BOOK: [_subpage("Front Matter", f"{BOOK}/00")]}),
        )


def test_network_failure_surfaces_as_a_generator_error():
    def exploding(_url: str) -> str:
        raise gen.SeedGeneratorError("connection refused")

    with pytest.raises(gen.SeedGeneratorError, match="connection refused"):
        gen.fetch_outline(BOOK, http_get=exploding)


def test_one_request_per_page_no_refetching():
    """Chapter pages are fetched once each; a book is ~30 requests, not 900."""

    http_get = _fake_http(_two_chapter_book())
    gen.fetch_outline(BOOK, http_get=http_get)
    assert len(http_get.calls) == 3  # the book, plus one per chapter
    assert len(set(http_get.calls)) == 3


# --------------------------------------------------------------------------
# The overwrite guard.
# --------------------------------------------------------------------------


def test_conflicting_ids_are_detected_across_schemes():
    """A seed built by another scheme has different ids for the same pages.

    Overwriting it would orphan every question already published against those
    topics, which is what the ``--force`` guard exists to prevent.
    """

    seed = _build()
    foreign = json.loads(json.dumps(seed))
    foreign["chapters"][0]["topics"][0]["stable_id"] = (
        "00000000-0000-5000-8000-000000000000"
    )

    conflicts = gen.conflicting_topic_ids(foreign, seed)
    assert len(conflicts) == 1
    assert conflicts[0] == gen.normalize_source_url(
        seed["chapters"][0]["topics"][0]["canonical_url"]
    )


def test_no_conflict_when_regenerating_the_same_book():
    seed = _build()
    assert gen.conflicting_topic_ids(seed, _build()) == []


def test_new_topics_are_not_reported_as_conflicts():
    """A book that gained a page is an addition, not an identity change."""

    pages = _two_chapter_book()
    seed_before = _build(pages)
    pages[f"{BOOK}/02%3A_Two"].append(
        _subpage("2.2: Added", f"{BOOK}/02%3A_Two/2.02%3A_Added", children=False)
    )
    assert gen.conflicting_topic_ids(seed_before, _build(pages)) == []


def test_cli_refuses_to_overwrite_a_conflicting_seed(tmp_path, capsys, monkeypatch):
    destination = tmp_path / "existing-v1.json"
    foreign = json.loads(json.dumps(_build()))
    foreign["chapters"][0]["topics"][0]["stable_id"] = (
        "00000000-0000-5000-8000-000000000000"
    )
    destination.write_text(json.dumps(foreign))
    original = destination.read_text()

    monkeypatch.setattr(
        gen,
        "fetch_outline",
        lambda url, **kw: gen.fetch_outline(
            BOOK, http_get=_fake_http(_two_chapter_book())
        ),
    )
    code = gen.main(
        [BOOK, "--author", "A", "--license", "ccby", "--out", str(destination)]
    )

    assert code == 2
    assert destination.read_text() == original, "must not write on refusal"
    assert "stable_id" in capsys.readouterr().err


def test_cli_force_overwrites(tmp_path, monkeypatch):
    destination = tmp_path / "existing-v1.json"
    foreign = json.loads(json.dumps(_build()))
    foreign["chapters"][0]["topics"][0]["stable_id"] = (
        "00000000-0000-5000-8000-000000000000"
    )
    destination.write_text(json.dumps(foreign))

    monkeypatch.setattr(
        gen,
        "fetch_outline",
        lambda url, **kw: gen.fetch_outline(
            BOOK, http_get=_fake_http(_two_chapter_book())
        ),
    )
    code = gen.main(
        [
            BOOK,
            "--author",
            "A",
            "--license",
            "ccby",
            "--out",
            str(destination),
            "--force",
        ]
    )

    assert code == 0
    assert json.loads(destination.read_text()) == _build()


def test_cli_writes_a_new_seed_and_reports_counts(tmp_path, capsys, monkeypatch):
    destination = tmp_path / "new-v1.json"
    monkeypatch.setattr(
        gen,
        "fetch_outline",
        lambda url, **kw: gen.fetch_outline(
            BOOK, http_get=_fake_http(_two_chapter_book())
        ),
    )
    code = gen.main(
        [
            BOOK,
            "--author",
            "A. Author",
            "--license",
            "ccbyncsa",
            "--license-version",
            "4.0",
            "--out",
            str(destination),
        ]
    )

    assert code == 0
    assert json.loads(destination.read_text()) == _build()
    assert "3 publishable topics" in capsys.readouterr().err


def test_cli_reports_generation_failure_without_writing(tmp_path, capsys, monkeypatch):
    destination = tmp_path / "never-written.json"

    def exploding(*_args, **_kwargs):
        raise gen.SeedGeneratorError("book not found")

    monkeypatch.setattr(gen, "fetch_outline", exploding)
    code = gen.main(
        [BOOK, "--author", "A", "--license", "ccby", "--out", str(destination)]
    )

    assert code == 1
    assert not destination.exists()
    assert "book not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Framework metadata.
# --------------------------------------------------------------------------


def test_framework_carries_every_field_the_loader_requires():
    framework = _build()["framework"]
    for required in (
        "title",
        "author",
        "descriptor_type",
        "description",
        "license",
        "source_url",
    ):
        assert framework[required], f"{required} must be populated"


def test_title_defaults_to_the_book_slug_and_can_be_overridden():
    outline = gen.fetch_outline(BOOK, http_get=_fake_http(_two_chapter_book()))
    assert outline.title == "Example Book"

    named = gen.fetch_outline(
        BOOK, http_get=_fake_http(_two_chapter_book()), book_title="Real Title"
    )
    assert named.title == "Real Title"


def test_license_version_may_be_absent():
    """``license_version`` is optional in the loader; None must round-trip."""

    seed = _build(license_version=None)
    assert seed["framework"]["license_version"] is None
    assert "license_version" in seed["framework"]
