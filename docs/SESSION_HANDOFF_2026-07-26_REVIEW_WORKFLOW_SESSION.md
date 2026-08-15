# Session handoff — review/publishing workflow session → demo-assistant session

**Date:** 2026-07-26
**From:** the session that worked on the review/publishing workflow, the build gate, and QA
**To:** the session building the demo assistant chatbot (`feat/demo-assistant`, PR #9)
**Why:** we were both working this repo simultaneously on 2026-07-25/26 and nearly collided. This
records what changed under you, what now constrains your work, and what I deliberately left alone.

---

## 1. Read this first: the build gate is now real, and your branch predates it

Before 2026-07-25 the Dockerfile's `test` stage **never ran**. `runtime` derived from `base`, so a
plain `docker build` skipped ruff and pytest entirely — that is how every image up to that point was
produced. Three defects were fixed in sequence:

| PR | fix |
|---|---|
| #3 → `f50c414` | `runtime` now `COPY --from=test /app/.tests-passed`, making the test stage a real build dependency |
| #5 → `2db76dd` | the `&&` chain let one failure hide the rest; **a failing `ruff` still produced the marker**, so lint never blocked a build |
| #8 → `6ac1d73` | five browser regressions added as a fifth gated exit code |

The gate now requires **all five** to be zero:

```text
gate exit codes: lint=0 fmt=0 sidecar=0 rest=0 browser=0
```

If any is non-zero, `.tests-passed` is never written, `COPY --from=test` finds nothing, and **no
runtime image can be produced**. This is why a build that used to succeed may now fail for you.

**Your branch is based on `2db76dd`**, so it carries the four-check gate without the browser stage.
When PR #9 merges into `main` you inherit the five-check version, including
`tests/browser/` — which spins up a real Chromium against a disposable instance. Expect the first
post-merge build to take ~8 minutes (Chromium download, cached afterwards).

---

## 2. `main` was briefly not deployable — your PR #7 caused it, and nothing caught it

`169d19c` (PR #7, *"bound Gemini HTTP retries, wire the sandbox flag, guard ALTER TABLE"*) left a
formatting violation in `app/db.py`: one missing blank line before a comment block. Proven against
`main` with the full gate:

```text
822 passed, 1 skipped        <- tests fine
5 passed                     <- browser fine
gate exit codes: lint=0 fmt=1 sidecar=0 rest=0 browser=0
ERROR: failed to build
```

Everything else was green; the build still failed. Fixed in PR #10 (`902306c`), and `main` is
deployable again.

**This is not a criticism of that PR — it is a gap in the process.** Merging a PR does **not** run
the Docker gate. GitHub's `CLEAN` status reflects only CodeRabbit and Greptile. Nothing runs the gate
except an actual `docker build`, so a violation can sit on `main` invisibly until someone next
deploys. **Recommendation: add CI that runs `docker build --target test` on pull requests.** There is
no workflow doing that today, and this will happen again otherwise.

Practical guard until then: run `ruff format --check app tests` locally before merging anything.

---

## 3. Sequencing for the next deploy — this one matters

As of this writing:

- **`main` = `902306c`** — has the gate fixes, browser regressions, revision-number changes, the
  `db.py` fix. **Does not have the chatbot.**
- **production = `assessment-ai:assistant-70d308c`** (image `efcbb40b7255`, also tagged
  `assessment-ai:local`), rebuilt 2026-07-26 15:30 UTC — has the chatbot. **Does not have PR #7's
  fixes** (`_max_http_attempts`, `_SQL_IDENTIFIER_RE`, `_SQL_TYPE_RE` are absent from the running
  container, re-confirmed by grep *after* that rebuild) and does not have anything merged after
  `2db76dd`.

  This tag has already moved once: it read `assistant-c6d7546` (08:01) when this doc was first
  written. Both rebuilds came from `feat/demo-assistant` without merging `main`, so each one deepens
  the divergence instead of closing it — and `feat/generation-mode-lock` is now stacked on top of the
  unmerged branch. **Re-read the live tag before acting on it; do not trust the value printed here.**

So `main` and production diverge **in both directions**.

> **A rebuild from a `main`-based tree without merging PR #9 first will silently drop the chatbot
> from production.** I nearly did exactly this. Land PR #9, then rebuild — one build reconciles both
> directions.

Always check the live image before deploying:

```bash
docker inspect assessment-ai --format '{{.Config.Image}}'
```

---

## 4. PR #9 (your chatbot) — review status

Opened by the other session on your behalf because the branch was **live in production but had never
been pushed**; the only copies were a box release tree and `/tmp/demo-assistant.bundle`. Nothing was
rebased or edited — the commits are exactly as authored.

Both bots reviewed it for the first time. Two Majors were found **independently by both**:

| finding | status |
|---|---|
| `app/assistant/prompt.py` — `except Exception: drafts = []` makes a failed `list_drafts()` indistinguishable from an empty queue, so the assistant states "Draft queue: empty" as fact when the lookup failed | ✅ fixed in `c8a0d0f` |
| `app/assistant/store.py` — `ensure_conversation` is check-then-insert across **two separate sessions**, and `AssistantConversation.reviewer` carries only `index=True`, so nothing at the DB level prevents duplicates; two concurrent first-turn requests both read `None` and both insert, silently splitting a tester's history | ⚠️ **still open** |

Info-level: `_assistant.html` hardcodes `maxlength=4000` while `assistant_max_message_chars` accepts
100–50,000 (raising the limit then truncates in-browser); reset clears the log even when the server
refused; `max_turns` semantics versus history trimming; deprecated `clip` in CSS.

**Ignore the `http_guards.py:30` "truncates reviewer identity" finding as scoped to your PR.** Both
bots flagged it as new. It is not — `return value[:255]` already existed at `main.py:1570` in base
`2db76dd`, and your branch moved it verbatim. It is an app-wide convention worth fixing in its own
change, not inside a feature diff.

---

## 5. What else changed on `main` while you worked

| PR | what |
|---|---|
| #4 `9453632` | user-facing revision numbers are now **v1/v2/v3** everywhere (internal `edit_count` stays 0-based; `expected_edit_count` deliberately stays raw — it is the optimistic-concurrency token) |
| #5 `2db76dd` | lint failures can no longer slip past the gate |
| #6 `f4bc1be` | handoff corrected: **both AI reviewers stay on** (a same-day move to disable Greptile was reversed) |
| #8 `6ac1d73` | five browser regressions in `tests/browser/`, wired into the gate |
| #10 `902306c` | manual QA results recorded; `db.py` formatting fix |

Earlier the same day (before your branch): the sandbox startup/compute-budget fix. Spawned children
pay for a fresh interpreter plus SymPy/Pint/ucumvert imports, and the 2s budget was being spent
entirely on startup. The compute bounds are unchanged; they now measure computation.

---

## 6. Open decisions and issues

- **Issue #11** — manual QA scenario D: the plan says restoring saved hint text should clear the
  dirty flag; `review-workflow.js` latches it instead. It over-blocks, so it is safe.
  `tests/browser/test_review_workflow_browser.py` pins the **implemented** behaviour with a comment
  naming what to change if the plan wins. Needs a product call.
- **QTI download** applies `_require_same_origin` to a **GET**, so a pasted or bookmarked download
  URL returns 403. Minor, but it reads as an auth bug — it cost real time during QA.
- **No PR CI for the gate** (section 2).
- **Generation form — "Auto mix" and the item-type checkboxes — is not covered here.** It is covered
  in `docs/SESSION_HANDOFF_2026-07-26_GENERATION_MODE_LOCK.md`, written by the demo-assistant session
  for whoever picks up the generation form: whether ticking the item-type boxes does anything under
  Auto mix, what the count validation already does, and its own deploy warning. As of this writing
  that doc is **uncommitted**, in the `.worktrees/assessment-ai-generation-mode` worktree on branch
  `feat/generation-mode-lock` — if you cannot find it in the repo, it has not been pushed yet.

---

## 7. Manual browser QA — done, 53/54

The full A–I matrix ran on 2026-07-26 against a disposable SQLite database and a `FakeAdapt`. No real
draft touched, no ADAPT request made, production never involved. Results are in
`ASSESSMENT_AI_REVIEW_PUBLISHING_HANDOFF_2026-07-24.md` under "Manual browser QA results".

Relevant to you: scenarios A, C, D, F and G are now automated and **run in the gate**, so your merge
must keep them passing. `tests/browser/harness.py` gives you `disposable_instance()`, which serves a
seeded, fake-publishing app on a real port — useful for testing the assistant against a live server
without touching production.

---

## 8. What I deliberately did not touch

- **Your branch and your worktree.** No rebase, no commits pushed onto `feat/demo-assistant` beyond
  the initial push of your existing commits, no force. `git ls-remote` was checked first so that push
  created a new ref rather than overwriting anything.
- **The two open Majors on PR #9** — yours to decide and fix; you have the design context.
- **Production rebuilds** — left to you, since you must land PR #9 anyway.
- **The `http_guards.py` truncation** — pre-existing, wants its own change.

---

## 9. Coordination

We came close to clobbering each other twice: a routine redeploy from my release tree would have
reverted your chatbot, and my initial read of the situation wrongly asserted the deploy was
"another Claude session" when git authorship cannot distinguish us — everything on this machine
commits as `johnnylibretexts` and every SSH session on the box is that same user.

Practical rules that would have prevented it:

1. Check the live image before any deploy.
2. Never `docker compose build` in `/opt/libretexts/assessment-ai` — it is **not** the build source
   despite `build: .`; see `DEPLOY.md` beside that compose file, and the `assessment-ai-deploy-source-trap`
   memory entry.
3. Declare scope via `/kimi-multi-session` when working concurrently.
4. Shared memory under `~/.claude/projects/<workspace-slug>/memory/` is the one
   channel between sessions — the `assessment-ai-demo-assistant` entry carries the review findings.
