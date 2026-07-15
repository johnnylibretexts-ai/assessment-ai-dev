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
    DraftQualificationReceipt,
    DomainStratum,
    EngineProbeReceipt,
    OutageBoundary,
    OutageOutcome,
    OutageReceipt,
    Publishability,
    ProviderCallReceipt,
    ReviewRecord,
    ReviewRole,
    SeedReceipt,
    ShadowMode,
    ShadowReceipt,
)
from evaluation.validators import (
    compare_shadow_receipts,
    validate_corpus_manifest,
    validate_draft_qualification_receipts,
    validate_engine_probe_receipts,
    validate_outage_receipts,
    validate_provider_call_receipts,
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


def test_automated_pilot_replaces_human_review_as_release_gate() -> None:
    manifest = _corpus_manifest()
    drafts, calls = _automated_draft_receipts(manifest, rounds=1)

    budget = validate_provider_call_receipts(calls)
    pilot = validate_draft_qualification_receipts(
        drafts,
        calls,
        manifest,
        mode="pilot",
    )

    assert budget.passed
    assert pilot.passed
    assert pilot.counts["drafts"] == 19
    assert pilot.counts["publication_attempts"] == 0

    drafts[0] = drafts[0].model_copy(update={"artifact_label": "invalid"})
    # The label is a schema-level guard, so invalid evidence cannot be loaded.
    try:
        DraftQualificationReceipt.model_validate(drafts[0].model_dump())
    except ValueError:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unreviewed artifact label must be immutable")


def test_full_automated_gate_requires_380_drafts_and_all_corpus_pages() -> None:
    manifest = _corpus_manifest()
    drafts, calls = _automated_draft_receipts(manifest, rounds=20)

    result = validate_draft_qualification_receipts(drafts, calls, manifest)

    assert result.passed
    assert result.counts["drafts"] == 380
    assert result.counts["used_pages"] == 48

    drafts[19] = drafts[19].model_copy(update={"hint_leak_detected": True})
    failing = validate_draft_qualification_receipts(drafts, calls, manifest)
    assert not failing.passed
    assert any("failed hint leak" in failure for failure in failing.failures)


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


def test_outage_validator_requires_every_independent_boundary() -> None:
    receipts = [
        OutageReceipt(
            run_id="outage-run",
            boundary=boundary,
            injection_method="isolated_failure_injection",
            failure_code=f"{boundary.value}_unavailable",
            outcome=(
                OutageOutcome.TERMINAL
                if boundary == OutageBoundary.PROVIDER
                else OutageOutcome.RETRYABLE
            ),
            retry_safe=boundary != OutageBoundary.PROVIDER,
            terminal_safe=boundary == OutageBoundary.PROVIDER,
            no_partial_publication=True,
            adapt_core_ready=True,
            recovered=True,
            secrets_redacted=True,
            advanced_flags_false=True,
            evidence_sha256=_sha(boundary.value),
            platform_state_sha256=_sha("platform-state"),
        )
        for boundary in OutageBoundary
    ]

    passing = validate_outage_receipts(receipts)
    assert passing.passed
    assert passing.counts == {
        "receipts": 7,
        "boundaries": 7,
        "retryable": 6,
        "terminal": 1,
    }

    failing = validate_outage_receipts(receipts[:-1])
    assert not failing.passed
    assert any("qti_storage" in failure for failure in failing.failures)

    duplicated = receipts.copy()
    duplicated[-1] = duplicated[-1].model_copy(
        update={"evidence_sha256": duplicated[0].evidence_sha256}
    )
    duplicate_evidence = validate_outage_receipts(duplicated)
    assert not duplicate_evidence.passed
    assert any(
        "independent evidence" in failure for failure in duplicate_evidence.failures
    )

    mixed_state = receipts.copy()
    mixed_state[-1] = mixed_state[-1].model_copy(
        update={"platform_state_sha256": _sha("different-platform-state")}
    )
    mixed_state_result = validate_outage_receipts(mixed_state)
    assert not mixed_state_result.passed
    assert any("platform-state" in failure for failure in mixed_state_result.failures)


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


def _automated_draft_receipts(
    manifest: CorpusManifest,
    *,
    rounds: int,
) -> tuple[list[DraftQualificationReceipt], list[ProviderCallReceipt]]:
    pages = list(manifest.pages)
    medicine_pages = [
        page for page in pages if page.stratum == DomainStratum.MEDICINE_HEALTH
    ]
    drafts: list[DraftQualificationReceipt] = []
    calls: list[ProviderCallReceipt] = []
    stages = (
        "concept_extraction",
        "initial_draft",
        "critique",
        "revision",
        "hint_ladder",
    )
    non_bow_index = 0
    sequence = 0
    call_sequence = 0
    for round_index in range(rounds):
        for item_type in AssessmentItemType:
            sequence += 1
            case_id = f"build08-draft-{sequence:03d}"
            if item_type == AssessmentItemType.BOW_TIE:
                page = medicine_pages[round_index % len(medicine_pages)]
            else:
                page = pages[non_bow_index % len(pages)]
                non_bow_index += 1

            call_ids: list[str] = []
            for stage in stages:
                call_sequence += 1
                call_id = f"{call_sequence:032x}"
                call_ids.append(call_id)
                prompt_tokens = 100
                output_tokens = 50
                cost = (
                    prompt_tokens * 300_000
                    + output_tokens * 2_500_000
                    + 999_999
                ) // 1_000_000
                calls.append(
                    ProviderCallReceipt(
                        qualification_run_id="automated-run",
                        call_id=call_id,
                        sequence=call_sequence,
                        case_id=case_id,
                        stage=stage,
                        prompt_version=f"{stage}-v1",
                        attempt_count=1,
                        prompt_token_count=prompt_tokens,
                        output_token_count=output_tokens,
                        total_token_count=prompt_tokens + output_tokens,
                        estimated_cost_microusd=cost,
                    )
                )

            drafts.append(
                DraftQualificationReceipt(
                    qualification_run_id="automated-run",
                    sequence=sequence,
                    case_id=case_id,
                    pilot_case=sequence <= 19,
                    generation_run_id=f"generation-{sequence}",
                    draft_id=sequence,
                    page_key=page.page_key,
                    stratum=page.stratum,
                    source_identity=page.source_identity,
                    content_sha256=page.content_sha256,
                    license=page.license,
                    item_type=item_type,
                    context_type=list(ItemContextType)[(sequence - 1) % 5],
                    provider_call_ids=call_ids,
                    schema_valid=True,
                    citation_valid=True,
                    source_hash_valid=True,
                    license_valid=True,
                    critique_executed=True,
                    revision_executed=True,
                    hint_ladder_executed=True,
                    hint_rung_count=3,
                    hint_leak_detected=False,
                    qti_valid=True,
                    qti_sha256=_sha(case_id),
                    engine_validation_passed=True,
                    unsafe_executable_source_detected=False,
                    detected_critical_defect=False,
                    advanced_flags_false=True,
                    adapt_publishing_disabled=True,
                    publication_attempt_count=0,
                )
            )
    return drafts, calls


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
