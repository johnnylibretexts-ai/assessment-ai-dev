# Assessment AI LibreOne SSO runbook

## Identity boundaries

Human access and machine publishing are intentionally separate:

- LibreOne/CAS authorizes a human reviewer for application `1003`.
- OAuth2 Proxy validates the OIDC response and returns the LibreOne UUID to Caddy.
- Caddy destroys any caller-supplied identity headers *before* the auth subrequest, then sets
  `X-Reviewer` to the UUID and removes the session cookie and all bearer/access-token headers
  before proxying to Assessment AI. **No email is forwarded** — `X-Reviewer-Email` is deliberately
  not copied downstream (nothing reads it, and `user_id_claim = "sub"` would make it carry a
  subject rather than an email). See `adr/0001-forward-auth-identity-binding.md`.
- Assessment AI publishes to ADAPT only with its dedicated role-5 service account.

Never reuse an ADAPT password as an OIDC secret, never put the OIDC client secret in git, and never
pass a human OIDC token to the Assessment AI or ADAPT application containers.

## Runtime files and API actors

The VPS holds these uncommitted authentication files:

- `/opt/libretexts/assessment-ai/.env.oidc`, mode `0600`, containing only
  `OAUTH2_PROXY_CLIENT_SECRET` and a separately generated 32-byte
  `OAUTH2_PROXY_COOKIE_SECRET`.
- `/opt/libretexts/libreone-cas/runtime-services/AssessmentAI-1003.json`, mode `0600`, generated
  from the committed CAS template with the same OIDC client secret.
- `/opt/libretexts/.secrets/cas-sso-boundary.env`, mode `0600`, for a read-only LibreOne API actor
  with only `users:read`. CAS uses it for the principal-attribute repository and authorization
  interrupt. The corresponding protected `cas.properties` entries point to
  `/api/v1/users/principal-attributes` and `/api/v1/auth/cas-interrupt-check`.
- `/opt/libretexts/.secrets/assessment-ai-entitlements.env`, mode `0600`, for the operator-only
  entitlement actor with only `applications:write` and `users:write`.

Bind-mount the completed CAS descriptor read-only at
`/etc/cas/services/AssessmentAI-1003.json`. Do not replace or expose the existing service directory.
Never give the CAS runtime the entitlement actor's write-capable credential.

## Grant or revoke reviewer access

LibreOne application `1003` is hidden and has no default access. Use a dedicated LibreOne API actor
with `applications:write` and `users:write`; do not edit the database directly.

Set the target UUID from the protected handoff and keep API credentials out of shell history:

```bash
curl --fail --silent --show-error \
  --user "$LIBREONE_API_USER:$LIBREONE_API_PASSWORD" \
  --header 'Content-Type: application/json' \
  --data '{"application_id":1003}' \
  "https://one.libretexts.dev/api/v1/users/$REVIEWER_UUID/applications"
```

Revoke:

```bash
curl --fail --silent --show-error \
  --user "$LIBREONE_API_USER:$LIBREONE_API_PASSWORD" \
  --request DELETE \
  "https://one.libretexts.dev/api/v1/users/$REVIEWER_UUID/applications/1003"
```

The OIDC session cookie expires after 15 minutes. Revocation therefore takes effect no later than
15 minutes after the last successful OIDC flow. For an immediate test, clear the
`__Host-assessment-ai` cookie or visit `/oauth2/sign_out` before retrying.

## Deployment order

1. Back up the platform registry, LibreOne database, CAS service/configuration, Assessment AI
   Compose files, OIDC runtime files, and Caddyfile.
2. Install the new registry and update the expected registry digest for every enforcing runtime.
3. Reconcile application `1003` into LibreOne and confirm unrelated applications and
   `UserApplication` records are unchanged.
4. Install the runtime CAS descriptor; configure the read-only LibreOne attribute repository and
   authorization interrupt; deploy the CAS validator build; and restart CAS.
