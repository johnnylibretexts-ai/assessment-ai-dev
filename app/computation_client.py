from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from . import computation
from .computation_runtime_identity import (
    RUNTIME_MANIFEST_RESPONSE_HEADER,
    validate_runtime_manifest_sha256,
)


DEFAULT_SOCKET_PATH = Path("/run/assessment-computation/compute.sock")
SERVICE_BASE_URL = "http://assessment-computation"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024

_CONNECT_TIMEOUT_SECONDS = 0.5
_WRITE_TIMEOUT_SECONDS = 1.0
_POOL_TIMEOUT_SECONDS = 0.5
_PROBE_TIMEOUT_SECONDS = 1.0
_NUMERIC_TIMEOUT_SECONDS = 2.0
_ALGEBRAIC_TIMEOUT_SECONDS = 5.0
_SIDECAR_RESPONSE_GRACE_SECONDS = 0.75
IN_PROCESS_RUNTIME_MANIFEST_SHA256 = "0" * 64

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class ComputationClientError(RuntimeError):
    """Base class for safe, fail-closed computation service failures."""

    code = "computation_client_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ComputationTransportError(ComputationClientError):
    code = "computation_transport_error"


class ComputationTimeoutError(ComputationTransportError):
    code = "computation_timeout"


class ComputationUnavailableError(ComputationTransportError):
    code = "computation_unavailable"


class ComputationProtocolError(ComputationClientError):
    code = "computation_invalid_response"


class ComputationRuntimeIdentityError(ComputationProtocolError):
    code = "computation_runtime_identity_mismatch"


class ComputationRequestTooLargeError(ComputationClientError):
    code = "computation_request_too_large"


class ComputationResponseTooLargeError(ComputationProtocolError):
    code = "computation_response_too_large"


class ComputationRejectedError(ComputationClientError):
    code = "computation_request_rejected"


class ComputationServiceError(ComputationClientError):
    code = "computation_service_error"


