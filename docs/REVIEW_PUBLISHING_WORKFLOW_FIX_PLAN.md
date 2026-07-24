# Assessment AI Review and Publishing Workflow Fix Plan

**Date:** 2026-07-24
**Status:** Implemented, qualified, and deployed; bounded hint repairs are complete.
Human hint approval and publication remain intentionally pending.
**Assessment AI baseline:** `feat/math-rendering` at `6bc57fe`
**Writable remotes:** `johnnylibretexts/assessment-ai-dev` and `johnnylibretexts/adapt-dev` only

## Deployment closure — 2026-07-24

- Assessment AI source: `d7bcce6`; live image:
  `sha256:0c1ede5f83c71a7511a124249e7691a3c7b8c6d36a2d98d8a0ff329af8de4a1c`
- ADAPT source: `f38a93a6e`; live image:
  `sha256:d324f2bbe3b23f6e75ac182c8e2f3404ad7929ecde364ccfc322b0ab58e32532`
- Pre-deploy Assessment AI SQLite and ADAPT MySQL backups are stored under
  `/opt/libretexts/backups/assessment-ai-review-workflow-20260724/`.
- The live read-only hint audit reports zero findings after append-only repairs to drafts 10,
  13, and 16. Their replacement hint ladders remain unapproved.
- Existing publication state was preserved: 3 publications and 8 publication attempts.
- The read-only 380-draft corpus demo was not changed.

## Goal

Make review and publishing behave as a trustworthy, understandable workflow:

1. generated and carried-forward hints can never be approved with citations outside the exact
   question source;
2. failed validation preserves the reviewer’s entered text and checked boxes and points to the
   exact field that must be fixed;
3. question approval, hint approval, and publication readiness are visibly distinct and retain
   their saved state;
4. an already-approved question does not continue to present a misleading active approval button;
5. publishing is disabled when no curated framework covers the source, rather than offering a
   large list of unrelated topics;
6. draft 16 receives a legitimate Mathematical Methods in Chemistry alignment before it can be
   published.

## Confirmed Root Causes

### Hint grounding mismatch

The generator receives the entire selected-concept excerpt and validates generated hint citations
against `concept.source_paragraphs`. Approval and saving later validate the same hints against the
narrower `question.citation_paragraphs`. A hint can therefore be accepted at generation time but
rejected when a reviewer tries to save or approve it.

There are two additional persistence gaps:

- initial generated hint records are inserted without calling the repository’s canonical
  `_validate_hint_ladder(question, ladder)` invariant;
- `edit_draft()` copies the previous hint ladder to the new question revision without checking
  whether its citations are still valid for the revised question.

### Review form reset

Hint editing, hint approval, and question approval are three independent HTML forms. Error paths
redirect back to the GET page with only an error string. The redirected GET reloads database state,
so unsaved text and checked boxes disappear. This is an atomic validation failure, not a partial
save, but the UI makes it look like saved state was lost.

### Approval presentation

Question approval is persisted independently from hint approval, but the page always displays an
active “Approve draft” button. It does not present one aggregate publication-readiness state.
Consequently, an approved question with unapproved hints looks unfinished in several contradictory
ways.

### Framework mismatch

Assessment AI and ADAPT contain one hard-coded framework seed:
`Fundamentals of General, Organic, and Biological Chemistry`. Draft 16 comes from
`Mathematical Methods in Chemistry`. The UI detects that no exact URL mapping exists, but still
shows every unrelated GOB topic and leaves publishing available after manual selection.

## Live Baseline

A read-only audit of the normal live database found:

- 12 current hint ladders;
- invalid current rungs on drafts 10, 13, and 16;
- draft 16 conceptual hint has invalid citations 47 and 63;
- 3 successful publications;
- no successful publication contains a hint citation outside its published question snapshot.

Existing successful publications must remain immutable.

## Product Decisions

1. **Keep question and hint approvals as separate audit decisions.** Do not merge their records or
   silently approve one with the other.
2. **Present them as a readiness checklist, not as ambiguous adjacent forms.** The reviewer can see
   Question, Hints, Framework, and Publication states together.
3. **Keep hint approval atomic.** All three confirmations are saved together only after every rung
   passes validation. Do not persist misleading partial approval.
4. **Preserve failed submissions in the rendered response.** Errors return an inline HTTP 422 page
   populated with the submitted values. Successful writes retain POST/Redirect/GET.
5. **Do not infer or silently repair citations.** Invalid existing ladders remain unapproved and
   are repaired through a new append-only hint version after human review.
6. **Do not permit arbitrary framework selection.** Publication alignment is derived from a
   committed curated source mapping. An unmapped source is a hard blocker.
7. **Add a real Mathematical Methods in Chemistry framework.** Do not map differential equations
   to a vaguely related GOB topic.
