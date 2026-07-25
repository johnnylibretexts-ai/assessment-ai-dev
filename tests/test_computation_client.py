from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel

from app import computation
from app.computation_client import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    AssessmentComputationClient,
    ComputationClientError,
    ComputationProtocolError,
    ComputationRejectedError,
    ComputationRequestTooLargeError,
    ComputationResponseTooLargeError,
    ComputationRuntimeIdentityError,
    ComputationServiceError,
    ComputationServiceStatus,
    ComputationTimeoutError,
    ComputationUnavailableError,
    InProcessAssessmentComputationClient,
)
from app.computation_runtime_identity import RUNTIME_MANIFEST_RESPONSE_HEADER


TEST_RUNTIME_MANIFEST_SHA256 = "0" * 64


def service_status(status: str) -> dict[str, str]:
    return {
        "status": status,
        "service": "assessment-computation",
        "schema_version": "assessment-computation-v0",
        "runtime_manifest_sha256": TEST_RUNTIME_MANIFEST_SHA256,
    }


def make_client(
    handler: Any,
) -> AssessmentComputationClient:
    def identity_bound_handler(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if response.status_code == 200:
            response.headers[RUNTIME_MANIFEST_RESPONSE_HEADER] = (
                TEST_RUNTIME_MANIFEST_SHA256
            )
        return response

    return AssessmentComputationClient(
        transport=httpx.MockTransport(identity_bound_handler)
    )


def numeric_blueprint() -> computation.AssessmentComputationBlueprint:
    return computation.AssessmentComputationBlueprint.model_validate_json(
        json.dumps(
            {
                "schema_version": "assessment-computation-v0",
                "profile": {"family": "numeric", "delivery": "numerical"},
                "operation": "evaluate",
                "expression": {"kind": "integer", "integer": 2},
            }
        )
    )


def numeric_validation_request() -> computation.ComputationValidationRequest:
    return computation.ComputationValidationRequest.model_validate_json(
        json.dumps(
            {
                "blueprint": numeric_blueprint().model_dump(mode="json"),
                "candidate_expression": {"kind": "integer", "integer": 2},
            }
        )
    )


def numeric_result() -> computation.ComputationResult:
    return computation.ComputationResult.model_validate_json(
        json.dumps(
            {
                "blueprint_hash": computation.canonical_blueprint_hash(
                    numeric_blueprint()
                ),
                "operation": "evaluate",
                "exact_value": "2",
                "approximate_value": "2",
                "numeric_value": "2",
                "canonical_expression": "2",
            }
        )
    )


def numeric_report() -> computation.AssessmentValidationReport:
    result = numeric_result()
    return computation.AssessmentValidationReport.model_validate_json(
        json.dumps(
            {
                "status": "validated",
                "blueprint_hash": result.blueprint_hash,
                "result": result.model_dump(mode="json"),
            }
        )
    )


@pytest.mark.asyncio
async def test_compute_and_validate_round_trip_typed_wire_models() -> None:
    blueprint = numeric_blueprint()
    validation_request = numeric_validation_request()
    expected_result = numeric_result()
    expected_report = numeric_report()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["content-type"] == "application/json"
        if request.url.path == "/v0/compute":
            received = computation.AssessmentComputationBlueprint.model_validate_json(
                request.content
            )
            assert received == blueprint
            response = expected_result
        else:
            assert request.url.path == "/v0/validate"
            received = computation.ComputationValidationRequest.model_validate_json(
                request.content
            )
            assert received == validation_request
            response = expected_report
        return httpx.Response(200, json=response.model_dump(mode="json"))

    async with make_client(handler) as client:
        assert await client.compute(blueprint) == expected_result
        assert await client.validate(validation_request) == expected_report

    assert [request.url.path for request in requests] == [
        "/v0/compute",
        "/v0/validate",
    ]
    assert all(request.extensions["timeout"]["read"] == 2.75 for request in requests)


@pytest.mark.asyncio
async def test_default_transport_uses_only_the_configured_unix_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=service_status("ok"))
    )

    def make_transport(**kwargs: object) -> httpx.AsyncBaseTransport:
        captured.update(kwargs)
        return transport

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", make_transport)
    client = AssessmentComputationClient(Path("/tmp/assessment-computation.sock"))
    try:
        assert captured == {
            "uds": "/tmp/assessment-computation.sock",
            "retries": 0,
        }
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_health_and_readiness_are_strict_typed_probes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = "ok" if request.url.path == "/healthz" else "ready"
        return httpx.Response(200, json=service_status(status))

    async with make_client(handler) as client:
        health = await client.health()
        readiness = await client.ready()

    assert health == ComputationServiceStatus(
        status="ok",
        service="assessment-computation",
        schema_version="assessment-computation-v0",
        runtime_manifest_sha256=TEST_RUNTIME_MANIFEST_SHA256,
    )
    assert readiness.status == "ready"
    assert [request.url.path for request in requests] == ["/healthz", "/readyz"]
    assert all(request.url.host == "assessment-computation" for request in requests)
    assert all(request.extensions["timeout"]["read"] == 1.0 for request in requests)


