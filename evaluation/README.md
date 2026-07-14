# BUILD-08 deterministic qualification harness

This package creates and validates BUILD-08 evidence while every advanced
Assessment AI flag remains disabled. It performs no provider call, publication,
engine request, textbook write, or live flag change by itself.

Stable commands:

```bash
assessment-ai-evaluate write-schemas --output-dir evidence/schemas
assessment-ai-evaluate build-fixtures --output evidence/fixtures.json
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

Engine probes are also deliberately not final seed receipts. They capture
runtime-observed values, semantic determinism, constraints, real expected/wrong
grading, the exact engine image, and sealed network-isolation evidence. They
retain `persisted_grade_match`, `object_idempotent`, and
`cross_owner_access_blocked` as explicit remaining checks. In particular, the
runner never treats Python preview RNG output as a PG/IMathAS answer oracle.

Validators exit `0` only when the corresponding release threshold passes and
exit `2` for valid but insufficient or failing evidence. Invalid JSON/schema
input fails closed. The aggregate report passes only when corpus, human review,
seed execution, and off-versus-observe shadow parity all pass.
