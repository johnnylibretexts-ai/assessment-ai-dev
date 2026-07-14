from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable

from app.schemas import AssessmentItemType

from .models import (
    CorpusManifest,
    Publishability,
    ReviewRecord,
    ReviewRole,
    SectionResult,
    SeedReceipt,
    ShadowMode,
    ShadowReceipt,
)


def validate_corpus_manifest(manifest: CorpusManifest) -> SectionResult:
    counts = Counter(page.stratum.value for page in manifest.pages)
    return SectionResult(
        name="corpus",
        passed=True,
        counts={"pages": len(manifest.pages), **dict(sorted(counts.items()))},
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
