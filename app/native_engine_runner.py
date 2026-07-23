from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, TypeVar

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from .computation import FormulaAdapterPromotionEvidence


NATIVE_RUNNER_SCHEMA_VERSION = "assessment-computation-native-runner-v0"
NATIVE_RUNNER_RECEIPT_VERSION = "assessment-computation-native-receipt-v0"
NATIVE_RUNNER_BASE_URL = "http://assessment-native-engine-runner"
MAX_NATIVE_RUNNER_REQUEST_BYTES = 64 * 1024
MAX_NATIVE_RUNNER_RESPONSE_BYTES = 256 * 1024
NATIVE_RUNNER_TIMEOUT_SECONDS = 30.0

_CONNECT_TIMEOUT_SECONDS = 0.5
_WRITE_TIMEOUT_SECONDS = 2.0
_POOL_TIMEOUT_SECONDS = 0.5
_RESPONSE_GRACE_SECONDS = 1.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUNNER_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_REVISION = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$")

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class NativeRunnerError(RuntimeError):
    """Base class for sanitized native-runner failures."""

    code = "native_runner_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NativeRunnerUnavailableError(NativeRunnerError):
    code = "native_runner_unavailable"


class NativeRunnerTimeoutError(NativeRunnerUnavailableError):
    code = "native_runner_timeout"


class NativeRunnerUnqualifiedError(NativeRunnerError):
    code = "native_runner_unqualified"


class NativeRunnerRejectedError(NativeRunnerError):
    code = "native_runner_rejected"


class NativeRunnerProtocolError(NativeRunnerError):
    code = "native_runner_invalid_response"


class NativeRunnerRequestTooLargeError(NativeRunnerRejectedError):
    code = "native_runner_request_too_large"


class NativeRunnerResponseTooLargeError(NativeRunnerProtocolError):
    code = "native_runner_response_too_large"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class NativeSeedObservation(StrictModel):
    """One engine-observed, self-hashed seed execution.

    Submission text is deliberately omitted.  The runner records only hashes;
    the application recomputes the canonical submissions from the typed AST and
    these bounded integer observations before trusting the receipt.
    """

    seed: StrictInt = Field(ge=0, le=0x7FFFFFFF)
    observed_variables: dict[str, StrictInt] = Field(max_length=12)
    correct_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alternate_correct_submission_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    wrong_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters_satisfied: Literal[True]
    constraints_satisfied: Literal[True]
    correct_answer_accepted: Literal[True]
    alternate_correct_answer_accepted: bool | None = None
    wrong_answer_rejected: Literal[True]
    rendered: Literal[True]
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings_count: Literal[0]
    errors_count: Literal[0]
    outbound_request_count: Literal[0]
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_observation(self) -> NativeSeedObservation:
        if self.render_sha256 != self.repeat_render_sha256:
            raise ValueError("native observation render was not stable")
        submission_hashes = {
            self.correct_submission_sha256,
            self.wrong_submission_sha256,
        }
        if self.alternate_correct_submission_sha256 is not None:
            submission_hashes.add(self.alternate_correct_submission_sha256)
        expected_count = (
            3 if self.alternate_correct_submission_sha256 is not None else 2
        )
        if len(submission_hashes) != expected_count:
            raise ValueError("native observation submissions must be distinct")
        if _hash_without(self, "observation_sha256") != self.observation_sha256:
            raise ValueError("native observation hash does not match its payload")
        return self


