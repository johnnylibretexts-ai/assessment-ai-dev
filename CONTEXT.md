# Assessment AI

Generates assessment items with an LLM, holds them for human review, and publishes the approved
ones to ADAPT. This glossary fixes the vocabulary for that path.

## Language

### The item under review

**Draft**:
A generated assessment item held for review. Carries a revision counter that increments on every
edit.
_Avoid_: question (that's the content inside a draft), item, card

**Revision**:
One state of a draft's content. Every edit produces a new revision.
_Avoid_: version, edit

### Getting a draft published

**Publication**:
The record of a draft being sent to ADAPT — one per unique combination of content, destination,
licence, alignment, and exporter versions. Re-publishing identical inputs resumes the existing
publication rather than creating a second one.
_Avoid_: publish job, export, release

**Publication attempt**:
One recorded try at a single step of a publication, kept append-only so the history of a publication
survives its retries.
_Avoid_: try, run, log entry

**Publication step**:
One stage of the publish path, declared as data: its name, the state a success moves the publication
to, and its failure disposition. The publish path is the ordered list of them. The recovery taken
from the *unknown* state is not a step — it rejoins the sequence rather than holding a position in
it.
_Avoid_: stage, phase, action (that is the column a step's attempts are recorded under)

**Step body**:
The external call a publication step exists to make. Passed to the attempts module as a callable and
left where it lives, because one interface cannot honestly cover an HTTP create, a bridge request and
a local file write.
_Avoid_: handler, task, operation

**Failure disposition**:
The state a publication step's failure leaves behind, declared with the step rather than decided at
the call site. The steps deliberately disagree — an external write that may have landed is a
different situation from a local check that wrote nothing — and declaring it is what keeps that
disagreement visible instead of implied.
_Avoid_: rollback, error state

**Unreadable publication state**:
A publication whose recorded state is not one this build knows. Distinct from the *unknown* state,
which is a state this build does know and has a recovery path for. Unreadable means the record was
written by something else — a successor build, or a hand edit — so this build cannot say what has
already happened to it.
_Avoid_: unknown state (that's a different, recognised thing), invalid state, corrupt

**Publication tone**:
The visual grouping the publication history renders a record in, derived from its state rather than
being the state. A closed set written in this build, so the class it produces is always one the
stylesheet knows — which is the point, because an *unreadable publication state* carries a raw value
no selector could have named in advance. Colour groups; the headline is what names a state.
_Avoid_: status colour, state class (that is `state-*`, which carries the raw stored value), severity

**Publication precondition**:
A condition that must hold before a draft may be published. There is one enumerated set of them;
each either passes or emits a blocker. This is the structural concept — *gate* is a property some
preconditions have, not a separate mechanism.
_Avoid_: gate (as the general term), check, validation, guard

**Review gate**:
A publication precondition that is satisfied by a recorded human decision, as opposed to one
evaluated from evidence or configuration. A narrowing of *publication precondition*, never a
synonym for it.
_Avoid_: approval step, sign-off

**Blocker**:
What an unsatisfied publication precondition emits: the reason a draft cannot be published, in
terms a reviewer can act on.
_Avoid_: error, failure, violation

**Revision-scoped approval**:
The rule that an approval is only valid for the revision it approved. Editing a draft invalidates
approvals recorded against the previous revision.
_Avoid_: stale approval, expiry

### Evidence

**Attestation**:
A named person's recorded assertion about a draft, supplied as input to a precondition rather than
satisfying one directly.
_Avoid_: approval (that's a review gate), sign-off

**Reviewer**:
The authenticated person acting on a draft, identified by their SSO subject.
_Avoid_: user, author, editor

**Computation specialist**:
The allowlisted, identified person whose *attestation* binds to an exact validation-report hash.
"Specialist" unqualified means this one — the word is earned here by the allowlist and the binding,
and belongs to no other role on the review page.
_Avoid_: specialist (unqualified, when the *specialist-review flag* is meant), reviewer

**Model self-assessment**:
A field the generator emits about its own output. It may inform a *reviewer*; it may never, on its
own, satisfy or block a *publication precondition* — nothing can verify it and no human can
override it.
_Avoid_: confidence, flag, self-report

**Specialist-review flag**:
The *model self-assessment* that an item is clinical or parameterized and warrants specialist
attention. Set only at generation and editable by nobody. It is answered by an acknowledgement on
the review form, which is not recorded: no column, no *reviewer*, no timestamp. It refuses an
approval until it is seen; it establishes nothing about who saw it.
_Avoid_: specialist review (implies a recorded review), specialist gate, specialist confirmation
