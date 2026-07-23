from __future__ import annotations

import hashlib
import json
from types import MappingProxyType

import httpx
import pytest
from pydantic import ValidationError

import app.native_engine_runner as runner_module
from app.native_engine_runner import (
    NATIVE_RUNNER_RECEIPT_VERSION,
    NativeEngineRunnerRequest,
    NativeRunnerProtocolError,
    NativeRunnerTimeoutError,
    NativeRunnerUnqualifiedError,
    QualifiedNativeEngineRunner,
    UnixSocketNativeEngineRunner,
    build_native_runner_request,
    build_native_seed_observation,
    seed_observations_sha256,
    seed_plan_sha256,
)


def _promotion() -> QualifiedNativeEngineRunner:
    return QualifiedNativeEngineRunner(
        runner_id="qualified-runner",
        runner_version="runner-v0",
        runner_manifest_sha256="1" * 64,
        runner_image_digest=f"sha256:{'2' * 64}",
        engine="webwork",
        compiler_version="assessment-computation-typed-ast-v0",
        answer_kind="numeric",
        native_grader="MathObjects::Real::cmp",
        engine_image_digest=f"sha256:{'3' * 64}",
        adapter_image_digest=None,
        network_attestation_sha256="4" * 64,
        qualification_report_sha256="5" * 64,
        promotion_approval_sha256="6" * 64,
    )


def _request() -> NativeEngineRunnerRequest:
    source = "DOCUMENT();\nBEGIN_TEXT\nqualified\nEND_TEXT\nENDDOCUMENT();"
    seeds = list(range(100, 125))
    return build_native_runner_request(
        runner_id="qualified-runner",
        engine="webwork",
        compiler_version="assessment-computation-typed-ast-v0",
        answer_kind="numeric",
        native_grader="MathObjects::Real::cmp",
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        blueprint_sha256="7" * 64,
        draft_sha256="8" * 64,
        seeds=seeds,
    )


def _receipt_payload(
    request: NativeEngineRunnerRequest,
    promotion: QualifiedNativeEngineRunner,
    **updates: object,
) -> dict[str, object]:
    observations = [
        build_native_seed_observation(
            seed=seed,
            observed_variables={"a": (index % 3) + 1},
            correct_submission_sha256=hashlib.sha256(
                f"correct:{seed}".encode()
            ).hexdigest(),
            wrong_submission_sha256=hashlib.sha256(
                f"wrong:{seed}".encode()
            ).hexdigest(),
            render_sha256=hashlib.sha256(f"render:{seed}".encode()).hexdigest(),
            repeat_render_sha256=hashlib.sha256(f"render:{seed}".encode()).hexdigest(),
        )
        for index, seed in enumerate(request.seeds)
    ]
    aggregate_render = runner_module._canonical_sha256(
        [item.render_sha256 for item in observations]
    )
    payload: dict[str, object] = {
        "schema_version": NATIVE_RUNNER_RECEIPT_VERSION,
        "runner_id": promotion.runner_id,
        "runner_version": promotion.runner_version,
        "runner_manifest_sha256": promotion.runner_manifest_sha256,
        "runner_image_digest": promotion.runner_image_digest,
        "qualification_report_sha256": promotion.qualification_report_sha256,
        "promotion_approval_sha256": promotion.promotion_approval_sha256,
        "engine": request.engine,
        "compiler_version": request.compiler_version,
        "answer_kind": request.answer_kind,
        "native_grader": promotion.native_grader,
        "formula_adapter_promotion": (
            request.formula_adapter_promotion.model_dump(
                mode="json",
                exclude_none=False,
            )
            if request.formula_adapter_promotion is not None
            else None
        ),
        "source_sha256": request.source_sha256,
        "blueprint_sha256": request.blueprint_sha256,
        "draft_sha256": request.draft_sha256,
        "request_sha256": request.request_sha256,
        "seeds": request.seeds,
        "seed_plan_sha256": seed_plan_sha256(request.seeds),
        "seed_receipts_sha256": seed_observations_sha256(observations),
        "seeds_validated": 25,
        "observations": [
            item.model_dump(mode="json", exclude_none=False) for item in observations
        ],
        "engine_image_digest": promotion.engine_image_digest,
        "adapter_image_digest": promotion.adapter_image_digest,
        "network_attestation_sha256": promotion.network_attestation_sha256,
        "correct_answer_accepted": True,
        "wrong_answer_rejected": True,
        "rendered": True,
        "render_sha256": aggregate_render,
        "repeat_render_sha256": aggregate_render,
        "warnings_count": 0,
        "errors_count": 0,
        "outbound_request_count": 0,
        "passed": True,
    }
    payload.update(updates)
    payload["receipt_sha256"] = runner_module._canonical_sha256(payload)
    return payload


