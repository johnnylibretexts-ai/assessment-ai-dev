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

    assert len(frameworks) == 2
    assert len({item.title for item in frameworks}) == 2
    assert len({item.stable_id_namespace for item in frameworks}) == 2
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
