from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import computation
from app import computation_runtime_identity
from app import computation_service
from app.computation_runtime_identity import (
    RUNTIME_MANIFEST_RESPONSE_HEADER,
    ComputationRuntimeIdentity,
    RuntimeManifestError,
    load_runtime_identity,
    write_runtime_manifest,
)


TEST_RUNTIME_IDENTITY = ComputationRuntimeIdentity(
    manifest_sha256="0" * 64,
    source_tree_sha256="1" * 64,
    dependency_versions=(),
)
SERVICE_STATUS = {
    "status": "ok",
    "service": "assessment-computation",
    "schema_version": "assessment-computation-v0",
    "runtime_manifest_sha256": TEST_RUNTIME_IDENTITY.manifest_sha256,
}


def _service_app():
    return computation_service.create_app(TEST_RUNTIME_IDENTITY)


def _numeric_blueprint() -> dict[str, Any]:
    return {
        "schema_version": "assessment-computation-v0",
        "profile": {"family": "numeric", "delivery": "numerical"},
        "operation": "evaluate",
        "expression": {"kind": "integer", "integer": 2},
    }


def _require_pinned_runtime() -> None:
    try:
        computation.assert_computation_dependencies()
    except computation.ComputationDependencyError:
        pytest.skip("unit dependencies are not installed in the host test runtime")


def test_health_and_readiness_have_strict_non_secret_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _service_app()
    with TestClient(app) as client:
        assert client.get("/healthz").json() == SERVICE_STATUS
        assert (
            client.get("/healthz").headers[RUNTIME_MANIFEST_RESPONSE_HEADER]
            == TEST_RUNTIME_IDENTITY.manifest_sha256
        )

        monkeypatch.setattr(
            computation_service, "_core_interfaces_available", lambda: True
        )
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {**SERVICE_STATUS, "status": "ready"}

        monkeypatch.setattr(
            computation_service, "_core_interfaces_available", lambda: False
        )
        unavailable = client.get("/readyz")
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "detail": "The computation engine dependency is unavailable."
        }


def test_readiness_fails_closed_without_a_verified_build_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_identity() -> ComputationRuntimeIdentity:
        raise RuntimeManifestError("private build path")

    monkeypatch.setattr(
        computation_service,
        "load_runtime_identity",
        unavailable_identity,
    )
    monkeypatch.setattr(
        computation_service,
        "_core_interfaces_available",
        lambda: True,
    )
    with TestClient(computation_service.create_app()) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["runtime_manifest_sha256"] == "unavailable"
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json() == {
            "detail": "The computation runtime identity is unavailable."
        }


def test_build_manifest_binds_runtime_source_and_dependency_versions(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    manifest_path = tmp_path / "computation-runtime-manifest.json"

    written = write_runtime_manifest(repository, manifest_path)
    loaded = load_runtime_identity(manifest_path, root=repository)

    assert loaded == written
    assert len(loaded.manifest_sha256) == 64
    assert len(loaded.source_tree_sha256) == 64
    dependency_versions = dict(loaded.dependency_versions)
    assert {
        name: dependency_versions[name] for name in ("pint", "sympy", "ucumvert")
    } == {
        "pint": "0.25.3",
        "sympy": "1.14.0",
        "ucumvert": "0.3.2",
    }
    assert {"fastapi", "httpx", "pydantic", "uvicorn"} <= set(dependency_versions)
    assert list(dependency_versions) == sorted(dependency_versions)
    assert manifest_path.stat().st_size <= 16 * 1024


def _runtime_distributions(
    *,
    sympy_version: str = "1.14.0",
    extras: tuple[tuple[str, str], ...] = (),
) -> tuple[SimpleNamespace, ...]:
    versions = (
        ("Pint", "0.25.3"),
        ("sympy", sympy_version),
        ("ucumvert", "0.3.2"),
        *extras,
    )
    return tuple(
        SimpleNamespace(metadata={"Name": name}, version=version)
        for name, version in versions
    )


def test_runtime_manifest_rejects_duplicate_normalized_distribution_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        computation_runtime_identity.importlib.metadata,
        "distributions",
        lambda: _runtime_distributions(
            extras=(("Example_Package", "1.0"), ("example-package", "2.0"))
        ),
    )

    with pytest.raises(RuntimeManifestError, match="duplicate normalized"):
        computation_runtime_identity.build_runtime_manifest(
            Path(__file__).resolve().parents[1]
        )