@pytest.mark.asyncio
async def test_probe_rejects_wrong_status_and_extra_fields() -> None:
    responses = iter(
        [
            httpx.Response(200, json=service_status("ready")),
            httpx.Response(
                200,
                json={**service_status("ready"), "internal_path": "/private"},
            ),
        ]
    )
    async with make_client(lambda _request: next(responses)) as client:
        with pytest.raises(ComputationProtocolError):
            await client.health()
        with pytest.raises(ComputationProtocolError):
            await client.ready()


@pytest.mark.asyncio
async def test_client_requires_and_pins_the_runtime_manifest_identity() -> None:
    other_identity = "1" * 64
    responses = iter(
        [
            httpx.Response(
                200,
                json=service_status("ok"),
                headers={
                    RUNTIME_MANIFEST_RESPONSE_HEADER: TEST_RUNTIME_MANIFEST_SHA256
                },
            ),
            httpx.Response(
                200,
                json={
                    **service_status("ready"),
                    "runtime_manifest_sha256": other_identity,
                },
                headers={RUNTIME_MANIFEST_RESPONSE_HEADER: other_identity},
            ),
        ]
    )
    client = AssessmentComputationClient(
        transport=httpx.MockTransport(lambda _request: next(responses)),
        expected_runtime_manifest_sha256=TEST_RUNTIME_MANIFEST_SHA256,
    )
    async with client:
        await client.health()
        with pytest.raises(ComputationRuntimeIdentityError):
            await client.ready()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {RUNTIME_MANIFEST_RESPONSE_HEADER: "unavailable"},
        {RUNTIME_MANIFEST_RESPONSE_HEADER: "not-a-sha256"},
    ],
)
async def test_client_rejects_missing_or_unverified_runtime_identity(
    headers: dict[str, str],
) -> None:
    client = AssessmentComputationClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=service_status("ok"),
                headers=headers,
            )
        )
    )
    async with client:
        with pytest.raises(ComputationRuntimeIdentityError):
            await client.health()


@pytest.mark.asyncio
async def test_client_rejects_conflicting_header_and_status_identity() -> None:
    client = AssessmentComputationClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=service_status("ok"),
                headers={RUNTIME_MANIFEST_RESPONSE_HEADER: "1" * 64},
            )
        )
    )
    async with client:
        with pytest.raises(ComputationRuntimeIdentityError):
            await client.health()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (httpx.ReadTimeout, ComputationTimeoutError),
        (httpx.ConnectError, ComputationUnavailableError),
    ],
)
async def test_transport_failures_are_typed_and_sanitized(
    failure: type[httpx.RequestError],
    expected_type: type[Exception],
) -> None:
    private_detail = "private-socket-or-request-detail"

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure(private_detail, request=request)

    async with make_client(handler) as client:
        with pytest.raises(expected_type) as raised:
            await client.health()

    assert private_detail not in str(raised.value)


@pytest.mark.asyncio
async def test_error_responses_do_not_expose_or_parse_their_body() -> None:
    private_detail = "private-sidecar-diagnostic"

    async with make_client(
        lambda _request: httpx.Response(503, text=private_detail)
    ) as client:
        with pytest.raises(ComputationUnavailableError) as raised:
            await client.ready()

    assert raised.value.status_code == 503
    assert private_detail not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [
        (400, ComputationRejectedError),
        (413, ComputationRequestTooLargeError),
        (415, ComputationRejectedError),
        (422, ComputationRejectedError),
        (500, ComputationServiceError),
        (504, ComputationTimeoutError),
    ],
)
async def test_sidecar_status_codes_map_to_fail_closed_typed_errors(
    status_code: int,
    expected_type: type[ComputationClientError],
) -> None:
    async with make_client(
        lambda _request: httpx.Response(status_code, text="private diagnostic")
    ) as client:
        with pytest.raises(expected_type) as raised:
            await client.health()

    assert raised.value.status_code == status_code
    assert "private diagnostic" not in str(raised.value)


@pytest.mark.asyncio
async def test_unexpected_status_fails_closed() -> None:
    async with make_client(
        lambda _request: httpx.Response(
            302,
            headers={"location": "http://unexpected.invalid/"},
        )
    ) as client:
        with pytest.raises(ComputationServiceError) as raised:
            await client.health()

    assert raised.value.status_code == 302


@pytest.mark.asyncio
async def test_response_requires_json_and_a_valid_typed_body() -> None:
    responses = iter(
        [
            httpx.Response(200, text="not-json"),
            httpx.Response(
                200,
                content=b"{not valid json",
                headers={"content-type": "application/json"},
            ),
        ]
    )
    async with make_client(lambda _request: next(responses)) as client:
        with pytest.raises(ComputationProtocolError):
            await client.health()
        with pytest.raises(ComputationProtocolError):
            await client.health()


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_declared_and_streamed_response_sizes_are_bounded() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-length": str(MAX_RESPONSE_BYTES + 1),
                },
            ),
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=ChunkStream(
                    b"x" * MAX_RESPONSE_BYTES,
                    b"x",
                ),
            ),
        ]
    )
    async with make_client(lambda _request: next(responses)) as client:
        with pytest.raises(ComputationResponseTooLargeError):
            await client.health()
        with pytest.raises(ComputationResponseTooLargeError):
            await client.health()


