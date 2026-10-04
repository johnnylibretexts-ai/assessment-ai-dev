# AGENTS.md — assessment-ai

Assessment AI service (FastAPI + reviewer/generation workflow), deployed to the
LibreTexts.dev box as `assess-ai.libretexts.dev`.

Workspace-level rules — box access, deploy flow, the hard rules on the
`libretexts` GitHub org and the CXone sandbox write guard — live in the
`AGENTS.md` one directory up, in the workspace root that holds this checkout and
its siblings. Read that too; it is not restated here. (Deliberately not an
absolute path: this file is committed, and the workspace lives at a different
place on every machine that clones it.)

## Agent skills

### Issue tracker

GitHub Issues on `johnnylibretexts-ai/assessment-ai-dev`, always with an explicit
`--repo`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, label strings unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — root `CONTEXT.md` + `docs/adr/`, both of which exist. The
glossary fixes the vocabulary for the draft-to-published-item path and the code
is written in those words. See `docs/agents/domain.md`.
