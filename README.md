# LibreTexts Assessment AI

Standalone, review-gated assessment drafting from public LibreTexts pages.

The first vertical slice is deliberately narrow:

1. read one public LibreTexts page through the fixed read proxy;
2. normalize cited paragraphs with stable offsets;
3. extract concepts and draft one multiple-choice item;
4. run a separate critique/revision pass;
5. store the source, raw provenance, and revised draft in SQLite;
6. require separate human confirmation of Bloom level and difficulty;
7. after a separate publish action, create a public ADAPT bank item and immutable QTI 3.0.1 ZIP.

ADAPT publishing is disabled by default and does not affect generation readiness. When provisioned,
the service authenticates as the dedicated role-5 `assessment-ai@libretexts.dev` editor, writes only
to its owned **Assessment AI — Approved** folder, and includes framework alignment in the question
create payload. It never adds a question to an assignment or writes to a textbook page.

## Page sources

The generation form accepts exactly one public page per request:

- A full HTTPS URL from `bio`, `biz`, `chem`,
  `eng`, `espanol`, `geo`, `human`, `k12`, `math`, `med`, `phys`, `socialsci`, `stats`, or
  `workforce` at `*.libretexts.org`.

Public sources are disabled by default. Enable them only after read-only proxy smoke checks:

```dotenv
ASSESSMENT_AI_PUBLIC_SOURCES_ENABLED=true
```

The public adapter validates the URL before networking, ignores query strings and fragments for
identity, and sends only fixed `PUT` read requests to `https://api.libretexts.org/endpoint/info`
and `/endpoint/contents` with `mode: view`. It never sends CXone credentials, cookies, caller
headers, or requests directly to the source host. Proxy failures fail closed; there is no HTML
scraping or raw Deki fallback. Book-wide crawling is not supported. The word “publishing” in this
service means publishing a reviewed question to the ADAPT dev shared question bank; LibreTexts
source pages remain read-only.

Dev sandbox sources are disabled at the adapter factory and HTTP route. Legacy `sandbox_path`
submissions are rejected, stored sandbox drafts are omitted from the queue and return HTTP 404,
and the Docker service does not load the CXone credential file. New forms submit
`source_type=public` and `source_locator=<value>`.

## LLM providers

The service supports Ollama Cloud and the Gemini API behind the same validated structured-output
interface. Providers are tried in the configured order, so either can be primary and the other can
be a fallback:

```dotenv
ASSESSMENT_AI_LLM_PROVIDER_ORDER=ollama,gemini
ASSESSMENT_AI_OLLAMA_BASE_URL=https://ollama.com
ASSESSMENT_AI_OLLAMA_MODEL=gpt-oss:120b
ASSESSMENT_AI_OLLAMA_API_KEY=...
ASSESSMENT_AI_GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
ASSESSMENT_AI_GEMINI_MODEL=gemini-3.5-flash
ASSESSMENT_AI_GEMINI_THINKING_LEVEL=minimal
ASSESSMENT_AI_GEMINI_API_KEY=...
ASSESSMENT_AI_GEMINI_MAX_OUTPUT_TOKENS=8192
```

Ollama Cloud currently does not enforce structured outputs. The client therefore includes the JSON
schema in the prompt, parses the response, validates it with Pydantic, and retries with validation
errors. A truly local model uses native schema-constrained output; a `:cloud`/`-cloud` model reached
through a signed-in local Ollama daemon uses the same client-side validation as the direct cloud
API. This keeps the model transport swappable and preserves a fully self-hostable path.

Gemini uses its native JSON Schema structured-output mode and the response is still validated with
the same Pydantic models before it can enter the review queue. If a provider fails after its bounded
attempts, the next ready provider is tried. The successful provider and model are stored with each
LLM call for audit provenance.

`/healthz` is container liveness; `/readyz` returns HTTP 503 until at least one selected provider is
ready. A direct cloud provider is ready only when its API key is non-empty. This lets the review
shell stay observable without claiming generation is ready.

## Assessment Computation v0 spike

Assessment Computation is a LibreTexts-owned, assessment-specific validation layer. It is not a
Wolfram|Alpha clone, query service, or general code runner. Its default mode is `off`; a request
must explicitly select both an allowlisted family and delivery. Null profiles retain the accepted
generation path and provider-call sequence.

