# A model's self-assessment informs the reviewer; it never gates publication

**Status:** accepted (2026-08-16)

ADR 0002 closed with an open thread: *"Specialist review is not a gate and never was. The review
form carries a specialist checkbox that is validated and then discarded — no column, no reviewer, no
timestamp. This ADR does not change that; it is tracked separately, because persisting it would be a
new gate rather than a restatement of an existing one."* This ADR closes it.

**The specialist-review acknowledgement is not persisted, and will not be. More generally: a field
the generator emits about its own output may inform a reviewer, but may never, on its own, satisfy
or block a publication precondition.** Two fields are in that class today —
`specialist_review_required` and `needs_human_verification` (`schemas.py`) — and the rule is stated
here so the third one does not have to rediscover it.

## Why not persist it

The obvious reading of #11 was that this is an unfinished gate: it sits in the same fieldset as two
persisted confirmations, and the item carries a flag specifically to drive it. Four things stood
against that reading, and together they were decisive.

**The trigger cannot be raised by a human.** `specialist_review_required` is set only by the
generator, against its own JSON schema. The edit endpoint refuses both lowering *and* raising it,
deliberately. So a persisted record would attest that *the model* thought a specialist was needed —
not that anyone agreed. Making it human-raisable is the precondition for ever persisting it, and
that is a separate change with its own reasoning.

**It is redundant for half of what it claims to cover.** The checkbox said "this clinical or
parameterized item." Parameterized and computation items already have a real gate:
`ComputationEvidenceAccepted` in the enumerated precondition list, backed by `ComputationAttestation`
— allowlisted subject, bound to an exact report hash, carrying a rationale, append-only, scoped to
an `edit_count`. Persisting the checkbox would build a second, weaker mechanism beside a correct one.
Its only non-redundant job was the *clinical* half.

**Persisting a self-assertion would be worse than persisting nothing.** The control asked the
reviewer to affirm their own qualification, with no allowlist, no second person and no rationale. A
stored row saying "the person already reviewing ticked a box claiming they were qualified" reads, in
an audit trail, exactly like specialist sign-off — while establishing strictly less than the
computation attestation beside it. Today's defect is a missing record; that would have been a
misleading one.

**The path has never been exercised.** Across both live databases when this was written: 380 corpus
drafts, of which one carries the flag and none is approved; 2 live drafts, neither flagged, both
published. The 422 refusal has never fired against a real approval. Persisting it properly means
building allowlist-checked identity infrastructure for a path with no traffic.

## What the defect actually was

Not that the value is discarded — that turns out to be correct. The defect is that a control which
records nothing was dressed as a peer of two constraint-backed confirmations: same fieldset, same
`Required human checks` legend, same visual weight, and an error message asserting that "a qualified
specialist must confirm this item."

So the fix is presentational, and the load-bearing part of it is one sentence rendered on the page:
**this acknowledgement is not recorded.** The ADR explains why; the UI is what actually prevents the
misreading, at the moment and place someone would make it.

## Considered options

| Option | Rejected because |
|---|---|
| Keep it transient, demote the control, state the policy (**chosen**) | — |
| Persist it as a new publication precondition | Requires a human-raisable trigger, allowlist-checked identity, a column, a timestamp and revision scoping — the `ComputationAttestation` shape, built a second time, for a path with no traffic and no consumer of the audit trail it would produce. |
| Persist the self-assertion as-is, without an allowlist | Manufactures an audit record that reads like specialist sign-off and is not one. Strictly worse than the missing record it replaces. |
| Remove the checkbox and the refusal entirely | Throws away the one part that works. The refusal does stop an approval, and it is the only thing putting the generator's flag in front of a human. |
| Move the check into `PUBLICATION_PRECONDITIONS` without persisting | Not available. Preconditions judge assembled evidence; there would be nothing stored to read. |

## Consequences

- **The retained 422 is an acknowledgement, not a review gate.** It blocks an approval rather than a
  publication, records nothing, and proves nothing. That is what keeps it consistent with the policy
  above: forcing the reviewer to see a self-assessment is informing them, emphatically. The wording
  on the page and in the error must not imply more than that.
- **The specialist control stays out of `PUBLICATION_PRECONDITIONS`, and that is now deliberate.**
  ADR 0002 made the list the enumeration of everything that must hold before publishing. This is not
  such a condition, so its absence is no longer an oversight to be tidied up later.
- **Rendering unticked on every fresh visit is correct.** #11 noted the checkbox re-checks only from
  submitted values after a validation error, unlike bloom and difficulty which re-check from stored
  state, and read that as the missing persistence showing through. It is: there is no stored value,
  and there should not be one.
- **`needs_human_verification` is covered by the policy but not by this change.** It is read by
  nothing at all today. Surfacing it to the reviewer as a read-only note is tracked separately, so
  that #11 stays scoped to the flag it was filed about.
  - *Done in #21 (2026-08-16):* the field reaches the reviewer as a note that attributes the claim
    to the generator, and nothing else. No control, no column, no precondition — the informing half
    of the policy without the gating half. Frequency was unmeasured when the ADR was written and is
    now measured: **1 of the 380 corpus drafts** carries it, and it is not the same item as the one
    carrying `specialist_review_required` (overlap: zero). So the note is rare enough to mean
    something when it appears, and it covers an item the specialist flag does not.
- **The door to persistence is not nailed shut, and its hinges are named.** If an accreditation or
  clinical-safety process ever needs this record, the work is: make the flag human-raisable, decide
  who counts as a specialist (the allowlist already exists for computation), add the column with
  reviewer identity and timestamp, scope it to the revision, and add one entry to
  `PUBLICATION_PRECONDITIONS`. Nothing here forecloses that; it declines to build it speculatively.
