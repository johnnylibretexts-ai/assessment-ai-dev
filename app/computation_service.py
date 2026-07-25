from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import signal
import sys
import time
from functools import lru_cache
from multiprocessing.connection import Connection
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .computation_runtime_identity import (
    RUNTIME_MANIFEST_RESPONSE_HEADER,
    UNAVAILABLE_RUNTIME_MANIFEST_SHA256,
    ComputationRuntimeIdentity,
    RuntimeManifestError,
    load_runtime_identity,
)


SERVICE_NAME = "assessment-computation"
SCHEMA_VERSION = "assessment-computation-v0"

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CHILD_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
MAX_CONCURRENT_CHILDREN = 1

NUMERIC_TIMEOUT_SECONDS = 2.0
ALGEBRAIC_TIMEOUT_SECONDS = 5.0
ADMISSION_TIMEOUT_SECONDS = 0.25
# Children are spawned, not forked, so each one pays for a fresh interpreter
# plus the SymPy/Pint/ucumvert imports (ucumvert also parses the UCUM essence
# XML). That is seconds of startup before any computation happens. It is bounded
# separately from the compute budget above so the timeouts mean what they say:
# without this the 2s numeric budget was spent almost entirely on imports, which
# left roughly no compute allowance on a modest host and made every request 504.
STARTUP_ALLOWANCE_SECONDS = 10.0

_PIPE_OVERHEAD_BYTES = 1
# Startup handshake. Distinct from every response code the child can send
# (O ok, V rejected, D dependency, S oversized, E failed closed).
_CHILD_READY = b"R"
_PROCESS_POLL_SECONDS = 0.05
_PROCESS_EXIT_GRACE_SECONDS = 0.25

_Operation = Literal["compute", "validate"]


