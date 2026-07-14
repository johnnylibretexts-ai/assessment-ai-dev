from __future__ import annotations

import hashlib

from app.adapt import (
    AdaptDestination,
    build_assessment_payload,
    build_external_engine_payload,
)
from app.parameterized import compile_parameterized_item
from app.qti import preflight_qti
from app.schemas import AssessmentItemType, ItemContextType
from app.source_policy import parse_public_source_url
from evaluation.fixtures import build_draft, build_fixture_bundle, build_seed_plan
from evaluation.models import (
    CorpusManifest,
    CorpusPage,
    DomainStratum,
    EngineProbeReceipt,
    Publishability,
    ReviewRecord,
    ReviewRole,
    SeedReceipt,
    ShadowMode,
    ShadowReceipt,
)
from evaluation.validators import (
    compare_shadow_receipts,
    validate_corpus_manifest,
    validate_engine_probe_receipts,
    validate_review_ledger,
    validate_seed_receipts,
)


DESTINATION = AdaptDestination(
    folder_id=1,
    author="LibreTexts Assessment AI",
    license="CC BY 4.0",
)


def test_fixture_bundle_covers_every_type_and_context() -> None:
    bundle = build_fixture_bundle()

    assert len(bundle.cases) == 95
    assert {case.draft.item_type for case in bundle.cases} == set(AssessmentItemType)
    assert {case.draft.context_type for case in bundle.cases} == set(ItemContextType)
    assert all(len(case.hint_ladder.rungs) == 3 for case in bundle.cases)


def test_every_type_maps_to_adapt_and_schema_valid_qti() -> None:
    for item_type in AssessmentItemType:
        draft = build_draft(item_type)
        preflight_qti(
            draft,
            publication_key=f"build08-{item_type.value}",
            title=item_type.value,
            metadata={"source": "sealed-fixture"},
        )
        if item_type in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}:
            assert draft.response.parameterized is not None
            compiled = compile_parameterized_item(
                draft.response.parameterized, validation_seeds=100
            )
            payload = build_external_engine_payload(
                draft,
                destination=DESTINATION,
                source_url="https://math.libretexts.org/Books/Fixture",
                title=item_type.value,
                engine_source=compiled.source,
                technology_id=1 if item_type == AssessmentItemType.IMATHAS else None,
            )
            assert payload["technology"] == item_type.value
        else:
            payload = build_assessment_payload(
                draft,
                destination=DESTINATION,
                source_url="https://chem.libretexts.org/Books/Fixture",
                title=item_type.value,
            )
            assert payload["technology"] == "qti"


def test_seed_plan_has_twenty_items_per_engine_and_one_hundred_seeds() -> None:
    plan = build_seed_plan()

    assert len(plan) == 4_000
    for item_type in (AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS):
        items = {case.item_id for case in plan if case.item_type == item_type}
        assert len(items) == 20
        for item_id in items:
            assert {case.seed for case in plan if case.item_id == item_id} == set(
                range(1, 101)
            )


def test_corpus_manifest_enforces_six_balanced_strata() -> None:
    manifest = _corpus_manifest()
    result = validate_corpus_manifest(manifest)

    assert result.passed
    assert result.counts["pages"] == 48


def test_review_validator_enforces_per_type_thresholds() -> None:
    records = _review_records()
    passing = validate_review_ledger(records)

    assert passing.passed
    assert passing.counts["primary_drafts"] == 380
    assert passing.counts["minimum_kappa"] == 1.0

    failing = validate_review_ledger(
        [
            record.model_copy(update={"critical_defect": True})
            if record.item_type == AssessmentItemType.MULTIPLE_CHOICE
            and record.role == ReviewRole.PRIMARY
            and record.draft_id.endswith("-00")
            else record
            for record in records
        ]
    )
    assert not failing.passed
    assert any("critical defect rate" in failure for failure in failing.failures)


def test_seed_validator_requires_every_execution_check() -> None:
    receipts = _seed_receipts()
    passing = validate_seed_receipts(receipts)

    assert passing.passed
    assert passing.counts["receipts"] == 4_000

    receipts[0] = receipts[0].model_copy(update={"outbound_request_count": 1})
    failing = validate_seed_receipts(receipts)
    assert not failing.passed
    assert failing.counts["failed_receipts"] == 1


def test_engine_probe_validator_requires_both_complete_runtime_matrices() -> None:
    receipt = EngineProbeReceipt(
        run_id="probe-run",
        item_id="webwork-01",
        item_type=AssessmentItemType.WEBWORK,
        seed=1,
        compiler_version="parameterized-dsl-v1",
        source_sha256=_sha("source"),
        endpoint_host="wwrenderer.libretexts.dev",
        engine_image_sha256="sha256:" + "a" * 64,
        network_isolation_attestation_sha256=_sha("network"),
        runtime_values_sha256=_sha("values"),
        semantic_render_sha256=_sha("render"),
        deterministic=True,
        constraints_satisfied=True,
        rendered=True,
        render_duration_ms=10,
        warning_count=0,
        error_count=0,
        expected_answer_accepted=True,
        wrong_answer_rejected=True,
        expected_score=1,
        wrong_score=0,
        remaining_checks=[
            "persisted_grade_match",
            "object_idempotent",
            "cross_owner_access_blocked",
        ],
    )

    result = validate_engine_probe_receipts([receipt])

    assert not result.passed
    assert result.counts["failed_receipts"] == 0
    assert any("imathas" in failure for failure in result.failures)