class NativeEngineRunnerRequest(StrictModel):
    """One closed, server-built request for an exact compiled draft artifact."""

    schema_version: Literal["assessment-computation-native-runner-v0"] = (
        NATIVE_RUNNER_SCHEMA_VERSION
    )
    runner_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    engine: Literal["webwork", "imathas"]
    compiler_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    answer_kind: Literal["numeric", "formula"]
    native_grader: str = Field(min_length=1, max_length=100)
    formula_adapter_promotion: FormulaAdapterPromotionEvidence | None = None
    source: str = Field(min_length=1, max_length=100_000)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blueprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seeds: list[StrictInt] = Field(min_length=25, max_length=25)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bindings(self) -> NativeEngineRunnerRequest:
        if hashlib.sha256(self.source.encode("utf-8")).hexdigest() != (
            self.source_sha256
        ):
            raise ValueError("native runner source does not match source_sha256")
        if len(set(self.seeds)) != 25:
            raise ValueError("native runner requires 25 unique deterministic seeds")
        if self.native_grader != expected_native_grader(self.engine, self.answer_kind):
            raise ValueError("native runner grader is not the qualified grader")
        if self.answer_kind == "formula":
            promotion = self.formula_adapter_promotion
            if (
                promotion is None
                or promotion.engine != self.engine
                or promotion.compiler_version != self.compiler_version
                or promotion.native_grader != self.native_grader
            ):
                raise ValueError(
                    "formula request requires its exact adapter promotion identity"
                )
        elif self.formula_adapter_promotion is not None:
            raise ValueError("numeric request cannot claim a formula adapter promotion")
        if _hash_without(self, "request_sha256") != self.request_sha256:
            raise ValueError("native runner request hash does not match its payload")
        return self


class NativeEngineRunnerReceipt(StrictModel):
    """Aggregate receipt for all 25 native executions of one draft artifact."""

    schema_version: Literal["assessment-computation-native-receipt-v0"] = (
        NATIVE_RUNNER_RECEIPT_VERSION
    )
    runner_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    runner_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    runner_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    qualification_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: Literal["webwork", "imathas"]
    compiler_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$",
    )
    answer_kind: Literal["numeric", "formula"]
    native_grader: str = Field(min_length=1, max_length=100)
    formula_adapter_promotion: FormulaAdapterPromotionEvidence | None = None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blueprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seeds: list[StrictInt] = Field(min_length=25, max_length=25)
    seed_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seeds_validated: Literal[25]
    observations: list[NativeSeedObservation] = Field(
        min_length=25,
        max_length=25,
    )
    engine_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    network_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    correct_answer_accepted: Literal[True]
    wrong_answer_rejected: Literal[True]
    rendered: Literal[True]
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings_count: Literal[0]
    errors_count: Literal[0]
    outbound_request_count: Literal[0]
    passed: Literal[True]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> NativeEngineRunnerReceipt:
        if len(set(self.seeds)) != 25:
            raise ValueError("native receipt requires 25 unique deterministic seeds")
        if self.seed_plan_sha256 != seed_plan_sha256(self.seeds):
            raise ValueError("native receipt seed-plan hash does not match its seeds")
        if self.seeds != [observation.seed for observation in self.observations]:
            raise ValueError(
                "native receipt observations do not match the exact seed plan"
            )
        if self.seed_receipts_sha256 != seed_observations_sha256(self.observations):
            raise ValueError(
                "native receipt aggregate hash does not match its observations"
            )
        if self.answer_kind == "formula":
            if any(
                observation.alternate_correct_submission_sha256 is None
                or observation.alternate_correct_answer_accepted is not True
                for observation in self.observations
            ):
                raise ValueError(
                    "formula receipts require a distinct accepted alternate form"
                )
        elif any(
            observation.alternate_correct_submission_sha256 is not None
            or observation.alternate_correct_answer_accepted is not None
            for observation in self.observations
        ):
            raise ValueError("numeric receipts cannot claim alternate formula forms")
        aggregate_render = _canonical_sha256(
            [observation.render_sha256 for observation in self.observations]
        )
        aggregate_repeat = _canonical_sha256(
            [observation.repeat_render_sha256 for observation in self.observations]
        )
        if (
            self.render_sha256 != aggregate_render
            or self.repeat_render_sha256 != aggregate_repeat
        ):
            raise ValueError("native receipt render hashes are not observation-bound")
        if self.render_sha256 != self.repeat_render_sha256:
            raise ValueError("native receipt render was not stable")
        if self.native_grader != expected_native_grader(self.engine, self.answer_kind):
            raise ValueError("native receipt grader is not the qualified grader")
        if self.answer_kind == "formula":
            promotion = self.formula_adapter_promotion
            if (
                promotion is None
                or promotion.engine != self.engine
                or promotion.compiler_version != self.compiler_version
                or promotion.native_grader != self.native_grader
                or promotion.engine_image_digest != self.engine_image_digest
                or promotion.adapter_image_digest != self.adapter_image_digest
            ):
                raise ValueError(
                    "formula receipt requires its exact adapter promotion identity"
                )
        elif self.formula_adapter_promotion is not None:
            raise ValueError("numeric receipt cannot claim a formula adapter promotion")
        if self.engine == "webwork" and self.adapter_image_digest is not None:
            raise ValueError("WeBWorK receipt cannot claim an adapter image")
        if self.engine == "imathas" and self.adapter_image_digest is None:
            raise ValueError("IMathAS receipt requires an adapter image")
        if _hash_without(self, "receipt_sha256") != self.receipt_sha256:
            raise ValueError("native receipt hash does not match its payload")
        return self


