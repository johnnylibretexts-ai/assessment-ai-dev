from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from app.config import Settings

from .corpus import build_public_corpus_manifest, load_corpus_source_catalog
from .adapt_seed import build_adapt_seed_items, finalize_seed_receipts
from .adapt_browser import build_adapt_browser_manifest
from .browser_canary import seed_browser_canary
from .engine_probe import IMathASProbeClient, run_imathas_probes, run_webwork_probes
from .fixtures import build_fixture_bundle, build_seed_plan
from .generation import (
    CANARY_DATABASE_URL,
    build_public_draft_plan,
    run_provider_qualification,
)
from .publication_canary import (
    run_publication_recovery_probe,
    seed_publication_canary,
    validate_publication_canary,
)
from .models import (
    CorpusManifest,
    DraftQualificationReceipt,
    DraftQualificationPlan,
    AdaptSeedAttestation,
    AdaptSeedItem,
    EngineProbeReceipt,
    FixtureBundle,
    OutageReceipt,
    QualificationReport,
    ProviderBudgetState,
    ProviderCallReceipt,
    ReviewRecord,
    SeedPlanCase,
    SeedReceipt,
    ShadowReceipt,
)
from .validators import (
    compare_shadow_receipts,
    validate_corpus_manifest,
    validate_draft_qualification_receipts,
    validate_engine_probe_receipts,
    validate_outage_receipts,
    validate_provider_call_receipts,
    validate_review_ledger,
    validate_seed_receipts,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assessment-ai-evaluate",
        description="Deterministic BUILD-08 release-qualification harness",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fixtures = commands.add_parser("build-fixtures")
    fixtures.add_argument("--output", type=Path, required=True)

    browser_canary = commands.add_parser("seed-browser-canary")
    browser_canary.add_argument("--database-url", required=True)
    browser_canary.add_argument("--output", type=Path, required=True)

    publication_canary = commands.add_parser("seed-publication-canary")
    publication_canary.add_argument("--database-url", required=True)
    publication_canary.add_argument("--output", type=Path, required=True)

    publication_recovery = commands.add_parser("probe-publication-recovery")
    publication_recovery.add_argument("--database-url", required=True)
    publication_recovery.add_argument("--output", type=Path, required=True)

    publication_validate = commands.add_parser("validate-publication-canary")
    publication_validate.add_argument("--database-url", required=True)
    publication_validate.add_argument("--output", type=Path, required=True)

    adapt_browser = commands.add_parser("build-adapt-browser-manifest")
    adapt_browser.add_argument("--output", type=Path, required=True)

    seed_plan = commands.add_parser("build-seed-plan")
    seed_plan.add_argument("--output", type=Path, required=True)
    seed_plan.add_argument("--run-id", default="build08-disabled-seed-plan")

    schemas = commands.add_parser("write-schemas")
    schemas.add_argument("--output-dir", type=Path, required=True)

    corpus = commands.add_parser("validate-corpus")
    corpus.add_argument("manifest", type=Path)
    corpus.add_argument("--output", type=Path)

    build_corpus = commands.add_parser("build-corpus")
    build_corpus.add_argument("catalog", type=Path)
    build_corpus.add_argument("--output", type=Path, required=True)
    build_corpus.add_argument("--retry-attempts", type=int, default=3)
    build_corpus.add_argument("--retry-delay-seconds", type=float, default=1.0)

    provider_calls = commands.add_parser("validate-provider-calls")
    provider_calls.add_argument("ledger", type=Path)
    provider_calls.add_argument("--budget-state", type=Path, required=True)
    provider_calls.add_argument("--output", type=Path)

    drafts = commands.add_parser("validate-drafts")
    drafts.add_argument("ledger", type=Path)
    drafts.add_argument("--provider-calls", type=Path, required=True)
    drafts.add_argument("--corpus", type=Path, required=True)
    drafts.add_argument("--mode", choices=("pilot", "full"), default="full")
    drafts.add_argument("--output", type=Path)

    provider_run = commands.add_parser("run-provider-corpus")
    provider_run.add_argument("manifest", type=Path)
    provider_run.add_argument("--plan", type=Path, required=True)
    provider_run.add_argument("--provider-calls", type=Path, required=True)
    provider_run.add_argument("--drafts", type=Path, required=True)
    provider_run.add_argument("--mode", choices=("pilot", "full"), required=True)
    provider_run.add_argument("--database-url", default=CANARY_DATABASE_URL)

    draft_plan = commands.add_parser("build-draft-plan")
    draft_plan.add_argument("manifest", type=Path)
    draft_plan.add_argument("--output", type=Path, required=True)

    reviews = commands.add_parser("validate-reviews")
    reviews.add_argument("ledger", type=Path)
    reviews.add_argument("--output", type=Path)

    seeds = commands.add_parser("validate-seeds")
    seeds.add_argument("receipts", type=Path)
    seeds.add_argument("--output", type=Path)

    engine_probes = commands.add_parser("validate-engine-probes")
    engine_probes.add_argument("receipts", type=Path)
    engine_probes.add_argument("--output", type=Path)

    outages = commands.add_parser("validate-outages")
    outages.add_argument("receipts", type=Path)
    outages.add_argument("--output", type=Path)

    merge_probes = commands.add_parser("merge-engine-probes")
    merge_probes.add_argument("receipts", type=Path, nargs="+")
    merge_probes.add_argument("--output", type=Path, required=True)

    finalize_seeds = commands.add_parser("finalize-seeds")
    finalize_seeds.add_argument("engine_probes", type=Path)
    finalize_seeds.add_argument("adapt_attestations", type=Path)
    finalize_seeds.add_argument("--output", type=Path, required=True)

    seed_items = commands.add_parser("build-adapt-seed-items")
    seed_items.add_argument("engine_probes", type=Path)
    seed_items.add_argument("imathas_ids", type=Path)
    seed_items.add_argument("--output", type=Path, required=True)

    webwork_probes = commands.add_parser("probe-webwork")
    webwork_probes.add_argument("seed_plan", type=Path)
    webwork_probes.add_argument("--output", type=Path, required=True)
    webwork_probes.add_argument("--engine-image-sha256", required=True)
    webwork_probes.add_argument("--network-attestation-sha256", required=True)
    webwork_probes.add_argument("--concurrency", type=int, default=4)
    webwork_probes.add_argument("--max-cases", type=int)

    imathas_probes = commands.add_parser("probe-imathas")
    imathas_probes.add_argument("seed_plan", type=Path)
    imathas_probes.add_argument("--output", type=Path, required=True)
    imathas_probes.add_argument("--engine-image-sha256", required=True)
    imathas_probes.add_argument("--adapter-image-sha256", required=True)
    imathas_probes.add_argument("--network-attestation-sha256", required=True)
    imathas_probes.add_argument("--concurrency", type=int, default=4)
    imathas_probes.add_argument("--max-cases", type=int)

    shadow = commands.add_parser("compare-shadow")
    shadow.add_argument("receipts", type=Path)
    shadow.add_argument("--output", type=Path)

    report = commands.add_parser("report")
    report.add_argument("--corpus", type=Path, required=True)
    report.add_argument("--drafts", type=Path, required=True)
    report.add_argument("--provider-calls", type=Path, required=True)
    report.add_argument("--budget-state", type=Path, required=True)
    report.add_argument("--seeds", type=Path, required=True)
    report.add_argument("--outages", type=Path, required=True)
    report.add_argument("--shadow", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build-fixtures":
        _write_json(args.output, build_fixture_bundle())
        return 0
    if args.command == "seed-browser-canary":
        manifest = seed_browser_canary(
            args.database_url,
            canary_marker=os.getenv("BUILD08_ASSESSMENT_CANARY", ""),
        )
        _write_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "seeded": manifest["fixture_count"],
                    "item_types": manifest["item_type_count"],
                    "contexts": manifest["context_type_count"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "seed-publication-canary":
        manifest = seed_publication_canary(
            args.database_url,
            canary_marker=os.getenv("BUILD08_ASSESSMENT_CANARY", ""),
        )
        _write_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "seeded": manifest["fixture_count"],
                    "item_types": manifest["item_type_count"],
                    "recovery_probes": 1,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "probe-publication-recovery":
        result = asyncio.run(
            run_publication_recovery_probe(
                args.database_url,
                canary_marker=os.getenv("BUILD08_ASSESSMENT_CANARY", ""),
                settings=Settings(),
            )
        )
        _write_json(args.output, result)
        print(json.dumps({"passed": result["passed"]}, sort_keys=True))
        return 0
    if args.command == "validate-publication-canary":
        result = asyncio.run(
            validate_publication_canary(
                args.database_url,
                canary_marker=os.getenv("BUILD08_ASSESSMENT_CANARY", ""),
                settings=Settings(),
            )
        )
        _write_json(args.output, result)
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "item_types": result["item_type_count"],
                    "qti_packages": result["qti_package_count"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "build-adapt-browser-manifest":
        manifest = build_adapt_browser_manifest()
        _write_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "items": manifest["item_type_count"],
                    "native_qti": manifest["native_qti_count"],
                    "external_engines": manifest["external_engine_count"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "build-seed-plan":
        _write_jsonl(args.output, build_seed_plan(args.run_id))
        return 0
    if args.command == "write-schemas":
        _write_schemas(args.output_dir)
        return 0
    if args.command == "validate-corpus":
        result = validate_corpus_manifest(
            CorpusManifest.model_validate(_read_json(args.manifest))
        )
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "build-corpus":
        catalog = load_corpus_source_catalog(args.catalog)
        manifest = asyncio.run(
            build_public_corpus_manifest(
                catalog,
                Settings(),
                retry_attempts=args.retry_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
            )
        )
        _write_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "pages": len(manifest.pages),
                    "strata": len({page.stratum for page in manifest.pages}),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-provider-calls":
        result = validate_provider_call_receipts(
            _read_jsonl(args.ledger, ProviderCallReceipt),
            ProviderBudgetState.model_validate(_read_json(args.budget_state)),
            require_settled=True,
        )
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "validate-drafts":
        result = validate_draft_qualification_receipts(
            _read_jsonl(args.ledger, DraftQualificationReceipt),
            _read_jsonl(args.provider_calls, ProviderCallReceipt),
            CorpusManifest.model_validate(_read_json(args.corpus)),
            mode=args.mode,
        )
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "run-provider-corpus":
        drafts, spent = asyncio.run(
            run_provider_qualification(
                CorpusManifest.model_validate(_read_json(args.manifest)),
                plan_path=args.plan,
                provider_call_path=args.provider_calls,
                draft_receipt_path=args.drafts,
                mode=args.mode,
                settings=Settings(),
                database_url=args.database_url,
            )
        )
        print(
            json.dumps(
                {
                    "drafts": drafts,
                    "spent_microusd": spent,
                    "spent_usd": round(spent / 1_000_000, 6),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "build-draft-plan":
        plan = asyncio.run(
            build_public_draft_plan(
                CorpusManifest.model_validate(_read_json(args.manifest)),
                Settings(),
            )
        )
        _write_json(args.output, plan)
        print(json.dumps({"cases": len(plan.cases)}, sort_keys=True))
        return 0
    if args.command == "validate-reviews":
        result = validate_review_ledger(_read_jsonl(args.ledger, ReviewRecord))
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "validate-seeds":
        result = validate_seed_receipts(_read_jsonl(args.receipts, SeedReceipt))
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "validate-engine-probes":
        result = validate_engine_probe_receipts(
            _read_jsonl(args.receipts, EngineProbeReceipt)
        )
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "validate-outages":
        result = validate_outage_receipts(_read_jsonl(args.receipts, OutageReceipt))
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "merge-engine-probes":
        records = [
            record
            for path in args.receipts
            for record in _read_jsonl(path, EngineProbeReceipt)
        ]
        keys = [(record.item_id, record.seed) for record in records]
        if len(keys) != len(set(keys)):
            raise ValueError("engine probe inputs contain duplicate item/seed pairs")
        records.sort(
            key=lambda record: (record.item_type.value, record.item_id, record.seed)
        )
        _write_jsonl(args.output, records)
        print(json.dumps({"merged": len(records)}, sort_keys=True))
        return 0
    if args.command == "finalize-seeds":
        receipts = finalize_seed_receipts(
            _read_jsonl(args.engine_probes, EngineProbeReceipt),
            _read_jsonl(args.adapt_attestations, AdaptSeedAttestation),
        )
        result = validate_seed_receipts(receipts)
        if not result.passed:
            raise ValueError(
                "finalized seed receipts did not satisfy the release validator: "
                + "; ".join(result.failures)
            )
        _write_jsonl(args.output, receipts)
        print(json.dumps({"finalized": len(receipts), "passed": True}, sort_keys=True))
        return 0
    if args.command == "build-adapt-seed-items":
        raw_mapping = _read_json(args.imathas_ids)
        if not isinstance(raw_mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, int)
            for key, value in raw_mapping.items()
        ):
            raise ValueError(
                "IMathAS object mapping must be a string-to-integer object"
            )
        items = build_adapt_seed_items(
            _read_jsonl(args.engine_probes, EngineProbeReceipt),
            imathas_ids=raw_mapping,
        )
        _write_jsonl(args.output, items)
        print(json.dumps({"items": len(items)}, sort_keys=True))
        return 0
    if args.command == "probe-webwork":
        image = args.engine_image_sha256
        if not image.startswith("sha256:") or len(image) != 71:
            raise ValueError("engine image must be a full sha256 digest")
        attestation = args.network_attestation_sha256
        if len(attestation) != 64 or any(
            character not in "0123456789abcdef" for character in attestation
        ):
            raise ValueError("network attestation must be a lowercase SHA-256")
        attempted, passed = asyncio.run(
            run_webwork_probes(
                _read_jsonl(args.seed_plan, SeedPlanCase),
                output=args.output,
                engine_image_sha256=image,
                network_isolation_attestation_sha256=attestation,
                concurrency=args.concurrency,
                max_cases=args.max_cases,
            )
        )
        print(json.dumps({"attempted": attempted, "passed": passed}, sort_keys=True))
        return 0 if attempted == passed else 2
    if args.command == "probe-imathas":
        _validate_image_digest(args.engine_image_sha256)
        _validate_image_digest(args.adapter_image_sha256)
        _validate_sha256(args.network_attestation_sha256, "network attestation")
        bridge_token = os.getenv("IMATHAS_BRIDGE_TOKEN", "")
        adapt_jwe_secret = os.getenv("IMATHAS_ADAPT_JWE_SECRET", "")
        if not bridge_token or not adapt_jwe_secret:
            raise ValueError("IMathAS probes require isolated runner credentials")
        attempted, passed = asyncio.run(
            run_imathas_probes(
                _read_jsonl(args.seed_plan, SeedPlanCase),
                output=args.output,
                engine_image_sha256=args.engine_image_sha256,
                adapter_image_sha256=args.adapter_image_sha256,
                network_isolation_attestation_sha256=(args.network_attestation_sha256),
                concurrency=args.concurrency,
                max_cases=args.max_cases,
                client=IMathASProbeClient(
                    bridge_token=bridge_token,
                    adapt_jwe_secret=adapt_jwe_secret,
                ),
            )
        )
        print(json.dumps({"attempted": attempted, "passed": passed}, sort_keys=True))
        return 0 if attempted == passed else 2
    if args.command == "compare-shadow":
        result = compare_shadow_receipts(_read_jsonl(args.receipts, ShadowReceipt))
        _emit(result, args.output)
        return 0 if result.passed else 2
    if args.command == "report":
        provider_calls = _read_jsonl(args.provider_calls, ProviderCallReceipt)
        budget_state = ProviderBudgetState.model_validate(
            _read_json(args.budget_state)
        )
        sections = [
            validate_corpus_manifest(
                CorpusManifest.model_validate(_read_json(args.corpus))
            ),
            validate_provider_call_receipts(
                provider_calls, budget_state, require_settled=True
            ),
            validate_draft_qualification_receipts(
                _read_jsonl(args.drafts, DraftQualificationReceipt),
                provider_calls,
                CorpusManifest.model_validate(_read_json(args.corpus)),
            ),
            validate_seed_receipts(_read_jsonl(args.seeds, SeedReceipt)),
            validate_outage_receipts(_read_jsonl(args.outages, OutageReceipt)),
            compare_shadow_receipts(_read_jsonl(args.shadow, ShadowReceipt)),
        ]
        report = QualificationReport(
            passed=all(section.passed for section in sections), sections=sections
        )
        _write_json(args.output, report)
        return 0 if report.passed else 2
    raise AssertionError(f"unhandled command {args.command}")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_image_digest(value: str) -> None:
    if not value.startswith("sha256:") or len(value) != 71:
        raise ValueError("engine image must be a full sha256 digest")
    _validate_sha256(value.removeprefix("sha256:"), "engine image")


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")


def _read_jsonl(path: Path, model: type[ModelT]) -> list[ModelT]:
    records: list[ModelT] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid record") from exc
    return records


def _write_json(path: Path, value: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for value in values
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _emit(value: BaseModel, output: Path | None) -> None:
    if output is not None:
        _write_json(output, value)
        return
    print(value.model_dump_json(indent=2))


def _write_schemas(output_dir: Path) -> None:
    schemas: dict[str, type[BaseModel]] = {
        "corpus-manifest.schema.json": CorpusManifest,
        "provider-call-receipt.schema.json": ProviderCallReceipt,
        "provider-budget-state.schema.json": ProviderBudgetState,
        "draft-qualification-receipt.schema.json": DraftQualificationReceipt,
        "draft-qualification-plan.schema.json": DraftQualificationPlan,
        "fixture-bundle.schema.json": FixtureBundle,
        "review-record.schema.json": ReviewRecord,
        "seed-plan.schema.json": SeedPlanCase,
        "seed-receipt.schema.json": SeedReceipt,
        "adapt-seed-attestation.schema.json": AdaptSeedAttestation,
        "adapt-seed-item.schema.json": AdaptSeedItem,
        "engine-probe-receipt.schema.json": EngineProbeReceipt,
        "outage-receipt.schema.json": OutageReceipt,
        "shadow-receipt.schema.json": ShadowReceipt,
    }
    for filename, model in schemas.items():
        _write_json(output_dir / filename, model.model_json_schema())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