8. **Do not rewrite existing publications or delete old hint versions.**

## Implementation Sequence

### Phase 0 — Isolated branches, backup, and invariant fixtures

**Assessment AI**

- Create an isolated worktree and branch from `feat/math-rendering`:
  `fix/review-publishing-workflow`.
- Capture the baseline test results and current image digest.
- Add reusable test fixtures representing:
  - a concept citing paragraphs 39–63;
  - a question citing only paragraphs 39–42;
  - a hint ladder citing 41, 42, 47, and 63.

**ADAPT**

- Create an isolated worktree from the current accepted `johnnylibretexts/adapt-dev` source line.
- Confirm the push remote owner is `johnnylibretexts`; never push to `origin`
  (`kreut/libretext`) or `libretexts/*`.
- Before live deployment, back up the Assessment AI database and record the exact live Assessment
  AI and ADAPT image digests. Do not copy or replace live `.env` or `docker-compose.yml`.

### Phase 1 — Establish one canonical hint-grounding invariant

**Modify**

- `app/pipeline.py`
- `app/db.py`
- `tests/test_pipeline.py`
- `tests/test_generation_qualification.py`
- add focused repository tests if needed

**Changes**

1. Make `question.citation_paragraphs` the single allowed set for every current hint rung.
2. Render only the question-cited paragraphs into the hint-generation source block.
3. Add an explicit `<allowed_hint_citations>` list to the prompt and bump
   `HINT_PROMPT_VERSION`.
4. Change `_validate_hint_grounding()` to validate against the exact revised question, not the
   broader concept.
5. Call `_validate_hint_ladder(revised_question, ladder)` inside
   `replace_generated_drafts()` before inserting a generated hint record.
6. In `edit_draft()`:
   - copy a previous ladder as `ready_for_review` only when it remains valid;
   - otherwise carry it as an unapproved repair candidate with a computed `needs_repair` state,
     zero confirmations, and a precise system note;
   - never carry approval across a question edit.
7. Keep publication-time validation as a final fail-closed check.

**Acceptance**

- A generated hint citing paragraph 47 for a question citing 39–42 is rejected before persistence.
- A provider cannot bypass the invariant by returning a schema-valid ladder.
- Editing a question cannot produce an apparently approvable invalid ladder.
- A valid carried ladder remains editable but unapproved for the new question revision.

### Phase 2 — Model structured hint validity and publication readiness

**Modify**

- `app/db.py`
- `app/publishing.py`
- `app/main.py`
- optionally add `app/readiness.py`
- `tests/test_publishing.py`
- `tests/test_main.py`

**Changes**

1. Add a pure hint-grounding inspection result that identifies, per rung:
   - allowed paragraph numbers;
   - submitted paragraph numbers;
   - invalid paragraph numbers;
   - answer-leak flags.
2. Derive a current hint state:
   `missing`, `needs_repair`, `ready_for_review`, or `approved`.
3. Add a publication-readiness result containing all local blockers at once, with stable codes:
   - `question_not_approved`;
   - `hints_missing`;
   - `hints_need_repair`;
   - `hints_not_approved`;
   - `engine_validation_missing`;
   - `computation_evidence_invalid`;
   - `license_unresolved`;
   - `framework_unmapped`;
   - `publishing_not_configured`.
4. Use the same readiness rules for the GET view and the publication service. The final POST still
   repeats transaction-bound and remote ADAPT checks.
5. Ensure an `approved` hint status never overrides failed current grounding.

**Acceptance**

- Draft 16 reports Question = approved, Hints = needs repair, Framework = unmapped, and
  Publication = blocked.
- The page shows all blockers rather than revealing them one failed submission at a time.
- A forged POST cannot bypass a blocker hidden by the UI.

### Phase 3 — Preserve failed form state and show field-level errors

**Modify**

- `app/main.py`
- `app/templates/draft.html`
- `app/static/styles.css`
- `tests/test_main.py`

**Changes**

1. Refactor the draft-page context builder so GET and failed POST responses use the same rendering
   path.
2. On hint-edit validation failure, return HTTP 422 with:
   - all submitted hint text;
   - all submitted citations;
   - the edit note;
   - per-rung inline errors;
   - the exact allowed paragraph list.
3. On hint-approval failure, return HTTP 422 with:
   - the submitted confirmation checks preserved;
   - review notes preserved;
   - inline grounding or answer-leak errors.
4. On question-approval failure, preserve its confirmations and notes in the same way.
5. Keep 303 redirects only for successful writes.
6. Add `role="alert"`, `aria-invalid`, and `aria-describedby` associations and focus the first
   invalid field as progressive enhancement.

**Acceptance**

- Clicking Save with `41, 42, 47, 63` keeps those values visible and highlights only `47, 63`.
- Checked confirmation boxes remain checked on a failed approval response.
- No failed POST creates a hint version, approval record, or review-history event.

