from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable

from app.schemas import AssessmentItemType

from .models import (
    CorpusManifest,
    DraftQualificationReceipt,
    EngineProbeReceipt,
    OutageBoundary,
    OutageReceipt,
    Publishability,
    ProviderBudgetState,
    ProviderCallReceipt,
    ReviewRecord,
    ReviewRole,
    SectionResult,
    SeedReceipt,
    ShadowMode,
    ShadowReceipt,
)


EXPECTED_DRAFT_STAGES = {
    "concept_extraction",
    "initial_draft",
    "critique",
    "revision",
    "hint_ladder",
}


def validate_outage_receipts(records: Iterable[OutageReceipt]) -> SectionResult:
    receipts = list(records)
    failures: list[str] = []
    run_ids = {receipt.run_id for receipt in receipts}
    if len(run_ids) != 1:
        failures.append("outage receipts must contain exactly one run_id")

    boundary_counts = Counter(receipt.boundary for receipt in receipts)
    for boundary in OutageBoundary:
        if boundary_counts[boundary] != 1:
            failures.append(
                f"{boundary.value}: requires exactly one independent outage receipt"
            )

    evidence_hashes = [receipt.evidence_sha256 for receipt in receipts]
    if len(evidence_hashes) != len(set(evidence_hashes)):
        failures.append("outage receipts must reference independent evidence artifacts")
    platform_states = {receipt.platform_state_sha256 for receipt in receipts}
    if len(platform_states) != 1:
        failures.append("outage receipts must share one sealed platform-state attestation")

    for receipt in receipts:
        failed_checks = [
            name
            for name, passed in {
                "safe outcome": receipt.retry_safe or receipt.terminal_safe,
                "no partial publication": receipt.no_partial_publication,
                "ADAPT core readiness": receipt.adapt_core_ready,
                "recovery": receipt.recovered,
                "secret redaction": receipt.secrets_redacted,
                "advanced flags disabled": receipt.advanced_flags_false,
            }.items()
            if not passed
        ]
        if failed_checks:
            failures.append(
                f"{receipt.boundary.value}: failed {', '.join(failed_checks)}"
            )

    return _result(
        "outages",
        failures,
        {
            "receipts": len(receipts),
            "boundaries": len(boundary_counts),
            "retryable": sum(receipt.retry_safe for receipt in receipts),
            "terminal": sum(receipt.terminal_safe for receipt in receipts),
        },
    )


def validate_engine_probe_receipts(
    records: Iterable[EngineProbeReceipt],
) -> SectionResult:
    receipts = list(records)
    failures: list[str] = []
    run_ids = {receipt.run_id for receipt in receipts}
    if len(run_ids) != 1:
        failures.append("engine probes must contain exactly one run_id")

    keys = [(receipt.item_id, receipt.seed) for receipt in receipts]
    if len(keys) != len(set(keys)):
        failures.append("engine probes contain duplicate item/seed pairs")

    expected_hosts = {
        AssessmentItemType.WEBWORK: "wwrenderer.libretexts.dev",
        AssessmentItemType.IMATHAS: "imathas.libretexts.dev",
    }
    by_item: dict[str, list[EngineProbeReceipt]] = defaultdict(list)
    for receipt in receipts:
        by_item[receipt.item_id].append(receipt)
        if receipt.endpoint_host != expected_hosts[receipt.item_type]:
            failures.append(f"{receipt.item_id}/{receipt.seed}: unapproved engine host")

    per_engine = Counter(
        item_receipts[0].item_type.value
        for item_receipts in by_item.values()
        if item_receipts
    )
    required_seeds = set(range(1, 101))
    for item_id, item_receipts in by_item.items():
        if {receipt.seed for receipt in item_receipts} != required_seeds:
            failures.append(f"{item_id}: requires exactly seeds 1 through 100")
        if len({receipt.item_type for receipt in item_receipts}) != 1:
            failures.append(f"{item_id}: item_type changed across probes")
        if len({receipt.source_sha256 for receipt in item_receipts}) != 1:
            failures.append(f"{item_id}: source hash changed across probes")
        if len({receipt.engine_image_sha256 for receipt in item_receipts}) != 1:
            failures.append(f"{item_id}: engine image changed across probes")
        if len(
            {
                receipt.network_isolation_attestation_sha256
                for receipt in item_receipts
            }
        ) != 1:
            failures.append(f"{item_id}: network attestation changed across probes")

    for engine in (AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS):
        if per_engine[engine.value] < 20:
            failures.append(f"{engine.value}: requires at least 20 unique items")

    failed = [
        receipt
        for receipt in receipts
        if not (
            receipt.deterministic
            and receipt.constraints_satisfied
            and receipt.rendered
            and receipt.warning_count == 0
            and receipt.error_count == 0
            and receipt.expected_answer_accepted
            and receipt.wrong_answer_rejected
            and receipt.expected_score == 1.0
            and receipt.wrong_score == 0.0
            and (
                receipt.item_type != AssessmentItemType.IMATHAS
                or (
                    receipt.object_idempotent_observed is True
                    and receipt.adapter_image_sha256 is not None
                    and receipt.engine_object_sha256 is not None
                )
            )
        )
    ]
    if failed:
        failures.append(f"{len(failed)} engine probes failed one or more checks")
    if len(receipts) < 4_000:
        failures.append("engine probe gate requires at least 4,000 executions")

    return _result(
        "engine_probes",
        failures,
        {
            "receipts": len(receipts),
            "unique_items": len(by_item),
            "webwork_items": per_engine[AssessmentItemType.WEBWORK.value],
            "imathas_items": per_engine[AssessmentItemType.IMATHAS.value],
            "failed_receipts": len(failed),
        },
    )


