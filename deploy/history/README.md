# Archived edge configs — **not deployable**

Nothing in this directory is live, and nothing here should ever be copied to the box. The live edge
config is `../Caddyfile.assess-ai`. These are prior *production* states preserved because they
existed **only on the VPS** and matched no commit in this repo.

## Why they are here at all

During the period these cover, the box's `Caddyfile.assess-ai` had drifted from the repo — edits
were applied in place on the VPS and never committed. When PR #27 reconciled the edge config, the
pre-reconciliation states survived only as `.bak-*` files in
`/opt/libretexts/assessment-ai/deploy/` on the box.

A 2026-08-08 hygiene pass was about to shred them as strays. They are not strays: checked against
**every** commit that has ever touched `deploy/Caddyfile.assess-ai`, neither hash appears. Deleting
them would have destroyed the only record of two production configurations — the same reasoning
that keeps the 46 retained `adapt-app` images rather than reclaiming the disk.

Committing them here makes the box copies genuinely redundant, so those can now be shredded.

## What each one is

| file | sha256 | bytes | what it is |
|---|---|---|---|
| `Caddyfile.assess-ai.2026-08-01T16-54-46Z` | `c57e62abcf25be1c…` | 1462 | **Before** the CSP fix. `form-action 'self'` — the state in which an expired-session form POST failed *silently*, because the browser blocked the cross-origin navigation to CAS. |
| `Caddyfile.assess-ai.2026-08-03T16-39-04Z` | `a071ca278518a9f7…` | 1489 | **After** the CSP fix, **before** PR #27. `form-action 'self' https://one.libretexts.dev`. This is the file PR #27's deploy runbook pre-checked against. |

The two differ by **exactly one line** — the CSP `form-action` directive. Both are 54 lines; the
current reconciled config is 132.

Both are credential-free: their only auth-adjacent content is `header_up -Authorization` and
`header_up -X-Auth-Request-Access-Token` strip directives. No Basic Auth hashes, no tokens.

## Deliberately not a `git mv` or a rewrite

These are recorded as new files at their real content, not grafted into the history of
`deploy/Caddyfile.assess-ai`. The point is to preserve the bytes and their provenance, not to
pretend the repo always had them. The filenames carry the UTC timestamp of the box backup they came
from, so they can be matched back to the runbooks in
`.scratch/assessment-ai-sso/` that reference those hashes.