def test_shadow_comparator_requires_event_completeness_and_exact_parity() -> None:
    receipts = _shadow_receipts()
    passing = compare_shadow_receipts(receipts)

    assert passing.passed
    assert passing.counts["paired_cases"] == 19
    assert passing.counts["telemetry_completeness"] == 1.0

    receipts[1] = receipts[1].model_copy(update={"grade": 0.5})
    failing = compare_shadow_receipts(receipts)
    assert not failing.passed
    assert any("parity changed" in failure for failure in failing.failures)


def _corpus_manifest() -> CorpusManifest:
    sources = {
        DomainStratum.CHEMISTRY: "chem",
        DomainStratum.BIOLOGY: "bio",
        DomainStratum.MATHEMATICS: "math",
        DomainStratum.MEDICINE_HEALTH: "med",
        DomainStratum.HUMANITIES_SOCIAL: "human",
        DomainStratum.SPANISH_FRENCH: "espanol",
    }
    pages: list[CorpusPage] = []
    for stratum, library in sources.items():
        for index in range(8):
            canonical_url = (
                f"https://{library}.libretexts.org/Bookshelves/BUILD08/"
                f"{stratum.value}/{index}"
            )
            location = parse_public_source_url(canonical_url)
            pages.append(
                CorpusPage(
                    page_key=f"{stratum.value}-{index}",
                    stratum=stratum,
                    title=f"{stratum.value} source {index}",
                    canonical_url=canonical_url,
                    source_identity=location.identity,
                    source_page_id=str(index),
                    license="CC BY 4.0",
                    content_sha256=_sha(f"content-{stratum.value}-{index}"),
                    paragraph_sha256=[_sha(f"paragraph-{stratum.value}-{index}")],
                )
            )
    return CorpusManifest(pages=pages)


def _review_records() -> list[ReviewRecord]:
    records: list[ReviewRecord] = []
    for item_type in AssessmentItemType:
        for index in range(20):
            draft_id = f"{item_type.value}-{index:02d}"
            values = {
                "run_id": "review-run",
                "draft_id": draft_id,
                "page_key": f"page-{index:02d}",
                "item_type": item_type,
                "context_type": ItemContextType.STANDARD,
                "reviewer_id": f"primary-{item_type.value}",
                "role": ReviewRole.PRIMARY,
                "critical_defect": False,
                "factual_correct": True,
                "source_supported": True,
                "answer_correct": True,
                "interaction_quality": True,
                "bloom_aligned": True,
                "difficulty_aligned": True,
                "accessible": True,
                "publishability": Publishability.NO_EDIT,
                "citation_complete": True,
                "license_complete": True,
                "hint_ladder_reviewed": True,
                "hints_non_leaking": True,
                "hints_progressive": True,
                "specialist_qualified": item_type == AssessmentItemType.BOW_TIE,
                "clinical_approved": (
                    True if item_type == AssessmentItemType.BOW_TIE else None
                ),
            }
            records.append(ReviewRecord.model_validate(values))
            if index < 4:
                records.append(
                    ReviewRecord.model_validate(
                        {
                            **values,
                            "reviewer_id": f"independent-{item_type.value}",
                            "role": ReviewRole.INDEPENDENT,
                        }
                    )
                )
    return records


def _seed_receipts() -> list[SeedReceipt]:
    receipts: list[SeedReceipt] = []
    for item_type in (AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS):
        for item_index in range(20):
            item_id = f"{item_type.value}-{item_index:02d}"
            source_sha = _sha(item_id)
            for seed in range(1, 101):
                receipts.append(
                    SeedReceipt(
                        run_id="seed-run",
                        item_id=item_id,
                        item_type=item_type,
                        seed=seed,
                        compiler_version="parameterized-dsl-v1",
                        source_sha256=source_sha,
                        deterministic=True,
                        constraints_satisfied=True,
                        rendered=True,
                        render_duration_ms=10,
                        warning_count=0,
                        error_count=0,
                        outbound_request_count=0,
                        expected_answer_accepted=True,
                        wrong_answer_rejected=True,
                        persisted_grade_match=True,
                        object_idempotent=True,
                        cross_owner_access_blocked=True,
                    )
                )
    return receipts


def _shadow_receipts() -> list[ShadowReceipt]:
    receipts: list[ShadowReceipt] = []
    for item_type in AssessmentItemType:
        case_id = f"shadow-{item_type.value}"
        shared = {
            "run_id": "shadow-run",
            "case_id": case_id,
            "item_type": item_type,
            "student_dom_sha256": _sha(f"dom-{case_id}"),
            "student_api_sha256": _sha(f"api-{case_id}"),
            "submission_sha256": _sha(f"submission-{case_id}"),
            "grade": 1.0,
            "penalty": 0.0,
            "shown_hint_state": "[]",
            "gradebook_sha256": _sha(f"gradebook-{case_id}"),
        }
        receipts.append(
            ShadowReceipt(
                **shared,
                mode=ShadowMode.OFF,
                request_id=f"request-off-{item_type.value}",
                event_ids=[],
                expected_event_count=0,
                observed_event_count=0,
            )
        )
        receipts.append(
            ShadowReceipt(
                **shared,
                mode=ShadowMode.OBSERVE,
                request_id=f"request-observe-{item_type.value}",
                event_ids=[f"event-{item_type.value}-{index}" for index in range(3)],
                expected_event_count=3,
                observed_event_count=3,
            )
        )
    return receipts


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