The v0 contract uses a closed typed expression tree, pinned SymPy, and the named
`libretexts-edu-units-v0` UCUM subset. The typed computation contract has no field for Python,
SymPy source, PG/PHP, URLs, paths, files, imports, or executable code. External-engine source is
rendered only from fixed server templates, and learner-facing computation text is encoded as
runtime data. The unit subset accepts only its named allowlisted atoms, joined with UCUM `.` and
`/` operators and per-atom integer exponents from -3 through 3. Repeated division is evaluated
left-associatively; a leading `/` uses UCUM's whole-term reciprocal semantics. Parentheses,
annotations, arbitrary numeric factors, affine/logarithmic/procedure-defined units, custom units,
and every unlisted atom remain rejected. This describes the implemented subset and is not a claim
of full UCUM conformance or completed qualification. Fixed WeBWorK and IMathAS formula adapters
exist, but their exact source-controlled promotion registry ships empty. Both formula deliveries
therefore report `unsupported`, never `partially_validated` or `validated`, until fresh native
receipts pass and an exact reviewed compiler/engine/report promotion is added. They never fall back
to string comparison. Formula promotion is scoped to the algebraic `substitute`, `expand`,
`factor`, and `equivalent` operations; a symbolic `substitute` must retain exactly one response
symbol after fixed substitutions. `evaluate` is deliberately excluded from formula promotion.
The currently implemented numeric adapter subset accepts only exact integer
parameter grids; rational and decimal parameter ranges report `unsupported` instead of being
rounded through binary floats. The core validates complete real linear/quadratic solution sets, but
external-engine solve delivery is deferred in v0, including single-root equations, until a typed
native response adapter is qualified.

The algebra fixture split is 3 numeric substitutions, 11 formula operations, and 6 bounded solves
per engine. The sometimes-visible “11/20” number is therefore the formula-only slice, not supported
qualification coverage: all 20 algebra fixtures have sealed WeBWorK and IMathAS execution plans.
Numeric and formula plans execute the byte-identical `assessment-computation-typed-ast-v0`
production compiler source and hash. The six solve plans use a separate, explicitly
qualification-only `solution_set` template and do not expand the production `numeric | formula`
contract or make solve delivery eligible.

Run the computation engine as the networkless Unix-socket sidecar:

```bash
ASSESSMENT_AI_ENV_FILE=.env.example \
  docker compose -f docker-compose.yml -f docker-compose.computation.yml config

ASSESSMENT_AI_ENV_FILE=.env.example \
  docker compose --profile computation \
  -f docker-compose.yml -f docker-compose.computation.yml up --build
```

The Compose overlay uses `ASSESSMENT_COMPUTATION_IMAGE_REFERENCE` as the single source for both the
image Docker starts and the image reference Assessment AI evaluates. Local tags are feature-off
development inputs only. Qualification, `assist` evidence intended for promotion, and canary work
must use one immutable lowercase `repository@sha256:...` reference; a mutable tag cannot be
promoted.

The sidecar has no host port, network, credentials, database, or source mount. It runs non-root
with a read-only root filesystem and creates a fresh bounded child for every request. The
`computation` Compose profile is intentionally not started in `off`, so sidecar failure cannot
block the accepted application. SymPy, Pint, and ucumvert are compute-only package extras installed
by `Dockerfile.compute`; the networked Assessment AI image neither installs nor imports them.
During the sidecar build, the Dockerfile writes a canonical manifest over the computation Python
source, Dockerfile, package metadata, lockfile, and the complete normalized, sorted set of installed
Python distributions and versions. Manifest creation and startup both fail closed unless SymPy
1.14.0, Pint 0.25.3, and ucumvert 0.3.2 are present exactly; a changed transitive or transport
dependency also changes the manifest identity. The offline `evaluation` package, its CLI, and
wheel-installed duplicate project packages are removed from the sidecar runtime, leaving
`/app/app` as the sole local Python tree covered by the source hash. The sidecar verifies that
manifest at startup and returns its SHA-256 in every successful response. The client pins that
handshake for its process lifetime.

Run the opt-in real-container qualification harness before recording candidate evidence:

```bash
uv run python scripts/qualify_computation_container.py \
  --output /tmp/assessment-computation-container-qualification.json
```

