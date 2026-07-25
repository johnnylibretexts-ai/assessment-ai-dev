from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .config import Settings
from .db import (
    ComputationAttestationRead,
    ComputationEvidenceError,
    ComputationGateBinding,
    ComputationValidationRead,
    Draft,
    DraftRepository,
    computation_formula_adapter_promotion_sha256,
    draft_content_sha256,
    validate_computation_evidence,
)
from .schemas import AssessmentItemType


COMPUTATIONAL_ITEM_TYPES = frozenset(
    {
        AssessmentItemType.NUMERICAL,
        AssessmentItemType.WEBWORK,
        AssessmentItemType.IMATHAS,
    }
)
ATTESTABLE_STATUSES = frozenset({"partially_validated", "unsupported"})


@dataclass(frozen=True)
class QualifiedComputationRuntime:
    """Code-reviewed binding from qualification evidence to one immutable image."""

    image_reference: str
    container_digest: str
    runtime_manifest_sha256: str
    qualification_report_sha256: str
    families: frozenset[str]
    ucum_qualification_report_sha256: str | None = None


# Qualification promotion is intentionally a source-controlled action. The v0
# spike ships with no promoted image, so enforce mode fails closed until the
# offline corpus, security, UCUM, native-engine, and canary evidence is reviewed.
QUALIFIED_COMPUTATION_RUNTIMES: Mapping[str, QualifiedComputationRuntime] = (
    MappingProxyType({})
)
_QUALIFIED_FAMILIES = frozenset({"numeric", "algebraic", "unit"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def qualified_computation_runtime(
    *,
    image_reference: str,
    container_digest: str,
) -> QualifiedComputationRuntime | None:
    """Resolve one exact, structurally valid source-controlled promotion."""

    if (
        not isinstance(image_reference, str)
        or not isinstance(container_digest, str)
        or _IMAGE_DIGEST.fullmatch(container_digest) is None
        or not image_reference.endswith(f"@{container_digest}")
        or image_reference == f"@{container_digest}"
        or any(character.isspace() for character in image_reference)
    ):
        return None
    binding = QUALIFIED_COMPUTATION_RUNTIMES.get(container_digest)
    if type(binding) is not QualifiedComputationRuntime:
        return None
    if (
        type(binding.image_reference) is not str
        or type(binding.container_digest) is not str
        or type(binding.runtime_manifest_sha256) is not str
        or type(binding.qualification_report_sha256) is not str
        or binding.container_digest != container_digest
        or binding.image_reference != image_reference
        or _SHA256.fullmatch(binding.runtime_manifest_sha256) is None
        or _SHA256.fullmatch(binding.qualification_report_sha256) is None
        or type(binding.families) is not frozenset
        or not binding.families
        or not binding.families <= _QUALIFIED_FAMILIES
    ):
        return None
    ucum_hash = binding.ucum_qualification_report_sha256
    if ("unit" in binding.families) != (ucum_hash is not None):
        return None
    if ucum_hash is not None and (
        type(ucum_hash) is not str or _SHA256.fullmatch(ucum_hash) is None
    ):
        return None
    evidence_hashes = {
        binding.runtime_manifest_sha256,
        binding.qualification_report_sha256,
        *([ucum_hash] if ucum_hash is not None else []),
    }
    if len(evidence_hashes) != 2 + (ucum_hash is not None):
        return None
    return binding


def computation_runtime_promotion_declared(container_digest: str) -> bool:
    """Return only whether a digest key exists; never trust the entry payload."""

    return (
        isinstance(container_digest, str)
        and _IMAGE_DIGEST.fullmatch(container_digest) is not None
        and container_digest in QUALIFIED_COMPUTATION_RUNTIMES
    )


def computation_runtime_registry_state_sha256(container_digest: str) -> str:
    """Fingerprint the declared registry slot, including malformed entries.

    Cache versioning must distinguish an absent promotion from a declared but
    invalid one without treating any malformed field as trusted evidence.
    Values are reduced to bounded, type-tagged fingerprints before hashing.
    """

    declared = computation_runtime_promotion_declared(container_digest)
    entry = QUALIFIED_COMPUTATION_RUNTIMES.get(container_digest) if declared else None
    if type(entry) is QualifiedComputationRuntime:
        assert entry is not None
        payload: object = {
            "declared": True,
            "entry_type": "QualifiedComputationRuntime",
            "image_reference": _untrusted_registry_value(entry.image_reference),
            "container_digest": _untrusted_registry_value(entry.container_digest),
            "runtime_manifest_sha256": _untrusted_registry_value(
                entry.runtime_manifest_sha256
            ),
            "qualification_report_sha256": _untrusted_registry_value(
                entry.qualification_report_sha256
            ),
            "families": _untrusted_registry_value(entry.families),
            "ucum_qualification_report_sha256": _untrusted_registry_value(
                entry.ucum_qualification_report_sha256
            ),
        }
    elif declared:
        payload = {
            "declared": True,
            "entry_type": (
                f"{type(entry).__module__}.{type(entry).__qualname__}"[:200]
            ),
        }
    else:
        payload = {"declared": False}
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def _untrusted_registry_value(value: object) -> object:
    """Return a bounded canonical fingerprint for one untrusted registry field."""

    if value is None or type(value) is bool or type(value) is int:
        return {"type": type(value).__name__, "value": value}
    if type(value) is str:
        text = value
        bounded = (
            text
            if len(text) <= 512
            else f"{text[:256]}\N{HORIZONTAL ELLIPSIS}{text[-256:]}"
        )
        return {
            "type": "str",
            "length": len(text),
            "bounded_sha256": hashlib.sha256(
                bounded.encode("utf-8", errors="replace")
            ).hexdigest(),
        }
    if type(value) in {tuple, list}:
        items = [_untrusted_registry_value(item) for item in value[:16]]
        return {
            "type": type(value).__name__,
            "length": len(value),
            "items": items,
        }
    if type(value) in {frozenset, set}:
        # The only valid set field is the three-value family allowlist. Keep
        # malformed large sets bounded and deterministic across hash seeds.
        if len(value) <= 16:
            items = sorted(
                (_untrusted_registry_value(item) for item in value),
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            known = sorted(
                item
                for item in value
                if type(item) is str and item in _QUALIFIED_FAMILIES
            )
        else:
            items = []
            known = []
        return {
            "type": type(value).__name__,
            "length": len(value),
            "known_families": known,
            "unknown_count": len(value) - len(known),
            "items": items,
        }
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}"[:200],
    }


def computation_runtime_is_qualified(settings: Settings) -> bool:
    """Return whether every configured family is promoted for the exact image."""

    binding = configured_computation_runtime(settings)
    if binding is None:
        return False
    families = frozenset(settings.computation_families)
    if not families or not families <= binding.families:
        return False
    return (
        "unit" not in families or binding.ucum_qualification_report_sha256 is not None
    )


def computation_runtime_promotion_sha256(
    *,
    image_reference: str,
    container_digest: str,
) -> str | None:
    """Hash the exact current runtime promotion for generation identity."""

    binding = qualified_computation_runtime(
        image_reference=image_reference,
        container_digest=container_digest,
    )
    if binding is None:
        return None
    payload = {
        "image_reference": binding.image_reference,
        "container_digest": binding.container_digest,
        "runtime_manifest_sha256": binding.runtime_manifest_sha256,
        "qualification_report_sha256": binding.qualification_report_sha256,
        "families": sorted(binding.families),
        "ucum_qualification_report_sha256": (binding.ucum_qualification_report_sha256),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def configured_computation_runtime(
    settings: Settings,
) -> QualifiedComputationRuntime | None:
    """Resolve an exact immutable-reference registry binding, if promoted."""

    digest = settings.computation_container_digest
    if digest == "unavailable":
        return None
    return qualified_computation_runtime(
        image_reference=settings.computation_image_reference,
        container_digest=digest,
    )


class ComputationPolicyError(ValueError):
    """Raised when enforce mode rejects an approval or publication action."""


@dataclass(frozen=True)
class ComputationGateDecision:
    """One fail-closed computation-policy decision for an exact draft edit."""

    mode: Literal["off", "assist", "enforce"]
    scoped: bool
    allowed: bool
    validation_status: str | None
    reason_code: str
    message: str
    report_sha256: str | None = None
    validation_record_id: int | None = None
    evidence_sha256: str | None = None
    formula_adapter_promotion_sha256: str | None = None
    runtime_image_reference: str | None = None
    runtime_container_digest: str | None = None
    runtime_promotion_sha256: str | None = None
    attestation_sha256s: tuple[str, ...] = ()
    expected_edit_count: int | None = None
    expected_draft_sha256: str | None = None

    @property
    def enforced(self) -> bool:
        return self.mode == "enforce" and self.scoped

    @property
    def publication_evidence(self) -> dict[str, object] | None:
        """Return stable key/metadata material only when the gate is enforced."""

        if not self.enforced or self.report_sha256 is None:
            return None
        evidence: dict[str, object] = {
            "validation_status": self.validation_status,
            "report_sha256": self.report_sha256,
            "evidence_sha256": self.evidence_sha256,
            "attestation_sha256s": list(self.attestation_sha256s),
        }
        if self.formula_adapter_promotion_sha256 is not None:
            evidence["formula_adapter_promotion_sha256"] = (
                self.formula_adapter_promotion_sha256
            )
        if self.runtime_promotion_sha256 is not None:
            evidence["runtime_promotion_sha256"] = self.runtime_promotion_sha256
        return evidence

    def require_allowed(self, *, action: str) -> None:
        if self.allowed:
            return
        raise ComputationPolicyError(
            f"Computation evidence blocks {action}: {self.message}"
        )

    def atomic_binding(self, settings: Settings) -> ComputationGateBinding:
        """Build the transaction-bound expectation for an allowed decision."""

        if self.mode != "enforce" or not self.allowed:
            raise ComputationPolicyError(
                "Only an allowed enforce-mode decision has an atomic binding."
            )
        if self.expected_edit_count is None or self.expected_draft_sha256 is None:
            raise ComputationPolicyError(
                "The computation decision is not bound to a draft revision."
            )
        return ComputationGateBinding(
            expected_edit_count=self.expected_edit_count,
            expected_draft_sha256=self.expected_draft_sha256,
            scoped=self.scoped,
            validation_record_id=self.validation_record_id,
            evidence_sha256=self.evidence_sha256,
            validation_status=self.validation_status,
            report_sha256=self.report_sha256,
            formula_adapter_promotion_sha256=(self.formula_adapter_promotion_sha256),
            runtime_image_reference=self.runtime_image_reference,
            runtime_container_digest=self.runtime_container_digest,
            runtime_promotion_sha256=self.runtime_promotion_sha256,
            attestation_sha256s=self.attestation_sha256s,
            trusted_specialist_subjects=settings.computation_specialist_subjects,
        )


def evaluate_computation_gate(
    settings: Settings,
    repository: DraftRepository,
    draft: Draft,
) -> ComputationGateDecision:
    """Evaluate the v0 approval/publication gate without changing evidence.

    Off and assist return immediately so accepted BUILD-08 behavior performs no
    new evidence reads and its publication idempotency material remains exact.
    """

    mode = settings.computation_mode
    if mode != "enforce":
        return ComputationGateDecision(
            mode=mode,
            scoped=False,
            allowed=True,
            validation_status=None,
            reason_code="mode_not_enforced",
            message="Computation policy is not enforced.",
        )

    expected_hash = draft_content_sha256(draft.current_json)
    if not repository.draft_revision_matches(
        draft.id,
        edit_count=draft.edit_count,
        draft_sha256=expected_hash,
    ):
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=None,
            reason_code="stale_detached_draft",
            message=(
                "The draft changed while computation evidence was being checked. "
                "Reload the current revision."
            ),
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    current = repository.get_current_computation_validation(
        draft.id,
        expected_edit_count=draft.edit_count,
        draft_sha256=expected_hash,
    )
    has_computational_type = draft.current.item_type in COMPUTATIONAL_ITEM_TYPES
    if current is None:
        history = repository.list_computation_validations(draft.id)
        if not has_computational_type and not history:
            return ComputationGateDecision(
                mode=mode,
                scoped=False,
                allowed=True,
                validation_status=None,
                reason_code="not_applicable",
                message="This draft has no computation evidence or computational type.",
                expected_edit_count=draft.edit_count,
                expected_draft_sha256=expected_hash,
            )
        if history:
            return _blocked_missing(
                mode,
                reason_code="stale_evidence",
                message=(
                    "The previous computation report is stale for the current edit. "
                    "Revalidate this exact draft."
                ),
                expected_edit_count=draft.edit_count,
                expected_draft_sha256=expected_hash,
            )
        return _blocked_missing(
            mode,
            reason_code="legacy_without_blueprint",
            message=(
                "This pre-v0 computational draft has no typed blueprint or current "
                "validation report. Revalidate it before approval or publication."
            ),
            validation_status="unsupported",
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )

    try:
        typed_report = validate_computation_evidence(
            current,
            require_authorizable=False,
        )
    except ComputationEvidenceError:
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=current.status.strip().casefold(),
            reason_code="invalid_computation_evidence",
            message=(
                "The current computation report failed typed integrity checks. "
                "Revalidate this exact draft."
            ),
            report_sha256=current.report_sha256,
            validation_record_id=current.id,
            evidence_sha256=current.evidence_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )

    status = typed_report.status.value
    if status == "not_applicable":
        typed_history = _has_typed_computation_history(
            repository.list_computation_validations(draft.id)
        )
        if has_computational_type or typed_history:
            return ComputationGateDecision(
                mode=mode,
                scoped=True,
                allowed=False,
                validation_status=status,
                reason_code="invalid_computation_scope",
                message=(
                    "A computational item cannot use a not_applicable report. "
                    "Revalidate this exact draft."
                ),
                report_sha256=current.report_sha256,
                expected_edit_count=draft.edit_count,
                expected_draft_sha256=expected_hash,
            )
        return ComputationGateDecision(
            mode=mode,
            scoped=False,
            allowed=True,
            validation_status=status,
            reason_code="not_applicable",
            message="No explicit computation profile applies to this draft.",
            report_sha256=current.report_sha256,
            validation_record_id=current.id,
            evidence_sha256=current.evidence_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    if status == "validation_failed":
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=status,
            reason_code="validation_failed",
            message=(
                "The current computation validation failed. Attestations cannot "
                "override a failed report."
            ),
            report_sha256=current.report_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    if typed_report.reason == "legacy_without_blueprint":
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=status,
            reason_code="legacy_without_blueprint",
            message=(
                "This pre-v0 computational draft has no typed blueprint. "
                "Explicit revalidation is required; an attestation cannot "
                "replace missing typed evidence."
            ),
            report_sha256=current.report_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    try:
        validate_computation_evidence(current, require_authorizable=True)
    except ComputationEvidenceError:
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=status,
            reason_code="invalid_computation_evidence",
            message=(
                "The current computation report is not qualified to authorize "
                "enforce mode. Revalidate this exact draft."
            ),
            report_sha256=current.report_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    qualification_failure = _runtime_qualification_failure(
        settings,
        current,
    )
    if qualification_failure is not None:
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=status,
            reason_code=qualification_failure[0],
            message=qualification_failure[1],
            report_sha256=current.report_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    runtime_promotion_sha256 = computation_runtime_promotion_sha256(
        image_reference=settings.computation_image_reference,
        container_digest=settings.computation_container_digest,
    )
    if runtime_promotion_sha256 is None:
        return ComputationGateDecision(
            mode=mode,
            scoped=True,
            allowed=False,
            validation_status=status,
            reason_code="runtime_not_qualified",
            message="The current runtime promotion identity is unavailable.",
            report_sha256=current.report_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    if status == "validated":
        return _allowed_with_evidence(
            mode,
            current,
            reason_code="validated",
            message="Every required computation check passed.",
            runtime_image_reference=settings.computation_image_reference,
            runtime_container_digest=settings.computation_container_digest,
            runtime_promotion_sha256=runtime_promotion_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    if status in ATTESTABLE_STATUSES:
        current_attestations = repository.list_current_computation_attestations(
            draft.id,
            report_sha256=current.report_sha256,
        )
        trusted = _trusted_attestations(settings, current_attestations)
        if not trusted:
            return ComputationGateDecision(
                mode=mode,
                scoped=True,
                allowed=False,
                validation_status=status,
                reason_code="specialist_attestation_required",
                message=(
                    f"The current report is {status} and requires an attestation "
                    "from a currently trusted computation specialist."
                ),
                report_sha256=current.report_sha256,
                expected_edit_count=draft.edit_count,
                expected_draft_sha256=expected_hash,
            )
        return _allowed_with_evidence(
            mode,
            current,
            reason_code="specialist_attested",
            message=(
                f"The current {status} report has a trusted specialist attestation."
            ),
            attestations=trusted,
            runtime_image_reference=settings.computation_image_reference,
            runtime_container_digest=settings.computation_container_digest,
            runtime_promotion_sha256=runtime_promotion_sha256,
            expected_edit_count=draft.edit_count,
            expected_draft_sha256=expected_hash,
        )
    return ComputationGateDecision(
        mode=mode,
        scoped=True,
        allowed=False,
        validation_status=status,
        reason_code="invalid_validation_status",
        message=(
            "The current computation report has a status that is not eligible "
            "under enforce mode."
        ),
        report_sha256=current.report_sha256,
        expected_edit_count=draft.edit_count,
        expected_draft_sha256=expected_hash,
    )


def _has_typed_computation_history(
    records: tuple[ComputationValidationRead, ...],
) -> bool:
    for record in records:
        try:
            payload = json.loads(record.blueprint_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("profile"), dict):
            return True
    return False


def _runtime_qualification_failure(
    settings: Settings,
    record: ComputationValidationRead,
) -> tuple[str, str] | None:
    try:
        blueprint = json.loads(record.blueprint_json)
        family = str(blueprint["profile"]["family"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return (
            "runtime_blueprint_invalid",
            "The qualified runtime check could not resolve the typed family.",
        )
    enabled_families = frozenset(settings.computation_families)
    if family not in enabled_families:
        return (
            "family_not_enabled",
            f"The {family} family is not enabled by the current strict allowlist.",
        )
    digest = settings.computation_container_digest
    binding = configured_computation_runtime(settings)
    if binding is None:
        return (
            "runtime_not_qualified",
            "The configured immutable computation image reference has no exact "
            "code-reviewed qualification binding. Keep computation enforcement "
            "off until qualification is promoted.",
        )
    if (
        binding.container_digest != digest
        or binding.image_reference != settings.computation_image_reference
        or record.container_digest != digest
    ):
        return (
            "runtime_digest_mismatch",
            "The current report is not bound to the configured qualified "
            "computation image.",
        )
    identity_failure = _runtime_identity_evidence_failure(record, binding)
    if identity_failure is not None:
        return identity_failure
    if family not in binding.families:
        return (
            "family_not_qualified",
            f"The {family} family is not qualified for the configured image.",
        )
    family_evidence_failure = _runtime_family_evidence_failure(record, family)
    if family_evidence_failure is not None:
        return family_evidence_failure
    if family == "unit" and binding.ucum_qualification_report_sha256 is None:
        return (
            "ucum_profile_not_qualified",
            "The configured image has no bound passed UCUM-subset qualification.",
        )
    if family == "unit":
        ucum_identity_failure = _ucum_identity_evidence_failure(record, binding)
        if ucum_identity_failure is not None:
            return ucum_identity_failure
    return None


def _runtime_family_evidence_failure(
    record: ComputationValidationRead,
    family: str,
) -> tuple[str, str] | None:
    try:
        report = json.loads(record.report_json)
        checks = report["checks"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        checks = None
    matching = (
        [
            check
            for check in checks
            if isinstance(check, dict)
            and check.get("code") == "runtime_family_qualification"
        ]
        if isinstance(checks, list)
        else []
    )
    if len(matching) != 1:
        return (
            "runtime_family_evidence_missing",
            "The computation report does not contain exactly one runtime-family "
            "qualification check.",
        )
    check = matching[0]
    details = check.get("details")
    if (
        check.get("status") != "passed"
        or not isinstance(details, dict)
        or details.get("family") != family
    ):
        return (
            "runtime_family_evidence_mismatch",
            "The computation report is not bound to the currently requested "
            f"{family} runtime qualification.",
        )
    return None


def _runtime_identity_evidence_failure(
    record: ComputationValidationRead,
    binding: QualifiedComputationRuntime,
) -> tuple[str, str] | None:
    try:
        report = json.loads(record.report_json)
        checks = report["checks"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return (
            "runtime_identity_evidence_missing",
            "The computation report has no valid runtime identity evidence.",
        )
    if not isinstance(checks, list):
        return (
            "runtime_identity_evidence_missing",
            "The computation report has no valid runtime identity evidence.",
        )
    matching = [
        check
        for check in checks
        if isinstance(check, dict) and check.get("code") == "runtime_identity"
    ]
    if len(matching) != 1:
        return (
            "runtime_identity_evidence_missing",
            "The computation report does not contain exactly one runtime identity "
            "check.",
        )
    check = matching[0]
    details = check.get("details")
    if (
        check.get("status") != "passed"
        or not isinstance(details, dict)
        or details.get("image_reference") != binding.image_reference
        or details.get("container_digest") != binding.container_digest
        or details.get("runtime_manifest_sha256") != binding.runtime_manifest_sha256
        or details.get("qualification_report_sha256")
        != binding.qualification_report_sha256
    ):
        return (
            "runtime_identity_mismatch",
            "The computation report is not bound to the configured qualified "
            "runtime manifest and immutable image reference.",
        )
    return None


def _ucum_identity_evidence_failure(
    record: ComputationValidationRead,
    binding: QualifiedComputationRuntime,
) -> tuple[str, str] | None:
    try:
        report = json.loads(record.report_json)
        checks = report["checks"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        checks = None
    matching = (
        [
            check
            for check in checks
            if isinstance(check, dict)
            and check.get("code") == "ucum_subset_qualification"
        ]
        if isinstance(checks, list)
        else []
    )
    check = matching[0] if len(matching) == 1 else None
    details = check.get("details") if isinstance(check, dict) else None
    if (
        check is None
        or check.get("status") != "passed"
        or not isinstance(details, dict)
        or details.get("qualification_report_sha256")
        != binding.ucum_qualification_report_sha256
    ):
        return (
            "ucum_profile_identity_mismatch",
            "The unit report is not bound to the current reviewed UCUM-subset "
            "qualification report.",
        )
    return None


def require_computation_gate(
    settings: Settings,
    repository: DraftRepository,
    draft: Draft,
    *,
    action: str,
) -> ComputationGateDecision:
    """Return an eligible decision or raise a reusable policy error."""

    decision = evaluate_computation_gate(settings, repository, draft)
    decision.require_allowed(action=action)
    return decision


def _blocked_missing(
    mode: Literal["enforce"],
    *,
    reason_code: str,
    message: str,
    validation_status: str | None = None,
    expected_edit_count: int | None = None,
    expected_draft_sha256: str | None = None,
) -> ComputationGateDecision:
    return ComputationGateDecision(
        mode=mode,
        scoped=True,
        allowed=False,
        validation_status=validation_status,
        reason_code=reason_code,
        message=message,
        expected_edit_count=expected_edit_count,
        expected_draft_sha256=expected_draft_sha256,
    )


def _allowed_with_evidence(
    mode: Literal["enforce"],
    validation: ComputationValidationRead,
    *,
    reason_code: str,
    message: str,
    attestations: tuple[ComputationAttestationRead, ...] = (),
    runtime_image_reference: str,
    runtime_container_digest: str,
    runtime_promotion_sha256: str,
    expected_edit_count: int,
    expected_draft_sha256: str,
) -> ComputationGateDecision:
    return ComputationGateDecision(
        mode=mode,
        scoped=True,
        allowed=True,
        validation_status=validation.status.strip().casefold(),
        reason_code=reason_code,
        message=message,
        report_sha256=validation.report_sha256,
        validation_record_id=validation.id,
        evidence_sha256=validation.evidence_sha256,
        formula_adapter_promotion_sha256=(
            computation_formula_adapter_promotion_sha256(validation)
        ),
        runtime_image_reference=runtime_image_reference,
        runtime_container_digest=runtime_container_digest,
        runtime_promotion_sha256=runtime_promotion_sha256,
        attestation_sha256s=tuple(
            sorted(item.attestation_sha256 for item in attestations)
        ),
        expected_edit_count=expected_edit_count,
        expected_draft_sha256=expected_draft_sha256,
    )


def _trusted_attestations(
    settings: Settings,
    attestations: tuple[ComputationAttestationRead, ...],
) -> tuple[ComputationAttestationRead, ...]:
    trusted_subjects = frozenset(settings.computation_specialist_subjects)
    return tuple(
        item
        for item in attestations
        if item.is_current and item.specialist_identity in trusted_subjects
    )
