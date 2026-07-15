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