The harness builds a uniquely tagged runtime image, starts a uniquely named container and socket
volume under the deployed `--init`, read-only, networkless, no-new-privileges, cap-drop, tmpfs,
PID, memory, and CPU flags, and removes those exact three resources even on failure. It verifies
the inspected Docker configuration and actual non-root service state, root-filesystem write and
egress denial, zero capability masks, Unix-socket readiness and computation, per-request child
limits, forced timeout recovery, fresh child PIDs, deterministic repeat results, and no surviving
request child. It never discovers or modifies an existing container. The normal pytest suite skips
this build; CI or an operator can run the same test explicitly with:

```bash
ASSESSMENT_COMPUTATION_RUN_DOCKER_QUALIFICATION=1 \
  uv run pytest -q tests/test_computation_container_qualification.py
```

```dotenv
ASSESSMENT_AI_COMPUTATION_MODE=assist
ASSESSMENT_AI_COMPUTATION_FAMILY_ALLOWLIST=numeric,algebraic,unit
ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_ALLOWLIST=trusted-proxy-subject
ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER=X-Assessment-AI-Authenticated-Subject
ASSESSMENT_AI_COMPUTATION_TRUSTED_PROXY_TOKEN=<runtime-only-base64url-secret>
ASSESSMENT_AI_COMPUTATION_NATIVE_RUNNER_SOCKET_PATH=/run/assessment-native-runner/runner.sock
ASSESSMENT_AI_COMPUTATION_NATIVE_RUNNER_ID=reviewed-runner-id
ASSESSMENT_COMPUTATION_IMAGE_REFERENCE=registry.example/assessment-computation@sha256:<64 lowercase hex characters>
```

The digest is derived from that one immutable reference; there is no independent claimed-digest
setting. A sidecar-reported manifest is not treated as cryptographic proof of its OCI image. The
source-controlled qualification registry must bind the exact repository reference, digest, and
observed build-manifest SHA-256 to reviewed evidence, and the client, validation report, and
enforce-mode policy all require that exact match. The remaining trust boundary is the container
runtime and deployment control plane: they must honor the immutable digest reference and Unix
socket wiring. The handshake detects accidental image/socket/build drift but cannot make a
malicious sidecar attest its own OCI digest.

Native WeBWorK/IMathAS receipts use a separate optional Unix-socket client; no URL is accepted.
The runner socket and identity must be configured together, and the
`QUALIFIED_NATIVE_ENGINE_RUNNERS` source-controlled registry ships empty. Unconfigured,
unavailable, or unqualified native execution remains inconclusive in `assist`; a timeout,
rejected request, or mismatched receipt fails closed. A passed receipt binds the exact engine,
compiler, compiled-source hash, blueprint, bound draft, grader, and 25-seed plan. The trust
boundary is local socket ownership and deployment integrity; v0 does not add receipt signing.

The shipped
`QUALIFIED_COMPUTATION_RUNTIMES` registry is deliberately empty, so `enforce` fails with
`runtime_not_qualified` even for a syntactically valid digest. Promoting a candidate requires a
separate source-reviewed registry entry binding the immutable image and build manifest to the
passed qualification report, allowed families, and—when units are enabled—the passed
named-UCUM-subset report. `assist` may collect partial evidence while this registry remains empty;
production enforcement cannot. An unpromoted runtime identity is `inconclusive`, so an otherwise
validated report is downgraded to `partially_validated`. A unit report also remains
`partially_validated` until its exact runtime digest is promoted with the named UCUM-subset
qualification report.

`assist` adds blueprint-first generation and reports but no new approval gate. `enforce` blocks
approval and publication on missing, stale, or failed evidence. `partially_validated` and
`unsupported` require an append-only attestation from an allowlisted trusted-proxy subject.
`validated` only means that the structured computational assertions and required native receipts
passed; source fidelity, wording, accessibility, and pedagogy remain mandatory human review. Human
approval and the separate publication action are never bypassed.

Pre-v0 numerical, WeBWorK, and IMathAS drafts remain
`unsupported: legacy_without_blueprint` until a reviewer explicitly opens the
validation panel and submits a strict `assessment-computation-v0` blueprint.
That action is available only while computation is not `off`, requires the
blueprint family to be allowlisted, and requires its delivery to match the
current draft type. The sidecar computes and validates the blueprint before the
server replaces all answer-bearing fields. Persistence then locks and verifies
the exact edit count and draft hash, creates a fresh revision and append-only
report in one transaction, makes prior evidence and approvals stale, and
returns the item to human review. The migration form accepts JSON for the typed
schema only; it does not accept expression strings, engine source, URLs, paths,
files, or executable code. It does not approve or publish the draft.

