# Demo Assistant — design

**Date:** 2026-07-25
**Branch:** `feat/demo-assistant` (off `origin/main` = `2db76dd`)
**Status:** approved, implementing

## Problem

Assessment AI is being demoed to a handful of testers. A tester with a question — "what does Bloom
level mean?", "why is publishing greyed out?", "what is ADAPT?" — has nowhere to ask it inside the
app.

## What this is

A **Demo Assistant**: a chatbot that answers any question, grounded in a curated corpus about
Assessment AI and the LibreTexts.dev stack, plus live app state.

It is a demo-support affordance only. It has nothing to do with generating questions, reviewing
drafts, or publishing to ADAPT. It is built as a self-contained, feature-flagged, deletable unit so
it can never be confused with the qualified generation pipeline.

## What this is not

- Not part of the generation, review, or publishing path.
- Not a tool-using agent. It has no tools; text in, text out. It cannot mutate a draft, publish, or
  reach ADAPT, CXone, WeBWorK, or IMathAS.
- Not installed on `assess-ai-corpus.libretexts.dev` (that site blocks mutating methods by design).
- Not enabled by default. `ASSESSMENT_AI_ASSISTANT_ENABLED` defaults to `false`.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Grounding | Curated corpus baked into the image + live app state per request | The corpus is ~15k tokens against a 1M-token window |
| RAG | **No** | Embeddings + vector store + chunking is real infrastructure for a *worse* answer at this corpus size |
| Knowledge graph | Excluded | `graphify-out/graph.json` is a 1.1MB code-structure graph for developers; testers ask product questions |
| Persistence | SQLite, scoped per reviewer | Records what testers actually asked; survives reload |
| UI | Slide-over panel from the header | Ask without losing the draft under review |
| Streaming | SSE, token-by-token | ~1s to first token instead of a 5–15s blank wait |
| Page awareness | Server-side state reconstruction | Exact, cheap, and can explain *why* something is blocked — which isn't on screen |
| Reply rendering | Plain text via `textContent` | Unconditionally XSS-safe, no markdown dependency |

## Architecture

Everything lives in `app/assistant/`, plus one shared extraction and three UI files.

| File | Role |
|---|---|
| `app/http_guards.py` | `require_same_origin()` / `reviewer_identity()`, moved out of `main.py`; shared, breaks the import cycle |
| `app/assistant/corpus.py` + `corpus/*.md` | The baked-in curated knowledge, loaded and cached |
| `app/assistant/context.py` | Route → compact authoritative page-state summary |
| `app/assistant/llm.py` | Provider-pluggable streaming chat client (Gemini + Ollama) |
| `app/assistant/prompt.py` | System instruction + corpus + runtime facts + page context + history |
| `app/assistant/store.py` | Own `DeclarativeBase`, two tables on the existing engine |
| `app/assistant/service.py` | Validation, rate limit, history trimming, streaming, persistence |
| `app/assistant/routes.py` | `GET /assistant/conversation`, `POST /assistant/message` (SSE), `POST /assistant/reset` |

### Why a separate LLM client

`app/llm.py`'s `LLMClient` protocol is structured-output-only (`complete(prompt, schema)`). Chat
needs free text and streaming — a different shape entirely. A separate client also means the
qualified generation path is not touched at all.

It stays provider-pluggable (Gemini and Ollama, selected by the existing `llm_provider_order`) so
self-hosted and open-weight models remain first-class, per the project's open-source rule.

### Why not reuse `_draft_detail()`

It lives in `main.py`, which would reintroduce a circular import, and it returns the full item JSON
— token bloat. `context.py` reads the `Draft` record directly and pulls a purpose-built subset:
status, item type, concept, Bloom, difficulty, critique issues, hint-grounding failures, engine
validation, publication state.

### Why its own declarative base

`app/db.py` is already 1700+ lines. A separate `AssistantBase` with its own `create_schema(engine)`,
called from the lifespan only when the flag is on, keeps `db.py` untouched and means deleting the
package deletes its schema registration with it.

## Data

- `assistant_conversations` — id, reviewer (indexed), title, created_at, updated_at
- `assistant_messages` — id, conversation_id (FK, cascade), role, content, model, page_route,
  error_code, created_at

Never read by the generation pipeline. Reviewer scoping is enforced in every query.

## System instruction, in substance

Answer any question the tester asks, including general ones outside this app. Prefer the corpus and
runtime facts over recollection, and say so when they conflict. You have no tools and cannot change
anything, so never claim to have performed an action. Never output credentials, secrets, tokens, or
internal addresses. Say plainly when you don't know. Never describe the 380 BUILD-08 corpus drafts
as published, student-facing, human-reviewed, or clinically approved.

## Corpus contents

`00-assistant-guide.md`, `10-feature-flags.md`, `20-item-types.md`, `30-libretexts-stack.md`,
`40-faq.md`.

**Hard exclusion, enforced by a test:** nothing from `handoff-july-6.md`; no credentials, tokens,
API keys, password hashes, or private IP addresses. Public `*.libretexts.dev` URLs only.

## Settings

`assistant_enabled` (false), `assistant_model` (""), `assistant_max_turns` (24),
`assistant_max_message_chars` (4000), `assistant_rate_limit_per_minute` (12),
`assistant_timeout_seconds` (120.0). `assistant_status(settings)` mirrors `webwork_status` and is
surfaced in `/healthz`.

## Deployment notes

- Live auth is LibreOne SSO via oauth2-proxy, which supplies `X-Reviewer`. Identity works unchanged.
- **No Caddy change needed:** the live CSP already has `connect-src 'self'`, and Caddy auto-disables
  buffering for `text/event-stream`.
- `/opt/libretexts/assessment-ai/deploy/Caddyfile.assess-ai` is ahead of git (carries the oauth2
  config). Treat it as box-local; never overwrite from a checkout.
- Build from a fresh release tree per `DEPLOY.md`, never from the runtime directory.
