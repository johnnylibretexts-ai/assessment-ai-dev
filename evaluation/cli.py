from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .engine_probe import IMathASProbeClient, run_imathas_probes, run_webwork_probes
from .fixtures import build_fixture_bundle, build_seed_plan
from .models import (
    CorpusManifest,
    EngineProbeReceipt,
    FixtureBundle,
    QualificationReport,
    ReviewRecord,
    SeedPlanCase,
    SeedReceipt,
    ShadowReceipt,
)
from .validators import (
    compare_shadow_receipts,
    validate_corpus_manifest,
    validate_engine_probe_receipts,
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

    seed_plan = commands.add_parser("build-seed-plan")
    seed_plan.add_argument("--output", type=Path, required=True)
    seed_plan.add_argument("--run-id", default="build08-disabled-seed-plan")

    schemas = commands.add_parser("write-schemas")
    schemas.add_argument("--output-dir", type=Path, required=True)

    corpus = commands.add_parser("validate-corpus")
    corpus.add_argument("manifest", type=Path)
    corpus.add_argument("--output", type=Path)

    reviews = commands.add_parser("validate-reviews")
    reviews.add_argument("ledger", type=Path)
    reviews.add_argument("--output", type=Path)

    seeds = commands.add_parser("validate-seeds")
    seeds.add_argument("receipts", type=Path)
    seeds.add_argument("--output", type=Path)

    engine_probes = commands.add_parser("validate-engine-probes")
    engine_probes.add_argument("receipts", type=Path)
    engine_probes.add_argument("--output", type=Path)

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
    report.add_argument("--reviews", type=Path, required=True)
    report.add_argument("--seeds", type=Path, required=True)
    report.add_argument("--shadow", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build-fixtures":
        _write_json(args.output, build_fixture_bundle())
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
                network_isolation_attestation_sha256=(
                    args.network_attestation_sha256
                ),
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
        sections = [
            validate_corpus_manifest(
                CorpusManifest.model_validate(_read_json(args.corpus))
            ),
            validate_review_ledger(_read_jsonl(args.reviews, ReviewRecord)),
            validate_seed_receipts(_read_jsonl(args.seeds, SeedReceipt)),
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
        "fixture-bundle.schema.json": FixtureBundle,
        "review-record.schema.json": ReviewRecord,
        "seed-plan.schema.json": SeedPlanCase,
        "seed-receipt.schema.json": SeedReceipt,
        "engine-probe-receipt.schema.json": EngineProbeReceipt,
        "shadow-receipt.schema.json": ShadowReceipt,
    }
    for filename, model in schemas.items():
        _write_json(output_dir / filename, model.model_json_schema())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
