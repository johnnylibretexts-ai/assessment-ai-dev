from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from app.llm import LLMAttemptMetadata, LLMCallMetadata, LLMResult
from app.pipeline import CONCEPT_PROMPT_VERSION
from app.schemas import AssessmentItemType
from app.source_policy import parse_public_source_url
from evaluation.generation import (
    BudgetedGeminiClient,
    ProviderCallLedger,
    build_draft_plan,
)
from evaluation.models import (
    BudgetReservation,
    CorpusManifest,
    CorpusPage,
    DomainStratum,
    ProviderCallReceipt,
    ProviderBudgetState,
)
from evaluation.validators import validate_provider_call_receipts


class Answer(BaseModel):
    answer: str


class FakeGemini:
    async def complete(
        self,
        _prompt: str,
        schema: type[Answer],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[Answer]:
        value = schema(answer="safe")
        attempt = LLMAttemptMetadata(
            attempt=1,
            raw_response='{"answer":"safe"}',
            response_metadata={
                "promptTokenCount": 20,
                "candidatesTokenCount": 8,
                "totalTokenCount": 28,
            },
        )
        return LLMResult[Answer](
            value=value,
            metadata=LLMCallMetadata(
                provider="gemini",
                model="gemini-2.5-flash-lite",
                prompt_version=prompt_version,
                attempt=1,
                raw_response=attempt.raw_response,
                response_metadata=attempt.response_metadata,
                attempts=(attempt,),
            ),
        )

    async def aclose(self) -> None:
        return None


def test_draft_plan_covers_380_cases_and_uses_all_pages() -> None:
    manifest = _manifest()
    image_keys = {
        "chemistry-0",
        "biology-0",
        "medicine_health-0",
    }

    plan = build_draft_plan(manifest, image_page_keys=image_keys)

    assert len(plan.cases) == 380
    assert {case.item_type for case in plan.cases[:19]} == set(AssessmentItemType)
    assert {case.page_key for case in plan.cases} == {
        page.page_key for page in manifest.pages
    }
    bow_ties = [
        case for case in plan.cases if case.item_type == AssessmentItemType.BOW_TIE
    ]
    assert len(bow_ties) == 20
    assert {case.stratum for case in bow_ties} == {DomainStratum.MEDICINE_HEALTH}


async def test_budgeted_client_writes_usage_without_model_output(tmp_path: Path) -> None:
    path = tmp_path / "provider-calls.jsonl"
    ledger = ProviderCallLedger(path, "test-run")
    client = BudgetedGeminiClient(FakeGemini(), ledger)
    client.start_case("build08-draft-001")

    result = await client.complete(
        "Return a safe answer.",
        Answer,
        prompt_version=CONCEPT_PROMPT_VERSION,
    )
    call_ids = client.finish_case()

    assert result.value.answer == "safe"
    assert len(call_ids) == 1
    assert ledger.spent_microusd == 6
    assert validate_provider_call_receipts(ledger.calls).passed
    payload = path.read_text(encoding="utf-8")
    assert '"answer":"safe"' not in payload
    assert path.stat().st_mode & 0o777 == 0o600


def test_provider_receipts_reject_any_thinking_tokens() -> None:
    call = ProviderCallReceipt(
        qualification_run_id="test-run",
        call_id="1" * 32,
        sequence=1,
        case_id="build08-draft-001",
        stage="concept_extraction",
        prompt_version=CONCEPT_PROMPT_VERSION,
        attempt_count=1,
        prompt_token_count=20,
        output_token_count=11,
        total_token_count=31,
        thought_token_count=3,
        estimated_cost_microusd=7,
    )

    result = validate_provider_call_receipts([call])

    assert not result.passed
    assert result.failures == [
        f"{call.call_id}: Gemini thinking was not fully disabled"
    ]


def test_release_validation_rejects_open_budget_reservations() -> None:
    settled_call = ProviderCallReceipt(
        qualification_run_id="test-run",
        call_id="1" * 32,
        sequence=1,
        case_id="build08-draft-001",
        stage="concept_extraction",
        prompt_version=CONCEPT_PROMPT_VERSION,
        attempt_count=1,
        prompt_token_count=20,
        output_token_count=8,
        total_token_count=28,
        estimated_cost_microusd=6,
    )
    state = ProviderBudgetState(
        qualification_run_id="test-run",
        settled_microusd=6,
        open_reservations={
            "2" * 32: BudgetReservation(
                case_id="build08-draft-001",
                stage="concept_extraction",
            )
        },
    )

    runtime_result = validate_provider_call_receipts([settled_call], state)
    release_result = validate_provider_call_receipts(
        [settled_call], state, require_settled=True
    )

    assert runtime_result.passed
    assert not release_result.passed
    assert release_result.failures == [
        "provider qualification has open budget reservations"
    ]


def _manifest() -> CorpusManifest:
    libraries = {
        DomainStratum.CHEMISTRY: "chem",
        DomainStratum.BIOLOGY: "bio",
        DomainStratum.MATHEMATICS: "math",
        DomainStratum.MEDICINE_HEALTH: "med",
        DomainStratum.HUMANITIES_SOCIAL: "human",
        DomainStratum.SPANISH_FRENCH: "espanol",
    }
    pages: list[CorpusPage] = []
    for stratum, library in libraries.items():
        for index in range(8):
            url = (
                f"https://{library}.libretexts.org/Bookshelves/BUILD08/"
                f"{stratum.value}/{index}"
            )
            location = parse_public_source_url(url)
            pages.append(
                CorpusPage(
                    page_key=f"{stratum.value}-{index}",
                    stratum=stratum,
                    title=f"{stratum.value} {index}",
                    canonical_url=url,
                    source_identity=location.identity,
                    license="CC BY 4.0",
                    content_sha256=_sha(f"content-{stratum.value}-{index}"),
                    paragraph_sha256=[_sha(f"paragraph-{stratum.value}-{index}")],
                )
            )
    return CorpusManifest(pages=pages)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