5. Grant the protected demo reviewer application `1003`.
6. Validate the proxy config *before* starting anything, which also catches an
   unedited `.env.oidc` (the example cookie secret is an invalid length, so this
   fails rather than booting on a secret published in this repository):

   ```bash
   docker run --rm --env-file .env.oidc \
     -v "$PWD/deploy/oauth2-proxy.cfg:/etc/oauth2-proxy.cfg:ro" \
     quay.io/oauth2-proxy/oauth2-proxy@sha256:10a1165743a192e1940b4708fb9647027185ce11a681a1c5519b442ff7f1f561 \
     --config=/etc/oauth2-proxy.cfg --config-test
   ```

7. Start `assessment-ai-oidc` while the live Caddy Basic Auth gate is still active.
8. Check `http://127.0.0.1:8194/ping`, OIDC discovery, and the callback redirect.
9. Validate and atomically reload Caddy with the OIDC `forward_auth` site block:
   `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
10. Run the acceptance checks below.

## Acceptance checks

- A fresh request redirects to LibreOne and does not return `WWW-Authenticate: Basic`.
- A reviewer with application `1003` reaches the queue and new audit records contain the LibreOne
  UUID.
- A user without application `1003` is denied by LibreOne.
- Caller-supplied `X-Reviewer`, `X-Reviewer-Email`, `Authorization`,
  `X-Auth-Request-*`, and `X-Forwarded-Access-Token` values never reach FastAPI.
- An ADAPT session created with **Campus Login** permits a silent CAS round trip into Assessment AI.
  ADAPT's direct email/password login does not create a CAS session.
- Publishing creates/reconciles the question with the dedicated role-5 ADAPT service account.
- `assess-ai-corpus.libretexts.dev` retains its existing Basic Auth and read-only method gate.

## Rollback

For the fast edge rollback, restore the Basic Auth block below.

> ⚠️ **Roll back the authentication, not the hardening.** An earlier version of
> this snippet was written before `main` gained the hardened CSP and
> `encode zstd gzip`, so following it during an incident would have silently
> regressed both — and dropped the `form-action` CAS origin, re-breaking the
> silent form-submission failure. The block below is `main`'s config: it swaps
> only the authentication and identity lines.

```caddyfile
assess-ai.libretexts.dev {
	import secheaders
	import gate
	header {
		Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
		Permissions-Policy "camera=(), geolocation=(), microphone=()"
		X-Frame-Options "DENY"
	}
	encode zstd gzip

	request_body {
		max_size 256KB
	}

	reverse_proxy 127.0.0.1:8093 {
		header_up -X-Assessment-AI-Proxy-Token
		header_up -{$ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER:X-Assessment-AI-Authenticated-Subject}
		header_up X-Reviewer {http.auth.user.id}
		header_up {$ASSESSMENT_AI_COMPUTATION_SPECIALIST_SUBJECT_HEADER:X-Assessment-AI-Authenticated-Subject} {http.auth.user.id}
		header_up X-Assessment-AI-Proxy-Token "{$ASSESSMENT_AI_COMPUTATION_TRUSTED_PROXY_TOKEN}"
	}
}
```

Notes on this rollback target, so nobody "fixes" it mid-incident:

- `form-action` has **no** CAS origin here, and should not. Under Basic Auth
  there is no cross-origin redirect to LibreOne, so the exception is unnecessary.
- `{http.auth.user.id}` is **correct here and only here.** `basic_auth` is the
  one module that sets it. It is fail-open under `forward_auth`, which is why
  the SSO config does not use it — see
  `adr/0001-forward-auth-identity-binding.md`.
- The two computation `header_up -X` / `header_up X` pairs are self-cancelling
  (`HeaderOps.ApplyTo` runs Delete after Set), so the specialist path stays
  inert. That is the pre-existing behaviour and it fails closed; do not try to
  repair it during a rollback.

Validate and reload Caddy, confirm Basic Auth works, then stop only `assessment-ai-oidc`. Do not
delete volumes and do not change the ADAPT publishing credential.

For a full rollback, first restore the Basic Auth edge. Then restore the prior registry and digest,
remove runtime descriptor/application `1003`, restore the LibreOne snapshot if reconciliation
changed anything unexpected, and restart the registry-enforcing runtimes.
