from __future__ import annotations

import json
import base64
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.parameterized import (
    ParameterizedCompileError,
    evaluate_parameterized_answer,
    parameterized_constraints_satisfied,
)
from app.schemas import AssessmentItemType
from evaluation.engine_probe import (
    IMathASProbeClient,
    WebWorkProbeClient,
    run_imathas_probes,
    run_webwork_probes,
)
from evaluation.fixtures import build_parameter_spec, build_seed_plan
from evaluation.models import EngineProbeReceipt
from evaluation.validators import validate_engine_probe_receipts


IMAGE = "sha256:" + "a" * 64
ATTESTATION = "b" * 64
ADAPTER_IMAGE = "sha256:" + "c" * 64


def test_runtime_value_evaluation_rejects_preview_substitution_and_bad_grid() -> None:
    spec = build_parameter_spec(AssessmentItemType.WEBWORK, 0)

    assert evaluate_parameterized_answer(spec, {"mass": 1, "speed": 8}) == 8
    assert parameterized_constraints_satisfied(spec, {"mass": 1, "speed": 8})
    with pytest.raises(ParameterizedCompileError):
        evaluate_parameterized_answer(spec, {"mass": 1.5, "speed": 8})
    with pytest.raises(ParameterizedCompileError):
        evaluate_parameterized_answer(spec, {"mass": 1, "speed": 8, "extra": 2})


@pytest.mark.asyncio
async def test_webwork_probe_uses_runtime_values_not_python_preview(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        form = parse_qs(request.content.decode())
        answer = form.get("AnSwEr0001", [None])[0]
        score = None if answer is None else (1 if float(answer) == 8 else 0)
        score_input = (
            ""
            if score is None
            else f'<input name="problem-result-score" value="{score}">'
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><body>Find momentum for mass "
                "<script type='math/tex'>1</script> and speed "
                f"<script type='math/tex'>8</script>. {score_input}</body></html>"
            ),
        )

    case = next(
        case
        for case in build_seed_plan("runtime-truth")
        if case.item_type == AssessmentItemType.WEBWORK and case.seed == 1
    )
    assert case.expected_answer != 8
    output = tmp_path / "probes.jsonl"
    client = WebWorkProbeClient(transport=httpx.MockTransport(handler))

    attempted, passed = await run_webwork_probes(
        [case],
        output=output,
        engine_image_sha256=IMAGE,
        network_isolation_attestation_sha256=ATTESTATION,
        concurrency=1,
        client=client,
    )

    assert (attempted, passed, calls) == (1, 1, 4)
    receipt = EngineProbeReceipt.model_validate_json(output.read_text())
    assert receipt.expected_score == 1
    assert receipt.wrong_score == 0
    assert receipt.deterministic
    assert not receipt.final_receipt_ready
    assert set(receipt.remaining_checks) == {
        "persisted_grade_match",
        "object_idempotent",
        "cross_owner_access_blocked",
    }

    incomplete = validate_engine_probe_receipts([receipt])
    assert not incomplete.passed
    assert incomplete.counts["failed_receipts"] == 0
    assert any("4,000" in failure for failure in incomplete.failures)


@pytest.mark.asyncio
async def test_webwork_probe_is_resumable(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "Find momentum for mass 1 and speed 8. "
                '<input name="problem-result-score" value="1">'
            ),
        )

    case = next(
        case
        for case in build_seed_plan("resume")
        if case.item_type == AssessmentItemType.WEBWORK
    )
    output = tmp_path / "probes.jsonl"
    receipt = EngineProbeReceipt(
        run_id=case.run_id,
        item_id=case.item_id,
        item_type=case.item_type,
        seed=case.seed,
        compiler_version=case.compiler_version,
        source_sha256=case.source_sha256,
        endpoint_host="wwrenderer.libretexts.dev",
        engine_image_sha256=IMAGE,
        network_isolation_attestation_sha256=ATTESTATION,
        deterministic=True,
        constraints_satisfied=True,
        rendered=True,
        render_duration_ms=1,
        warning_count=0,
        error_count=0,
        expected_answer_accepted=True,
        wrong_answer_rejected=True,
        expected_score=1,
        wrong_score=0,
        remaining_checks=[
            "persisted_grade_match",
            "object_idempotent",
            "cross_owner_access_blocked",
        ],
    )
    output.write_text(json.dumps(receipt.model_dump(mode="json")) + "\n")

    attempted, passed = await run_webwork_probes(
        [case],
        output=output,
        engine_image_sha256=IMAGE,
        network_isolation_attestation_sha256=ATTESTATION,
        client=WebWorkProbeClient(transport=httpx.MockTransport(handler)),
    )

    assert (attempted, passed) == (0, 0)
    assert len(output.read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_imathas_probe_uses_runtime_values_and_idempotent_object(
    tmp_path: Path,
) -> None:
    create_calls = 0
    initial_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls, initial_calls
        if request.url.host == "127.0.0.1" and request.url.path == "/v1/questions":
            create_calls += 1
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"question_id": 17, "created": create_calls == 1},
            )
        if request.url.host == "127.0.0.1":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<iframe src='https://imathas.libretexts.dev/imathas/"
                    "embedq2.php?jwt=opaque'></iframe>"
                ),
            )
        if request.method == "GET":
            initial_calls += 1
            display = {
                "html": (
                    "Find momentum for mass `1` and speed `8`. "
                    '<input type="text" name="qn5" id="qn5">'
                )
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    f'<input name="state" value="state-{initial_calls}">'
                    f"<script>showandinit(5,{json.dumps(display)});</script>"
                ),
            )
        form = parse_qs(request.content.decode())
        score = 1 if float(form["qn5"][0]) == 8 else 0
        payload = base64.urlsafe_b64encode(
            json.dumps({"score": score, "errors": []}).encode()
        ).rstrip(b"=").decode()
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jwt": f"header.{payload}.signature"},
        )

    case = next(
        case
        for case in build_seed_plan("imathas-runtime-truth")
        if case.item_type == AssessmentItemType.IMATHAS and case.seed == 1
    )
    assert case.expected_answer != 8
    output = tmp_path / "imathas-probes.jsonl"
    client = IMathASProbeClient(
        bridge_token="bridge-token",
        adapt_jwe_secret="jwe-secret",
        transport=httpx.MockTransport(handler),
        problem_token_factory=lambda _secret, _question, _seed: "problem-token",
    )

    attempted, passed = await run_imathas_probes(
        [case],
        output=output,
        engine_image_sha256=IMAGE,
        adapter_image_sha256=ADAPTER_IMAGE,
        network_isolation_attestation_sha256=ATTESTATION,
        concurrency=1,
        client=client,
    )

    assert (attempted, passed, create_calls, initial_calls) == (1, 1, 2, 2)
    receipt = EngineProbeReceipt.model_validate_json(output.read_text())
    assert receipt.expected_score == 1
    assert receipt.wrong_score == 0
    assert receipt.object_idempotent_observed is True
    assert receipt.adapter_image_sha256 == ADAPTER_IMAGE
    assert receipt.engine_object_sha256 is not None
