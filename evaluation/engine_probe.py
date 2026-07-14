from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.parameterized import (
    compile_parameterized_item,
    evaluate_parameterized_answer,
    parameterized_constraints_satisfied,
)
from app.schemas import AssessmentItemType, ParameterizedItemSpec

from .fixtures import build_parameter_spec
from .models import EngineProbeReceipt, SeedPlanCase


WEBWORK_ENDPOINT = "https://wwrenderer.libretexts.dev/render-api"
IMATHAS_PUBLIC_HOST = "imathas.libretexts.dev"
IMATHAS_BRIDGE_INTERNAL = "http://127.0.0.1:8000"
IMATHAS_INTERNAL = "http://imathas/imathas"
MAX_RENDER_BYTES = 2 * 1024 * 1024
NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
REMAINING_CHECKS = [
    "persisted_grade_match",
    "object_idempotent",
    "cross_owner_access_blocked",
]


class EngineProbeError(RuntimeError):
    pass


class _TextAndInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.inputs: dict[str, str] = {}

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "input":
            return
        values = dict(attrs)
        name = values.get("name")
        if name:
            self.inputs[name] = values.get("value") or ""


class _IframeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "iframe":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


class WebWorkProbeClient:
    def __init__(
        self,
        *,
        endpoint: str = WEBWORK_ENDPOINT,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "wwrenderer.libretexts.dev"
            or parsed.port is not None
            or parsed.username
            or parsed.password
            or parsed.path != "/render-api"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WeBWorK probe endpoint must be the pinned dev renderer")
        self.endpoint = endpoint
        self.endpoint_host = parsed.hostname
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def probe(
        self,
        case: SeedPlanCase,
        *,
        engine_image_sha256: str,
        network_isolation_attestation_sha256: str,
    ) -> EngineProbeReceipt:
        if case.item_type != AssessmentItemType.WEBWORK:
            raise ValueError("WebWorkProbeClient accepts only WeBWorK cases")
        spec, source = _source_for_case(case)
        started = time.monotonic()
        warning_count = 0
        error_count = 0
        failure_stage = "initial_render"
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                first = await self._render(client, source, case.seed)
                failure_stage = "repeat_render"
                second = await self._render(client, source, case.seed)
                failure_stage = "parse_runtime_values"
                first_values = _runtime_values(first, spec)
                second_values = _runtime_values(second, spec)
                failure_stage = "evaluate_runtime_answer"
                expected_answer = evaluate_parameterized_answer(spec, first_values)
                constraints_satisfied = parameterized_constraints_satisfied(
                    spec, first_values
                )
                wrong_answer = expected_answer + max(1.0, abs(expected_answer) * 0.1)
                failure_stage = "submit_expected"
                accepted = await self._render(
                    client, source, case.seed, answer=expected_answer
                )
                failure_stage = "submit_wrong"
                rejected = await self._render(
                    client, source, case.seed, answer=wrong_answer
                )
            expected_score = _score(accepted)
            wrong_score = _score(rejected)
            warning_count = sum(
                _warning_count(payload)
                for payload in (first, second, accepted, rejected)
            )
            error_count = sum(
                _error_count(payload)
                for payload in (first, second, accepted, rejected)
            )
            deterministic = first_values == second_values
            values_payload = json.dumps(
                first_values, sort_keys=True, separators=(",", ":")
            )
            semantic_payload = json.dumps(
                {
                    "values": first_values,
                    "expected_score": expected_score,
                    "wrong_score": wrong_score,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return EngineProbeReceipt(
                run_id=case.run_id,
                item_id=case.item_id,
                item_type=case.item_type,
                seed=case.seed,
                compiler_version=case.compiler_version,
                source_sha256=case.source_sha256,
                endpoint_host=self.endpoint_host,
                engine_image_sha256=engine_image_sha256,
                network_isolation_attestation_sha256=(
                    network_isolation_attestation_sha256
                ),
                runtime_values_sha256=_sha256(values_payload),
                semantic_render_sha256=_sha256(semantic_payload),
                deterministic=deterministic,
                constraints_satisfied=constraints_satisfied,
                rendered=True,
                render_duration_ms=min(
                    120_000, round((time.monotonic() - started) * 1_000)
                ),
                warning_count=warning_count,
                error_count=error_count,
                expected_answer_accepted=expected_score == 1.0,
                wrong_answer_rejected=wrong_score == 0.0,
                expected_score=expected_score,
                wrong_score=wrong_score,
                remaining_checks=REMAINING_CHECKS,
            )
        except Exception:
            return EngineProbeReceipt(
                run_id=case.run_id,
                item_id=case.item_id,
                item_type=case.item_type,
                seed=case.seed,
                compiler_version=case.compiler_version,
                source_sha256=case.source_sha256,
                endpoint_host=self.endpoint_host,
                engine_image_sha256=engine_image_sha256,
                network_isolation_attestation_sha256=(
                    network_isolation_attestation_sha256
                ),
                deterministic=False,
                constraints_satisfied=False,
                rendered=False,
                render_duration_ms=min(
                    120_000, round((time.monotonic() - started) * 1_000)
                ),
                warning_count=warning_count,
                error_count=max(1, error_count),
                expected_answer_accepted=False,
                wrong_answer_rejected=False,
                failure_stage=failure_stage,
                remaining_checks=REMAINING_CHECKS,
            )

    async def _render(
        self,
        client: httpx.AsyncClient,
        source: str,
        seed: int,
        *,
        answer: float | None = None,
    ) -> str:
        form = {
            "permissionLevel": "20",
            "problemSeed": str(seed),
            "outputFormat": "static",
            "problemSource": base64.b64encode(source.encode()).decode(),
        }
        if answer is not None:
            form.update(
                {
                    "answersSubmitted": "1",
                    "showSummary": "1",
                    "showScoreSummary": "1",
                    "AnSwEr0001": format(answer, ".15g"),
                }
            )
        try:
            async with client.stream("POST", self.endpoint, data=form) as response:
                if response.status_code != 200:
                    raise EngineProbeError("renderer returned a non-success status")
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("text/html"):
                    raise EngineProbeError("renderer returned an unexpected content type")
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > MAX_RENDER_BYTES:
                    raise EngineProbeError("renderer response exceeded the size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RENDER_BYTES:
                        raise EngineProbeError("renderer response exceeded the size limit")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EngineProbeError("renderer request failed") from exc
        return body.decode("utf-8", errors="replace")


class IMathASProbeClient:
    def __init__(
        self,
        *,
        bridge_token: str,
        adapt_jwe_secret: str,
        bridge_internal_url: str = IMATHAS_BRIDGE_INTERNAL,
        imathas_internal_url: str = IMATHAS_INTERNAL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        problem_token_factory: Callable[[str, int, int], str] | None = None,
    ) -> None:
        if not bridge_token or not adapt_jwe_secret:
            raise ValueError("IMathAS probe credentials must be non-empty")
        if bridge_internal_url != IMATHAS_BRIDGE_INTERNAL:
            raise ValueError("IMathAS bridge probe must use its loopback endpoint")
        if imathas_internal_url != IMATHAS_INTERNAL:
            raise ValueError("IMathAS probe must use the pinned private engine endpoint")
        self.bridge_token = bridge_token
        self.adapt_jwe_secret = adapt_jwe_secret
        self.bridge_internal_url = bridge_internal_url
        self.imathas_internal_url = imathas_internal_url
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.problem_token_factory = problem_token_factory or _problem_jwe

    async def ensure_question(
        self, case: SeedPlanCase
    ) -> tuple[int, ParameterizedItemSpec, str, bool]:
        spec, source = _imathas_source_for_case(case)
        publication_key = _sha256(
            f"build08-engine-probe:{case.run_id}:{case.item_id}:{case.source_sha256}"
        )
        payload = {
            "publication_key": publication_key,
            "description": f"BUILD-08 engine probe {case.item_id}",
            "author": "LibreTexts Assessment AI",
            "source": source,
            "source_url": (
                "https://math.libretexts.org/Bookshelves/BUILD08/"
                "Parameterized_Engine_Qualification"
            ),
        }
        headers = {"Authorization": f"Bearer {self.bridge_token}"}
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as client:
            first = await client.post(
                f"{self.bridge_internal_url}/v1/questions",
                headers=headers,
                json=payload,
            )
            second = await client.post(
                f"{self.bridge_internal_url}/v1/questions",
                headers=headers,
                json=payload,
            )
        if first.status_code != 200 or second.status_code != 200:
            raise EngineProbeError("IMathAS question creation failed")
        try:
            first_body = first.json()
            second_body = second.json()
            first_id = int(first_body["question_id"])
            second_id = int(second_body["question_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineProbeError("IMathAS bridge returned an invalid question") from exc
        idempotent = first_id == second_id and second_body.get("created") is False
        if first_id < 1 or not idempotent:
            raise EngineProbeError("IMathAS question creation was not idempotent")
        return first_id, spec, source, idempotent

    async def probe(
        self,
        case: SeedPlanCase,
        *,
        question_id: int,
        spec: ParameterizedItemSpec,
        source: str,
        object_idempotent: bool,
        engine_image_sha256: str,
        adapter_image_sha256: str,
        network_isolation_attestation_sha256: str,
    ) -> EngineProbeReceipt:
        started = time.monotonic()
        object_sha = _sha256(f"imathas-question:{question_id}")
        failure_stage = "source_integrity"
        try:
            if hashlib.sha256(source.encode()).hexdigest() != case.source_sha256:
                raise EngineProbeError("IMathAS source changed after question creation")
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                failure_stage = "initial_render"
                first_page = await self._initial_page(client, question_id, case.seed)
                failure_stage = "repeat_render"
                second_page = await self._initial_page(client, question_id, case.seed)
                failure_stage = "parse_runtime_values"
                first_state, first_ref, first_values = _imathas_state(first_page, spec)
                second_state, second_ref, second_values = _imathas_state(
                    second_page, spec
                )
                failure_stage = "evaluate_runtime_answer"
                expected_answer = evaluate_parameterized_answer(spec, first_values)
                constraints_satisfied = parameterized_constraints_satisfied(
                    spec, first_values
                )
                wrong_answer = expected_answer + max(1.0, abs(expected_answer) * 0.1)
                failure_stage = "submit_expected"
                expected_score, expected_errors = await self._submit(
                    client, first_state, first_ref, expected_answer
                )
                failure_stage = "submit_wrong"
                wrong_score, wrong_errors = await self._submit(
                    client, second_state, second_ref, wrong_answer
                )
            values_payload = json.dumps(
                first_values, sort_keys=True, separators=(",", ":")
            )
            semantic_payload = json.dumps(
                {
                    "values": first_values,
                    "expected_score": expected_score,
                    "wrong_score": wrong_score,
                    "object": object_sha,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            error_count = expected_errors + wrong_errors
            return EngineProbeReceipt(
                run_id=case.run_id,
                item_id=case.item_id,
                item_type=case.item_type,
                seed=case.seed,
                compiler_version=case.compiler_version,
                source_sha256=case.source_sha256,
                endpoint_host=IMATHAS_PUBLIC_HOST,
                engine_image_sha256=engine_image_sha256,
                adapter_image_sha256=adapter_image_sha256,
                engine_object_sha256=object_sha,
                object_idempotent_observed=object_idempotent,
                network_isolation_attestation_sha256=(
                    network_isolation_attestation_sha256
                ),
                runtime_values_sha256=_sha256(values_payload),
                semantic_render_sha256=_sha256(semantic_payload),
                deterministic=(
                    first_ref == second_ref and first_values == second_values
                ),
                constraints_satisfied=constraints_satisfied,
                rendered=True,
                render_duration_ms=min(
                    120_000, round((time.monotonic() - started) * 1_000)
                ),
                warning_count=0,
                error_count=error_count,
                expected_answer_accepted=expected_score == 1.0,
                wrong_answer_rejected=wrong_score == 0.0,
                expected_score=expected_score,
                wrong_score=wrong_score,
                remaining_checks=REMAINING_CHECKS,
            )
        except Exception:
            return EngineProbeReceipt(
                run_id=case.run_id,
                item_id=case.item_id,
                item_type=case.item_type,
                seed=case.seed,
                compiler_version=case.compiler_version,
                source_sha256=case.source_sha256,
                endpoint_host=IMATHAS_PUBLIC_HOST,
                engine_image_sha256=engine_image_sha256,
                adapter_image_sha256=adapter_image_sha256,
                engine_object_sha256=object_sha,
                object_idempotent_observed=object_idempotent,
                network_isolation_attestation_sha256=(
                    network_isolation_attestation_sha256
                ),
                deterministic=False,
                constraints_satisfied=False,
                rendered=False,
                render_duration_ms=min(
                    120_000, round((time.monotonic() - started) * 1_000)
                ),
                warning_count=0,
                error_count=1,
                expected_answer_accepted=False,
                wrong_answer_rejected=False,
                failure_stage=failure_stage,
                remaining_checks=REMAINING_CHECKS,
            )

    async def _initial_page(
        self, client: httpx.AsyncClient, question_id: int, seed: int
    ) -> str:
        problem_token = self.problem_token_factory(
            self.adapt_jwe_secret, question_id, seed
        )
        wrapper = await self._bounded_text(
            client,
            "GET",
            f"{self.bridge_internal_url}/adapt/embedq2.php",
            params={"problemJWT": problem_token, "frame_id": "build08-probe"},
        )
        parser = _IframeParser()
        parser.feed(wrapper)
        if len(parser.sources) != 1:
            raise EngineProbeError("IMathAS wrapper did not contain one engine frame")
        public = urlparse(parser.sources[0])
        if (
            public.scheme != "https"
            or public.hostname != IMATHAS_PUBLIC_HOST
            or public.port is not None
            or not public.path.startswith("/imathas/")
        ):
            raise EngineProbeError("IMathAS wrapper returned an unapproved engine URL")
        internal = public._replace(scheme="http", netloc="imathas").geturl()
        return await self._bounded_text(client, "GET", internal)

    async def _submit(
        self,
        client: httpx.AsyncClient,
        state: str,
        question_ref: int,
        answer: float,
    ) -> tuple[float, int]:
        name = f"qn{question_ref}"
        body = await self._bounded_text(
            client,
            "POST",
            f"{self.imathas_internal_url}/embedq2.php",
            data={
                "state": state,
                name: format(answer, ".15g"),
                f"{name}-val": format(answer, ".15g"),
                "toscoreqn": json.dumps({str(question_ref): [0]}),
            },
            expected_content_type="application/json",
        )
        try:
            response = json.loads(body)
            token = response["jwt"]
            payload_segment = token.split(".")[1]
            payload = json.loads(
                base64.urlsafe_b64decode(
                    payload_segment + "=" * (-len(payload_segment) % 4)
                )
            )
            score = float(payload["score"])
            errors = payload.get("errors")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EngineProbeError("IMathAS returned an invalid score payload") from exc
        if not 0 <= score <= 1:
            raise EngineProbeError("IMathAS returned an invalid score")
        error_count = len(errors) if isinstance(errors, list) else int(bool(errors))
        return score, error_count

    async def _bounded_text(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        expected_content_type: str = "text/html",
        **kwargs: object,
    ) -> str:
        try:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    raise EngineProbeError("IMathAS returned a non-success status")
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith(expected_content_type):
                    raise EngineProbeError("IMathAS returned an unexpected content type")
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > MAX_RENDER_BYTES:
                    raise EngineProbeError("IMathAS response exceeded the size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RENDER_BYTES:
                        raise EngineProbeError("IMathAS response exceeded the size limit")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EngineProbeError("IMathAS request failed") from exc
        return body.decode("utf-8", errors="replace")


async def run_webwork_probes(
    cases: Iterable[SeedPlanCase],
    *,
    output: Path,
    engine_image_sha256: str,
    network_isolation_attestation_sha256: str,
    concurrency: int = 4,
    max_cases: int | None = None,
    client: WebWorkProbeClient | None = None,
) -> tuple[int, int]:
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    complete = _completed_keys(output)
    pending = [
        case
        for case in cases
        if case.item_type == AssessmentItemType.WEBWORK
        and (case.item_id, case.seed) not in complete
    ]
    if max_cases is not None:
        pending = pending[:max_cases]
    probe_client = client or WebWorkProbeClient()
    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    passed = 0

    async def execute(case: SeedPlanCase) -> None:
        nonlocal passed
        async with semaphore:
            receipt = await probe_client.probe(
                case,
                engine_image_sha256=engine_image_sha256,
                network_isolation_attestation_sha256=(
                    network_isolation_attestation_sha256
                ),
            )
        if _probe_passed(receipt):
            passed += 1
        async with lock:
            _append_receipt(output, receipt)

    await asyncio.gather(*(execute(case) for case in pending))
    return len(pending), passed


async def run_imathas_probes(
    cases: Iterable[SeedPlanCase],
    *,
    output: Path,
    engine_image_sha256: str,
    adapter_image_sha256: str,
    network_isolation_attestation_sha256: str,
    concurrency: int = 4,
    max_cases: int | None = None,
    client: IMathASProbeClient,
) -> tuple[int, int]:
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    complete = _completed_keys(output)
    pending = [
        case
        for case in cases
        if case.item_type == AssessmentItemType.IMATHAS
        and (case.item_id, case.seed) not in complete
    ]
    if max_cases is not None:
        pending = pending[:max_cases]
    item_cases: dict[str, SeedPlanCase] = {}
    for case in pending:
        item_cases.setdefault(case.item_id, case)
    questions: dict[str, tuple[int, ParameterizedItemSpec, str, bool]] = {}
    for item_id, case in sorted(item_cases.items()):
        questions[item_id] = await client.ensure_question(case)

    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    passed = 0

    async def execute(case: SeedPlanCase) -> None:
        nonlocal passed
        question_id, spec, source, idempotent = questions[case.item_id]
        async with semaphore:
            receipt = await client.probe(
                case,
                question_id=question_id,
                spec=spec,
                source=source,
                object_idempotent=idempotent,
                engine_image_sha256=engine_image_sha256,
                adapter_image_sha256=adapter_image_sha256,
                network_isolation_attestation_sha256=(
                    network_isolation_attestation_sha256
                ),
            )
        if _probe_passed(receipt):
            passed += 1
        async with lock:
            _append_receipt(output, receipt)

    await asyncio.gather(*(execute(case) for case in pending))
    return len(pending), passed


def _source_for_case(case: SeedPlanCase) -> tuple[ParameterizedItemSpec, str]:
    match = re.fullmatch(r"webwork-(\d{2})", case.item_id)
    if not match:
        raise EngineProbeError("seed item ID does not map to a sealed WeBWorK spec")
    index = int(match.group(1)) - 1
    if not 0 <= index < 20:
        raise EngineProbeError("seed item index is outside the sealed fixture set")
    spec = build_parameter_spec(AssessmentItemType.WEBWORK, index)
    compiled = compile_parameterized_item(spec, validation_seeds=100)
    if compiled.source_sha256 != case.source_sha256:
        raise EngineProbeError("compiled source hash does not match the sealed plan")
    return spec, compiled.source


def _imathas_source_for_case(
    case: SeedPlanCase,
) -> tuple[ParameterizedItemSpec, str]:
    match = re.fullmatch(r"imathas-(\d{2})", case.item_id)
    if not match:
        raise EngineProbeError("seed item ID does not map to a sealed IMathAS spec")
    index = int(match.group(1)) - 1
    if not 0 <= index < 20:
        raise EngineProbeError("seed item index is outside the sealed fixture set")
    spec = build_parameter_spec(AssessmentItemType.IMATHAS, index)
    compiled = compile_parameterized_item(spec, validation_seeds=100)
    if compiled.source_sha256 != case.source_sha256:
        raise EngineProbeError("compiled source hash does not match the sealed plan")
    return spec, compiled.source


def _runtime_values(rendered: str, spec: ParameterizedItemSpec) -> dict[str, int | float]:
    parser = _TextAndInputParser()
    parser.feed(rendered)
    text = " ".join(" ".join(parser.text).split())
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(rf"`({NUMBER_PATTERN})`", r"\1", text)
    pattern = re.escape(spec.prompt_template)
    for variable in spec.variables:
        pattern = pattern.replace(re.escape("{" + variable.name + "}"), f"(?P<{variable.name}>{NUMBER_PATTERN})")
    match = re.search(pattern, text)
    if not match:
        raise EngineProbeError("renderer response did not contain the expected prompt")
    values: dict[str, int | float] = {}
    for variable in spec.variables:
        raw = float(match.group(variable.name))
        values[variable.name] = int(raw) if variable.integer and raw.is_integer() else raw
    return values


def _imathas_state(
    rendered: str, spec: ParameterizedItemSpec
) -> tuple[str, int, dict[str, int | float]]:
    parser = _TextAndInputParser()
    parser.feed(rendered)
    state = parser.inputs.get("state")
    if not state or len(state) > 100_000:
        raise EngineProbeError("IMathAS page did not contain bounded state")
    display_match = re.search(r"showandinit\(\d+,\s*", rendered)
    if not display_match:
        raise EngineProbeError("IMathAS page did not contain a rendered question")
    try:
        display, _ = json.JSONDecoder().raw_decode(rendered[display_match.end() :])
        question_html = display["html"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EngineProbeError("IMathAS question display was invalid") from exc
    if not isinstance(question_html, str) or len(question_html) > MAX_RENDER_BYTES:
        raise EngineProbeError("IMathAS question display was invalid")
    question_parser = _TextAndInputParser()
    question_parser.feed(question_html)
    names = [name for name in question_parser.inputs if re.fullmatch(r"qn\d+", name)]
    if len(names) != 1:
        raise EngineProbeError("IMathAS question did not contain one response control")
    question_ref = int(names[0][2:])
    return state, question_ref, _runtime_values(question_html, spec)


def _problem_jwe(secret: str, question_id: int, seed: int) -> str:
    try:
        from jwcrypto import jwe, jwk
    except ImportError as exc:  # pragma: no cover - exercised in engine container
        raise EngineProbeError(
            "IMathAS probes require jwcrypto in the isolated runner"
        ) from exc
    claims = {
        "adapt": {
            "assignment_id": 1,
            "question_id": 1,
            "technology": "imathas",
        },
        "imathas": {"id": question_id, "seed": seed},
    }
    encoded_key = base64.urlsafe_b64encode(secret.encode()).rstrip(b"=").decode()
    token = jwe.JWE(
        json.dumps(claims, separators=(",", ":")).encode(),
        protected={"alg": "PBES2-HS512+A256KW", "enc": "A256GCM", "zip": "DEF"},
    )
    token.add_recipient(jwk.JWK(kty="oct", k=encoded_key))
    return token.serialize(compact=True)


def _score(rendered: str) -> float:
    parser = _TextAndInputParser()
    parser.feed(rendered)
    raw = parser.inputs.get("problem-result-score")
    if raw is None:
        raise EngineProbeError("graded response did not contain a score")
    score = float(raw)
    if not 0 <= score <= 1:
        raise EngineProbeError("renderer returned an invalid score")
    return score


def _warning_count(rendered: str) -> int:
    return len(re.findall(r'class="[^"]*alert-warning\b', rendered, flags=re.I))


def _error_count(rendered: str) -> int:
    indicators = (
        r'class="[^"]*alert-danger\b',
        r'WeBWorK Error',
        r'Problem generation failed',
    )
    # An incorrect submitted answer legitimately uses alert-danger. Count it only
    # when no valid zero score is present.
    count = sum(len(re.findall(pattern, rendered, flags=re.I)) for pattern in indicators)
    if 'name="problem-result-score"' in rendered and 'value="0"' in rendered:
        count = max(0, count - 1)
    return count


def _probe_passed(receipt: EngineProbeReceipt) -> bool:
    return bool(
        receipt.deterministic
        and receipt.constraints_satisfied
        and receipt.rendered
        and receipt.warning_count == 0
        and receipt.error_count == 0
        and receipt.expected_answer_accepted
        and receipt.wrong_answer_rejected
    )


def _completed_keys(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    keys: set[tuple[str, int]] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            receipt = EngineProbeReceipt.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid engine probe") from exc
        key = (receipt.item_id, receipt.seed)
        if key in keys:
            raise ValueError(f"{path}:{line_number}: duplicate engine probe")
        keys.add(key)
    return keys


def _append_receipt(path: Path, receipt: EngineProbeReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt.model_dump(mode="json"), sort_keys=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
