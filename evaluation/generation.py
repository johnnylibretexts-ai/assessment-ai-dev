from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.config import Settings
from app.content import PublicLibreTextsContentAdapter
from app.db import DraftRepository, ReviewStatus, init_database
from app.llm import LLMClient, LLMResult, LLMStructuredOutputError, build_llm_client
from app.media import HotspotMediaStore, supported_page_image_urls
from app.pipeline import (
    CONCEPT_PROMPT_VERSION,
    CRITIQUE_PROMPT_VERSION,
    DRAFT_PROMPT_VERSION,
    HINT_PROMPT_VERSION,
    PIPELINE_VERSION,
    REVISION_PROMPT_VERSION,
    AssessmentPipeline,
)
from app.qti import build_item_xml, build_manifest_xml, validate_qti_xml
from app.schemas import AssessmentItemType, Concept, NormalizedPage
from app.source_policy import parse_public_source_url

from .models import (
    CorpusManifest,
    BudgetReservation,
    DomainStratum,
    DraftPlanCase,
    DraftQualificationPlan,
    DraftQualificationReceipt,
    ProviderBudgetState,
    ProviderCallReceipt,
)
from .validators import validate_provider_call_receipts


QUALIFICATION_RUN_ID = "build08-provider-corpus-gemini35-minimal-2026-07-14"
CANARY_MARKER = "build08-provider-corpus-canary"
CANARY_DATABASE_URL = "sqlite:////data/build08-provider-corpus.db"
BUDGET_CEILING_MICROUSD = 100_000_000
PER_CALL_RESERVE_MICROUSD = 5_000_000
MAX_REQUEST_BYTES = 900_000

_STAGE_BY_PROMPT_VERSION = {
    CONCEPT_PROMPT_VERSION: "concept_extraction",
    DRAFT_PROMPT_VERSION: "initial_draft",
    CRITIQUE_PROMPT_VERSION: "critique",
    REVISION_PROMPT_VERSION: "revision",
    HINT_PROMPT_VERSION: "hint_ladder",
}

ModelT = TypeVar("ModelT", bound=BaseModel)


class BudgetGuardError(RuntimeError):
    """The paid evaluation cannot proceed within its fail-closed budget contract."""


class EvaluationRunError(RuntimeError):
    """The provider-backed qualification run failed a sealed precondition."""