def validate_corpus_manifest(manifest: CorpusManifest) -> SectionResult:
    counts = Counter(page.stratum.value for page in manifest.pages)
    return SectionResult(
        name="corpus",
        passed=True,
        counts={"pages": len(manifest.pages), **dict(sorted(counts.items()))},
    )


def validate_provider_call_receipts(
    records: Iterable[ProviderCallReceipt],
    budget_state: ProviderBudgetState | None = None,
    *,
    require_settled: bool = False,
) -> SectionResult:
    calls = list(records)
    failures: list[str] = []
    run_ids = {call.qualification_run_id for call in calls}
    if len(run_ids) != 1:
        failures.append("provider call ledger must contain exactly one run_id")

    call_ids = [call.call_id for call in calls]
    if len(call_ids) != len(set(call_ids)):
        failures.append("provider call ledger contains duplicate call IDs")
    sequences = [call.sequence for call in calls]
    if sorted(sequences) != list(range(1, len(calls) + 1)):
        failures.append("provider call sequence must be contiguous from one")

    for call in calls:
        if call.thinking_level != "minimal":
            failures.append(f"{call.call_id}: Gemini thinking was not minimal")
        if not call.usage_complete:
            failures.append(f"{call.call_id}: provider usage metadata is incomplete")
            if call.estimated_cost_microusd != call.per_call_reserve_microusd:
                failures.append(
                    f"{call.call_id}: incomplete usage must charge the full reserve"
                )
        else:
            expected_cost = _estimated_gemini_cost_microusd(
                call.prompt_token_count,
                call.output_token_count,
            )
            if call.estimated_cost_microusd != expected_cost:
                failures.append(f"{call.call_id}: provider cost does not match usage")
        if call.estimated_cost_microusd > call.per_call_reserve_microusd:
            failures.append(f"{call.call_id}: provider cost exceeded its reserve")

    settled = sum(call.estimated_cost_microusd for call in calls)
    ceiling = calls[0].budget_ceiling_microusd if calls else 100_000_000
    open_reserve = 0
    if budget_state is not None:
        if run_ids and {budget_state.qualification_run_id} != run_ids:
            failures.append("provider budget state run ID does not match calls")
        if budget_state.settled_microusd != settled:
            failures.append("provider budget settled total does not match calls")
        call_ids_set = set(call_ids)
        if call_ids_set.intersection(budget_state.open_reservations):
            failures.append("settled provider calls still have open reservations")
        open_reserve = sum(
            reservation.reserved_microusd
            for reservation in budget_state.open_reservations.values()
        )
        if require_settled and budget_state.open_reservations:
            failures.append("provider qualification has open budget reservations")
        ceiling = budget_state.budget_ceiling_microusd
    spent = settled + open_reserve
    if spent > ceiling:
        failures.append("provider spending exceeded the USD 100 ceiling")
    return _result(
        "provider_budget",
        failures,
        {
            "calls": len(calls),
            "run_id": next(iter(run_ids)) if len(run_ids) == 1 else "mixed",
            "spent_microusd": spent,
            "spent_usd": round(spent / 1_000_000, 6),
            "settled_microusd": settled,
            "open_reservations": (
                len(budget_state.open_reservations) if budget_state is not None else 0
            ),
            "budget_ceiling_usd": ceiling / 1_000_000,
        },
    )


