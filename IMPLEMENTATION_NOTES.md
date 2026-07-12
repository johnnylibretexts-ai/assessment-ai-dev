# P0 implementation contract

These notes capture verified differences between the v2.1 design document and the current live
ADAPT/CXone contracts. The canonical spec now records the audited P1 hinting contract separately
from the working P0 service described here.

- P0 includes minimal SQLite persistence and a review surface; otherwise its human Bloom/difficulty
  gates would have no executable path.
- Content ingestion is public-only. The credential-free reader accepts only the 14 official library
  hosts and can call only the fixed LibreTexts `/endpoint/info` and `/endpoint/contents` proxy
  operations. Sandbox generation and stored sandbox drafts are blocked at the HTTP boundary, the
  adapter factory defaults sandbox support off, and the container receives no CXone credential
  file. The generic mirror client, direct public Deki API, and HTML scraping are not exposed.
- Ollama Cloud remains the first selected hosted runtime, with Gemini configured as the deployed
  fallback and a local Ollama profile retained. Ollama Cloud responses are client-validated because
  its hosted API does not currently enforce JSON-schema output; Gemini uses native JSON Schema and
  is validated again with the same Pydantic contract before persistence.
- ADAPT has no POST framework-sync endpoint. Question alignment is written through the
  `framework_item_sync_question` field on question create/update.
- ADAPT publication uses a dedicated JWT role-5 editor and an owned `my_questions` folder. Caddy
  authentication for the review UI remains a separate gate and does not satisfy that API contract.
  Approval and publication are separate actions. The create response supplies additive question and
  page IDs; deterministic tags provide reconciliation without duplicate POST retries.
- The first curated framework is a committed two-level Chemistry seed (29 chapters and 252 immediate
  page topics) with deterministic UUIDv5 identifiers. The corresponding current numeric ADAPT level
  IDs are resolved and stored with each publication. There is no runtime framework crawl.
- Source licensing uses a longest-prefix verified mapping. The initial Chemistry book is enforced as
  CC BY-NC-SA 3.0; unmapped sources require a reviewer-selected and confirmed license.
- QTI export is a deterministic QTI 3.0.1 assessment-item ZIP, validated offline against pinned
  official 1EdTech schemas. It is separate from ADAPT's internal `qti_json` representation.
- Existing ADAPT hint UI and telemetry support one flat hint, not per-rung progressive reveal. P1
  graduated hinting therefore requires the additive private-fork contract: server-only rung storage
  (never `qti_json`), append-only attempt inputs, one-row-per-request lifecycle telemetry,
  `shown_hints` retained as the legacy penalty marker, and `off | observe | enforce` rollout modes
  with a pilot allowlist. Assessment AI may generate and review ladders behind its own
  disabled-by-default flag, but ADAPT publication must remain off until that consumer contract and
  its no-leak/legacy-parity tests are deployed. Client-reported active time is not enforcement-grade
  and WebWork's JWT path does not provide it, so the first rollout must remain `off` or `observe`.