### Phase 4 — Replace the ambiguous button cluster with a readiness checklist

**Modify**

- `app/main.py`
- `app/templates/draft.html`
- add `app/static/review-workflow.js`
- `app/static/styles.css`
- `tests/test_main.py`
- `tests/test_browser_canary.py`

**UI**

Create four visible states near the top of the review pane:

1. **Question review** — Pending or Approved, including reviewer and timestamp.
2. **Hint review** — Missing, Needs repair, Ready for review, or Approved.
3. **Framework alignment** — Exact curated topic or Blocked.
4. **Publication** — Ready or a concise blocker list.

**Behavior**

- Rename “Approve draft” to “Approve question”.
- Hide the active approval button after the current question revision is approved; show a durable
  approved summary instead.
- Hide the hint confirmation form while the ladder needs repair.
- After a successful hint save, clearly state that only hint confirmations were reset.
- While a reviewer changes hint text or citations in the browser, disable hint approval and show
  “Save these changes before approval.”
- After successful all-three approval, replace the active controls with an approved summary and
  keep a deliberate “Edit hints” path.
- Explain that editing the question creates a new revision and resets both approvals; editing only
  hints creates a new hint version and resets only hint approval.
- Keep all correctness rules server-side; JavaScript only improves feedback.

**Acceptance**

- Refreshing after a successful approval preserves and visibly reports the approval.
- An already-approved question never looks like it still requires the same approval.
- The reviewer cannot accidentally check confirmations for stale, unsaved hint text.

### Phase 5 — Replace the single hard-coded framework with a registry

**Assessment AI files**

- `app/catalog.py`
- `app/catalogs/fundamentals-gob-chemistry-v1.json`
- add `app/catalogs/mathematical-methods-chemistry-v1.json`
- `app/adapt.py`
- `app/main.py`
- `app/publishing.py`
- `tests/test_adapt.py`
- `tests/test_publishing.py`

**Changes**

1. Introduce `CuratedFramework` and `CuratedAlignment` values.
2. Load and validate every committed framework seed in `app/catalogs/`.
3. Enforce globally unique framework IDs, chapter IDs, topic IDs, and normalized canonical topic
   URLs.
4. Replace `chemistry_seed()`/`suggested_topic()` call sites with:
   - `curated_frameworks()`;
   - `alignment_for_source(source_url)`;
   - `alignment_by_topic_id(topic_id)`.
5. Build a reviewed `Mathematical Methods in Chemistry v1` seed from the book’s canonical table of
   contents, not from model inference. Include the exact canonical URL for draft 16’s
   “5.1: Second Order Ordinary Differential Equations”.
6. Make the mapped framework and topic read-only in the publication panel. Keep the human
   alignment confirmation checkbox.
7. When no exact curated mapping exists:
   - display “Publishing blocked: this source does not yet have a curated framework topic”;
   - show no unrelated topic dropdown;
   - render no enabled publish button.
8. On POST, derive the expected alignment from the stored source URL and reject any supplied topic
   that does not exactly match it.

**Acceptance**

- Draft 16 resolves to Mathematical Methods in Chemistry / its exact 5.1 topic.
- A GOB Chemistry source still resolves to the existing framework with unchanged stable IDs.
- An unknown source is blocked both in the UI and service layer.
- A forged GOB topic for draft 16 is rejected.

### Phase 6 — Generalize ADAPT’s idempotent framework provisioning

**Modify in an isolated `johnnylibretexts/adapt-dev` worktree**

- `app/Console/Commands/LibreTexts/ProvisionAssessmentAI.php`
- `tests/Feature/LibreTexts/ProvisionAssessmentAITest.php`
- add `resources/frameworks/mathematical-methods-chemistry-v1.json`
- retain `resources/frameworks/fundamentals-gob-chemistry-v1.json`

**Changes**

1. Load a sorted manifest/list of approved Assessment AI framework seeds instead of one constant
   seed file.
2. Validate every seed before opening the provisioning transaction.
3. Provision each framework and its levels idempotently for the existing Assessment AI service
   user.
4. Preserve the existing service user, folder, GOB framework, level IDs, and published items.
5. Print all provisioned framework IDs without printing secrets.
6. Add a cross-workspace verification script or deployment check that confirms the Assessment AI
   and ADAPT copies of each seed have identical SHA-256 hashes.

**Acceptance**

- Running the command twice creates no duplicates.
- The existing GOB framework is unchanged.
- The new Mathematical Methods framework and exact draft 16 topic exist in ADAPT.
- No upstream `origin` write occurs; commits target only `johnnylibretexts/adapt-dev`.

### Phase 7 — Bounded existing-data repair

**Add**

- `scripts/audit_hint_grounding.py`
- tests for dry-run and apply boundaries
- a short runbook section in this document or a companion deployment runbook