def validate_draft_qualification_receipts(
    records: Iterable[DraftQualificationReceipt],
    provider_calls: Iterable[ProviderCallReceipt],
    manifest: CorpusManifest,
    *,
    mode: str = "full",
) -> SectionResult:
    if mode not in {"pilot", "full"}:
        raise ValueError("draft qualification mode must be pilot or full")
    receipts = list(records)
    calls = list(provider_calls)
    failures: list[str] = []
    expected_count = 19 if mode == "pilot" else 380
    if len(receipts) != expected_count:
        failures.append(f"{mode} requires exactly {expected_count} draft receipts")

    run_ids = {receipt.qualification_run_id for receipt in receipts}
    call_run_ids = {call.qualification_run_id for call in calls}
    if len(run_ids) != 1:
        failures.append("draft receipts must contain exactly one run_id")
    if call_run_ids != run_ids:
        failures.append("draft and provider-call run IDs must match")

    expected_sequences = list(range(1, expected_count + 1))
    if sorted(receipt.sequence for receipt in receipts) != expected_sequences:
        failures.append(f"{mode} draft sequence is incomplete or duplicated")
    if len({receipt.draft_id for receipt in receipts}) != len(receipts):
        failures.append("draft receipts contain duplicate draft IDs")
    if len({receipt.case_id for receipt in receipts}) != len(receipts):
        failures.append("draft receipts contain duplicate case IDs")

    corpus_by_key = {page.page_key: page for page in manifest.pages}
    calls_by_id = {call.call_id: call for call in calls}
    referenced_call_ids: list[str] = []
    per_type = Counter(receipt.item_type for receipt in receipts)
    strata_by_type: dict[AssessmentItemType, set[str]] = defaultdict(set)
    for receipt in receipts:
        page = corpus_by_key.get(receipt.page_key)
        if page is None:
            failures.append(f"{receipt.case_id}: page is absent from the sealed corpus")
        elif any(
            (
                receipt.stratum != page.stratum,
                receipt.source_identity != page.source_identity,
                receipt.content_sha256 != page.content_sha256,
                receipt.license != page.license,
            )
        ):
            failures.append(f"{receipt.case_id}: source provenance does not match corpus")
        strata_by_type[receipt.item_type].add(receipt.stratum.value)

        selected_calls = [
            calls_by_id[call_id]
            for call_id in receipt.provider_call_ids
            if call_id in calls_by_id
        ]
        referenced_call_ids.extend(receipt.provider_call_ids)
        if len(selected_calls) != 5:
            failures.append(f"{receipt.case_id}: requires five recorded provider calls")
        elif (
            {call.stage for call in selected_calls} != EXPECTED_DRAFT_STAGES
            or any(call.case_id != receipt.case_id for call in selected_calls)
        ):
            failures.append(f"{receipt.case_id}: provider stage ledger is incomplete")

        checks = {
            "schema": receipt.schema_valid,
            "citation": receipt.citation_valid,
            "source hash": receipt.source_hash_valid,
            "license": receipt.license_valid,
            "critique": receipt.critique_executed,
            "revision": receipt.revision_executed,
            "hint ladder": receipt.hint_ladder_executed,
            "three hint rungs": receipt.hint_rung_count == 3,
            "hint leak": not receipt.hint_leak_detected,
            "QTI": receipt.qti_valid,
            "engine validation": receipt.engine_validation_passed,
            "safe executable source": not receipt.unsafe_executable_source_detected,
            "critical defect": not receipt.detected_critical_defect,
            "advanced flags": receipt.advanced_flags_false,
            "publishing disabled": receipt.adapt_publishing_disabled,
            "no publication": receipt.publication_attempt_count == 0,
        }
        for name, passed in checks.items():
            if not passed:
                failures.append(f"{receipt.case_id}: failed {name}")

        if (
            receipt.item_type == AssessmentItemType.BOW_TIE
            and receipt.stratum.value != "medicine_health"
        ):
            failures.append(f"{receipt.case_id}: bow-tie source is not medicine/health")

    if len(referenced_call_ids) != len(set(referenced_call_ids)):
        failures.append("provider calls cannot qualify more than one draft")

    if mode == "pilot":
        for item_type in AssessmentItemType:
            if per_type[item_type] != 1:
                failures.append(f"{item_type.value}: pilot requires exactly one draft")
    else:
        for item_type in AssessmentItemType:
            if per_type[item_type] != 20:
                failures.append(f"{item_type.value}: requires exactly 20 drafts")
            if item_type != AssessmentItemType.BOW_TIE and len(
                strata_by_type[item_type]
            ) < 3:
                failures.append(f"{item_type.value}: requires at least three strata")
        used_pages = {receipt.page_key for receipt in receipts}
        if used_pages != set(corpus_by_key):
            failures.append("full draft ledger must use all 48 sealed corpus pages")

    spent = sum(call.estimated_cost_microusd for call in calls)
    projected = math.ceil(spent * 380 / len(receipts)) if receipts else 0
    if mode == "pilot" and projected > 100_000_000:
        failures.append("pilot projects the 380-draft run above USD 100")
    if mode == "full" and spent > 100_000_000:
        failures.append("full draft run exceeded USD 100")

    return _result(
        "automated_draft_pilot" if mode == "pilot" else "automated_drafts",
        failures,
        {
            "drafts": len(receipts),
            "item_types": len(per_type),
            "used_pages": len({receipt.page_key for receipt in receipts}),
            "provider_calls": len(calls),
            "spent_microusd": spent,
            "projected_full_run_microusd": projected,
            "publication_attempts": sum(
                receipt.publication_attempt_count for receipt in receipts
            ),
        },
    )