Computation-specialist identity has a separate trusted-proxy contract; `X-Reviewer` never grants
specialist authority. The authenticated edge proxy must:

1. Strip client-supplied `X-Assessment-AI-Proxy-Token` and the configured specialist-subject
   header before authentication.
2. Authenticate the reviewer, then set the canonical subject header and
   `X-Assessment-AI-Proxy-Token` only on the upstream request.
3. Use a unique random base64url token of at least 32 characters supplied to both proxy and backend
   through runtime secret environments. Never place the literal token in committed Caddy or
   Compose configuration, health responses, logs, or publication metadata.
4. Keep the Assessment AI backend unreachable from untrusted networks so clients cannot bypass the
   header-stripping proxy.

The backend validates the proxy token in constant time before it reads the specialist subject, then
requires an exact subject-allowlist match. A missing or invalid token hides the attestation control
and rejects attestation writes; configuring an allowlist alone grants no authority. Before any
`assist` or `enforce` reviewer rollout, prove and retain evidence that direct backend access is
blocked and that client-spoofed versions of both headers are stripped. Rotate the proxy token after
any suspected exposure and re-run those negative tests.

Build the clean-room fixture and planned native-engine manifests with:

```bash
assessment-ai-evaluate build-computation-fixtures \
  --output /tmp/assessment-computation-fixtures.json
assessment-ai-evaluate build-computation-seed-plan --run-id qualification-v0 \
  --output /tmp/assessment-computation-seeds.jsonl
assessment-ai-evaluate run-computation-mutations \
  --output /tmp/assessment-computation-mutations.json
assessment-ai-evaluate execute-computation-native-plan \
  /tmp/assessment-computation-seeds.jsonl \
  --engine webwork \
  --runner-socket /run/assessment-native/runner.sock \
  --run-id qualification-v0 \
  --webwork-engine-image-digest sha256:<digest> \
  --imathas-engine-image-digest sha256:<digest> \
  --imathas-adapter-image-digest sha256:<digest> \
  --network-attestation-sha256 <sha256> \
  --imathas-namespace acv0-canary-qualification-v0 \
  --output /tmp/assessment-computation-native-receipts.jsonl
assessment-ai-evaluate validate-computation-native-receipts \
  /tmp/assessment-computation-native-receipts.jsonl \
  --trust-policy /path/to/operator-pinned-canary-policy.json \
  --output /tmp/assessment-computation-native-report.json
assessment-ai-evaluate build-computation-algebra-native-plan \
  --run-id qualification-v0 \
  --imathas-namespace acv0-canary-qualification-v0 \
  --output /tmp/assessment-computation-algebra-plan.jsonl
assessment-ai-evaluate execute-computation-algebra-native-plan \
  /tmp/assessment-computation-algebra-plan.jsonl \
  --engine webwork \
  --runner-socket /run/assessment-native/runner.sock \
  --run-id qualification-v0 \
  --webwork-engine-image-digest sha256:<digest> \
  --imathas-engine-image-digest sha256:<digest> \
  --imathas-adapter-image-digest sha256:<digest> \
  --network-attestation-sha256 <sha256> \
  --imathas-namespace acv0-canary-qualification-v0 \
  --output /tmp/assessment-computation-algebra-receipts.jsonl
assessment-ai-evaluate execute-computation-algebra-native-plan \
  /tmp/assessment-computation-algebra-plan.jsonl \
  --engine imathas \
  --runner-socket /run/assessment-native/runner.sock \
  --run-id qualification-v0 \
  --webwork-engine-image-digest sha256:<digest> \
  --imathas-engine-image-digest sha256:<digest> \
  --imathas-adapter-image-digest sha256:<digest> \
  --network-attestation-sha256 <sha256> \
  --imathas-namespace acv0-canary-qualification-v0 \
  --output /tmp/assessment-computation-algebra-receipts.jsonl
assessment-ai-evaluate validate-computation-algebra-native-receipts \
  /tmp/assessment-computation-algebra-receipts.jsonl \
  --trust-policy /path/to/algebra-operator-pinned-canary-policy.json \
  --output /tmp/assessment-computation-algebra-report.json
assessment-ai-evaluate execute-computation-workflow-positives \
  --computation-socket /run/assessment-computation/compute.sock \
  --expected-runtime-manifest-sha256 <sha256> \
  --run-id qualification-v0 \
  --output /tmp/assessment-computation-workflow-positive-receipts.jsonl
assessment-ai-evaluate validate-computation-workflow-evidence \
  /tmp/assessment-computation-workflow-positive-receipts.jsonl \
  --mutation-report /tmp/assessment-computation-mutations.json \
  --trust-policy /path/to/workflow-trust-policy.json \
  --output /tmp/assessment-computation-workflow-report.json
assessment-ai-evaluate merge-computation-qualified-observations \
  --workflow-evidence /tmp/assessment-computation-workflow-report.json \
  --native-report /tmp/assessment-computation-native-report.json \
  --algebra-native-report /tmp/assessment-computation-algebra-report.json \
  --output /tmp/assessment-computation-qualified-observations.json
assessment-ai-evaluate validate-computation-build08-compatibility \
  --seed-receipts /path/to/build08-final-seeds.jsonl \
  --engine-probes /path/to/build08-engine-probes.jsonl \
  --adapt-attestations /path/to/build08-adapt-attestations.jsonl \
  --trust-policy /path/to/build08-trust-policy.json \
  --output /tmp/assessment-computation-build08-report.json
assessment-ai-evaluate execute-local-computation-canary-stage \
  /path/to/self-hashed-stage-execution-request.json \
  --disposable-root /path/to/marked-local-disposable-root \
  --working-directory /path/to/marked-local-disposable-root \
  --input-state /path/to/marked-local-disposable-root/input-state.json \
  --output-state /path/to/marked-local-disposable-root/output-state.json \
  --event-ledger /path/to/marked-local-disposable-root/events.jsonl \
  --observer-attestation /path/to/marked-local-disposable-root/observer.json \
  --output /tmp/assessment-computation-canary-stage-receipt.json
assessment-ai-evaluate qualify-computation-ucum \
  --artifact /path/to/UcumFunctionalTests.xml \
  --sha256 <reviewed-artifact-sha256> \
  --equivalence-attestation /path/to/reviewed-equivalence-attestation.json \
  --output /tmp/assessment-computation-ucum-report.json
assessment-ai-evaluate validate-computation-canary-stages \
  /path/to/ten-stage-canary-receipts.jsonl \
  --output /tmp/assessment-computation-canary-report.json
assessment-ai-evaluate validate-computation-sme-reviews \
  /path/to/append-only-sme-reviews.jsonl \
  --output /tmp/assessment-computation-sme-report.json
assessment-ai-evaluate validate-computation-paired-study \
  /path/to/raw-paired-study.json \
  --output /tmp/assessment-computation-paired-study-report.json
```