class OversizedRequest(BaseModel):
    payload: str


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_before_transport() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=service_status("ok"))

    request = OversizedRequest(payload="x" * MAX_REQUEST_BYTES)
    async with make_client(handler) as client:
        with pytest.raises(ComputationRequestTooLargeError):
            await client._post_model(
                "/v0/compute",
                request,
                ComputationServiceStatus,
                timeout_seconds=2.0,
            )
    assert called is False


@pytest.mark.asyncio
async def test_compute_and_validate_route_to_typed_endpoints_and_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(
        lambda _request: pytest.fail("transport should be replaced in this test")
    )
    calls: list[tuple[object, ...]] = []
    result = object()
    report = object()

    async def fake_post(
        path: str,
        request: BaseModel,
        response_model: type[BaseModel],
        *,
        timeout_seconds: float,
    ) -> object:
        calls.append((path, request, response_model, timeout_seconds))
        return result if path == "/v0/compute" else report

    monkeypatch.setattr(client, "_post_model", fake_post)
    numeric_blueprint_value = numeric_blueprint()
    validation_request = numeric_validation_request()
    try:
        assert await client.compute(numeric_blueprint_value) is result
        assert await client.validate(validation_request) is report
    finally:
        await client.aclose()

    assert calls == [
        (
            "/v0/compute",
            numeric_blueprint_value,
            computation.ComputationResult,
            2.0,
        ),
        (
            "/v0/validate",
            validation_request,
            computation.AssessmentValidationReport,
            2.0,
        ),
    ]


@pytest.mark.asyncio
async def test_algebraic_compute_uses_the_bounded_five_second_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(
        lambda _request: pytest.fail("transport should be replaced in this test")
    )
    deadlines: list[float] = []

    async def fake_post(
        _path: str,
        _request: BaseModel,
        _response_model: type[BaseModel],
        *,
        timeout_seconds: float,
    ) -> object:
        deadlines.append(timeout_seconds)
        return object()

    monkeypatch.setattr(client, "_post_model", fake_post)
    algebraic_blueprint = cast(
        computation.AssessmentComputationBlueprint,
        SimpleNamespace(profile=SimpleNamespace(family="algebraic")),
    )
    try:
        await client.compute(algebraic_blueprint)
    finally:
        await client.aclose()

    assert deadlines == [5.0]


@pytest.mark.asyncio
async def test_in_process_facade_calls_the_core_functions_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = cast(computation.AssessmentComputationBlueprint, object())
    validation_request = cast(computation.ComputationValidationRequest, object())
    expected_result = numeric_result()
    expected_report = numeric_report()
    received: list[object] = []

    def compute(value: object) -> object:
        received.append(value)
        return expected_result

    def validate(value: object) -> object:
        received.append(value)
        return expected_report

    monkeypatch.setattr(computation, "compute_blueprint", compute)
    monkeypatch.setattr(computation, "validate_computation", validate)
    async with InProcessAssessmentComputationClient() as client:
        assert await client.compute(blueprint) is expected_result
        assert await client.validate(validation_request) is expected_report
        assert (await client.health()).status == "ok"
        assert (await client.ready()).status == "ready"

    assert received == [blueprint, validation_request]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("core_error", "client_error"),
    [
        (computation.ComputationDependencyError, ComputationUnavailableError),
        (computation.ComputationUnsupportedError, ComputationRejectedError),
        (computation.ComputationValidationError, ComputationRejectedError),
        (computation.ComputationError, ComputationServiceError),
    ],
)
async def test_in_process_facade_sanitizes_core_failures(
    monkeypatch: pytest.MonkeyPatch,
    core_error: type[computation.ComputationError],
    client_error: type[ComputationClientError],
) -> None:
    private_detail = "private-computation-detail"

    def fail(_blueprint: object) -> object:
        raise core_error(private_detail)

    monkeypatch.setattr(computation, "compute_blueprint", fail)
    client = InProcessAssessmentComputationClient()
    with pytest.raises(client_error) as raised:
        await client.compute(numeric_blueprint())

    assert private_detail not in str(raised.value)


@pytest.mark.asyncio
async def test_in_process_facade_fails_closed_on_unexpected_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_detail = "private-unexpected-detail"

    def crash(_blueprint: object) -> object:
        raise ValueError(private_detail)

    monkeypatch.setattr(computation, "compute_blueprint", crash)
    client = InProcessAssessmentComputationClient()
    with pytest.raises(ComputationServiceError) as raised:
        await client.compute(numeric_blueprint())
    assert private_detail not in str(raised.value)

    monkeypatch.setattr(computation, "compute_blueprint", lambda _blueprint: object())
    with pytest.raises(ComputationProtocolError):
        await client.compute(numeric_blueprint())