def validate_review_ledger(records: Iterable[ReviewRecord]) -> SectionResult:
    ledger = list(records)
    failures: list[str] = []
    run_ids = {record.run_id for record in ledger}
    if len(run_ids) != 1:
        failures.append("review ledger must contain exactly one run_id")

    by_draft: dict[str, list[ReviewRecord]] = defaultdict(list)
    for record in ledger:
        by_draft[record.draft_id].append(record)

    primary_by_draft: dict[str, ReviewRecord] = {}
    for draft_id, draft_records in by_draft.items():
        primaries = [
            record for record in draft_records if record.role == ReviewRole.PRIMARY
        ]
        if len(primaries) != 1:
            failures.append(f"{draft_id}: requires exactly one primary review")
            continue
        primary_by_draft[draft_id] = primaries[0]

    per_type_counts: dict[str, int] = {}
    double_review_counts: dict[str, int] = {}
    kappa_by_type: dict[str, float] = {}
    for item_type in AssessmentItemType:
        primaries = [
            record
            for record in primary_by_draft.values()
            if record.item_type == item_type
        ]
        per_type_counts[item_type.value] = len(primaries)
        if len(primaries) < 20:
            failures.append(
                f"{item_type.value}: requires at least 20 primary reviewed drafts"
            )
        if not primaries:
            double_review_counts[item_type.value] = 0
            kappa_by_type[item_type.value] = 0.0
            continue

        checks = {
            "critical defect rate": not any(
                record.critical_defect for record in primaries
            ),
            "factual correctness": _ratio(primaries, "factual_correct") >= 0.95,
            "source support": _ratio(primaries, "source_supported") >= 0.95,
            "answer correctness": _ratio(primaries, "answer_correct") >= 0.95,
            "interaction quality": _ratio(primaries, "interaction_quality") >= 0.95,
            "Bloom alignment": _ratio(primaries, "bloom_aligned") >= 0.95,
            "difficulty alignment": _ratio(primaries, "difficulty_aligned") >= 0.95,
            "accessibility": _ratio(primaries, "accessible") == 1.0,
            "publishability": sum(
                record.publishability
                in {Publishability.NO_EDIT, Publishability.MINOR_EDIT}
                for record in primaries
            )
            / len(primaries)
            >= 0.90,
            "citation completeness": _ratio(primaries, "citation_complete") == 1.0,
            "license completeness": _ratio(primaries, "license_complete") == 1.0,
            "hint review completeness": _ratio(primaries, "hint_ladder_reviewed")
            == 1.0,
            "hint leak gate": _ratio(primaries, "hints_non_leaking") == 1.0,
            "hint progression gate": _ratio(primaries, "hints_progressive") == 1.0,
        }
        for name, passed in checks.items():
            if not passed:
                failures.append(f"{item_type.value}: failed {name}")

        paired: list[tuple[bool, bool]] = []
        for primary in primaries:
            independent = sorted(
                (
                    record
                    for record in by_draft[primary.draft_id]
                    if record.role == ReviewRole.INDEPENDENT
                    and record.reviewer_id != primary.reviewer_id
                ),
                key=lambda record: record.reviewer_id,
            )
            if independent:
                paired.append(
                    (not primary.critical_defect, not independent[0].critical_defect)
                )
        double_review_counts[item_type.value] = len(paired)
        required_pairs = math.ceil(len(primaries) * 0.20)
        if len(paired) < required_pairs:
            failures.append(
                f"{item_type.value}: requires {required_pairs} independent double reviews"
            )
        kappa = _cohen_kappa(paired)
        kappa_by_type[item_type.value] = round(kappa, 4)
        if kappa < 0.70:
            failures.append(f"{item_type.value}: critical-defect kappa is below 0.70")

        if item_type == AssessmentItemType.BOW_TIE:
            approved = [
                record
                for record in primaries
                if record.specialist_qualified
                and record.clinical_approved is True
                and not record.critical_defect
            ]
            if len(approved) < 20:
                failures.append(
                    "bow_tie: requires 20 specialist-qualified clinical approvals"
                )

    counts: dict[str, int | float | str | bool] = {
        "records": len(ledger),
        "primary_drafts": len(primary_by_draft),
        "run_id": next(iter(run_ids)) if len(run_ids) == 1 else "mixed",
        "minimum_per_type": min(per_type_counts.values(), default=0),
        "minimum_double_reviews_per_type": min(
            double_review_counts.values(), default=0
        ),
        "minimum_kappa": min(kappa_by_type.values(), default=0.0),
    }
    return _result("human_review", failures, counts)


