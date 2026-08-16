# An unreadable publication state refuses to publish rather than resuming

**Status:** accepted (2026-08-15)

A publication's `state` is a plain `String(30)` with no CHECK constraint, so a value outside
`PublicationState` is representable. The resume dispatch matches four recognised states and lets
everything else fall through into the full publish path, which sends a **second** `adapt_create` for
a question ADAPT already holds and repoints the record at it, stranding the first. Nobody chose
that; it is where the `if` chain happens to end. **A state this build does not recognise now raises
`PublicationValidationError` instead, leaving the record untouched.**

## Why this is a live scenario and not a hypothetical

The way an unreadable state reaches production is a **rollback**: a newer build writes a state its
enum has and the older one does not, then the service is rolled back to the older image. This
project deploys by image swap with documented rollback tags and has **no migration tooling**, so
that sequence is the normal operating procedure, not an accident. A hand-edited row gets there too.

## Why not reconcile, which is what the neighbouring case does

`_reconcile` handles `PublicationState.UNKNOWN` by asking ADAPT whether the question exists, adopting
it when it does, and never blind-creating. Routing unreadable states there would recover
automatically and reuse a tested path, which is the tempting option.

It was rejected because the two cases are not the same question. `UNKNOWN` means *we do not know what
ADAPT did*. An unreadable state means *we do not know what our own successor did* — and reconciling
would let an old build adopt the question and drive the publication to `SUCCEEDED`, completing a
publication whose newer semantics it cannot see. A publication record that claims more than it did is
worse than one that is stuck, because only the second kind gets noticed.

| Option | Rejected because |
|---|---|
| Refuse, leave the record untouched (**chosen**) | — |
| Treat it as `UNKNOWN` and reconcile | An old build finishes work a newer build started and it cannot interpret. |
| Keep the fall-through | Duplicate `adapt_create`, orphaned question, record silently repointed. |
| A CHECK constraint so the value is unrepresentable | The constraint is created with the table, so it would encode the *old* enum and reject the *newer* build's legitimate writes — breaking the upgrade to prevent a rollback. Also needs a table rebuild, per ADR 0002. |

## Consequences

- **The refusal writes nothing.** Not even `failed`. Marking the row would overwrite a successor's
  state with a guess, destroy the evidence an operator needs, and silently convert the refusal into
  the `failed` in-place retry — which republishes from the start, the behaviour being removed.
- **No publication attempt is recorded.** A *publication attempt* is one recorded try at one step;
  a refusal tries no step, and the append-only log is only worth reading if every row is a real try.
- **This is not a publication precondition**, though the enumerated list is where a reader will look
  first. `publication_key` is derived from the resolved alignment, hint snapshot, computation
  decision and engine binding, so it is not known until an ADAPT `resolve_destination` call has
  happened — far past readiness. A precondition could only block on *any* unreadable row for the
  draft, which would over-block: a row for an earlier revision has a different key, would never
  enter the dispatch, and would block the current revision forever with nothing the reviewer could
  do about it.
- **Scope is out-of-enum only.** A recognised state whose columns contradict it already fails closed
  — `_continue_after_adapt` raises when `adapt_question_id` is missing. Closing the fall-through
  makes the publish path uniform: it refuses whenever it is confused.
- **During a rollback window, affected drafts are stuck** until someone looks. That is the accepted
  cost, and the reason the message names the offending state.