**Behavior**

- Default to read-only dry-run.
- Inspect only the newest hint version for each draft’s current `edit_count`.
- Report draft ID, hint version, rung, allowed citations, and invalid citations.
- Never rewrite an old hint record.
- Do not automatically delete citations or claim that remaining citations support the prose.
- Repair through the normal Save Hint Edits path, which appends a new unapproved version.
- Re-run the audit after repairs and require zero invalid current rungs.

**Known live repair set**

- Draft 10: conceptual and specific rungs cite invalid paragraph 6.
- Draft 13: conceptual rung cites invalid paragraphs 35, 53, and 95.
- Draft 16: conceptual rung cites invalid paragraphs 47 and 63; the expected manual edit is
  `41, 42`, subject to reviewer confirmation that those paragraphs support the hint.

No successful publication currently requires repair. If a future audit finds an invalid immutable
publication snapshot, stop and use a separate manual remediation plan; do not edit it in place.

### Phase 8 — Test and qualification gates

**Assessment AI**

Run focused tests first, then:

```text
uv run pytest -q
uv run ruff check .
npm ci --ignore-scripts
npm run vendor:mathjax
docker build --target test -t assessment-ai-review-workflow-test .
docker build --target runtime -t assessment-ai:review-workflow-candidate .
```

Add browser coverage for:

- valid save → confirmation reset notice;
- invalid save → inline error and preserved values;
- invalid approval → preserved checks;
- approved question and approved hints surviving refresh;
- dirty hint edits disabling approval;
- mapped and unmapped framework panels;
- full keyboard operation, focus order, and axe A/AA;
- MathJax previews continuing to update after a 422 render.

**ADAPT**

- Run the focused provisioning feature test.
- Run the existing Assessment AI publishing contract tests.
- Build the candidate from the accepted fork source and verify the current live override networks
  and configuration remain intact.

**Cross-service canary**

Use a disposable canary draft, not draft 16, to prove:

1. valid hint edits create a new version;
2. all-three approval persists;
3. question approval persists;
4. exact framework alignment resolves;
5. one canary publish reaches the owned Assessment AI folder;
6. retry remains idempotent and does not create a duplicate;
7. QTI and publication evidence bind the exact approved question, hint version, source, license,
   and framework.

### Phase 9 — Deployment order

1. Record live health, image digests, current publication count, and database checksum.
2. Back up the normal Assessment AI database. Do not touch the separate 380-draft corpus database.
3. Deploy the additive ADAPT provisioning code from the isolated fork worktree.
4. Run the idempotent provisioning command once and verify both frameworks through read-only API
   calls.
5. Build and deploy the Assessment AI candidate image without replacing the box-local `.env` or
   compose file.
6. Run health and read-only readiness checks.
7. Run the hint audit in dry-run mode; confirm the expected drafts 10, 13, and 16.
8. Repair those drafts through normal append-only hint saves; leave them unapproved.
9. Have a human reviewer approve the corrected hints and, where needed, the current question.
10. Verify draft 16 shows the exact Mathematical Methods alignment.
11. Publish draft 16 only after explicit user authorization; publication creates a public ADAPT
    item and is not part of automatic deployment.
12. Update Graphify for both changed component trees after code changes.

The read-only corpus demo remains unchanged. It receives no writable workflow JavaScript, provider
credentials, framework publishing controls, or database migration.

## Rollback

- Restore the previous Assessment AI image and prior Caddy configuration if changed.
- Restore the previous ADAPT image/override; do not reset or pull over the dirty live checkout.
- Leave the newly provisioned Mathematical Methods framework in place if unused; it is additive
  and safer to leave dormant than delete.
- Old hint versions remain immutable. If a new repair version is wrong, append another corrected
  version; do not delete history.
- Existing successful publications remain untouched.
- Database restoration is a last resort and must target only the normal Assessment AI database,
  never the corpus demo or ADAPT volumes.

## Definition of Done

- New generation cannot persist a hint citation outside the exact question source.
- Draft edits cannot carry an invalid ladder forward as apparently approvable.
- Invalid form submissions preserve reviewer text, checks, and notes.
- Question and hint approvals visibly persist after refresh.
- Approved controls are replaced by clear saved-state summaries.
- Publication readiness lists every blocker in one place.
- Unmapped sources cannot select unrelated framework topics in the UI or by forged POST.
- Draft 16 maps to a committed Mathematical Methods in Chemistry topic.
- Live drafts 10, 13, and 16 have zero invalid current hint citations after human repair.
- Existing successful publications remain byte-for-byte and database-record immutable.
- Full Assessment AI, ADAPT contract, browser accessibility, and cross-service canary suites pass.
- Deployment and rollback evidence records exact image digests and database checksums without
  exposing secrets.