def validate_seed_receipts(records: Iterable[SeedReceipt]) -> SectionResult:
    receipts = list(records)
    failures: list[str] = []
    run_ids = {receipt.run_id for receipt in receipts}
    if len(run_ids) != 1:
        failures.append("seed receipts must contain exactly one run_id")

    keys = [(receipt.item_id, receipt.seed) for receipt in receipts]
    if len(keys) != len(set(keys)):
        failures.append("seed receipts contain duplicate item/seed pairs")

    by_item: dict[str, list[SeedReceipt]] = defaultdict(list)
    for receipt in receipts:
        by_item[receipt.item_id].append(receipt)
    per_engine = Counter(
        item_receipts[0].item_type.value
        for item_receipts in by_item.values()
        if item_receipts
    )
    for engine in (AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS):
        if per_engine[engine.value] < 20:
            failures.append(f"{engine.value}: requires at least 20 unique items")

    required_seeds = set(range(1, 101))
    for item_id, item_receipts in by_item.items():
        seeds = {receipt.seed for receipt in item_receipts}
        if seeds != required_seeds:
            failures.append(f"{item_id}: requires exactly seeds 1 through 100")
        if len({receipt.item_type for receipt in item_receipts}) != 1:
            failures.append(f"{item_id}: item_type changed across receipts")
        if len({receipt.source_sha256 for receipt in item_receipts}) != 1:
            failures.append(f"{item_id}: compiled source hash is nondeterministic")
        if len({receipt.compiler_version for receipt in item_receipts}) != 1:
            failures.append(f"{item_id}: compiler version changed across receipts")

    failed_receipts = [
        receipt
        for receipt in receipts
        if not (
            receipt.deterministic
            and receipt.constraints_satisfied
            and receipt.rendered
            and receipt.warning_count == 0
            and receipt.error_count == 0
            and receipt.outbound_request_count == 0
            and receipt.expected_answer_accepted
            and receipt.wrong_answer_rejected
            and receipt.persisted_grade_match
            and receipt.object_idempotent
            and receipt.cross_owner_access_blocked
        )
    ]
    if failed_receipts:
        failures.append(
            f"{len(failed_receipts)} seed receipts failed one or more release checks"
        )
    if len(receipts) < 4_000:
        failures.append("seed gate requires at least 4,000 executions")

    return _result(
        "parameterized_seeds",
        failures,
        {
            "receipts": len(receipts),
            "unique_items": len(by_item),
            "webwork_items": per_engine[AssessmentItemType.WEBWORK.value],
            "imathas_items": per_engine[AssessmentItemType.IMATHAS.value],
            "failed_receipts": len(failed_receipts),
        },
    )


