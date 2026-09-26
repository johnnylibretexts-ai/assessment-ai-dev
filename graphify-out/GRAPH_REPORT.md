# Graph Report - assessment-ai  (2026-07-12)

## Corpus Check
- 39 files · ~39,071 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 701 nodes · 2900 edges · 20 communities (17 shown, 3 thin omitted)
- Extraction: 76% EXTRACTED · 24% INFERRED · 0% AMBIGUOUS · INFERRED: 702 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e5b4db79`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_DraftRepository|DraftRepository]]
- [[_COMMUNITY_QuestionDraft|QuestionDraft]]
- [[_COMMUNITY_PublicSourceValidationError|PublicSourceValidationError]]
- [[_COMMUNITY_test_llm.py|test_llm.py]]
- [[_COMMUNITY_schemas.py|schemas.py]]
- [[_COMMUNITY_NormalizedPage|NormalizedPage]]
- [[_COMMUNITY_test_pipeline.py|test_pipeline.py]]
- [[_COMMUNITY_Settings|Settings]]
- [[_COMMUNITY_create_app|create_app]]
- [[_COMMUNITY_main.py|main.py]]
- [[_COMMUNITY_jobs.py|jobs.py]]
- [[_COMMUNITY_IMathASBridgeClient|IMathASBridgeClient]]
- [[_COMMUNITY_HotspotMediaStore|HotspotMediaStore]]
- [[_COMMUNITY_Design QA — generation progress state|Design QA — generation progress state]]
- [[_COMMUNITY_LibreTexts Assessment AI|LibreTexts Assessment AI]]
- [[_COMMUNITY___init__.py|__init__.py]]
- [[_COMMUNITY_IMPLEMENTATION_NOTES|IMPLEMENTATION_NOTES.md]]
- [[_COMMUNITY_libretexts-assessment-ai|libretexts-assessment-ai]]

## God Nodes (most connected - your core abstractions)
1. `Settings` - 115 edges
2. `QuestionDraft` - 97 edges
3. `NormalizedPage` - 85 edges
4. `DraftRepository` - 75 edges
5. `Draft` - 63 edges
6. `Concept` - 52 edges
7. `Critique` - 50 edges
8. `ReviewStatus` - 49 edges
9. `create_app()` - 45 edges
10. `HintLadderDraft` - 45 edges

## Surprising Connections (you probably didn't know these)
- `Answer` --uses--> `Settings`  [INFERRED]
  tests/test_llm.py → app/config.py
- `_FailingProvider` --uses--> `Settings`  [INFERRED]
  tests/test_llm.py → app/config.py
- `_SuccessfulProvider` --uses--> `Settings`  [INFERRED]
  tests/test_llm.py → app/config.py
- `FakeAdapt` --uses--> `Settings`  [INFERRED]
  tests/test_publishing.py → app/config.py
- `FakeContent` --uses--> `Draft`  [INFERRED]
  tests/test_pipeline.py → app/db.py

## Import Cycles
- None detected.

## Communities (20 total, 3 thin omitted)

### Community 0 - "DraftRepository"
Cohesion: 0.06
Nodes (82): _actor(), _analyze_hint_leaks(), _answer_fragments(), Base, _clear_confirmations(), _completed_generation_for_source(), ConcurrentDraftUpdateError, Database (+74 more)

### Community 1 - "QuestionDraft"
Cohesion: 0.06
Nodes (76): AdaptAmbiguousError, AdaptClient, AdaptCreateResult, AdaptDestination, AdaptPublishingError, _bow_tie_responses(), build_mcq_payload(), _choice_responses() (+68 more)

### Community 2 - "PublicSourceValidationError"
Cohesion: 0.07
Nodes (88): build_content_adapter(), _child_flag(), ContentAdapterError, ContentAuthenticationError, ContentConfigurationError, ContentLimitError, ContentNotFoundError, ContentResponseError (+80 more)

### Community 3 - "test_llm.py"
Cohesion: 0.07
Nodes (61): build_llm_client(), _concise_validation_error(), _decode_json_candidates(), GeminiClient, generation_status(), _json_for_audit(), LLMAttemptMetadata, LLMCallMetadata (+53 more)

### Community 4 - "schemas.py"
Cohesion: 0.11
Nodes (44): build_assessment_payload(), build_external_engine_payload(), Map a constrained, prevalidated external-engine item to ADAPT., Map a reviewed typed item to ADAPT's verified create contract., _compile_imathas(), compile_parameterized_item(), _compile_webwork(), CompiledParameterizedItem (+36 more)

### Community 5 - "NormalizedPage"
Cohesion: 0.12
Nodes (27): normalized_page_hash(), Hash source content, independent of fetch time and transport metadata., _bounded_source_excerpt(), _concept_prompt(), _critique_prompt(), _draft_prompt(), GenerationOutcome, _hint_prompt() (+19 more)

### Community 6 - "test_pipeline.py"
Cohesion: 0.17
Nodes (35): canonicalize_source_path(), Immutable-by-key source identity with the latest fetched representation., Return one stable sandbox path or host-qualified public page identity., SourceSnapshot, AssessmentPipeline, CitationValidationError, One-page/one-MCQ generation skeleton with a mandatory refinement pass., BloomLevel (+27 more)

### Community 7 - "Settings"
Cohesion: 0.09
Nodes (12): _pinned_dev_url(), Runtime settings. Secrets are read at runtime and never serialized., Settings, BaseSettings, MonkeyPatch, test_adapt_publishing_health_state_is_independent_and_requires_full_config(), test_adapt_target_is_pinned_to_the_dev_api(), test_cxone_scope_is_not_runtime_expandable() (+4 more)

### Community 8 - "create_app"
Cohesion: 0.33
Nodes (18): create_app(), SecretStr, page(), public_page(), Path, question(), sandbox_page(), seed() (+10 more)

### Community 9 - "main.py"
Cohesion: 0.18
Nodes (13): get_settings(), _draft_detail(), _draft_summary(), _ensure_sqlite_parent(), Any, Request, _redirect_with_message(), _require_same_origin() (+5 more)

### Community 10 - "jobs.py"
Cohesion: 0.19
Nodes (8): AbstractAsyncContextManager, GenerationWorker, Any, Exception, Single durable SQLite-backed worker for long-running model calls., _safe_error_code(), _validated_item_types(), GenerateRequest

### Community 11 - "IMathASBridgeClient"
Cohesion: 0.26
Nodes (9): EnginePublishingError, IMathASBridgeClient, IMathASQuestion, RuntimeError, MonkeyPatch, Path, settings(), test_imathas_bridge_client_redacts_remote_failure_body() (+1 more)

### Community 12 - "HotspotMediaStore"
Cohesion: 0.29
Nodes (9): _canonical_media_url(), HotspotMediaError, HotspotMediaStore, AsyncBaseTransport, page(), Path, settings(), test_hotspot_image_must_be_discovered_then_is_reencoded_locally() (+1 more)

### Community 13 - "Design QA — generation progress state"
Cohesion: 0.25
Nodes (7): Comparison history, Design QA — generation progress state, Findings, Focused loading-state evidence, Full-view comparison evidence, Implementation checklist, Required fidelity surfaces

### Community 14 - "LibreTexts Assessment AI"
Cohesion: 0.33
Nodes (5): ADAPT publishing and QTI export, LibreTexts Assessment AI, LLM providers, Local development, Page sources

## Knowledge Gaps
- **12 isolated node(s):** `libretexts-assessment-ai`, `P0 implementation contract`, `Page sources`, `LLM providers`, `ADAPT publishing and QTI export` (+7 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Settings` connect `Settings` to `QuestionDraft`, `PublicSourceValidationError`, `test_llm.py`, `create_app`, `main.py`, `jobs.py`, `IMathASBridgeClient`, `HotspotMediaStore`?**
  _High betweenness centrality (0.276) - this node is a cross-community bridge._
