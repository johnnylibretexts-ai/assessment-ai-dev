# Assessment AI Review and Publishing Workflow Handoff

**Date:** 2026-07-24

**Audience:** Claude Code and the next human reviewer

**Status:** Implemented, tested, deployed, and awaiting independent code review plus broader manual QA

**Scope:** Assessment AI review/publishing workflow and ADAPT framework provisioning

## Read this first

This handoff covers the work prompted by these live reviewer failures:

- `conceptual hint cites paragraph(s) outside the item source: 47, 63`
- `This source is outside the currently curated framework. Do not choose a merely similar topic`
- failed Save/Approve actions appeared to reset the reviewer’s work
- the interface showed “Published to ADAPT” and “Approved — not yet published” at the same time

The fixes are already live. The most important remaining work is an independent review with
CodeRabbit and Greptile, manual browser evaluation, and a deliberate integration decision for two
branches whose correct review bases are **not the repositories’ current `main` branches**.

Do not publish Draft 16 as part of testing. Publishing creates a real ADAPT question and requires
explicit user authorization. Draft 14 is already the live publication-state example.

## Hard safety boundaries

These rules apply to every continuation of this work:

1. Write only to repositories owned by `johnnylibretexts`.
2. Never push to `github.com/libretexts/*`.
3. Never push ADAPT changes to `origin`; that remote is `kreut/libretext`. The only writable ADAPT
   remote is `johnnylibretexts-dev`.
4. The workspace root is not a Git repository. Work only inside the two isolated worktrees listed
   below.
5. Do not reset, pull, or clean the dirty live ADAPT checkout. The live checkout intentionally has
   approximately 110 local entries and is not the source of the running image.
6. Do not overwrite VPS `.env` or `docker-compose.yml` files. They contain live-only values.
7. Never run `docker compose down -v`.
8. Use one consolidated `ssh hostinger` command per step. Parallel SSH bursts can trigger
   fail2ban.
9. Do not expose Basic Auth, API keys, database credentials, identity UUIDs, or other secrets in
   commands, logs, PRs, or this document.
10. Do not modify or reset the 380-draft corpus demo. It is separate, read-only, and intentionally
    unchanged by this work.
11. Treat every AI review finding as a hypothesis. Reproduce it and inspect the surrounding
    invariants before applying a suggested fix.

## Source checkouts, branches, and exact review ranges

### Assessment AI

Local worktree:

```text
<workspace-root>/.worktrees/assessment-ai-math-rendering
```

Writable repository:

```text
https://github.com/johnnylibretexts/assessment-ai-dev.git
```

Branch:

```text
fix/review-publishing-workflow
```

Relevant commits:

```text
395e8cb docs: record publication-state rollout
ecddda4 fix: show publication state by draft revision
4ff4659 docs: record review workflow deployment
d7bcce6 fix: clarify review and publishing workflow
6bc57fe fix: canonicalize sealed corpus engine previews
```

The implementation review range is:

```bash
git diff 6bc57fe..ecddda4
```

The complete handoff/documentation range is:

```bash
git diff 6bc57fe..fix/review-publishing-workflow
```

**Do not review this branch blindly against `main`.** The branch was intentionally built on the
accepted `feat/math-rendering` source line at `6bc57fe`. A PR must target that feature branch, or a
temporary review-base branch pointing exactly to `6bc57fe`, so CodeRabbit and Greptile see only
the intended review/publishing work.

Current expected worktree noise:

```text
?? graphify-out/
```

Graphify output is generated locally and must not be committed.

### ADAPT

Local worktree:

```text
<workspace-root>/.worktrees/adapt-review-publishing-workflow
```

Writable remote:

```text
johnnylibretexts-dev -> https://github.com/johnnylibretexts/adapt-dev.git
```

Forbidden upstream remote:

```text
origin -> https://github.com/kreut/libretext.git
```

Branch:

```text
fix/review-publishing-workflow
```

Relevant commits:

```text
43137d01a fix: load legacy IMathAS assignment questions
f38a93a6e feat: provision assessment AI framework catalogs
0201a1b6d fix: label question expansion controls
```

The only valid implementation review range is:

```bash
git diff 0201a1b6d..43137d01a
```

ADAPT’s fork `main` is not a safe automatic PR base for this review. Create a temporary
`johnnylibretexts/adapt-dev` review-base branch at exactly `0201a1b6d`, or review the exact commit
range locally. Never create or push that base branch on `origin`.

Current expected worktree noise:

```text
?? graphify-out/
```

## What changed