def test_runtime_manifest_rejects_unqualified_compute_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        computation_runtime_identity.importlib.metadata,
        "distributions",
        lambda: _runtime_distributions(sympy_version="1.14.1"),
    )

    with pytest.raises(RuntimeManifestError, match=r"sympy==1\.14\.0"):
        computation_runtime_identity.build_runtime_manifest(
            Path(__file__).resolve().parents[1]
        )


def test_runtime_manifest_verification_detects_distribution_set_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    manifest_path = tmp_path / "computation-runtime-manifest.json"
    monkeypatch.setattr(
        computation_runtime_identity.importlib.metadata,
        "distributions",
        lambda: _runtime_distributions(extras=(("FastAPI", "0.139.2"),)),
    )
    write_runtime_manifest(repository, manifest_path)
    monkeypatch.setattr(
        computation_runtime_identity.importlib.metadata,
        "distributions",
        lambda: _runtime_distributions(extras=(("FastAPI", "0.139.3"),)),
    )

    with pytest.raises(
        RuntimeManifestError,
        match="runtime dependencies do not match",
    ):
        load_runtime_identity(manifest_path, root=repository)


def test_runtime_manifest_fails_closed_before_exceeding_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extras = tuple(
        (
            f"runtime-package-{index:03d}-{'x' * 96}",
            f"1+{'a' * 60}",
        )
        for index in range(100)
    )
    monkeypatch.setattr(
        computation_runtime_identity.importlib.metadata,
        "distributions",
        lambda: _runtime_distributions(extras=extras),
    )

    with pytest.raises(RuntimeManifestError, match="exceeds the permitted size"):
        computation_runtime_identity.build_runtime_manifest(
            Path(__file__).resolve().parents[1]
        )


def test_readiness_calls_the_complete_pinned_dependency_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computation_service._core_interfaces_available.cache_clear()
    monkeypatch.setattr(
        computation,
        "assert_computation_dependencies",
        lambda: None,
    )
    assert computation_service._core_interfaces_available() is True

    def unavailable() -> None:
        raise computation.ComputationDependencyError("do not expose this detail")

    monkeypatch.setattr(computation, "assert_computation_dependencies", unavailable)
    computation_service._core_interfaces_available.cache_clear()
    assert computation_service._core_interfaces_available() is False
    computation_service._core_interfaces_available.cache_clear()


def test_installed_pinned_runtime_passes_the_real_readiness_probe() -> None:
    _require_pinned_runtime()
    computation_service._core_interfaces_available.cache_clear()
    assert computation_service._core_interfaces_available() is True
    computation_service._core_interfaces_available.cache_clear()


@pytest.mark.parametrize(
    ("content", "content_type", "expected_status"),
    [
        (b"", "application/json", 422),
        (b"{", "application/json", 422),
        (b"[]", "application/json", 422),
        (b'{"a":1,"a":2}', "application/json", 422),
        (b'{"value":NaN}', "application/json", 422),
        (b"{}", "text/plain", 415),
    ],
)
def test_transport_rejects_ambiguous_or_non_json_requests_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    content_type: str,
    expected_status: int,
) -> None:
    def must_not_run(*_args: object) -> bytes:
        raise AssertionError("invalid input reached the computation child")

    monkeypatch.setattr(computation_service, "_execute_isolated", must_not_run)
    with TestClient(_service_app()) as client:
        response = client.post(
            "/v0/compute",
            content=content,
            headers={"Content-Type": content_type},
        )
    assert response.status_code == expected_status


