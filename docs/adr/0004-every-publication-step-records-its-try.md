# Every publication step records its try, including the two that advance nothing

**Status:** accepted and implemented (2026-08-16) via
[#14](https://github.com/johnnylibretexts-ai/assessment-ai-dev/issues/14). The recording is in
`run_publication_step` in `app/publication_attempts.py`;
`test_a_step_that_advances_nothing_still_records_the_try`,
`test_a_step_that_advances_nothing_keeps_the_evidence_it_reports` and
`test_imathas_question_id_reaches_the_adapt_payload_and_its_attempt` pin the three halves.

`qti_preflight` and `imathas_create` used to write **no** `publication_attempts` row when they
passed. Every other step wrote one on both branches. The log of a successful IMathAS publication
therefore read `adapt_create, qti_finalize` — with no trace that the bridge had been called at all,
even though that call had created a question in IMathAS. **The decision is that every step records
its try, and a step that advances nothing records it under the state the publication is already
in.**

This closes a contradiction with the project's own vocabulary. `CONTEXT.md` defines a *publication
attempt* as "one recorded try at a single step of a publication, kept append-only so the history of
a publication survives its retries". A preflight that passed is a try that happened. ADR 0003 argues
the converse — that the log is only worth reading if every row is a real try — and the same
reasoning runs in this direction: it is only worth reading as a history if every real try is a row.
The glossary needed no edit; the code moved to meet it.

## What `reaches=None` used to decide

A single `None` on `PublicationStep.reaches` silently owned two unrelated facts: *this step advances
no state*, and *this step records nothing*. Nothing forced them together — they arrived as one value
because one value was cheap. `reaches` now says only the first. This is the same defect shape
[#2](https://github.com/johnnylibretexts-ai/assessment-ai-dev/issues/2) removed elsewhere in this path,
one level down.

## Why the row carries the state the publication was already in

`publication_attempts.resulting_state` is `String(30)`, `nullable=False`, and this repo has no
migration tooling (ADR 0002), so a non-advancing step must name *some* state. It records the one the
publication is in — which is not a placeholder standing in for a missing answer. It makes the column
mean one consistent thing everywhere: **the state the publication is in after this try**. That was
already what it held for every advance and for every failure disposition.

| Option | Rejected because |
|---|---|
| Record under the state the publication is in (**chosen**) | — |
| Make `resulting_state` nullable for these rows | A schema change, which this repo has no tooling for. |
| A sentinel state such as `checked` | Invents a publication state that no publication is ever in, to describe a step that deliberately moves none. |
| Keep the silence | The two steps that leave no trace include the one making an external write. |

## Consequences

- **The IMathAS question ID is now written down somewhere.** No publication column holds it —
  `Publication` has `adapt_question_id` and no engine equivalent — so before this it reached the
  ADAPT payload through a `nonlocal` and was then discarded. Its attempt row is now the only durable
  record of which question the bridge made. This is why a step that advances nothing may report a
  `response`: the row exists to hang evidence on.
- **`values` are still refused from such a step.** A response is evidence and belongs on the attempt;
  values are columns on the publication, and a step that moves the publication nowhere makes no write
  to put them in. Discarding them silently would lose something the body meant to persist.
- **A resume does not re-run the preflight, so it does not double the row.** Resume dispatch reads
  `steps_after` and re-enters after the step the state names, which is before the preflight body is
  ever reached. A retry from `failed` does re-run it, and gets a second row — correctly, because it
  is a second try.
- **Five existing publishing tests changed.** All five gained exactly one row at the front of a
  sequence, none lost or reordered a step. Unlike the [#2](https://github.com/johnnylibretexts-ai/assessment-ai-dev/issues/2)
  refactor — whose whole evidence was that the publishing suite passed *unmodified* — this is a
  deliberate behaviour change, so editing those assertions is the correct outcome rather than a
  signal that a seam is wrong.
- **Rows accumulate slightly faster.** One extra row per publish, two for IMathAS. The objection that
  this is noise on the common path was weighed and rejected: nothing reads the log for volume, and
  the one thing it is read for — reconstructing what happened — is exactly what completeness serves.
- **The log is still write-only in production.** `Publication.attempts` is eagerly loaded but no
  route or template renders it, so the value of this change is forensic, realised the first time
  someone reads the table during an incident. That asymmetry is the argument for doing it now: rows
  missed cannot be added retroactively.
- **No schema change.** `response_json` was already nullable on the attempt row.
