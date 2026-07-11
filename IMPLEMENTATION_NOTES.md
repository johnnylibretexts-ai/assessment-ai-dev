# P0 implementation contract

These notes capture verified differences between the v2.1 design document and the current live
ADAPT/CXone contracts. The canonical spec now records the audited P1 hinting contract separately
from the working P0 service described here.

- P0 includes minimal SQLite persistence and a review surface; otherwise its human Bloom/difficulty
  gates would have no executable path.
- Content ingestion is pinned to `dev.libretexts.org/Sandboxes/johnnyphung`, GET-only, and fails
  closed. The generic mirror client is not exposed.
- Ollama Cloud remains the first selected hosted runtime, with Gemini configured as the deployed
  fallback and a local Ollama profile retained. Ollama Cloud responses are client-validated because
  its hosted API does not currently enforce JSON-schema output; Gemini uses native JSON Schema and
  is validated again with the same Pydantic contract before persistence.
- ADAPT has no POST framework-sync endpoint. Question alignment is written through the
  `framework_item_sync_question` field on question create/update.
- ADAPT publication needs a JWT instructor/editor identity and an owned `my_questions` folder.
  Caddy authentication for the review UI does not satisfy that API contract.
- Existing ADAPT hint UI and telemetry support one flat hint, not per-rung progressive reveal. P1
  graduated hinting therefore requires the additive private-fork contract: server-only rung storage
  (never `qti_json`), append-only attempt inputs, one-row-per-request lifecycle telemetry,
  `shown_hints` retained as the legacy penalty marker, and `off | observe | enforce` rollout modes
  with a pilot allowlist. Assessment AI may generate and review ladders behind its own
  disabled-by-default flag, but ADAPT publication must remain off until that consumer contract and
  its no-leak/legacy-parity tests are deployed. Client-reported active time is not enforcement-grade
  and WebWork's JWT path does not provide it, so the first rollout must remain `off` or `observe`.