def test_request_body_is_stream_bounded_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*_args: object) -> bytes:
        raise AssertionError("oversized input reached the computation child")

    monkeypatch.setattr(computation_service, "_execute_isolated", must_not_run)
    body = b'{"padding":"' + (b"x" * computation_service.MAX_REQUEST_BYTES) + b'"}'
    with TestClient(_service_app()) as client:
        response = client.post(
            "/v0/compute",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {
        "detail": "The computation request exceeds the permitted size."
    }


@pytest.mark.parametrize(
    ("path", "payload", "expected_operation", "expected_timeout"),
    [
        (
            "/v0/compute",
            {"profile": {"family": "algebraic"}},
            "compute",
            computation_service.ALGEBRAIC_TIMEOUT_SECONDS,
        ),
        (
            "/v0/validate",
            {"blueprint": {"profile": {"family": "unit"}}},
            "validate",
            computation_service.NUMERIC_TIMEOUT_SECONDS,
        ),
    ],
)
def test_valid_json_is_dispatched_with_family_specific_deadline(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    payload: dict[str, Any],
    expected_operation: str,
    expected_timeout: float,
) -> None:
    observed: dict[str, Any] = {}

    def fake_execute(operation: str, body: bytes, timeout: float) -> bytes:
        observed.update(
            operation=operation,
            payload=json.loads(body),
            timeout=timeout,
        )
        return b'{"result":"bounded"}'

    monkeypatch.setattr(computation_service, "_execute_isolated", fake_execute)
    with TestClient(_service_app()) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"result": "bounded"}
    assert observed == {
        "operation": expected_operation,
        "payload": payload,
        "timeout": expected_timeout,
    }


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (422, "The computation request was rejected."),
        (500, "The computation request failed closed."),
        (503, "The computation engine dependency is unavailable."),
        (504, "The computation request exceeded its time limit."),
    ],
)
def test_child_failures_are_sanitized_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    detail: str,
) -> None:
    def fail(*_args: object) -> bytes:
        raise computation_service._IsolatedExecutionError(status_code, detail)

    monkeypatch.setattr(computation_service, "_execute_isolated", fail)
    with TestClient(_service_app()) as client:
        response = client.post("/v0/compute", json={})
    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_admission_control_allows_only_one_live_computation_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_execute(*_args: object) -> bytes:
        started.set()
        if not release.wait(timeout=2):
            raise AssertionError("test did not release the computation child")
        return b'{"result":"bounded"}'

    monkeypatch.setattr(computation_service, "_execute_isolated", slow_execute)

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=_service_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://assessment-computation",
        ) as client:
            first_task = asyncio.create_task(
                client.post("/v0/compute", json=_numeric_blueprint())
            )
            child_started = await asyncio.to_thread(started.wait, 1)
            assert child_started is True
            try:
                second = await client.post("/v0/compute", json=_numeric_blueprint())
            finally:
                release.set()
            first = await first_task
        return first, second

    first, second = asyncio.run(exercise())
    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json() == {
        "detail": "The computation service is at its execution limit."
    }


def test_real_spawned_child_computes_and_validates_typed_models() -> None:
    blueprint = _numeric_blueprint()
    with TestClient(_service_app()) as client:
        computed = client.post("/v0/compute", json=blueprint)
        validated = client.post(
            "/v0/validate",
            json={
                "blueprint": blueprint,
                "candidate_expression": {"kind": "integer", "integer": 2},
            },
        )

    assert computed.status_code == 200
    result = computed.json()
    assert result["schema_version"] == "assessment-computation-v0"
    assert result["operation"] == "evaluate"
    assert result["exact_value"] == "2"
    assert result["numeric_value"] == "2"
    assert len(result["blueprint_hash"]) == 64

    assert validated.status_code == 200
    report = validated.json()
    assert report["status"] == "validated"
    assert report["result"]["blueprint_hash"] == result["blueprint_hash"]
    assert {check["status"] for check in report["checks"]} == {"passed"}
    assert report["limitations"] == [
        "Validation covers structured computational assertions only.",
        "Source alignment, wording, accessibility, and pedagogy require human review.",
    ]


def test_real_spawned_child_converts_a_qualified_ucum_unit_within_limits() -> None:
    _require_pinned_runtime()
    blueprint = {
        "schema_version": "assessment-computation-v0",
        "profile": {"family": "unit", "delivery": "numerical"},
        "operation": "convert_unit",
        "expression": {"kind": "integer", "integer": 100},
        "source_unit": "cm",
        "target_unit": "m",
    }
    with TestClient(_service_app()) as client:
        response = client.post("/v0/compute", json=blueprint)

    assert response.status_code == 200
    result = response.json()
    assert result["operation"] == "convert_unit"
    assert result["numeric_value"] == "1"
    assert result["target_unit"] == "m"