@dataclass(frozen=True)
class QualifiedNativeEngineRunner:
    """Reviewed promotion for one exact runner, engine, compiler, and grader."""

    runner_id: str
    runner_version: str
    runner_manifest_sha256: str
    runner_image_digest: str
    engine: Literal["webwork", "imathas"]
    compiler_version: str
    answer_kind: Literal["numeric", "formula"]
    native_grader: str
    engine_image_digest: str
    adapter_image_digest: str | None
    network_attestation_sha256: str
    qualification_report_sha256: str
    promotion_approval_sha256: str


# A native runner cannot promote itself. Qualification requires a reviewed,
# source-controlled entry for the exact runner/engine/compiler/grader identity.
# The v0 spike intentionally ships with no promoted runner.
QUALIFIED_NATIVE_ENGINE_RUNNERS: Mapping[
    tuple[str, str, str, str], QualifiedNativeEngineRunner
] = MappingProxyType({})


class NativeEngineRunner(Protocol):
    @property
    def runner_id(self) -> str: ...

    def qualification_for(
        self,
        *,
        engine: str,
        compiler_version: str,
        answer_kind: str,
    ) -> QualifiedNativeEngineRunner | None: ...

    async def validate(
        self,
        request: NativeEngineRunnerRequest,
    ) -> NativeEngineRunnerReceipt: ...

    async def aclose(self) -> None: ...


