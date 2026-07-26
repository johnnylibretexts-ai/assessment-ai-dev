# Session handoff — demo-assistant session → generation-form session

**Date:** 2026-07-26
**From:** the session that built the demo assistant (`feat/demo-assistant`, PR #9)
**To:** whichever session owns the generation form / advanced-items UI
**Why:** the owner asked a generation-form UX question in this session by mistake. I answered it and
started building, then stopped. This hands over the answer, the half-built change, and two facts
about `main` that matter to anyone touching that form.

This is the reply to `docs/SESSION_HANDOFF_2026-07-26_REVIEW_WORKFLOW_SESSION.md` (branch
`docs/manual-browser-qa`, PR #12), which handed off to me. Same convention.

---

## 1. The question, and the answers straight from the code

The owner asked three things about the "What should Assessment AI create?" fieldset on `/`. All
three are settled by reading `app/pipeline.py`, `app/jobs.py`, and `app/main.py` — no guesswork.

### Q. Under **Auto mix**, is there any point ticking the item-type checkboxes?

**No. They are discarded server-side.** `app/jobs.py:143`:

```python
selected = list(request.item_types) if request.generation_mode == "selected" else None
```

In auto mode `item_types` never reaches the pipeline. It is not a weighting, not a hint — the
request is built as though nothing was ticked. A tester can carefully select "Matching" and
"Matrix", generate, get neither, and reasonably conclude the app ignored them. It did.

**This is the defect worth fixing.** The form currently invites input it throws away.

### Q. Auto mix with a total of 4 — does that give four random types?

Total is right, **random is wrong**. `app/pipeline.py:1141`:

```python
return tuple(selected[index % len(selected)] for index in range(item_count))
```

That is a round-robin over `AUTO_ITEM_TYPES` (`pipeline.py:85`) in **fixed declaration order**. Auto
mix with a total of 4 always yields, in order:

1. multiple choice
2. matching
3. ordering
4. multiple response

Every run, every source. There is no shuffle anywhere in this path.

Two consequences. Demoing the same count twice in front of the same audience produces an identical
type sequence — worth knowing before a live demo. And the label "Auto mix" oversells it; it is a
fixed rotation. If anyone wants real variety, that is a **pipeline change**, not a UI change, and it
would need care: `_resolve_item_types` is deterministic today and at least one test
(`tests/test_advanced_items.py:33`) depends on the total-across-types contract.

There is also a redundant special case at `pipeline.py:1134-1138`: `item_count == 1` in auto mode is
hardcoded to `MULTIPLE_CHOICE`, which is already `AUTO_ITEM_TYPES[0]`, so the branch changes nothing.
Harmless; noted so nobody assumes it is load-bearing.

### Q. Should "Total number of items" be relabeled "number of questions per selection"?

**No — that inverts the meaning, and would break the error message.** The field is the total. Two
independent places depend on that reading:

- `pipeline.py:1141` returns exactly `item_count` items regardless of how many types are ticked.
- `main.py:372` **rejects** `item_count < len(item_types)` with: *"Set Total number of items to at
  least the number of selected item types."*

So three types with a total of 4 produces **4 items** (one type repeating), not 12. The current label
is correct, the help text at `index.html:57` already says so explicitly, and the validation error
quotes the label by name — renaming the field would make that message incoherent.

**Recommendation: leave the label alone.**

---

## 2. Two things about `main` that will waste your time if you miss them

### The count-minimum validation already exists. Do not rebuild it.

`origin/main` (`902306c`) already has, in `app/static/generation.js`: `setCustomValidity` on the
total, auto-raising the total when you tick more types than the current count, and the 8-type cap
that disables unticked boxes at the ceiling. I proposed building this before checking, and was
wrong — it landed while this session was elsewhere.

### `fix/generation-license-ux` is stale and superseded

The worktree `.worktrees/assessment-ai-generation-license-ux` (`fbded43`, *"Autofill source licenses
and clarify generation totals"*) is **unmerged** and its `generation.js` / `index.html` changes are an
**earlier, weaker version** of what is already on `main` — its help text lacks the 8-type wording and
the "Select All That Apply is a question format" clarification that `main` has.

Do not merge that branch to get the totals work; you already have better. If it holds anything still
wanted, it is the **license autofill** in `app/content.py` / `app/catalog.py` / `app/db.py`, which is
a separate concern from the form UX and which I did not evaluate.

---

## 3. What I built, and where it is sitting

A worktree off current `origin/main`, containing the one genuinely missing piece — the auto-mix lock:

```
.worktrees/assessment-ai-generation-mode   [feat/generation-mode-lock]  base 902306c
```

**Status:** rebased onto merged `main` (`23f84df`) and committed on this branch. Two files, +21/-2:

| file | change |
|---|---|
| `app/static/generation.js` | in auto mode, disable every item-type checkbox, add `is-locked` to `#item-type-options`, and swap the help text for an auto-mode explanation; restore on switch to Choose types |
| `app/static/styles.css` | `.item-type-grid.is-locked { opacity: .5 }` plus `cursor: not-allowed` |

Design notes, so you can accept or reject them deliberately:

- **Checked boxes are preserved, not cleared, when switching to auto.** Switch back to Choose types
  and the selection is intact. Clearing would silently destroy work on a mis-click.
- **The lock is JS-only, deliberately.** I did not render `disabled` in the template. If
  `generation.js` fails to load, the current behaviour (boxes clickable, ignored under auto) is
  merely misleading; a server-rendered `disabled` would make Choose types **completely unusable**
  without JS. Misleading beats broken. The script is `defer`red (`base.html:11`) so the unstyled
  flash is negligible.
- **The lock only engages when the mode control is actually readable** — `locked` requires both
  `#generation_mode` and `#item-count` to exist, so a markup change that removes them degrades to a
  usable form rather than a permanently frozen one.

### What is NOT done

- **No tests.** See the warning below — this needs a real one, not a substring assertion.
- **Not run through `ruff` / `ruff format` / pytest / `docker build --target test`.**
- **Not deployed, not pushed, no PR.** The box is untouched.

To drop it entirely: `git worktree remove .worktrees/assessment-ai-generation-mode --force` and
`git branch -D feat/generation-mode-lock`.

---

## 4. If you finish it: please do not test this the way this repo currently tests JS

The existing pattern (`tests/test_main.py:284-288`) fetches `/static/generation.js` and asserts
substrings of the source:

```python
assert "sourceInput.readOnly = true" in generation_script.text
```

That proves the characters are in the file. It proves nothing about whether a checkbox is actually
disabled in a browser. **The demo assistant shipped three separate defects that every markup-and-
substring assertion passed happily**: replies rendered white-on-white, the panel could not be closed
because `[hidden]` lost to `display: flex`, and an open panel put a destructive "New" button exactly
where the launcher had been. Each was invisible to source-text tests and obvious within seconds in a
browser.

This change is in the same category — its entire observable behaviour is computed state
(`checkbox.disabled`) and applied CSS. A substring test would lock in the *implementation* while
leaving the *behaviour* unverified.

Two things worth doing instead:

1. **A browser test.** `main` now has a gated `tests/browser/` stage (fifth gate exit code,
   real Chromium against a disposable instance) — per the inbound handoff, that is the right home,
   and it runs in the build gate. Assert `is_disabled()` on the checkboxes after selecting each mode,
   and assert the count field's validity message. There is also an ungated Playwright suite at
   `e2e/` if you want a live-stack check, but note `e2e/` is **not a git repo** (Mac + box only) and
   its `global-setup.ts` CAS login is currently failing, which blocks every project in it.
2. **A CSS guard.** The JS toggles `is-locked`; if that class is ever dropped from `styles.css` the
   toggle silently becomes a no-op and the boxes look enabled while being disabled — a worse state
   than today. One assertion that the stylesheet defines the class is cheap insurance. This exact
   failure mode (JS toggling a class the stylesheet did not honour) is what broke the assistant
   panel's close button.

---

## 5. What I deliberately did not touch

- **`app/pipeline.py`, `app/jobs.py`, `app/main.py`** — the generation path is the qualified
  pipeline. This change is presentation only: no request the server receives is different, and every
  server-side validation still runs unchanged. Keep it that way unless you actually want the
  auto-mix ordering to change, which is a much larger conversation.
- **`app/templates/index.html`** — no markup change was needed; `#item-type-options` and
  `#item-type-help` already exist and are already wired to `aria-describedby`.
- **The 380-draft corpus demo** (`assess-ai-corpus.libretexts.dev`) — read-only, separate DB, Caddy
  blocks mutating methods. Nothing here goes near it.
- **Deploy.** Live `assess-ai.libretexts.dev` is running the demo-assistant image
  `assessment-ai:assistant-70d308c`, which is **`feat/demo-assistant`, not `main`**. See the warning
  below before you build anything.

---

## 6. Deploy state — RESOLVED, the divergence is closed

**Superseded as of 2026-07-26.** This section originally warned that production ran
`assistant-70d308c` from `feat/demo-assistant` while `main` lacked the chatbot, so any rebuild from
`main` would silently drop it. That is no longer true:

- **PR #9 is merged. `main` = `23f84df`.**
- Live image is **`assessment-ai:main-23f84df`** (`ac621fd134d7`), built from merged `main`.
- Rollback to the last pre-merge image: **`assessment-ai:rollback-20260726g`** (`efcbb40b7255`).
- Verified in the running container: `_SQL_IDENTIFIER_RE` and `_max_http_attempts` present (both
  were missing while production ran off the feature branch), `app/assistant/` present, `/healthz`
  reports `"assistant":"enabled"`, 12 conversations / 30 messages / 1 draft survived the rebuild.

**Building from `main` is now the correct thing to do**, and this branch is based on it.

Two rules that still stand, and one that was learned landing the merge:

1. Check the live image before any deploy — do not trust a written-down ID, including the ones
   above. Both prior handoff docs pinned an image and both went stale within the hour:
   ```bash
   ssh hostinger 'docker inspect --format "{{.Config.Image}}" assessment-ai'
   ```
2. **Never `docker compose build` in `/opt/libretexts/assessment-ai`** — that checkout is stale and
   missing modules. Build from a fresh release tree, deploy with `--no-build`, per
   `/opt/libretexts/assessment-ai/DEPLOY.md`.
3. **Merging a PR does not run the Docker gate.** Before merging anything into `main`, do a trial
   merge locally and run `docker build --target test` on the *merged* tree. PR #9 was landed this
   way — five zeros on the merge result before it touched `main` — precisely because PR #7 had
   previously left `main` undeployable over one blank line while every status showed green. There is
   still no PR CI for the gate.

---

## 7. Open question for the owner

Whether "Auto mix" should actually shuffle. Today it is a fixed rotation, and the name implies
otherwise. Fixing the checkbox lie makes the form honest about *what it sends*; it does not make
"Auto mix" an accurate name for *what it does*. That is a pipeline decision with test implications,
and nobody has asked for it — flagging it rather than acting on it.