- **Why does `QuestionDraft` connect `QuestionDraft` to `DraftRepository`, `schemas.py`, `NormalizedPage`, `test_pipeline.py`, `create_app`, `main.py`?**
  _High betweenness centrality (0.092) - this node is a cross-community bridge._
- **Why does `NormalizedPage` connect `NormalizedPage` to `DraftRepository`, `QuestionDraft`, `PublicSourceValidationError`, `schemas.py`, `test_pipeline.py`, `create_app`, `HotspotMediaStore`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Are the 44 inferred relationships involving `Settings` (e.g. with `AdaptAmbiguousError` and `AdaptClient`) actually correct?**
  _`Settings` has 44 INFERRED edges - model-reasoned connections that need verification._
- **Are the 47 inferred relationships involving `QuestionDraft` (e.g. with `AdaptAmbiguousError` and `AdaptClient`) actually correct?**
  _`QuestionDraft` has 47 INFERRED edges - model-reasoned connections that need verification._
- **Are the 46 inferred relationships involving `NormalizedPage` (e.g. with `ContentAdapterError` and `ContentAuthenticationError`) actually correct?**
  _`NormalizedPage` has 46 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `DraftRepository` (e.g. with `Concept` and `Critique`) actually correct?**
  _`DraftRepository` has 23 INFERRED edges - model-reasoned connections that need verification._