class UnixSocketNativeEngineRunner:
    """Strict, bounded client for a local native-engine runner Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        runner_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        resolved_socket_path = Path(socket_path)
        socket_path_text = str(resolved_socket_path)
        if (
            not resolved_socket_path.is_absolute()
            or resolved_socket_path.suffix != ".sock"
            or ".." in resolved_socket_path.parts
            or "://" in socket_path_text
        ):
            raise ValueError(
                "native runner socket_path must be an absolute .sock path "
                "without URLs or parent traversal"
            )
        if _RUNNER_ID.fullmatch(runner_id) is None:
            raise ValueError("native runner id is invalid")
        if transport is None:
            transport = httpx.AsyncHTTPTransport(uds=socket_path_text, retries=0)
        self._runner_id = runner_id
        self._client = httpx.AsyncClient(
            base_url=NATIVE_RUNNER_BASE_URL,
            transport=transport,
            follow_redirects=False,
            timeout=_timeout(NATIVE_RUNNER_TIMEOUT_SECONDS),
            headers={
                "Accept": "application/json",
                "User-Agent": "LibreTexts-Assessment-AI/native-runner-v0",
            },
        )

    @property
    def runner_id(self) -> str:
        return self._runner_id

    async def __aenter__(self) -> UnixSocketNativeEngineRunner:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def qualification_for(
        self,
        *,
        engine: str,
        compiler_version: str,
        answer_kind: str,
    ) -> QualifiedNativeEngineRunner | None:
        return qualified_native_engine_runner(
            runner_id=self.runner_id,
            engine=engine,
            compiler_version=compiler_version,
            answer_kind=answer_kind,
        )

    async def validate(
        self,
        request: NativeEngineRunnerRequest,
    ) -> NativeEngineRunnerReceipt:
        if request.runner_id != self.runner_id:
            raise NativeRunnerRejectedError(
                "The native runner request identity is invalid."
            )
        promotion = self.qualification_for(
            engine=request.engine,
            compiler_version=request.compiler_version,
            answer_kind=request.answer_kind,
        )
        if promotion is None:
            raise NativeRunnerUnqualifiedError(
                "The native runner is not qualified for this artifact."
            )
        body = request.model_dump_json().encode("utf-8")
        if len(body) > MAX_NATIVE_RUNNER_REQUEST_BYTES:
            raise NativeRunnerRequestTooLargeError(
                "The native runner request exceeds the permitted size."
            )
        try:
            async with self._client.stream(
                "POST",
                "/v0/validate",
                content=body,
                headers={"Content-Type": "application/json"},
                timeout=_timeout(
                    NATIVE_RUNNER_TIMEOUT_SECONDS + _RESPONSE_GRACE_SECONDS
                ),
            ) as response:
                _raise_for_status(response.status_code)
                _require_json(response)
                response_body = await _read_bounded_response(response)
        except NativeRunnerError:
            raise
        except httpx.TimeoutException:
            raise NativeRunnerTimeoutError(
                "The native engine runner timed out."
            ) from None
        except httpx.RequestError:
            raise NativeRunnerUnavailableError(
                "The native engine runner is unavailable."
            ) from None
        try:
            receipt = NativeEngineRunnerReceipt.model_validate_json(response_body)
        except (ValidationError, ValueError):
            raise NativeRunnerProtocolError(
                "The native engine runner returned an invalid receipt."
            ) from None
        _verify_receipt(request, receipt, promotion)
        return receipt


def expected_native_grader(engine: str, answer_kind: str) -> str:
    graders = {
        ("webwork", "numeric"): "MathObjects::Real::cmp",
        ("webwork", "formula"): "MathObjects::Formula::cmp",
        ("imathas", "numeric"): "native_calculated_v0",
        ("imathas", "formula"): "native_symbolic_equivalence_v0",
    }
    try:
        return graders[(engine, answer_kind)]
    except KeyError:
        raise ValueError("native runner engine or answer kind is unsupported") from None


def qualified_native_engine_runner(
    *,
    runner_id: str,
    engine: str,
    compiler_version: str,
    answer_kind: str,
) -> QualifiedNativeEngineRunner | None:
    key = (runner_id, engine, compiler_version, answer_kind)
    promotion = QUALIFIED_NATIVE_ENGINE_RUNNERS.get(key)
    if promotion is None:
        return None
    expected_grader = expected_native_grader(engine, answer_kind)
    hash_values = (
        promotion.runner_manifest_sha256,
        promotion.network_attestation_sha256,
        promotion.qualification_report_sha256,
        promotion.promotion_approval_sha256,
    )
    if (
        key
        != (
            promotion.runner_id,
            promotion.engine,
            promotion.compiler_version,
            promotion.answer_kind,
        )
        or _RUNNER_ID.fullmatch(promotion.runner_id) is None
        or _REVISION.fullmatch(promotion.runner_version) is None
        or _REVISION.fullmatch(promotion.compiler_version) is None
        or promotion.native_grader != expected_grader
        or any(_SHA256.fullmatch(value) is None for value in hash_values)
        or len(set(hash_values)) != len(hash_values)
        or _DIGEST.fullmatch(promotion.runner_image_digest) is None
        or _DIGEST.fullmatch(promotion.engine_image_digest) is None
    ):
        return None
    if engine == "webwork" and promotion.adapter_image_digest is not None:
        return None
    if engine == "imathas" and (
        promotion.adapter_image_digest is None
        or _DIGEST.fullmatch(promotion.adapter_image_digest) is None
    ):
        return None
    return promotion


def build_native_runner_request(
    *,
    runner_id: str,
    engine: Literal["webwork", "imathas"],
    compiler_version: str,
    answer_kind: Literal["numeric", "formula"],
    native_grader: str,
    source: str,
    source_sha256: str,
    blueprint_sha256: str,
    draft_sha256: str,
    seeds: Sequence[int],
    formula_adapter_promotion: FormulaAdapterPromotionEvidence | None = None,
) -> NativeEngineRunnerRequest:
    payload = {
        "schema_version": NATIVE_RUNNER_SCHEMA_VERSION,
        "runner_id": runner_id,
        "engine": engine,
        "compiler_version": compiler_version,
        "answer_kind": answer_kind,
        "native_grader": native_grader,
        "formula_adapter_promotion": (
            formula_adapter_promotion.model_dump(mode="json", exclude_none=False)
            if formula_adapter_promotion is not None
            else None
        ),
        "source": source,
        "source_sha256": source_sha256,
        "blueprint_sha256": blueprint_sha256,
        "draft_sha256": draft_sha256,
        "seeds": list(seeds),
    }
    payload["request_sha256"] = _canonical_sha256(payload)
    return NativeEngineRunnerRequest.model_validate(payload)


def build_native_seed_observation(
    *,
    seed: int,
    observed_variables: Mapping[str, int],
    correct_submission_sha256: str,
    wrong_submission_sha256: str,
    render_sha256: str,
    repeat_render_sha256: str,
    alternate_correct_submission_sha256: str | None = None,
    alternate_correct_answer_accepted: bool | None = None,
    parameters_satisfied: bool = True,
    constraints_satisfied: bool = True,
    correct_answer_accepted: bool = True,
    wrong_answer_rejected: bool = True,
    rendered: bool = True,
    warnings_count: int = 0,
    errors_count: int = 0,
    outbound_request_count: int = 0,
) -> NativeSeedObservation:
    payload = {
        "seed": seed,
        "observed_variables": dict(observed_variables),
        "correct_submission_sha256": correct_submission_sha256,
        "alternate_correct_submission_sha256": (alternate_correct_submission_sha256),
        "wrong_submission_sha256": wrong_submission_sha256,
        "parameters_satisfied": parameters_satisfied,
        "constraints_satisfied": constraints_satisfied,
        "correct_answer_accepted": correct_answer_accepted,
        "alternate_correct_answer_accepted": (alternate_correct_answer_accepted),
        "wrong_answer_rejected": wrong_answer_rejected,
        "rendered": rendered,
        "render_sha256": render_sha256,
        "repeat_render_sha256": repeat_render_sha256,
        "warnings_count": warnings_count,
        "errors_count": errors_count,
        "outbound_request_count": outbound_request_count,
    }
    payload["observation_sha256"] = _canonical_sha256(payload)
    return NativeSeedObservation.model_validate(payload)


def seed_plan_sha256(seeds: Sequence[int]) -> str:
    return _canonical_sha256(list(seeds))


def seed_observations_sha256(
    observations: Sequence[NativeSeedObservation],
) -> str:
    return _canonical_sha256(
        [
            (
                observation.model_dump(mode="json", exclude_none=False)
                if isinstance(observation, BaseModel)
                else observation
            )
            for observation in observations
        ]
    )


def native_runner_registry_sha256() -> str:
    """Hash the source-controlled promotion registry for generation identity."""

    return _canonical_sha256(
        [
            {
                "key": list(key),
                "promotion": asdict(promotion),
            }
            for key, promotion in sorted(QUALIFIED_NATIVE_ENGINE_RUNNERS.items())
        ]
    )


def _verify_receipt(
    request: NativeEngineRunnerRequest,
    receipt: NativeEngineRunnerReceipt,
    promotion: QualifiedNativeEngineRunner,
) -> None:
    expected = {
        "runner_id": request.runner_id,
        "runner_version": promotion.runner_version,
        "runner_manifest_sha256": promotion.runner_manifest_sha256,
        "runner_image_digest": promotion.runner_image_digest,
        "qualification_report_sha256": promotion.qualification_report_sha256,
        "promotion_approval_sha256": promotion.promotion_approval_sha256,
        "engine": request.engine,
        "compiler_version": request.compiler_version,
        "answer_kind": request.answer_kind,
        "native_grader": promotion.native_grader,
        "formula_adapter_promotion": request.formula_adapter_promotion,
        "source_sha256": request.source_sha256,
        "blueprint_sha256": request.blueprint_sha256,
        "draft_sha256": request.draft_sha256,
        "request_sha256": request.request_sha256,
        "seeds": request.seeds,
        "seed_plan_sha256": seed_plan_sha256(request.seeds),
        "seeds_validated": 25,
        "engine_image_digest": promotion.engine_image_digest,
        "adapter_image_digest": promotion.adapter_image_digest,
        "network_attestation_sha256": promotion.network_attestation_sha256,
    }
    observed = {field: getattr(receipt, field) for field in expected}
    if observed != expected:
        raise NativeRunnerProtocolError(
            "The native engine receipt does not match the exact qualified request."
        )


def _hash_without(model: BaseModel, field: str) -> str:
    return _canonical_sha256(
        model.model_dump(mode="json", exclude={field}, exclude_none=False)
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _timeout(read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=read_seconds,
        write=_WRITE_TIMEOUT_SECONDS,
        pool=_POOL_TIMEOUT_SECONDS,
    )


def _raise_for_status(status_code: int) -> None:
    if status_code == 200:
        return
    if status_code == 504:
        raise NativeRunnerTimeoutError(
            "The native engine runner timed out.",
            status_code=status_code,
        )
    if status_code == 503:
        raise NativeRunnerUnavailableError(
            "The native engine runner is unavailable.",
            status_code=status_code,
        )
    if status_code in {400, 413, 415, 422}:
        raise NativeRunnerRejectedError(
            "The native engine runner rejected the request.",
            status_code=status_code,
        )
    raise NativeRunnerProtocolError(
        f"The native engine runner failed with HTTP {status_code}.",
        status_code=status_code,
    )


def _require_json(response: httpx.Response) -> None:
    media_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    )
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise NativeRunnerProtocolError(
            "The native engine runner returned an invalid content type."
        )


async def _read_bounded_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise NativeRunnerProtocolError(
                "The native engine runner returned an invalid response length."
            ) from None
        if declared_length < 0:
            raise NativeRunnerProtocolError(
                "The native engine runner returned an invalid response length."
            )
        if declared_length > MAX_NATIVE_RUNNER_RESPONSE_BYTES:
            raise NativeRunnerResponseTooLargeError(
                "The native engine runner response exceeds the permitted size."
            )
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_NATIVE_RUNNER_RESPONSE_BYTES:
            raise NativeRunnerResponseTooLargeError(
                "The native engine runner response exceeds the permitted size."
            )
        body.extend(chunk)
    if not body:
        raise NativeRunnerProtocolError(
            "The native engine runner returned an empty response."
        )
    return bytes(body)


__all__ = [
    "MAX_NATIVE_RUNNER_REQUEST_BYTES",
    "MAX_NATIVE_RUNNER_RESPONSE_BYTES",
    "NATIVE_RUNNER_RECEIPT_VERSION",
    "NATIVE_RUNNER_SCHEMA_VERSION",
    "QUALIFIED_NATIVE_ENGINE_RUNNERS",
    "NativeEngineRunner",
    "NativeEngineRunnerReceipt",
    "NativeEngineRunnerRequest",
    "NativeRunnerError",
    "NativeRunnerProtocolError",
    "NativeRunnerRejectedError",
    "NativeRunnerResponseTooLargeError",
    "NativeRunnerTimeoutError",
    "NativeRunnerUnavailableError",
    "NativeRunnerUnqualifiedError",
    "NativeSeedObservation",
    "QualifiedNativeEngineRunner",
    "UnixSocketNativeEngineRunner",
    "build_native_runner_request",
    "build_native_seed_observation",
    "expected_native_grader",
    "native_runner_registry_sha256",
    "qualified_native_engine_runner",
    "seed_observations_sha256",
    "seed_plan_sha256",
]
