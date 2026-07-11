# P0 implementation contract

These notes capture verified differences between the v2.1 design document and the current live
ADAPT/CXone contracts. The canonical spec is preserved unchanged.

- P0 includes minimal SQLite persistence and a review surface; otherwise its human Bloom/difficulty
  gates would have no executable path.
- Content ingestion is pinned to `dev.libretexts.org/Sandboxes/johnnyphung`, GET-only, and fails
  closed. The generic mirror client is not exposed.
- Ollama Cloud is the selected hosted runtime, with a local Ollama profile retained. Cloud responses
  are client-validated because Ollama Cloud does not currently enforce JSON-schema output.
- ADAPT has no POST framework-sync endpoint. Question alignment is written through the
  `framework_item_sync_question` field on question create/update.
- ADAPT publication needs a JWT instructor/editor identity and an owned `my_questions` folder.
  Caddy authentication for the review UI does not satisfy that API contract.
- Existing ADAPT hint UI and telemetry support one flat hint, not per-rung progressive reveal. The
  three-rung ladder remains deferred unless a later scope explicitly allows ADAPT UI/telemetry work.
