# There is no "review gate" concept — there are publication preconditions

**Status:** accepted (2026-08-15)

The codebase and its documentation talk about "the three review gates," but nothing in the code
enumerates them. What exists is seven `blockers.append(...)` sites in `_collect_blockers`, of which
three are the ones people call gates and four are called preconditions — and nothing distinguishes
the two groups. **We are making *publication precondition* the structural concept: one enumerated
list of seven, each answering `evaluate(context) -> Blocker | None`. "Review gate" survives as
vocabulary for the subset satisfied by a recorded human decision, with no representation in code.**

## Why the three "gates" could not be unified as gates

They are three different shapes, not three instances of one thing:

| | Question review | Hint ladder | Computation |
|---|---|---|---|
| Stored as | two booleans on the draft, plus a status enum | a status string and a JSON map on a child table | **nothing — recomputed on every call** |
| Set by | a reviewer, via a form | a reviewer, via a form | nobody; it is evaluated from evidence |
| Invalidated by | an explicit clearing step | the approval simply stops matching the current revision | content hash comparison |
| Blocker | one literal reason | three literal reasons, feature-flagged | one dynamic reason, raised as an exception |

A registry over these three would need an adapter per gate, and each adapter would contain
essentially the whole gate — a shallow interface bought at the cost of a new indirection. The
honest common shape is broader and simpler: *this condition must hold before publishing, and here
is the blocker if it does not*. Seven things satisfy that, not three.

## Considered options

| Option | Rejected because |
|---|---|
| Seven publication preconditions, one interface (**chosen**) | — |
| A registry over all three gates, one adapter each | Each adapter would hold the entire gate; the interface adds a hop and hides nothing. |
| A registry over the two human-decision gates only, leaving computation separate | Preserves the split that caused the problem: the enumeration would still be partial, so "add a gate and forget the blocker" stays possible for anything outside it. |

## Consequences

- **`ck_publish_ready_requires_review_gates` now under-describes what it guards, deliberately.** It
  covers only the question-review booleans; hints and computation live in other tables and cannot
  be expressed in a single-row CHECK. There is no migration tooling in this repo — the schema is
  created by `create_all` plus a hardcoded additive-only block — so renaming the constraint would
  require a table rebuild. It keeps its misleading name. **The precondition list is the
  enumeration; the constraint is a partial belt-and-braces guard.**
- **Feature-flagged preconditions stay in the list and return no blocker when disabled.** A
  conditionally-assembled list would reintroduce a second place that decides which preconditions
  exist, which is where a future one would get forgotten.
- **The remaining desync inverts from silent to loud.** The review page still renders its checkboxes
  from hardcoded template markup, so adding a precondition enforces it at publish before the UI
  shows it. A reviewer then gets blocked with a stated reason rather than publishing past an
  unenforced gate — a bug report instead of a corrupt record. Closing the other half means giving
  the review template a prepared view value, which is separate work.
- **Specialist review is not a gate and never was.** The review form carries a specialist checkbox
  that is validated and then discarded — no column, no reviewer, no timestamp. This ADR does not
  change that; it is tracked separately, because persisting it would be a new gate rather than a
  restatement of an existing one.
