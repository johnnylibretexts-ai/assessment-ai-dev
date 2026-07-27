from pathlib import Path

import app.catalog
from app.catalog import (
    alignment_by_topic_id,
    alignment_for_source,
    curated_frameworks,
    curated_topics,
    normalize_source_url,
    source_license,
)


MATHEMATICAL_METHODS_URL = (
    "https://chem.libretexts.org/Bookshelves/"
    "Physical_and_Theoretical_Chemistry_Textbook_Maps/"
    "Mathematical_Methods_in_Chemistry_%28Levitus%29/"
    "05%3A_Second_Order_Ordinary_Differential_Equations/"
    "5.01%3A_Second_Order_Ordinary_Differential_Equations"
)


def test_catalog_loads_multiple_frameworks_with_unique_exact_mappings() -> None:
    frameworks = curated_frameworks()
    topics = curated_topics()

    # Counted from the seed files rather than hardcoded: adding a book is a
    # routine, expected change, and a literal here just fails on the next one
    # without saying anything about correctness. What must hold is that every
    # installed seed loads and that nothing collides.
    installed = list((Path(app.catalog.__file__).parent / "catalogs").glob("*.json"))
    assert len(installed) >= 2
    assert len(frameworks) == len(installed)
    assert len({item.title for item in frameworks}) == len(installed)
    assert len({item.stable_id_namespace for item in frameworks}) == len(installed)
    assert len({item.stable_id for item in topics}) == len(topics)
    assert len({normalize_source_url(item.canonical_url) for item in topics}) == len(
        topics
    )


def test_mathematical_methods_source_maps_to_its_own_framework_and_license() -> None:
    alignment = alignment_for_source(MATHEMATICAL_METHODS_URL)

    assert alignment is not None
    assert alignment.framework.title == "Mathematical Methods in Chemistry (Levitus)"
    assert alignment.topic.title == "5.1: Second Order Ordinary Differential Equations"
    assert alignment_by_topic_id(alignment.topic.stable_id) == alignment
    license_mapping = source_license(MATHEMATICAL_METHODS_URL)
    assert license_mapping is not None
    assert license_mapping.code == "ccbyncsa"
    assert license_mapping.version == "4.0"


def test_unmapped_source_never_gets_a_merely_similar_topic() -> None:
    assert (
        alignment_for_source(
            "https://chem.libretexts.org/Bookshelves/Other/Ordinary_Differential_Equations"
        )
        is None
    )


CHEMISTRY_2E_URL = (
    "https://chem.libretexts.org/Bookshelves/General_Chemistry/"
    "Chemistry_2e_%28OpenStax%29/02%3A_Atoms_Molecules_and_Ions/"
    "2.01%3A_Early_Ideas_in_Atomic_Theory"
)

CONCEPTS_IN_BIOLOGY_URL = (
    "https://bio.libretexts.org/Bookshelves/Introductory_and_General_Biology/"
    "Concepts_in_Biology_%28OpenStax%29/05%3A_Photosynthesis/"
    "5.01%3A_Overview_of_Photosynthesis"
)


def test_chemistry_2e_can_be_published() -> None:
    """The book that used to be the standard demo failure.

    Chemistry 2e generated fine and resolved its license, then refused to
    publish because no curated topic matched -- the error people hit first.
    """

    alignment = alignment_for_source(CHEMISTRY_2E_URL)

    assert alignment is not None
    assert alignment.framework.title == "Chemistry 2e (OpenStax)"
    assert alignment.topic.title == "2.1: Early Ideas in Atomic Theory"
    assert alignment_by_topic_id(alignment.topic.stable_id) == alignment

    license_mapping = source_license(CHEMISTRY_2E_URL)
    assert license_mapping is not None
    assert license_mapping.code == "ccby"
    assert license_mapping.version == "4.0"


def test_concepts_in_biology_can_be_published() -> None:
    """A book on bio.libretexts.org, whose Deki API is closed to us."""

    alignment = alignment_for_source(CONCEPTS_IN_BIOLOGY_URL)

    assert alignment is not None
    assert alignment.framework.title == "Concepts in Biology (OpenStax)"
    assert alignment.topic.title == "5.1: Overview of Photosynthesis"
    assert alignment_by_topic_id(alignment.topic.stable_id) == alignment

    license_mapping = source_license(CONCEPTS_IN_BIOLOGY_URL)
    assert license_mapping is not None
    assert license_mapping.code == "ccby"
    assert license_mapping.version == "4.0"


def test_generated_catalogs_match_on_url_not_on_encoding() -> None:
    """Both spellings of a page are the same page.

    The seeds store %28/%29; a browser or the Deki API may hand back literal
    parentheses. Publishing must succeed either way.
    """

    canonical = alignment_for_source(CHEMISTRY_2E_URL)
    assert canonical is not None

    literal = CHEMISTRY_2E_URL.replace("%28", "(").replace("%29", ")")
    assert alignment_for_source(literal) == canonical
    # `is not None` would pass on a *different* topic, which is the failure
    # this is guarding against.
    assert alignment_for_source(CHEMISTRY_2E_URL + "/") == canonical
