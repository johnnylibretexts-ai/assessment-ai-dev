# Bind reviewer identity to the oauth2-proxy subject, not `{http.auth.user.id}`

**Status:** accepted (2026-08-02)

`deploy/Caddyfile.assess-ai` diverged in both directions: `main` kept Basic Auth (`import gate`) and
gained `encode zstd gzip` plus the computation-specialist wiring, while `feat/libreone-sso` replaced
Basic Auth with oauth2-proxy + OIDC/CAS. Both of `main`'s downstream identity headers were bound to
`{http.auth.user.id}`, a placeholder that only the Basic Auth module ever sets. **We delete that
placeholder, bind `X-Reviewer` from oauth2-proxy's `X-Auth-Request-User` (the OIDC `sub`) via
`copy_headers`, and derive the computation-specialist subject from that copied value** — with
explicit client-header strips ahead of the auth subrequest, all inside a `route { }` block.

## Why the placeholder had to go

`{http.auth.user.id}` is set exclusively by Caddy's `caddyauth` module after a successful
`basic_auth`. `forward_auth` contains no `caddyauth` module, so nothing ever sets it — and Caddy's
`ReplaceKnown` copies **unrecognised placeholders through verbatim** rather than emptying them.

The value reaching the backend would therefore be the literal 20-character string
`{http.auth.user.id}`. `app/http_guards.py::reviewer_identity` fails closed on *empty*, and that
string is not empty. Carrying `main`'s line into the SSO config would have authenticated every
request as one shared fictional reviewer named `{http.auth.user.id}`, and written that name into
review and attestation records.

This is a **fail-open identity** (see the workspace glossary). It is the reason this merge was not
transcription work.

## Considered options

**Reviewer identity.** Settled by OpenID Connect Core §5.7: `sub` (with `iss`) is the only claim an
RP may treat as a stable identifier; `email`, `preferred_username` and friends MUST NOT be used.
oauth2-proxy's `X-Auth-Request-User` is `sub` for the OIDC provider regardless of configuration, so
`X-Auth-Request-User>X-Reviewer` is both correct and already what the branch did.

**Specialist subject.** `copy_headers` compiles to a `map[from]to`, so one source header cannot feed
two destinations — the specialist subject needed its own answer.

| Option | Rejected because |
|---|---|
| Derive from `{http.request.header.X-Reviewer}` (**chosen**) | — |
| Let the backend derive it from its own validated `x-reviewer` | Requires a backend change, and collapses a boundary the backend actively defends: `config.py` validates that the subject header is a dedicated `X-Assessment-AI-*` name distinct from the proxy-token header, and `main.py` comments that "the general `X-Reviewer` identity remains intentionally separate". |
| Drop the computation wiring until the feature is enabled | Silently discards `main`'s contribution to the merge, and leaves the backend's `computation_*` code with no edge that could ever feed it. |

## What the merged configuration does, and why each part is load-bearing

- **`route { }` wraps the application block.** The Caddyfile adapter sorts `request_header` *after*
  `forward_auth` in `defaultDirectiveOrder`. Outside a `route`, the strips below would run after the
  copy and delete the identity they exist to protect. This is not stylistic.
- **`request_header -X-Reviewer` etc. before `forward_auth`.** On Caddy ≥ 2.11.2 these are redundant
  — `copy_headers` emits an unconditional `Delete` ahead of its guarded `Set`. On 2.10.0–2.11.1 they
  are the *only* defence against CVE-2026-30851, where a 2xx auth response that omits the identity
  header leaves the client's forged value intact. **Do not delete them in review as dead code.** The
  accepted risk below includes a scenario in which this deployment downgrades.
- **Strips are `request_header` lines, never `header_up -X` beside `header_up X`.** `HeaderOps.ApplyTo`
  runs Delete *after* Set within a single block, so co-locating them silently drops the header.
  `main` co-located both computation headers, which means that feature has never received a token or
  a subject since it was written — inert, but fail-closed, so nothing broke.