### 1. Hint grounding now uses the exact question source

The canonical allowed set for every hint rung is now
`question.citation_paragraphs`, not the broader selected-concept excerpt.

The implementation:

- supplies only the question-cited paragraphs to the hint-generation prompt;
- explicitly gives the model the allowed citation list;
- validates generated hint ladders before inserting them;
- revalidates hints carried across a question edit;
- removes approval from any carried ladder after a question edit;
- leaves invalid carried hints visible as repair candidates instead of deleting or silently
  approving them;
- repeats grounding and answer-leak checks when saving, approving, and publishing.

The original Draft 16 failure was therefore legitimate: its question source did not include
paragraphs 47 or 63, even though those paragraphs were present in the broader selected concept.
The generator and the reviewer were previously enforcing different source boundaries.

### 2. Failed forms preserve reviewer input

The draft page now uses one shared server-side renderer for both GET requests and failed POST
requests.

On validation failure:

- the response is an inline HTTP 422;
- submitted hint text, citation values, review notes, and checked boxes are preserved;
- the exact invalid field and allowed paragraphs are shown;
- no new hint version, approval, or review-history row is written.

Successful writes still use POST/Redirect/GET with HTTP 303.

### 3. The approval workflow is explicit

Question approval and hint approval remain separate audit decisions. The interface presents them
as one four-part readiness checklist:

1. Question review
2. Hint review
3. Framework alignment
4. Publication

After an approval is saved, the active form is replaced with a durable approval summary. Hint
approval applies atomically to all three saved hint rungs. Editing a saved hint makes the form
dirty and disables approval until the edit is saved. The button is now labeled
“Approve saved hint version.”

### 4. Framework selection is exact and curated

The single hard-coded framework became a framework registry.

Assessment AI and ADAPT now both ship:

- the existing Fundamentals of General, Organic, and Biological Chemistry catalog;
- a new Mathematical Methods in Chemistry catalog with an exact mapping for the Draft 16 source.

Catalog validation rejects:

- duplicate framework titles;
- duplicate source URLs;
- duplicate stable ID namespaces;
- duplicate canonical topic URLs;
- invalid UUIDs or URLs;
- a forged topic submitted from another framework.

An unmapped source is a hard publication blocker. The reviewer cannot choose a merely similar
topic from an unrelated framework.

### 5. Publication labels are revision-aware

Publication is now derived from the saved draft revision, not merely from the draft’s approval
flags.

The intended state model is:

| Current state | Queue/detail label | Publication action |
|---|---|---|
| Current revision is successfully published | `Published to ADAPT` | Duplicate publish form hidden |
| Current revision is approved but has never been published | `Approved — not yet published` | Publish form available when otherwise ready |
| Earlier revision is published, current newer revision is not | Current revision explicitly says not published; earlier published revision and ADAPT question ID remain visible | Publish current revision only after review |
| Current revision is not approved/ready | Pending or blocked state with reasons | Publish unavailable |

Therefore, “published v1 or v2, current v3 not yet published” is valid and should be displayed
explicitly. The older ADAPT question remains published. Editing the draft does not silently update
or replace it.

Opening the editor does not create a new revision. A new revision exists only after a saved edit.

### 6. Assignment 44 now loads legacy IMathAS records alongside the published QTI item

After Draft 14 was published as ADAPT question 125, adding it to assignment 44 exposed an
unrelated legacy-data defect. Assignment 44 already contained IMathAS questions 77 and 86 whose
`technology_iframe` values were bare URLs rather than iframe HTML. The assignment formatter tried
to call `getAttribute()` on a nonexistent iframe DOM node and returned HTTP 500 before rendering
question 125.

Commit `43137d01a` changes the IMathAS runtime to:

- prefer the persisted positive `technology_id`;
- rebuild the runtime URL from ADAPT's configured IMathAS service;
- fall back to extracting an ID from older iframe HTML only when no persisted ID exists;
- generate future demo IMathAS records with actual iframe HTML.

No live question rows were rewritten. The exact authenticated assignment API now returns HTTP 200
with all five records:

```text
75:qti, 76:qti, 77:imathas, 86:imathas, 125:qti
```

## Main implementation map

### Assessment AI files