def _promote(
    monkeypatch: pytest.MonkeyPatch,
    promotion: QualifiedNativeEngineRunner,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "QUALIFIED_NATIVE_ENGINE_RUNNERS",
        MappingProxyType(
            {
                (
                    promotion.runner_id,
                    promotion.engine,
                    promotion.compiler_version,
                    promotion.answer_kind,
                ): promotion
            }
        ),
    )


@pytest.mark.asyncio
async def test_unix_socket_runner_accepts_only_exact_qualified_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _promotion()
    request = _request()
    _promote(monkeypatch, promotion)
    observed_paths: list[str] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        observed_paths.append(http_request.url.path)
        assert json.loads(http_request.content) == request.model_dump(mode="json")
        return httpx.Response(
            200,
            json=_receipt_payload(request, promotion),
            headers={"content-type": "application/json"},
        )

    runner = UnixSocketNativeEngineRunner(
        "/run/assessment-native/runner.sock",
        runner_id=promotion.runner_id,
        transport=httpx.MockTransport(handler),
    )
    try:
        receipt = await runner.validate(request)
    finally:
        await runner.aclose()

    assert receipt.request_sha256 == request.request_sha256
    assert receipt.seeds == request.seeds
    assert observed_paths == ["/v0/validate"]


@pytest.mark.asyncio
async def test_unqualified_runner_never_contacts_transport() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    runner = UnixSocketNativeEngineRunner(
        "/run/assessment-native/runner.sock",
        runner_id="qualified-runner",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NativeRunnerUnqualifiedError):
            await runner.validate(_request())
    finally:
        await runner.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_mismatched_receipt_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _promotion()
    request = _request()
    _promote(monkeypatch, promotion)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_receipt_payload(request, promotion, source_sha256="b" * 64),
            headers={"content-type": "application/json"},
        )

    runner = UnixSocketNativeEngineRunner(
        "/run/assessment-native/runner.sock",
        runner_id=promotion.runner_id,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(
            NativeRunnerProtocolError,
            match="exact qualified request",
        ):
            await runner.validate(request)
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_timeout_is_sanitized_and_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _promotion()
    request = _request()
    _promote(monkeypatch, promotion)

    async def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private runner detail", request=http_request)

    runner = UnixSocketNativeEngineRunner(
        "/run/assessment-native/runner.sock",
        runner_id=promotion.runner_id,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NativeRunnerTimeoutError) as caught:
            await runner.validate(request)
    finally:
        await runner.aclose()
    assert str(caught.value) == "The native engine runner timed out."
    assert "private runner detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_nonzero_warning_receipt_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion = _promotion()
    request = _request()
    _promote(monkeypatch, promotion)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_receipt_payload(request, promotion, warnings_count=1),
            headers={"content-type": "application/json"},
        )

    runner = UnixSocketNativeEngineRunner(
        "/run/assessment-native/runner.sock",
        runner_id=promotion.runner_id,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NativeRunnerProtocolError):
            await runner.validate(request)
    finally:
        await runner.aclose()


def test_native_runner_rejects_urls_and_request_url_fields() -> None:
    with pytest.raises(ValueError, match="without URLs"):
        UnixSocketNativeEngineRunner(
            "https://attacker.example/runner.sock",
            runner_id="qualified-runner",
        )

    payload = _request().model_dump(mode="json")
    payload["url"] = "https://attacker.example"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NativeEngineRunnerRequest.model_validate(payload)
