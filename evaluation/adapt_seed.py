from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from app.parameterized import compile_parameterized_item
from app.schemas import AssessmentItemType

from .fixtures import build_parameter_spec
from .models import (
    AdaptSeedAttestation,
    AdaptSeedItem,
    EngineProbeReceipt,
    SeedReceipt,
)


def build_adapt_seed_items(
    probes: Iterable[EngineProbeReceipt],
    *,
    imathas_ids: dict[str, int],
) -> list[AdaptSeedItem]:
    """Rebuild and bind the 40 immutable sources to their engine objects."""

    by_item: dict[str, EngineProbeReceipt] = {}
    for probe in probes:
        previous = by_item.setdefault(probe.item_id, probe)
        if (
            previous.run_id,
            previous.item_type,
            previous.compiler_version,
            previous.source_sha256,
            previous.engine_object_sha256,
        ) != (
            probe.run_id,
            probe.item_type,
            probe.compiler_version,
            probe.source_sha256,
            probe.engine_object_sha256,
        ):
            raise ValueError(f"{probe.item_id}: engine identity changed across seeds")

    items: list[AdaptSeedItem] = []
    for item_id, probe in sorted(by_item.items()):
        match = re.fullmatch(r"(webwork|imathas)-(\d{2})", item_id)
        if match is None:
            raise ValueError(f"{item_id}: invalid sealed item ID")
        index = int(match.group(2)) - 1
        compiled = compile_parameterized_item(
            build_parameter_spec(probe.item_type, index), validation_seeds=100
        )
        if compiled.source_sha256 != probe.source_sha256:
            raise ValueError(f"{item_id}: rebuilt source does not match engine probe")
        technology_id = None
        object_sha = None
        if probe.item_type == AssessmentItemType.IMATHAS:
            technology_id = imathas_ids.get(item_id)
            if technology_id is None:
                raise ValueError(f"{item_id}: missing IMathAS object mapping")
            object_sha = hashlib.sha256(
                f"imathas-question:{technology_id}".encode()
            ).hexdigest()
            if object_sha != probe.engine_object_sha256:
                raise ValueError(f"{item_id}: IMathAS object mapping does not match")
        items.append(
            AdaptSeedItem(
                run_id=probe.run_id,
                item_id=item_id,
                item_type=probe.item_type,
                compiler_version=probe.compiler_version,
                source_sha256=probe.source_sha256,
                engine_source=compiled.source,
                technology_id=technology_id,
                engine_object_sha256=object_sha,
            )
        )
    if len(items) != 40:
        raise ValueError(f"ADAPT seed item manifest requires 40 items, got {len(items)}")
    return items


def finalize_seed_receipts(
    probes: Iterable[EngineProbeReceipt],
    attestations: Iterable[AdaptSeedAttestation],
) -> list[SeedReceipt]:
    """Combine sealed engine truth with ADAPT clone persistence evidence.

    The two ledgers must have an exact one-to-one key and identity match.  This
    deliberately refuses to infer a passing receipt from a seed plan or from an
    item-level summary.
    """

    probe_records = list(probes)
    attestation_records = list(attestations)
    probe_by_key = _unique_by_key(probe_records, "engine probe")
    attestation_by_key = _unique_by_key(attestation_records, "ADAPT attestation")
    if set(probe_by_key) != set(attestation_by_key):
        missing = sorted(set(probe_by_key) - set(attestation_by_key))
        extra = sorted(set(attestation_by_key) - set(probe_by_key))
        raise ValueError(
            "engine probes and ADAPT attestations do not have identical keys "
            f"(missing={len(missing)}, extra={len(extra)})"
        )

    receipts: list[SeedReceipt] = []
    for key in sorted(probe_by_key):
        probe = probe_by_key[key]
        attestation = attestation_by_key[key]
        identity = (
            probe.run_id,
            probe.item_type,
            probe.compiler_version,
            probe.source_sha256,
        )
        attested_identity = (
            attestation.run_id,
            attestation.item_type,
            attestation.compiler_version,
            attestation.source_sha256,
        )
        if identity != attested_identity:
            raise ValueError(f"{probe.item_id}/{probe.seed}: ledger identity mismatch")
        if not _probe_passed(probe):
            raise ValueError(
                f"{probe.item_id}/{probe.seed}: engine probe did not pass"
            )
        if probe.expected_score is None or abs(probe.expected_score - 1.0) > 1e-9:
            raise ValueError(
                f"{probe.item_id}/{probe.seed}: expected engine score is not 1"
            )
        if abs(attestation.expected_score - probe.expected_score) > 1e-9:
            raise ValueError(
                f"{probe.item_id}/{probe.seed}: attested expected score changed"
            )
        if abs(attestation.persisted_score - probe.expected_score) > 1e-9:
            raise ValueError(
                f"{probe.item_id}/{probe.seed}: refreshed ADAPT grade did not match"
            )
        if not all(
            (
                attestation.grade_refreshed,
                attestation.object_idempotent,
                attestation.cross_owner_access_blocked,
                attestation.canary_network_internal,
                attestation.hint_mode_off,
            )
        ):
            raise ValueError(
                f"{probe.item_id}/{probe.seed}: ADAPT canary check did not pass"
            )
        receipts.append(
            SeedReceipt(
                run_id=probe.run_id,
                item_id=probe.item_id,
                item_type=probe.item_type,
                seed=probe.seed,
                compiler_version=probe.compiler_version,
                source_sha256=probe.source_sha256,
                deterministic=probe.deterministic,
                constraints_satisfied=probe.constraints_satisfied,
                rendered=probe.rendered,
                render_duration_ms=probe.render_duration_ms,
                warning_count=probe.warning_count,
                error_count=probe.error_count,
                outbound_request_count=0,
                expected_answer_accepted=probe.expected_answer_accepted,
                wrong_answer_rejected=probe.wrong_answer_rejected,
                persisted_grade_match=True,
                object_idempotent=True,
                cross_owner_access_blocked=True,
            )
        )
    return receipts


def _unique_by_key(records: list, label: str) -> dict[tuple[str, int], object]:
    indexed: dict[tuple[str, int], object] = {}
    for record in records:
        key = (record.item_id, record.seed)
        if key in indexed:
            raise ValueError(f"{label} ledger contains duplicate item/seed pairs")
        indexed[key] = record
    return indexed


def _probe_passed(receipt: EngineProbeReceipt) -> bool:
    return bool(
        receipt.deterministic
        and receipt.constraints_satisfied
        and receipt.rendered
        and receipt.warning_count == 0
        and receipt.error_count == 0
        and receipt.expected_answer_accepted
        and receipt.wrong_answer_rejected
        and receipt.network_isolation_attestation_sha256
    )