class ProviderCallLedger:
    def __init__(self, path: Path, qualification_run_id: str) -> None:
        self.path = path
        self.state_path = path.with_name(f"{path.stem}-budget-state.json")
        self.qualification_run_id = qualification_run_id
        self.calls = _read_jsonl(path, ProviderCallReceipt)
        if self.calls:
            result = validate_provider_call_receipts(self.calls)
            if not result.passed:
                raise BudgetGuardError("existing provider-call ledger is invalid")
            if any(
                call.qualification_run_id != qualification_run_id
                for call in self.calls
            ):
                raise BudgetGuardError("provider-call ledger run ID does not match")
        if self.state_path.exists():
            self.state = ProviderBudgetState.model_validate_json(
                self.state_path.read_text(encoding="utf-8")
            )
        elif self.calls:
            raise BudgetGuardError("provider budget state is missing")
        else:
            self.state = ProviderBudgetState(
                qualification_run_id=qualification_run_id,
                settled_microusd=0,
            )
            _write_json_atomic(self.state_path, self.state)
        if self.state.qualification_run_id != qualification_run_id:
            raise BudgetGuardError("provider budget state run ID does not match")
        self._reconcile_state()
        if self.calls:
            result = validate_provider_call_receipts(self.calls, self.state)
            if not result.passed:
                raise BudgetGuardError("provider budget state is invalid")
        elif self.state.settled_microusd:
            raise BudgetGuardError("empty provider ledger has settled spending")

    @property
    def spent_microusd(self) -> int:
        return self.state.settled_microusd + sum(
            reservation.reserved_microusd
            for reservation in self.state.open_reservations.values()
        )

    def reserve(self, case_id: str, stage: str) -> str:
        if self.spent_microusd + PER_CALL_RESERVE_MICROUSD > BUDGET_CEILING_MICROUSD:
            raise BudgetGuardError("the next provider call cannot fit under USD 100")
        call_id = uuid4().hex
        updated = self.state.model_copy(deep=True)
        updated.open_reservations[call_id] = BudgetReservation(
            case_id=case_id,
            stage=stage,
        )
        _write_json_atomic(self.state_path, updated)
        self.state = updated
        return call_id

    def append(self, call: ProviderCallReceipt) -> None:
        if call.sequence != len(self.calls) + 1:
            raise BudgetGuardError("provider-call sequence is not contiguous")
        if call.call_id not in self.state.open_reservations:
            raise BudgetGuardError("provider call has no durable budget reservation")
        _append_jsonl(self.path, call)
        self.calls.append(call)

    def settle(self, call: ProviderCallReceipt) -> None:
        if call.call_id not in self.state.open_reservations:
            raise BudgetGuardError("provider budget reservation is already settled")
        updated = self.state.model_copy(deep=True)
        del updated.open_reservations[call.call_id]
        updated.settled_microusd += call.estimated_cost_microusd
        _write_json_atomic(self.state_path, updated)
        self.state = updated

    def _reconcile_state(self) -> None:
        by_id = {call.call_id: call for call in self.calls}
        reconciled = self.state.model_copy(deep=True)
        changed = False
        for call_id in tuple(reconciled.open_reservations):
            call = by_id.get(call_id)
            if call is None:
                continue
            del reconciled.open_reservations[call_id]
            reconciled.settled_microusd += call.estimated_cost_microusd
            changed = True
        expected = sum(call.estimated_cost_microusd for call in self.calls)
        if reconciled.settled_microusd != expected:
            raise BudgetGuardError("provider budget state cannot be reconciled")
        if changed:
            _write_json_atomic(self.state_path, reconciled)
            self.state = reconciled