- **`X-Auth-Request-Email>X-Reviewer-Email` is removed.** Nothing in the backend reads it; setting
  `user_id_claim = "sub"` makes it carry a `sub` rather than an email, so the name lies; and it was
  the `copy_headers` entry most likely to trip CVE-2026-30851, since oauth2-proxy omits a header
  entirely for an empty claim.
- **CSP is `main`'s hardened policy plus `https://one.libretexts.dev` in `form-action`**, matching
  what is live. Without the CAS origin, deploying this file would silently re-break every form action
  on session expiry.

## Consequences

- **The specialist allowlist must hold opaque OIDC `sub` values**, not emails or usernames. An
  operator adding a Computation specialist has to look up that person's `sub`, which is only visible
  from a real session. That is the price of §5.7 compliance and it is worth paying.
- **`sub` is unique per issuer.** With one fixed `oidc_issuer_url` this is safe. If a second IdP is
  ever added, the reviewer key must become `iss` + `sub`, and this ADR must be revisited.
- **The computation-specialist path is now correctly wired and still disabled.** `computation_mode`
  defaults to `off`, the subject allowlist is empty, no proxy token is set, and no
  `ASSESSMENT_AI_COMPUTATION_*` variable exists on the box. That fail-closed posture is **intended**,
  not an oversight; enabling the feature is a separate deliberate act requiring a minted 32–256
  character token and a populated allowlist.
- **`oauth2-proxy.cfg` carries a latent defect this ADR does not fix.** `user_id_claim = "sub"`
  alongside `oidc_email_claim = "email"` trips oauth2-proxy's backwards-compatibility block, which
  rewrites `EmailClaim` to `sub` and, as a side effect, **skips `email_verified` enforcement** even
  though `insecure_oidc_allow_unverified_email = false`. Since `X-Auth-Request-User` is `sub`
  regardless, the setting buys nothing. It is left alone here because removing it changes who can log
  in and cannot be verified without an interactive SSO login. Tracked as
  `.scratch/assessment-ai-sso/issues/02-oauth2-proxy-user-id-claim.md`.
- **PR #26 becomes an empty diff.** It carried the `form-action` CAS origin into the repo; this
  change includes it.

## Accepted risk: an unpatched race in the mechanism this ADR specifies

[GHSA-6365-7ppr-5r92](https://github.com/caddyserver/caddy/security/advisories/GHSA-6365-7ppr-5r92)
— *Connection created for the wrong upstream for `forward_auth` + `reverse_proxy`*, Moderate, no CVE,
published 2026-07-10. A connection race can send the auth subrequest to the wrong upstream; the
advisory notes you could "pass a `forward_auth` check unexpectedly if the raced connection happens to
have the same endpoints and replies with 200 OK".

The affected shape is `forward_auth` and `reverse_proxy` in one handler — exactly what this ADR
specifies. The advisory names **v2.11.5** as the fix, and **v2.11.5 does not exist**: the latest
release is v2.11.4 (2026-06-03) and the tag 404s. The box runs v2.11.4. There is nothing to upgrade
to.

We ship anyway. Downgrading to v2.10.2 would escape the race but reintroduce CVE-2026-30851
(High 8.1, deterministic identity injection) — a strictly worse trade than a Moderate,
non-deterministic race whose worst case additionally requires the raced upstream to answer 2xx on
`/oauth2/auth`. The explicit strips above mean that if a downgrade ever *does* happen, this config
remains correct.

Owned as a standing watch at `.scratch/assessment-ai-sso/issues/01-caddy-forward-auth-race.md`. If a
failure in this site ever presents as **intermittent** rather than deterministic, suspect this race
before suspecting the configuration.

## Not verified here

This configuration has never been exercised against the real LibreOne issuer. Every behavioural claim
above is either read from upstream Go source at a pinned tag or measured against a local harness on
Caddy v2.11.4 and v2.10.2 — see `research/2026-08-02-forward-auth-identity-binding/` in the
workspace. Before deploying, confirm with a real session that a forged `X-Reviewer` does not reach
the backend, that a forged proxy token is refused, and that no cookie yields a 302 to
`/oauth2/sign_in`.