| File | Purpose |
|---|---|
| `app/adapt.py` | Multi-framework ADAPT lookup/provisioning expectations |
| `app/catalog.py` | Catalog registry, exact source mapping, uniqueness validation |
| `app/catalogs/mathematical-methods-chemistry-v1.json` | New curated framework seed |
| `app/db.py` | Hint invariants, current-version behavior, revision/publication queries |
| `app/hint_audit.py` | Pure read-only current-hint audit |
| `app/main.py` | Shared page renderer, preserved 422 responses, version-aware page context |
| `app/pipeline.py` | Exact hint-generation source and pre-persistence validation |
| `app/publishing.py` | Readiness checks, mapped-topic enforcement, fail-closed publication |
| `app/static/review-workflow.js` | Dirty hint detection and invalid-field focus |
| `app/static/styles.css` | Readiness and validation presentation |
| `app/templates/base.html` | Loads workflow behavior |
| `app/templates/draft.html` | Four-state review UI and version-aware publication UI |
| `app/templates/index.html` | Version-aware queue labels |
| `scripts/audit_hint_grounding.py` | Read-only SQLite audit CLI |
| `scripts/verify_framework_catalogs.py` | Cross-repository catalog digest verifier |
| `tests/test_adapt.py` | ADAPT contract coverage |
| `tests/test_catalog.py` | Registry and exact-mapping coverage |
| `tests/test_hint_audit.py` | Current-version-only, no-mutation audit coverage |
| `tests/test_main.py` | Form preservation, approvals, queue/detail publication states |
| `tests/test_pipeline.py` | Generation and carried-hint grounding invariants |
| `tests/test_publishing.py` | Readiness, forged topics, publication idempotency |

Full implementation statistics from `6bc57fe..ecddda4`:

```text
22 files changed, 2220 insertions(+), 230 deletions(-)
```

### ADAPT files

| File | Purpose |
|---|---|
| `app/Console/Commands/LibreTexts/ProvisionAssessmentAI.php` | Validate and provision every versioned framework seed atomically |
| `app/Console/Commands/LibreTexts/SeedDemo.php` | Seed future IMathAS demos with valid iframe HTML |
| `app/Question.php` | Resolve legacy IMathAS records from persisted IDs without parsing bare URLs as HTML |
| `resources/frameworks/mathematical-methods-chemistry-v1.json` | Matching ADAPT framework seed |
| `tests/Feature/QuestionsViewTest.php` | Legacy bare-URL and iframe-fallback assignment rendering tests |
| `tests/Feature/LibreTexts/ProvisionAssessmentAITest.php` | Multi-catalog, idempotency, and validation tests |

The ADAPT provisioning command now:

- loads a sorted glob of `resources/frameworks/*-v1.json`;
- validates every catalog before starting the database transaction;
- validates global uniqueness across all catalogs;
- provisions the existing service user, folder, frameworks, and levels in one transaction;
- is idempotent;
- preserves existing GOB IDs;
- does not print credentials or secrets.

## Catalog integrity evidence

The catalog copies in both repositories must remain byte-identical.

Expected SHA-256 values:

```text
e2e5cabebdc12c6d106726fbd19dcb9b8a1ac804238e1e7f2aa65a3d32b2931e  fundamentals-gob-chemistry-v1.json
005c078f778eb49cad63f6a688386aa3e813fe99e04b84ca4857c05f9c315bc8  mathematical-methods-chemistry-v1.json
```

Verify them with:

```bash
cd <workspace-root>/.worktrees/assessment-ai-math-rendering
uv run python scripts/verify_framework_catalogs.py \
  --adapt-root <workspace-root>/.worktrees/adapt-review-publishing-workflow
```

## Live deployment state

### Assessment AI

Live source commit:

```text
ecddda4
```

Current stable tag:

```text
assessment-ai:local
```

Current live image:

```text
sha256:91fc9eb28e386a247cb30ec16a1c5e496326a77c57fe4a4739c10202a8ef01c1
```

Rollback tags:

```text
assessment-ai:pre-publication-state-0c1ede5
assessment-ai:review-workflow-d7bcce6
```

Both rollback tags identify:

```text
sha256:0c1ede5f83c71a7511a124249e7691a3c7b8c6d36a2d98d8a0ff329af8de4a1c
```

### ADAPT

Live source commit:

```text
43137d01a
```

Current stable tag:

```text
adapt-app:build08-shadow-0201a1b6
```

Current live image:

```text
sha256:06e5f600c2d944b10ad9d837912daa8330ea651fb69e4f50b83946a0f65e56b5
```

Rollback tag:

```text
adapt-app:pre-imathas-runtime-d324f2b
```

Rollback image:

```text
sha256:d324f2bbe3b23f6e75ac182c8e2f3404ad7929ecde364ccfc322b0ab58e32532
```

