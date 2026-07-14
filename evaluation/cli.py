from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .fixtures import build_fixture_bundle, build_seed_plan
from .models import (
    CorpusManifest,
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
        "shadow-receipt.schema.json": ShadowReceipt,
    }
    for filename, model in schemas.items():
        _write_json(output_dir / filename, model.model_json_schema())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