The mutation runner locally materializes all 200 semantic and safety mutations against the strict
schema, typed validator, compiler, and sealed hashes; it makes no network or native-engine calls.
The native executor rebuilds each engine source from the sealed typed lineage, sends a closed
request to a separately isolated runner over an existing Unix socket, checks the returned runtime
identity and observed values, and derives the receipt itself. It accepts no HTTP(S) runner URL.
Run the two 2,000-case engine halves separately, merge their JSONL receipts without changing the
rows, and then import the exact 4,000-row ledger. The importer qualifies only the exact
40-lineage × 100-seed plan under an operator-pinned run/profile, engine-image, adapter-image, and
network-attestation policy; otherwise it reports `not_run`, `partial`, or `failed`. Imported rows
remain execution claims rather than independently observed executions, and receipt self-hashes
provide integrity rather than authenticity. Qualification additionally binds the SHA-256 of the
exact imported JSONL bytes.
IMathAS receipts must identify one disposable canary namespace and stable per-lineage objects, and
the separately reviewed policy must bind both namespace creation and verified cleanup attestations.
Reformatting the ledger, mixing namespaces, or omitting cleanup proof fails closed.

The algebra executor imports exactly 40 sealed requests—20 fixtures independently compiled for
each engine—and calls the same bounded Unix-socket runner endpoint using a distinct strict request
schema. It derives receipts from native observations instead of accepting caller-authored receipt
rows. Every receipt must bind the exact manifest, fixture, compiler source, correct, alternate
correct where applicable, and wrong submissions; report constraint satisfaction; accept correct
equivalents; reject the wrong answer; render identically twice; and contain no warnings, errors, or
outbound requests. IMathAS objects must remain unique inside the disposable canary namespace. The
two 20-case engine halves append to one raw 40-row ledger. Its report stays qualification-only,
never enables production delivery, and is included by exact report hash in the final evidence
bundle.

