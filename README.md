# LibreTexts Assessment AI

Standalone, review-gated assessment drafting from public LibreTexts pages and the approved LibreTexts dev sandbox.

The first vertical slice is deliberately narrow:

1. read one public LibreTexts page through the fixed read proxy, or one page under `dev.libretexts.org/Sandboxes/johnnyphung`;
2. normalize cited paragraphs with stable offsets;
3. extract concepts and draft one multiple-choice item;
4. run a separate critique/revision pass;
5. store the source, raw provenance, and revised draft in SQLite;
6. require separate human confirmation of Bloom level and difficulty.

Nothing publishes to ADAPT yet. The current ADAPT API requires a JWT identity with an owned
`my_questions` folder, and framework alignment must be submitted in the question create/update
payload. Publishing stays fail-closed until that identity and destination are provisioned.

## Page sources

The generation form accepts exactly one page per request:

- **Public LibreTexts page** (default when enabled): a full HTTPS URL from `bio`, `biz`, `chem`,
  `eng`, `espanol`, `geo`, `human`, `k12`, `math`, `med`, `phys`, `socialsci`, `stats`, or
  `workforce` at `*.libretexts.org`.
- **Dev sandbox page**: the existing relative path under `Sandboxes/johnnyphung`.

Public sources are disabled by default. Enable them only after read-only proxy smoke checks:

```dotenv
ASSESSMENT_AI_PUBLIC_SOURCES_ENABLED=true
```

The public adapter validates the URL before networking, ignores query strings and fragments for
identity, and sends only fixed `PUT` read requests to `https://api.libretexts.org/endpoint/info`
and `/endpoint/contents` with `mode: view`. It never sends CXone credentials, cookies, caller
headers, or requests directly to the source host. Proxy failures fail closed; there is no HTML
scraping or raw Deki fallback. Book-wide crawling and public-page publishing are not supported.

Existing `sandbox_path` form submissions remain accepted as sandbox requests for compatibility.
New forms submit `source_type=public|sandbox` and `source_locator=<value>`.

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
ASSESSMENT_AI_GEMINI_MODEL=gemini-2.5-flash
ASSESSMENT_AI_GEMINI_API_KEY=...
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

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn app.main:app --reload
```

Use `.env.example` as the non-secret template. Never commit `.env` or CXone/LLM credentials.
For Docker Compose on a non-VPS machine, point Compose at the existing credential file without
copying it and use the non-secret template for validation:
`CXONE_ENV_FILE="$HOME/.cxone.env" ASSESSMENT_AI_ENV_FILE=.env.example docker compose config`.