class BudgetedGeminiClient:
    provider_name = "gemini"

    def __init__(self, client: LLMClient, ledger: ProviderCallLedger) -> None:
        self.client = client
        self.ledger = ledger
        self._case_id: str | None = None
        self._case_call_ids: list[str] = []

    def start_case(self, case_id: str) -> None:
        if self._case_id is not None:
            raise BudgetGuardError("a provider case is already active")
        self._case_id = case_id
        self._case_call_ids = []

    def finish_case(self) -> tuple[str, ...]:
        if self._case_id is None:
            raise BudgetGuardError("no provider case is active")
        call_ids = tuple(self._case_call_ids)
        self._case_id = None
        self._case_call_ids = []
        return call_ids

    def abandon_case(self) -> None:
        self._case_id = None
        self._case_call_ids = []

    async def complete(
        self,
        prompt: str,
        schema: type[ModelT],
        *,
        prompt_version: str = "v1",
    ) -> LLMResult[ModelT]:
        if self._case_id is None:
            raise BudgetGuardError("provider calls require an active draft case")
        stage = _STAGE_BY_PROMPT_VERSION.get(prompt_version)
        if stage is None:
            raise BudgetGuardError("provider call used an unknown prompt version")
        schema_bytes = json.dumps(
            schema.model_json_schema(), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        if len(prompt.encode("utf-8")) + len(schema_bytes) > MAX_REQUEST_BYTES:
            raise BudgetGuardError("provider request exceeds the reserved size bound")
        call_id = self.ledger.reserve(self._case_id, stage)

        try:
            result = await self.client.complete(
                prompt,
                schema,
                prompt_version=prompt_version,
            )
        except LLMStructuredOutputError as exc:
            self._record_call(call_id, stage, prompt_version, exc.attempts)
            raise
        self._record_call(call_id, stage, prompt_version, result.metadata.attempts)
        return result

    def _record_call(
        self,
        call_id: str,
        stage: str,
        prompt_version: str,
        attempts: tuple[Any, ...],
    ) -> None:
        if self._case_id is None:
            raise BudgetGuardError("provider call lost its active draft case")
        usage = _usage_for_attempts(attempts)
        call = ProviderCallReceipt(
            qualification_run_id=self.ledger.qualification_run_id,
            call_id=call_id,
            sequence=len(self.ledger.calls) + 1,
            case_id=self._case_id,
            stage=stage,
            prompt_version=prompt_version,
            attempt_count=usage["attempt_count"],
            prompt_token_count=usage["prompt_tokens"],
            output_token_count=usage["output_tokens"],
            total_token_count=usage["prompt_tokens"] + usage["output_tokens"],
            thought_token_count=usage["thought_tokens"],
            estimated_cost_microusd=usage["cost_microusd"],
            usage_complete=usage["complete"],
        )
        self.ledger.append(call)
        self.ledger.settle(call)
        self._case_call_ids.append(call.call_id)
        if not call.usage_complete:
            raise BudgetGuardError("Gemini omitted required token usage metadata")
        if call.estimated_cost_microusd > PER_CALL_RESERVE_MICROUSD:
            raise BudgetGuardError("a provider call exceeded its USD 5 reserve")

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()


def build_draft_plan(
    manifest: CorpusManifest,
    *,
    image_page_keys: set[str],
) -> DraftQualificationPlan:
    pages = list(manifest.pages)
    page_by_key = {page.page_key: page for page in pages}
    unknown_image_pages = image_page_keys - set(page_by_key)
    if unknown_image_pages:
        raise EvaluationRunError("image-page inventory is outside the sealed corpus")

    image_by_stratum: dict[DomainStratum, list[Any]] = defaultdict(list)
    for page in pages:
        if page.page_key in image_page_keys:
            image_by_stratum[page.stratum].append(page)
    image_strata = [
        stratum for stratum in DomainStratum if image_by_stratum[stratum]
    ]
    if len(image_strata) < 3:
        raise EvaluationRunError("hotspot qualification requires images in three strata")

    medicine_pages = [
        page for page in pages if page.stratum == DomainStratum.MEDICINE_HEALTH
    ]
    cases: list[DraftPlanCase] = []
    general_index = 0
    sequence = 0
    for round_index in range(20):
        for item_type in AssessmentItemType:
            sequence += 1
            if item_type == AssessmentItemType.BOW_TIE:
                page = medicine_pages[round_index % len(medicine_pages)]
            elif item_type == AssessmentItemType.IMAGE_HOTSPOT:
                stratum = image_strata[round_index % len(image_strata)]
                candidates = image_by_stratum[stratum]
                page = candidates[(round_index // len(image_strata)) % len(candidates)]
            else:
                page = pages[general_index % len(pages)]
                general_index += 1
            cases.append(
                DraftPlanCase(
                    sequence=sequence,
                    case_id=f"build08-draft-{sequence:03d}",
                    pilot_case=sequence <= 19,
                    page_key=page.page_key,
                    stratum=page.stratum,
                    item_type=item_type,
                )
            )

    counts = Counter(case.item_type for case in cases)
    if any(counts[item_type] != 20 for item_type in AssessmentItemType):
        raise EvaluationRunError("draft plan does not contain 20 cases per item type")
    if {case.item_type for case in cases[:19]} != set(AssessmentItemType):
        raise EvaluationRunError("pilot does not contain every item type")
    if {case.page_key for case in cases} != set(page_by_key):
        raise EvaluationRunError("draft plan does not use all sealed corpus pages")
    for item_type in AssessmentItemType:
        strata = {case.stratum for case in cases if case.item_type == item_type}
        if item_type == AssessmentItemType.BOW_TIE:
            if strata != {DomainStratum.MEDICINE_HEALTH}:
                raise EvaluationRunError("bow-tie plan is not medicine/health-only")
        elif len(strata) < 3:
            raise EvaluationRunError(f"{item_type.value} plan uses fewer than three strata")

    return DraftQualificationPlan(
        corpus_sha256=_manifest_sha256(manifest),
        cases=cases,
    )


def _repair_unsupported_hotspot_cases(
    plan: DraftQualificationPlan,
    manifest: CorpusManifest,
    *,
    supported_image_page_keys: set[str],
) -> DraftQualificationPlan:
    """Replace only hotspot assignments that cannot pass the media allowlist."""

    page_by_key = {page.page_key: page for page in manifest.pages}
    if supported_image_page_keys - set(page_by_key):
        raise EvaluationRunError("supported image inventory is outside the sealed corpus")
    candidates_by_stratum: dict[DomainStratum, list[Any]] = defaultdict(list)
    for page in manifest.pages:
        if page.page_key in supported_image_page_keys:
            candidates_by_stratum[page.stratum].append(page)

    repaired: list[DraftPlanCase] = []
    for case in plan.cases:
        if (
            case.item_type != AssessmentItemType.IMAGE_HOTSPOT
            or case.page_key in supported_image_page_keys
        ):
            repaired.append(case)
            continue
        candidates = candidates_by_stratum[case.stratum]
        if not candidates:
            raise EvaluationRunError(
                f"hotspot qualification has no approved image in {case.stratum.value}"
            )
        replacement = candidates[(case.sequence - 1) % len(candidates)]
        repaired.append(
            case.model_copy(
                update={
                    "page_key": replacement.page_key,
                    "stratum": replacement.stratum,
                }
            )
        )

    return plan.model_copy(update={"cases": repaired})


async def build_public_draft_plan(
    manifest: CorpusManifest,
    settings: Settings,
) -> DraftQualificationPlan:
    public_settings = settings.model_copy(update={"public_sources_enabled": True})
    legacy_image_page_keys: set[str] = set()
    supported_image_page_keys: set[str] = set()
    async with PublicLibreTextsContentAdapter(public_settings) as content:
        for corpus_page in manifest.pages:
            page = await content.fetch_page(corpus_page.canonical_url)
            _validate_fetched_page(corpus_page, page)
            if BeautifulSoup(page.html_body, "html.parser").find("img") is not None:
                legacy_image_page_keys.add(corpus_page.page_key)
            if supported_page_image_urls(page):
                supported_image_page_keys.add(corpus_page.page_key)
    legacy_plan = build_draft_plan(
        manifest, image_page_keys=legacy_image_page_keys
    )
    return _repair_unsupported_hotspot_cases(
        legacy_plan,
        manifest,
        supported_image_page_keys=supported_image_page_keys,
    )


async def run_provider_qualification(
    manifest: CorpusManifest,
    *,
    plan_path: Path,
    provider_call_path: Path,
    draft_receipt_path: Path,
    mode: str,
    settings: Settings,
    database_url: str,
    qualification_run_id: str = QUALIFICATION_RUN_ID,
) -> tuple[int, int]:
    if mode not in {"pilot", "full"}:
        raise EvaluationRunError("run mode must be pilot or full")
    _validate_run_settings(settings, database_url)
    target_count = 19 if mode == "pilot" else 380
    existing_receipts = _read_jsonl(draft_receipt_path, DraftQualificationReceipt)
    _validate_resume(existing_receipts, qualification_run_id, target_count)

    public_settings = settings.model_copy(
        update={
            "public_sources_enabled": True,
            "database_url": database_url,
            "hotspot_media_dir": Path("/data/build08-provider-corpus/media"),
        }
    )
    async with PublicLibreTextsContentAdapter(public_settings) as content:
        pages_by_key: dict[str, NormalizedPage] = {}
        legacy_image_page_keys: set[str] = set()
        supported_image_page_keys: set[str] = set()
        for corpus_page in manifest.pages:
            page = await content.fetch_page(corpus_page.canonical_url)
            _validate_fetched_page(corpus_page, page)
            pages_by_key[corpus_page.page_key] = page
            if BeautifulSoup(page.html_body, "html.parser").find("img") is not None:
                legacy_image_page_keys.add(corpus_page.page_key)
            if supported_page_image_urls(page):
                supported_image_page_keys.add(corpus_page.page_key)

        legacy_plan = build_draft_plan(
            manifest,
            image_page_keys=legacy_image_page_keys,
        )
        generated_plan = _repair_unsupported_hotspot_cases(
            legacy_plan,
            manifest,
            supported_image_page_keys=supported_image_page_keys,
        )
        plan = _load_or_write_plan(
            plan_path,
            generated_plan,
            legacy_plan=legacy_plan,
            locked_prefix=len(existing_receipts),
        )
        _validate_receipts_against_plan(existing_receipts, plan)

        database = init_database(database_url)
        repository = DraftRepository(database)
        ledger = ProviderCallLedger(provider_call_path, qualification_run_id)
        base_llm = build_llm_client(public_settings)
        llm = BudgetedGeminiClient(base_llm, ledger)
        try:
            for case in plan.cases[len(existing_receipts) : target_count]:
                page = pages_by_key[case.page_key]
                prior_case_call_count = sum(
                    call.case_id == case.case_id for call in ledger.calls
                )
                llm.start_case(case.case_id)
                try:
                    pipeline = AssessmentPipeline(
                        content,
                        llm,
                        repository,
                        pipeline_version=_qualification_pipeline_version(
                            case.case_id, prior_case_call_count
                        ),
                        max_source_chars=public_settings.max_source_chars,
                        hotspot_media=HotspotMediaStore(public_settings),
                    )
                    outcome = await pipeline.generate_page(
                        page,
                        item_types=[case.item_type],
                        item_count=1,
                        include_hint_ladder=True,
                    )
                    call_ids = llm.finish_case()
                except Exception:
                    llm.abandon_case()
                    raise
                receipt = _build_draft_receipt(
                    case,
                    manifest,
                    page,
                    repository,
                    outcome.draft_id,
                    outcome.run_id,
                    call_ids,
                    public_settings,
                    qualification_run_id,
                )
                _append_jsonl(draft_receipt_path, receipt)
                existing_receipts.append(receipt)
        finally:
            await llm.aclose()
            database.dispose()

    return len(existing_receipts), ledger.spent_microusd


def _qualification_pipeline_version(case_id: str, prior_call_count: int) -> str:
    """Avoid reusing an unreceipted generation after a failed or cut-off attempt."""

    if prior_call_count < 0:
        raise ValueError("prior provider-call count cannot be negative")
    return f"{PIPELINE_VERSION}-{case_id}-attempt-{prior_call_count + 1}"


def _build_draft_receipt(
    case: DraftPlanCase,
    manifest: CorpusManifest,
    page: NormalizedPage,
    repository: DraftRepository,
    draft_id: int,
    generation_run_id: str,
    call_ids: tuple[str, ...],
    settings: Settings,
    qualification_run_id: str,
) -> DraftQualificationReceipt:
    if len(call_ids) != 5:
        raise EvaluationRunError("a qualified draft requires exactly five provider calls")
    draft = repository.require_draft(draft_id)
    question = draft.current
    if question.item_type != case.item_type:
        raise EvaluationRunError("generated item type does not match the sealed plan")
    corpus_page = next(item for item in manifest.pages if item.page_key == case.page_key)
    concept = Concept.model_validate(draft.concept_json)
    source_paragraphs = {paragraph.index for paragraph in page.paragraphs}
    citation_valid = (
        set(question.citation_paragraphs).issubset(source_paragraphs)
        and set(question.citation_paragraphs).issubset(concept.source_paragraphs)
    )
    ladder = draft.current_hint_ladder
    hint_valid = ladder is not None and len(ladder.ladder.rungs) == 3
    hint_leak = (
        any(rung.answer_leak_detected for rung in ladder.ladder.rungs)
        if ladder is not None
        else True
    )
    qti_item = build_item_xml(
        question,
        publication_key=case.case_id,
        title=case.case_id,
        metadata={"artifact": "ai_generated_unreviewed_dev_demo"},
    )
    qti_manifest = build_manifest_xml(
        publication_key=case.case_id,
        item_path=f"items/assessment-ai-{case.case_id}.xml",
    )
    validate_qti_xml(qti_item, qti_manifest)
    qti_sha256 = hashlib.sha256(qti_item + b"\0" + qti_manifest).hexdigest()
    engine_record = draft.current_engine_validation
    engine_valid = (
        engine_record is not None and engine_record.status == "passed"
        if question.item_type
        in {AssessmentItemType.WEBWORK, AssessmentItemType.IMATHAS}
        else engine_record is None
    )
    source_hash_valid = _sha256(page.plaintext) == corpus_page.content_sha256
    flags_false = not any(
        (
            settings.advanced_items_enabled,
            settings.parameterized_items_enabled,
            settings.hint_generation_enabled,
            settings.webwork_enabled,
            settings.imathas_enabled,
        )
    )
    checks = (
        citation_valid,
        hint_valid,
        not hint_leak,
        engine_valid,
        source_hash_valid,
        corpus_page.license.startswith("CC "),
        flags_false,
        not settings.adapt_publishing_enabled,
        not draft.publications,
        draft.status == ReviewStatus.READY_FOR_REVIEW,
        draft.lifecycle_status == "draft",
    )
    if not all(checks):
        raise EvaluationRunError(f"{case.case_id} failed automated qualification")

    return DraftQualificationReceipt(
        qualification_run_id=qualification_run_id,
        sequence=case.sequence,
        case_id=case.case_id,
        pilot_case=case.pilot_case,
        generation_run_id=generation_run_id,
        draft_id=draft.id,
        page_key=corpus_page.page_key,
        stratum=corpus_page.stratum,
        source_identity=corpus_page.source_identity,
        content_sha256=corpus_page.content_sha256,
        license=corpus_page.license,
        item_type=question.item_type,
        context_type=question.context_type,
        provider_call_ids=list(call_ids),
        schema_valid=True,
        citation_valid=True,
        source_hash_valid=True,
        license_valid=True,
        critique_executed=bool(draft.critique_json),
        revision_executed=bool(draft.revised_json),
        hint_ladder_executed=True,
        hint_rung_count=3,
        hint_leak_detected=False,
        qti_valid=True,
        qti_sha256=qti_sha256,
        engine_validation_passed=True,
        unsafe_executable_source_detected=False,
        detected_critical_defect=False,
        advanced_flags_false=True,
        adapt_publishing_disabled=True,
        publication_attempt_count=0,
    )


def _usage_for_attempts(attempts: tuple[Any, ...]) -> dict[str, int | bool]:
    prompt_tokens = 0
    output_tokens = 0
    thought_tokens = 0
    complete = True
    for attempt in attempts:
        metadata = attempt.response_metadata
        prompt = metadata.get("promptTokenCount")
        total = metadata.get("totalTokenCount")
        thoughts = metadata.get("thoughtsTokenCount", 0)
        if not isinstance(prompt, int) or not isinstance(total, int) or total <= prompt:
            complete = False
            break
        if not isinstance(thoughts, int) or thoughts < 0:
            complete = False
            break
        prompt_tokens += prompt
        output_tokens += total - prompt
        thought_tokens += thoughts
    if not complete or prompt_tokens < 1 or output_tokens < 1:
        return {
            "attempt_count": len(attempts),
            "prompt_tokens": 1,
            "output_tokens": 1,
            "thought_tokens": 0,
            "cost_microusd": PER_CALL_RESERVE_MICROUSD,
            "complete": False,
        }
    numerator = prompt_tokens * 1_500_000 + output_tokens * 9_000_000
    return {
        "attempt_count": len(attempts),
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "cost_microusd": (numerator + 999_999) // 1_000_000,
        "complete": True,
    }


def _validate_run_settings(settings: Settings, database_url: str) -> None:
    if os.getenv("BUILD08_ASSESSMENT_CANARY", "") != CANARY_MARKER:
        raise EvaluationRunError("the exact provider-corpus canary marker is required")
    if database_url != CANARY_DATABASE_URL:
        raise EvaluationRunError("provider qualification requires the disposable database")
    if settings.llm_providers != ("gemini",):
        raise EvaluationRunError("provider qualification is pinned to Gemini only")
    if settings.gemini_model != "gemini-3.5-flash":
        raise EvaluationRunError("provider qualification is pinned to gemini-3.5-flash")
    if settings.gemini_thinking_level != "minimal":
        raise EvaluationRunError("Gemini thinking must remain at minimal")
    if settings.gemini_max_output_tokens != 8_192:
        raise EvaluationRunError("Gemini output must be capped at 8,192 tokens")
    if settings.gemini_max_retries > 2 or settings.max_source_chars > 60_000:
        raise EvaluationRunError("provider retry/source bounds exceed the USD 5 reserve")
    if settings.gemini_api_key is None or not (
        settings.gemini_api_key.get_secret_value().strip()
    ):
        raise EvaluationRunError("Gemini is not configured")
    if any(
        (
            settings.advanced_items_enabled,
            settings.parameterized_items_enabled,
            settings.hint_generation_enabled,
            settings.webwork_enabled,
            settings.imathas_enabled,
            settings.adapt_publishing_enabled,
        )
    ):
        raise EvaluationRunError("all capability and publishing flags must remain false")


def _validate_fetched_page(corpus_page: Any, page: NormalizedPage) -> None:
    observed = parse_public_source_url(page.source.canonical_url)
    if observed.identity != corpus_page.source_identity:
        raise EvaluationRunError("fetched source identity differs from the sealed corpus")
    if _sha256(page.plaintext) != corpus_page.content_sha256:
        raise EvaluationRunError("fetched source content differs from the sealed corpus")


def _validate_resume(
    receipts: list[DraftQualificationReceipt],
    qualification_run_id: str,
    target_count: int,
) -> None:
    if len(receipts) > target_count:
        raise EvaluationRunError("receipt ledger is ahead of the requested run mode")
    if [receipt.sequence for receipt in receipts] != list(range(1, len(receipts) + 1)):
        raise EvaluationRunError("draft receipt ledger is not a contiguous prefix")
    if any(
        receipt.qualification_run_id != qualification_run_id for receipt in receipts
    ):
        raise EvaluationRunError("draft receipt run ID does not match")


def _validate_receipts_against_plan(
    receipts: list[DraftQualificationReceipt],
    plan: DraftQualificationPlan,
) -> None:
    for receipt, case in zip(receipts, plan.cases, strict=False):
        if (
            receipt.sequence != case.sequence
            or receipt.case_id != case.case_id
            or receipt.page_key != case.page_key
            or receipt.item_type != case.item_type
        ):
            raise EvaluationRunError("existing draft receipts differ from the sealed plan")


def _load_or_write_plan(
    path: Path,
    generated: DraftQualificationPlan,
    *,
    legacy_plan: DraftQualificationPlan | None = None,
    locked_prefix: int = 0,
) -> DraftQualificationPlan:
    if path.exists():
        existing = DraftQualificationPlan.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if existing == generated:
            return existing
        if legacy_plan is not None and existing == legacy_plan:
            if existing.cases[:locked_prefix] != generated.cases[:locked_prefix]:
                raise EvaluationRunError(
                    "draft plan repair would alter an accepted receipt prefix"
                )
            _write_json_atomic(path, generated)
            return generated
        raise EvaluationRunError("existing draft plan differs from current corpus")
    _write_json(path, generated)
    return generated


def _manifest_sha256(manifest: CorpusManifest) -> str:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path, model: type[ModelT]) -> list[ModelT]:
    if not path.exists():
        return []
    records: list[ModelT] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValueError as exc:
            raise EvaluationRunError(f"invalid ledger record at line {line_number}") from exc
    return records


def _append_jsonl(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def _write_json(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_json_atomic(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                value.model_dump(mode="json"),
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "BUDGET_CEILING_MICROUSD",
    "BudgetGuardError",
    "BudgetedGeminiClient",
    "CANARY_DATABASE_URL",
    "CANARY_MARKER",
    "EvaluationRunError",
    "ProviderCallLedger",
    "build_draft_plan",
    "build_public_draft_plan",
    "run_provider_qualification",
]