Required live ADAPT networks, both of which were preserved:

```text
adapt_adapt
build08_adapt_engine_private
```

Required hinting configuration, also preserved:

```text
HINTING_V2_MODE=observe
HINTING_V2_PILOT_ASSIGNMENT_IDS=40
HINTING_V2_STAFF_PREVIEW=false
HINTING_V2_MASTERY_ENABLED=false
```

### Corpus demo

The read-only 380-draft corpus demo was not rebuilt, reset, or mutated.

Current corpus image:

```text
assessment-ai:corpus-math-6bc57fe
sha256:7d7d1d15ae23fac5cbc9fb77fe7bf98ff645557a9a441bc84d7eb05fa6ed20f8
```

## Backups and release artifacts

VPS backup directory:

```text
/opt/libretexts/backups/assessment-ai-review-workflow-20260724/
```

Contents:

```text
assessment-ai.db.pre-deploy
adapt-pre-provision.sql
assessment-ai.db.pre-publication-state-ecddda4
```

Expected checksum for the pre-publication-state SQLite backup:

```text
d94eafef56a38979f60ceafdcb6370d6ec5c2b8f1ba1145fbd8256ba88cc9a7a
```

Release sources/bundles:

```text
/opt/libretexts/.review-workflow-release/20260724/
```

Database restoration is a last resort. Do not restore the normal database merely to roll back
code, and never apply these backups to the corpus demo.

## Live data changes

### ADAPT frameworks

After provisioning:

- framework ID 1 is the existing GOB Chemistry framework with 281 levels;
- framework ID 2 is Mathematical Methods in Chemistry with 6 levels.

### Append-only hint repairs

Invalid current hint ladders were repaired by creating new versions. No old hint versions were
deleted or rewritten.

At repair time the replacements were deliberately unapproved:

```text
Draft 10, hint v2
  conceptual: [2, 4]
  strategic:  [3, 5]
  specific:   [3, 5]

Draft 13, hint v2
  conceptual: [83, 87]
  strategic:  [83, 86, 87]
  specific:   [83, 86, 87]

Draft 16, hint v2
  conceptual: [41, 42]
  strategic:  [39, 41]
  specific:   [41]
```

The final live read-only audit reported zero invalid current hint citations.

### Publications

After the user’s live Draft 14 publication:

```text
successful publications: 4
publication attempts:     11
```

Draft 14 current state:

```text
current revision:      1
publication ID:        4
ADAPT question/page ID: 125
finalized:             2026-07-24 17:48:23 UTC
technology:            QTI
public:                true
```

The queue and detail page both show:

```text
Published to ADAPT
Current revision 1 · Published to ADAPT as question 125
```

The duplicate publish form is absent for Draft 14.

Draft 16 current state:

```text
current revision: 1
question: approved (draft status ready_to_publish)
hints: NOT approved (edit count 1, ladders v1 and v2 both ready_for_review)
framework: exact Mathematical Methods topic mapped
publication: not yet published
```

Its publish form is present, but it must not be submitted without explicit user authorization.

## Automated testing already completed

### Assessment AI

Local final qualification:

```text
uv run ruff check .                                      PASS
uv run ruff format --check app tests                     PASS
uv run pytest -q                                         841 passed, 1 skipped
```

The only pytest notice was an existing Starlette deprecation warning.

Docker test target:

```text
tests/test_computation_service.py                         29 passed
remaining tests                                          812 passed, 1 skipped
```

Docker test image:

```text
sha256:4d8502be1a9359ef1630dccf340bc04ea6c60198941bd2d2a469f36e47044a17
```

Additional checks:

- MathJax vendor build passed.
- A live invalid-hint POST returned 422, preserved submitted state, and created no database row.
- Exact Draft 16 framework mapping was verified live.
- Unmapped-source publication blocking was verified.
- The final live hint audit returned zero findings.
- SQLite `PRAGMA quick_check` returned `ok`.

### ADAPT

Completed checks:

```text
PHP syntax checks                                         PASS
ProvisionAssessmentAI focused PHPUnit                    2 tests, 22 assertions
legacy IMathAS assignment PHPUnit                       2 tests, 7 assertions
candidate image build                                    PASS
live provisioning                                        PASS
idempotent re-run                                        PASS
authenticated assignment 44 API                         HTTP 200, 5 questions
```

The focused PHPUnit test used an isolated disposable MySQL database, not the live ADAPT database.

## Re-run the automated qualification

### Assessment AI full local suite

```bash
cd <workspace-root>/.worktrees/assessment-ai-math-rendering
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check app tests
uv run pytest -q
```

