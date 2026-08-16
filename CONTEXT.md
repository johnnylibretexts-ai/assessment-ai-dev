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
