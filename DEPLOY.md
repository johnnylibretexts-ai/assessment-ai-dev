# Deploying Assessment AI

Assessment AI is a review-gated drafting service. It reads a page, drafts an
assessment item, and holds that draft until a human approves it. Nothing it
generates reaches students without a separate, explicit publish action.

It is **not** a single container. Authentication happens at the edge, and the
identity the application trusts is asserted by that edge — never by the browser.
Getting that wiring wrong is the main way this deployment fails, and it fails
quietly. Read [Six traps](#six-traps) before changing any proxy configuration.

---

## What gets deployed

| Service | Port | What it is |
|---|---|---|
| `assessment-ai` | `127.0.0.1:8093` | The application. FastAPI + SQLite, `read_only` root filesystem, all capabilities dropped. |
| `assessment-ai-oidc` | `127.0.0.1:8194` | oauth2-proxy, pinned by digest. Answers the edge's auth subrequest; serves no application traffic. |
| `assessment-computation` | *(unix socket)* | Optional sandboxed evaluator. `network_mode: none`, reached only through a socket. Off by default. |
| Your reverse proxy | 443 | Terminates TLS, authenticates every request, and **destroys client-supplied identity headers**. |

Both published ports bind to `127.0.0.1`. Nothing is reachable except through
your proxy.

### The identity chain

```
browser ──▶ Caddy ──▶ /oauth2/auth (oauth2-proxy) ──▶ LibreOne OIDC/CAS
                │                       │
                │              202 + X-Auth-Request-User (the OIDC `sub`)
                │                       │
                └──▶ copy to X-Reviewer ──▶ assessment-ai :8093
```

**The `sub` claim is the identity.** Not the email, not the username — OpenID
Connect Core 5.7 makes `sub` the only claim a relying party may key on. The
reasoning, and why `X-Auth-Request-Email` is deliberately *not* forwarded, is in
[`docs/adr/0001-forward-auth-identity-binding.md`](docs/adr/0001-forward-auth-identity-binding.md).

---

## Deploy

### 1. Configure

```bash
cp .env.example .env
cp .env.oidc.example .env.oidc
```

`.env.oidc` **will not boot until you edit it.** Its placeholder cookie secret
is a deliberately invalid length, so oauth2-proxy refuses to start rather than
silently running production on a secret published in this repository. Generate a
real one:

```bash
openssl rand -base64 32 | tr -- '+/' '-_'
```

**The URL defaults in `.env.example` point at the LibreTexts.dev development
box.** Replace these five with your own before deploying:

```dotenv
ASSESSMENT_AI_ALLOWED_ORIGIN
ASSESSMENT_AI_ADAPT_BASE_URL
ASSESSMENT_AI_HOTSPOT_MEDIA_PUBLIC_BASE
ASSESSMENT_AI_WEBWORK_BASE_URL   /   ASSESSMENT_AI_WEBWORK_RENDERER_URL
ASSESSMENT_AI_IMATHAS_BASE_URL
```

### Two settings are pinned, not defaulted — do not try to change them

`sandbox_root` and `cxone_host` look like ordinary settings and are not. Each
has a `field_validator` in `app/config.py` that **rejects any other value**:

```
sandbox_root  →  pinned to "Sandboxes/johnnyphung"
cxone_host    →  pinned to "dev.libretexts.org"
```

Setting either to anything else raises a `ValidationError` at startup, which
reads like a broken build and is actually the guard working. Together they are
the CXone write scope: they confine everything this service can reach in CXone
Expert to a single sandbox, and `tests/test_config.py::test_cxone_scope_is_not_runtime_expandable`
asserts that the scope cannot be widened at runtime.

Neither appears in `.env.example`, deliberately — there is nothing to configure.
(`ASSESSMENT_AI_SANDBOX_SOURCES_ENABLED`, which *is* in there, is a different
setting: it toggles whether sandbox pages may be used as generation sources at
all.)

**A fork that needs a different CXone scope must change those validators in
code and re-scope the guard deliberately** — it is not an environment change,
and it should not be made into one casually.

Every capability ships **disabled**. That is the intended starting state:

```dotenv
ASSESSMENT_AI_PUBLIC_SOURCES_ENABLED=false
ASSESSMENT_AI_SANDBOX_SOURCES_ENABLED=false
ASSESSMENT_AI_ADVANCED_ITEMS_ENABLED=false
ASSESSMENT_AI_PARAMETERIZED_ITEMS_ENABLED=false
ASSESSMENT_AI_HINT_GENERATION_ENABLED=false
ASSESSMENT_AI_WEBWORK_ENABLED=false
ASSESSMENT_AI_IMATHAS_ENABLED=false
ASSESSMENT_AI_ADAPT_PUBLISHING_ENABLED=false
ASSESSMENT_AI_COMPUTATION_MODE=off
```

Enable them one at a time, verifying each before the next.

### 2. Supply an AI provider key

Generation calls a commercial hosted model and does nothing without a key.
Set **one** of these, and order them with `ASSESSMENT_AI_LLM_PROVIDER_ORDER`:

```dotenv
ASSESSMENT_AI_LLM_PROVIDER_ORDER=ollama,gemini
ASSESSMENT_AI_OLLAMA_API_KEY=...
ASSESSMENT_AI_GEMINI_API_KEY=...
```

Both are paid third-party services, and generation transmits source page text to
whichever one is configured. See `THIRD-PARTY.md`.

### 3. Register the OIDC client

In your identity provider, create a confidential client with:

- redirect URI `https://<your-host>/oauth2/callback`
- scopes `openid profile email`
- `RS256` signing

Then set the issuer, client id and redirect URI in `deploy/oauth2-proxy.cfg`,
and the client secret in `.env.oidc`.

### 4. Start

```bash
docker compose up -d --build
```

With the computation sidecar (only if you have a reason to):

```bash
docker compose -f docker-compose.yml -f docker-compose.computation.yml \
  --profile computation up -d --build
```

### 5. Put it behind your proxy

`deploy/Caddyfile.assess-ai` is the reference configuration. **Copy it rather
than writing your own** — several lines in it look redundant and are not, and
each one carries a comment explaining what breaks without it.

Change the hostname, the CAS origin in the `form-action` CSP directive, and
nothing else until you have read those comments.

### 6. Verify

```bash
# Unauthenticated request must redirect to sign-in, never return content
curl -sI https://<your-host>/ | head -1

# The app must not be reachable except through the proxy
curl -sI http://127.0.0.1:8093/ | head -1     # only from the host itself

# The deployment contract is enforced by tests, not by inspection
docker compose run --rm --build assessment-ai python -m pytest tests/test_deployment_contract.py
```

That last test exists because the proxy configuration has failed silently
before. Run it after any change to the Caddyfile or oauth2-proxy config.

---

## Six traps

Each of these has actually happened, and none of them produces an obvious error.

**Four of the six are asserted by `tests/test_deployment_contract.py`**, so you
do not have to catch them by reading:

| Trap | Test |
|---|---|
| 1. Missing `route { }` | `test_app_block_is_wrapped_in_route_so_written_order_is_execution_order` |
| 2. Set-then-delete in one block | `test_no_reverse_proxy_block_both_sets_and_deletes_the_same_field` |
| 4. CSP blocks the CAS redirect | `test_csp_allows_form_posts_to_reach_cas` |
| 5. Basic Auth in front of SSO | `test_basic_auth_placeholder_is_gone` |

Traps 3 and 6 are environment-dependent and cannot be asserted from the
configuration alone.

**1. Omitting the `route { }` block in the Caddyfile deletes the identity.**
Caddy's Caddyfile adapter sorts `request_header` *after* `forward_auth` in its
default directive order. Without an explicit `route`, the header strips run
after the identity is copied in and remove it. The backend receives no reviewer
at all. Symptom: everyone is unauthenticated despite logging in successfully.

**2. `header_up -<name>` for a name you `Set` in the same block wins.**
Caddy's `HeaderOps.ApplyTo` runs deletes *after* sets, so the header never
arrives. This was a live bug: both computation headers were set and then
deleted, and the specialist path had never once received either. Strips belong
in the `request_header` lines earlier in the chain, not next to the `Set`.

**3. `trusted_proxy_ips` is the Docker bridge gateway, not `127.0.0.1`.**
Caddy runs on the host and connects to the published `127.0.0.1:8194`, so
docker-proxy forwards the connection and oauth2-proxy sees it arriving from the
bridge gateway. Setting `127.0.0.1/32` trusts nothing. Docker assigns the
subnet, and a network recreate can renumber it. Symptom is **not an outage** —
logins still succeed, they just lose the deep link and land on `/`. Re-derive
with:

```bash
docker inspect assessment-ai-oidc \
  --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

**4. The CSP `form-action` directive must list your CAS origin.**
It is enforced across the whole redirect chain, so when a session expires a form
POST that redirects out to the identity provider is blocked. Symptom: the button
appears dead, with nothing in the application log.

**5. Do not put Basic Auth in front of this.**
There is deliberately no `import gate` in the reference Caddyfile. A gate would
sit in *front* of SSO, so users would authenticate twice and the identity the
application trusts would still come from OIDC. It adds a prompt and no security.

**6. `cookie_expire` is an absolute cap, not an idle timeout.**
With `cookie_refresh` disabled it is the only thing bounding a session. At 15
minutes, reviewers lost sessions mid-review roughly twice per half hour — and
because a form POST is answered with a 302, the request body is discarded, so
each expiry destroyed the pending decision. It is set to `2h`, which is bounded
by the identity provider's own idle timeout so the application session can never
outlive the session that authorised it. Raising it further requires either
`cookie_refresh > 0` (which oauth2-proxy rejects while `session_cookie_minimal`
is true) or accepting a session that outlives its authorisation.

---

## Data and backup

State lives in the `assessment_ai_data` named volume, mounted at `/data`:

- `assessment-ai.db` — SQLite: sources, drafts, provenance, decisions
- `qti/` — immutable QTI 3.0.1 ZIP exports
- `media/` — hotspot images

The container root filesystem is `read_only` with a 64 MB `tmpfs` at `/tmp`.
**Anything written outside `/data` is lost on restart, by design.** Back up the
volume, not the container:

```bash
docker run --rm -v assessment_ai_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/assessment-ai-$(date +%F).tar.gz -C /data .
```

## Enabling ADAPT publishing

Publishing stays off until a service identity exists on the ADAPT side. It
authenticates as a dedicated editor account, writes **only** into its own
folder, and never adds a question to an assignment or writes to a textbook page.

Before setting `ASSESSMENT_AI_ADAPT_PUBLISHING_ENABLED=true`, confirm the
account, the folder id, and the framework all exist — and that the account's
permissions are scoped to that folder and nothing else.

## Upgrading

```bash
git pull
docker compose up -d --build
docker compose run --rm assessment-ai python -m pytest tests/test_deployment_contract.py
```

The oauth2-proxy image is pinned by digest and does not move on rebuild. Bumping
it is a deliberate edit to `docker-compose.yml`, which is the intended
behaviour: this container is the authentication boundary.