### Assessment AI focused workflow suite

```bash
cd <workspace-root>/.worktrees/assessment-ai-math-rendering
uv run pytest -q \
  tests/test_catalog.py \
  tests/test_hint_audit.py \
  tests/test_main.py \
  tests/test_pipeline.py \
  tests/test_publishing.py \
  tests/test_adapt.py
```

### Assessment AI Docker qualification

This executes the repository’s complete test target in a clean container:

```bash
cd <workspace-root>/.worktrees/assessment-ai-math-rendering
docker build --target test -t assessment-ai:review-workflow-test .
```

### Read-only hint audit

Run only against a copied SQLite database unless the live database URL and mount have first been
verified. The script converts SQLite URLs to read-only mode and examines only the newest current
hint version.

```bash
cd <workspace-root>/.worktrees/assessment-ai-math-rendering
uv run python scripts/audit_hint_grounding.py \
  --database-url sqlite:////absolute/path/to/copied-assessment-ai.db \
  --json
```

Expected result:

```json
{"finding_count": 0, "findings": []}
```

The script must exit nonzero when it finds invalid current rungs. Confirm that its input database
checksum is unchanged before and after the run.

### ADAPT focused suite

Never point this test at the live database. Use a disposable MySQL service/database, then run:

```bash
cd <workspace-root>/.worktrees/adapt-review-publishing-workflow
php -l app/Console/Commands/LibreTexts/ProvisionAssessmentAI.php
php -l app/Console/Commands/LibreTexts/SeedDemo.php
php -l app/Question.php
php -l tests/Feature/LibreTexts/ProvisionAssessmentAITest.php
vendor/bin/phpunit tests/Feature/LibreTexts/ProvisionAssessmentAITest.php
vendor/bin/phpunit tests/Feature/QuestionsViewTest.php --filter=imathas_runtime
```

Expected:

```text
ProvisionAssessmentAI: 2 tests, 22 assertions
legacy IMathAS runtime: 2 tests, 7 assertions
```

### Cross-repository catalog check

Always run after changing either catalog copy:

```bash
cd <workspace-root>/.worktrees/assessment-ai-math-rendering
uv run python scripts/verify_framework_catalogs.py \
  --adapt-root <workspace-root>/.worktrees/adapt-review-publishing-workflow
```

## Manual browser evaluation plan

Use a disposable local database or an explicitly designated test draft for mutation tests. Do not
edit or republish Draft 14 or Draft 16 solely for QA.

### A. Failed hint edit preserves state

1. Open an unapproved disposable draft.
2. Enter valid edits in all three hint fields.
3. Add one citation that is outside the question’s allowed paragraph set.
4. Click Save Hint Edits.

Expected:

- HTTP 422;
- the page remains on the draft;
- all text, citations, and notes remain exactly as submitted;
- the invalid citation field has an inline error and `aria-invalid`;
- the error states the exact allowed paragraphs;
- the first invalid field receives focus;
- no hint version or review-history row is inserted.

### B. Failed hint approval preserves confirmations

1. On a disposable draft, make the saved hint invalid through controlled fixture/setup.
2. Check all three confirmation boxes.
3. Enter a review note.
4. Click Approve Saved Hint Version.

Expected:

- HTTP 422;
- all three boxes remain checked;
- the note remains present;
- the invalid rung is identified;
- no partial approval is saved.

### C. Successful approval persists

1. Save a valid three-rung hint ladder.
2. Check all three confirmations and approve it.
3. Refresh the page and navigate away/back.

Expected:

- approval remains saved;
- the approval form is replaced by reviewer/timestamp summary;
- the readiness checklist says Hint review = Approved.

Repeat for question approval. Question and hint approval must remain independent.

### D. Dirty edits cannot be approved

1. Open an approved or ready hint version.
2. Change hint text or citations without saving.

Expected:

- approval is disabled;
- the UI explains that edits must be saved first;
- returning the saved text restores the clean state.

### E. Exact framework mapping

Check Draft 16 read-only:

- framework title is Mathematical Methods in Chemistry;
- topic is the exact curated 5.1 mapping;
- no unrelated GOB topic dropdown is offered;
- license comes from the mapped catalog.

With a fixture whose source URL is not mapped:

- framework status is blocked;
- publication is unavailable;
- forging a known topic ID in the POST is rejected server-side.

### F. Current revision already published

Check Draft 14 read-only:

- queue and detail both say `Published to ADAPT`;
- ADAPT question ID 125 is visible;
- duplicate publish form is absent;
- a success banner does not coexist with a current-revision “not yet published” badge.

### G. Earlier revision published, newer revision pending

Perform this only in a disposable database:

1. Publish revision N through a fake/integration ADAPT adapter.
2. Save an edit, creating revision N+1.
3. Approve revision N+1 but do not publish it.

Expected:

- current N+1 says approved/current revision not yet published;
- earlier N remains identified as published;
- its ADAPT question ID remains visible;
- current N+1 has a publish action only when every readiness gate passes;
- no second ADAPT POST occurs merely by opening or editing the draft.

### H. Accessibility and keyboard behavior

Evaluate at desktop and narrow viewport:

- tab order follows the visible workflow;
- every checkbox and input has a useful label;
- validation errors are announced through `role="alert"` and linked descriptions;
- focus moves to the first invalid field without trapping the keyboard;
- badge color is not the only state cue;
- MathJax content remains readable;
- run axe or an equivalent automated accessibility scan and manually verify any finding.

### I. QTI and ADAPT evidence

For an already published disposable/test item:

- the protected QTI download works only for an authorized reviewer;
- the downloaded package opens and represents the saved revision;
- the recorded ADAPT question ID exists;
- repeated publication requests reconcile to one remote question rather than creating duplicates.

Do not use Draft 16 for this test without authorization.

## CodeRabbit review procedure