class ComputationServiceStatus(BaseModel):
    """Strict response contract shared by the health and readiness probes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "ready"]
    service: Literal["assessment-computation"]
    schema_version: Literal["assessment-computation-v0"]
    runtime_manifest_sha256: str

    @field_validator("runtime_manifest_sha256")
    @classmethod
    def validate_manifest_sha256(cls, value: str) -> str:
        if value == "unavailable":
            return value
        return validate_runtime_manifest_sha256(value)


class ComputationClient(Protocol):
    """Transport-independent asynchronous computation client contract."""

    async def compute(
        self, blueprint: computation.AssessmentComputationBlueprint
    ) -> computation.ComputationResult: ...

    async def validate(
        self, request: computation.ComputationValidationRequest
    ) -> computation.AssessmentValidationReport: ...

    async def health(self) -> ComputationServiceStatus: ...

    async def ready(self) -> ComputationServiceStatus: ...

    async def aclose(self) -> None: ...


class AssessmentComputationClient:
    """Bounded HTTP client for the isolated computation Unix socket."""

    def __init__(
        self,
        socket_path: str | Path = DEFAULT_SOCKET_PATH,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        expected_runtime_manifest_sha256: str | None = None,
    ) -> None:
        socket_path_text = str(socket_path)
        if not socket_path_text.strip():
            raise ValueError("socket_path must not be blank")
        if transport is None:
            transport = httpx.AsyncHTTPTransport(
                uds=socket_path_text,
                retries=0,
            )
        self._client = httpx.AsyncClient(
            base_url=SERVICE_BASE_URL,
            transport=transport,
            follow_redirects=False,
            timeout=_timeout(_ALGEBRAIC_TIMEOUT_SECONDS),
            headers={
                "Accept": "application/json",
                "User-Agent": "LibreTexts-Assessment-AI/computation-v0",
            },
        )
        self._expected_runtime_manifest_sha256 = (
            validate_runtime_manifest_sha256(expected_runtime_manifest_sha256)
            if expected_runtime_manifest_sha256 is not None
            else None
        )
        self._observed_runtime_manifest_sha256: str | None = None

    async def __aenter__(self) -> AssessmentComputationClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def compute(
        self, blueprint: computation.AssessmentComputationBlueprint
    ) -> computation.ComputationResult:
        return await self._post_model(
            "/v0/compute",
            blueprint,
            computation.ComputationResult,
            timeout_seconds=_deadline_for_blueprint(blueprint),
        )

    async def validate(
        self, request: computation.ComputationValidationRequest
    ) -> computation.AssessmentValidationReport:
        return await self._post_model(
            "/v0/validate",
            request,
            computation.AssessmentValidationReport,
            timeout_seconds=_deadline_for_validation_request(request),
        )

    async def health(self) -> ComputationServiceStatus:
        status = await self._request_model(
            "GET",
            "/healthz",
            ComputationServiceStatus,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        if status.status != "ok":
            raise ComputationProtocolError(
                "The computation service returned an invalid health response."
            )
        return status

    async def ready(self) -> ComputationServiceStatus:
        status = await self._request_model(
            "GET",
            "/readyz",
            ComputationServiceStatus,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        if status.status != "ready":
            raise ComputationProtocolError(
                "The computation service returned an invalid readiness response."
            )
        return status

    async def _post_model(
        self,
        path: str,
        request: BaseModel,
        response_model: type[ResponseModelT],
        *,
        timeout_seconds: float,
    ) -> ResponseModelT:
        body = request.model_dump_json(by_alias=True).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ComputationRequestTooLargeError(
                "The computation request exceeds the permitted size."
            )
        return await self._request_model(
            "POST",
            path,
            response_model,
            # The isolated child owns the 2s/5s execution budget. This small,
            # fixed transport grace lets the sidecar return its sanitized 504.
            timeout_seconds=timeout_seconds + _SIDECAR_RESPONSE_GRACE_SECONDS,
            content=body,
        )

    async def _request_model(
        self,
        method: Literal["GET", "POST"],
        path: str,
        response_model: type[ResponseModelT],
        *,
        timeout_seconds: float,
        content: bytes | None = None,
    ) -> ResponseModelT:
        headers = {"Content-Type": "application/json"} if content is not None else None
        try:
            async with self._client.stream(
                method,
                path,
                content=content,
                headers=headers,
                timeout=_timeout(timeout_seconds),
            ) as response:
                _raise_for_status(response.status_code)
                _require_json_content_type(response)
                body = await _read_bounded_response(response)
        except ComputationClientError:
            raise
        except httpx.TimeoutException:
            raise ComputationTimeoutError(
                "The computation service request timed out."
            ) from None
        except httpx.RequestError:
            raise ComputationUnavailableError(
                "The computation service is unavailable."
            ) from None

        try:
            parsed = response_model.model_validate_json(body)
        except (ValidationError, ValueError):
            raise ComputationProtocolError(
                "The computation service returned an invalid response."
            ) from None
        self._bind_runtime_identity(response, parsed)
        return parsed

    def _bind_runtime_identity(
        self,
        response: httpx.Response,
        parsed: BaseModel,
    ) -> None:
        header_value = response.headers.get(RUNTIME_MANIFEST_RESPONSE_HEADER, "")
        try:
            observed = validate_runtime_manifest_sha256(header_value)
        except ValueError:
            raise ComputationRuntimeIdentityError(
                "The computation service did not provide a valid runtime identity."
            ) from None
        if isinstance(parsed, ComputationServiceStatus):
            if parsed.runtime_manifest_sha256 != observed:
                raise ComputationRuntimeIdentityError(
                    "The computation service returned conflicting runtime identities."
                )
        if (
            self._expected_runtime_manifest_sha256 is not None
            and observed != self._expected_runtime_manifest_sha256
        ):
            raise ComputationRuntimeIdentityError(
                "The computation service runtime is not the configured qualified build."
            )
        if (
            self._observed_runtime_manifest_sha256 is not None
            and observed != self._observed_runtime_manifest_sha256
        ):
            raise ComputationRuntimeIdentityError(
                "The computation service runtime identity changed during this "
                "application process."
            )
        self._observed_runtime_manifest_sha256 = observed


class InProcessAssessmentComputationClient:
    """Test facade with the same async surface and no HTTP or socket access."""

    async def compute(
        self, blueprint: computation.AssessmentComputationBlueprint
    ) -> computation.ComputationResult:
        try:
            _bind_in_process_runtime()
            result = computation.compute_blueprint(blueprint)
        except computation.ComputationDependencyError:
            raise ComputationUnavailableError(
                "The computation engine dependency is unavailable."
            ) from None
        except (
            computation.ComputationUnsupportedError,
            computation.ComputationValidationError,
        ):
            raise ComputationRejectedError(
                "The computation request was rejected."
            ) from None
        except computation.ComputationError:
            raise ComputationServiceError("The computation request failed.") from None
        except Exception:
            raise ComputationServiceError("The computation request failed.") from None
        if not isinstance(result, computation.ComputationResult):
            raise ComputationProtocolError(
                "The in-process computation engine returned an invalid result."
            )
        return result

    async def validate(
        self, request: computation.ComputationValidationRequest
    ) -> computation.AssessmentValidationReport:
        try:
            _bind_in_process_runtime()
            report = computation.validate_computation(request)
        except computation.ComputationDependencyError:
            raise ComputationUnavailableError(
                "The computation engine dependency is unavailable."
            ) from None
        except (
            computation.ComputationUnsupportedError,
            computation.ComputationValidationError,
        ):
            raise ComputationRejectedError(
                "The computation request was rejected."
            ) from None
        except computation.ComputationError:
            raise ComputationServiceError("The computation request failed.") from None
        except Exception:
            raise ComputationServiceError("The computation request failed.") from None
        if not isinstance(report, computation.AssessmentValidationReport):
            raise ComputationProtocolError(
                "The in-process computation engine returned an invalid report."
            )
        return report

    async def health(self) -> ComputationServiceStatus:
        return ComputationServiceStatus(
            status="ok",
            service="assessment-computation",
            schema_version="assessment-computation-v0",
            runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
        )

    async def ready(self) -> ComputationServiceStatus:
        return ComputationServiceStatus(
            status="ready",
            service="assessment-computation",
            schema_version="assessment-computation-v0",
            runtime_manifest_sha256=IN_PROCESS_RUNTIME_MANIFEST_SHA256,
        )

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> InProcessAssessmentComputationClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


def _bind_in_process_runtime() -> None:
    """Load compute-only dependencies only for the explicit test facade."""

    from .computation_runtime import bind_runtime_dependencies

    bind_runtime_dependencies()


def _timeout(read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=read_seconds,
        write=_WRITE_TIMEOUT_SECONDS,
        pool=_POOL_TIMEOUT_SECONDS,
    )


def _deadline_for_blueprint(
    blueprint: computation.AssessmentComputationBlueprint,
) -> float:
    family = getattr(blueprint.profile.family, "value", blueprint.profile.family)
    if family == "algebraic":
        return _ALGEBRAIC_TIMEOUT_SECONDS
    return _NUMERIC_TIMEOUT_SECONDS


def _deadline_for_validation_request(
    request: computation.ComputationValidationRequest,
) -> float:
    blueprint = getattr(request, "blueprint", None)
    if isinstance(blueprint, computation.AssessmentComputationBlueprint):
        return _deadline_for_blueprint(blueprint)
    return _ALGEBRAIC_TIMEOUT_SECONDS


def _raise_for_status(status_code: int) -> None:
    if status_code == 200:
        return
    if status_code == 504:
        raise ComputationTimeoutError(
            "The computation service request timed out.",
            status_code=status_code,
        )
    if status_code == 503:
        raise ComputationUnavailableError(
            "The computation service is unavailable.",
            status_code=status_code,
        )
    if status_code == 413:
        raise ComputationRequestTooLargeError(
            "The computation service rejected an oversized request.",
            status_code=status_code,
        )
    if status_code in {400, 415, 422}:
        raise ComputationRejectedError(
            "The computation service rejected the request.",
            status_code=status_code,
        )
    raise ComputationServiceError(
        f"The computation service failed with HTTP {status_code}.",
        status_code=status_code,
    )


def _require_json_content_type(response: httpx.Response) -> None:
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise ComputationProtocolError(
            "The computation service returned an invalid content type."
        )


async def _read_bounded_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise ComputationProtocolError(
                "The computation service returned an invalid response length."
            ) from None
        if declared_length < 0:
            raise ComputationProtocolError(
                "The computation service returned an invalid response length."
            )
        if declared_length > MAX_RESPONSE_BYTES:
            raise ComputationResponseTooLargeError(
                "The computation service response exceeds the permitted size."
            )

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ComputationResponseTooLargeError(
                "The computation service response exceeds the permitted size."
            )
        body.extend(chunk)
    if not body:
        raise ComputationProtocolError(
            "The computation service returned an empty response."
        )
    return bytes(body)