The qualified-observation merge does not rewrite raw service reports. It derives a separate
evaluation ledger only when the exact workflow evidence, passed 4,000-row native report, and passed
40-row algebra report share the manifest, run, engine images, adapter, network attestation, and
IMathAS namespace. It can upgrade only the sealed 20 algebra and 40 engine-twin positive
observations; each upgraded case binds its specific report hash. Acceptance consumes that derived
ledger hash, so caller-authored `validated` states cannot satisfy coverage without the exact
reports. The merge explicitly records that solution-set production delivery remains disabled.

SME qualification likewise binds the exact append-only JSONL file, the current manifest and target
hash on every row, 280 unique record hashes, and a separately scoped reviewer attestation hash per
target. The paired study carries 100 exact arm-draft records; every reviewer score binds its draft,
the treatment blueprint/report where applicable, fixed provider settings, unique provider-call
receipts, and recomputed per-draft provider cost. A favorable score ledger detached from those
artifacts cannot qualify.

Zero-event safety evidence is not a scalar assertion. It requires distinct hashes and independent
observer attestations for eight raw monitor ledgers (publication, grading, permission, migration
loss, network, file access, process escape, and cross-draft isolation), plus exact off-parity,
backup/restore, rollback, fake-publication, database-snapshot, and isolation receipt hashes. Its run
and network identity must match both native engine plans. These hashes still establish integrity
rather than observer authenticity; the source-controlled exact-bundle promotion remains the final
trust boundary.

Workflow acceptance is likewise derived, not supplied as a favorable 300-row aggregate. The
positive runner accepts only an existing Unix socket and one exact computation-runtime manifest
hash; it accepts no URL, provider client, receipt rows, or favorable state/oracle flags. For every
one of the 100 sealed positive fixtures it obtains the typed compute result, derives the validation
request from that result, obtains two independent validation responses, recomputes the oracle and
deterministic replay decision, verifies typed compiler/source identity for WeBWorK and IMathAS, and
then creates the output ledger exclusively. Each receipt embeds the request, result, both reports,
and their canonical and capture hashes. The importer recomputes those bindings, requires exactly
100 rows, binds the exact JSONL bytes and runtime identity through a reviewed trust policy, and
combines them with the 200 receipts in the offline mutation report. Algebraic and external-engine
cases remain honestly `partially_validated` until their separate hash-bound native qualification
reports pass; the workflow runner never relabels them as validated.
BUILD-08 compatibility is rebuilt from the exact final-seed, engine-probe, and ADAPT-attestation
ledgers; a caller-created `4,000/4,000` object cannot qualify it.

The disposable canary ledger has ten ordered stages: offline corpus/security, cloned-database
`off` parity, backup/restore, prior-image rollback, `assist` numeric, `assist` algebraic, `assist`
unit, `enforce` with fake publication adapters and specialist attestations, the 380-draft
compatibility shadow, and return to `off`. Every stage has its own raw event ledger and independent
observer attestation, uses zero real publication attempts, and binds the same manifest, candidate
images, and cloned database. All ten receipts bind accepted BUILD-08 commit
`8497aad448d18c49d967480134eff9f80a444bd0` and prior image
`sha256:cd0caf5a10eecf871627d28f40316d05bb1598280e1ba6ef36fa2de3fd4950ed`.
The rollback stage must record that exact observed image; state equality alone is insufficient.
Raw event-ledger and observer-artifact hashes must each be unique across all ten stages and the two
hash sets must be disjoint, so one favorable artifact cannot be replayed as every stage.

The optional local stage executor accepts no favorable `passed` input. It byte-pins one executable
inside an exactly marked disposable local root, strips the inherited environment, bounds command
output and timeout, and rejects overt URL, SSH, Hostinger, `/opt/libretexts`, and live LibreTexts
target arguments. It hashes actual input/output state artifacts, requires exactly the
stage-specific observation plus a zero-real-publication event, verifies a separately authored
observer record over those exact bytes, and only then derives a receipt from the command's zero
exit status. Timeout and error cleanup targets the exact new process group, including descendants.
This is not a network or filesystem sandbox: a wrapper can contain a hard-coded remote action or
read host files. The command itself must run in an operator-proven networkless, credentialless,
disposable environment. This is a bounded evidence-import interface, not a deployment tool or
proof of observer identity; exact source-controlled promotion remains the trust boundary.