CodeRabbit’s official command reference distinguishes a full review from an incremental review:
[`@coderabbitai full review`](https://docs.coderabbit.ai/reference/review-commands) reviews the
entire PR from scratch, while `@coderabbitai review` reviews only new changes.

### Prepare reviewable PRs

1. Confirm the only writable owner is `johnnylibretexts`.
2. Confirm the diff is exactly the intended range:

   ```bash
   git diff --stat 6bc57fe..fix/review-publishing-workflow
   git diff --stat 0201a1b6d..43137d01a
   ```

3. Create separate PRs for Assessment AI and ADAPT.
4. Target Assessment AI at `feat/math-rendering`/`6bc57fe`.
5. Target ADAPT at a temporary review-base branch pointing exactly to `0201a1b6d`.
6. Mark the PRs as draft while findings are being triaged.
7. Put the exact base and head SHAs in each PR description.
8. Paste the relevant hard safety boundaries and acceptance criteria from this handoff into the
   PR description.

Do not accept a PR showing hundreds of unrelated files. Close or retarget it before review.

### Trigger the first review

Post this as a top-level PR comment:

```text
@coderabbitai full review
```

Ask the human reviewer to focus the PR description on these risks:

- all hint write paths enforce the same exact question citation set;
- no 422 error path mutates data or loses submitted form state;
- approvals cannot survive a question/hint revision incorrectly;
- current and earlier publications cannot be conflated;
- duplicate publication is still idempotent under retries/concurrency;
- framework identity and topic selection cannot be forged;
- ADAPT provisioning validates all catalogs before its transaction and remains idempotent;
- legacy IMathAS records cannot crash an entire assignment and stored URLs cannot override a
  valid persisted technology ID;
- no secret, upstream write, corpus mutation, or destructive deployment behavior was introduced.

### Triage findings

For each CodeRabbit comment:

1. Record its severity, file/line, claim, and proposed failure mode.
2. Reproduce the claim with a focused test or executable example.
3. Classify it as valid, duplicate, intentional, or false positive.
4. If valid, add a regression test first when practical.
5. Apply the smallest fix that preserves the documented product decisions.
6. Run the focused tests, then the full repository suite.
7. Reply with evidence. Do not simply resolve the thread.

Do not use CodeRabbit Autofix on this branch without reviewing the patch line-by-line. Database,
publication, and deployment fixes are especially unsuitable for blind application.

After meaningful fixes, post:

```text
@coderabbitai review
```

That requests an incremental pass. Use another `@coderabbitai full review` only after a broad
rewrite or if the original review used the wrong base.

The official documentation also supports `pause`/`resume` during rapid updates. Do not run
`@coderabbitai resolve` until every thread has evidence; it resolves all CodeRabbit comments at
once.

## Greptile review procedure

The user referred to this as “grep tile”; the product name is **Greptile**.

Greptile’s official
[developer guide](https://www.greptile.com/docs/code-review/developer-essentials) says a PR can be
manually reviewed by commenting `@greptileai`; draft PRs can be reviewed with
`@greptileai review this draft`. Its
[trigger documentation](https://www.greptile.com/docs/code-review-bot/trigger-code-review) also
notes that the repository must be enabled/indexed and the PR branch must not be filtered out.

### Before triggering

1. Enable/index only the two `johnnylibretexts` repositories.
2. Verify the PR bases using the exact SHAs above.
3. Confirm Greptile is not indexing or reading the forbidden upstream as a writable target.
4. Give Greptile this handoff and `AGENTS.md` as context.
5. If repository configuration is added, review
   [Greptile’s `.greptile/` reference](https://www.greptile.com/docs/code-review/greptile-config-reference)
   first. Do not commit generated config merely to get this one review.

### Trigger a broad pass

On each draft PR:

```text
@greptileai review this draft
```

If no review appears, check repository enablement, indexing completion, draft behavior, and branch
filters before changing code.

### Ask targeted follow-up questions

Post these one at a time so each answer has a clear scope:

```text
@greptileai Trace every path that creates, edits, carries, approves, or publishes a hint.
Can any path accept a citation outside question.citation_paragraphs? Cite files and lines.
```

```text
@greptileai Review every HTTP 422 path in the draft workflow. Can any failed request either
mutate the database or lose submitted text, citations, notes, or checked confirmations?
```

```text
@greptileai Trace publication state from database queries through queue and detail templates.
Can a current published revision be labeled not published, or can an earlier publication be
mistaken for the current revision?
```

```text
@greptileai Review publication idempotency and concurrency behavior. Look for a route that can
create a duplicate ADAPT question after a timeout, retry, or concurrent request.
```

```text
@greptileai Trace framework and topic identity from committed catalog through UI, POST,
publication validation, and ADAPT provisioning. Can a forged or merely similar topic pass?
```

```text
@greptileai Review ProvisionAssessmentAI for validation-before-write, transactionality,
idempotency, preservation of existing IDs, and cross-catalog uniqueness.
```

```text
@greptileai Trace the IMathAS assignment-rendering path for records with a persisted technology
ID, a bare URL, valid iframe HTML, missing data, or conflicting URL/ID values. Can any legacy
record crash the whole assignment or redirect runtime traffic away from the configured service?
```

```text
@greptileai Look specifically for secret exposure, forbidden upstream writes, mutation of the
read-only corpus, destructive Docker behavior, or assumptions that would clobber live overrides.
```

Require file/line evidence and a concrete failure scenario. A low confidence score is not itself a
bug, and a high score does not replace testing.

### Triage Greptile findings

Use the same reproduce/classify/test/fix/retest loop as CodeRabbit. Do not use “Fix with your
Agent” or “Fix All” blindly. If sending a finding to Claude Code, include only that finding,
surrounding code, the applicable invariant, and the exact test command.

After fixes, manually retrigger Greptile and verify that its “last reviewed commit” matches the
new head SHA.

## Cross-review acceptance gate

This work is ready to integrate only when all of the following are true:

- CodeRabbit has reviewed the exact intended diff.
- Greptile has reviewed the exact intended diff.
- Every P0/P1 or equivalent finding is either fixed with a regression test or explicitly rejected
  with reproducible evidence.
- The Assessment AI full local suite passes.
- The Assessment AI clean Docker test target passes.
- The ADAPT focused PHPUnit suite passes against a disposable database.
- Framework catalog hashes match across repositories.
- The read-only hint audit reports zero findings on a copied current database.
- Manual tests A through I above pass. Test I is the QTI/ADAPT evidence and publication retry
  idempotency check; excluding it would let integration pass without validating the
  cross-service publication artifact or duplicate prevention.
- Draft 14 still shows current revision published with no duplicate publish button.
- Draft 16 remains not published unless the user explicitly authorizes publication.
- The authenticated assignment 44 question API returns HTTP 200 and includes question 125.
- The corpus demo image and behavior remain unchanged.
- The live Assessment AI and ADAPT health checks pass.
- ADAPT still has both required Docker networks and the accepted hinting-v2 observe settings.
- The final integration target and merge order have been approved by the user.

## Recommended next work

### P0 — Independent review

Run the CodeRabbit and Greptile procedures above against correctly based PRs. This is the next
required step.

### P0 — Manual browser QA

Execute the manual matrix against a disposable database/test draft, including accessibility and
the older-published/newer-unpublished state.

### P1 — Decide the integration strategy

The two branches were based on staged accepted source lines, not necessarily the current fork
`main` branches. Before merging:

1. fetch the writable forks;
2. compare the exact base and branch ancestry;
3. decide whether to merge the prerequisite feature branches first, rebase onto a new accepted
   integration branch, or keep the release commits as a deployment branch;
4. rerun both AI reviews after any rebase;
5. never force-push a branch another reviewer is actively using without coordination.

### P1 — Add a durable browser regression

The server-side test coverage is strong, but the most important product workflow deserves a
browser-level regression covering:

- failed-form preservation;
- approval persistence after reload;
- dirty-form approval disablement;
- current versus earlier publication labels;
- duplicate publish suppression.

Use a disposable local database and fake ADAPT adapter.

### P1 — Review accessibility

Run an automated axe scan and a keyboard/screen-reader smoke test. Fix confirmed issues without
changing the underlying review state model.

### P2 — Product polish to consider separately

These are not required to close the bug:

- decide whether user-facing revision numbers should be presented consistently as v1/v2/v3
  everywhere, independently of internal `edit_count`;
- link directly to the published ADAPT question if a stable authorized route is available;
- add a compact publication history panel if multiple revisions become common;
- add durable CodeRabbit/Greptile repository instructions after this first review demonstrates
  which guidance is useful.

Do not bundle these polish items into a bug-fix patch without separate review.

## Rollback procedures

Rollback only if live health or a confirmed regression requires it. Capture current container,
image, network, and database counts first.

### Assessment AI code rollback

On the VPS, retag the known rollback image as `assessment-ai:local`, then recreate only the
Assessment AI service from its existing live compose files:

```bash
cd /opt/libretexts/assessment-ai
docker tag \
  sha256:0c1ede5f83c71a7511a124249e7691a3c7b8c6d36a2d98d8a0ff329af8de4a1c \
  assessment-ai:local
docker compose up -d --no-deps --no-build --force-recreate assessment-ai
```

Verify health, queue rendering, publication counts, and logs. Do not restore SQLite unless the
database itself is proven corrupt or incompatible.

### ADAPT code rollback

Retag the known rollback image to the existing stable tag, then recreate only `app` with all
accepted override files:

```bash
cd /opt/libretexts/adapt
docker tag \
  sha256:d324f2bbe3b23f6e75ac182c8e2f3404ad7929ecde364ccfc322b0ab58e32532 \
  adapt-app:build08-shadow-0201a1b6
docker compose \
  -f docker-compose.yml \
  -f /opt/libretexts/.build08-overrides/adapt-final-promotion.override.yml \
  -f /opt/libretexts/.build08-overrides/adapt-shadow-observe.override.yml \
  -f /opt/libretexts/.build08-overrides/adapt-engine-network.override.yml \
  up -d --no-deps --no-build --force-recreate app
```

Immediately verify:

- both required networks are attached;
- hinting-v2 is still in observe mode with the accepted values;
- ADAPT health and login work;
- no `.env` or compose file changed.

Do not run `docker compose down`, and never run it with `-v`.

## Claude Code startup checklist

1. Read the workspace-root `AGENTS.md` completely.
2. Read this handoff completely.
3. Read `docs/REVIEW_PUBLISHING_WORKFLOW_FIX_PLAN.md`.
4. Inspect both worktrees and confirm only `graphify-out/` is untracked.
5. Confirm all remotes before any push:

   ```bash
   git -C <workspace-root>/.worktrees/assessment-ai-math-rendering remote -v
   git -C <workspace-root>/.worktrees/adapt-review-publishing-workflow remote -v
   ```

6. Run the exact diff commands and confirm the bases.
7. Run the focused tests and catalog verifier.
8. Prepare correctly based draft PRs in `johnnylibretexts/*` only.
9. Trigger CodeRabbit and Greptile.
10. Triage findings with evidence; do not auto-apply fixes.
11. Run the full and Docker suites after valid fixes.
12. Execute the manual browser evaluation on disposable data.
13. Report findings, test evidence, changed SHAs, and any live action before proposing integration.

## Final state to preserve

- Draft 14: current revision is published to ADAPT as question 125.
- Draft 16: approved and exactly mapped, but not published.
- Current revision publication and earlier revision publication are distinct states.
- Invalid reviewer submissions preserve input and write nothing.
- Every current hint citation is grounded in the exact question source.
- Only exact curated framework mappings can be published.
- Assessment AI normal database is healthy; corpus demo is untouched.
- ADAPT frameworks are provisioned idempotently.
- Assignment 44 loads questions 75, 76, 77, 86, and published question 125 without an API error.
- Legacy IMathAS bare URLs are resolved from their persisted technology IDs without rewriting data.
- Live rollback images and pre-deploy backups remain available.