def test_compute_budget_excludes_child_startup() -> None:
    _require_pinned_runtime()
    # Children are spawned, so each pays for a fresh interpreter plus the
    # SymPy/Pint/ucumvert imports -- seconds of work before any computation.
    # This budget is far smaller than that startup but ample for the conversion
    # itself, so it passes only while the startup allowance is accounted for
    # separately. When the deadline still started before process.start(), this
    # returned 504, which is exactly how every request failed on a modest host.
    body = json.dumps(
        {
            "schema_version": "assessment-computation-v0",
            "profile": {"family": "unit", "delivery": "numerical"},
            "operation": "convert_unit",
            "expression": {"kind": "integer", "integer": 100},
            "source_unit": "cm",
            "target_unit": "m",
        }
    ).encode("utf-8")

    payload = computation_service._execute_isolated("compute", body, 0.5)

    result = json.loads(payload)
    assert result["numeric_value"] == "1"
    assert result["target_unit"] == "m"


def test_real_spawned_child_rejects_unknown_fields_without_echoing_input() -> None:
    blueprint = {**_numeric_blueprint(), "external_url": "https://evil.example"}
    with TestClient(_service_app()) as client:
        response = client.post("/v0/compute", json=blueprint)

    assert response.status_code == 422
    assert response.json() == {"detail": "The computation request was rejected."}
    assert "evil.example" not in response.text


def test_child_refuses_to_send_an_oversized_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedResult:
        def model_dump_json(self, **_kwargs: object) -> str:
            return json.dumps({"result": "x" * computation_service.MAX_RESPONSE_BYTES})

    class RecordingSender:
        def __init__(self) -> None:
            self.messages: list[bytes] = []
            self.closed = False

        def send_bytes(self, message: bytes) -> None:
            self.messages.append(message)

        def close(self) -> None:
            self.closed = True

    sender = RecordingSender()
    monkeypatch.setattr(computation_service, "_apply_resource_limits", lambda _: None)
    monkeypatch.setattr(
        computation_service, "_dispatch", lambda *_args: OversizedResult()
    )
    computation_service._child_entrypoint(
        sender,  # type: ignore[arg-type]
        "compute",
        b"{}",
        computation_service.NUMERIC_TIMEOUT_SECONDS,
    )
    # The child now announces readiness once its pinned runtime is imported,
    # before doing any work, so the caller's compute budget excludes startup.
    assert sender.messages == [computation_service._CHILD_READY, b"S"]
    assert sender.closed is True


def test_container_overlay_has_no_network_or_host_port_and_shares_only_socket() -> None:
    repository = Path(__file__).resolve().parents[1]
    dockerfile = (repository / "Dockerfile.compute").read_text()
    compose = (repository / "docker-compose.computation.yml").read_text()

    assert "USER assessment-compute" in dockerfile
    assert "umask 000" in dockerfile
    assert "/run/assessment-computation/compute.sock" in dockerfile
    assert "/app/evaluation" in dockerfile
    assert "/usr/local/lib/python3.12/site-packages/app" in dockerfile
    assert "/usr/local/lib/python3.12/site-packages/evaluation" in dockerfile
    assert "/usr/local/bin/assessment-ai-evaluate" in dockerfile
    assert "find_spec('evaluation') is None" in dockerfile
    assert "network_mode: none" in compose
    assert "profiles:" in compose
    assert "- computation" in compose
    assert "depends_on:" not in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "env_file:" not in compose
    assert "ports:" not in compose
    assert (
        "ASSESSMENT_AI_COMPUTATION_SOCKET_PATH: "
        "/run/assessment-computation/compute.sock"
    ) in compose
    assert (
        "ASSESSMENT_AI_COMPUTATION_IMAGE_REFERENCE: "
        "${ASSESSMENT_COMPUTATION_IMAGE_REFERENCE:-"
        "assessment-computation:v0-local}"
    ) in compose
    assert (
        compose.count(
            "${ASSESSMENT_COMPUTATION_IMAGE_REFERENCE:-assessment-computation:v0-local}"
        )
        == 2
    )
    assert "ASSESSMENT_COMPUTATION_IMAGE_DIGEST" not in compose


def test_zero_umask_makes_the_shared_socket_connectable_across_container_uids() -> None:
    with tempfile.TemporaryDirectory(prefix="ltc-", dir="/tmp") as directory:
        socket_path = Path(directory) / "compute.sock"
        previous_umask = os.umask(0)
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(str(socket_path))
        finally:
            os.umask(previous_umask)
            listener.close()
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o777