The UCUM harness never downloads a test artifact: it requires a local regular file and exact
checksum, verifies the behavior-controlling UCUM assets, and distinguishes the pinned mirror from
arbitrary checksum-pinned dry-run XML. It can reach `passed` only when a separately imported review
attests byte-for-byte equivalence between those exact local bytes and the official UCUM 2.2
attachment and binds the official release record, comparison ledger, reviewer subject, and reviewer
attestation. It still never claims full functional-suite or UCUM conformance.

These commands do not claim that the planned 8,000 native executions, 40 native algebra grader
executions, ten-stage cloned-database canary, SME review, or blinded provider study have run. The
canary is unexecuted, the feature remains `off`, and the promotion registry remains empty. A live
reviewer or publication rollout requires separate approval.
The final spike gate ignores aggregate pass counts: it requires the exact workflow-observation,
mutation, 4,000-row native-receipt, 40-row algebra-native, named-UCUM, 280-target SME-review, raw
two-reviewer paired-study, provider cost, BUILD-08 compatibility, and zero-safety-event report
identities. Caller-created `4,000/4,000` or paired summary objects are descriptive only and can never
mark the spike passed. Even a complete, self-consistent evidence bundle remains non-authoritative
until its exact bundle hash, candidate image, manifest, and separate approval record are added to
the source-controlled promotion registry; that registry intentionally ships empty.

## ADAPT publishing and QTI export

Provisioning is an explicit ADAPT administrator operation. On the ADAPT host, provide a generated
password only through the runtime environment and run:

```bash
ASSESSMENT_AI_SERVICE_PASSWORD='runtime-secret' \
  php artisan libretexts:provision-assessment-ai
```

The command is idempotent and prints only the service user, owned folder, and framework IDs. It
imports the committed Chemistry framework seed; it does not crawl LibreTexts at runtime. Put the
resulting folder ID and the same service password only in Assessment AI's VPS `.env`:

```dotenv
ASSESSMENT_AI_ADAPT_PUBLISHING_ENABLED=false
ASSESSMENT_AI_ADAPT_BASE_URL=https://adapt.libretexts.dev/api
ASSESSMENT_AI_ADAPT_EMAIL=assessment-ai@libretexts.dev
ASSESSMENT_AI_ADAPT_PASSWORD=...
ASSESSMENT_AI_ADAPT_FOLDER_ID=...
ASSESSMENT_AI_ADAPT_FOLDER_NAME=Assessment AI — Approved
ASSESSMENT_AI_ADAPT_AUTHOR=LibreTexts Assessment AI
ASSESSMENT_AI_ADAPT_PUBLIC=true
ASSESSMENT_AI_QTI_STORAGE_DIR=/data/qti
```

An instructor first chooses **Approve draft** after confirming Bloom level and difficulty. Approval
does not contact ADAPT. The separate publication panel enforces the source license, requires a
confirmed curated topic, and exposes **Publish to ADAPT** only when publishing is configured.

Each immutable draft revision gets a deterministic publication key. Exact retries return the same
ADAPT question and QTI artifact. Ambiguous POST outcomes are marked `unknown` and reconciled by the
exact deterministic tag before any further action. If ADAPT succeeds but QTI finalization fails,
the state remains `adapt_created` and only QTI is retried. Later edited-and-reapproved revisions
create new ADAPT questions; existing questions are never patched.

Successful publications create a schema-validated, deterministic QTI item package under `/data/qti`
and expose it through a reviewer-protected download route. The official pinned 1EdTech QTI 3.0.1
item and packaging schemas used for offline validation are bundled in `app/qti_schemas`, with source
URLs and hashes recorded in `SCHEMAS.json`.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn app.main:app --reload
```

Use `.env.example` as the non-secret template. Never commit `.env` or LLM credentials.
For Docker Compose on a non-VPS machine, use the non-secret template for validation:
`ASSESSMENT_AI_ENV_FILE=.env.example docker compose config`.
