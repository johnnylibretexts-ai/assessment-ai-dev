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
from .computation import (
    AcceptanceMetrics,
    AlgebraNativeExecutionObservation,
    AlgebraNativeExecutionRequest,
    AlgebraNativeQualificationReceipt,
    AlgebraNativeQualificationReport,
    Build08CompatibilityQualification,
    Build08CompatibilityTrustPolicy,
    CanaryQualificationReport,
    CanaryStageEvent,
    CanaryStageExecutionRequest,
    CanaryStageObserverAttestation,
    CanaryStageReceipt,
    ComputationEvaluationManifest,
    EngineSeedPlanCase,
    LocalCanaryStageArtifacts,
    NativeExecutionObservation,
    NativeExecutionRequest,
    NativeQualificationReceipt,
    NativeQualificationReport,
    NativeQualificationTrustPolicy,
    OfflineMutationQualificationReport,
    PairedDraftEvidence,
    PairedStudyEvidenceLedger,
    PairedStudyQualificationReport,
    QualificationMergedObservationEvidence,
    SmeReviewQualificationReport,
    SmeReviewRecord,
    SafetyMonitorReceipt,
    SpikeSafetyEvidence,
    UcumQualificationReport,
    UcumArtifactEquivalenceAttestation,
    WorkflowEvidenceTrustPolicy,
    WorkflowObservationEvidence,
    WorkflowPositiveReceipt,
    UnixSocketAlgebraNativeExecutor,
    UnixSocketNativeExecutor,
    build_algebra_native_execution_plan,
    build_computation_evaluation_manifest,
    build_computation_seed_plan,
    build_qualification_merged_observation_evidence,
    build_workflow_observation_evidence,
    execute_algebra_native_plan,
    execute_computation_native_plan,
    execute_local_canary_stage,
    execute_workflow_positive_plan,
    load_algebra_native_execution_plan,
    load_algebra_native_qualification_receipts,
    load_build08_adapt_attestations,
    load_build08_compatibility_trust_policy,
    load_build08_engine_probes,
    load_build08_seed_receipts,
    load_canary_stage_execution_request,
    load_canary_stage_receipts,
    load_native_qualification_receipts,
    load_native_qualification_trust_policy,
    load_paired_study_evidence,
    load_sme_review_records,
    load_ucum_artifact_equivalence_attestation,
    load_workflow_evidence_trust_policy,
    load_workflow_positive_receipts,
    qualify_ucum_subset,
    run_offline_mutation_qualification,
    validate_algebra_native_qualification_receipts,
    validate_build08_compatibility_receipts,
    validate_canary_stage_receipts,
    validate_native_qualification_receipts,
    validate_paired_study_evidence,
    validate_sme_review_records,
)
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

    computation_fixtures = commands.add_parser("build-computation-fixtures")
    computation_fixtures.add_argument("--output", type=Path, required=True)

    computation_seed_plan = commands.add_parser("build-computation-seed-plan")
    computation_seed_plan.add_argument("--output", type=Path, required=True)
    computation_seed_plan.add_argument(
        "--run-id", default="assessment-computation-v0-planned"
    )

    computation_mutations = commands.add_parser(
        "run-computation-mutations",
        help="execute all 200 typed mutations locally without native-engine calls",
    )
    computation_mutations.add_argument("--output", type=Path, required=True)

    computation_native = commands.add_parser(
        "validate-computation-native-receipts",
        help="import and validate an offline native-engine receipt ledger",
    )
    computation_native.add_argument("receipts", type=Path)
    computation_native.add_argument("--trust-policy", type=Path, required=True)
    computation_native.add_argument("--output", type=Path, required=True)

    computation_algebra_plan = commands.add_parser(
        "build-computation-algebra-native-plan",
        help="write the exact 40-request qualification-only algebra plan",
    )
    computation_algebra_plan.add_argument("--run-id", required=True)
    computation_algebra_plan.add_argument("--imathas-namespace", required=True)
    computation_algebra_plan.add_argument("--output", type=Path, required=True)

    computation_algebra_execute = commands.add_parser(
        "execute-computation-algebra-native-plan",
        help=(
            "execute one engine half of the sealed algebra plan through a bounded "
            "Unix-socket canary runner"
        ),
    )
    computation_algebra_execute.add_argument("plan", type=Path)
    computation_algebra_execute.add_argument(
        "--engine", choices=("webwork", "imathas"), required=True
    )
    computation_algebra_execute.add_argument(
        "--runner-socket", type=Path, required=True
    )
    computation_algebra_execute.add_argument("--run-id", required=True)
    computation_algebra_execute.add_argument("--output", type=Path, required=True)
    computation_algebra_execute.add_argument(
        "--webwork-engine-image-digest", required=True
    )
    computation_algebra_execute.add_argument(
        "--imathas-engine-image-digest", required=True
    )
    computation_algebra_execute.add_argument(
        "--imathas-adapter-image-digest", required=True
    )
    computation_algebra_execute.add_argument(
        "--network-attestation-sha256", required=True
    )
    computation_algebra_execute.add_argument("--imathas-namespace", required=True)
    computation_algebra_execute.add_argument("--concurrency", type=int, default=4)
    computation_algebra_execute.add_argument("--max-cases", type=int)

    computation_algebra_validate = commands.add_parser(
        "validate-computation-algebra-native-receipts",
        help="validate the exact raw 40-receipt algebra qualification ledger",
    )
    computation_algebra_validate.add_argument("receipts", type=Path)
    computation_algebra_validate.add_argument(
        "--trust-policy", type=Path, required=True
    )
    computation_algebra_validate.add_argument("--output", type=Path, required=True)

    computation_observation_merge = commands.add_parser(
        "merge-computation-qualified-observations",
        help=(
            "derive qualified evaluation observations from exact workflow and "
            "native qualification reports"
        ),
    )
    computation_observation_merge.add_argument(
        "--workflow-evidence", type=Path, required=True
    )
    computation_observation_merge.add_argument(
        "--native-report", type=Path, required=True
    )
    computation_observation_merge.add_argument(
        "--algebra-native-report", type=Path, required=True
    )
    computation_observation_merge.add_argument("--output", type=Path, required=True)

    computation_native_execute = commands.add_parser(
        "execute-computation-native-plan",
        help=(
            "execute sealed typed cases through an isolated Unix-socket canary runner"
        ),
    )
    computation_native_execute.add_argument("seed_plan", type=Path)
    computation_native_execute.add_argument(
        "--engine", choices=("webwork", "imathas"), required=True
    )
    computation_native_execute.add_argument("--runner-socket", type=Path, required=True)
    computation_native_execute.add_argument("--run-id", required=True)
    computation_native_execute.add_argument("--output", type=Path, required=True)
    computation_native_execute.add_argument(
        "--webwork-engine-image-digest", required=True
    )
    computation_native_execute.add_argument(
        "--imathas-engine-image-digest", required=True
    )
    computation_native_execute.add_argument(
        "--imathas-adapter-image-digest", required=True
    )
    computation_native_execute.add_argument(
        "--network-attestation-sha256", required=True
    )
    computation_native_execute.add_argument("--imathas-namespace", required=True)
    computation_native_execute.add_argument("--concurrency", type=int, default=4)
    computation_native_execute.add_argument("--max-cases", type=int)

    computation_workflow_execute = commands.add_parser(
        "execute-computation-workflow-positives",
        help=(
            "derive the exact 100 positive receipts from the isolated computation "
            "Unix socket"
        ),
    )
    computation_workflow_execute.add_argument(
        "--computation-socket",
        type=Path,
        required=True,
    )
    computation_workflow_execute.add_argument(
        "--expected-runtime-manifest-sha256",
        required=True,
    )
    computation_workflow_execute.add_argument("--run-id", required=True)
    computation_workflow_execute.add_argument("--output", type=Path, required=True)

    computation_workflow = commands.add_parser(
        "validate-computation-workflow-evidence",
        help="derive all workflow observations from exact raw receipt ledgers",
    )
    computation_workflow.add_argument("positive_receipts", type=Path)
    computation_workflow.add_argument("--mutation-report", type=Path, required=True)
    computation_workflow.add_argument("--trust-policy", type=Path, required=True)
    computation_workflow.add_argument("--output", type=Path, required=True)

    computation_build08 = commands.add_parser(
        "validate-computation-build08-compatibility",
        help="rebuild the unchanged BUILD-08 suite from its three raw ledgers",
    )
    computation_build08.add_argument("--seed-receipts", type=Path, required=True)
    computation_build08.add_argument("--engine-probes", type=Path, required=True)
    computation_build08.add_argument("--adapt-attestations", type=Path, required=True)
    computation_build08.add_argument("--trust-policy", type=Path, required=True)
    computation_build08.add_argument("--output", type=Path, required=True)

    computation_canary = commands.add_parser(
        "validate-computation-canary-stages",
        help="validate the exact ten-stage disposable canary ledger",
    )
    computation_canary.add_argument("receipts", type=Path)
    computation_canary.add_argument("--output", type=Path, required=True)

    computation_canary_execute = commands.add_parser(
        "execute-local-computation-canary-stage",
        help="derive one stage receipt from a bounded local disposable command",
    )
    computation_canary_execute.add_argument("request", type=Path)
    computation_canary_execute.add_argument(
        "--disposable-root", type=Path, required=True
    )
    computation_canary_execute.add_argument(
        "--working-directory", type=Path, required=True
    )
    computation_canary_execute.add_argument("--input-state", type=Path, required=True)
    computation_canary_execute.add_argument("--output-state", type=Path, required=True)
    computation_canary_execute.add_argument("--event-ledger", type=Path, required=True)
    computation_canary_execute.add_argument(
        "--observer-attestation", type=Path, required=True
    )
    computation_canary_execute.add_argument(
        "--timeout-seconds", type=float, default=300.0
    )
    computation_canary_execute.add_argument("--output", type=Path, required=True)

    computation_ucum = commands.add_parser(
        "qualify-computation-ucum",
        help="check a local checksum-pinned UCUM functional-test artifact",
    )
    computation_ucum.add_argument("--artifact", type=Path)
    computation_ucum.add_argument("--sha256")
    computation_ucum.add_argument("--equivalence-attestation", type=Path)
    computation_ucum.add_argument("--output", type=Path, required=True)

    computation_sme = commands.add_parser(
        "validate-computation-sme-reviews",
        help="validate append-only SME approvals bound to the fixture manifest",
    )
    computation_sme.add_argument("reviews", type=Path)
    computation_sme.add_argument("--output", type=Path, required=True)

    computation_study = commands.add_parser(
        "validate-computation-paired-study",
        help="recompute the blinded paired-study gates from its raw review ledger",
    )
    computation_study.add_argument("ledger", type=Path)
    computation_study.add_argument("--output", type=Path, required=True)

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
    if args.command == "build-computation-fixtures":
        manifest = build_computation_evaluation_manifest()
        _write_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "positive_surfaces": manifest.total_positive_surfaces,
                    "mutations": manifest.total_mutations,
                    "planned_engine_executions": (
                        manifest.total_planned_engine_executions
                    ),
                    "review_status": manifest.provenance.review_status,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "build-computation-seed-plan":
        plan = build_computation_seed_plan(args.run_id)
        _write_jsonl(args.output, plan)
        print(
            json.dumps(
                {
                    "planned": len(plan),
                    "execution_status": "planned_not_executed",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run-computation-mutations":
        report = run_offline_mutation_qualification()
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "executed": report.executed_mutations,
                    "detected": report.detected_mutations,
                    "execution_status": report.execution_status,
                    "native_engine_executions": report.native_engine_executions,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-computation-native-receipts":
        receipts = load_native_qualification_receipts(args.receipts)
        trust_policy = load_native_qualification_trust_policy(args.trust_policy)
        report = validate_native_qualification_receipts(
            receipts,
            trust_policy=trust_policy,
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "execution_status": report.execution_status,
                    "imported": report.imported_receipts,
                    "valid": report.valid_receipts,
                    "qualified": report.qualified,
                },
                sort_keys=True,
            )
        )
        return 0 if report.qualified else 1
    if args.command == "build-computation-algebra-native-plan":
        plan = build_algebra_native_execution_plan(
            run_id=args.run_id,
            imathas_namespace=args.imathas_namespace,
        )
        _write_jsonl(args.output, plan)
        print(
            json.dumps(
                {
                    "planned": len(plan),
                    "execution_status": "planned_not_executed",
                    "production_delivery_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "execute-computation-algebra-native-plan":
        plan = load_algebra_native_execution_plan(args.plan)
        if {case.run_id for case in plan} != {args.run_id}:
            raise ValueError("algebra native plan run ID does not match --run-id")
        attempted, written = asyncio.run(
            execute_algebra_native_plan(
                plan,
                output=args.output,
                executor=UnixSocketAlgebraNativeExecutor(args.runner_socket),
                engine=args.engine,
                run_id=args.run_id,
                webwork_engine_image_digest=args.webwork_engine_image_digest,
                imathas_engine_image_digest=args.imathas_engine_image_digest,
                imathas_adapter_image_digest=args.imathas_adapter_image_digest,
                network_attestation_sha256=args.network_attestation_sha256,
                imathas_namespace=args.imathas_namespace,
                concurrency=args.concurrency,
                max_cases=args.max_cases,
            )
        )
        print(
            json.dumps(
                {
                    "attempted": attempted,
                    "receipts_written": written,
                    "engine": args.engine,
                    "qualification_only": True,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-computation-algebra-native-receipts":
        report = validate_algebra_native_qualification_receipts(
            load_algebra_native_qualification_receipts(args.receipts),
            trust_policy=load_native_qualification_trust_policy(args.trust_policy),
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "execution_status": report.execution_status,
                    "imported": report.imported_receipts,
                    "valid": report.valid_receipts,
                    "qualified": report.qualified,
                    "production_delivery_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 0 if report.qualified else 1
    if args.command == "merge-computation-qualified-observations":
        evidence = build_qualification_merged_observation_evidence(
            WorkflowObservationEvidence.model_validate(
                _read_json(args.workflow_evidence)
            ),
            native_qualification=NativeQualificationReport.model_validate(
                _read_json(args.native_report)
            ),
            algebra_native_qualification=(
                AlgebraNativeQualificationReport.model_validate(
                    _read_json(args.algebra_native_report)
                )
            ),
        )
        _write_json(args.output, evidence)
        print(
            json.dumps(
                {
                    "observations": len(evidence.observations),
                    "upgraded_positives": evidence.upgraded_positive_count,
                    "solution_set_production_delivery_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "execute-computation-native-plan":
        plan = _read_jsonl(args.seed_plan, EngineSeedPlanCase)
        if {case.run_id for case in plan} != {args.run_id}:
            raise ValueError("typed seed plan run ID does not match --run-id")
        attempted, written = asyncio.run(
            execute_computation_native_plan(
                plan,
                output=args.output,
                executor=UnixSocketNativeExecutor(args.runner_socket),
                engine=args.engine,
                run_id=args.run_id,
                webwork_engine_image_digest=args.webwork_engine_image_digest,
                imathas_engine_image_digest=args.imathas_engine_image_digest,
                imathas_adapter_image_digest=args.imathas_adapter_image_digest,
                network_attestation_sha256=args.network_attestation_sha256,
                imathas_namespace=args.imathas_namespace,
                concurrency=args.concurrency,
                max_cases=args.max_cases,
            )
        )
        print(
            json.dumps(
                {
                    "attempted": attempted,
                    "receipts_written": written,
                    "engine": args.engine,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "execute-computation-workflow-positives":
        receipts = asyncio.run(
            execute_workflow_positive_plan(
                socket_path=args.computation_socket,
                expected_runtime_manifest_sha256=(
                    args.expected_runtime_manifest_sha256
                ),
                output=args.output,
                run_id=args.run_id,
            )
        )
        print(
            json.dumps(
                {
                    "receipts_written": len(receipts),
                    "case_service_responses_observed": sum(
                        receipt.service_response_count for receipt in receipts
                    ),
                    "transport": "unix_socket",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-computation-workflow-evidence":
        mutation_report = OfflineMutationQualificationReport.model_validate(
            _read_json(args.mutation_report)
        )
        report = build_workflow_observation_evidence(
            load_workflow_positive_receipts(args.positive_receipts),
            mutation_qualification=mutation_report,
            trust_policy=load_workflow_evidence_trust_policy(args.trust_policy),
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "observations": len(report.observations),
                    "raw_ledger_bound": True,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-computation-build08-compatibility":
        report = validate_build08_compatibility_receipts(
            load_build08_seed_receipts(args.seed_receipts),
            load_build08_engine_probes(args.engine_probes),
            load_build08_adapt_attestations(args.adapt_attestations),
            trust_policy=load_build08_compatibility_trust_policy(args.trust_policy),
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "execution_status": report.execution_status,
                    "valid": report.valid_receipts,
                    "qualified": report.qualified,
                },
                sort_keys=True,
            )
        )
        return 0 if report.qualified else 1
    if args.command == "execute-local-computation-canary-stage":
        if args.output.is_symlink() or args.output.exists():
            raise SystemExit("canary stage receipt output must not pre-exist")
        output_identity = args.output.resolve(strict=False)
        evidence_identities = {
            path.resolve(strict=False)
            for path in (
                args.request,
                args.input_state,
                args.output_state,
                args.event_ledger,
                args.observer_attestation,
            )
        }
        if output_identity in evidence_identities:
            raise SystemExit(
                "canary stage receipt output must not overwrite source evidence"
            )
        receipt = asyncio.run(
            execute_local_canary_stage(
                load_canary_stage_execution_request(args.request),
                LocalCanaryStageArtifacts(
                    working_directory=args.working_directory,
                    input_state=args.input_state,
                    output_state=args.output_state,
                    raw_event_ledger=args.event_ledger,
                    observer_attestation=args.observer_attestation,
                    timeout_seconds=args.timeout_seconds,
                ),
                disposable_root=args.disposable_root,
            )
        )
        _write_json(args.output, receipt)
        print(
            json.dumps(
                {
                    "stage": receipt.stage,
                    "sequence": receipt.sequence,
                    "receipt_sha256": receipt.receipt_sha256,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-computation-canary-stages":
        report = validate_canary_stage_receipts(
            load_canary_stage_receipts(args.receipts)
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "execution_status": report.execution_status,
                    "stages": report.imported_stages,
                    "qualified": report.qualified,
                },
                sort_keys=True,
            )
        )
        return 0 if report.qualified else 1
    if args.command == "qualify-computation-ucum":
        if (args.artifact is None) != (args.sha256 is None):
            raise SystemExit("--artifact and --sha256 must be supplied together")
        if args.equivalence_attestation is not None and args.artifact is None:
            raise SystemExit(
                "--equivalence-attestation requires --artifact and --sha256"
            )
        report = qualify_ucum_subset(
            args.artifact,
            expected_sha256=args.sha256,
            equivalence_attestation=(
                load_ucum_artifact_equivalence_attestation(args.equivalence_attestation)
                if args.equivalence_attestation is not None
                else None
            ),
        )
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "artifact_status": report.artifact_status,
                    "qualification_status": report.qualification_status,
                    "executed": report.subset_cases_executed,
                    "qualified": report.subset_qualified,
                },
                sort_keys=True,
            )
        )
        return 0 if report.subset_qualified else 1
    if args.command == "validate-computation-sme-reviews":
        report = validate_sme_review_records(load_sme_review_records(args.reviews))
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "execution_status": report.execution_status,
                    "reviewed": report.reviewed_targets,
                    "approved": report.approved_targets,
                    "qualified": report.qualified,
                },
                sort_keys=True,
            )
        )
        return 0 if report.qualified else 1
    if args.command == "validate-computation-paired-study":
        report = validate_paired_study_evidence(load_paired_study_evidence(args.ledger))
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "execution_status": report.execution_status,
                    "concepts": report.concept_count,
                    "provider_cost_usd": report.provider_cost_usd,
                    "qualified": report.qualified,
                },
                sort_keys=True,
            )
        )
        return 0 if report.qualified else 1
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
        budget_state = ProviderBudgetState.model_validate(_read_json(args.budget_state))
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
        "computation-evaluation-manifest.schema.json": ComputationEvaluationManifest,
        "computation-seed-plan.schema.json": EngineSeedPlanCase,
        "computation-mutation-report.schema.json": OfflineMutationQualificationReport,
        "computation-native-receipt.schema.json": NativeQualificationReceipt,
        "computation-native-execution-request.schema.json": NativeExecutionRequest,
        "computation-native-execution-observation.schema.json": (
            NativeExecutionObservation
        ),
        "computation-native-trust-policy.schema.json": NativeQualificationTrustPolicy,
        "computation-native-report.schema.json": NativeQualificationReport,
        "computation-algebra-native-execution-request.schema.json": (
            AlgebraNativeExecutionRequest
        ),
        "computation-algebra-native-execution-observation.schema.json": (
            AlgebraNativeExecutionObservation
        ),
        "computation-algebra-native-receipt.schema.json": (
            AlgebraNativeQualificationReceipt
        ),
        "computation-algebra-native-report.schema.json": (
            AlgebraNativeQualificationReport
        ),
        "computation-ucum-report.schema.json": UcumQualificationReport,
        "computation-ucum-equivalence-attestation.schema.json": (
            UcumArtifactEquivalenceAttestation
        ),
        "computation-sme-review.schema.json": SmeReviewRecord,
        "computation-sme-review-report.schema.json": SmeReviewQualificationReport,
        "computation-paired-draft.schema.json": PairedDraftEvidence,
        "computation-paired-study.schema.json": PairedStudyEvidenceLedger,
        "computation-paired-study-report.schema.json": PairedStudyQualificationReport,
        "computation-workflow-observations.schema.json": WorkflowObservationEvidence,
        "computation-qualified-observations.schema.json": (
            QualificationMergedObservationEvidence
        ),
        "computation-workflow-positive-receipt.schema.json": WorkflowPositiveReceipt,
        "computation-workflow-trust-policy.schema.json": WorkflowEvidenceTrustPolicy,
        "computation-build08-native-evidence.schema.json": (
            Build08CompatibilityQualification
        ),
        "computation-build08-trust-policy.schema.json": (
            Build08CompatibilityTrustPolicy
        ),
        "computation-canary-execution.schema.json": CanaryStageExecutionRequest,
        "computation-canary-event.schema.json": CanaryStageEvent,
        "computation-canary-observer.schema.json": CanaryStageObserverAttestation,
        "computation-canary-stage.schema.json": CanaryStageReceipt,
        "computation-canary-report.schema.json": CanaryQualificationReport,
        "computation-safety-monitor.schema.json": SafetyMonitorReceipt,
        "computation-safety-evidence.schema.json": SpikeSafetyEvidence,
        "computation-acceptance-metrics.schema.json": AcceptanceMetrics,
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
