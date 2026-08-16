# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This repo is **single-context**: one `CONTEXT.md` at the root, one `docs/adr/`.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists — it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

### Current state of this repo

- `CONTEXT.md` — **exists**, and covers the path from a generated draft to a published ADAPT item: *draft*, *revision*, *publication*, *publication attempt*, *publication step*, *step body*, *failure disposition*, *unreadable publication state*, *publication precondition*, *review gate*, *blocker*, *revision-scoped approval*, *attestation*, *reviewer*. Read it before naming anything in that area — the code is written in those words, and several entries exist specifically to say which near-synonym **not** to use. (The workspace one level up has its own root `CONTEXT.md` seeded with the identity cluster — that is the workspace glossary, a different document.)
- `docs/adr/` — **exists**, currently three decisions:
  - `0001-forward-auth-identity-binding.md` — how Assessment AI binds identity through oauth2-proxy `forward_auth` to the OIDC `sub`. Read it before touching anything in the auth, session, or user-identity path.
  - `0002-publication-preconditions-not-review-gates.md` — why there is no "gate" concept, only one enumerated list of publication preconditions. Read it before adding anything that must hold before a draft publishes; it also says why the `ck_publish_ready_requires_review_gates` constraint keeps its misleading name.
  - `0003-unreadable-publication-state-refuses-rather-than-resumes.md` — what a resume does with a publication state this build cannot read: it refuses, and writes nothing. **Accepted and implemented**; the guard is in the resume dispatch in `app/publishing.py` and a test pins the refusal.

## File structure

Single-context repo (most repos):

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

Multi-context repo (presence of `CONTEXT-MAP.md` at the root):

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_