def compare_shadow_receipts(records: Iterable[ShadowReceipt]) -> SectionResult:
    receipts = list(records)
    failures: list[str] = []
    run_ids = {receipt.run_id for receipt in receipts}
    if len(run_ids) != 1:
        failures.append("shadow receipts must contain exactly one run_id")

    request_ids = [receipt.request_id for receipt in receipts]
    if len(request_ids) != len(set(request_ids)):
        failures.append("shadow receipts contain duplicate request IDs")
    event_ids = [event for receipt in receipts for event in receipt.event_ids]
    if len(event_ids) != len(set(event_ids)):
        failures.append("shadow receipts contain duplicate event IDs")
    if any(receipt.contains_pii_or_response_content for receipt in receipts):
        failures.append("shadow evidence contains PII or response content")

    by_case: dict[str, dict[ShadowMode, ShadowReceipt]] = defaultdict(dict)
    for receipt in receipts:
        if receipt.mode in by_case[receipt.case_id]:
            failures.append(
                f"{receipt.case_id}: duplicate {receipt.mode.value} receipt"
            )
        by_case[receipt.case_id][receipt.mode] = receipt

    expected_by_type: Counter[str] = Counter()
    observed_by_type: Counter[str] = Counter()
    parity_fields = (
        "student_dom_sha256",
        "student_api_sha256",
        "submission_sha256",
        "grade",
        "penalty",
        "shown_hint_state",
        "gradebook_sha256",
    )
    paired = 0
    for case_id, modes in by_case.items():
        if set(modes) != {ShadowMode.OFF, ShadowMode.OBSERVE}:
            failures.append(f"{case_id}: requires one off and one observe receipt")
            continue
        off = modes[ShadowMode.OFF]
        observe = modes[ShadowMode.OBSERVE]
        paired += 1
        if off.item_type != observe.item_type:
            failures.append(f"{case_id}: item type changed between modes")
        if observe.expected_event_count != 3:
            failures.append(f"{case_id}: observe mode must expect exactly three events")
        expected_by_type[observe.item_type.value] += observe.expected_event_count
        observed_by_type[observe.item_type.value] += observe.observed_event_count
        changed = [
            field
            for field in parity_fields
            if getattr(off, field) != getattr(observe, field)
        ]
        if changed:
            failures.append(
                f"{case_id}: off/observe parity changed {', '.join(changed)}"
            )

    for item_type in AssessmentItemType:
        expected = expected_by_type[item_type.value]
        if not expected:
            failures.append(f"{item_type.value}: missing shadow replay coverage")
            continue
        completeness = observed_by_type[item_type.value] / expected
        if completeness < 0.95:
            failures.append(f"{item_type.value}: telemetry completeness is below 95%")
    total_expected = sum(expected_by_type.values())
    total_observed = sum(observed_by_type.values())
    total_completeness = total_observed / total_expected if total_expected else 0.0
    if total_completeness < 0.95:
        failures.append("overall telemetry completeness is below 95%")

    return _result(
        "shadow_parity",
        failures,
        {
            "paired_cases": paired,
            "item_types": len(expected_by_type),
            "expected_events": total_expected,
            "observed_events": total_observed,
            "telemetry_completeness": round(total_completeness, 6),
        },
    )


def _ratio(records: list[ReviewRecord], field: str) -> float:
    return sum(bool(getattr(record, field)) for record in records) / len(records)


def _cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    if not pairs:
        return 0.0
    observed = sum(left == right for left, right in pairs) / len(pairs)
    left_true = sum(left for left, _ in pairs) / len(pairs)
    right_true = sum(right for _, right in pairs) / len(pairs)
    expected = left_true * right_true + (1 - left_true) * (1 - right_true)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def _estimated_gemini_cost_microusd(
    prompt_token_count: int,
    output_token_count: int,
) -> int:
    numerator = (
        prompt_token_count * 1_500_000 + output_token_count * 9_000_000
    )
    return math.ceil(numerator / 1_000_000)


def _result(
    name: str,
    failures: list[str],
    counts: dict[str, int | float | str | bool],
) -> SectionResult:
    return SectionResult(
        name=name,
        passed=not failures,
        counts=counts,
        failures=failures,
    )
