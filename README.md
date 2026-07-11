# LibreTexts Assessment AI

Standalone, review-gated assessment drafting for the LibreTexts dev sandbox.

The first vertical slice is deliberately narrow:

1. read one page under `dev.libretexts.org/Sandboxes/johnnyphung` using GET only;
2. normalize cited paragraphs with stable offsets;
3. extract concepts and draft one multiple-choice item;
4. run a separate critique/revision pass;
5. store the source, raw provenance, and revised draft in SQLite;
6. require separate human confirmation of Bloom level and difficulty.

Nothing publishes to ADAPT yet. The current ADAPT API requires a JWT identity with an owned
`my_questions` folder, and framework alignment must be submitted in the question create/update
payload. Publishing stays fail-closed until that identity and destination are provisioned.

## Ollama Cloud

The deployed profile uses Ollama's direct cloud API:

```dotenv
ASSESSMENT_AI_OLLAMA_BASE_URL=https://ollama.com
ASSESSMENT_AI_OLLAMA_MODEL=gpt-oss:120b
ASSESSMENT_AI_OLLAMA_API_KEY=...
```

Ollama Cloud currently does not enforce structured outputs. The client therefore includes the JSON
schema in the prompt, parses the response, validates it with Pydantic, and retries with validation
errors. A truly local model uses native schema-constrained output; a `:cloud`/`-cloud` model reached
through a signed-in local Ollama daemon uses the same client-side validation as the direct cloud
API. This keeps the model transport swappable and preserves a fully self-hostable path.

`/healthz` is container liveness; `/readyz` returns HTTP 503 until the selected direct cloud profile
has a non-empty API key. This lets the review shell stay observable without claiming generation is
ready.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn app.main:app --reload
```

Use `.env.example` as the non-secret template. Never commit `.env` or CXone/Ollama credentials.
For Docker Compose on a non-VPS machine, point Compose at the existing credential file without
copying it and use the non-secret template for validation:
`CXONE_ENV_FILE="$HOME/.cxone.env" ASSESSMENT_AI_ENV_FILE=.env.example docker compose config`.
