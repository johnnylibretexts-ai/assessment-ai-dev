# BUILD-08 deterministic qualification harness

This package creates and validates BUILD-08 evidence while every advanced
Assessment AI flag remains disabled. It performs no provider call, publication,
engine request, textbook write, or live flag change by itself.

Stable commands:

```bash
assessment-ai-evaluate write-schemas --output-dir evidence/schemas
assessment-ai-evaluate build-fixtures --output evidence/fixtures.json
BUILD08_ASSESSMENT_CANARY=build08-assessment-browser-canary \
  assessment-ai-evaluate seed-browser-canary \
  --database-url sqlite:////data/build08-browser-canary.db \
  --output evidence/assessment-browser-canary.json
assessment-ai-evaluate build-seed-plan --output evidence/seed-plan.jsonl
assessment-ai-evaluate validate-corpus evidence/corpus.json
assessment-ai-evaluate validate-reviews evidence/reviews.jsonl
assessment-ai-evaluate validate-seeds evidence/seed-receipts.jsonl
assessment-ai-evaluate probe-webwork evidence/seed-plan.jsonl \
  --output evidence/engine-probes.jsonl \
  --engine-image-sha256 sha256:<full-image-digest> \
  --network-attestation-sha256 <sealed-network-evidence-digest>
assessment-ai-evaluate probe-imathas evidence/seed-plan.jsonl \
  --output evidence/imathas-engine-probes.jsonl \
  --engine-image-sha256 sha256:<full-engine-image-digest> \
  --adapter-image-sha256 sha256:<full-bridge-image-digest> \
  --network-attestation-sha256 <sealed-network-evidence-digest>
assessment-ai-evaluate validate-engine-probes evidence/engine-probes.jsonl
assessment-ai-evaluate merge-engine-probes \
  evidence/webwork-engine-probes.jsonl \
  evidence/imathas-engine-probes.jsonl \
  --output evidence/engine-probes.jsonl
assessment-ai-evaluate finalize-seeds \
  evidence/engine-probes.jsonl \
  evidence/adapt-seed-attestations.jsonl \
  --output evidence/seed-receipts.jsonl
assessment-ai-evaluate build-adapt-seed-items \
  evidence/engine-probes.jsonl \
  evidence/imathas-object-ids.json \
  --output evidence/adapt-seed-items.jsonl
assessment-ai-evaluate compare-shadow evidence/shadow-receipts.jsonl
assessment-ai-evaluate report \
  --corpus evidence/corpus.json \
  --reviews evidence/reviews.jsonl \
  --seeds evidence/seed-receipts.jsonl \
  --shadow evidence/shadow-receipts.jsonl \
  --output evidence/qualification-report.json
```

The fixture bundle covers all 19 item types crossed with all five context
variants. The seed plan contains 20 WeBWorK and 20 IMathAS items at seeds 1–100
(4,000 planned executions). A plan is not an execution receipt and cannot pass
the seed validator.

`seed-browser-canary` persists that exact 95-case fixture matrix, its three-rung
hint ladders, and 100-seed external-engine previews into an absolute,
file-backed SQLite database. It refuses to run without the exact disposable
canary marker and rejects an existing draft database unless it is already the
same sealed matrix. The command does not enable generation, publishing,
parameterized-item, hint, WeBWorK, or IMathAS runtime flags.

Engine probes are also deliberately not final seed receipts. They capture
runtime-observed values, semantic determinism, constraints, real expected/wrong
grading, the exact engine image, and sealed network-isolation evidence. They
retain `persisted_grade_match`, `object_idempotent`, and
`cross_owner_access_blocked` as explicit remaining checks. In particular, the
runner never treats Python preview RNG output as a PG/IMathAS answer oracle.

`finalize-seeds` is fail closed: the canary attestation and engine ledgers must
contain exactly the same 4,000 item/seed keys and matching immutable identities.
Every attestation must include a refreshed matching grade, an idempotent object,
blocked cross-owner access, an internal-only canary network, the exact ADAPT
image, the clone-backup digest, and disabled hint mode. A plan or item summary
cannot be promoted into final receipts.

Validators exit `0` only when the corresponding release threshold passes and
exit `2` for valid but insufficient or failing evidence. Invalid JSON/schema
input fails closed. The aggregate report passes only when corpus, human review,
seed execution, and off-versus-observe shadow parity all pass.