class _IsolatedExecutionError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def create_app(
    runtime_identity: ComputationRuntimeIdentity | None = None,
) -> FastAPI:
    if runtime_identity is None:
        try:
            runtime_identity = load_runtime_identity()
        except RuntimeManifestError:
            runtime_identity = None
    application = FastAPI(
        title="LibreTexts Assessment Computation",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.execution_slots = asyncio.Semaphore(MAX_CONCURRENT_CHILDREN)
    application.state.runtime_identity = runtime_identity

    @application.middleware("http")
    async def bind_runtime_identity(
        request: Request,
        call_next: Any,
    ) -> Response:
        response = await call_next(request)
        identity: ComputationRuntimeIdentity | None = request.app.state.runtime_identity
        response.headers[RUNTIME_MANIFEST_RESPONSE_HEADER] = (
            identity.manifest_sha256
            if identity is not None
            else UNAVAILABLE_RUNTIME_MANIFEST_SHA256
        )
        return response

    @application.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse(_service_status("ok", request.app.state.runtime_identity))

    @application.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        if request.app.state.runtime_identity is None:
            return JSONResponse(
                {"detail": "The computation runtime identity is unavailable."},
                status_code=503,
            )
        if not _core_interfaces_available():
            return JSONResponse(
                {"detail": "The computation engine dependency is unavailable."},
                status_code=503,
            )
        return JSONResponse(
            _service_status("ready", request.app.state.runtime_identity)
        )

    @application.post("/v0/compute")
    async def compute(request: Request) -> Response:
        return await _handle_request("compute", request)

    @application.post("/v0/validate")
    async def validate(request: Request) -> Response:
        return await _handle_request("validate", request)

    return application


async def _handle_request(operation: _Operation, request: Request) -> Response:
    try:
        body = await _read_bounded_json_body(request)
        timeout_seconds = _deadline_for_request(operation, body)
        result = await _execute_with_admission_control(
            request,
            operation,
            body,
            timeout_seconds,
        )
    except _IsolatedExecutionError as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return Response(content=result, media_type="application/json")


async def _execute_with_admission_control(
    request: Request,
    operation: _Operation,
    body: bytes,
    timeout_seconds: float,
) -> bytes:
    slots: asyncio.Semaphore = request.app.state.execution_slots
    try:
        await asyncio.wait_for(slots.acquire(), timeout=ADMISSION_TIMEOUT_SECONDS)
    except TimeoutError:
        raise _IsolatedExecutionError(
            503, "The computation service is at its execution limit."
        ) from None

    try:
        execution = asyncio.create_task(
            asyncio.to_thread(
                _execute_isolated,
                operation,
                body,
                timeout_seconds,
            )
        )
    except BaseException:
        slots.release()
        raise

    execution.add_done_callback(
        lambda completed: _release_execution_slot(slots, completed)
    )
    return await asyncio.shield(execution)


def _release_execution_slot(
    slots: asyncio.Semaphore,
    execution: asyncio.Task[bytes],
) -> None:
    slots.release()
    if not execution.cancelled():
        execution.exception()


async def _read_bounded_json_body(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        raise _IsolatedExecutionError(415, "Content-Type must be application/json.")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _IsolatedExecutionError(400, "Content-Length is invalid.") from None
        if declared_length < 0:
            raise _IsolatedExecutionError(400, "Content-Length is invalid.")
        if declared_length > MAX_REQUEST_BYTES:
            raise _IsolatedExecutionError(
                413, "The computation request exceeds the permitted size."
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise _IsolatedExecutionError(
                413, "The computation request exceeds the permitted size."
            )
        body.extend(chunk)
    if not body:
        raise _IsolatedExecutionError(422, "The computation request is empty.")

    try:
        parsed = _load_strict_json(bytes(body))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _IsolatedExecutionError(
            422, "The computation request is not valid JSON."
        ) from None
    if not isinstance(parsed, dict):
        raise _IsolatedExecutionError(
            422, "The computation request must be a JSON object."
        )
    return bytes(body)


def _deadline_for_request(operation: _Operation, body: bytes) -> float:
    try:
        payload = _load_strict_json(body)
        blueprint = payload if operation == "compute" else payload.get("blueprint", {})
        profile = blueprint.get("profile", {})
        family = profile.get("family")
    except (AttributeError, UnicodeDecodeError, ValueError, RecursionError):
        return NUMERIC_TIMEOUT_SECONDS
    if family == "algebraic":
        return ALGEBRAIC_TIMEOUT_SECONDS
    return NUMERIC_TIMEOUT_SECONDS


def _execute_isolated(
    operation: _Operation,
    body: bytes,
    timeout_seconds: float,
) -> bytes:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_entrypoint,
        args=(sender, operation, body, timeout_seconds),
        daemon=True,
        name=f"assessment-computation-{operation}",
    )
    try:
        process.start()
    except BaseException:
        receiver.close()
        sender.close()
        raise _IsolatedExecutionError(
            500, "The computation request failed closed."
        ) from None
    sender.close()

    message: bytes | None = None
    try:
        # Two phases: a bounded startup allowance for the child to boot and
        # import its pinned runtime, then the compute budget proper measured
        # from readiness. A child that dies or fails during startup answers with
        # its own status byte instead of ``_CHILD_READY``.
        first = _await_child_message(receiver, process, STARTUP_ALLOWANCE_SECONDS)
        if first == _CHILD_READY:
            message = _await_child_message(receiver, process, timeout_seconds)
        else:
            message = first
        if message is None:
            if process.is_alive():
                _stop_process(process)
                raise _IsolatedExecutionError(
                    504, "The computation request exceeded its time limit."
                )
            process.join(_PROCESS_EXIT_GRACE_SECONDS)
            if process.exitcode == -getattr(signal, "SIGXCPU", 24):
                raise _IsolatedExecutionError(
                    504, "The computation request exceeded its time limit."
                )
            raise _IsolatedExecutionError(500, "The computation request failed closed.")
    finally:
        receiver.close()
        if process.is_alive():
            process.join(_PROCESS_EXIT_GRACE_SECONDS)
        if process.is_alive():
            _stop_process(process)
        process.close()

    if not message:
        raise _IsolatedExecutionError(500, "The computation request failed closed.")

    code, payload = message[:1], message[1:]
    if code == b"O":
        if not payload or len(payload) > MAX_RESPONSE_BYTES:
            raise _IsolatedExecutionError(
                500, "The computation response exceeded the permitted size."
            )
        return payload
    if code == b"V":
        raise _IsolatedExecutionError(422, "The computation request was rejected.")
    if code == b"D":
        raise _IsolatedExecutionError(
            503, "The computation engine dependency is unavailable."
        )
    if code == b"S":
        raise _IsolatedExecutionError(
            500, "The computation response exceeded the permitted size."
        )
    raise _IsolatedExecutionError(500, "The computation request failed closed.")


def _await_child_message(
    receiver: Connection,
    process: multiprocessing.Process,
    budget_seconds: float,
) -> bytes | None:
    """Wait up to ``budget_seconds`` for one framed message from the child."""

    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if receiver.poll(min(_PROCESS_POLL_SECONDS, max(0.0, remaining))):
            try:
                return receiver.recv_bytes(MAX_RESPONSE_BYTES + _PIPE_OVERHEAD_BYTES)
            except (EOFError, OSError):
                return None
        if not process.is_alive():
            return None
    return None


def _child_entrypoint(
    sender: Connection,
    operation: _Operation,
    body: bytes,
    timeout_seconds: float,
) -> None:
    try:
        # The CPU rlimit has to cover startup too: importing SymPy burns real
        # CPU seconds, so limiting the child to the compute budget alone got it
        # SIGXCPU-killed mid-import and surfaced as a bogus 504.
        _apply_resource_limits(STARTUP_ALLOWANCE_SECONDS + timeout_seconds)
    except BaseException:
        _send_child_message(sender, b"E")
        sender.close()
        return
    try:
        # Pay every fixed startup cost before announcing readiness so the
        # caller's compute deadline covers computation only. Importing this
        # module binds the pinned dependencies onto the computation core; the
        # dependency assertion then forces the UCUM registry to be built (it
        # parses the UCUM essence XML) and verifies the pinned versions and
        # artifact checksum. The registry is built lazily on first use, so
        # without this it would be constructed during the computation and eat
        # the budget just as the imports used to.
        from . import computation_runtime

        computation_runtime.assert_computation_dependencies()
    except ImportError:
        _send_child_message(sender, b"D")
        sender.close()
        return
    except BaseException as exc:
        _send_child_message(sender, _classify_child_exception(exc))
        sender.close()
        return
    _send_child_message(sender, _CHILD_READY)
    try:
        payload = _load_strict_json(body)
        result = _dispatch(operation, payload)
        encoded = result.model_dump_json(by_alias=True).encode("utf-8")
        if not encoded or len(encoded) > MAX_RESPONSE_BYTES:
            _send_child_message(sender, b"S")
            return
        _send_child_message(sender, b"O" + encoded)
    except ImportError:
        _send_child_message(sender, b"D")
    except BaseException as exc:
        code = _classify_child_exception(exc)
        _send_child_message(sender, code)
    finally:
        sender.close()


def _dispatch(operation: _Operation, payload: Any) -> Any:
    from .computation import (
        AssessmentComputationBlueprint,
        ComputationValidationRequest,
    )
    from .computation_runtime import (
        compute_blueprint,
        validate_computation,
    )

    if operation == "compute":
        blueprint = AssessmentComputationBlueprint.model_validate(payload)
        return compute_blueprint(blueprint)
    request = ComputationValidationRequest.model_validate(payload)
    return validate_computation(request)


def _classify_child_exception(exc: BaseException) -> bytes:
    from pydantic import ValidationError

    if isinstance(exc, (ValidationError, ValueError)):
        return b"V"
    try:
        from .computation import (
            ComputationDependencyError,
            ComputationError,
            ComputationUnsupportedError,
            ComputationValidationError,
        )
    except ImportError:
        return b"D"
    if isinstance(exc, ComputationDependencyError):
        return b"D"
    if isinstance(
        exc,
        (ComputationUnsupportedError, ComputationValidationError),
    ):
        return b"V"
    if isinstance(exc, ComputationError):
        return b"E"
    return b"E"


def _apply_resource_limits(timeout_seconds: float) -> None:
    import resource

    cpu_soft = max(1, math.ceil(timeout_seconds))
    _set_resource_limit(resource.RLIMIT_CPU, cpu_soft, cpu_soft + 1)
    _set_resource_limit(resource.RLIMIT_CORE, 0, 0)
    _set_resource_limit(
        resource.RLIMIT_FSIZE,
        MAX_RESPONSE_BYTES + 4096,
        MAX_RESPONSE_BYTES + 4096,
    )
    _set_resource_limit(resource.RLIMIT_NOFILE, 32, 32)
    if sys.platform.startswith("linux"):
        _set_resource_limit(
            resource.RLIMIT_AS,
            MAX_CHILD_ADDRESS_SPACE_BYTES,
            MAX_CHILD_ADDRESS_SPACE_BYTES,
        )
        if hasattr(resource, "RLIMIT_NPROC"):
            _set_resource_limit(resource.RLIMIT_NPROC, 1, 1)


def _set_resource_limit(resource_id: int, soft: int, hard: int) -> None:
    import resource

    _current_soft, current_hard = resource.getrlimit(resource_id)
    if current_hard != resource.RLIM_INFINITY:
        hard = min(hard, current_hard)
    soft = min(soft, hard)
    resource.setrlimit(resource_id, (soft, hard))


def _stop_process(process: multiprocessing.Process) -> None:
    process.terminate()
    process.join(_PROCESS_EXIT_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_PROCESS_EXIT_GRACE_SECONDS)


def _send_child_message(sender: Connection, message: bytes) -> None:
    try:
        sender.send_bytes(message)
    except (BrokenPipeError, EOFError, OSError):
        return


def _load_strict_json(body: bytes) -> Any:
    return json.loads(
        body,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite_json,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


@lru_cache(maxsize=1)
def _core_interfaces_available() -> bool:
    try:
        from .computation import (
            AssessmentComputationBlueprint,
            ComputationValidationRequest,
        )
        from .computation_runtime import (
            assert_computation_dependencies,
            compute_blueprint,
            validate_computation,
        )

        assert_computation_dependencies()
    except Exception:
        return False
    return (
        AssessmentComputationBlueprint is not None
        and ComputationValidationRequest is not None
        and callable(compute_blueprint)
        and callable(validate_computation)
    )


def _service_status(
    status: Literal["ok", "ready"],
    runtime_identity: ComputationRuntimeIdentity | None,
) -> dict[str, str]:
    return {
        "status": status,
        "service": SERVICE_NAME,
        "schema_version": SCHEMA_VERSION,
        "runtime_manifest_sha256": (
            runtime_identity.manifest_sha256
            if runtime_identity is not None
            else UNAVAILABLE_RUNTIME_MANIFEST_SHA256
        ),
    }


app = create_app()
