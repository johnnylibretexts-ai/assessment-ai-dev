# Item types, contexts, engines, and hints

## Item types

Which of these are offered depends on whether advanced items and parameterized items are enabled on
this deployment.

**Basic**

- `multiple_choice` — one stem, several choices, exactly one correct.
- `true_false` — a claim the student judges.
- `numerical` — a numeric response, checked against a value and tolerance rather than by string
  match.

**Selection**

- `multiple_response` — several choices, more than one correct.
- `select_all` — the student must find every correct option.
- `select_n` — the student must pick exactly N, and N is stated.
- `select_choice` — an inline choice embedded in running text.
- `dropdown` — one or more dropdowns inside a passage.

**Construction and arrangement**

- `fill_in_blank` — free text into a gap, matched against accepted responses.
- `matching` — pair items from two sets.
- `ordering` — put items in the correct sequence.
- `drag_drop_cloze` — drag tokens into gaps in a passage.

**Interaction with a stimulus**

- `image_hotspot` — click a region of an image. Requires an eligible source image.
- `highlight_text` — select the relevant span of a passage.
- `highlight_table` — select the relevant cells of a table.
- `matrix` — a grid of related judgements.
- `bow_tie` — a clinical-reasoning shape: a central problem with contributing factors on one side
  and actions on the other.

**Parameterized (external engines)**

- `webwork` — rendered by the self-hosted WeBWorK renderer.
- `imathas` — rendered through the IMathAS bridge.

## Item context types

Separately from its type, an item has a *context* describing how it's framed:

- `standard` — a plain question.
- `scenario` — set in a short situation.
- `case` — a longer case with more detail.
- `shared_stimulus` — several items share one passage, table, or figure.
- `difficulty_variant` — a deliberate easier/harder sibling of another item.

## What "parameterized" means

A parameterized item is not one fixed question — it's a template with variables. Each student gets
different numbers, and the engine computes the correct answer for their particular draw. That's why
WeBWorK and IMathAS items need a live engine: the answer isn't stored, it's computed.

Because they execute, they carry more constraints than a plain multiple-choice item. Generated
parameterized content goes through the same structured validation as everything else, and external
engine source is rendered only from fixed server-side templates — a model never supplies executable
code.

## The graduated hint ladder

When hint generation is on, an item can carry up to three rungs, meant to be revealed in order:

1. **Conceptual** — points at the idea the student needs, without touching this specific question.
   *"Think about what happens to the equilibrium when you add product."*
2. **Strategic** — names the approach or procedure to use. *"Set up an ICE table and compare Q to
   K."*
3. **Specific** — narrows to this question's particulars, still without giving the answer. *"You're
   looking at the effect on the reverse reaction rate specifically."*

Each rung cites source paragraphs, exactly like the item does. The system runs an answer-leak check
and refuses a rung that reveals the correct response — a hint that says "so the answer is C" fails
that check by design. Hints are reviewed and approved separately from the question.
